"""回回归测试：tickets 老库重建迁移必须全量保留旧表数据（2026-08-26）。

历史缺陷：db._migrate_tickets_nullable_deadline 用显式 29 列 INSERT..SELECT
复制数据，漏掉了 _SCHEMA 中实际存在的 completed_confirm_by /
completed_confirm_at，且复制完立即 DROP TABLE tickets_old——任何一张
「deadline 列仍带 NOT NULL 且已有完工确认留痕」的库走这条升级路径时，
确认留痕被静默清零，不可恢复。

本文件冻结 2026-08-26 时点的 tickets 全部 31 列形态作为「老库」夹具：

- T1 复制保真：重建后完工确认两列的值原样保留，且 deadline 两列转为可空；
- T2 守卫：旧表若出现新 schema 未收录的列，必须报错拒绝而非静默丢弃，
  并原样保留 tickets_old 供人工处置。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

# ─────────────────────── 老库夹具 ───────────────────────

# 2026-08-26 形态 tickets 的 31 列（与 _SCHEMA 同名同序，差别仅在
# initial/current_deadline_at 仍带 NOT NULL 约束）。
_LEGACY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("ticket_no", "TEXT NOT NULL UNIQUE"),
    ("group_id", "TEXT NOT NULL"),
    ("store_name", "TEXT NOT NULL"),
    ("reporter_id", "TEXT NOT NULL"),
    ("subject", "TEXT NOT NULL"),
    ("location", "TEXT NOT NULL"),
    ("problem_description", "TEXT NOT NULL"),
    ("sla_days", "INTEGER NOT NULL"),
    ("initial_deadline_at", "TEXT NOT NULL DEFAULT ''"),
    ("current_deadline_at", "TEXT NOT NULL DEFAULT ''"),
    ("current_timeout_cycle_id", "INTEGER"),
    ("status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
    ("waiting_side", "TEXT NOT NULL DEFAULT 'NONE'"),
    ("waiting_since", "TEXT"),
    ("current_responsibility_cycle_id", "INTEGER"),
    ("last_business_event_at", "TEXT"),
    ("last_business_message_id", "TEXT"),
    ("version", "INTEGER NOT NULL DEFAULT 1"),
    ("cancelled_at", "TEXT"),
    ("cancelled_by", "TEXT"),
    ("cancel_reason", "TEXT"),
    ("stopped_at", "TEXT"),
    ("stopped_by", "TEXT"),
    ("stop_reason", "TEXT"),
    ("duplicate_of_ticket_id", "INTEGER"),
    ("reopen_count", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "TEXT NOT NULL"),
    ("closed_at", "TEXT"),
    ("completed_confirm_by", "TEXT"),
    ("completed_confirm_at", "TEXT"),
)


def _legacy_ddl(extra_column: tuple[str, str] | None = None) -> str:
    cols = [f"    {name} {ddl}," for name, ddl in _LEGACY_COLUMNS]
    if extra_column is not None:
        cols.append(f"    {extra_column[0]} {extra_column[1]},")
    body = "\n".join(cols).rstrip(",")
    return f"CREATE TABLE tickets (\n{body}\n)"


_LEGACY_ROW = {
    "ticket_no": "老店-主题-1天-001",
    "group_id": "G1",
    "store_name": "老店",
    "reporter_id": "r",
    "subject": "收银机",
    "location": "前台",
    "problem_description": "死机",
    "sla_days": 1,
    "initial_deadline_at": "2026-08-25 10:00:00",
    "current_deadline_at": "2026-08-26 10:00:00",
    "status": "ACTIVE",
    "waiting_side": "ENGINEER_SIDE",
    "waiting_since": "2026-08-26 09:00:00",
    "version": 3,
    "reopen_count": 1,
    "created_at": "2026-08-25 09:00:00",
    "completed_confirm_by": "uid-mgr",
    "completed_confirm_at": "2026-08-26 10:05:00",
}


def _make_legacy_db(path, extra_column=None):
    """构造一张「deadline 仍 NOT NULL」的老库，写入一行含确认留痕的数据。"""
    conn = sqlite3.connect(path)  # 默认 foreign_keys 关闭，避免空 groups 干扰
    conn.row_factory = sqlite3.Row
    conn.execute(_legacy_ddl(extra_column))
    names = [name for name, _ in _LEGACY_COLUMNS]
    values = {n: _LEGACY_ROW.get(n) for n in names}
    placeholders = ", ".join("?" for _ in names)
    conn.execute(
        f"INSERT INTO tickets ({', '.join(names)}) VALUES ({placeholders})",
        tuple(values[n] for n in names),
    )
    return conn


def _run_rebuild(conn) -> None:
    """直接驱动目标方法（无需完整 Database 实例：方法体不依赖实例状态）。"""
    from db import Database

    Database.__new__(Database)._migrate_tickets_nullable_deadline(conn)


def _fetch_only_row(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM tickets").fetchone()


def _assert_deadlines_nullable(conn) -> None:
    info = {
        r["name"]: r["notnull"]
        for r in conn.execute("PRAGMA table_info(tickets)").fetchall()
    }
    assert info["initial_deadline_at"] == 0, info
    assert info["current_deadline_at"] == 0, info
    conn.execute(
        "INSERT INTO tickets (ticket_no, group_id, store_name, reporter_id,"
        " subject, location, problem_description, sla_days, created_at)"
        " VALUES ('老店-主题-待商榷-002','G1','老店','r','s','l','p',0,'2026-08-26 11:00:00')"
    )
    row = conn.execute(
        "SELECT * FROM tickets WHERE ticket_no='老店-主题-待商榷-002'"
    ).fetchone()
    assert row["initial_deadline_at"] is None and row["current_deadline_at"] is None


# ─────────────────────────── 测试 ───────────────────────────


def test_rebuild_preserves_completed_confirm_columns(tmp_path):
    """重建迁移不得丢失 completed_confirm_by/at 的既有数据（回归核心）。"""
    conn = _make_legacy_db(tmp_path / "legacy.db")

    _run_rebuild(conn)

    row = _fetch_only_row(conn)
    # 其余字段照常保留（抽样三处非重点列作为锚点）
    assert row["ticket_no"] == "老店-主题-1天-001"
    assert row["version"] == 3
    assert row["waiting_since"] == "2026-08-26 09:00:00"
    # 回归本体：确认留痕不允许清零
    assert row["completed_confirm_by"] == "uid-mgr"
    assert row["completed_confirm_at"] == "2026-08-26 10:05:00"

    _assert_deadlines_nullable(conn)


def test_rebuild_refuses_unknown_legacy_column_instead_of_silent_loss(tmp_path):
    """旧表出现新 schema 未覆盖的列 → 报错保护，绝不静默丢弃数据。"""
    conn = _make_legacy_db(
        tmp_path / "legacy_extra.db",
        extra_column=("will_be_lost_col", "TEXT DEFAULT ''"),
    )
    conn.execute("UPDATE tickets SET will_be_lost_col='keepme'")

    with pytest.raises(RuntimeError) as excinfo:
        _run_rebuild(conn)

    assert "will_be_lost_col" in str(excinfo.value)
    # 保护现场：旧表连同其数据必须仍在（人工处置用）
    leftover_tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "tickets_old" in leftover_tables
    saved = conn.execute("SELECT will_be_lost_col FROM tickets_old").fetchone()
    assert saved["will_be_lost_col"] == "keepme"


def test_column_names_used_for_copy_are_plain_identifiers():
    """动态拼进 SQL 的列名必须是纯标识符（防御未知对象名注入拼接）。"""
    identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    for name, _ddl in _LEGACY_COLUMNS:
        assert identifier.fullmatch(name), name
