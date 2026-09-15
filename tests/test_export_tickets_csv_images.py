"""CSV 导出图片跟随测试（图片文件列 + ticket_images/ 复制 + 正文路径）。

覆盖：
- 「图片文件」列输出相对路径，图片被复制到 out_dir/ticket_images/<工单命名>/
- 页面正文「图片解析」行附相对路径；有文件无解析文本也列行
- 无图工单填「无」；重复导出幂等（不重复复制）
- 归档源丢失时打印警告跳过，不中断导出
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from db import Database
from images.archive import ImageArchiveStore

# 最小合法 PNG 魔数 + 填充（存储层 save() 不校验内容，仅走路径/MIME 映射）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


def _insert_ticket(conn: sqlite3.Connection, ticket_no: str) -> int:
    cur = conn.execute(
        "INSERT INTO tickets (ticket_no, group_id, store_name, reporter_id, subject,"
        " location, problem_description, sla_days, status, created_at)"
        " VALUES (?, 'g1', '测试店', 'oid-1', '主题', '位置', '描述', 1, 'COMPLETED', ?)",
        (ticket_no, "2026-08-20 10:00:00"),
    )
    conn.commit()
    return cur.lastrowid


def _store_image(store: ImageArchiveStore, message_id: str) -> str:
    saved = store.save(
        message_id=message_id,
        attachment_index=0,
        data=PNG_BYTES,
        mime_type="image/png",
        sent_at=datetime(2026, 8, 20, 10, 0, 0),
    )
    return saved.relative_path


def _insert_attachment(
    conn: sqlite3.Connection,
    ticket_id: int,
    stored_path: str,
    *,
    vision: str | None = None,
    sha256: str = "deadbeef",
) -> None:
    conn.execute(
        "INSERT INTO message_attachments (message_id, attachment_index, ticket_id,"
        " source_type, source_ref, stored_path, sha256, byte_size, mime_type,"
        " analyzed_status, vision_result_json, created_at)"
        " VALUES ('msg-1', 0, ?, 'dingtalk_media', '$abc', ?, ?, 40, 'image/png',"
        " 'ANALYZED', ?, '2026-08-20 10:00:00')",
        (ticket_id, stored_path, sha256, vision),
    )
    conn.commit()


def _read_rows(csv_path: Path) -> tuple[list[str], list[list[str]]]:
    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _run_export(monkeypatch, db: Database, store: ImageArchiveStore, out_dir: Path) -> Path:
    from scripts import export_tickets_csv as mod

    monkeypatch.setattr(mod, "ImageArchiveStore", lambda: store)
    return mod.export_tickets_csv(db=db, out_dir=out_dir)


def test_csv_exports_images_and_references_them(monkeypatch, db, tmp_path):
    conn = db.connect()
    store = ImageArchiveStore(root=tmp_path / "archives")
    rel = _store_image(store, "msg-img-1")
    tid = _insert_ticket(conn, "店A-工单1")
    _insert_attachment(conn, tid, rel, vision="风机积灰，已清理")
    _insert_ticket(conn, "店A-工单2")  # 无图工单

    out_dir = tmp_path / "out"
    out_path = _run_export(monkeypatch, db, store, out_dir)

    header, rows = _read_rows(out_path)
    assert header.index("图片文件") == header.index("关闭角色") + 1
    assert header[-1] == "页面正文"
    by_no = {r[0]: r for r in rows}

    # 有图工单：新列填相对路径，文件真实复制到位
    expected_rel = f"ticket_images/店A-工单1/{Path(rel).name}"
    assert by_no["店A-工单1"][header.index("图片文件")] == expected_rel
    copied = out_dir / expected_rel
    assert copied.exists() and copied.read_bytes() == PNG_BYTES

    # 正文「图片解析」附同一路径
    page = by_no["店A-工单1"][header.index("页面正文")]
    assert "- 风机积灰，已清理（图片：ticket_images/店A-工单1/" in page

    # 无图工单填「无」，且不产生其目录
    assert by_no["店A-工单2"][header.index("图片文件")] == "无"
    assert not (out_dir / "ticket_images" / "店A-工单2").exists()


def test_reexport_is_idempotent(monkeypatch, db, tmp_path):
    conn = db.connect()
    store = ImageArchiveStore(root=tmp_path / "archives")
    rel = _store_image(store, "msg-img-1")
    tid = _insert_ticket(conn, "店A-工单1")
    _insert_attachment(conn, tid, rel)

    out_dir = tmp_path / "out"
    _run_export(monkeypatch, db, store, out_dir)
    dest = out_dir / "ticket_images" / "店A-工单1" / Path(rel).name
    first_mtime = dest.stat().st_mtime_ns
    first_size = dest.stat().st_size

    _, rows = _read_rows(_run_export(monkeypatch, db, store, out_dir))
    assert len(rows) == 1
    assert dest.stat().st_mtime_ns == first_mtime  # 未重写
    assert dest.stat().st_size == first_size


def test_missing_source_skips_with_warning(monkeypatch, db, tmp_path, capsys):
    conn = db.connect()
    store = ImageArchiveStore(root=tmp_path / "archives")
    tid = _insert_ticket(conn, "店A-工单3")
    _insert_attachment(conn, tid, "attachments/2099/01/01/msgX/9-deadbeef.png")

    out_dir = tmp_path / "out"
    out_path = _run_export(monkeypatch, db, store, out_dir)

    captured = capsys.readouterr()
    assert "⚠️ 图片导出跳过" in captured.out

    header, rows = _read_rows(out_path)
    assert rows[0][header.index("图片文件")] == "无"
