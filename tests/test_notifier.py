import pytest

from db import Database
from notifier import Notifier


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.init_schema()
    yield database
    database.close()


def test_duplicate_immediate_reply_is_sent_once(db):
    sent = []
    notifier = Notifier(db, lambda target, text: sent.append((target, text)))

    notifier.send_group_now("G1", "同一回复", message_id="reply-1")
    notifier.send_group_now("G1", "同一回复", message_id="reply-1")

    assert sent == [("G1", "同一回复")]


def test_disabled_immediate_reply_does_not_enqueue_outbox(db):
    sent = []
    notifier = Notifier(db, lambda target, text: sent.append((target, text)), enabled=False)

    notifier.send_group_now("G1", "影子提示", message_id="shadow-reply")

    assert sent == []
    row = db.connect().execute(
        "SELECT COUNT(*) AS n FROM notification_deliveries"
    ).fetchone()
    assert row["n"] == 0


def test_failed_immediate_reply_flushes_original_payload(db):
    attempts = []

    def sender(target, text):
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError("temporary")

    notifier = Notifier(db, sender)
    notifier.send_group_now("G1", "原始失败提示", message_id="failed-reply")
    notifier.flush()

    assert attempts == ["原始失败提示", "原始失败提示"]


def _outbox_status(db):
    return [dict(r) for r in db.connect().execute(
        "SELECT target_id, status, last_error FROM notification_deliveries"
    )]


def test_failed_dedup_user_marks_outbox_failed(db):
    """发送失败必须标 FAILED（此前误标 SENT，2026-08-24 修复）。"""
    def broken_sender(target, text):
        raise RuntimeError("dws 退出码 1: boom")

    notifier = Notifier(db, broken_sender)
    ok = notifier.send_deduped_user("u1", "升级提醒", dedupe_key="resp_dm:1:0")

    assert ok is False
    rows = _outbox_status(db)
    assert len(rows) == 1
    assert rows[0]["target_id"] == "user:u1"
    assert rows[0]["status"] == "FAILED"
    assert "boom" in rows[0]["last_error"]

    # 同 key 再调不重发（去重生效，也不会覆盖 FAILED 记录）
    ok2 = notifier.send_deduped_user("u1", "升级提醒", dedupe_key="resp_dm:1:0")
    assert ok2 is False
    assert len(_outbox_status(db)) == 1


def test_failed_dedup_group_marks_outbox_failed(db):
    notifier = Notifier(db, lambda target, text: (_ for _ in ()).throw(RuntimeError("down")))
    ok = notifier.send_deduped_group("G1", text="时效提醒", dedupe_key="sla_overdue:1")

    assert ok is False
    rows = _outbox_status(db)
    assert rows[0]["status"] == "FAILED"


def test_successful_dedup_user_marks_sent(db):
    sent = []
    notifier = Notifier(db, lambda target, text: sent.append(target))
    ok = notifier.send_deduped_user("u9", "升级提醒", dedupe_key="resp_dm:9:0")

    assert ok is True
    assert sent == ["user:u9"]
    assert _outbox_status(db)[0]["status"] == "SENT"


def test_outbox_claim_is_exclusive_across_database_connections(tmp_path):
    path = tmp_path / "shared.db"
    first_db = Database(path)
    second_db = Database(path)
    first_db.init_schema()
    with first_db.transaction("seed_notification"):
        first_db.insert_notification(
            dedupe_key="claim-once", ticket_id=None, notification_type="group_text",
            target_type="group", target_id="G1", payload_text="一次",
        )

    first_claim = first_db.claim_pending_notifications()
    second_claim = second_db.claim_pending_notifications()

    assert [row["dedupe_key"] for row in first_claim] == ["claim-once"]
    assert second_claim == []
    first_db.close()
    second_db.close()
