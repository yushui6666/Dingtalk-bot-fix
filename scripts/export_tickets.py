"""导出工单到 CSV（Excel 可直接打开）。

用法::

    python scripts/export_tickets.py                    # 按时间戳导出到 data/
    python scripts/export_tickets.py --group 测试群      # 只导某个群
    python scripts/export_tickets.py -o /tmp/工单.csv    # 指定输出路径
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import BASE_DIR, GROUPS  # noqa: E402
from db import Database  # noqa: E402

# 工单状态 → 中文
_STATUS_LABELS = {
    "ACTIVE": "进行中",
    "ACTIVE_OVERDUE": "已超时",
    "PENDING_CONFIRM": "待店长确认",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
    "STOPPED": "已停修",
}

_HEADERS = [
    "工单编号", "门店", "主题", "位置", "问题描述", "状态",
    "时效(天)", "创建时间", "预计完成", "实际完成", "报修人ID", "取消原因",
    "消息记录",
]

_ROLE_LABELS = {
    "MANAGER": "店长",
    "ENGINEER": "工程师",
    "LEADER": "工程负责人",
    "OTHER": "其他成员",
    "SYSTEM": "系统",
}


def _ticket_messages(conn, ticket_id: int) -> str:
    """工单全部消息，按时间顺序合并成一段文本。"""
    rows = conn.execute(
        "SELECT sender_role, content, sent_at FROM messages "
        "WHERE ticket_id=? ORDER BY sent_at, id",
        (ticket_id,),
    ).fetchall()
    parts = []
    for r in rows:
        role = _ROLE_LABELS.get(r["sender_role"], r["sender_role"])
        content = str(r["content"] or "").replace("\n", " ")
        parts.append(f"[{r['sent_at']} {role}] {content}")
    return "\n".join(parts)


def export(group_filter: str | None, output: Path) -> int:
    db = Database()
    conn = db.connect()

    rows = conn.execute(
        "SELECT * FROM tickets ORDER BY created_at DESC"
    ).fetchall()

    # 群过滤：群名或工单编号前缀匹配
    group_names = {g["store_name"] for g in GROUPS}
    if group_filter:
        group_names = {group_filter}

    tickets = []
    for row in rows:
        d = dict(row)
        if group_filter and d["store_name"] not in group_names:
            continue
        tickets.append(d)

    with open(output, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(_HEADERS)
        for t in tickets:
            writer.writerow([
                t["ticket_no"],
                t["store_name"],
                t["subject"],
                t["location"],
                (t["problem_description"] or "").replace("\n", " "),
                _STATUS_LABELS.get(t["status"], t["status"]),
                t["sla_days"],
                t["created_at"],
                t["current_deadline_at"],
                t["closed_at"] or "",
                t["reporter_id"],
                (t["cancel_reason"] or "").replace("\n", " "),
                _ticket_messages(conn, t["id"]),
            ])

    print(f"已导出 {len(tickets)} 张工单 → {output}")
    return len(tickets)


def main() -> None:
    parser = argparse.ArgumentParser(description="导出工单 CSV")
    parser.add_argument("--group", default=None, help="仅导出指定群名（默认全部）")
    parser.add_argument("-o", "--output", type=Path, default=None, help="输出文件路径")
    args = parser.parse_args()

    output = args.output or (
        BASE_DIR / "data" / f"tickets_export_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    export(args.group, output)


if __name__ == "__main__":
    main()
