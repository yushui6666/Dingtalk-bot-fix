import pytest

from db import Database
from semantics.types import ValidatedCommand
from tickets.executor import RESULT_INTERNAL_ERROR, TicketCommandExecutor
from tickets.repository import TicketRepository


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "executor.db")
    database.init_schema()
    database.upsert_group({"group_id": "G1", "store_name": "测试店"})
    yield database
    database.close()


def test_failed_execution_replay_preserves_failure(db):
    executor = TicketCommandExecutor(db, TicketRepository(db))
    command = ValidatedCommand(
        message_id="replay-failure",
        group_id="G1",
        actor_id="u1",
        actor_role="MANAGER",
        intent="ticket.complete",
        target_ticket_id=999,
        expected_ticket_version=1,
        fields={},
        source="keyword",
    )

    first = executor.execute(command)
    replay = executor.execute(command)

    assert first.status == RESULT_INTERNAL_ERROR
    assert replay.status == RESULT_INTERNAL_ERROR


def test_executor_rejects_stale_expected_ticket_version(db):
    with db.transaction("seed_ticket"):
        ticket_id = db.insert_ticket({
            "ticket_no": "T1", "group_id": "G1", "store_name": "测试店",
            "reporter_id": "u1", "subject": "门", "location": "前台",
            "problem_description": "坏了", "sla_days": 1,
            "initial_deadline_at": "2026-08-21 10:00:00",
            "current_deadline_at": "2026-08-21 10:00:00", "status": "ACTIVE",
        })
    current = db.get_ticket(ticket_id)
    assert db.update_ticket_cas(
        ticket_id, current["version"], "subject=?", ("已被别人更新",)
    )
    command = ValidatedCommand(
        message_id="stale-version", group_id="G1", actor_id="u1", actor_role="MANAGER",
        intent="ticket.complete", target_ticket_id=ticket_id,
        expected_ticket_version=1, fields={}, source="keyword",
    )

    result = TicketCommandExecutor(db, TicketRepository(db)).execute(command)

    assert result.status == "REJECTED"
    assert db.get_ticket(ticket_id)["status"] == "ACTIVE"
