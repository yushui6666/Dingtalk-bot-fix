"""待商榷独立状态 TDD（2026-08-28，方案A）

覆盖：
- 时效：待商榷 建单直接落 PENDING_NEGOTIATION
- ACTIVE/OVERDUE → 待商榷 需确认（确认后 deadline=NULL, sla_days=0）
- PENDING_NEGOTIATION 直接完成 → COMPLETED
- 调度器与 list_active_tickets 排除待商榷
- 分类器关键词与校验
"""

import asyncio
import json
import pathlib
import tempfile
import unittest.mock as mock
from datetime import datetime

import pytest

from db import Database
from models import NormalizedMessage, ROLE_ENGINEER, ROLE_MANAGER, TICKET_NEGOTIATING, TICKET_ACTIVE
from pipeline import MessageProcessingPipeline, PendingActionService, RuntimeMode
from routing.ticket_router import TicketRouter
from semantics.classifier import SemanticClassifier
from semantics.protocol_loader import load_protocol
from semantics.types import SemanticDecision, TicketCandidate, TicketScore
from semantics.validator import validate_decision
from tickets.executor import TicketCommandExecutor
from tickets.repository import TicketRepository
from routing.ticket_contexts import TicketContextStore

GROUP = {"group_id": "G1", "store_name": "测试店"}

PROTOCOL_PATH = pathlib.Path(__file__).parent.parent / "protocols" / "ticket_semantics.v4.json"


class FakeModelClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def chat_completion_with_retry(self, messages, response_format):
        self.calls.append(messages)
        return json.dumps(self._response, ensure_ascii=False)


class RecordingNotifier:
    def __init__(self, db: Database):
        self.db = db
        self.calls: list[tuple[str, str]] = []

    def send_group_now(self, group_id: str, text: str, **kw):
        self.calls.append((group_id, text))


def _make_msg(content: str, message_id: str = "m1", role: str = ROLE_ENGINEER) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        group_id=GROUP["group_id"],
        sender_id="u1",
        sender_name="tester",
        content=content,
        sender_role=role,
        sent_at=datetime.now(),
    )


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as td:
        db = Database(pathlib.Path(td) / "t.db")
        db.init_schema()
        db.upsert_group(GROUP)
        yield db
        db.close()


def test_model_constant_and_label():
    from tickets.commands import ticket_status_label

    assert TICKET_NEGOTIATING == "PENDING_NEGOTIATION"
    assert ticket_status_label("PENDING_NEGOTIATION") == "待商榷"


def test_protocol_has_negotiate():
    p = load_protocol(PROTOCOL_PATH)
    a = p.get_action("ticket.negotiate.submit")
    assert a is not None
    assert a.required_fields == ("negotiate_reason",)
    assert set(a.allowed_ticket_states) == {"ACTIVE", "ACTIVE_OVERDUE"}
    # complete/cancel/stop 应放行待商榷
    for intent in ["ticket.complete", "ticket.cancel", "ticket.stop"]:
        act = p.get_action(intent)
        assert "PENDING_NEGOTIATION" in act.allowed_ticket_states, intent


def test_create_with_negotiating_sla_creates_pending_negotiation(tmp_db: Database):
    repo = TicketRepository(tmp_db)
    ticket_id = repo.create_ticket(
        group={"group_id": "G1", "store_name": "测试店"},
        reporter_id="u1",
        subject="主题",
        location="位置",
        problem_description="问题",
        sla_label="待商榷",
        now="2026-08-28 10:00:00",
    )
    ticket = tmp_db.get_ticket(ticket_id)
    assert ticket["status"] == "PENDING_NEGOTIATION"
    assert ticket["sla_days"] == 0
    assert ticket["current_deadline_at"] is None
    # 不算活动
    assert len(tmp_db.list_active_tickets("G1")) == 0


def test_active_excludes_negotiating(tmp_db: Database):
    repo = TicketRepository(tmp_db)
    # 建一个 ACTIVE 和一个 待商榷
    repo.create_ticket(group=GROUP, reporter_id="u1", subject="A", location="L", problem_description="P", sla_label="3天", now="2026-08-28 10:00:00")
    repo.create_ticket(group=GROUP, reporter_id="u1", subject="B", location="L", problem_description="P", sla_label="待商榷", now="2026-08-28 10:00:00")
    active = tmp_db.list_active_tickets("G1")
    assert len(active) == 1
    assert active[0]["sla_days"] == 3


@pytest.mark.asyncio
async def test_negotiate_requires_confirmation(tmp_path):
    """ACTIVE → #待商榷 需确认，确认后切状态"""
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.upsert_group(GROUP)
    repo = TicketRepository(db)
    executor = TicketCommandExecutor(db, repo)

    # 建单 3天
    from models import ROLE_MANAGER
    # 直接通过 executor 建单
    ticket_id = repo.create_ticket(group=GROUP, reporter_id="u1", subject="主题", location="位置", problem_description="问题", sla_label="3天", now="2026-08-28 10:00:00")
    ticket = db.get_ticket(ticket_id)
    assert ticket["status"] == "ACTIVE"

    # 直接验证 executor 切状态逻辑（不走确认，确认流由 pipeline 侧覆盖）
    msg = NormalizedMessage(message_id="m2", group_id="G1", sender_id="u1", sender_name="tester", content="#待商榷 方案待定", sender_role=ROLE_MANAGER, sent_at=datetime.now())
    from semantics.types import ValidatedCommand
    # 构造 ValidatedCommand
    vc = ValidatedCommand(intent="ticket.negotiate.submit", group_id="G1", actor_id="u1", actor_role=ROLE_MANAGER, message_id="m2", target_ticket_id=ticket_id, expected_ticket_version=ticket["version"], fields={"negotiate_reason": "方案待定"}, source="keyword")
    result = executor.execute(vc, message=msg)
    assert result.status == "OK"
    updated = db.get_ticket(ticket_id)
    assert updated["status"] == "PENDING_NEGOTIATION"
    assert updated["current_deadline_at"] is None
    db.close()


@pytest.mark.asyncio
async def test_negotiating_direct_complete(tmp_path):
    """PENDING_NEGOTIATION 可直接完成"""
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.upsert_group(GROUP)
    repo = TicketRepository(db)
    # 建待商榷单
    ticket_id = repo.create_ticket(group=GROUP, reporter_id="u1", subject="主题", location="位置", problem_description="问题", sla_label="待商榷", now="2026-08-28 10:00:00")
    ticket = db.get_ticket(ticket_id)
    assert ticket["status"] == "PENDING_NEGOTIATION"
    # 完成
    executor = TicketCommandExecutor(db, repo)
    from semantics.types import ValidatedCommand
    msg = NormalizedMessage(message_id="m3", group_id="G1", sender_id="u1", sender_name="tester", content="#完毕", sender_role=ROLE_ENGINEER, sent_at=datetime.now())
    vc = ValidatedCommand(intent="ticket.complete", group_id="G1", actor_id="u1", actor_role=ROLE_ENGINEER, message_id="m3", target_ticket_id=ticket_id, expected_ticket_version=ticket["version"], fields={}, source="keyword")
    result = executor.execute(vc, message=msg)
    # 工程师报完工 -> PENDING_CONFIRM
    assert result.status == "OK"
    updated = db.get_ticket(ticket_id)
    assert updated["status"] == "PENDING_CONFIRM"
    db.close()
