"""按模版导出工单为 Markdown 记录，保存到 工单记录/ 目录。

用法::

    python scripts/export_tickets_md.py                    # 导出全部工单
    python scripts/export_tickets_md.py --group 测试群      # 只导某个群
    python scripts/export_tickets_md.py --ticket 编号       # 只导某张工单

输出目录：/Users/example/Desktop/钉钉消息/工单记录/（可用 --out 覆盖）
每个工单一个 .md 文件，文件名 = 工单编号.md。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import GROUPS, USER_ID_MAP  # noqa: E402
from db import Database  # noqa: E402

# 内置兜底映射（测试账号等未出现在门店 CSV 中的成员）
NAME_MAP: dict[str, str] = {
    "oid_manager_b": "店长B",
    "oid_engineer_d": "工程师D",
    "oid_manager_a": "店长A",
}

# 门店成员姓名映射文件（openDingtalkId → 姓名，含店长/区域负责人/工程师等）
_STORE_CSV = Path("/Users/example/WorkBuddy/2026-08-13-16-38-27/dingtalk_stores/门店群数据汇总_v2.csv")


def _load_store_name_map() -> dict[str, str]:
    """从门店 CSV 提取 ID → 姓名 映射。

    同时索引 openDingtalkId 与 userId 两套 ID（库内消息/群配置存的是数字
    userId，导出时两者都可能遇到）。
    """
    import csv

    mapping: dict[str, str] = {}
    if not _STORE_CSV.exists():
        return mapping
    with open(_STORE_CSV, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            for name_col, oid_cols in [
                ("店长姓名", ("店长openDingtalkId", "店长userId")),
                ("区域负责人姓名", ("区域负责人openDingtalkId", "区域负责人userId")),
                ("总工程师姓名", ("总工程师openDingtalkId", "总工程师userId")),
                ("工程师姓名", ("工程师openDingtalkId", "工程师userId")),
            ]:
                names = [n.strip() for n in (row.get(name_col) or "").split(";") if n.strip()]
                for oid_col in oid_cols:
                    oids = [o.strip() for o in (row.get(oid_col) or "").split(";") if o.strip()]
                    for name, oid in zip(names, oids):
                        if name and oid:
                            mapping[oid] = name
    return mapping


NAME_MAP = {**_load_store_name_map(), **NAME_MAP}

_ROLE_LABELS = {
    "MANAGER": "店长",
    "ENGINEER": "工程师",
    "LEADER": "工程负责人",
    "OTHER": "其他成员",
    "SYSTEM": "系统",
}

_STATUS_LABELS = {
    "ACTIVE": "进行中",
    "ACTIVE_OVERDUE": "已超时",
    "PENDING_CONFIRM": "待店长确认",
    "PENDING_NEGOTIATION": "待商榷",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
    "STOPPED": "已停修",
}


def _name(oid: str | None) -> str:
    if not oid:
        return "—"
    return NAME_MAP.get(oid, oid[:8] + "…")


def _fmt(dt: str) -> str:
    return (dt or "")[:16]


def _status_text(t: dict) -> str:
    return _STATUS_LABELS.get(t["status"], t["status"])


def _order_status_label(status: str) -> str:
    return (status or "").strip() or "未更新"


def _diagnosis_current(conn, ticket_id: int) -> list[str]:
    row = conn.execute(
        "SELECT items_json FROM diagnosis_versions WHERE ticket_id=? AND is_current=1 "
        "ORDER BY id DESC LIMIT 1", (ticket_id,)
    ).fetchone()
    if not row:
        return []
    try:
        return list(json.loads(row["items_json"]))
    except (json.JSONDecodeError, TypeError):
        return []


def _diagnosis_history(conn, ticket_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM diagnosis_versions WHERE ticket_id=? ORDER BY id", (ticket_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _repair_current(conn, ticket_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM repair_method_versions WHERE ticket_id=? AND is_current=1 "
        "ORDER BY id DESC LIMIT 1", (ticket_id,)
    ).fetchone()
    return dict(row) if row else None


def _order_info(conn, ticket_id: int) -> list[dict]:
    """工单关联订单（订单号 + 最后状态），来自 order_monitor 监控表。"""
    rows = conn.execute(
        "SELECT order_id, last_status FROM order_monitor WHERE ticket_id=? ORDER BY order_id",
        (ticket_id,),
    ).fetchall()
    orders = [dict(r) for r in rows]
    if not orders:
        # 兜底：仅登记过订单号但未进入监控表的工单
        rcur = _repair_current(conn, ticket_id)
        if rcur and rcur.get("order_no"):
            orders = [{"order_id": rcur["order_no"], "last_status": ""}]
    return orders


def _repair_history(conn, ticket_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM repair_method_versions WHERE ticket_id=? ORDER BY id", (ticket_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _part_current(conn, ticket_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM part_versions WHERE ticket_id=? AND is_current=1 "
        "ORDER BY id DESC LIMIT 1", (ticket_id,)
    ).fetchone()
    return dict(row) if row else None


def _part_history(conn, ticket_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM part_versions WHERE ticket_id=? ORDER BY id", (ticket_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _timeout_history(conn, ticket_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM timeout_cycles WHERE ticket_id=? ORDER BY cycle_no", (ticket_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _messages(conn, ticket_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE ticket_id=? ORDER BY sent_at, id", (ticket_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _attachments(conn, ticket_id: int) -> list[dict]:
    """工单全部已归档图片附件（含多模态解析结果）。"""
    rows = conn.execute(
        "SELECT * FROM message_attachments "
        "WHERE ticket_id=? AND stored_path IS NOT NULL "
        "ORDER BY id",
        (ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _image_absolute_path(stored_path: str | None) -> str | None:
    """附件相对路径 → 绝对路径（用于 Markdown 展示）。"""
    if not stored_path:
        return None
    try:
        from images.archive import ImageArchiveStore

        store = ImageArchiveStore()
        return str(store.resolve_relative_path(stored_path))
    except Exception:
        return None


def render_ticket(t: dict, conn) -> str:
    tid = t["id"]
    lines: list[str] = []

    # 标题
    lines.append(f"# {t['ticket_no']}\n")
    lines.append("| 字段 | 内容 |")
    lines.append("|------|------|")
    lines.append(f"| 工单命名 | {t['ticket_no']} |")
    lines.append(f"| 店铺 | {t['store_name']} |")
    lines.append(f"| 状态 | {_status_text(t)} |")
    lines.append(f"| 报修人 | {_name(t['reporter_id'])} |")
    lines.append(f"| 主题 | {t['subject']} |")
    lines.append(f"| 位置 | {t['location']} |")
    lines.append(f"| 问题描述 | {(t['problem_description'] or '').replace(chr(10), ' ') } |")
    lines.append(f"| 时效 | {t['sla_days']}天 |")
    lines.append(f"| 报修时间 | {_fmt(t['created_at'])} |")
    lines.append(f"| 初始截止时间 | {_fmt(t['initial_deadline_at'])} |")
    lines.append(f"| 最终截止时间 | {_fmt(t['current_deadline_at'])} |")

    # 订单号 + 最后状态（order_monitor 监控表）
    orders = _order_info(conn, tid)
    if orders:
        lines.append("| 订单号 | " + "、".join(o["order_id"] for o in orders) + " |")
        last_status = "；".join(
            f"{o['order_id']}:{_order_status_label(o['last_status'])}" for o in orders
        )
        lines.append(f"| 订单最后状态 | {last_status} |")
    else:
        lines.append("| 订单号 | （暂无） |")
        lines.append("| 订单最后状态 | （暂无） |")

    # 关闭信息（若已关闭/取消/停修）
    if t["status"] in ("COMPLETED", "CANCELLED"):
        lines.append(f"| 关闭人 | {_name(t.get('cancelled_by') or '') if t['status']=='CANCELLED' else _closer_name(conn, tid, t)} |")
        lines.append(f"| 关闭角色 | {_closer_role(conn, tid, t)} |")
        lines.append(f"| 关闭时间 | {_fmt(t['closed_at'] or t['cancelled_at'])} |")
        if t.get("cancel_reason"):
            lines.append(f"| 取消原因 | {(t['cancel_reason'] or '').replace(chr(10), ' ')} |")
    elif t["status"] == "STOPPED":
        lines.append(f"| 停修人 | {_name(t.get('stopped_by') or '')} |")
        lines.append(f"| 停修时间 | {_fmt(t['stopped_at'])} |")
        if t.get("stop_reason"):
            lines.append(f"| 停修原因 | {(t['stop_reason'] or '').replace(chr(10), ' ')} |")
    lines.append("")

    # 当前故障判断
    diag = _diagnosis_current(conn, tid)
    lines.append("## 当前故障判断\n")
    if diag:
        for d in diag:
            lines.append(f"- {d}")
    else:
        lines.append("- （暂无）")
    lines.append("")

    # 故障判断历史
    dhis = _diagnosis_history(conn, tid)
    lines.append("## 故障判断历史\n")
    for i, v in enumerate(dhis, 1):
        try:
            items = list(json.loads(v["items_json"]))
        except (json.JSONDecodeError, TypeError):
            items = []
        lines.append(f"### 第 {i} 版 — {_name(v['engineer_id'])}（{_fmt(v['submitted_at'])}）\n")
        for it in items:
            lines.append(f"- {it}")
        lines.append("")
    if not dhis:
        lines.append("- （无）\n")

    # 当前维修方式
    rcur = _repair_current(conn, tid)
    lines.append("## 当前维修方式\n")
    if rcur:
        lines.append(f"维修方式：{rcur['repair_method']}\n")
        if rcur.get("order_no"):
            lines.append(f"当前订单号：{rcur['order_no']}\n")
    else:
        lines.append("- （暂无）\n")
    lines.append("")

    # 维修方式历史
    rhis = _repair_history(conn, tid)
    lines.append("## 维修方式历史\n")
    for i, v in enumerate(rhis, 1):
        lines.append(f"### 第 {i} 版 — {_name(v['engineer_id'])}（{_fmt(v['submitted_at'])}）\n")
        lines.append(f"维修方式：{v['repair_method']}\n")
        if v.get("order_no"):
            lines.append(f"订单号：{v['order_no']}\n")
        lines.append("")
    if not rhis:
        lines.append("- （无）\n")

    # 本次使用备件（2026-09-09 备件登记；仅记录，不动库存）
    pcur = _part_current(conn, tid)
    lines.append("## 本次使用备件\n")
    if pcur:
        parts_text = pcur.get("parts") or ""
        lines.append(f"备件：{parts_text}\n")
        lines.append(f"记录人：{_name(pcur.get('engineer_id'))}（{_fmt(pcur.get('submitted_at'))}）\n")
    else:
        lines.append("- （无）\n")
    lines.append("")

    # 备件历史
    phis = _part_history(conn, tid)
    lines.append("## 备件历史\n")
    for i, v in enumerate(phis, 1):
        parts_text = v.get("parts") or ""
        lines.append(f"### 第 {i} 版 — {_name(v.get('engineer_id'))}（{_fmt(v.get('submitted_at'))}）\n")
        lines.append(f"备件：{parts_text}\n")
        if v.get("raw_text"):
            lines.append(f"原文：{v['raw_text']}\n")
        lines.append("")
    if not phis:
        lines.append("- （无）\n")

    # 超时与延期记录
    touts = _timeout_history(conn, tid)
    lines.append("## 超时与延期记录\n")
    if touts:
        lines.append("| 轮次 | 旧截止时间 | 超时原因 | 提交人 | 提交时间 | 新截止时间 |")
        lines.append("|------|-----------|----------|--------|----------|-----------|")
        for v in touts:
            lines.append(
                f"| {v['cycle_no']} | {_fmt(v['old_deadline_at'])} | "
                f"{(v['reason'] or '').replace(chr(10), ' ')} | "
                f"{_name(v['reason_engineer_id'])} | {_fmt(v['reason_submitted_at'])} | "
                f"{_fmt(v['new_deadline_at'])} |"
            )
        lines.append("")
    else:
        lines.append("- （无）\n")

    # 报修内容
    msgs = _messages(conn, tid)
    create_msg = next((m for m in msgs if m["sender_role"] == "MANAGER" and "报修" in (m["content"] or "")), msgs[0] if msgs else None)
    lines.append("## 报修内容\n")
    if create_msg:
        lines.append("```\n" + create_msg["content"].strip() + "\n```\n")
    lines.append("")

    # 处理过程
    lines.append("## 处理过程\n")
    lines.append("> 以下按消息发送时间顺序排列。\n")
    for m in msgs:
        role = _ROLE_LABELS.get(m["sender_role"], m["sender_role"])
        sender = _name(m["sender_id"])
        lines.append(f"**{sender}**（{role}）{_fmt(m['sent_at'])}")
        content = m["content"].strip()
        for seg in content.split("\n"):
            lines.append(f"> {seg}")
        lines.append("")
    if not msgs:
        lines.append("- （无消息记录）\n")

    # 图片与解析内容
    atts = _attachments(conn, tid)
    if atts:
        lines.append("## 图片记录\n")
        for a in atts:
            abs_path = _image_absolute_path(a["stored_path"])
            if abs_path:
                lines.append(f"![图片]({abs_path})")
            else:
                lines.append(f"- 图片（{a['mime_type'] or '未知'}，未归档）")
            vision = (a.get("vision_result_json") or "").strip()
            if vision:
                lines.append("")
                lines.append(f"**解析内容**：{vision}")
            elif a.get("analyzed_status") == "ANALYZED" and not vision:
                lines.append("")
                lines.append("**解析内容**：（空）")
            lines.append("")
        lines.append("")

    # 完成确认（需求 2026-08-24 #3：工程师报完工 → 店长「确认修好」后才算完成）
    if t["status"] == "PENDING_CONFIRM":
        lines.append("## 完成确认\n")
        lines.append("- 工程师已报完工，等待店长回复「确认修好」或「没修好」")
        if t.get("waiting_since"):
            lines.append(f"- 等待开始：{_fmt(t['waiting_since'])}（超时将按响应 SLA 提醒/升级）")
        lines.append("")
    elif t["status"] == "COMPLETED":
        lines.append("## 完成确认\n")
        if t.get("completed_confirm_by"):
            lines.append(f"- 确认人：{_name(t['completed_confirm_by'])}（店长）")
            lines.append(f"- 确认时间：{_fmt(t['completed_confirm_at'])}")
            reject_note = _reject_history(conn, tid)
            for r in reject_note:
                lines.append(f"- 此前店长曾反馈未修好：{r}")
        else:
            lines.append("- 店长本人报完工，直接完成（无独立确认环节）")
        lines.append("")

    # 关闭信息
    lines.append("## 关闭信息\n")
    if t["status"] == "COMPLETED":
        lines.append(f"- 关闭人：{_closer_name(conn, tid, t)}（{_closer_role(conn, tid, t)}）")
        lines.append(f"- 关闭时间：{_fmt(t['closed_at'])}")
        close_msg = _completion_message(conn, tid, t)
        if close_msg:
            lines.append(f"- 关闭消息：{close_msg['content'].strip()}")
    elif t["status"] == "CANCELLED":
        lines.append(f"- 取消人：{_name(t['cancelled_by'])}")
        lines.append(f"- 取消时间：{_fmt(t['cancelled_at'])}")
    elif t["status"] == "STOPPED":
        lines.append(f"- 停修人：{_name(t.get('stopped_by') or '')}")
        lines.append(f"- 停修时间：{_fmt(t.get('stopped_at') or '')}")
        if t.get("stop_reason"):
            lines.append(f"- 停修原因：{(t['stop_reason'] or '').strip()}")
    lines.append("")

    return "\n".join(lines)


def _completion_message(conn, ticket_id: int, t: dict) -> dict | None:
    """完工消息：优先用工单的 last_business_message_id（最后业务事件），可靠。"""
    last_id = t.get("last_business_message_id")
    if last_id:
        row = conn.execute(
            "SELECT * FROM messages WHERE ticket_id=? AND message_id=?",
            (ticket_id, last_id),
        ).fetchone()
        if row:
            return dict(row)
    msgs = _messages(conn, ticket_id)
    for m in reversed(msgs):
        if m["sender_role"] in ("MANAGER", "ENGINEER"):
            return m
    return None


def _reject_history(conn, ticket_id: int) -> list[str]:
    """店长驳回完工（「没修好」）的历史记录，按时间排列。

    驳回理由不落独立列，从消息归档中按链接类型 CONFIRM_WINDOW + 内容识别。
    """
    rows = conn.execute(
        "SELECT m.content, m.sent_at FROM messages m "
        "JOIN message_ticket_links l ON l.message_id = m.message_id "
        "WHERE m.ticket_id=? AND l.link_type='CONFIRM_WINDOW' "
        "AND m.sender_role='MANAGER' AND (m.content LIKE '%没修好%' OR m.content LIKE '%还未修好%') "
        "ORDER BY m.sent_at, m.id",
        (ticket_id,),
    ).fetchall()
    return [
        f"「{(r['content'] or '').strip()[:60]}」（{_fmt(r['sent_at'])}）"
        for r in rows
    ]


def _closer_name(conn, ticket_id: int, t: dict) -> str:
    close = _completion_message(conn, ticket_id, t)
    if close:
        return _name(close["sender_id"])
    return _name(t.get("closed_by") or "")


def _closer_role(conn, ticket_id: int, t: dict) -> str:
    close = _completion_message(conn, ticket_id, t)
    if close:
        return _ROLE_LABELS.get(close["sender_role"], close["sender_role"])
    return ""


def export_all(*, out_dir: Path, group_filter: str | None = None, ticket_filter: str | None = None) -> int:
    db = Database()
    conn = db.connect()

    where = ""
    params: list = []
    if ticket_filter:
        where = "WHERE ticket_no=?"
        params = [ticket_filter]
    rows = conn.execute(f"SELECT * FROM tickets {where} ORDER BY created_at DESC", params).fetchall()

    group_names = {g["store_name"] for g in GROUPS}
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for row in rows:
        t = dict(row)
        if group_filter and t["store_name"] != group_filter:
            continue
        md = render_ticket(t, conn)
        out = out_dir / f"{t['ticket_no']}.md"
        out.write_text(md, encoding="utf-8")
        written += 1
        print(f"已导出 {t['ticket_no']} → {out}")
    print(f"\n共导出 {written} 张工单 → {out_dir}")
    db.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="按模版导出工单 Markdown 记录")
    parser.add_argument("--group", default=None, help="仅导出指定群名（默认全部）")
    parser.add_argument("--ticket", default=None, help="仅导出指定工单编号")
    parser.add_argument("--out", type=Path, default=Path("/Users/example/Desktop/钉钉消息/工单记录"),
                        help="输出目录（默认 工单记录/）")
    args = parser.parse_args()
    export_all(out_dir=args.out, group_filter=args.group, ticket_filter=args.ticket)


if __name__ == "__main__":
    main()
