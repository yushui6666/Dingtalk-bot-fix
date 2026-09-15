"""本地工单 → 钉钉 AI 表格「报修工单」表同步（看板数据源）。

被 scripts/sync_tickets_to_aitable.py（手动全量/增量）和
workers/scheduler.py（定期增量）复用。基于 dws CLI 调用，不依赖模型。

以工单号（ticket_no）为唯一键：线上已存在则更新，否则创建。
调用方控制频率（scheduler 默认每 120 秒一次；业务事件后也可手动触发）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from config import AITABLE_SYNC_BASE_ID, AITABLE_SYNC_TABLE_ID, DWS_CMD
from db import Database
from logger import get_logger

logger = get_logger(__name__)

# 本地 tickets 字段 → AI 表格「报修工单」字段 ID（dws aitable field list 获取）
_FIELD_IDS = {
    "ticket_no": "vNi4Gyn",       # 工单号
    "store_name": "EDr7rtx",      # 门店
    "subject": "SSXAzuj",         # 主题
    "location": "37eWToV",        # 位置
    "problem_description": "ALiF0so",  # 问题描述
    "sla_days": "gvpL0iy",        # 时效(天)
    "status": "xEPIpPz",          # 状态
    "waiting_side": "ObQu8e5",    # 等待方
    "created_at": "gwR5jOr",      # 创建时间
    "current_deadline_at": "QRBQ8PZ",  # 当前截止
    "last_business_event_at": "r9P0ELf",  # 最后业务事件
    "closed_at": "Dr4Yx4i",       # 关闭时间
    "version": "g0i1WrY",         # 版本
    "reopen_count": "Jr7AISo",    # 重开次数
    "engineer": "3PJf0YA",        # 工程师（多选，来自门店群配置）
    "messages": "RUQR1Ii",        # 消息记录（多行文本，已归属工单的消息）
}

# 工程师 userId → AI 表格选项姓名（与 groups.json engineer_ids 对应）
_ENGINEER_NAME_MAP = {
    "90000000000000001": "工程师B",
    "90000000000000002": "工程师C",
    "9000000000000001": "工程师D",
    "900000000000000002": "工程师A",
}

_DATE_COLUMNS = {
    "created_at",
    "current_deadline_at",
    "last_business_event_at",
    "closed_at",
}


def _run_dws(args: list[str]) -> dict:
    """调用 dws CLI 并解析 JSON 输出；失败抛异常。"""
    cmd = [DWS_CMD, *args, "--format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"dws {' '.join(args[:3])} 失败: {proc.stderr[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dws 输出非 JSON: {exc}") from exc


def _fmt_datetime(value: str | None) -> str | None:
    """'2026-08-13 14:49:42' → '2026-08-13T14:49:00+08:00'（AI 表格 date 格式）。"""
    if not value:
        return None
    text = str(value).strip()
    try:
        date_part, time_part = text.split(" ")
    except ValueError:
        return text
    hh, mm = time_part.split(":")[:2]
    return f"{date_part}T{hh}:{mm}:00+08:00"


def _fmt_ticket_messages(rows: list) -> str:
    """工单已归属消息 → 可读多行文本（时间 角色：内容）。

    rows: messages 表行（已按 sent_at 排序），角色取 sender_role。
    """
    lines: list[str] = []
    for row in rows:
        sent_at = str(row["sent_at"] or "")[:16]
        role = {
            "MANAGER": "店长",
            "ENGINEER": "工程师",
            "LEADER": "工程负责人",
            "OTHER": "成员",
        }.get(row["sender_role"], row["sender_role"] or "未知")
        content = str(row["content"] or "").strip()
        lines.append(f"[{sent_at}] {role}: {content}")
    return "\n".join(lines)


def _messages_by_ticket(db: Database) -> dict[int, str]:
    """{ticket_id: 消息文本}，一次性拉取所有已归属工单的消息。"""
    conn = db.connect()
    rows = conn.execute(
        "SELECT ticket_id, sender_role, content, sent_at FROM messages"
        " ORDER BY ticket_id, sent_at"
    ).fetchall()
    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r["ticket_id"], []).append(r)
    return {tid: _fmt_ticket_messages(msgs) for tid, msgs in grouped.items()}


def _ticket_to_cells(row: dict, engineers: list[str] | None = None,
                     messages: str | None = None) -> dict:
    """本地工单行 → AI 表格 cells（fieldId → value）。

    engineers: 该工单所属门店配置的工程师姓名列表（填多选字段），可空。
    messages: 该工单已归属消息文本（填「消息记录」字段），可空。
    """
    cells: dict = {}
    for field, field_id in _FIELD_IDS.items():
        if field == "engineer":
            if engineers:
                cells[field_id] = engineers
            continue
        if field == "messages":
            if messages:
                cells[field_id] = messages
            continue
        value = row.get(field)
        if value is None:
            continue
        if field == "sla_days" and value == 0:
            continue  # 待商榷工单不设时效：看板「时效(天)」留空，由工单号体现
        if field in _DATE_COLUMNS:
            value = _fmt_datetime(value)
        cells[field_id] = value
    return cells


def _group_engineers_by_store() -> dict[str, list[str]]:
    """{门店名: [工程师姓名...]}，来自群配置 GROUPS 的 engineer_ids。"""
    from config import GROUPS

    mapping: dict[str, list[str]] = {}
    for g in GROUPS:
        names = [
            _ENGINEER_NAME_MAP[uid]
            for uid in g.get("engineer_ids", [])
            if uid in _ENGINEER_NAME_MAP
        ]
        if names:
            mapping.setdefault(g["store_name"], names)
    return mapping


def _online_ticket_no_map() -> dict[str, str]:
    """线上「报修工单」表全部记录 → {工单号: recordId}。"""
    out = _run_dws([
        "aitable", "record", "query",
        "--base-id", AITABLE_SYNC_BASE_ID, "--table-id", AITABLE_SYNC_TABLE_ID,
    ])
    recs = out.get("data", {}).get("records", [])
    mapping: dict[str, str] = {}
    for rec in recs:
        cells = rec.get("cells", {})
        ticket_no = cells.get(_FIELD_IDS["ticket_no"])
        if ticket_no:
            mapping[str(ticket_no)] = rec["recordId"]
    return mapping


def _batch(records: list[dict], size: int = 100) -> list[list[dict]]:
    return [records[i:i + size] for i in range(0, len(records), size)]


def sync_once(db: Database, *, full: bool = False, prune: bool = False) -> dict:
    """同步一轮：返回 {online_total, to_create, to_update, created, updated, to_delete, deleted}。

    full=False 时增量同步（活动工单、最近 7 天关闭、有版本更新的工单）；
    full=True 时全量 upsert 所有工单。
    prune=True 时自动删除线上存在但本地已不存在的工单（镜像删除）。
    """
    conn = db.connect()
    if full:
        rows = conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status IN ('ACTIVE','ACTIVE_OVERDUE')"
            " OR (closed_at IS NOT NULL AND created_at >= datetime('now', '-7 days'))"
            " OR version > 1 ORDER BY id"
        ).fetchall()

    online = _online_ticket_no_map()
    engineers_by_store = _group_engineers_by_store()
    messages_by_ticket = _messages_by_ticket(db)
    creates: list[dict] = []
    updates: list[dict] = []
    for row in rows:
        d = dict(row)
        ticket_no = d["ticket_no"]
        engineers = engineers_by_store.get(d["store_name"])
        messages = messages_by_ticket.get(d["id"])
        cells = _ticket_to_cells(d, engineers, messages)
        if ticket_no in online:
            updates.append({"recordId": online[ticket_no], "cells": cells})
        else:
            creates.append({"cells": cells})

    created_total = updated_total = 0
    for chunk in _batch(creates):
        out = _run_dws([
            "aitable", "record", "create",
            "--base-id", AITABLE_SYNC_BASE_ID, "--table-id", AITABLE_SYNC_TABLE_ID,
            "--records", json.dumps(chunk, ensure_ascii=False),
        ])
        created_total += len(out.get("data", {}).get("newRecordIds", []))
    for chunk in _batch(updates):
        out = _run_dws([
            "aitable", "record", "upsert",
            "--base-id", AITABLE_SYNC_BASE_ID, "--table-id", AITABLE_SYNC_TABLE_ID,
            "--records", json.dumps(chunk, ensure_ascii=False),
        ])
        updated_total += len(out.get("data", {}).get("updatedRecordIds", []))

    # 镜像删除：线上存在但本地不存在的工单 → 删除
    deleted_total = 0
    to_delete: list[dict] = []
    if prune:
        local_nos = {
            str(r["ticket_no"])
            for r in conn.execute("SELECT ticket_no FROM tickets").fetchall()
        }
        to_delete = [
            {"recordId": rid, "ticket_no": no}
            for no, rid in online.items()
            if no not in local_nos
        ]
        for chunk in _batch(to_delete):
            ids = [d["recordId"] for d in chunk]
            out = _run_dws([
                "aitable", "record", "delete",
                "--base-id", AITABLE_SYNC_BASE_ID, "--table-id", AITABLE_SYNC_TABLE_ID,
                "--record-ids", ",".join(ids), "--yes",
            ])
            deleted_total += out.get("data", {}).get("deletedCount", 0)

    result = {
        "online_total": len(online),
        "to_create": len(creates),
        "to_update": len(updates),
        "created": created_total,
        "updated": updated_total,
        "to_delete": len(to_delete) if prune else 0,
        "deleted": deleted_total,
    }
    return result


def sync(db_path: Path, *, dry_run: bool = False, full: bool = False, prune: bool = False) -> dict:
    """CLI 入口兼容：dry_run 时只统计不写入。"""
    db = Database(db_path)
    if dry_run:
        conn = db.connect()
        if full:
            rows = conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE status IN ('ACTIVE','ACTIVE_OVERDUE')"
                " OR (closed_at IS NOT NULL AND created_at >= datetime('now', '-7 days'))"
                " OR version > 1 ORDER BY id"
            ).fetchall()
        online = _online_ticket_no_map()
        messages_by_ticket = _messages_by_ticket(db)
        creates = [
            {"ticket_no": r["ticket_no"], "cells": _ticket_to_cells(
                dict(r), None, messages_by_ticket.get(r["id"]))}
            for r in rows if r["ticket_no"] not in online
        ]
        updates = [
            {"ticket_no": r["ticket_no"], "cells": _ticket_to_cells(
                dict(r), None, messages_by_ticket.get(r["id"]))}
            for r in rows if r["ticket_no"] in online
        ]
        local_nos = {
            str(r["ticket_no"])
            for r in conn.execute("SELECT ticket_no FROM tickets").fetchall()
        }
        to_delete = [no for no in online if no not in local_nos] if prune else []
        return {
            "online_total": len(online),
            "to_create": len(creates),
            "to_update": len(updates),
            "created": None,
            "updated": None,
            "to_delete": len(to_delete),
            "deleted": None,
            "note": "dry-run 模式，未实际写入",
        }
    return sync_once(db, full=full, prune=prune)
