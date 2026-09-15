"""待商榷（PENDING_NEGOTIATION）工单路由可达性 TDD —— 2026-09-14 事故

事故现场（生产只读实证）：
  群 cid_demo_group_b==（北京商场14大悦城品牌B店）唯一工单 id=49
  「北京商场14大悦城品牌B店-商场14大悦城-7天-001」状态为待商榷；工程师 11:22 发
  「001已完成」→ 机器人回「当前没有可操作的活动工单，请先创建工单。」
  - semantic_decisions id=701：intent=ticket.complete conf=0.90 target_ticket_no=None
  - app.log 11:22:53：消息路由 route=CLARIFY candidates=0
  用户 11:31 在管理后台手动改为已完成（POST /api/tickets/49/status，admin-manual）。

根因：路由候选来自 db.list_active_tickets（只取 ACTIVE/ACTIVE_OVERDUE；待商榷按
2026-08-28 设计「不算活动」）→ 候选=0 → 第 7 步 CLARIFY → 空候选分支回误导文案。
而协议 ticket.complete / ticket.cancel / ticket.stop 的 allowed_ticket_states 早已
含 PENDING_NEGOTIATION（2026-08-28 用户决策「待商榷可直接完工」，cancel/stop 亦可）
→ 协议放行、路由不可达（实现层缺口，非产品决策缺口）。

本文件钉住修复后的行为，并显式钉住「有意未放开」的边界（特殊情况登记）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from db import Database
from models import (
    NormalizedMessage,
    ROLE_ENGINEER,
    ROLE_LEADER,
    ROLE_MANAGER,

    TICKET_COMPLETED,
    TICKET_NEGOTIATING,
    TICKET_PENDING_CONFIRM,
)
from notifier import Notifier
from pipeline import MessageProcessingPipeline, RuntimeMode
from routing.pending_actions import PendingActionService
from routing.ticket_contexts import TicketContextStore
from routing.ticket_router import TicketRouter
from semantics.protocol_loader import load_protocol
from semantics.types import SemanticDecision
from tickets.executor import TicketCommandExecutor
from tickets.repository import TicketRepository

_PROTOCOL_PATH = Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"

GROUP = {
    "group_id": "cid-jingxi",
    "store_name": "北京商场14大悦城品牌B店",
    "manager_ids": ["uid-mgr"],
    "engineer_ids": ["uid-eng"],
    "other_member_ids": [],
}

# 生产误导文案（pipeline._create_clarify_pending 空候选分支）
NO_ACTIVE_TEXT = "当前没有可操作的活动工单"


class StubClassifier:
    """按 message_id 返回预设决策，精确复刻生产模型输出（不走关键词/本地兜底）。"""

    def __init__(self) -> None:
        self.responses: dict[str, SemanticDecision] = {}

    def preset(
        self,
        message_id: str,
        intent: str,
        *,
        fields: dict | None = None,
        target_ticket_no: str | None = None,
        confidence: float = 0.9,
    ) -> None:
        self.responses[message_id] = SemanticDecision(
            protocol_version="4.0.0",
            source="SEMANTIC_MODEL",
            intent=intent,
            target_ticket_no=target_ticket_no,
            intent_confidence=confidence,
            fields=dict(fields or {}),
        )

    async def classify(self, message, candidates=None, pending_action=None, history=None, **kwargs):
        decision = self.responses.get(message.message_id)
        if decision is not None:
            return decision
        return SemanticDecision(
            protocol_version="4.0.0", source="SEMANTIC_MODEL",
            intent="chat.ignore", target_ticket_no=None, intent_confidence=0.0,
        )


class Env:
    def __init__(self, db, repo, classifier, sent, pipeline) -> None:
        self.db = db
        self.repo = repo
        self.classifier = classifier
        self.sent = sent
        self.pipeline = pipeline

    async def process(self, text: str, message_id: str, *, role: str, sender: str) -> str:
        msg = NormalizedMessage(
            message_id=message_id, group_id=GROUP["group_id"], sender_id=sender,
            sender_name=sender, content=text, message_type="text",
            sent_at=datetime.now(), sender_role=role,
        )
        self.db.enqueue_message(msg)
        row = self.db.connect().execute(
            "SELECT * FROM inbox_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        return await self.pipeline.process(dict(row))


@pytest.fixture()
def env(tmp_path) -> Env:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.upsert_group(GROUP)
    protocol = load_protocol(_PROTOCOL_PATH)
    repo = TicketRepository(db)
    classifier = StubClassifier()
    sent: list[str] = []
    pipeline = MessageProcessingPipeline(
        db=db, repo=repo, protocol=protocol, router=TicketRouter(),
        context=TicketContextStore(db), pending=PendingActionService(db),
        executor=TicketCommandExecutor(db, repo),
        notifier=Notifier(db, lambda target, text: sent.append(text)),
        classifier=classifier, mode=RuntimeMode.PRODUCTION, max_attempts=1,
    )
    yield Env(db, repo, classifier, sent, pipeline)
    db.close()


def _make_negotiating_ticket(env: Env, *, subject: str = "商场14大悦城") -> int:
    """事故单形态：建单即待商榷（sla=待商榷 → PENDING_NEGOTIATION）。"""
    tid = env.repo.create_ticket(
        group=GROUP, reporter_id="uid-mgr", subject=subject, location="外观门头",
        problem_description="招牌灯故障", sla_label="待商榷", now="2026-08-22 21:00:15",
    )
    assert env.db.get_ticket(tid)["status"] == TICKET_NEGOTIATING
    return tid


def _waiting_pending(env: Env) -> dict | None:
    row = env.db.connect().execute(
        "SELECT * FROM pending_actions WHERE status='WAITING'"
    ).fetchone()
    return dict(row) if row else None


# ─────────────────────── 候选来源（数据层） ───────────────────────


def test_snapshot_candidates_can_include_negotiating(tmp_path):
    """按状态集合取候选（路由层用）；默认行为仍是「只要活动工单」。"""
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.upsert_group(GROUP)
    repo = TicketRepository(db)
    active = repo.create_ticket(
        group=GROUP, reporter_id="uid-mgr", subject="A", location="L",
        problem_description="P", sla_label="3天", now="2026-08-22 21:00:15",
    )
    negotiating = repo.create_ticket(
        group=GROUP, reporter_id="uid-mgr", subject="B", location="L",
        problem_description="P", sla_label="待商榷", now="2026-08-22 21:05:00",
    )

    # 默认（None）＝ 活动工单，待商榷不算活动（2026-08-28 设计不变）
    assert [c.ticket_id for c in repo.snapshot_candidates(GROUP["group_id"])] == [active]

    widened = repo.snapshot_candidates(
        GROUP["group_id"], ("ACTIVE", "ACTIVE_OVERDUE", TICKET_NEGOTIATING)
    )
    assert [c.ticket_id for c in widened] == [active, negotiating]
    assert widened[1].status == TICKET_NEGOTIATING
    db.close()


# ─────────────────────── 事故复现（核心） ───────────────────────


@pytest.mark.asyncio
async def test_engineer_complete_negotiating_ticket_by_number(env: Env):
    """事故现场：唯一工单为待商榷 + 工程师「001已完成」→ 必须能落单，不再回误导文案。"""
    tid = _make_negotiating_ticket(env)
    env.classifier.preset(
        "m1", "ticket.complete", fields={"completion_note": "已完成"}, confidence=0.90
    )

    await env.process("001已完成", "m1", role=ROLE_ENGINEER, sender="uid-eng")

    assert not any(NO_ACTIVE_TEXT in s for s in env.sent), env.sent
    # 工程师报完工 → 待店长确认（既有规则不变），且回执必须发声
    assert env.db.get_ticket(tid)["status"] == TICKET_PENDING_CONFIRM
    assert any("已报完工" in s for s in env.sent), env.sent


@pytest.mark.asyncio
async def test_manager_complete_negotiating_ticket_by_number(env: Env):
    """店长本人报完工 → 直接终态（既有规则；待商榷不再拦在路由外）。"""
    tid = _make_negotiating_ticket(env)
    env.classifier.preset(
        "m1", "ticket.complete", fields={"completion_note": "已完成"}, confidence=0.90
    )

    await env.process("001已完成", "m1", role=ROLE_MANAGER, sender="uid-mgr")

    assert not any(NO_ACTIVE_TEXT in s for s in env.sent), env.sent
    ticket = env.db.get_ticket(tid)
    assert ticket["status"] == TICKET_COMPLETED
    assert ticket["closed_at"] is not None


@pytest.mark.asyncio
async def test_complete_negotiating_ticket_single_candidate_without_number(env: Env):
    """无编号但群内唯一工单为待商榷 → 单候选兜底仍可用（如「已经修好了」）。"""
    tid = _make_negotiating_ticket(env)
    env.classifier.preset("m1", "ticket.complete", fields={"completion_note": "已修好"})

    await env.process("已经修好了", "m1", role=ROLE_ENGINEER, sender="uid-eng")

    assert not any(NO_ACTIVE_TEXT in s for s in env.sent), env.sent
    assert env.db.get_ticket(tid)["status"] == TICKET_PENDING_CONFIRM


@pytest.mark.asyncio
async def test_complete_picks_negotiating_ticket_in_mixed_group(env: Env):
    """混合群（1 待商榷 + 1 活动）：短编号只唯一命中待商榷那张 → 必须归它，不得张冠李戴。

    序号是群级自增：待商榷单先建 → 001，活动单 → 002；消息里的「001」必须
    命中 001 那张，且模型未填编号（复刻生产 target_ticket_no=None）。
    """
    negotiating = _make_negotiating_ticket(env, subject="商场14大悦城")
    active = env.repo.create_ticket(
        group=GROUP, reporter_id="uid-mgr", subject="别的单", location="L",
        problem_description="P", sla_label="3天", now="2026-08-22 21:00:15",
    )
    assert env.db.get_ticket(negotiating)["ticket_no"].endswith("-001")
    assert env.db.get_ticket(active)["ticket_no"].endswith("-002")

    env.classifier.preset("m1", "ticket.complete", fields={"completion_note": "已完成"})
    await env.process("001已完成", "m1", role=ROLE_MANAGER, sender="uid-mgr")

    assert env.db.get_ticket(negotiating)["status"] == TICKET_COMPLETED
    assert env.db.get_ticket(active)["status"] != TICKET_COMPLETED


@pytest.mark.asyncio
async def test_numberless_complete_never_silently_hits_unrelated_ticket(env: Env):
    """安全回归：待商榷单 + 另一张活动单，无编号报完工不得静默落到活动单上。

    修复前该群的候选只剩「那张活动单」（待商榷不可见）→ 命中单候选兜底
    （LINK_SINGLE）→ 会静默把无关工单改成已完成；与 2026-08-28「007 错挂 005」
    同类事故。修复后候选含待商榷单 → 多候选 → 必须澄清而不是猜。
    """
    negotiating = _make_negotiating_ticket(env, subject="商场14大悦城")
    active = env.repo.create_ticket(
        group=GROUP, reporter_id="uid-mgr", subject="别的单", location="L",
        problem_description="P", sla_label="3天", now="2026-08-22 21:00:15",
    )
    env.classifier.preset("m1", "ticket.complete", fields={"completion_note": "已修好"})

    await env.process("已经修好了", "m1", role=ROLE_ENGINEER, sender="uid-eng")

    assert env.db.get_ticket(active)["status"] != TICKET_COMPLETED, "静默关错了无关工单"
    assert env.db.get_ticket(negotiating)["status"] == TICKET_NEGOTIATING  # 未归属 → 不动作
    assert any("请选择或提供编号" in s for s in env.sent), env.sent


# ─────────────────────── cancel / stop 同族缺口 ───────────────────────


@pytest.mark.asyncio
async def test_cancel_negotiating_ticket_reaches_confirmation(env: Env):
    """待商榷工单可取消（协议 2026-08-28 放行）→ 应进入待确认，而不是「没有活动工单」。"""
    tid = _make_negotiating_ticket(env)
    env.classifier.preset("m1", "ticket.cancel", fields={"cancel_reason": "门店不修了"})

    await env.process("001取消工单", "m1", role=ROLE_MANAGER, sender="uid-mgr")

    assert not any(NO_ACTIVE_TEXT in s for s in env.sent), env.sent
    pending = _waiting_pending(env)
    assert pending is not None, env.sent
    assert tid in json.loads(pending["candidate_ticket_ids_json"])


@pytest.mark.asyncio
async def test_stop_pending_confirm_ticket_reaches_confirmation(env: Env):
    """待店长确认工单可停修（2026-08-24 用户决策，协议已放行）→ 同样不该被路由拦住。

    停修仅限工程负责人（角色权限表），故此处由 LEADER 发送。
    """
    tid = env.repo.create_ticket(
        group=GROUP, reporter_id="uid-mgr", subject="停修单", location="L",
        problem_description="P", sla_label="3天", now="2026-08-22 21:00:15",
    )
    ticket = env.db.get_ticket(tid)
    assert env.db.update_ticket_cas(
        tid, ticket["version"], "status=?", (TICKET_PENDING_CONFIRM,)
    )
    # 直接校验执行器可停修（2026-08-24 决策）：路由必须能把候选送到这里
    env.classifier.preset("m1", "ticket.stop", fields={"stop_reason": "客户不修了"})

    await env.process("001停止维修", "m1", role=ROLE_LEADER, sender="uid-leader")

    assert not any(NO_ACTIVE_TEXT in s for s in env.sent), env.sent
    pending = _waiting_pending(env)
    assert pending is not None, env.sent
    assert tid in json.loads(pending["candidate_ticket_ids_json"])


# ─────────────────────── 有意未放开的边界（防误扩） ───────────────────────


@pytest.mark.asyncio
async def test_special_case_on_negotiating_ticket_stays_unrouted(env: Env):
    """边界：特殊情况登记到待商榷单属 2026-09-10 方案「问题二」，用户尚未拍板。

    有意保持现状（候选仍为空 → 仍回提示文案），避免未经决策放开产品行为。
    若日后决定放开，本测试应随决策一并改写。
    """
    tid = _make_negotiating_ticket(env)
    env.classifier.preset(
        "m1", "ticket.special_case.submit",
        fields={"special_case_reason": "等客户确认方案"},
    )

    await env.process("特殊情况：等客户确认方案", "m1", role=ROLE_ENGINEER, sender="uid-eng")

    assert any(NO_ACTIVE_TEXT in s for s in env.sent), env.sent
    assert env.db.get_ticket(tid)["status"] == TICKET_NEGOTIATING


@pytest.mark.asyncio
async def test_add_detail_on_negotiating_ticket_stays_unrouted(env: Env):
    """边界：协议 ticket.add_detail 仅允许 ACTIVE/ACTIVE_OVERDUE → 有意不取待商榷。

    带显式编号时由既有「编号硬校验」给出状态解释（不是误导性的「请先创建工单」）；
    工单状态不变、也不得执行补充动作。
    """
    tid = _make_negotiating_ticket(env)
    env.classifier.preset("m1", "ticket.add_detail", fields={},
                          target_ticket_no=env.db.get_ticket(tid)["ticket_no"])

    await env.process("001 补充一下：门头字掉了", "m1", role=ROLE_ENGINEER, sender="uid-eng")

    assert env.db.get_ticket(tid)["status"] == TICKET_NEGOTIATING
    assert any("待商榷" in s and "不能执行" in s for s in env.sent), env.sent
    assert not any(NO_ACTIVE_TEXT in s for s in env.sent), env.sent


@pytest.mark.asyncio
async def test_active_ticket_regression_unchanged(env: Env):
    """回归：正常活动工单的完工链路不受影响。"""
    tid = env.repo.create_ticket(
        group=GROUP, reporter_id="uid-mgr", subject="正常单", location="L",
        problem_description="P", sla_label="3天", now="2026-08-22 21:00:15",
    )
    env.classifier.preset("m1", "ticket.complete", fields={"completion_note": "已完成"})

    await env.process("001已完成", "m1", role=ROLE_MANAGER, sender="uid-mgr")

    assert env.db.get_ticket(tid)["status"] == TICKET_COMPLETED
    assert not any(NO_ACTIVE_TEXT in s for s in env.sent), env.sent
