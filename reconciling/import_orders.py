"""淘宝对账数据导入（与「淘宝订单自动下载与地址对账」工具衔接）。

把工具产出的 `订单地址明细.xlsx`（order_id → 收货地址/订单状态/物流单号）
导入系统 `taobao_orders` 表，供报修工单在提交订单号时校验 + 快递确认增强。

操作化（可被定时任务调用）：
- ``python -m reconciling.import_orders`` —— 无需参数，使用 config 默认路径导入
- ``from reconciling.import_orders import run_import; run_import()`` —— 程序化调用
- 路径可用环境变量 ``TAOBAO_ORDER_DETAIL_XLSX`` / ``TAOBAO_PENDING_XLSX`` 覆盖
- 幂等：按 order_id upsert，重复导入不产生重复；源文件缺失时跳过并记录
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import openpyxl

from db import Database
from logger import get_logger

logger = get_logger(__name__)


def _load_sheet_rows(path: Path) -> list[dict[str, Any]]:
    """读取 xlsx 首个工作表 → 字典行（表头为准）。"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    data: list[dict[str, Any]] = []
    for row in rows[1:]:
        if row is None or all(c is None for c in row):
            continue
        data.append({headers[i]: row[i] for i in range(min(len(headers), len(row)))})
    return data


def import_orders(db: Database, xlsx_path: Path, pending_path: Path | None = None) -> dict[str, int]:
    """把对账表内容 upsert 进 taobao_orders，返回统计 {main, pending, orders}。

    纯逻辑，不初始化数据库、不打印。源文件不存在时对应统计为 0（不抛异常）。
    """
    stats = {"main": 0, "pending": 0, "orders": 0}

    if xlsx_path.exists():
        by_order: dict[str, dict[str, Any]] = {}
        for row in _load_sheet_rows(xlsx_path):
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                continue
            agg = by_order.setdefault(order_id, {
                "products": [], "tracking": None, "address": None, "status": None,
            })
            product = str(row.get("product_name") or "").strip()
            qty = row.get("quantity")
            variant = str(row.get("variant") or "").strip()
            agg["products"].append(f"{product}({variant})×{qty}" if variant and qty else product)
            agg["tracking"] = agg["tracking"] or (str(row.get("tracking_number") or "").strip() or None)
            agg["address"] = agg["address"] or (str(row.get("address") or "").strip() or None)
            agg["status"] = agg["status"] or (str(row.get("status") or "").strip() or None)
        for order_id, agg in by_order.items():
            db.upsert_taobao_order(
                order_id=order_id,
                product_summary="；".join(p for p in agg["products"] if p)[:200] or "",
                tracking_number=agg["tracking"],
                address=agg["address"],
                status=agg["status"],
                source="地址明细",
            )
        stats["main"] = len(by_order)
        stats["orders"] = len(by_order)
    else:
        logger.warning("订单地址明细文件不存在，跳过：%s", xlsx_path)

    if pending_path is not None and pending_path.exists():
        for row in _load_sheet_rows(pending_path):
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                continue
            db.upsert_taobao_order(
                order_id=order_id,
                product_summary=str(row.get("product_name") or "")[:200],
                tracking_number=None,
                address=None,
                status="待人工处理",
                source="待人工处理",
            )
            stats["pending"] += 1
            stats["orders"] += 1
    elif pending_path is not None:
        logger.info("待人工处理文件不存在，跳过：%s", pending_path)

    return stats


def run_import(
    *,
    db_path: Path | str | None = None,
    xlsx_path: Path | str | None = None,
    pending_path: Path | str | None = None,
) -> dict[str, int]:
    """执行一次对账导入（定时任务/程序化入口）。

    未指定路径时使用 config 默认（可用 TAOBAO_* 环境变量覆盖）；
    自动初始化数据库并记录统计日志。返回 {main, pending, orders}。
    """
    from config import DB_PATH, TAOBAO_ORDER_DETAIL_XLSX, TAOBAO_PENDING_XLSX

    db = Database(db_path or DB_PATH)
    db.init_schema()
    try:
        xlsx = Path(xlsx_path or TAOBAO_ORDER_DETAIL_XLSX)
        pending = Path(pending_path or TAOBAO_PENDING_XLSX)
        stats = import_orders(db, xlsx, pending)
        logger.info(
            "对账导入完成 明细=%d 待人工=%d 共=%d source=%s",
            stats["main"], stats["pending"], stats["orders"], xlsx,
        )
        return stats
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入淘宝对账订单表（定时任务可调用）")
    parser.add_argument("--xlsx", type=Path, default=None, help="订单地址明细.xlsx（默认 config 路径）")
    parser.add_argument("--pending", type=Path, default=None, help="待人工处理.xlsx（可选）")
    parser.add_argument("--db", type=Path, default=None, help="数据库路径（默认 config.DB_PATH）")
    args = parser.parse_args(argv)

    from config import LOG_DIR

    from logger import setup_logging

    setup_logging(level="INFO", log_dir=LOG_DIR)
    try:
        stats = run_import(db_path=args.db, xlsx_path=args.xlsx, pending_path=args.pending)
    except Exception as exc:
        logger.error("对账导入失败 err=%s", exc)
        return 1

    print(f"对账导入完成：明细订单 {stats['main']}，待人工处理 {stats['pending']}，共 {stats['orders']} 单")
    return 0


if __name__ == "__main__":
    sys.exit(main())
