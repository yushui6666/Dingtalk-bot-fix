"""一次性（2026-09-03）：为「杭州商场23龙湖店」静默补建 3 张工单。

来源：店长 #报修（截图 2，用户文字转录）；时效按用户指示统一给 7 天。
- 零号特工-第一个房间手枪柜门+第三个房间挂门链-背板已经被钉子打的没地方钉钉子
- 天才特工营-部分按键背板-部分按键背板需更换，钉子没地方钉了
- 财阀继承人（八爪鱼）-按键背板-按键背板需更换，钉子没地方钉了

静默保证：只走 TicketRepository.create_ticket 落库，不插入任何 Outbox 通知、
不向群/用户外发消息；运行中的调度器自动同步 AI 表格看板。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from db import Database  # noqa: E402
from tickets.repository import TicketRepository  # noqa: E402

GROUP_ID = "cid_demo_group_g=="  # 杭州商场23龙湖店
STORE_NAME = "杭州商场23龙湖店"
REPORTER_ID = "90000000000000008"  # 店长
DB_PATH = _ROOT / "data" / "tickets.db"

CREATES: list[tuple[str, str, str, str]] = [
    ("零号特工", "第一个房间手枪柜门+第三个房间挂门链", "背板已经被钉子打的没地方钉钉子", "7天"),
    ("天才特工营", "部分按键背板", "部分按键背板需更换，钉子没地方钉了", "7天"),
    ("财阀继承人", "八爪鱼按键背板", "按键背板需更换，钉子没地方钉了", "7天"),
]


def backup(db_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = db_path.with_name(f"tickets.db.bak_{ts}_manual_create")
    src = sqlite3.connect(str(db_path))
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def main() -> int:
    db = Database(str(DB_PATH))
    db.init_schema()
    repo = TicketRepository(db)

    group = db.get_group(GROUP_ID)
    if group is None or group["store_name"] != STORE_NAME:
        print(f"ERROR 群配置缺失: {GROUP_ID}")
        return 1

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bak = backup(DB_PATH)
    print(f"[backup] {bak}")

    created = []
    for subject, location, problem, sla_label in CREATES:
        ticket_id = repo.create_ticket(
            group=group, reporter_id=REPORTER_ID,
            subject=subject, location=location,
            problem_description=problem, sla_label=sla_label, now=now,
        )
        t = db.get_ticket(ticket_id)
        created.append(t)
        print(
            f"[done] {t['ticket_no']} | {location} | {problem} | "
            f"截止 {t['current_deadline_at']}"
        )

    print(f"\n共新建 {len(created)} 张工单（静默：未外发任何消息，看板由调度器自动同步）")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
