"""调度器测试：待确认过期 + SLA 时效提醒（去重）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from db import Database
from notifier import Notifier
from workers.scheduler import SchedulerWorker

GROUP = {"group_id": "G1", "store_name": "测试店",
         "manager_ids": ["mgr"], "engineer_ids": ["eng"], "other_member_ids": ["staff"]}

NOW = datetime(2026, 8, 12, 12, 0, 0)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = Database(tmp_path / "sched.db")
    db.init_schema()
    db.upsert_group(GROUP)
    monkeypatch.setattr("config.ORDER_STORE_TABLE_PATH", tmp_path / "空.xlsx")  # 隔离共享表
    sent: list[str] = []
    notifier = Notifier(db, lambda target, text: sent.append(text))
    worker = SchedulerWorker(db=db, notifier=notifier, interval=60, remind_before_hours=6)
    yield SimpleNamespace(db=db, sent=sent, worker=worker, notifier=notifier)
    db.close()


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _insert_ticket(db: Database, ticket_no: str, deadline: str, status: str = "ACTIVE") -> int:
    with db.transaction("test_insert"):
        return db.insert_ticket({
            "ticket_no": ticket_no, "group_id": "G1", "store_name": "测试店",
            "reporter_id": "staff", "subject": "门", "location": "大厅",
            "problem_description": "坏了", "sla_days": 1,
            "initial_deadline_at": deadline, "current_deadline_at": deadline,
            "status": status,
        })


def test_sla_remind_default_before_hours_is_6(env):
    """到期提示默认提前量=6 小时（2026-08-30 用户决策，替代原 1 小时）。

    生产 main.py 构造 SchedulerWorker 时不传 remind_before_hours，
    走的正是 config.SLA_REMIND_BEFORE_HOURS 默认值；本测试钉住它，
    防止提前量被误改回 1 小时。
    """
    from config import SLA_REMIND_BEFORE_HOURS
    assert SLA_REMIND_BEFORE_HOURS == 6
    worker = SchedulerWorker(db=env.db, notifier=env.notifier, interval=60)
    assert worker._remind_before_hours == 6


def test_pending_expiry_resolves(env):
    _insert_ticket(env.db, "T1", "2026-08-13 10:00:00")
    # 直接插入一条过期 WAITING 待确认
    with env.db.transaction("seed_pending"):
        env.db._conn.execute(
            """INSERT INTO pending_actions (source_message_id, group_id, user_id, intent,
                   candidate_ticket_ids_json, fields_json, expected_versions_json,
                   status, version, created_at, expires_at)
               VALUES ('pm', 'G1', 'mgr', 'ticket.complete', '[]', '{}', '{}',
                       'WAITING', 0, ?, ?)""",
            (NOW.strftime("%Y-%m-%d %H:%M:%S"), "2026-08-01 00:00:00"),
        )
    env.worker.scan_pending_expiry(NOW)
    row = env.db.connect().execute(
        "SELECT status FROM pending_actions WHERE source_message_id='pm'").fetchone()
    assert row["status"] == "EXPIRED"


def test_sla_reminder_before_deadline(env):
    _insert_ticket(env.db, "T1", "2026-08-12 15:00:00")  # 3h 后到期，在 6h 窗口内
    _insert_ticket(env.db, "T2", "2026-08-20 10:00:00")  # 远期，不提醒
    sent = env.worker.scan_sla_reminders(NOW)
    assert sent == 1
    assert any("即将到期" in s and "T1" in s for s in env.sent)
    assert not any("T2" in s for s in env.sent)


def test_sla_reminder_overdue(env):
    _insert_ticket(env.db, "T1", "2026-08-11 10:00:00")  # 已超时
    sent = env.worker.scan_sla_reminders(NOW)
    assert sent == 1
    assert any("已超时效" in s and "T1" in s for s in env.sent)


def test_sla_reminder_dedup(env):
    _insert_ticket(env.db, "T1", "2026-08-12 15:00:00")
    env.worker.scan_sla_reminders(NOW)
    env.worker.scan_sla_reminders(NOW)  # 第二次扫描不应重复提醒
    remind_count = sum(1 for s in env.sent if "即将到期" in s)
    assert remind_count == 1
    # outbox 里去重键唯一
    rows = env.db.connect().execute(
        "SELECT dedupe_key FROM notification_deliveries WHERE notification_type='sla_remind'"
    ).fetchall()
    assert len(rows) == 1


def test_sla_reminder_shadow_mode_dedup(env, caplog):
    """影子模式下同一提醒只记录一次，不向群内发送、不污染 Outbox。"""
    shadow = Notifier(db=env.db, sender=lambda t, x: env.sent.append(x), enabled=False)
    worker = SchedulerWorker(db=env.db, notifier=shadow, interval=60, remind_before_hours=6)
    _insert_ticket(env.db, "T1", "2026-08-12 15:00:00")
    with caplog.at_level("INFO"):
        worker.scan_sla_reminders(NOW)
        worker.scan_sla_reminders(NOW)  # 第二次不应再刷日志
    assert env.sent == []  # 影子模式不外发
    logs = [r.message for r in caplog.records if "跳过去重群消息" in r.message]
    assert len(logs) == 1
    rows = env.db.connect().execute(
        "SELECT COUNT(*) AS n FROM notification_deliveries WHERE notification_type='sla_remind'"
    ).fetchone()
    assert rows["n"] == 0


def test_completed_ticket_not_reminded(env):
    _insert_ticket(env.db, "T1", "2026-08-12 14:00:00", status="COMPLETED")
    sent = env.worker.scan_sla_reminders(NOW)
    assert sent == 0


def test_reopen_clears_sla_dedupe(env):
    """重开后清理 SLA 去重键：已解释原因(EXTENDED)的工单重开可再次触发提醒并开新周期。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-11 10:00:00")  # 已超时
    env.worker.scan_sla_reminders(NOW)  # 首次提醒 → 建 WAITING_REASON 周期 + 写入去重键
    env.db.add_timeout_cycle_reason(ticket_id, "m1", "等配件", "eng")  # 已解释 → EXTENDED
    # 模拟重开工单（status 回 ACTIVE，走 _execute_reopen 同款清理）
    ticket = env.db.get_ticket(ticket_id)
    assert env.db.update_ticket_cas(
        ticket_id, ticket["version"], "status=?, closed_at=NULL, reopen_count=reopen_count+1,"
        " waiting_side='NONE', waiting_since=NULL, current_responsibility_cycle_id=NULL,"
        " last_business_event_at=?, last_business_message_id=?",
        ("ACTIVE", NOW.strftime("%Y-%m-%d %H:%M:%S"), "m-reopen"),
    )
    assert env.db.clear_ticket_sla_dedupe(ticket_id) >= 1
    env.worker.scan_sla_reminders(NOW)  # 重开后再次扫描 → 重新提醒并新建周期
    assert sum(1 for s in env.sent if "已超时效" in s) == 2
    rows = env.db.connect().execute(
        "SELECT id, status FROM timeout_cycles WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()
    assert [r["status"] for r in rows] == ["EXTENDED", "WAITING_REASON"]


def test_shadow_mode_opens_timeout_cycle(env):
    """影子模式下状态推进仍应建超时周期，避免工程师回 #超时原因 被拒。"""
    shadow = Notifier(db=env.db, sender=lambda t, x: None, enabled=False)
    worker = SchedulerWorker(db=env.db, notifier=shadow, interval=60, remind_before_hours=6)
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-11 10:00:00")  # 已超时
    worker.scan_sla_reminders(NOW)
    ticket = env.db.get_ticket(ticket_id)
    assert ticket["status"] == "ACTIVE_OVERDUE"
    assert ticket["current_timeout_cycle_id"] is not None


def test_sla_overdue_opens_waiting_reason_cycle(env):
    """超时提醒时建立 WAITING_REASON 周期并回填 current_timeout_cycle_id（幂等）。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-11 10:00:00")  # 已超时
    env.worker.scan_sla_reminders(NOW)
    env.worker.scan_sla_reminders(NOW)  # 第二次扫描不应重复建周期

    rows = env.db.connect().execute(
        "SELECT * FROM timeout_cycles WHERE ticket_id=?", (ticket_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "WAITING_REASON"
    assert rows[0]["old_deadline_at"] == "2026-08-11 10:00:00"
    assert rows[0]["reason"] is None
    ticket = env.db.get_ticket(ticket_id)
    assert ticket["current_timeout_cycle_id"] == rows[0]["id"]


def test_timeout_reason_resolves_waiting_cycle(env):
    """工程师回 #超时原因 → 周期 WAITING_REASON → EXTENDED，只接受一次原因。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-11 10:00:00")
    env.worker.scan_sla_reminders(NOW)
    ok = env.db.add_timeout_cycle_reason(ticket_id, "m1", "合页物流延迟未到货", "eng")
    assert ok is True
    # 同一周期第二次提交原因应被拒绝（已 EXTENDED，无未解释周期）
    ok2 = env.db.add_timeout_cycle_reason(ticket_id, "m2", "再次说明原因", "eng")
    assert ok2 is False

    rows = env.db.connect().execute(
        "SELECT * FROM timeout_cycles WHERE ticket_id=? ORDER BY cycle_no", (ticket_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "EXTENDED"
    assert rows[0]["reason"] == "合页物流延迟未到货"
    assert rows[0]["reason_engineer_id"] == "eng"
    assert rows[0]["reason_submitted_at"] is not None


def test_timeout_reason_without_scheduler_rejected(env):
    """未走调度器提醒（无 WAITING_REASON 周期）时提交原因应被拒绝。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-11 10:00:00")
    ok = env.db.add_timeout_cycle_reason(ticket_id, "m1", "配件缺货", "eng")
    assert ok is False
    rows = env.db.connect().execute(
        "SELECT * FROM timeout_cycles WHERE ticket_id=?", (ticket_id,)).fetchall()
    assert len(rows) == 0


def test_sla_reminder_overdue_single_cycle(env):
    """已超时并解释过原因后，后续扫描不得重复开新周期。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-11 10:00:00")
    env.worker.scan_sla_reminders(NOW)  # 首次超时提醒 → 建周期
    env.db.add_timeout_cycle_reason(ticket_id, "m1", "等配件", "eng")  # 已解释 → EXTENDED
    env.worker.scan_sla_reminders(NOW)  # 再扫：提醒已去重，不应再开周期
    rows = env.db.connect().execute(
        "SELECT * FROM timeout_cycles WHERE ticket_id=?", (ticket_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "EXTENDED"


def test_sla_overdue_marks_ticket_active_overdue(env):
    """计划书 §9.1：超时工单状态 ACTIVE → ACTIVE_OVERDUE（幂等）。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-11 10:00:00")
    env.worker.scan_sla_reminders(NOW)
    ticket = env.db.get_ticket(ticket_id)
    assert ticket["status"] == "ACTIVE_OVERDUE"
    # 再次扫描不重复提醒，状态保持
    env.worker.scan_sla_reminders(NOW)
    assert env.db.get_ticket(ticket_id)["status"] == "ACTIVE_OVERDUE"


def test_sla_not_overdue_keeps_active(env):
    """未超时工单状态保持 ACTIVE。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-12 15:00:00")  # 3h 后到期
    env.worker.scan_sla_reminders(NOW)
    assert env.db.get_ticket(ticket_id)["status"] == "ACTIVE"


def test_completed_ticket_not_marked_overdue(env):
    """已终态工单不被标记为 ACTIVE_OVERDUE。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-11 10:00:00", status="COMPLETED")
    env.worker.scan_sla_reminders(NOW)
    assert env.db.get_ticket(ticket_id)["status"] == "COMPLETED"


def test_switch_responsibility_manager_to_engineer(env):
    """店长发消息 → 等待工程师方，记录责任周期。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-20 10:00:00")
    switched = env.db.switch_responsibility(ticket_id, "MANAGER", "m1", "2026-08-17 10:00:00")
    assert switched is True
    ticket = env.db.get_ticket(ticket_id)
    assert ticket["waiting_side"] == "ENGINEER_SIDE"
    assert ticket["waiting_since"] == "2026-08-17 10:00:00"
    rows = env.db.connect().execute(
        "SELECT * FROM responsibility_cycles WHERE ticket_id=?", (ticket_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["waiting_side"] == "ENGINEER_SIDE"
    assert rows[0]["trigger_message_id"] == "m1"
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["due_at"] == "2026-08-17 14:00:00"  # +4h


def test_switch_responsibility_engineer_back_to_manager(env):
    """工程师回复 → 等待店长方，旧周期关闭、新周期开启。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-20 10:00:00")
    env.db.switch_responsibility(ticket_id, "MANAGER", "m1", "2026-08-17 10:00:00")
    switched = env.db.switch_responsibility(ticket_id, "ENGINEER", "m2", "2026-08-17 10:30:00")
    assert switched is True
    ticket = env.db.get_ticket(ticket_id)
    assert ticket["waiting_side"] == "MANAGER_SIDE"
    assert ticket["waiting_since"] == "2026-08-17 10:30:00"
    rows = env.db.connect().execute(
        "SELECT * FROM responsibility_cycles WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "CANCELLED"
    assert rows[0]["closed_by_message_id"] == "m2"
    assert rows[1]["status"] == "PENDING"
    assert rows[1]["waiting_side"] == "MANAGER_SIDE"


def test_switch_responsibility_other_role_ignored(env):
    """其他成员消息不切换责任方。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-20 10:00:00")
    switched = env.db.switch_responsibility(ticket_id, "OTHER", "m1", "2026-08-17 10:00:00")
    assert switched is False
    ticket = env.db.get_ticket(ticket_id)
    assert ticket["waiting_side"] == "NONE"
    rows = env.db.connect().execute(
        "SELECT * FROM responsibility_cycles WHERE ticket_id=?", (ticket_id,)).fetchall()
    assert len(rows) == 0


def test_close_responsibility_cycles_on_complete(env):
    """完成/取消终态关闭所有未决责任周期。"""
    ticket_id = _insert_ticket(env.db, "T1", "2026-08-20 10:00:00")
    env.db.switch_responsibility(ticket_id, "MANAGER", "m1", "2026-08-17 10:00:00")
    closed = env.db.close_responsibility_cycles(ticket_id, "m2")
    assert closed == 1
    rows = env.db.connect().execute(
        "SELECT * FROM responsibility_cycles WHERE ticket_id=?", (ticket_id,)).fetchall()
    assert rows[0]["status"] == "CANCELLED"
    assert rows[0]["closed_by_message_id"] == "m2"
