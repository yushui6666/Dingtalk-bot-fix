"""同步本地 SQLite 工单到钉钉 AI 表格「报修工单」表（看板数据源）。

用法::

    python scripts/sync_tickets_to_aitable.py --dry-run          # 只预览将同步的记录
    python scripts/sync_tickets_to_aitable.py                    # 增量同步（新建+更新）
    python scripts/sync_tickets_to_aitable.py --full             # 全量 upsert（所有工单）
    python scripts/sync_tickets_to_aitable.py --db path.db       # 指定本地数据库

依赖: 系统 PATH 中存在 ``dws`` CLI（AI 表格/多维表产品），且已登录（默认 profile）。
核心逻辑复用 workers/aitable_sync.sync_once；本脚本提供手动/定时入口。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import DB_PATH  # noqa: E402
from workers.aitable_sync import sync  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="同步本地工单到 AI 表格「报修工单」")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="本地 SQLite 路径")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写入")
    parser.add_argument("--full", action="store_true", help="全量 upsert 所有工单")
    parser.add_argument("--prune", action="store_true", help="同时删除线上已不存在的工单")
    args = parser.parse_args()

    result = sync(args.db, dry_run=args.dry_run, full=args.full, prune=args.prune)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
