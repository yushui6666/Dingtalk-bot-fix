"""主链路集成测试：消息 → 建单 → 推进 → 完成 → 确认 → 死信。

覆盖 v4.0 核心主功能（Task 5-11），关键词快路径 + 注入的假分类器。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from db import Database
from models import ImageAttachment, NormalizedMessage
from notifier import Notifier
from pipeline import MessageProcessingPipeline, RuntimeMode
from routing.pending_actions import PendingActionService
from routing.ticket_contexts import TicketContextStore
from routing.ticket_router import TicketRouter
from semantics.protocol_loader import load_protocol
from semantics.types import CommandResult, PendingActionStatus, SemanticDecision
from tickets.executor import RESULT_INTERNAL_ERROR, TicketCommandExecutor
from tickets.repository import TicketRepository

_PROTOCOL_PATH = Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"

GROUP = {"group_id": "G1", "store_name": "测试群（示例）",
         "manager_ids": ["uid-mgr"], "engineer_ids": ["uid-eng"], "other_member_ids": []}


class FakeClassifier:
    """按 message_id 返回预设语义决策；未预设则尝试本地解析 # 语法（模拟全AI对 # 的理解），否则返回 chat.ignore。"""

    def __init__(self, protocol=None) -> None:
        self.responses: dict[str, SemanticDecision] = {}
        self.protocol = protocol

    async def classify(self, message, candidates=None, pending_action=None, history=None) -> SemanticDecision:
        if message.message_id in self.responses:
            return self.responses[message.message_id]
        # 模拟全AI对 # 语法的理解：复用 keyword_matcher 但标记为 SEMANTIC_MODEL
        text = (message.content or "").strip()
        if text.startswith("#") and self.protocol is not None:
            try:
                from semantics.keyword_matcher import match_keyword as _mk
                kw_decision = _mk(text, self.protocol)
                if kw_decision is not None and kw_decision.intent != "system.clarify":
                    # 将 keyword 来源改为 SEMANTIC_MODEL 以符合全AI架构（本地校验仍一致）
                    return SemanticDecision(
                        protocol_version=kw_decision.protocol_version,
                        source="SEMANTIC_MODEL",
                        intent=kw_decision.intent,
                        target_ticket_no=kw_decision.target_ticket_no,
                        intent_confidence=0.95,
                        fields=dict(kw_decision.fields),
                        missing_fields=kw_decision.missing_fields,
                        candidate_scores=kw_decision.candidate_scores,
                        evidence=kw_decision.evidence,
                        requires_confirmation=kw_decision.requires_confirmation,
                    )
                elif kw_decision is not None:
                    return SemanticDecision(
                        protocol_version=kw_decision.protocol_version,
                        source="SEMANTIC_MODEL",
                        intent=kw_decision.intent,
                        target_ticket_no=kw_decision.target_ticket_no,
                        intent_confidence=0.9,
                        fields=dict(kw_decision.fields),
                        missing_fields=kw_decision.missing_fields,
                        evidence=kw_decision.evidence,
                    )
            except Exception:
                pass
            # 裸时效补充（如 "3天"）在草稿场景下由 pipeline 本地兜底，此处默认忽略
            # 但若内容本身就是时效词，尝试返回 ticket.create 供补充逻辑合并
            import re as _re
            if _re.fullmatch(r"\s*(1天|3天|7天|待商榷)\s*", text):
                return SemanticDecision(
                    protocol_version="4.0.0", source="SEMANTIC_MODEL",
                    intent="ticket.create", target_ticket_no=None,
                    intent_confidence=0.8, fields={"sla": text.strip()},
                )
            # 选择数字（2/第二个）由 pipeline 本地快路径处理，此处返回 chat.ignore 即可
        # 裸订单号场景（全AI下裸单号应识别为 repair_plan.submit）— 由 pipeline supplement 兜底，但此处也模拟
        import re as _re2
        order_tokens = _re2.findall(r"(?<![A-Za-z0-9-])[A-Za-z0-9-]{6,64}(?![A-Za-z0-9-])", text or "")
        orders = [
            t for t in order_tokens
            if sum(ch.isdigit() for ch in t) >= 6
            and not _re2.fullmatch(r"1[3-9]\d{9}", t)
        ]
        if orders and any(kw in text for kw in ("订单", "单号", "采购", "TB-")) or (len(orders) == 1 and len(text.strip()) < 30 and orders[0] in text):
            # 仅当上下文中有活动工单时才视为提交订单，避免误判手机号
            if candidates:
                fields = {"order_no": orders[0], "order_nos": orders}
                # 尝试提取诊断
                return SemanticDecision(
                    protocol_version="4.0.0", source="SEMANTIC_MODEL",
                    intent="ticket.repair_plan.submit", target_ticket_no=None,
                    intent_confidence=0.9, fields=fields,
                )
        return SemanticDecision(protocol_version="4.0.0", source="SEMANTIC_MODEL",
                             intent="chat.ignore", target_ticket_no=None, intent_confidence=0.0)


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    db.upsert_group(GROUP)
    protocol = load_protocol(_PROTOCOL_PATH)
    repo = TicketRepository(db)
    router = TicketRouter()
    context = TicketContextStore(db)
    pending = PendingActionService(db)
    executor = TicketCommandExecutor(db, repo)
    sent: list[str] = []
    notifier = Notifier(db, lambda target, text: sent.append(text))
    classifier = FakeClassifier(protocol=protocol)

    def make_pipeline(
        mode=RuntimeMode.PRODUCTION,
        max_attempts=3,
        classifier_override=classifier,
    ):
        return MessageProcessingPipeline(
            db=db, repo=repo, protocol=protocol, router=router, context=context,
            pending=pending, executor=executor, notifier=notifier,
            classifier=classifier_override, mode=mode, max_attempts=max_attempts,
        )

    async def process(text, message_id, role="MANAGER", sender="uid-mgr", pipeline=None):
        msg = NormalizedMessage(
            message_id=message_id, group_id="G1", sender_id=sender, sender_name="u",
            content=text, message_type="text", sent_at=datetime.now(), sender_role=role,
        )
        db.enqueue_message(msg)
        row = db.connect().execute(
            "SELECT * FROM inbox_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        return await (pipeline or make_pipeline()).process(dict(row))

    yield SimpleNamespace(
        db=db, sent=sent, classifier=classifier, process=process,
        make_pipeline=make_pipeline, context=context, executor=executor,
    )
    db.close()


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _active(db: Database) -> list[dict]:
    return db.list_active_tickets("G1")


def _ticket(db: Database, ticket_no: str) -> dict:
    t = db.get_ticket_by_no(ticket_no)
    assert t is not None, f"工单不存在: {ticket_no}"
    return t


@pytest.mark.asyncio
async def test_create_ticket_via_keyword(env):
    status = await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    assert status == "COMPLETED"
    t = _ticket(env.db, "测试群（示例）-收银机-1天-001")
    assert t["status"] == "ACTIVE" and t["version"] == 1
    assert t["sla_days"] == 1
    assert t["current_deadline_at"] == t["initial_deadline_at"]  # 创建时截止=初始截止
    assert t["current_deadline_at"] > datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 2026-08-24 一行式建单回执（驱动工程师响应，保留）
    assert any("已建单" in s and "收银机" in s for s in env.sent)


@pytest.mark.asyncio
async def test_create_requires_sla(env):
    """时效已设为必填：报修未写时效 → 拒绝建单，不静默默认（业务决策 2026-08-19）。"""
    status = await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机", "m1")
    assert status == "COMPLETED"  # 收件箱终态；实际结果看 processed_result
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m1'"
    ).fetchone()
    assert row["processed_result"] == "REJECTED"
    act = _active(env.db)
    assert len(act) == 0


@pytest.mark.asyncio
async def test_create_with_explicit_sla_respected(env):
    """用户明确写时效时按用户值。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：7天", "m1")
    act = _active(env.db)
    assert act[0]["sla_days"] == 7


@pytest.mark.asyncio
async def test_engineer_create_rejected(env):
    await env.process("#报修\n主题：A\n位置：1\n问题描述：x\n时效：3天", "m1")
    status = await env.process("#报修\n主题：B\n位置：2\n问题描述：y\n时效：3天",
                               "m2", role="ENGINEER", sender="uid-eng")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'").fetchone()
    assert row["processed_result"] == "REJECTED"
    assert len(_active(env.db)) == 1


@pytest.mark.asyncio
async def test_multiple_active_tickets_same_group(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    await env.process("#报修\n主题：门锁\n位置：二楼\n问题描述：打不开\n时效：3天", "m2")
    act = _active(env.db)
    assert len(act) == 2
    assert {t["ticket_no"] for t in act} == {"测试群（示例）-收银机-1天-001", "测试群（示例）-门锁-3天-002"}


@pytest.mark.asyncio
async def test_add_detail_and_complete(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    await env.process("#补充 工单编号：测试群（示例）-收银机-1天-001 内容：屏幕闪烁", "m2")
    t = _ticket(env.db, t1["ticket_no"])
    assert "屏幕闪烁" in t["problem_description"] and t["version"] == 2
    await env.process("#完毕 工单编号：测试群（示例）-收银机-1天-001", "m3")
    assert _ticket(env.db, t1["ticket_no"])["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_responsibility_switches_via_executor(env):
    """计划书 §9.3：建单(店长)等工程师 → 工程师补充后切等店长 → 完成关闭周期。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t = _ticket(env.db, "测试群（示例）-收银机-1天-001")
    assert t["waiting_side"] == "ENGINEER_SIDE"
    assert t["waiting_since"] is not None
    # 工程师补充 → 切到店长方
    await env.process("#补充 工单编号：测试群（示例）-收银机-1天-001 内容：确认故障", "m2",
                      role="ENGINEER", sender="uid-eng")
    t = _ticket(env.db, "测试群（示例）-收银机-1天-001")
    assert t["waiting_side"] == "MANAGER_SIDE"
    rows = env.db.connect().execute(
        "SELECT status FROM responsibility_cycles WHERE ticket_id=?", (t["id"],)).fetchall()
    assert rows[0]["status"] == "CANCELLED"  # 旧周期已关闭
    # 完成 → 全部未决周期关闭
    await env.process("#完毕 工单编号：测试群（示例）-收银机-1天-001", "m3")
    rows = env.db.connect().execute(
        "SELECT status FROM responsibility_cycles WHERE ticket_id=?", (t["id"],)).fetchall()
    assert all(r["status"] == "CANCELLED" for r in rows)


@pytest.mark.asyncio
async def test_select_context_then_route_by_context(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    await env.process("#报修\n主题：门锁\n位置：二楼\n问题描述：打不开\n时效：3天", "m2")
    await env.process("#选择工单 测试群（示例）-门锁-3天-002", "m3", role="ENGINEER", sender="uid-eng")
    # 双候选 + 无编号的补充 → 走用户上下文归属门锁
    await env.process("#补充 内容：换了新锁芯", "m4", role="ENGINEER", sender="uid-eng")
    t = _ticket(env.db, "测试群（示例）-门锁-3天-002")
    assert "新锁芯" in t["problem_description"]
    other = _ticket(env.db, "测试群（示例）-收银机-1天-001")
    assert "新锁芯" not in other["problem_description"]


@pytest.mark.asyncio
async def test_multi_candidate_without_number_clarifies(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    await env.process("#报修\n主题：门锁\n位置：二楼\n问题描述：打不开\n时效：3天", "m2")
    status = await env.process("#补充 内容：又出问题了", "m3")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m3'").fetchone()
    assert row["processed_result"] == "CLARIFY"
    assert any("多张工单" in s for s in env.sent)


@pytest.mark.asyncio
async def test_natural_language_without_model_ignored(env):
    status = await env.process("麻烦帮我查一下现在的工单情况", "m1")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m1'").fetchone()
    assert row["processed_result"] == "IGNORED"


@pytest.mark.asyncio
async def test_model_complete_executes_directly(env):
    """业务决策（2026-08-12）：自然语言说「修好了」即直接完成，不再强制二次确认。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    # 模型说“完成”→ 协议已改 NOT_REQUIRED → 直接执行
    env.classifier.responses["m2"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.complete",
        target_ticket_no=t1["ticket_no"], intent_confidence=0.95)
    await env.process("维修已经完成了", "m2")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'").fetchone()
    assert row["processed_result"] == "EXECUTED"
    assert _ticket(env.db, t1["ticket_no"])["status"] == "COMPLETED"
    # 未创建待确认
    assert env.db.get_waiting_pending("G1", "uid-mgr") is None
    assert any("已完成" in s for s in env.sent)


@pytest.mark.asyncio
async def test_suffix_number_routes_to_correct_ticket(env):
    """同群多张同主题工单时，消息里的短编号（如「002」）归到对应那张（2026-08-13）。"""
    await env.process("#报修\n主题：博物馆奇妙夜\n位置：一楼\n问题描述：门坏了\n时效：1天", "m1")
    await env.process("#报修\n主题：博物馆奇妙夜\n位置：二楼\n问题描述：门又坏了\n时效：3天", "m2")
    act = _active(env.db)
    assert len(act) == 2
    t002 = next(t for t in act if t["ticket_no"].endswith("-002"))
    # 模型识别为完成但没带编号；消息里的「002」应路由到 3天-002
    env.classifier.responses["m3"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.complete",
        target_ticket_no=None, intent_confidence=0.95)
    await env.process("002完成了", "m3")
    assert _ticket(env.db, t002["ticket_no"])["status"] == "COMPLETED"
    other = next(t for t in act if t["id"] != t002["id"])
    assert _ticket(env.db, other["ticket_no"])["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_number_reply_selects_nth_candidate_locally(env):
    """AI 列候选后回「2」→ 本地直接选第 2 个候选，不调模型（2026-08-13）。"""
    await env.process("#报修\n主题：博物馆奇妙夜\n位置：一楼\n问题描述：门坏了\n时效：1天", "m1")
    await env.process("#报修\n主题：博物馆奇妙夜\n位置：二楼\n问题描述：门又坏了\n时效：3天", "m2")
    act = _active(env.db)
    assert len(act) == 2
    await env.process("2", "m3")
    ctx = env.context.get_active("G1", "uid-mgr", datetime.now())
    assert ctx == act[1]["id"]  # 第 2 个候选
    # 选单成功需回执确认（用户要求 2026-08-25）
    assert any("已切换到工单" in s for s in env.sent)
    # 中文「第二个」同样生效
    await env.process("第一个", "m4")
    ctx2 = env.context.get_active("G1", "uid-mgr", datetime.now())
    assert ctx2 == act[0]["id"]


# ── 多候选归属问询：短编号识别与一次多选（规格 docs/superpowers/specs/
#    2026-08-27-multi-ticket-assign-design.md，用户决策 2026-08-27）──


async def _three_tickets_then_clarify(env):
    """建三张不同主题工单并发一条多候选补充 → 触发归属问询 pending。"""
    await env.process("#报修\n主题：古堡秘事\n位置：一楼\n问题描述：门锁失灵\n时效：1天", "c1")
    await env.process("#报修\n主题：勇者斯巴达\n位置：二楼\n问题描述：灯不亮\n时效：3天", "c2")
    await env.process("#报修\n主题：财阀继承人\n位置：三楼\n问题描述：音效断续\n时效：7天", "c3")
    act = _active(env.db)
    assert len(act) == 3
    status = await env.process("#补充 内容：追加检查记录MARK", "m-cl")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m-cl'").fetchone()
    assert row["processed_result"] == "CLARIFY"
    assert any("多张工单" in s for s in env.sent)
    return [t["ticket_no"] for t in act]


def _stub_select(env, message_id: str, target_no: str | None) -> None:
    """模拟真实模型行为：把用户消息里的编号原样回显进 ticket_no。"""
    env.classifier.responses[message_id] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.select", target_ticket_no=target_no,
        intent_confidence=0.9,
    )


@pytest.mark.asyncio
async def test_short_suffix_select_assigns_correct_ticket(env):
    """归属问询后回裸短编号「00X」→ 归到尾缀对应那张（截图事故回归）。"""
    nos = await _three_tickets_then_clarify(env)
    third = nos[2]
    _stub_select(env, "m-sel", third.rsplit("-", 1)[-1])  # 用户写「003」而非全编号
    status = await env.process(third.rsplit("-", 1)[-1], "m-sel")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m-sel'").fetchone()
    assert row["processed_result"] == "EXECUTED"
    t3 = _ticket(env.db, third)
    assert "追加检查记录MARK" in t3["problem_description"]
    for other in nos[:2]:
        assert "追加检查记录MARK" not in _ticket(env.db, other)["problem_description"]
    assert env.db.get_waiting_pending("G1", "uid-mgr") is None


@pytest.mark.asyncio
async def test_multi_short_codes_assign_all(env):
    """一次回多个编号「00X 00Y」→ 两张各落地一次（用户决策：支持读取多个）。"""
    nos = await _three_tickets_then_clarify(env)
    reply = f"{nos[0].rsplit('-', 1)[-1]} {nos[2].rsplit('-', 1)[-1]}"
    _stub_select(env, "m-sel", reply)
    await env.process(reply, "m-sel")
    got = [n for n in nos if "追加检查记录MARK" in _ticket(env.db, n)["problem_description"]]
    assert got == [nos[0], nos[2]]
    assert env.db.get_waiting_pending("G1", "uid-mgr") is None


@pytest.mark.asyncio
async def test_unknown_token_rejects_whole_batch_atomically(env):
    """混入无法识别的编号 → 整批拒绝零落地，指出坏段；pending 存活可重选。"""
    nos = await _three_tickets_then_clarify(env)
    reply = f"{nos[0].rsplit('-', 1)[-1]} 999"
    _stub_select(env, "m-sel", reply)
    status = await env.process(reply, "m-sel")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m-sel'").fetchone()
    assert row["processed_result"] == "REJECTED"
    for n in nos:
        assert "追加检查记录MARK" not in _ticket(env.db, n)["problem_description"]
    assert any("没有找到对应工单" in s and "999" in s for s in env.sent)
    assert env.db.get_waiting_pending("G1", "uid-mgr") is not None
    # 重选合法单号仍可成功
    second = nos[1]
    _stub_select(env, "m-sel2", second.rsplit("-", 1)[-1])
    await env.process(second.rsplit("-", 1)[-1], "m-sel2")
    assert "追加检查记录MARK" in _ticket(env.db, second)["problem_description"]


@pytest.mark.asyncio
async def test_version_conflict_rejects_batch_before_executing(env):
    """其中一张候选版本已被推进 → 整体拒绝、两张都不执行。"""
    nos = await _three_tickets_then_clarify(env)
    # 预检前先显式编号补充第 0 张，使其版本离开快照值
    await env.process(f"#补充 内容：预检前变动\n工单编号:{nos[0]}", "m-bump")
    reply = f"{nos[0].rsplit('-', 1)[-1]} {nos[1].rsplit('-', 1)[-1]}"
    _stub_select(env, "m-sel", reply)
    status = await env.process(reply, "m-sel")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m-sel'").fetchone()
    assert row["processed_result"] == "REJECTED"
    assert any("状态已更新" in s for s in env.sent)
    t1 = _ticket(env.db, nos[1])
    assert "追加检查记录MARK" not in t1["problem_description"]
    assert "追加检查记录MARK" not in _ticket(env.db, nos[0])["problem_description"]
    assert env.db.get_waiting_pending("G1", "uid-mgr") is not None


@pytest.mark.asyncio
async def test_index_tokens_in_one_message_assign_both(env):
    """回展示序号「1 3」→ 第 1 与第 3 张各落地（序号兜底语义）。"""
    nos = await _three_tickets_then_clarify(env)
    _stub_select(env, "m-sel", "1 3")
    await env.process("1 3", "m-sel")
    got = [n for n in nos if "追加检查记录MARK" in _ticket(env.db, n)["problem_description"]]
    assert got == [nos[0], nos[2]]


@pytest.mark.asyncio
async def test_duplicate_tokens_dedupe_single_apply(env):
    """重复编号「00X 00X」去重，目标单只落一次。"""
    nos = await _three_tickets_then_clarify(env)
    dup = nos[0].rsplit("-", 1)[-1]
    reply = f"{dup} {dup}"
    _stub_select(env, "m-sel", reply)
    await env.process(reply, "m-sel")
    first = _ticket(env.db, nos[0])
    assert first["problem_description"].count("追加检查记录MARK") == 1


@pytest.mark.asyncio
async def test_model_cancel_still_requires_confirmation(env):
    """取消/重开仍保持确认策略（高危误操作防护）。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    env.classifier.responses["m2"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.cancel",
        target_ticket_no=t1["ticket_no"], intent_confidence=0.95,
        fields={"cancel_reason": "误报"})
    await env.process("帮我把这个误报工单取消掉", "m2")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'").fetchone()
    assert row["processed_result"] == "WAITING_CONFIRMATION"
    assert _ticket(env.db, t1["ticket_no"])["status"] == "ACTIVE"
    # 确认提示用中文可读文案，不暴露 intent ID
    assert any("确认执行「取消工单」" in s for s in env.sent)
    assert not any("ticket.cancel" in s for s in env.sent)


@pytest.mark.asyncio
async def test_system_clarify_shows_readable_message(env):
    """system.clarify（消息有歧义）→ 直接可读澄清提示，不建「确认执行」待办。"""
    env.classifier.responses["m1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="system.clarify",
        target_ticket_no=None, intent_confidence=0.9,
        evidence=("报修", "完毕"))
    await env.process("先报修一下门坏了，然后又完毕了", "m1")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m1'").fetchone()
    assert row["processed_result"] == "CLARIFY"
    # 不创建待确认，也不出现「确认执行：system.clarify」
    assert env.db.get_waiting_pending("G1", "uid-mgr") is None
    assert any("有歧义" in s for s in env.sent)
    assert not any("system.clarify" in s for s in env.sent)


@pytest.mark.asyncio
async def test_model_failure_retries_then_dead_letter(env):
    # 未配置模型的聊天消息 → 分类器返回降级 fallback → 重试 → 死信
    def failing_classify(message, candidates=None, pending_action=None):
        raise RuntimeError("network down")

    # 用失败分类器替换
    env.classifier.classify = failing_classify
    pl = env.make_pipeline(max_attempts=3)
    msg = NormalizedMessage(message_id="m1", group_id="G1", sender_id="uid-mgr",
                            sender_name="u", content="随便聊聊", message_type="text",
                            sent_at=datetime.now(), sender_role="MANAGER")
    env.db.enqueue_message(msg)
    for i in range(3):
        row = env.db.connect().execute(
            "SELECT * FROM inbox_messages WHERE message_id='m1'").fetchone()
        status = await pl.process(dict(row))
        final_row = env.db.connect().execute(
            "SELECT status, attempts FROM inbox_messages WHERE message_id='m1'").fetchone()
        print(f"  重试 {i + 1}: status={final_row['status']} attempts={final_row['attempts']}")
        if final_row["status"] == "DEAD_LETTER":
            break
    final = env.db.connect().execute(
        "SELECT status, attempts, processed_result FROM inbox_messages WHERE message_id='m1'").fetchone()
    assert final["status"] == "DEAD_LETTER"
    assert final["attempts"] == 3


@pytest.mark.asyncio
async def test_shadow_mode_records_but_does_not_execute(env):
    pl = env.make_pipeline(mode=RuntimeMode.SHADOW)
    msg = NormalizedMessage(message_id="m1", group_id="G1", sender_id="uid-mgr",
                            sender_name="u", content="#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天",
                            message_type="text", sent_at=datetime.now(), sender_role="MANAGER")
    env.db.enqueue_message(msg)
    row = env.db.connect().execute("SELECT * FROM inbox_messages WHERE message_id='m1'").fetchone()
    await pl.process(dict(row))
    assert len(_active(env.db)) == 0
    dec = env.db.connect().execute(
        "SELECT intent FROM semantic_decisions WHERE message_id='m1'").fetchone()
    assert dec is not None and dec["intent"] == "ticket.create"


# ───────────────────────── #停止维修（v4.2） ─────────────────────────


@pytest.mark.asyncio
async def test_leader_can_perform_engineer_actions(env):
    """LEADER 为超集角色：兼任工程负责人的工程师仍可提交维修方式/订单号（不降权）。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    status = await env.process(
        "#维修方式 维修方式：淘宝采购后自行维修 订单号：TB-2026-0001",
        "m2", role="LEADER", sender="uid-leader")
    assert status == "COMPLETED"
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'").fetchone()
    assert row["processed_result"] == "EXECUTED"
    assert _ticket(env.db, t1["ticket_no"])["status"] == "ACTIVE"
    rcur = env.db.connect().execute(
        "SELECT repair_method FROM repair_method_versions WHERE ticket_id=? AND is_current=1",
        (t1["id"],)).fetchone()
    assert rcur is not None and rcur["repair_method"] == "淘宝采购后自行维修"


@pytest.mark.asyncio
async def test_leader_stop_ticket_via_keyword(env):
    """全AI架构：#停止维修（工程负责人）经 AI 识别后仍需确认（SEMANTIC_MODEL ALWAYS）。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    status = await env.process("#停止维修 原因：配件停产，无法修复",
                               "m2", role="LEADER", sender="uid-leader")
    # 全AI下 # 也走模型，停修高危动作需确认
    row = env.db.connect().execute("SELECT processed_result FROM inbox_messages WHERE message_id='m2'").fetchone()
    assert row["processed_result"] == "WAITING_CONFIRMATION"
    assert _ticket(env.db, t1["ticket_no"])["status"] == "ACTIVE"
    assert any("确认执行「停修工单」" in s for s in env.sent)
    # 确认后才真正停修
    env.classifier.responses["c1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="system.confirm_pending_action", target_ticket_no=None, intent_confidence=1.0)
    await env.process("确认", "c1", role="LEADER", sender="uid-leader")
    t = _ticket(env.db, t1["ticket_no"])
    assert t["status"] == "STOPPED"
    assert t["stop_reason"] == "配件停产，无法修复"
    assert t["stopped_by"] == "uid-leader"
    assert t["stopped_at"] and t["closed_at"]
    # 静默化：停修成功不回执
    assert not any("已停修" in s for s in env.sent)


@pytest.mark.asyncio
async def test_stop_requires_reason(env):
    """#停止维修 未提供原因 → 校验拒绝。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    await env.process("#停止维修", "m2", role="LEADER", sender="uid-leader")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'").fetchone()
    assert row["processed_result"] == "REJECTED"
    assert _ticket(env.db, t1["ticket_no"])["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_manager_cannot_stop_ticket(env):
    """停修仅限工程负责人（LEADER）；店长发 #停止维修 被拒。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    await env.process("#停止维修 原因：不修了", "m2", role="MANAGER", sender="uid-mgr")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'").fetchone()
    assert row["processed_result"] == "REJECTED"
    assert _ticket(env.db, t1["ticket_no"])["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_stopped_ticket_can_reopen(env):
    """STOPPED 终态可 #重开工单 恢复 ACTIVE（全AI下均需确认）。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    await env.process("#停止维修 原因：配件停产，无法修复", "m2",
                      role="LEADER", sender="uid-leader")
    env.classifier.responses["c_stop"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="system.confirm_pending_action", target_ticket_no=None, intent_confidence=1.0)
    await env.process("确认", "c_stop", role="LEADER", sender="uid-leader")
    assert _ticket(env.db, t1["ticket_no"])["status"] == "STOPPED"
    status = await env.process(
        f"#重开工单 工单编号：{t1['ticket_no']} 重开原因：新配件到货",
        "m3", role="LEADER", sender="uid-leader")
    row = env.db.connect().execute("SELECT processed_result FROM inbox_messages WHERE message_id='m3'").fetchone()
    assert row["processed_result"] == "WAITING_CONFIRMATION"
    env.classifier.responses["c_reopen"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="system.confirm_pending_action", target_ticket_no=None, intent_confidence=1.0)
    await env.process("确认", "c_reopen", role="LEADER", sender="uid-leader")
    t = _ticket(env.db, t1["ticket_no"])
    assert t["status"] == "ACTIVE"
    assert t["closed_at"] is None
    assert t["reopen_count"] >= 1


@pytest.mark.asyncio
async def test_model_stop_still_requires_confirmation(env):
    """模型来源 #停止维修（高危动作）→ 仍走确认流程。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t1 = _active(env.db)[0]
    env.classifier.responses["m2"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.stop",
        target_ticket_no=t1["ticket_no"], intent_confidence=0.95,
        fields={"stop_reason": "配件停产"})
    status = await env.process("这个工单不要再修了，配件停产",
                               "m2", role="LEADER", sender="uid-leader")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'").fetchone()
    assert row["processed_result"] == "WAITING_CONFIRMATION"
    assert _ticket(env.db, t1["ticket_no"])["status"] == "ACTIVE"
    # 确认提示用中文可读文案，不暴露 intent ID
    assert any("确认执行「停修工单」" in s for s in env.sent)
    assert not any("ticket.stop" in s for s in env.sent)
    env.classifier.responses["c1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="system.confirm_pending_action", target_ticket_no=None,
        intent_confidence=1.0)
    await env.process("确认", "c1", role="LEADER", sender="uid-leader")
    t = _ticket(env.db, t1["ticket_no"])
    assert t["status"] == "STOPPED"
    assert t["stop_reason"] == "配件停产"
    assert t["stopped_by"] == "uid-leader"


@pytest.mark.asyncio
async def test_incomplete_create_can_be_supplemented_with_bare_sla(env):
    env.classifier.responses["m1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.create",
        target_ticket_no=None, intent_confidence=0.95,
        fields={"subject": "收银机", "location": "前台", "problem_description": "死机"},
    )
    await env.process("前台收银机死机了", "m1")
    first = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m1'"
    ).fetchone()
    assert first["processed_result"] == "REJECTED"
    assert env.db.get_waiting_pending("G1", "uid-mgr") is not None

    await env.process("3天", "m2")
    second = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'"
    ).fetchone()
    assert second["processed_result"] == "EXECUTED"
    ticket = _active(env.db)[0]
    assert ticket["subject"] == "收银机"
    assert ticket["location"] == "前台"
    assert ticket["sla_days"] == 3


@pytest.mark.asyncio
async def test_assisted_incomplete_create_requires_confirmation_after_supplement(env):
    pipeline = env.make_pipeline(mode=RuntimeMode.ASSISTED)
    env.classifier.responses["m1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.create",
        target_ticket_no=None, intent_confidence=0.95,
        fields={"subject": "收银机", "location": "前台", "problem_description": "死机"},
    )
    await env.process("前台收银机死机了", "m1", pipeline=pipeline)
    await env.process("3天", "m2", pipeline=pipeline)

    second = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'"
    ).fetchone()
    assert second["processed_result"] == "WAITING_CONFIRMATION"
    assert _active(env.db) == []
    pending = env.db.get_waiting_pending("G1", "uid-mgr")
    assert pending is not None
    assert pending["intent"] == "ticket.create"

    env.classifier.responses["m3"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="system.confirm_pending_action", target_ticket_no=None,
        intent_confidence=1.0,
    )
    await env.process("确认", "m3", pipeline=pipeline)
    confirmed = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m3'"
    ).fetchone()
    assert confirmed["processed_result"] == "EXECUTED"
    assert _active(env.db)[0]["sla_days"] == 3


@pytest.mark.asyncio
async def test_query_completed_ticket_by_number(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    ticket = _active(env.db)[0]
    env.classifier.responses["m2"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.complete",
        target_ticket_no=ticket["ticket_no"], intent_confidence=0.95,
    )
    await env.process("已经修好了", "m2", role="ENGINEER", sender="uid-eng")
    # 2026-08-24 需求 #3：工程师报完工 → 待店长确认，不再直接完成
    assert _ticket(env.db, ticket["ticket_no"])["status"] == "PENDING_CONFIRM"
    # 店长确认后才完成
    env.classifier.responses["m25"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.confirm_complete",
        target_ticket_no=ticket["ticket_no"], intent_confidence=0.95,
    )
    await env.process("确认修好", "m25")
    assert _ticket(env.db, ticket["ticket_no"])["status"] == "COMPLETED"

    env.classifier.responses["m3"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.query",
        target_ticket_no=ticket["ticket_no"], intent_confidence=0.95,
    )
    await env.process(f"查询 {ticket['ticket_no']}", "m3")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m3'"
    ).fetchone()
    assert row["processed_result"] == "EXECUTED"
    assert any(ticket["ticket_no"] in text and "已完成" in text for text in env.sent)


@pytest.mark.asyncio
async def test_llm_disabled_dead_letter_does_not_recommend_disabled_keywords(env):
    pipeline = env.make_pipeline(max_attempts=1, classifier_override=None)
    await env.process("帮我查一下工单", "m1", pipeline=pipeline)
    row = env.db.connect().execute(
        "SELECT status, attempts FROM inbox_messages WHERE message_id='m1'"
    ).fetchone()
    assert row["status"] == "DEAD_LETTER"
    assert row["attempts"] == 1
    assert any("智能识别暂时不可用" in text for text in env.sent)
    assert all("标准关键词" not in text for text in env.sent)


@pytest.mark.asyncio
async def test_bare_mobile_number_is_not_submitted_as_order(env):
    await env.process("#报修\n主题：门锁\n位置：前台\n问题描述：打不开\n时效：3天", "m1")
    await env.process("13800138000", "m2", role="ENGINEER", sender="uid-eng")

    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'"
    ).fetchone()
    assert row["processed_result"] == "IGNORED"
    assert env.db.get_order_monitor("13800138000") is None


@pytest.mark.asyncio
async def test_confirm_pending_create_preserves_executor_failure_and_notifies(env, monkeypatch):
    pipeline = env.make_pipeline(mode=RuntimeMode.ASSISTED)
    env.classifier.responses["m1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.create",
        target_ticket_no=None, intent_confidence=0.95,
        fields={
            "subject": "收银机", "location": "前台",
            "problem_description": "死机", "sla": "3天",
        },
    )
    await env.process("前台收银机死机了，时效3天", "m1", pipeline=pipeline)
    assert env.db.get_waiting_pending("G1", "uid-mgr") is not None

    monkeypatch.setattr(
        env.executor,
        "execute",
        lambda *args, **kwargs: CommandResult(RESULT_INTERNAL_ERROR, None, None, ()),
    )
    env.classifier.responses["m2"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="system.confirm_pending_action", target_ticket_no=None,
        intent_confidence=1.0,
    )
    await env.process("确认", "m2", pipeline=pipeline)

    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'"
    ).fetchone()
    assert row["processed_result"] == RESULT_INTERNAL_ERROR
    assert _active(env.db) == []
    assert env.db.get_waiting_pending("G1", "uid-mgr") is not None
    assert any("工单操作未完成" in text for text in env.sent)


@pytest.mark.asyncio
async def test_shadow_image_has_no_business_side_effects(env):
    pipeline = env.make_pipeline(mode=RuntimeMode.SHADOW)
    msg = NormalizedMessage(
        message_id="img-shadow", group_id="G1", sender_id="uid-mgr", sender_name="u",
        content="", message_type="image", sent_at=datetime.now(), sender_role="MANAGER",
        attachments=[ImageAttachment(0, "unknown", "opaque-image")],
    )
    env.db.enqueue_message(msg)
    row = env.db.connect().execute(
        "SELECT * FROM inbox_messages WHERE message_id='img-shadow'"
    ).fetchone()
    await pipeline.process(dict(row))

    result = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='img-shadow'"
    ).fetchone()
    assert result["processed_result"] == "SHADOW"
    assert env.sent == []
    assert env.db.get_message_link("img-shadow") is None


@pytest.mark.asyncio
async def test_shadow_audit_failure_is_retried(env, monkeypatch):
    pipeline = env.make_pipeline(mode=RuntimeMode.SHADOW)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(env.db, "save_semantic_decision", fail_audit)
    await env.process(
        "#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天",
        "shadow-audit-failure",
        pipeline=pipeline,
    )

    row = env.db.connect().execute(
        "SELECT status, attempts FROM inbox_messages WHERE message_id='shadow-audit-failure'"
    ).fetchone()
    assert row["status"] == "RETRY_PENDING"
    assert row["attempts"] == 1


@pytest.mark.asyncio
async def test_vision_task_is_removed_after_completion(env, monkeypatch):
    class FakeVisionAnalyzer:
        def __init__(self, *, db):
            self.db = db

        async def analyze_message(self, message_id):
            return 0

    import images.vision
    monkeypatch.setattr(images.vision, "VisionAnalyzer", FakeVisionAnalyzer)
    pipeline = env.make_pipeline()
    pipeline._schedule_vision_analysis("vision-task")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert pipeline._vision_tasks == []


@pytest.mark.asyncio
async def test_stale_pending_rejection_does_not_claim_success(env):
    await env.process(
        "#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "stale-reject-create"
    )
    ticket = _active(env.db)[0]
    env.classifier.responses["stale-reject-request"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.cancel",
        target_ticket_no=ticket["ticket_no"], intent_confidence=0.95,
        fields={"cancel_reason": "误报"},
    )
    await env.process("请取消这张单", "stale-reject-request")
    pending = env.db.get_waiting_pending("G1", "uid-mgr")
    assert pending is not None
    assert env.db.resolve_pending(
        pending["id"], pending["version"], PendingActionStatus.REJECTED.value,
        "external", now=datetime.now()
    )
    env.classifier.responses["stale-reject"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="system.reject_pending_action", target_ticket_no=None,
        intent_confidence=1.0,
    )
    before = len(env.sent)
    await env.process("取消", "stale-reject")

    assert len(env.sent) == before


@pytest.mark.asyncio
async def test_confirm_pending_target_preserves_executor_failure_and_notifies(env, monkeypatch):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    ticket = _active(env.db)[0]
    env.classifier.responses["m2"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.cancel",
        target_ticket_no=ticket["ticket_no"], intent_confidence=0.95,
        fields={"cancel_reason": "误报"},
    )
    await env.process("这张单是误报，请取消", "m2")
    assert env.db.get_waiting_pending("G1", "uid-mgr") is not None

    monkeypatch.setattr(
        env.executor,
        "execute",
        lambda *args, **kwargs: CommandResult(RESULT_INTERNAL_ERROR, ticket["id"], None, ()),
    )
    env.classifier.responses["m3"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="system.confirm_pending_action", target_ticket_no=None,
        intent_confidence=1.0,
    )
    await env.process("确认", "m3")

    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m3'"
    ).fetchone()
    assert row["processed_result"] == RESULT_INTERNAL_ERROR
    assert _ticket(env.db, ticket["ticket_no"])["status"] == "ACTIVE"
    assert any("工单操作未完成" in text for text in env.sent)


@pytest.mark.asyncio
async def test_supplement_create_executor_failure_keeps_retryable_pending(env, monkeypatch):
    env.classifier.responses["m1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.create",
        target_ticket_no=None, intent_confidence=0.95,
        fields={"subject": "收银机", "location": "前台", "problem_description": "死机"},
    )
    await env.process("前台收银机死机了", "m1")
    original = env.db.get_waiting_pending("G1", "uid-mgr")
    assert original is not None

    monkeypatch.setattr(
        env.executor,
        "execute",
        lambda *args, **kwargs: CommandResult(RESULT_INTERNAL_ERROR, None, None, ()),
    )
    await env.process("3天", "m2")

    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'"
    ).fetchone()
    assert row["processed_result"] == RESULT_INTERNAL_ERROR
    retry = env.db.get_waiting_pending("G1", "uid-mgr")
    assert retry is not None
    assert retry["id"] != original["id"]
    assert retry["fields"] == {
        "subject": "收银机", "location": "前台",
        "problem_description": "死机", "sla": "3天",
    }
    assert any("工单操作未完成" in text for text in env.sent)


@pytest.mark.asyncio
async def test_shadow_incomplete_create_has_no_pending_or_group_message(env):
    pipeline = env.make_pipeline(mode=RuntimeMode.SHADOW)
    env.classifier.responses["m1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.create",
        target_ticket_no=None, intent_confidence=0.95,
        fields={"subject": "收银机", "location": "前台", "problem_description": "死机"},
    )
    await env.process("前台收银机死机了", "m1", pipeline=pipeline)

    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m1'"
    ).fetchone()
    assert row["processed_result"] == "SHADOW"
    assert env.db.get_waiting_pending("G1", "uid-mgr") is None
    assert env.sent == []
    decision = env.db.connect().execute(
        "SELECT intent FROM semantic_decisions WHERE message_id='m1'"
    ).fetchone()
    assert decision is not None and decision["intent"] == "ticket.create"


@pytest.mark.asyncio
async def test_shadow_pending_reply_does_not_consume_existing_pending(env):
    env.classifier.responses["m1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL", intent="ticket.create",
        target_ticket_no=None, intent_confidence=0.95,
        fields={"subject": "收银机", "location": "前台", "problem_description": "死机"},
    )
    await env.process("前台收银机死机了", "m1")
    before = env.db.get_waiting_pending("G1", "uid-mgr")
    assert before is not None
    sent_count = len(env.sent)

    pipeline = env.make_pipeline(mode=RuntimeMode.SHADOW)
    await env.process("3天", "m2", pipeline=pipeline)

    after = env.db.get_waiting_pending("G1", "uid-mgr")
    assert after is not None
    assert (after["id"], after["version"], after["status"], after["fields"]) == (
        before["id"], before["version"], before["status"], before["fields"],
    )
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='m2'"
    ).fetchone()
    assert row["processed_result"] == "SHADOW"
    assert len(env.sent) == sent_count
