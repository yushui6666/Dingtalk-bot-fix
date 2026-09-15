"""从门店群数据 CSV 导入群配置到 data/groups.json。

CSV 列（门店群数据汇总_v2.csv）：
    店名store_name, 群conversationId, 店长姓名, 店长userId, 店长openDingtalkId,
    区域负责人姓名, 区域负责人userId, 区域负责人openDingtalkId,
    总工程师姓名, 总工程师userId, 总工程师openDingtalkId,
    工程师姓名, 工程师userId, 工程师openDingtalkId

工程师/其 ID 可能以分号分隔多个（如 "工程师B;工程师A"）。

映射到 groups.json:
- 群: group_id=群conversationId, store_name=店名
       manager_ids=[店长userId], engineer_ids=[工程师userId...]
       engineering_leader_id=总工程师userId, regional_manager_id=区域负责人userId
- user_id_map: openDingtalkId→userId（店长/区域负责人/总工程师/工程师全收录）

用法::

    python scripts/import_store_groups.py <门店群数据汇总.csv> [--merge]

--merge 时保留 groups.json 中现有群（测试群等）；默认覆盖生成。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import GROUPS_CONFIG_PATH  # noqa: E402


def _split(value: str | None) -> list[str]:
    """按分号拆分多值字段，去空白、去空项。"""
    if not value:
        return []
    return [v.strip() for v in value.split(";") if v.strip()]


def import_csv(csv_path: Path) -> tuple[list[dict], dict[str, str]]:
    """解析 CSV → (groups, user_id_map)。"""
    groups: list[dict] = []
    user_id_map: dict[str, str] = {}

    with open(csv_path, encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            group_id = (row.get("群conversationId") or "").strip()
            store_name = (row.get("店名store_name") or "").strip()
            if not group_id or not store_name:
                continue

            manager_ids = _split(row.get("店长userId"))
            engineer_ids = _split(row.get("工程师userId"))
            leader_id = (row.get("总工程师userId") or "").strip()
            regional_id = (row.get("区域负责人userId") or "").strip()

            groups.append({
                "group_id": group_id,
                "store_name": store_name,
                "manager_ids": manager_ids,
                "engineer_ids": engineer_ids,
                "other_member_ids": [],
                "engineering_leader_id": leader_id,
                "regional_manager_id": regional_id,
                "is_active": True,
            })

            # user_id_map: openDingtalkId → userId（各角色，工程师多值 zip）
            mapping_rows = [
                ("店长openDingtalkId", "店长userId"),
                ("区域负责人openDingtalkId", "区域负责人userId"),
                ("总工程师openDingtalkId", "总工程师userId"),
                ("工程师openDingtalkId", "工程师userId"),
            ]
            for oid_col, uid_col in mapping_rows:
                for oid, uid in zip(_split(row.get(oid_col)), _split(row.get(uid_col))):
                    if oid and uid:
                        user_id_map[oid] = uid

    return groups, user_id_map


def main() -> None:
    parser = argparse.ArgumentParser(description="导入门店群 CSV 到 groups.json")
    parser.add_argument("csv", type=Path, help="门店群数据汇总 CSV 路径")
    parser.add_argument("--merge", action="store_true",
                        help="保留现有群（测试群等），追加门店群")
    args = parser.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"CSV 不存在: {args.csv}")

    groups, user_id_map = import_csv(args.csv)

    existing = {"groups": [], "user_id_map": {}}
    if args.merge and GROUPS_CONFIG_PATH.exists():
        existing = json.loads(GROUPS_CONFIG_PATH.read_text(encoding="utf-8"))

    if args.merge:
        known_ids = {g["group_id"] for g in existing["groups"]}
        new_groups = [g for g in groups if g["group_id"] not in known_ids]
        groups = existing["groups"] + new_groups
        user_id_map = {**existing["user_id_map"], **user_id_map}

    output = {
        "groups": groups,
        "user_id_map": user_id_map,
    }
    GROUPS_CONFIG_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {GROUPS_CONFIG_PATH}")
    print(f"  群总数: {len(groups)}（本次导入 {len(groups) - len(existing['groups'])}）")
    print(f"  user_id_map: {len(user_id_map)} 条映射")


if __name__ == "__main__":
    main()
