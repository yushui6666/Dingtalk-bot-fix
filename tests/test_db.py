"""数据库层集成测试：20 张表、事务幂等、v4 多工单、upsert_group。

覆盖计划书 15 节数据模型 + Phase 1 清单：
- 一次性建全 20 张表及索引、唯一约束；v4.0 Task 5 已移除
  「每群最多一张非终态工单」部分唯一索引，支持同群多工单并行
- v4.1 Task 4A 新增 message_attachments 图片附件表
- processed_events 幂等表：业务处理与幂等写入同事务（回滚后不残留）
- 群配置 upsert：覆盖更新保留 ticket_seq
"""

from pathlib import Path

import pytest

from config import GROUPS, DB_PATH
from db import Database
from models import TICKET_ACTIVE, TICKET_COMPLETED, TICKET_OVERDUE

# 期望的 20 张业务表
EXPECTED_TABLES = {
    "groups",
    "tickets",
    "responsibility_cycles",
    "messages",
    "processed_events",
    "diagnosis_versions",
    "repair_method_versions",
    "timeout_cycles",
    "notification_deliveries",
    "schema_migrations",
    "inbox_messages",
    "semantic_decisions",
    "message_ticket_links",
    "message_attachments",
    "ticket_contexts",
    "pending_actions",
    "action_executions",
    "delivery_confirmations",
    "taobao_orders",
    "order_monitor",
}

# 期望的关键索引（v4：无单活动工单唯一索引）
EXPECTED_INDEXES = {
    "idx_tickets_group_status",   # 活动工单按 (group_id, status) 查询
    "idx_tickets_group",
    "idx_tickets_status",
    "idx_cycles_ticket",
    "idx_cycles_status_due",
    "idx_messages_ticket",
    "idx_diagnosis_ticket",
    "idx_method_ticket",
    "idx_timeout_ticket",
    "idx_timeout_one_waiting",  # 同工单唯一 WAITING_REASON 周期
    "idx_notify_status",
    "idx_inbox_status",
    "idx_inbox_group",
    "idx_semantic_decisions_msg",
    "idx_links_ticket",
    "idx_attachments_ticket",
    "idx_attachments_msg",
    "idx_pending_one_waiting",  # 同 (group,user) 唯一 WAITING
    "idx_pending_status_exp",
    "idx_exec_src",
}


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """每个测试独立临时数据库，互不污染。"""
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


# ─────────────────────── 1. schema 完整性 ───────────────────────

def test_all_19_tables_created(db: Database):
    tables = {
        r["name"] for r in db.connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert EXPECTED_TABLES <= tables, f"缺少表: {EXPECTED_TABLES - tables}"


def test_all_indexes_created(db: Database):
    idx = {
        r["name"] for r in db.connect().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert EXPECTED_INDEXES <= idx, f"缺少索引: {EXPECTED_INDEXES - idx}"


def test_old_single_active_constraint_removed(db: Database):
    """v4.0 Task 5：v3 遗留单活动工单唯一索引必须已删除。"""
    idx = {
        r["name"] for r in db.connect().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert "idx_tickets_one_active" not in idx


def test_init_schema_idempotent(db: Database):
    """重复 init_schema 不报错（CREATE IF NOT EXISTS）。"""
    db.init_schema()
    tables = db.connect().execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    assert tables == 22  # 含 part_versions（2026-09-09 备件登记表）


# ─────────────────────── 2. 事务与幂等 ───────────────────────

def test_record_processed_event_and_rollback(db: Database):
    """业务事务内写入 processed_events，回滚后不残留（同事务要求）。"""
    with pytest.raises(RuntimeError):
        with db.transaction("test_business_tx"):
            db.record_processed_event("msg-1", "g1", "ARCHIVED")
            raise RuntimeError("模拟业务失败")

    assert db.message_already_seen("msg-1") is False, "回滚后幂等记录不应残留"


def test_record_processed_event_committed(db: Database):
    with db.transaction("test_business_tx"):
        db.record_processed_event("msg-2", "g1", "ARCHIVED")
    assert db.message_already_seen("msg-2") is True


def test_duplicate_message_seen_once(db: Database):
    """同一 message_id 重复插入只保留一条（INSERT OR IGNORE）。"""
    with db.transaction("test_dup"):
        db.record_processed_event("msg-3", "g1", "ARCHIVED")
        db.record_processed_event("msg-3", "g1", "IGNORED")
    conn = db.connect()
    count = conn.execute(
        "SELECT COUNT(*) FROM processed_events WHERE message_id='msg-3'"
    ).fetchone()[0]
    assert count == 1
    # 保留第一次写入的结果
    result = conn.execute(
        "SELECT result FROM processed_events WHERE message_id='msg-3'"
    ).fetchone()[0]
    assert result == "ARCHIVED"


# ─────────────────────── 3. 多工单并行（v4.0 Task 5） ───────────────────────

def _insert_ticket(db: Database, ticket_no: str, group_id: str, status: str = TICKET_ACTIVE):
    conn = db.connect()
    conn.execute(
        """INSERT INTO tickets (ticket_no, group_id, store_name, reporter_id, subject,
           location, problem_description, sla_days, initial_deadline_at,
           current_deadline_at, status, version, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)""",
        (ticket_no, group_id, "店A", "uid-mgr", "主题", "位置", "描述",
         3, "2026-08-11 10:00:00", "2026-08-14 10:00:00", status, "2026-08-11 10:00:00"),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_same_group_allows_multiple_active_tickets(db: Database):
    """v4.0 Task 5：同群可同时存在多张 ACTIVE 工单。"""
    _insert_ticket(db, "店A-主题-001", "g1")
    _insert_ticket(db, "店A-主题-002", "g1")  # 不再报错
    active = db.list_active_tickets("g1")
    assert len(active) == 2


def test_two_groups_independent_active_tickets(db: Database):
    """不同群可各自有活动工单（隔离）。"""
    _insert_ticket(db, "店A-主题-001", "g1")
    _insert_ticket(db, "店B-主题-001", "g2")  # 不报错


def test_completed_does_not_block_new_ticket(db: Database):
    """第一张 COMPLETED 后同群可再建新单。"""
    _insert_ticket(db, "店A-主题-001", "g1", status=TICKET_COMPLETED)
    _insert_ticket(db, "店A-主题-002", "g1")  # 不报错


def test_overdue_and_active_coexist(db: Database):
    """v4.0 Task 5：ACTIVE_OVERDUE 与 ACTIVE 可同群并存。"""
    _insert_ticket(db, "店A-主题-001", "g1", status=TICKET_OVERDUE)
    _insert_ticket(db, "店A-主题-002", "g1")  # 不再报错
    assert len(db.list_active_tickets("g1")) == 2


def test_list_active_tickets_only_nonterminal(db: Database):
    """list_active_tickets 只返回 ACTIVE/ACTIVE_OVERDUE。"""
    _insert_ticket(db, "店A-主题-001", "g1")
    _insert_ticket(db, "店A-主题-002", "g1", status=TICKET_COMPLETED)
    nos = {t["ticket_no"] for t in db.list_active_tickets("g1")}
    assert nos == {"店A-主题-001"}


# ─────────────────────── 4. upsert_group ───────────────────────

def test_upsert_group_creates(db: Database):
    db.upsert_group(GROUPS[0])
    g = db.get_group(GROUPS[0]["group_id"])
    assert g is not None
    assert g["store_name"] == GROUPS[0]["store_name"]
    assert g["manager_ids"] == GROUPS[0]["manager_ids"]
    assert g["engineer_ids"] == GROUPS[0]["engineer_ids"]
    assert g["ticket_seq"] == 0


def test_upsert_group_update_keeps_ticket_seq(db: Database):
    """重复 upsert 更新角色，但保留已递增的 ticket_seq。"""
    db.upsert_group(GROUPS[0])
    with db.transaction("test_seq_bump"):
        db.connect().execute(
            "UPDATE groups SET ticket_seq=5 WHERE group_id=?", (GROUPS[0]["group_id"],)
        )
    # 再次 upsert（模拟配置热更新）
    db.upsert_group(GROUPS[0])
    g = db.get_group(GROUPS[0]["group_id"])
    assert g["ticket_seq"] == 5, "ticket_seq 不应被 upsert 重置"


def test_upsert_group_different_groups_isolation(db: Database):
    g2 = dict(GROUPS[0])
    g2["group_id"] = "cid-group-002"
    g2["store_name"] = "另一家店"
    db.upsert_group(GROUPS[0])
    db.upsert_group(g2)
    assert db.get_group("cid-group-002")["store_name"] == "另一家店"
    # 两群 ticket_seq 独立
    assert db.get_group(GROUPS[0]["group_id"])["ticket_seq"] == 0
    assert db.get_group("cid-group-002")["ticket_seq"] == 0
