"""SQLite 数据层：表、索引、唯一约束、事务封装与 v4 多工单迁移。

v4.0 Task 5 已落地：
- 删除 v3 遗留「每群最多一张非终态工单」部分唯一索引，支持同群多工单并行。
- tickets 新增 version（乐观并发）/cancelled_*/duplicate_of_ticket_id/reopen_count。
- 新增 inbox_messages / semantic_decisions / message_ticket_links / ticket_contexts /
  pending_actions / action_executions / schema_migrations。

既有表（groups/tickets/responsibility_cycles/messages/processed_events/
diagnosis_versions/repair_method_versions/timeout_cycles/notification_deliveries）继续保留。

关键约束：
- messages.message_id 唯一；processed_events.message_id 主键
- diagnosis/repair_method 的 source_message_id 唯一（一条消息只产生一个版本）
- notification_deliveries.dedupe_key 唯一（Outbox 防重复）
- pending_actions 同一 (group_id, user_id) 至多一条 WAITING（部分唯一索引）
- action_executions.dedupe_key 唯一（防崩溃重复执行）

所有业务状态变更必须走 :meth:`Database.transaction`，保证同一事务内
写业务状态 + 写 processed_events + 预写通知 Outbox。
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from config import DB_PATH, SIDE_REPLY_TIMEOUT_HOURS
from logger import get_logger
from models import (
    CYCLE_CANCELLED,
    ROLE_ENGINEER,
    ROLE_LEADER,
    ROLE_MANAGER,
    TICKET_ACTIVE,
    TICKET_OVERDUE,
    WAITING_ENGINEER_SIDE,
    WAITING_MANAGER_SIDE,
    WAITING_NONE,
)

logger = get_logger(__name__)

_SCHEMA = """-- ─────────────────────── groups ───────────────────────
CREATE TABLE IF NOT EXISTS groups (
    group_id               TEXT PRIMARY KEY,
    store_name             TEXT NOT NULL,
    manager_ids            TEXT NOT NULL DEFAULT '[]',
    engineer_ids           TEXT NOT NULL DEFAULT '[]',
    other_member_ids       TEXT NOT NULL DEFAULT '[]',
    engineering_leader_id  TEXT NOT NULL DEFAULT '',
    regional_manager_id    TEXT NOT NULL DEFAULT '',
    current_ticket_id      INTEGER,
    ticket_seq             INTEGER NOT NULL DEFAULT 0,
    is_active              INTEGER NOT NULL DEFAULT 1
);

-- ─────────────────────── tickets ───────────────────────
CREATE TABLE IF NOT EXISTS tickets (
    id                             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no                      TEXT NOT NULL UNIQUE,
    group_id                       TEXT NOT NULL,
    store_name                     TEXT NOT NULL,
    reporter_id                    TEXT NOT NULL,
    subject                        TEXT NOT NULL,
    location                       TEXT NOT NULL,
    problem_description            TEXT NOT NULL,
    sla_days                       INTEGER NOT NULL,
    initial_deadline_at            TEXT,
    current_deadline_at            TEXT,
    current_timeout_cycle_id       INTEGER,
    status                         TEXT NOT NULL DEFAULT 'ACTIVE',
    waiting_side                   TEXT NOT NULL DEFAULT 'NONE',
    waiting_since                  TEXT,
    current_responsibility_cycle_id INTEGER,
    last_business_event_at         TEXT,
    last_business_message_id       TEXT,
    version                        INTEGER NOT NULL DEFAULT 1,
    cancelled_at                   TEXT,
    cancelled_by                   TEXT,
    cancel_reason                  TEXT,
    stopped_at                     TEXT,
    stopped_by                     TEXT,
    stop_reason                    TEXT,
    duplicate_of_ticket_id         INTEGER,
    reopen_count                   INTEGER NOT NULL DEFAULT 0,
    created_at                     TEXT NOT NULL,
    closed_at                      TEXT,
    completed_confirm_by           TEXT,
    completed_confirm_at           TEXT
);

-- v4.0 Task 5 已删除单活动工单唯一索引，支持同群多工单并行。
-- 活动工单按 (group_id, status) 查询。
CREATE INDEX IF NOT EXISTS idx_tickets_group_status ON tickets(group_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_group ON tickets(group_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

-- ─────────────────────── responsibility_cycles ───────────────────────
CREATE TABLE IF NOT EXISTS responsibility_cycles (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id          INTEGER NOT NULL,
    waiting_side       TEXT NOT NULL,
    trigger_message_id TEXT NOT NULL,
    waiting_since      TEXT NOT NULL,
    due_at             TEXT,
    status             TEXT NOT NULL DEFAULT 'PENDING',
    claimed_at         TEXT,
    closed_by_message_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycles_ticket ON responsibility_cycles(ticket_id);
CREATE INDEX IF NOT EXISTS idx_cycles_status_due ON responsibility_cycles(status, due_at);

-- ─────────────────────── messages ───────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id    INTEGER NOT NULL,
    message_id   TEXT NOT NULL UNIQUE,
    sender_id    TEXT NOT NULL,
    sender_role  TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    message_type TEXT NOT NULL DEFAULT 'text',
    sent_at      TEXT NOT NULL,
    raw_event    TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_ticket ON messages(ticket_id);

-- ─────────────────────── processed_events ───────────────────────
CREATE TABLE IF NOT EXISTS processed_events (
    message_id  TEXT PRIMARY KEY,
    group_id    TEXT NOT NULL,
    received_at TEXT NOT NULL,
    result      TEXT NOT NULL
);

-- ─────────────────────── diagnosis_versions ───────────────────────
CREATE TABLE IF NOT EXISTS diagnosis_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id         INTEGER NOT NULL,
    source_message_id TEXT NOT NULL UNIQUE,
    items_json        TEXT NOT NULL,
    engineer_id       TEXT NOT NULL,
    submitted_at      TEXT NOT NULL,
    is_current        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_diagnosis_ticket ON diagnosis_versions(ticket_id);

-- ─────────────────────── repair_method_versions ───────────────────────
CREATE TABLE IF NOT EXISTS repair_method_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id         INTEGER NOT NULL,
    source_message_id TEXT NOT NULL UNIQUE,
    repair_method     TEXT NOT NULL,
    order_no          TEXT,
    engineer_id       TEXT NOT NULL,
    submitted_at      TEXT NOT NULL,
    is_current        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_method_ticket ON repair_method_versions(ticket_id);

-- ─────────────────────── part_versions ───────────────────────
-- 记录工单本次维修使用的备件（规范名，非工程师原始措辞）。
-- 仅记录：不做库存增减。同一 source_message_id 只允许一条，is_current=1 为最新。
CREATE TABLE IF NOT EXISTS part_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id         INTEGER NOT NULL,
    source_message_id TEXT NOT NULL UNIQUE,
    parts             TEXT NOT NULL,          -- 规范备件名，多个用顿号连接，如 "继电器DC12V、干簧管"
    raw_text          TEXT,                   -- 工程师原始措辞（留档用）
    engineer_id       TEXT NOT NULL,
    submitted_at      TEXT NOT NULL,
    is_current        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_part_ticket ON part_versions(ticket_id);

-- ─────────────────────── timeout_cycles ───────────────────────
CREATE TABLE IF NOT EXISTS timeout_cycles (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id          INTEGER NOT NULL,
    cycle_no           INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'WAITING_REASON',
    old_deadline_at    TEXT NOT NULL,
    reminded_at        TEXT NOT NULL,
    reason             TEXT,
    reason_engineer_id TEXT,
    reason_submitted_at TEXT,
    new_deadline_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_timeout_ticket ON timeout_cycles(ticket_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_timeout_one_waiting
    ON timeout_cycles(ticket_id) WHERE status = 'WAITING_REASON';

-- ─────────────────────── notification_deliveries ───────────────────────
CREATE TABLE IF NOT EXISTS notification_deliveries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key        TEXT NOT NULL UNIQUE,
    ticket_id         INTEGER,
    notification_type TEXT NOT NULL,
    target_type       TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    scheduled_at      TEXT,
    payload_text      TEXT,
    claimed_at        TEXT,
    sent_at           TEXT,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_notify_status ON notification_deliveries(status, scheduled_at);

-- ─────────────────────── schema_migrations（v4.0 Task 5） ───────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- ─────────────────────── inbox_messages（v4.0 Task 5/6） ───────────────────────
CREATE TABLE IF NOT EXISTS inbox_messages (
    message_id          TEXT PRIMARY KEY,
    group_id            TEXT NOT NULL,
    sender_id           TEXT NOT NULL,
    sender_role         TEXT NOT NULL,
    content             TEXT NOT NULL DEFAULT '',
    message_type        TEXT NOT NULL DEFAULT 'text',
    reply_to_message_id TEXT,
    sent_at             TEXT NOT NULL,
    received_at         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'RECEIVED',
    processed_result    TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT,
    last_error          TEXT,
    claimed_by          TEXT,
    claimed_at          TEXT,
    lease_until         TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_messages(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_inbox_group ON inbox_messages(group_id, sent_at);

-- ─────────────────────── semantic_decisions（v4.0 Task 5） ───────────────────────
CREATE TABLE IF NOT EXISTS semantic_decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id           TEXT NOT NULL,
    protocol_version     TEXT NOT NULL,
    source               TEXT NOT NULL,
    intent               TEXT NOT NULL,
    target_ticket_no     TEXT,
    confidence           REAL NOT NULL,
    fields_json          TEXT NOT NULL DEFAULT '{}',
    missing_fields_json  TEXT NOT NULL DEFAULT '[]',
    evidence_json        TEXT NOT NULL DEFAULT '[]',
    decision_status      TEXT NOT NULL DEFAULT 'RECORDED',
    errors_json          TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semantic_decisions_msg ON semantic_decisions(message_id);

-- ─────────────────────── message_ticket_links（v4.0 Task 5） ───────────────────────
CREATE TABLE IF NOT EXISTS message_ticket_links (
    message_id    TEXT PRIMARY KEY,
    ticket_id     INTEGER NOT NULL,
    link_type     TEXT NOT NULL,
    routing_score REAL NOT NULL DEFAULT 0,
    linked_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_ticket ON message_ticket_links(ticket_id);

-- ─────────────────────── message_attachments（v4.1 Task 4A 图片附件存储） ───────────────────────
-- 消息到达时写元数据（source 信息），归档成功后回填 stored_path/sha256 等；
-- 工单归属后回填 ticket_id；工单结束后由分析层消费。
CREATE TABLE IF NOT EXISTS message_attachments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          TEXT NOT NULL,
    attachment_index    INTEGER NOT NULL,
    ticket_id           INTEGER,
    source_type         TEXT NOT NULL,
    source_ref          TEXT NOT NULL,
    file_name           TEXT,
    declared_mime_type  TEXT,
    stored_path         TEXT,
    sha256              TEXT,
    byte_size           INTEGER,
    mime_type           TEXT,
    analyzed_status     TEXT NOT NULL DEFAULT 'PENDING',
    vision_result_json  TEXT,
    analyzed_at         TEXT,
    error               TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (message_id, attachment_index)
);
CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON message_attachments(ticket_id, analyzed_status);
CREATE INDEX IF NOT EXISTS idx_attachments_msg ON message_attachments(message_id);

-- ─────────────────────── ticket_contexts（v4.0 Task 7） ───────────────────────
CREATE TABLE IF NOT EXISTS ticket_contexts (
    group_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    ticket_id  INTEGER NOT NULL,
    order_key  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

-- ─────────────────────── pending_actions（v4.0 Task 8） ───────────────────────
CREATE TABLE IF NOT EXISTS pending_actions (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id          TEXT NOT NULL,
    group_id                   TEXT NOT NULL,
    user_id                    TEXT NOT NULL,
    intent                     TEXT NOT NULL,
    candidate_ticket_ids_json  TEXT NOT NULL DEFAULT '[]',
    fields_json                TEXT NOT NULL DEFAULT '{}',
    expected_versions_json     TEXT NOT NULL DEFAULT '{}',
    status                     TEXT NOT NULL DEFAULT 'WAITING',
    version                    INTEGER NOT NULL DEFAULT 0,
    created_at                 TEXT NOT NULL,
    expires_at                 TEXT NOT NULL,
    confirmed_message_id       TEXT,
    resolved_at                TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_one_waiting
    ON pending_actions(group_id, user_id) WHERE status = 'WAITING';
CREATE INDEX IF NOT EXISTS idx_pending_status_exp ON pending_actions(status, expires_at);

-- ─────────────────────── action_executions（v4.0 Task 9） ───────────────────────
CREATE TABLE IF NOT EXISTS action_executions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key              TEXT NOT NULL UNIQUE,
    source_message_id       TEXT NOT NULL,
    confirmation_message_id TEXT,
    pending_action_id       INTEGER,
    intent                  TEXT NOT NULL,
    target_ticket_id        INTEGER,
    command_json            TEXT NOT NULL DEFAULT '{}',
    status                  TEXT NOT NULL DEFAULT 'PENDING',
    created_at              TEXT NOT NULL,
    applied_at              TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_src ON action_executions(source_message_id);

-- ─────────────────────── delivery_confirmations（v4.0 快递签收确认） ───────────────────────
CREATE TABLE IF NOT EXISTS delivery_confirmations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id          INTEGER NOT NULL,
    order_no           TEXT NOT NULL,
    group_id           TEXT NOT NULL,
    confirm_user_id    TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'WAITING',
    created_at         TEXT NOT NULL,
    resolved_at        TEXT,
    resolve_message_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_one_waiting
    ON delivery_confirmations(ticket_id, order_no) WHERE status = 'WAITING';
CREATE INDEX IF NOT EXISTS idx_delivery_status ON delivery_confirmations(status);

-- ─────────────────────── order_monitor（订单↔工单监控） ───────────────────────
CREATE TABLE IF NOT EXISTS order_monitor (
    order_id          TEXT PRIMARY KEY,
    ticket_id         INTEGER NOT NULL,
    store             TEXT,
    ticket_no         TEXT,
    last_status       TEXT NOT NULL DEFAULT '',
    shipped_notified  INTEGER NOT NULL DEFAULT 0,
    closed_notified   INTEGER NOT NULL DEFAULT 0,
    received_at       TEXT,
    received_notified INTEGER NOT NULL DEFAULT 0,
    xlsx_synced       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- ─────────────────────── taobao_orders（淘宝对账表导入） ───────────────────────
CREATE TABLE IF NOT EXISTS taobao_orders (
    order_id        TEXT PRIMARY KEY,
    product_summary TEXT,
    tracking_number TEXT,
    address         TEXT,
    status          TEXT,
    source          TEXT,
    updated_at      TEXT
);

-- ─────────────────────── ticket_special_cases（特殊情况暂停，2026-08-26） ───────────────────────
-- 责任方回复「特殊情况：原因；预计恢复：时间」后建立一条暂停记录；
-- resumed_at IS NULL 即为生效中的暂停：时效 SLA / 响应 SLA / 签收后每日催均豁免，
-- 实际业务动作或再次声明时关闭，并按「实际暂停时长」顺延 current_deadline_at。
CREATE TABLE IF NOT EXISTS ticket_special_cases (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id            INTEGER NOT NULL,
    source_message_id    TEXT NOT NULL UNIQUE,
    reason               TEXT NOT NULL,
    expected_resume_text TEXT,
    expected_resume_at   TEXT,
    paused_deadline_at   TEXT,
    submitted_by         TEXT NOT NULL,
    submitted_at         TEXT NOT NULL,
    resumed_at           TEXT,
    resume_message_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_special_case_ticket ON ticket_special_cases(ticket_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_special_case_one_active
    ON ticket_special_cases(ticket_id) WHERE resumed_at IS NULL;
"""


# v3.0 → v4.0：tickets 表迁移新增列（对已存在旧库幂等 ALTER）
_TICKET_MIGRATION_COLUMNS = {
    "version": "version INTEGER NOT NULL DEFAULT 1",
    "cancelled_at": "cancelled_at TEXT",
    "cancelled_by": "cancelled_by TEXT",
    "cancel_reason": "cancel_reason TEXT",
    "stopped_at": "stopped_at TEXT",
    "stopped_by": "stopped_by TEXT",
    "stop_reason": "stop_reason TEXT",
    "duplicate_of_ticket_id": "duplicate_of_ticket_id INTEGER",
    "reopen_count": "reopen_count INTEGER NOT NULL DEFAULT 0",
    # 2026-08-24 店长确认完工留痕
    "completed_confirm_by": "completed_confirm_by TEXT",
    "completed_confirm_at": "completed_confirm_at TEXT",
}


def _now_str() -> str:
    # TODO(v4.0) 应与 ordering.TZ (Asia/Shanghai) 对齐为 aware datetime，
    # 当前 datetime.now() 为 naive，与 ordering 模块的时区感知时间
    # 混用可能导致跨模块时间比较错误。
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """SQLite 连接与事务封装。单进程 asyncio 下串行使用。"""

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # ─────────────────────── 连接与建表 ───────────────────────
    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                # autocommit：裸 UPDATE 不会隐式开事务；事务统一由 transaction() 显式控制
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            logger.info("数据库连接建立 path=%s", self.db_path)
        return self._conn

    def init_schema(self) -> None:
        # executescript 会隐式 COMMIT，不能用 SAVEPOINT 事务包裹。
        conn = self.connect()
        conn.executescript(_SCHEMA)
        self._apply_migrations(conn)
        # 统计表数量做校验
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        logger.info(
            "数据库 schema 就绪 tables=%d (%s)",
            len(tables),
            ",".join(r["name"] for r in tables),
        )

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        """v3.0 → v4.0 幂等迁移（对已存在的旧数据库执行）。"""
        # 1. 删除 v3 遗留单活动工单唯一索引
        conn.execute("DROP INDEX IF EXISTS idx_tickets_one_active")

        # 2. tickets 新增列（幂等：先查 PRAGMA table_info 再决定是否 ALTER）
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        for column, ddl in _TICKET_MIGRATION_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {ddl}")

        # 3. order_monitor 新增签收字段（v4.1：到货签收后开始计时，不再下单自动延期）
        om_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(order_monitor)").fetchall()
        }
        _ORDER_MONITOR_MIGRATION_COLUMNS = {
            "received_at": "received_at TEXT",
            "received_notified": "received_notified INTEGER NOT NULL DEFAULT 0",
            # 共享表写入兜底（2026-08-25）：0=尚未确认写入 订单门店状态表.xlsx
            "xlsx_synced": "xlsx_synced INTEGER NOT NULL DEFAULT 0",
        }
        for column, ddl in _ORDER_MONITOR_MIGRATION_COLUMNS.items():
            if column not in om_cols:
                conn.execute(f"ALTER TABLE order_monitor ADD COLUMN {ddl}")

        notify_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(notification_deliveries)").fetchall()
        }
        if "payload_text" not in notify_cols:
            conn.execute("ALTER TABLE notification_deliveries ADD COLUMN payload_text TEXT")
        if "claimed_at" not in notify_cols:
            conn.execute("ALTER TABLE notification_deliveries ADD COLUMN claimed_at TEXT")

        # 4. 记录迁移版本
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at)"
            " VALUES ('4.0.0_task5_multiticket', ?)",
            (_now_str(),),
        )
        logger.info("v4.0 多工单迁移已应用（单活动工单约束已移除）")

        # 5. v4.2：tickets.deadline 两列改可空（支持「待商榷」不设时效工单）。
        #    SQLite 无法直接 ALTER 去 NOT NULL，采用标准重建表迁移（幂等）。
        self._migrate_tickets_nullable_deadline(conn)

    def _migrate_tickets_nullable_deadline(self, conn: sqlite3.Connection) -> None:
        """把 tickets.initial_deadline_at / current_deadline_at 的 NOT NULL 约束去掉。

        幂等：仅当两列仍带 notnull 约束时重建一次表；数据原样保留。
        新库（按 _SCHEMA 建表）本身已可空，直接跳过。

        复制策略（2026-08-26 回归修复）：按旧表 PRAGMA 动态推导全量列清单，
        不再手工枚举——此前硬编码 29 列曾漏掉 completed_confirm_by/at，
        重建即把完工确认留痕静默清零且不可恢复。两道防线：

        - 迁移动手前先校验所有列名为纯标识符（防拼接面异常）；
        - 新表建成后与旧表做差集，发现未收录的列立刻抛错并原样保留
          ``tickets_old`` 供人工处置（绝不静默丢列）。
        """
        cols = {
            row["name"]: row["notnull"]
            for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        if cols.get("initial_deadline_at") != 1 or cols.get("current_deadline_at") != 1:
            return  # 已是可空（或列不存在），无需重建
        old_columns = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
        ]
        bad_identifiers = [
            c for c in old_columns
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c)
        ]
        if bad_identifiers:
            raise RuntimeError(
                f"[migrate] tickets 存在非法列名，拒绝拼入复制语句: {bad_identifiers}"
            )
        conn.executescript(
            """
            DROP INDEX IF EXISTS idx_tickets_group_status;
            DROP INDEX IF EXISTS idx_tickets_group;
            DROP INDEX IF EXISTS idx_tickets_status;
            ALTER TABLE tickets RENAME TO tickets_old;
            """
        )
        conn.executescript(_SCHEMA)
        new_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        uncovered = [c for c in old_columns if c not in new_columns]
        if uncovered:
            # 防线 2：新表未覆盖的旧列拒绝静默丢弃；完整旧数据保留在
            # tickets_old，供人工核对处置后再手动清理。
            raise RuntimeError(
                f"[migrate] tickets 新表结构缺少旧表列，已中止复制: {uncovered}"
                "（完整旧数据保留在 tickets_old）"
            )
        column_sql = ", ".join(old_columns)
        conn.execute(
            f"INSERT INTO tickets ({column_sql}) SELECT {column_sql} FROM tickets_old"
        )
        conn.execute("DROP TABLE tickets_old")
        logger.info(
            "tickets deadline 列已改为可空（待商榷工单支持）；列清单动态复制 count=%d",
            len(old_columns),
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.info("数据库连接关闭 path=%s", self.db_path)

    # ─────────────────────── 事务封装 ───────────────────────
    @contextmanager
    def transaction(self, tx_name: str = "tx") -> Iterator[sqlite3.Connection]:
        """事务上下文：基于 SAVEPOINT，可重入（嵌套调用安全）。

        提交成功打 INFO，回滚打 ERROR。最外层 SAVEPOINT 的回滚即整体回滚，
        嵌套事务内回滚只回滚到对应保存点。

        业务状态 + processed_events + 通知 Outbox 必须同事务写入。
        """
        conn = self.connect()
        # 清洗保存点名（tx_name 可能含 ':' 等 SQLite 标识符不允许的字符）
        savepoint = "sp_" + "".join(ch if ch.isalnum() else "_" for ch in tx_name)
        logger.debug("事务开始 tx=%s", tx_name)
        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            yield conn
        except Exception as exc:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error:
                pass
            logger.error("事务回滚 tx=%s reason=%s", tx_name, exc)
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            logger.info("事务提交 tx=%s", tx_name)

    # ─────────────────────── 基础查询 ───────────────────────
    def get_group(self, group_id: str) -> Optional[dict[str, Any]]:
        conn = self.connect()
        row = conn.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["manager_ids"] = json.loads(d.get("manager_ids") or "[]")
        d["engineer_ids"] = json.loads(d.get("engineer_ids") or "[]")
        d["other_member_ids"] = json.loads(d.get("other_member_ids") or "[]")
        return d

    def upsert_group(self, g: dict[str, Any]) -> None:
        with self.transaction("upsert_group"):
            self._conn.execute(
                """INSERT INTO groups (
                       group_id, store_name, manager_ids, engineer_ids,
                       other_member_ids, engineering_leader_id, regional_manager_id,
                       ticket_seq, is_active)
                   VALUES (?,?,?,?,?,?,?, COALESCE((SELECT ticket_seq FROM groups
                       WHERE group_id=?), 0), 1)
                   ON CONFLICT(group_id) DO UPDATE SET
                       store_name=excluded.store_name,
                       manager_ids=excluded.manager_ids,
                       engineer_ids=excluded.engineer_ids,
                       other_member_ids=excluded.other_member_ids,
                       engineering_leader_id=excluded.engineering_leader_id,
                       regional_manager_id=excluded.regional_manager_id,
                       is_active=1""",
                (
                    g["group_id"], g["store_name"],
                    json.dumps(g.get("manager_ids", []), ensure_ascii=False),
                    json.dumps(g.get("engineer_ids", []), ensure_ascii=False),
                    json.dumps(g.get("other_member_ids", []), ensure_ascii=False),
                    g.get("engineering_leader_id", ""),
                    g.get("regional_manager_id", ""),
                    g["group_id"],
                ),
            )
        logger.info(
            "群配置写入 group_id=%s store=%s managers=%d engineers=%d",
            g["group_id"], g["store_name"],
            len(g.get("manager_ids", [])), len(g.get("engineer_ids", [])),
        )

    # ─────────────────────── 幂等 ───────────────────────
    def message_already_seen(self, message_id: str) -> bool:
        conn = self.connect()
        row = conn.execute(
            "SELECT result FROM processed_events WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is not None:
            # 规范 3.2：幂等命中跳过（INFO）
            logger.info("幂等命中跳过 message_id=%s result=%s", message_id, row["result"])
            return True
        return False

    def record_processed_event(self, message_id: str, group_id: str, result: str) -> None:
        """必须在业务事务内调用；result: ARCHIVED/IGNORED/REJECTED 等。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_events (message_id, group_id, received_at, result)"
            " VALUES (?,?,?,?)",
            (message_id, group_id, _now_str(), result),
        )

    # ─────────────────────── 收件箱 inbox_messages ───────────────────────
    def enqueue_message(self, msg: Any) -> bool:
        """消息入箱（幂等：message_id 已存在返回 False）。

        同一事务写入图片附件元数据（真实字节由归档层补齐）。
        """
        with self.transaction("inbox_enqueue"):
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO inbox_messages (
                       message_id, group_id, sender_id, sender_role, content,
                       message_type, reply_to_message_id, sent_at, received_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    msg.message_id, msg.group_id, msg.sender_id, msg.sender_role,
                    msg.content, msg.message_type, msg.reply_to_message_id,
                    msg.sent_at.strftime("%Y-%m-%d %H:%M:%S"),
                    _now_str(),
                ),
            )
            if cur.rowcount > 0:
                self._insert_attachment_metadata(
                    self._conn, msg.message_id, getattr(msg, "attachments", ())
                )
        return cur.rowcount > 0

    # ─────────────────────── 图片附件 message_attachments（v4.1 Task 4A） ───────────────────────
    def _insert_attachment_metadata(
        self, conn: sqlite3.Connection, message_id: str, attachments: Any, *, now: str | None = None
    ) -> None:
        """附件元数据幂等写入（同 message_id+index 已存在跳过）。"""
        now = now or _now_str()
        for att in attachments:
            conn.execute(
                """INSERT OR IGNORE INTO message_attachments
                       (message_id, attachment_index, source_type, source_ref,
                        file_name, declared_mime_type, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    message_id, att.attachment_index, att.source_type, att.source_ref,
                    att.file_name, att.declared_mime_type, now,
                ),
            )

    def list_attachment_rows(self, message_id: str) -> list[dict[str, Any]]:
        """某条消息的全部附件记录（含未归档/失败的），供归档与分析层消费。"""
        rows = self.connect().execute(
            "SELECT * FROM message_attachments WHERE message_id=? ORDER BY attachment_index",
            (message_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_attachment_archived(
        self, attachment_id: int, *, stored_path: str, sha256: str, byte_size: int, mime_type: str
    ) -> None:
        """归档成功：回填存储字段（error 清零）。"""
        with self.transaction("attachment_archived"):
            self._conn.execute(
                """UPDATE message_attachments
                   SET stored_path=?, sha256=?, byte_size=?, mime_type=?, error=NULL
                   WHERE id=?""",
                (stored_path, sha256, byte_size, mime_type, attachment_id),
            )

    def mark_attachment_failed(self, attachment_id: int, error: str) -> None:
        """归档失败：置 SKIPPED 并记录原因（不删除 source_ref，可人工/脚本重试）。"""
        with self.transaction("attachment_failed"):
            self._conn.execute(
                "UPDATE message_attachments SET error=?, analyzed_status='SKIPPED' WHERE id=?",
                (error, attachment_id),
            )

    def update_attachment_vision(
        self, attachment_id: int, *, result: str, status: str = "ANALYZED"
    ) -> None:
        """多模态解析成功：写入结果并标记已解析。"""
        from datetime import datetime

        with self.transaction("attachment_vision"):
            self._conn.execute(
                """UPDATE message_attachments
                   SET vision_result_json=?, analyzed_status=?, analyzed_at=?, error=NULL
                   WHERE id=?""",
                (result, status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), attachment_id),
            )

    def mark_attachment_analyze_error(self, attachment_id: int, error: str) -> None:
        """解析失败：记录错误，保留状态供重试。"""
        with self.transaction("attachment_analyze_error"):
            self._conn.execute(
                "UPDATE message_attachments SET error=?, analyzed_status='FAILED' WHERE id=?",
                (error, attachment_id),
            )

    def backfill_attachment_ticket(self, message_id: str, ticket_id: int) -> None:
        """消息归属工单后回填附件 ticket_id（link_message 内已自动调用，此为显式入口）。"""
        with self.transaction("attachment_backfill"):
            self._conn.execute(
                "UPDATE message_attachments SET ticket_id=? WHERE message_id=? AND ticket_id IS NULL",
                (ticket_id, message_id),
            )

    def list_ticket_attachments(self, ticket_id: int, *, only_archived: bool = True) -> list[dict[str, Any]]:
        """某工单的全部附件（工单结束后统一分析层使用）。"""
        sql = "SELECT * FROM message_attachments WHERE ticket_id=?"
        params: list[Any] = [ticket_id]
        if only_archived:
            sql += " AND stored_path IS NOT NULL AND analyzed_status != 'SKIPPED'"
        sql += " ORDER BY id"
        rows = self.connect().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_inbox_message(self, message_id: str) -> Optional[dict[str, Any]]:
        """按消息 ID 查收件箱记录（含 group_id 等），供附件下载等场景使用。"""
        row = self.connect().execute(
            "SELECT * FROM inbox_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def record_system_reply(
        self, group_id: str, message_id: str, text: str, *, sent_at: str | None = None
    ) -> None:
        """记录系统回执消息到收件箱（sender_role=SYSTEM，状态直接 COMPLETED）。

        目的：作为群聊上文供模型理解（用户回复「2」「报修」常是对系统
        刚才给出的选项/澄清的回答）；SYSTEM 消息不会被业务 Worker 再次处理。
        """
        from models import ROLE_SYSTEM

        sent_at = sent_at or _now_str()
        with self.transaction("record_system_reply"):
            self._conn.execute(
                """INSERT OR IGNORE INTO inbox_messages (
                       message_id, group_id, sender_id, sender_role, content,
                       message_type, reply_to_message_id, sent_at, received_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    message_id, group_id, "", ROLE_SYSTEM, text,
                    "text", None, sent_at, _now_str(), "COMPLETED",
                ),
            )

    def inbox_next_due(self, limit: int = 20, *, now: str | None = None) -> list[dict[str, Any]]:
        """取可处理消息（RECEIVED 或已到期的 RETRY_PENDING），按 (group, sent_at, message_id) 排序。"""
        now = now or _now_str()
        rows = self.connect().execute(
            """SELECT * FROM inbox_messages
               WHERE status IN ('RECEIVED', 'RETRY_PENDING')
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY group_id, sent_at, message_id LIMIT ?""",
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def inbox_next_due_for_group(
        self, group_id: str, limit: int = 20, *, now: str | None = None
    ) -> list[dict[str, Any]]:
        """取某群可处理消息（RECEIVED 或已到期 RETRY_PENDING），群内按顺序。"""
        now = now or _now_str()
        rows = self.connect().execute(
            """SELECT * FROM inbox_messages
               WHERE group_id=? AND status IN ('RECEIVED', 'RETRY_PENDING')
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY sent_at, message_id LIMIT ?""",
            (group_id, now, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_group_ids(self) -> list[str]:
        """所有已配置群 ID（供 Worker 按群并行）。"""
        rows = self.connect().execute(
            "SELECT group_id FROM groups ORDER BY group_id"
        ).fetchall()
        return [r["group_id"] for r in rows]

    def list_recent_group_messages(
        self, group_id: str, limit: int = 8, exclude_message_id: str | None = None
    ) -> list[dict[str, Any]]:
        """取某群最近 N 条消息（含系统回执），时间正序，供模型理解上下文。

        从收件箱取（含忽略/澄清消息），排除当前这条；按 (sent_at, message_id) 取最近 N 条。
        """
        exclude = "AND message_id != ?" if exclude_message_id else ""
        params: list[Any] = [group_id]
        if exclude_message_id:
            params.append(exclude_message_id)
        rows = self.connect().execute(
            f"""SELECT message_id, sender_id, sender_role,
                       CASE WHEN sender_role='SYSTEM' THEN '系统'
                            ELSE sender_id END AS sender_name,
                       content, sent_at
                FROM inbox_messages
                WHERE group_id=? {exclude}
                ORDER BY sent_at DESC, message_id DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def inbox_set_status(
        self,
        message_id: str,
        status: str,
        *,
        processed_result: str | None = None,
        last_error: str | None = None,
        attempts: int | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        """更新收件箱行状态（不单独开事务，调用方在业务事务内调用）。"""
        sets: list[str] = ["status=?"]
        params: list[Any] = [status]
        if processed_result is not None:
            sets.append("processed_result=?")
            params.append(processed_result)
        if last_error is not None:
            sets.append("last_error=?")
            params.append(last_error)
        if attempts is not None:
            sets.append("attempts=?")
            params.append(attempts)
        if next_attempt_at is not None:
            sets.append("next_attempt_at=?")
            params.append(next_attempt_at)
        params.append(message_id)
        self._conn.execute(
            f"UPDATE inbox_messages SET {', '.join(sets)} WHERE message_id=?", params
        )

    def inbox_reset_stale(self, *, now: str | None = None) -> int:
        """启动时把残留 PROCESSING 重置回 RECEIVED（单进程崩溃恢复）。"""
        with self.transaction("inbox_reset_stale"):
            cur = self._conn.execute(
                "UPDATE inbox_messages SET status='RECEIVED', claimed_by=NULL,"
                " claimed_at=NULL, lease_until=NULL WHERE status='PROCESSING'"
            )
        return cur.rowcount

    # ─────────────────────── 语义决策 semantic_decisions ───────────────────────
    def save_semantic_decision(
        self,
        message_id: str,
        *,
        protocol_version: str,
        source: str,
        intent: str,
        target_ticket_no: str | None,
        confidence: float,
        fields: dict[str, Any],
        missing_fields: tuple[str, ...],
        evidence: tuple[str, ...],
        decision_status: str = "RECORDED",
        errors: tuple[str, ...] = (),
    ) -> int:
        """保存一条识别结果审计记录，返回 id。"""
        with self.transaction("save_semantic_decision"):
            cur = self._conn.execute(
                """INSERT INTO semantic_decisions (
                       message_id, protocol_version, source, intent, target_ticket_no,
                       confidence, fields_json, missing_fields_json, evidence_json,
                       decision_status, errors_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    message_id, protocol_version, source, intent, target_ticket_no,
                    confidence,
                    json.dumps(fields, ensure_ascii=False),
                    json.dumps(list(missing_fields), ensure_ascii=False),
                    json.dumps(list(evidence), ensure_ascii=False),
                    decision_status,
                    json.dumps(list(errors), ensure_ascii=False),
                    _now_str(),
                ),
            )
        return cur.lastrowid

    # ─────────────────────── 消息归属 message_ticket_links ───────────────────────
    def link_message(
        self, message_id: str, ticket_id: int, link_type: str, routing_score: float = 0.0
    ) -> None:
        """记录消息最终归属（在业务事务内调用）。

        同时回填 message_attachments 的 ticket_id（图片可能先于建单出现）。
        """
        self._conn.execute(
            """INSERT OR REPLACE INTO message_ticket_links
                   (message_id, ticket_id, link_type, routing_score, linked_at)
               VALUES (?,?,?,?,?)""",
            (message_id, ticket_id, link_type, routing_score, _now_str()),
        )
        self._conn.execute(
            "UPDATE message_attachments SET ticket_id=? WHERE message_id=? AND ticket_id IS NULL",
            (ticket_id, message_id),
        )

    def get_message_link(self, message_id: str) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM message_ticket_links WHERE message_id=?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_quoted_ticket_id(self, reply_to_message_id: str) -> int | None:
        """钉钉引用：查被引用消息的归属工单。"""
        row = self.connect().execute(
            "SELECT ticket_id FROM message_ticket_links WHERE message_id=?",
            (reply_to_message_id,),
        ).fetchone()
        return row["ticket_id"] if row else None

    # ─────────────────────── 用户上下文 ticket_contexts ───────────────────────
    def set_ticket_context(
        self, group_id: str, user_id: str, ticket_id: int, order_key: str,
        expires_at: str, *, now: str | None = None,
    ) -> None:
        with self.transaction("set_ticket_context"):
            self._conn.execute(
                """INSERT INTO ticket_contexts (group_id, user_id, ticket_id, order_key, expires_at, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(group_id, user_id) DO UPDATE SET
                       ticket_id=excluded.ticket_id, order_key=excluded.order_key,
                       expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
                (group_id, user_id, ticket_id, order_key, expires_at, now or _now_str()),
            )

    def get_ticket_context(
        self, group_id: str, user_id: str, now: str
    ) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM ticket_contexts WHERE group_id=? AND user_id=? AND expires_at > ?",
            (group_id, user_id, now),
        ).fetchone()
        return dict(row) if row else None

    def clear_ticket_context(self, group_id: str, user_id: str) -> None:
        with self.transaction("clear_ticket_context"):
            self._conn.execute(
                "DELETE FROM ticket_contexts WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )

    def clear_contexts_by_ticket(self, ticket_id: int) -> int:
        with self.transaction("clear_contexts_by_ticket"):
            cur = self._conn.execute(
                "DELETE FROM ticket_contexts WHERE ticket_id=?", (ticket_id,)
            )
        return cur.rowcount

    # ─────────────────────── 待确认动作 pending_actions ───────────────────────
    def create_pending(
        self,
        *,
        source_message_id: str,
        group_id: str,
        user_id: str,
        intent: str,
        candidate_ticket_ids: tuple[int, ...],
        fields: dict[str, Any],
        expected_versions: dict[int, int],
        expires_at: str,
    ) -> int:
        with self.transaction("create_pending"):
            cur = self._conn.execute(
                """INSERT INTO pending_actions (
                       source_message_id, group_id, user_id, intent,
                       candidate_ticket_ids_json, fields_json, expected_versions_json,
                       status, version, created_at, expires_at)
                   VALUES (?,?,?,?,?,?,?, 'WAITING', 0, ?, ?)""",
                (
                    source_message_id, group_id, user_id, intent,
                    json.dumps(list(candidate_ticket_ids), ensure_ascii=False),
                    json.dumps(fields, ensure_ascii=False),
                    json.dumps(expected_versions, ensure_ascii=False),
                    _now_str(), expires_at,
                ),
            )
            return cur.lastrowid

    def _pending_row_to_dict(self, row: Any) -> dict[str, Any]:
        d = dict(row)
        d["candidate_ticket_ids"] = tuple(
            json.loads(d.get("candidate_ticket_ids_json") or "[]")
        )
        d["fields"] = json.loads(d.get("fields_json") or "{}")
        d["expected_versions"] = {
            int(k): int(v) for k, v in (json.loads(d.get("expected_versions_json") or "{}")).items()
        }
        return d

    def get_waiting_pending(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM pending_actions WHERE group_id=? AND user_id=? AND status='WAITING'",
            (group_id, user_id),
        ).fetchone()
        return self._pending_row_to_dict(row) if row else None

    def get_pending(self, pending_id: int) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM pending_actions WHERE id=?", (pending_id,)
        ).fetchone()
        return self._pending_row_to_dict(row) if row else None

    def supersede_waiting(self, group_id: str, user_id: str, *, now: str | None = None) -> int:
        """把该 (group,user) 的旧 WAITING 置为 SUPERSEDED，返回受影响行数。"""
        with self.transaction("supersede_waiting"):
            cur = self._conn.execute(
                "UPDATE pending_actions SET status='SUPERSEDED', resolved_at=?"
                " WHERE group_id=? AND user_id=? AND status='WAITING'",
                (now or _now_str(), group_id, user_id),
            )
        return cur.rowcount

    def resolve_pending(
        self, pending_id: int, expected_version: int, status: str,
        confirmed_message_id: str | None = None, *, now: str | None = None,
    ) -> bool:
        """CAS 解决待确认动作；行数为 0 表示版本冲突或已非 WAITING。"""
        with self.transaction("resolve_pending"):
            cur = self._conn.execute(
                "UPDATE pending_actions SET status=?, version=version+1,"
                " confirmed_message_id=?, resolved_at=?"
                " WHERE id=? AND status='WAITING' AND version=?",
                (status, confirmed_message_id, now or _now_str(), pending_id, expected_version),
            )
        return cur.rowcount > 0

    def expire_due_pendings(self, now: str) -> int:
        with self.transaction("expire_due_pendings"):
            cur = self._conn.execute(
                "UPDATE pending_actions SET status='EXPIRED', resolved_at=?"
                " WHERE status='WAITING' AND expires_at <= ?",
                (now, now),
            )
        return cur.rowcount

    # ─────────────────────── 执行记录 action_executions ───────────────────────
    def insert_execution(
        self,
        *,
        dedupe_key: str,
        source_message_id: str,
        confirmation_message_id: str | None,
        pending_action_id: int | None,
        intent: str,
        target_ticket_id: int | None,
        command_json: dict[str, Any],
    ) -> bool:
        """INSERT OR IGNORE；返回 False 表示该 dedupe_key 已存在（防重复执行）。"""
        with self.transaction("insert_execution"):
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO action_executions (
                       dedupe_key, source_message_id, confirmation_message_id,
                       pending_action_id, intent, target_ticket_id, command_json,
                       status, created_at)
                   VALUES (?,?,?,?,?,?,?, 'PENDING', ?)""",
                (
                    dedupe_key, source_message_id, confirmation_message_id,
                    pending_action_id, intent, target_ticket_id,
                    json.dumps(command_json, ensure_ascii=False),
                    _now_str(),
                ),
            )
        return cur.rowcount > 0

    def mark_execution_applied(self, dedupe_key: str, *, now: str | None = None) -> None:
        self._conn.execute(
            "UPDATE action_executions SET status='APPLIED', applied_at=?"
            " WHERE dedupe_key=?",
            (now or _now_str(), dedupe_key),
        )

    def execution_applied(self, dedupe_key: str) -> bool:
        row = self.connect().execute(
            "SELECT status FROM action_executions WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        return row is not None and row["status"] == "APPLIED"

    def execution_status(self, dedupe_key: str) -> str | None:
        row = self.connect().execute(
            "SELECT status FROM action_executions WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        return row["status"] if row else None

    def mark_execution_result(self, dedupe_key: str, status: str) -> None:
        self._conn.execute(
            "UPDATE action_executions SET status=? WHERE dedupe_key=?",
            (status, dedupe_key),
        )

    # ─────────────────────── 工单基础读写（供 tickets/repository 使用） ───────────────────────
    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_ticket_by_no(self, ticket_no: str) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        return dict(row) if row else None

    def list_active_tickets(self, group_id: str) -> list[dict[str, Any]]:
        rows = self.connect().execute(
            "SELECT * FROM tickets WHERE group_id=? AND status IN ('ACTIVE','ACTIVE_OVERDUE')"
            " ORDER BY id",
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_tickets_by_statuses(
        self, group_id: str, statuses: tuple[str, ...] | list[str]
    ) -> list[dict[str, Any]]:
        """按状态集合取群内工单（路由候选用）。

        2026-09-14：路由候选此前只来自 :meth:`list_active_tickets`（ACTIVE/
        ACTIVE_OVERDUE），协议已放行的在途状态（待商榷/待店长确认）因此永远
        拿不到候选 —— 北京商场14大悦城 …-001 待商榷单报完工被误回「当前没有
        可操作的活动工单」。此处提供按状态取候选的能力，放开哪些状态由调用方
        （pipeline）按协议 allowed_ticket_states 决定。
        """
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self.connect().execute(
            f"SELECT * FROM tickets WHERE group_id=? AND status IN ({placeholders})"
            " ORDER BY id",
            (group_id, *statuses),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_group_tickets(self, group_id: str) -> list[dict[str, Any]]:
        """群内全部工单（含终态，供 #重开工单 等定位终态工单）。"""
        rows = self.connect().execute(
            "SELECT * FROM tickets WHERE group_id=? ORDER BY id",
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_group_pending_confirm_tickets(self, group_id: str) -> list[dict[str, Any]]:
        """群内「待店长确认」工单，最早创建优先（完工确认窗口归档用）。"""
        rows = self.connect().execute(
            "SELECT * FROM tickets WHERE group_id=? AND status='PENDING_CONFIRM'"
            " ORDER BY id",
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def next_ticket_seq(self, group_id: str) -> int:
        """分配工单序号（必须在事务内调用，配合 upsert 递增）。"""
        row = self._conn.execute(
            "SELECT ticket_seq FROM groups WHERE group_id=?", (group_id,)
        ).fetchone()
        seq = int(row["ticket_seq"]) if row else 0
        new_seq = seq + 1
        self._conn.execute(
            "UPDATE groups SET ticket_seq=? WHERE group_id=?", (new_seq, group_id)
        )
        return new_seq

    def insert_ticket(self, row: dict[str, Any]) -> int:
        cur = self._conn.execute(
            """INSERT INTO tickets (
                   ticket_no, group_id, store_name, reporter_id, subject, location,
                   problem_description, sla_days, initial_deadline_at, current_deadline_at,
                   status, version, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["ticket_no"], row["group_id"], row["store_name"], row["reporter_id"],
                row["subject"], row["location"], row["problem_description"],
                row["sla_days"], row["initial_deadline_at"], row["current_deadline_at"],
                row.get("status", "ACTIVE"), 1, _now_str(),
            ),
        )
        return cur.lastrowid

    def update_ticket_cas(
        self, ticket_id: int, expected_version: int, set_clause: str, params: tuple[Any, ...]
    ) -> bool:
        """乐观版本条件更新；set_clause 形如 'status=?, closed_at=?'，params 含该子句参数。
        返回 False 表示版本冲突。"""
        cur = self._conn.execute(
            f"UPDATE tickets SET {set_clause}, version=version+1"
            " WHERE id=? AND version=?",
            (*params, ticket_id, expected_version),
        )
        return cur.rowcount > 0

    def mark_ticket_overdue(self, ticket_id: int) -> bool:
        """把已超时的活动工单标记为 ACTIVE_OVERDUE（计划书 §9.1，幂等）。

        由调度器在 SLA 扫描发现超时时调用，将状态从 ACTIVE 推进到
        ACTIVE_OVERDUE（等待原因或完成条件）。已处于 ACTIVE_OVERDUE
        或已终态（COMPLETED/CANCELLED）的工单不重复切换。
        不推进 version——状态是调度器语义标记，不是业务版本变更。
        """
        cur = self._conn.execute(
            "UPDATE tickets SET status=? WHERE id=? AND status=?",
            (TICKET_OVERDUE, ticket_id, TICKET_ACTIVE),
        )
        return cur.rowcount > 0

    # ─────────────────────── 责任方切换（计划书 §9.3） ───────────────────────
    def switch_responsibility(
        self, ticket_id: int, sender_role: str, message_id: str, sent_at: str
    ) -> bool:
        """按消息发送方角色切换工单等待责任方（计划书 §9.3）。

        规则：店长 → 等待工程师方；工程师/工程负责人 → 等待店长方；其他角色不切换。
        切换时关闭当前未决责任周期并开启新周期（due_at = sent_at + 4h）。
        返回是否发生了切换。
        """
        next_side = {
            ROLE_MANAGER: WAITING_ENGINEER_SIDE,
            ROLE_ENGINEER: WAITING_MANAGER_SIDE,
            ROLE_LEADER: WAITING_MANAGER_SIDE,
        }.get(sender_role)
        if next_side is None:
            return False
        ticket = self._conn.execute(
            "SELECT waiting_side FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if ticket is None or ticket["waiting_side"] == next_side:
            return False
        self._close_pending_cycles(ticket_id, message_id)
        due_at = self._add_hours(sent_at, SIDE_REPLY_TIMEOUT_HOURS)
        cur = self._conn.execute(
            """INSERT INTO responsibility_cycles
                   (ticket_id, waiting_side, trigger_message_id, waiting_since, due_at, status)
               VALUES (?,?,?,?,?, 'PENDING')""",
            (ticket_id, next_side, message_id, sent_at, due_at),
        )
        self._conn.execute(
            "UPDATE tickets SET waiting_side=?, waiting_since=?, current_responsibility_cycle_id=? WHERE id=?",
            (next_side, sent_at, cur.lastrowid, ticket_id),
        )
        return True

    def close_responsibility_cycles(self, ticket_id: int, closed_by_message_id: str) -> int:
        """关闭工单所有未决（PENDING）责任周期（完成/取消/重开终态转换时）。"""
        cur = self._conn.execute(
            "UPDATE responsibility_cycles SET status=?, closed_by_message_id=? "
            " WHERE ticket_id=? AND status='PENDING'",
            (CYCLE_CANCELLED, closed_by_message_id, ticket_id),
        )
        return cur.rowcount

    def _close_pending_cycles(self, ticket_id: int, closed_by_message_id: str) -> int:
        return self._conn.execute(
            "UPDATE responsibility_cycles SET status=?, closed_by_message_id=? "
            " WHERE ticket_id=? AND status='PENDING'",
            (CYCLE_CANCELLED, closed_by_message_id, ticket_id),
        ).rowcount

    def _add_hours(self, ts: str, hours: int) -> str:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        from datetime import timedelta

        return (dt + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    def add_ticket_message(
        self, message_id: str, ticket_id: int, sender_id: str, sender_role: str,
        content: str, message_type: str, sent_at: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO messages (message_id, ticket_id, sender_id, sender_role,
                   content, message_type, sent_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(message_id) DO NOTHING""",
            (message_id, ticket_id, sender_id, sender_role, content, message_type, sent_at),
        )

    def add_diagnosis_version(
        self, ticket_id: int, message_id: str, items: list[str], engineer_id: str
    ) -> None:
        self._conn.execute(
            "UPDATE diagnosis_versions SET is_current=0 WHERE ticket_id=? AND is_current=1",
            (ticket_id,),
        )
        self._conn.execute(
            """INSERT INTO diagnosis_versions
                   (ticket_id, source_message_id, items_json, engineer_id, submitted_at, is_current)
               VALUES (?,?,?,?,?,1)""",
            (ticket_id, message_id, json.dumps(items, ensure_ascii=False), engineer_id, _now_str()),
        )

    def add_repair_method_version(
        self, ticket_id: int, message_id: str, repair_method: str,
        order_no: str | None, engineer_id: str,
    ) -> None:
        self._conn.execute(
            "UPDATE repair_method_versions SET is_current=0 WHERE ticket_id=? AND is_current=1",
            (ticket_id,),
        )
        self._conn.execute(
            """INSERT INTO repair_method_versions
                   (ticket_id, source_message_id, repair_method, order_no, engineer_id, submitted_at, is_current)
               VALUES (?,?,?,?,?,?,1)""",
            (ticket_id, message_id, repair_method, order_no, engineer_id, _now_str()),
        )

    def add_part_version(
        self, ticket_id: int, message_id: str, parts: list[str],
        raw_text: str | None, engineer_id: str,
    ) -> None:
        """记录本次维修使用的备件（仅记录，不动库存）。

        parts 必须已是规范名（canonical）列表；parts 列存顿号连接的规范名串。
        同一 message_id 只允许一条（source_message_id UNIQUE）；
        同一工单最新一条 is_current=1。
        """
        self._conn.execute(
            "UPDATE part_versions SET is_current=0 WHERE ticket_id=? AND is_current=1",
            (ticket_id,),
        )
        self._conn.execute(
            """INSERT INTO part_versions
                   (ticket_id, source_message_id, parts, raw_text, engineer_id, submitted_at, is_current)
               VALUES (?,?,?,?,?,?,1)""",
            (ticket_id, message_id, "、".join(parts) if parts else "",
             raw_text, engineer_id, _now_str()),
        )

    @staticmethod
    def _split_parts(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(p) for p in value if str(p).strip()]
        return [p.strip() for p in str(value).split("、") if p.strip()]

    def get_current_part_version(self, ticket_id: int) -> dict | None:
        """返回工单最新的备件记录（is_current=1），无则 None；parts 为列表。"""
        row = self._conn.execute(
            "SELECT * FROM part_versions WHERE ticket_id=? AND is_current=1 ORDER BY id DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["parts"] = self._split_parts(d.get("parts"))
        return d

    def list_part_versions(self, ticket_id: int) -> list[dict]:
        """按提交时间正序返回工单全部备件记录；parts 为列表。"""
        rows = self._conn.execute(
            "SELECT * FROM part_versions WHERE ticket_id=? ORDER BY id",
            (ticket_id,),
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["parts"] = self._split_parts(d.get("parts"))
            out.append(d)
        return out

    def open_timeout_cycle(self, ticket_id: int, reminded_at: str) -> int | None:
        """为已超时工单建立/确认一个 WAITING_REASON 超时周期（幂等）。

        计划书 §9.5：到期未完成时建立 `WAITING_REASON` 超时周期，等待工程师解释。
        同一工单同时最多一个未解释周期（idx_timeout_one_waiting 唯一索引保证）。
        周期建立时记录旧截止时间（old_deadline_at）与提醒时间（reminded_at），
        并回填 tickets.current_timeout_cycle_id。返回周期 id；工单不存在返回 None。
        """
        row = self._conn.execute(
            "SELECT id FROM timeout_cycles"
            " WHERE ticket_id=? AND status='WAITING_REASON' ORDER BY id LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if row is not None:
            return row["id"]
        cur = self._conn.execute(
            """INSERT INTO timeout_cycles
                   (ticket_id, cycle_no, status, old_deadline_at, reminded_at)
               SELECT id,
                      COALESCE((SELECT MAX(cycle_no) FROM timeout_cycles WHERE ticket_id=t.id), 0) + 1,
                      'WAITING_REASON', current_deadline_at, ?
               FROM tickets t WHERE id=?""",
            (reminded_at, ticket_id),
        )
        if cur.rowcount == 0:
            return None
        self._conn.execute(
            "UPDATE tickets SET current_timeout_cycle_id=? WHERE id=?",
            (cur.lastrowid, ticket_id),
        )
        return cur.lastrowid

    # ─────────────────────── 特殊情况暂停（2026-08-26） ───────────────────────
    def add_special_case(
        self,
        ticket_id: int,
        message_id: str,
        reason: str,
        expected_resume_text: str | None,
        expected_resume_at: str | None,
        submitted_by: str,
        submitted_at: str,
    ) -> int:
        """登记一条特殊情况暂停（调用方须先关闭既有生效中的暂停，保证唯一索引）。

        同时快照暂停开始时的截止时间 paused_deadline_at，供审计。
        """
        row = self._conn.execute(
            "SELECT current_deadline_at FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        cur = self._conn.execute(
            """INSERT INTO ticket_special_cases
                   (ticket_id, source_message_id, reason, expected_resume_text,
                    expected_resume_at, paused_deadline_at, submitted_by, submitted_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticket_id, message_id, reason, expected_resume_text,
             expected_resume_at, row["current_deadline_at"] if row else None,
             submitted_by, submitted_at),
        )
        return cur.lastrowid

    def close_active_special_case(
        self, ticket_id: int, resume_message_id: str, resumed_at: str
    ) -> Optional[sqlite3.Row]:
        """关闭生效中的暂停，返回被关闭的记录（含 submitted_at 供顺延计算）；无则 None。"""
        row = self._conn.execute(
            "SELECT * FROM ticket_special_cases"
            " WHERE ticket_id=? AND resumed_at IS NULL ORDER BY id LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE ticket_special_cases SET resumed_at=?, resume_message_id=?"
            " WHERE id=? AND resumed_at IS NULL",
            (resumed_at, resume_message_id, row["id"]),
        )
        # 真·停表（2026-08-27）：把本次暂停时长从等待起点中剔除——
        # 只顺延早于暂停开始的等待周期（业务动作恢复时重置的新周期不动）；
        # 停表不清零：暂停前已累计的等待原样保留。
        try:
            started = datetime.strptime(row["submitted_at"], "%Y-%m-%d %H:%M:%S")
            ended = datetime.strptime(resumed_at, "%Y-%m-%d %H:%M:%S")
            delta_seconds = int((ended - started).total_seconds())
        except (TypeError, ValueError):
            delta_seconds = 0
        ticket_row = self._conn.execute(
            "SELECT waiting_since FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if (
            delta_seconds > 0
            and ticket_row is not None
            and ticket_row["waiting_since"]
            and ticket_row["waiting_since"] <= row["submitted_at"]
        ):
            new_since = (
                datetime.strptime(ticket_row["waiting_since"], "%Y-%m-%d %H:%M:%S")
                + timedelta(seconds=delta_seconds)
            ).strftime("%Y-%m-%d %H:%M:%S")
            self._conn.execute(
                "UPDATE tickets SET waiting_since=? WHERE id=? AND waiting_since=?",
                (new_since, ticket_id, ticket_row["waiting_since"]),
            )
        return row

    def set_ticket_deadline(self, ticket_id: int, new_deadline_at: str) -> bool:
        """直接写截止时间（暂停顺延用，已在业务事务内，不做 CAS）。"""
        cur = self._conn.execute(
            "UPDATE tickets SET current_deadline_at=? WHERE id=?",
            (new_deadline_at, ticket_id),
        )
        return cur.rowcount > 0

    def add_timeout_cycle_reason(
        self, ticket_id: int, message_id: str, timeout_reason: str, engineer_id: str
    ) -> bool:
        """解释当前未解释的超时周期：WAITING_REASON → EXTENDED（计划书 §4.8）。

        仅当工单存在尚未解释（WAITING_REASON）的周期时接受原因；
        同一超时周期只接受一次有效原因（status 转 EXTENDED 后不再接受）。
        无未解释周期返回 False（调用方应拒绝该消息）。
        记录原因但不自动延期（v4.1 决策，new_deadline_at 留空）。
        """
        row = self._conn.execute(
            "SELECT id FROM timeout_cycles"
            " WHERE ticket_id=? AND status='WAITING_REASON' ORDER BY id LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if row is None:
            return False
        self._conn.execute(
            """UPDATE timeout_cycles
                   SET status='EXTENDED', reason=?, reason_engineer_id=?, reason_submitted_at=?
               WHERE id=? AND status='WAITING_REASON'""",
            (timeout_reason, engineer_id, _now_str(), row["id"]),
        )
        return True

    # ─────────────────────── 通知 Outbox notification_deliveries ───────────────────────
    def insert_notification(
        self,
        *,
        dedupe_key: str,
        ticket_id: int | None,
        notification_type: str,
        target_type: str,
        target_id: str,
        scheduled_at: str | None = None,
        payload_text: str | None = None,
    ) -> int:
        """在业务事务内预写通知（PENDING）。"""
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO notification_deliveries
                   (dedupe_key, ticket_id, notification_type, target_type, target_id,
                    scheduled_at, payload_text, status)
               VALUES (?,?,?,?,?,?,?, 'PENDING')""",
            (dedupe_key, ticket_id, notification_type, target_type, target_id,
             scheduled_at or _now_str(), payload_text),
        )
        return cur.lastrowid if cur.rowcount > 0 else 0

    def claim_pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        now = _now_str()
        stale_before = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        with self.transaction("claim_notifications"):
            # 崩溃遗留的领取记录超过 5 分钟后可再次投递。
            self._conn.execute(
                "UPDATE notification_deliveries SET status='PENDING', claimed_at=NULL "
                "WHERE status='CLAIMED' AND claimed_at < ?",
                (stale_before,),
            )
            rows = self._conn.execute(
                """UPDATE notification_deliveries
                   SET status='CLAIMED', claimed_at=?
                   WHERE id IN (
                       SELECT id FROM notification_deliveries
                       WHERE status='PENDING'
                       ORDER BY scheduled_at LIMIT ?
                   )
                   RETURNING *""",
                (now, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_ticket_sla_dedupe(self, ticket_id: int) -> int:
        """清理某工单的 SLA 提醒去重键，使重开后的工单可再次触发提醒与超时周期。

        返回受影响行数。只清理 sla_remind/sla_overdue 前缀的键，不影响其他通知。
        """
        cur = self._conn.execute(
            "DELETE FROM notification_deliveries"
            " WHERE dedupe_key=? OR dedupe_key LIKE ?",
            (f"sla_overdue:{ticket_id}", f"sla_remind:{ticket_id}:%"),
        )
        return cur.rowcount

    def mark_notification(
        self, notification_id: int, status: str, *, error: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE notification_deliveries SET status=?, claimed_at=NULL, sent_at=?, last_error=?"
            " WHERE id=?",
            (status, _now_str() if status != "PENDING" else None, error, notification_id),
        )

    # ─────────────────────── 快递签收确认 delivery_confirmations ───────────────────────
    def create_delivery_confirmation(
        self, ticket_id: int, order_no: str, group_id: str, confirm_user_id: str
    ) -> int:
        """创建待确认快递记录（同单同单号 WAITING 已存在则返回 0）。"""
        with self.transaction("create_delivery_confirmation"):
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO delivery_confirmations
                       (ticket_id, order_no, group_id, confirm_user_id, status, created_at)
                   VALUES (?,?,?,?, 'WAITING', ?)""",
                (ticket_id, order_no, group_id, confirm_user_id, _now_str()),
            )
        return cur.lastrowid if cur.rowcount > 0 else 0

    def get_waiting_delivery_confirmation(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.connect().execute(
            """SELECT * FROM delivery_confirmations
               WHERE group_id=? AND confirm_user_id=? AND status='WAITING'
               ORDER BY id DESC LIMIT 1""",
            (group_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def resolve_delivery_confirmation(
        self, confirmation_id: int, status: str, message_id: str
    ) -> bool:
        with self.transaction("resolve_delivery_confirmation"):
            cur = self._conn.execute(
                "UPDATE delivery_confirmations SET status=?, resolved_at=?, resolve_message_id=?"
                " WHERE id=? AND status='WAITING'",
                (status, _now_str(), message_id, confirmation_id),
            )
        return cur.rowcount > 0

    def expire_deliveries_by_ticket(self, ticket_id: int) -> int:
        with self.transaction("expire_deliveries_by_ticket"):
            cur = self._conn.execute(
                "UPDATE delivery_confirmations SET status='EXPIRED', resolved_at=?"
                " WHERE ticket_id=? AND status='WAITING'",
                (_now_str(), ticket_id),
            )
        return cur.rowcount

    # ─────────────────────── 淘宝对账 taobao_orders ───────────────────────
    def upsert_taobao_order(
        self,
        *,
        order_id: str,
        product_summary: str,
        tracking_number: str | None,
        address: str | None,
        status: str | None,
        source: str,
    ) -> None:
        with self.transaction("upsert_taobao_order"):
            self._conn.execute(
                """INSERT INTO taobao_orders
                       (order_id, product_summary, tracking_number, address, status, source, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(order_id) DO UPDATE SET
                       product_summary=excluded.product_summary,
                       tracking_number=excluded.tracking_number,
                       address=excluded.address,
                       status=excluded.status,
                       source=excluded.source,
                       updated_at=excluded.updated_at""",
                (order_id, product_summary, tracking_number, address, status, source, _now_str()),
            )

    def get_taobao_order(self, order_no: str) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM taobao_orders WHERE order_id=?", (order_no,)
        ).fetchone()
        return dict(row) if row else None

    # ─────────────────────── 订单监控 order_monitor ───────────────────────
    def upsert_order_monitor(
        self,
        *,
        order_id: str,
        ticket_id: int,
        store: str,
        ticket_no: str,
    ) -> None:
        """登记一个报修工单提交的订单（首次提交时调用）。

        v4.1 起不再自动延期；等货期间照常算时效，签收后开始计时（见 scheduler）。
        """
        with self.transaction("upsert_order_monitor"):
            self._conn.execute(
                """INSERT INTO order_monitor
                       (order_id, ticket_id, store, ticket_no, last_status,
                        shipped_notified, closed_notified, created_at, updated_at)
                   VALUES (?,?,?,?, '', 0, 0, ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET
                       ticket_id=excluded.ticket_id, store=excluded.store,
                       ticket_no=excluded.ticket_no, updated_at=excluded.updated_at""",
                (order_id, ticket_id, store, ticket_no, _now_str(), _now_str()),
            )

    def get_order_monitor(self, order_id: str) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM order_monitor WHERE order_id=?", (order_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_order_status(
        self,
        order_id: str,
        status: str,
        *,
        shipped_notified: bool | None = None,
        closed_notified: bool | None = None,
        received_at: str | None = None,
        received_notified: bool | None = None,
    ) -> None:
        """更新订单 last_status 与通知标记（状态变化检测 + 一次性通知）。

        received_at/received_notified：到货签收后一次性标记（开始计时维修）。
        """
        sets = ["last_status=?", "updated_at=?"]
        params: list[Any] = [status, _now_str()]
        if shipped_notified is not None:
            sets.append("shipped_notified=?")
            params.append(1 if shipped_notified else 0)
        if closed_notified is not None:
            sets.append("closed_notified=?")
            params.append(1 if closed_notified else 0)
        if received_at is not None:
            sets.append("received_at=?")
            params.append(received_at)
        if received_notified is not None:
            sets.append("received_notified=?")
            params.append(1 if received_notified else 0)
        params.append(order_id)
        self._conn.execute(
            f"UPDATE order_monitor SET {', '.join(sets)} WHERE order_id=?", params
        )

    def mark_order_xlsx_synced(self, order_id: str, *, synced: bool = True) -> None:
        """标记订单行已写入共享表 订单门店状态表.xlsx（写入兜底用，2026-08-25）。

        追加成功或该行本已存在都算已同步；共享表写失败时保持 0，
        由调度器 scan_shared_table_resync 周期重试。
        """
        self._conn.execute(
            "UPDATE order_monitor SET xlsx_synced=?, updated_at=? WHERE order_id=?",
            (1 if synced else 0, _now_str(), order_id),
        )

    def list_unsynced_orders(self) -> list[dict[str, Any]]:
        """尚未确认写入共享表的订单（xlsx_synced=0），按登记先后排序。

        查询侧镜像写入侧清洗（pipeline._handle_order_submitted 的
        none/null/nil/空串 规则，2026-08-26 回归加固）：此类占位形态是历史
        幽灵行（order_id='None'）的特征，永远不允许进入补同步队列——否则
        scan_shared_table_resync 会按周期反复尝试把垃圾行写进共享表。
        """
        rows = self.connect().execute(
            "SELECT * FROM order_monitor"
            " WHERE xlsx_synced=0 AND order_id IS NOT NULL ORDER BY created_at, rowid"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            oid = str(r["order_id"] or "").strip()
            if not oid or oid.lower() in ("none", "null", "nil"):
                continue  # 幽灵占位行：留在库中备查，但不入补同步队列
            out.append(dict(r))
        return out

    def list_received_active_tickets(
        self, *, received_before: str | None = None
    ) -> list[dict[str, Any]]:
        """有订单已签收、且仍处于活动态的工单（签收后每日提醒直至完成）。

        特殊情况暂停期间豁免（2026-08-26）。
        received_before（YYYY-MM-DD，可选）：仅返回最近一次签收日期严格早于该日期的
        工单（2026-08-28 用户决策：签收当天已有一次性「开始计时维修」通知，
        每日一催自签收次日开始）；不传则保持原有行为。
        """
        rows = self.connect().execute(
            """SELECT t.*,
                      (SELECT MAX(om.received_at) FROM order_monitor om
                        WHERE om.ticket_id = t.id) AS last_received_at
               FROM tickets t
               WHERE EXISTS (
                       SELECT 1 FROM order_monitor om
                       WHERE om.ticket_id = t.id AND om.received_at IS NOT NULL
                     )
                 AND t.status IN ('ACTIVE', 'ACTIVE_OVERDUE')
                 AND NOT EXISTS (
                     SELECT 1 FROM ticket_special_cases sc
                     WHERE sc.ticket_id = t.id AND sc.resumed_at IS NULL
                 )
               ORDER BY t.id""",
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            if received_before is not None:
                last_date = str(item.get("last_received_at") or "")[:10]
                if last_date >= received_before:  # 今天（或未来）刚签收 → 次日再催
                    continue
            out.append(item)
        return out
