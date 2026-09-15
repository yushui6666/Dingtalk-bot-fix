"""淘宝对账导入测试：解析、幂等、文件缺失降级、run_import 入口。"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from db import Database
from reconciling.import_orders import import_orders, run_import


def _write_xlsx(path: Path, headers: list[str], rows: list[list]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "recon.db")
    d.init_schema()
    yield d
    d.close()


def test_import_orders_from_xlsx(db: Database, tmp_path: Path):
    xlsx = tmp_path / "明细.xlsx"
    _write_xlsx(xlsx, ["order_id", "product_name", "quantity", "tracking_number", "address", "status"], [
        ["O1", "合页", 2, "T001", "上海 黄浦", "卖家已发货"],
        ["O2", "门锁", 1, None, "北京 朝阳", "等待买家付款"],
    ])
    stats = import_orders(db, xlsx, None)
    assert stats == {"main": 2, "pending": 0, "orders": 2}
    o1 = db.get_taobao_order("O1")
    assert o1["status"] == "卖家已发货"
    assert o1["tracking_number"] == "T001"
    assert o1["address"] == "上海 黄浦"
    assert db.get_taobao_order("O2")["address"] == "北京 朝阳"


def test_import_pending_marks_manual(db: Database, tmp_path: Path):
    xlsx = tmp_path / "明细.xlsx"
    pending = tmp_path / "待人工.xlsx"
    _write_xlsx(xlsx, ["order_id", "product_name", "address"], [["O1", "合页", "上海 黄浦"]])
    _write_xlsx(pending, ["order_id", "product_name"], [["O2", "门锁"], ["O3", "灯"]])
    stats = import_orders(db, xlsx, pending)
    assert stats["main"] == 1 and stats["pending"] == 2
    assert db.get_taobao_order("O2")["status"] == "待人工处理"
    assert db.get_taobao_order("O3")["address"] is None


def test_import_missing_files_graceful(db: Database, tmp_path: Path):
    missing = tmp_path / "不存在.xlsx"
    stats = import_orders(db, missing, None)
    assert stats == {"main": 0, "pending": 0, "orders": 0}


def test_import_idempotent(db: Database, tmp_path: Path):
    xlsx = tmp_path / "明细.xlsx"
    _write_xlsx(xlsx, ["order_id", "product_name", "status"], [["O1", "合页", "卖家已发货"]])
    import_orders(db, xlsx, None)
    import_orders(db, xlsx, None)  # 重复导入
    count = db.connect().execute("SELECT COUNT(*) FROM taobao_orders").fetchone()[0]
    assert count == 1


def test_run_import_uses_config_defaults(tmp_path: Path, monkeypatch):
    """run_import 无参数时走 config 路径；文件缺失也正常返回统计。"""
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "r.db")
    monkeypatch.setattr(config, "TAOBAO_ORDER_DETAIL_XLSX", tmp_path / "明细.xlsx")
    monkeypatch.setattr(config, "TAOBAO_PENDING_XLSX", tmp_path / "待人工.xlsx")
    _write_xlsx(tmp_path / "明细.xlsx", ["order_id", "product_name"], [["O1", "合页"]])
    stats = run_import()
    assert stats["orders"] == 1
