"""9 月核查报告回归测试（2026-09-03 静默修复）。

生产实证：
1. 「003确认修好」（店长）被模型判为 ticket.complete，在活动候选中无归属 →
   「当前没有可操作的活动工单」，#67 滞留 PENDING_CONFIRM；
   而同构「004确认修好」判为 ticket.confirm_complete 正常关闭 #68。
2. 「009修好了」（店长）经单候选兜底误关无关工单 -011；
   「010修好了」（店长）因无活动候选被拒；#66/#69 滞留 PENDING_CONFIRM。
3. 「004更换吸铁石」「007需要重新绑绳子…」判为 repair_plan 但 fields={} →
   「无法执行：请明确维修方式」；#73/#76 维修方式版本数为 0。
4. 「011电磁阀坏了 淘宝重新采购」→ order_no="011」、
   「…在淘宝采购吸铁石」→ order_no="淘宝采购吸铁石" 被执行落库，
   污染 order_monitor（脏行已同步共享表）。

守护行为：
- normalize_semantic_decision：店长显式「确认」措辞 complete→confirm_complete；
  非法 order_no 剔除；repair_plan 无字段时原文兜底回填 repair_method。
- pipeline 店长确认兜底：complete 在活动候选无明确归属但短编号唯一命中
  待确认工单 → 按 confirm_complete 执行；无编号/歧义时保持原路由。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from db import Database
from models import NormalizedMessage
from pipeline import MessageProcessingPipeline, RuntimeMode
from routing.pending_actions import PendingActionService
from routing.ticket_contexts import TicketContextStore
from routing.ticket_router import TicketRouter
from semantics.classifier import normalize_semantic_decision
from semantics.protocol_loader import load_protocol
from semantics.types import SemanticDecision
from tickets.executor import TicketCommandExecutor
from tickets.repository import TicketRepository

from test_four_improvements import FakeClassifier, GROUP, RecordingNotifier

_FMT = "%Y-%m-%d %H:%M:%S"


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture()
def env(tmp_path):
    from pathlib import Path

    protocol_path = (
        Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"
    )
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.upsert_group(GROUP)
    protocol = load_protocol(protocol_path)
    repo = TicketRepository(db)
    context = TicketContextStore(db)
    pending = PendingActionService(db)
    executor = TicketCommandExecutor(db, repo)
    notifier = RecordingNotifier(db)
    classifier = FakeClassifier(protocol=protocol)
    pipeline = MessageProcessingPipeline(
        db=db, repo=repo, protocol=protocol, router=TicketRouter(),
        context=context, pending=pending, executor=executor,
        notifier=notifier, classifier=classifier, mode=RuntimeMode.PRODUCTION,
    )

    async def process(text, message_id, role="MANAGER", sender="uid-mgr"):
        msg = NormalizedMessage(
            message_id=message_id, group_id="G1", sender_id=sender, sender_name="u",
            content=text, message_type="text", sent_at=datetime.now(), sender_role=role,
        )
        db.enqueue_message(msg)
        row = db.connect().execute(
            "SELECT * FROM inbox_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        return await pipeline.process(dict(row))

    yield SimpleNamespace(
        db=db, notifier=notifier, process=process, classifier=classifier,
    )
    db.close()


def _insert_ticket(db: Database, ticket_no: str, *, status: str = "ACTIVE") -> int:
    conn = db.connect()
    cur = conn.execute(
        """INSERT INTO tickets (ticket_no, group_id, store_name, reporter_id, subject,
               location, problem_description, sla_days, status, version, created_at)
           VALUES (?, 'G1', '测试群（示例）', 'r', 's', 'l', 'p', 1, ?, 1,
                   datetime('now','localtime'))""",
        (ticket_no, status),
    )
    return cur.lastrowid


def _decision(intent: str, fields: dict | None = None, target: str | None = None) -> SemanticDecision:
    return SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent=intent, target_ticket_no=target, intent_confidence=0.9,
        fields=dict(fields or {}),
    )


def _msg(content: str, role: str = "MANAGER") -> NormalizedMessage:
    return NormalizedMessage(
        message_id="m-test", group_id="G1", sender_id="uid-mgr", sender_name="u",
        content=content, message_type="text", sent_at=datetime.now(), sender_role=role,
    )


def _ticket_status(db: Database, tid: int) -> str:
    return db.get_ticket(tid)["status"]


# ───────────────────────── 归一化单元测试 ─────────────────────────


def test_manager_confirm_word_normalized_to_confirm_complete():
    """店长「003确认修好」误判为 complete → 归一化为 confirm_complete。"""
    out = normalize_semantic_decision(_decision("ticket.complete"), _msg("003确认修好"))
    assert out.intent == "ticket.confirm_complete"
    assert "manager_confirm_normalized" in out.evidence


def test_manager_plain_complete_not_normalized():
    """店长「008修好了」无确认措辞 → 保持 complete（直接完工路径不受影响）。"""
    out = normalize_semantic_decision(_decision("ticket.complete"), _msg("008修好了"))
    assert out.intent == "ticket.complete"


def test_engineer_confirm_word_not_normalized():
    """工程师「确认修好」（角色无确认权限）→ 不归一化，交校验层判权限。"""
    out = normalize_semantic_decision(
        _decision("ticket.complete"), _msg("确认修好", role="ENGINEER")
    )
    assert out.intent == "ticket.complete"


@pytest.mark.parametrize("bad", ["011", "淘宝采购吸铁石", "None", "", "AB-1", "0011"])
def test_implausible_order_no_dropped(bad: str):
    """短编号/中文短语/占位值不得作为 order_no。"""
    out = normalize_semantic_decision(
        _decision("ticket.repair_plan.submit", {"order_no": bad}),
        _msg("011电磁阀坏了 淘宝重新采购", role="ENGINEER"),
    )
    assert "order_no" not in out.fields


@pytest.mark.parametrize("good", ["9990000000000000001", "TB-2024-0001", "TB-ANY-0001"])
def test_plausible_order_no_kept(good: str):
    """纯数字长单号与字母单号均保留。"""
    out = normalize_semantic_decision(
        _decision("ticket.repair_plan.submit", {"order_no": good}),
        _msg(f"订单号：{good}", role="ENGINEER"),
    )
    assert out.fields.get("order_no") == good


def test_real_order_no_kept():
    """真实订单号不受守卫影响。"""
    out = normalize_semantic_decision(
        _decision("ticket.repair_plan.submit", {"order_no": "9990000000000000001"}),
        _msg("订单号：9990000000000000001", role="ENGINEER"),
    )
    assert out.fields["order_no"] == "9990000000000000001"


def test_repair_method_fallback_strips_ticket_prefix():
    """「004更换吸铁石」fields 缺失 → 剥离编号兜底为维修方式。"""
    out = normalize_semantic_decision(
        _decision("ticket.repair_plan.submit", {}), _msg("004更换吸铁石", role="ENGINEER")
    )
    assert out.fields["repair_method"] == "更换吸铁石"


def test_repair_method_fallback_keeps_full_action_text():
    """「007需要重新绑绳子，多的话需要木工」→ 全文即维修方式。"""
    out = normalize_semantic_decision(
        _decision("ticket.repair_plan.submit", {}),
        _msg("007需要重新绑绳子，多的话需要木工", role="ENGINEER"),
    )
    assert out.fields["repair_method"] == "需要重新绑绳子，多的话需要木工"


def test_bare_order_number_backfills_order_no_not_method():
    """裸订单号漏抽时补回 order_no，不误填 repair_method。"""
    out = normalize_semantic_decision(
        _decision("ticket.repair_plan.submit", {}),
        _msg("9990000000000000001", role="ENGINEER"),
    )
    assert out.fields.get("order_no") == "9990000000000000001"
    assert "repair_method" not in out.fields


@pytest.mark.asyncio
async def test_cue_fallback_repair_gets_method_backfilled():
    """模型判 ignore 但命中单一维修动作词 → 兜底 repair_plan 同样回填维修方式。

    生产实证：死信 msgNzY0「维修方式：将铁块背部垫起…」模型判 ignore，
    cue 兜底转 repair_plan 但 fields={}（早退路径曾绕过归一化）。
    """
    from semantics.classifier import SemanticClassifier
    from semantics.types import TicketCandidate
    from test_model_contract import FakeModelClient, _load_protocol, _make_message

    protocol = _load_protocol()
    fake = FakeModelClient(response={
        "intent": "chat.ignore",
        "confidence": 0.9,
        "fields": {},
    })
    classifier = SemanticClassifier(client=fake, protocol=protocol)
    result = await classifier.classify(
        _make_message(content="维修方式：将铁块背部垫起使其跟反馈电锁平面平行", sender_role="ENGINEER"),
        candidates=[TicketCandidate(
            ticket_id=1, ticket_no="测试店-门锁-3天-001", group_id="g-test",
            subject="门锁", location="前台", problem_summary="打不开",
            status="ACTIVE", version=1,
        )],
    )
    assert result.intent == "ticket.repair_plan.submit"
    assert result.fields.get("repair_method", "").startswith("维修方式")


def test_payload_includes_field_hints_for_repair_plan():
    """提示词动作摘要带字段提示，模型知道 repair_plan 要抽什么。"""
    from semantics.classifier import _build_payload

    protocol = load_protocol(
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "protocols" / "ticket_semantics.v4.json"
    )
    payload = _build_payload(_msg("004更换吸铁石", role="ENGINEER"), [], None, protocol)
    system_text = payload["messages"][0]["content"]
    line = next(
        ln for ln in system_text.splitlines() if "ticket.repair_plan.submit" in ln
    )
    assert "repair_method" in line and "order_no" in line


# ───────────────────────── 端到端：确认链路 ─────────────────────────


@pytest.mark.asyncio
async def test_manager_003_confirm_closes_pending_ticket(env):
    """复现 #67：模型误判 complete，pipeline 归一化后正常关闭待确认工单。"""
    t3 = _insert_ticket(env.db, "测试群（示例）-零号特工-7天-003", status="PENDING_CONFIRM")
    # FakeClassifier 直接复现故障模型的输出（ticket.complete + 无编号）
    env.classifier.responses["m1"] = _decision("ticket.complete", {})
    await env.process("003确认修好", "m1")
    assert _ticket_status(env.db, t3) == "COMPLETED"
    texts = [t for _, t in env.notifier.calls]
    assert not any("没有可操作的活动工单" in t for t in texts)


@pytest.mark.asyncio
async def test_manager_numbered_complete_falls_back_to_pending(env):
    """复现「009修好了」：单候选 -011 不得被误关，-009 正常确认关闭。"""
    t9 = _insert_ticket(env.db, "测试群（示例）-天才特工营-3天-009", status="PENDING_CONFIRM")
    t11 = _insert_ticket(env.db, "测试群（示例）-古堡秘事-7天-011", status="ACTIVE")
    env.classifier.responses["m1"] = _decision("ticket.complete", {})
    await env.process("009修好了", "m1")
    assert _ticket_status(env.db, t9) == "COMPLETED"
    assert _ticket_status(env.db, t11) == "ACTIVE"


@pytest.mark.asyncio
async def test_manager_numbered_complete_without_active_candidates(env):
    """复现「010修好了」：无活动候选时不再被拒，-010 正常确认关闭。"""
    t10 = _insert_ticket(env.db, "测试群（示例）-勇者斯巴达-7天-010", status="PENDING_CONFIRM")
    env.classifier.responses["m1"] = _decision("ticket.complete", {})
    await env.process("010修好了", "m1")
    assert _ticket_status(env.db, t10) == "COMPLETED"


@pytest.mark.asyncio
async def test_manager_plain_complete_on_active_unchanged(env):
    """回归：「008修好了」命中活动工单 → 直接完工，不被兜底转确认。"""
    t8 = _insert_ticket(env.db, "测试群（示例）-黑魔法监狱-3天-008", status="ACTIVE")
    env.classifier.responses["m1"] = _decision("ticket.complete", {})
    await env.process("008修好了", "m1")
    assert _ticket_status(env.db, t8) == "COMPLETED"


@pytest.mark.asyncio
async def test_manager_numberless_complete_single_active_unchanged(env):
    """回归：无编号「修好了」+ 单活动工单 → 单候选兜底仍正常关闭。"""
    t11 = _insert_ticket(env.db, "测试群（示例）-古堡秘事-7天-011", status="ACTIVE")
    env.classifier.responses["m1"] = _decision("ticket.complete", {})
    await env.process("修好了", "m1")
    assert _ticket_status(env.db, t11) == "COMPLETED"


@pytest.mark.asyncio
async def test_manager_ambiguous_number_keeps_original_route(env):
    """歧义：两个待确认同尾缀 + 无活动候选 → 不猜测，保持澄清/拒绝。"""
    t1 = _insert_ticket(env.db, "测试群（示例）-A-3天-009", status="PENDING_CONFIRM")
    t2 = _insert_ticket(env.db, "测试群（示例）-B-3天-009", status="PENDING_CONFIRM")
    env.classifier.responses["m1"] = _decision("ticket.complete", {})
    await env.process("009修好了", "m1")
    assert _ticket_status(env.db, t1) == "PENDING_CONFIRM"
    assert _ticket_status(env.db, t2) == "PENDING_CONFIRM"


# ───────────────────────── 端到端：维修方式链路 ─────────────────────────


@pytest.mark.asyncio
async def test_engineer_colloquial_repair_method_recorded(env):
    """复现 #73：模型漏抽维修方式，兜底后正常落版本，不再拒绝。"""
    t4 = _insert_ticket(env.db, "测试群（示例）-博物馆-7天-004", status="ACTIVE")
    env.classifier.responses["m1"] = _decision("ticket.repair_plan.submit", {})
    await env.process("004更换吸铁石", "m1", role="ENGINEER", sender="uid-eng")
    rows = env.db.connect().execute(
        "SELECT repair_method, order_no FROM repair_method_versions WHERE ticket_id=? AND is_current=1",
        (t4,),
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "更换吸铁石"
    texts = [t for _, t in env.notifier.calls]
    assert not any("请明确维修方式" in t for t in texts)


@pytest.mark.asyncio
async def test_engineer_garbage_order_no_not_persisted(env):
    """复现脏订单行：order_no="011" 被剔除，不再静默落库；
    消息含淘宝采购且无真实单号 → 明确索要订单号而非注册垃圾。"""
    t11 = _insert_ticket(env.db, "测试群（示例）-古堡秘事-7天-011", status="ACTIVE")
    env.classifier.responses["m1"] = _decision(
        "ticket.repair_plan.submit", {"order_no": "011"}
    )
    await env.process("011电磁阀坏了 淘宝重新采购", "m1", role="ENGINEER", sender="uid-eng")
    assert (
        env.db.connect().execute(
            "SELECT COUNT(*) FROM order_monitor WHERE order_id='011'"
        ).fetchone()[0]
        == 0
    )
    texts = [t for _, t in env.notifier.calls]
    assert any("订单号" in t for t in texts)


@pytest.mark.asyncio
async def test_engineer_garbage_chinese_order_no_dropped(env):
    """复现脏订单行：order_no="淘宝采购吸铁石" 被剔除，不进订单监控。"""
    t3 = _insert_ticket(env.db, "测试群（示例）-勇者斯巴达-7天-003", status="ACTIVE")
    env.classifier.responses["m1"] = _decision(
        "ticket.repair_plan.submit", {"order_no": "淘宝采购吸铁石"}
    )
    await env.process(
        "门店自行联系供应商采购雪弗板，在淘宝采购吸铁石",
        "m1", role="ENGINEER", sender="uid-eng",
    )
    assert (
        env.db.connect().execute(
            "SELECT COUNT(*) FROM order_monitor WHERE order_id='淘宝采购吸铁石'"
        ).fetchone()[0]
        == 0
    )
    # 含淘宝采购但无真实单号 → 明确索要订单号，不注册中文短语
    texts = [t for _, t in env.notifier.calls]
    assert any("订单号" in t for t in texts)
