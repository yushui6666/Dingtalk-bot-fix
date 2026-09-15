"""死信重放（2026-09-03）：把 DEAD_LETTER 消息重置回 RECEIVED，交收件箱工作器重跑。

背景：死信均为「模型调用失败」重试耗尽所致（08-26 已修复 thinking 模式挤占
与 max_tokens，09-03 又补了归一化/兜底），重跑时走新代码 + 新提示词。
默认 dry-run 列出死信；--execute 重置状态（attempts=0、清空错误与下次时间）。
可加 --message-ids 只重放指定消息。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import Database  # noqa: E402

DB_PATH = ROOT / "data" / "tickets.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--message-ids", nargs="*", default=None)
    args = ap.parse_args()

    db = Database(str(DB_PATH))
    rows = [dict(r) for r in db.connect().execute(
        "SELECT message_id, group_id, sender_role, content, attempts, last_error, sent_at"
        " FROM inbox_messages WHERE status='DEAD_LETTER' ORDER BY sent_at"
    ).fetchall()]
    if args.message_ids:
        wanted = set(args.message_ids)
        rows = [r for r in rows if r["message_id"] in wanted]

    if not rows:
        print("无 DEAD_LETTER 消息")
        db.close()
        return 0

    for r in rows:
        print(f"{r['sent_at']} {r['group_id'][:14]}… {r['sender_role']}"
              f" attempts={r['attempts']} err={r['last_error']}"
              f"\n  {r['message_id']}\n  {(r['content'] or '')[:80]}")

    if not args.execute:
        print(f"\n[dry-run] 共 {len(rows)} 条死信，加 --execute 重置为 RECEIVED 交工作器重跑")
        db.close()
        return 0

    for r in rows:
        db.inbox_set_status(
            r["message_id"], "RECEIVED", attempts=0,
            last_error=None, next_attempt_at=None,
        )
    db.connect().commit()
    print(f"\n[done] 已重置 {len(rows)} 条死信为 RECEIVED（工作器将按群串行重跑）")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
