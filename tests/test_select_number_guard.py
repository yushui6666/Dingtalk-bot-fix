"""选单与特殊情况「显式编号解析失败静默兜底」守卫测试（2026-08-28）。

事故（2026-08-28 14:37 杭州商场21CC群，唯一活动工单 …-005 勇者斯巴达）：
1. 「财阀的007号单」→ ticket.select 的 ticket_no="007" 被 classifier 过滤为
   None，_handle_select 落入单候选兜底，静默切到 …-005 并回「✅ 已切换」；
2. 「007今日胶未干，明日测试再反馈」→ 判为特殊情况并经单候选/选单上下文
   静默登记到 …-005，工单时钟被错误冻结（真停表，完全静默）。

守卫规则（本文件守护的行为）：
- 选单：编号两步解析（全编号精确 → 短编号尾缀唯一）；消息显式编号与解析
  目标不符（模型违规展开成候选完整编号）→ 按「没有找到」拒绝；消息带
  「XX号单」形态显式指单但模型未返回编号 → 不落入单候选兜底；无编号指代
  （如「就切这张」）仍允许单候选兜底。
- 特殊情况：消息开头提到无法解析的短编号（本群无对应尾缀）→ 不登记，
  提示带工单号重发；标准格式与命中尾缀的路径不受影响。
- 查询：带编号查询支持短编号尾缀解析（含已完结工单）。
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
from semantics.protocol_loader import load_protocol
from semantics.types import SemanticDecision
from tickets.executor import TicketCommandExecutor
from tickets.repository import TicketRepository

from test_four_improvements import FakeClassifier, GROUP, RecordingNotifier
from test_model_contract import FakeModelClient, _load_protocol, _make_message

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


def _select(no: str | None) -> SemanticDecision:
    return SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.select", target_ticket_no=no, intent_confidence=0.9,
    )


def _special(reason: str, expected: str) -> SemanticDecision:
    return SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.special_case.submit", target_ticket_no=None,
        intent_confidence=0.9,
        fields={"special_case_reason": reason, "expected_resume_at": expected},
    )


def _active_case(db: Database, tid: int):
    return db.connect().execute(
        "SELECT * FROM ticket_special_cases WHERE ticket_id=? AND resumed_at IS NULL",
        (tid,),
    ).fetchone()


def _ctx(db: Database):
    row = db.get_ticket_context("G1", "uid-mgr", datetime.now().strftime(_FMT))
    return row["ticket_id"] if row else None


def _processed_result(db: Database, message_id: str) -> str | None:
    row = db.connect().execute(
        "SELECT result FROM processed_events WHERE message_id=?", (message_id,)
    ).fetchone()
    return row["result"] if row else None


# ───────────────────────── 选单：解析守卫 ─────────────────────────


@pytest.mark.asyncio
async def test_select_unresolved_short_number_rejected_not_switched(env):
    """「007号单」指向不存在的工单 → 明确拒绝，绝不静默切到唯一候选 …-005。"""
    _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    env.classifier.responses["m1"] = _select("007")
    await env.process("财阀的007号单", "m1")
    texts = [t for _, t in env.notifier.calls]
    assert any("没有找到该工单" in t for t in texts)
    assert not any("已切换" in t for t in texts)
    assert _ctx(env.db) is None
    assert _processed_result(env.db, "m1") == "REJECTED"


@pytest.mark.asyncio
async def test_select_short_number_suffix_resolution(env):
    """「007号单」本群存在尾缀 …-007 → 尾缀解析并切换成功。"""
    _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    t7 = _insert_ticket(env.db, "测试群（示例）-财阀-3天-007")
    env.classifier.responses["m1"] = _select("007")
    await env.process("007号单", "m1")
    assert _ctx(env.db) == t7
    assert any("已切换到工单 测试群（示例）-财阀-3天-007" in t for _, t in env.notifier.calls)


@pytest.mark.asyncio
async def test_select_model_expanded_wrong_full_number_rejected(env):
    """模型违规把「007号单」展开成候选完整编号 …-005 → 编号印证失败，拒绝。"""
    _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    env.classifier.responses["m1"] = _select("测试群（示例）-收银机-1天-005")
    await env.process("财阀的007号单", "m1")
    texts = [t for _, t in env.notifier.calls]
    assert any("没有找到该工单" in t for t in texts)
    assert not any("已切换" in t for t in texts)
    assert _ctx(env.db) is None


@pytest.mark.asyncio
async def test_select_numberless_single_candidate_still_switches(env):
    """回归：无编号指代（「就切到这张」）+ 单候选 → 仍正常切换。"""
    t5 = _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    env.classifier.responses["m1"] = _select(None)
    await env.process("就切到这张", "m1")
    assert _ctx(env.db) == t5
    assert any("已切换到工单" in t for _, t in env.notifier.calls)


@pytest.mark.asyncio
async def test_select_mentioned_number_without_model_target_rejected(env):
    """消息显式提到「007号单」但模型未返回编号 → 不落入单候选兜底。"""
    _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    env.classifier.responses["m1"] = _select(None)
    await env.process("切到007号单", "m1")
    texts = [t for _, t in env.notifier.calls]
    assert any("没有找到该工单" in t for t in texts)
    assert _ctx(env.db) is None


@pytest.mark.asyncio
async def test_select_ambiguous_suffix_rejected(env):
    """尾缀去前导零后命中多张（-05 与 -005）→ 提示提供完整编号。"""
    _insert_ticket(env.db, "测试群（示例）-A-1天-05")
    _insert_ticket(env.db, "测试群（示例）-B-1天-005")
    env.classifier.responses["m1"] = _select("05")
    await env.process("05号单", "m1")
    assert any("匹配多张" in t for _, t in env.notifier.calls)
    assert _ctx(env.db) is None


# ───────────────────────── 特殊情况：归属守卫 ─────────────────────────


@pytest.mark.asyncio
async def test_special_case_leading_unresolved_number_not_registered(env):
    """「007今日胶未干…」开头编号本群无对应工单 → 不登记，提示带工单号重发。"""
    t5 = _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    env.classifier.responses["m1"] = _special("007今日胶未干", "明日测试再反馈")
    await env.process(
        "007今日胶未干，明日测试再反馈", "m1", role="ENGINEER", sender="uid-eng"
    )
    assert _active_case(env.db, t5) is None
    texts = [t for _, t in env.notifier.calls]
    assert any("007" in t and "未登记" in t for t in texts)
    assert _processed_result(env.db, "m1") == "REJECTED"


@pytest.mark.asyncio
async def test_special_case_standard_format_still_registers(env):
    """回归：标准格式（「特殊情况：…」开头，无前导编号）→ 正常登记。"""
    t5 = _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    env.classifier.responses["m1"] = _special("等待到货", "一小时内")
    await env.process(
        "特殊情况：等待到货；预计恢复：一小时内", "m1",
        role="ENGINEER", sender="uid-eng",
    )
    assert _active_case(env.db, t5) is not None


@pytest.mark.asyncio
async def test_special_case_leading_number_matching_suffix_routes(env):
    """回归：开头编号命中本群活动工单尾缀（…-007）→ 尾缀路由正常登记。"""
    t5 = _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    t7 = _insert_ticket(env.db, "测试群（示例）-财阀-3天-007")
    env.classifier.responses["m1"] = _special("胶未干", "明日")
    await env.process(
        "007 特殊情况：胶未干；预计恢复：明日", "m1",
        role="ENGINEER", sender="uid-eng",
    )
    assert _active_case(env.db, t7) is not None
    assert _active_case(env.db, t5) is None


# ───────────────────────── 查询：尾缀解析 ─────────────────────────


@pytest.mark.asyncio
async def test_query_short_number_suffix_resolution(env):
    """「查007号单」→ 尾缀解析（含已完结工单），不再误报没有找到。"""
    _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    _insert_ticket(env.db, "测试群（示例）-财阀-3天-007", status="COMPLETED")
    env.classifier.responses["m1"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.query", target_ticket_no="007", intent_confidence=0.9,
    )
    await env.process("查一下007号单", "m1")
    texts = [t for _, t in env.notifier.calls]
    assert any("测试群（示例）-财阀-3天-007" in t for t in texts)
    assert not any("没有找到该工单" in t for t in texts)


# ───────────────────────── 事故精确场景：007 是待店长确认工单 ─────────────────────────


@pytest.mark.asyncio
async def test_select_pending_confirm_reference_explained(env):
    """「007号单」指向本群 PENDING_CONFIRM 工单 → 拒绝切换并说明状态（非活动工单）。"""
    _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    _insert_ticket(
        env.db, "测试群（示例）-财阀继承人-7天-007", status="PENDING_CONFIRM"
    )
    env.classifier.responses["m1"] = _select("007")
    await env.process("财阀的007号单", "m1")
    texts = [t for _, t in env.notifier.calls]
    assert any("待店长确认" in t and "财阀继承人" in t for t in texts)
    assert _ctx(env.db) is None


@pytest.mark.asyncio
async def test_special_case_pending_confirm_reference_explained(env):
    """「007今日胶未干…」指向本群 PENDING_CONFIRM 工单 → 不登记，说明状态。"""
    t5 = _insert_ticket(env.db, "测试群（示例）-收银机-1天-005")
    _insert_ticket(
        env.db, "测试群（示例）-财阀继承人-7天-007", status="PENDING_CONFIRM"
    )
    env.classifier.responses["m1"] = _special("007今日胶未干", "明日测试再反馈")
    await env.process(
        "007今日胶未干，明日测试再反馈", "m1", role="ENGINEER", sender="uid-eng"
    )
    assert _active_case(env.db, t5) is None
    texts = [t for _, t in env.notifier.calls]
    assert any("待店长确认" in t and "未登记" in t for t in texts)


# ───────────────────────── classifier：保留 select 编号 ─────────────────────────


@pytest.mark.asyncio
async def test_classifier_preserves_select_ticket_no_outside_candidates():
    """模型对 select 返回候选集合外的短编号（007）→ 原样保留，不再过滤为 None。"""
    from semantics.classifier import SemanticClassifier
    from semantics.types import TicketCandidate

    protocol = _load_protocol()
    fake = FakeModelClient(response={
        "intent": "ticket.select", "confidence": 0.9,
        "fields": {"ticket_no": "007"},
    })
    classifier = SemanticClassifier(client=fake, protocol=protocol)
    candidates = [
        TicketCandidate(
            ticket_id=1, ticket_no="测试群（示例）-收银机-1天-005", group_id="G1",
            subject="收银机", location="前台", problem_summary="死机",
            status="ACTIVE", version=1,
        )
    ]
    result = await classifier.classify(
        _make_message(content="财阀的007号单"), candidates=candidates
    )
    assert result.intent == "ticket.select"
    assert result.target_ticket_no == "007"
