"""图片附件存储层测试（v4.1 Task 4A）。

覆盖：魔数 MIME 检测、归档原子写入/去重/路径安全、URL 来源校验、
归档编排（下载→检测→落库）、DB 元数据与工单归属回填、事件结构化提取。
"""

from datetime import datetime
from pathlib import Path

import httpx
import pytest

from db import Database
from event_normalizer import normalize_event
from images.archive import (
    AttachmentArchiver,
    ImageArchiveStore,
    _is_safe_https_url,
    detect_image_mime,
)
from models import ImageAttachment, NormalizedMessage, ROLE_MANAGER

# 最小合法图片字节（以 JPEG 魔数开头即可通过检测）
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
UNKNOWN_BYTES = b"not-an-image"

NOW = datetime(2026, 8, 14, 10, 0, 0)


def _make_message(message_id: str = "msg-img-001", attachments=()):
    return NormalizedMessage(
        message_id=message_id,
        group_id="g1",
        sender_id="oid-1",
        sender_name="店长A",
        content="[图片]",
        message_type="image",
        sent_at=NOW,
        sender_role=ROLE_MANAGER,
        attachments=list(attachments),
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture()
def store(tmp_path: Path) -> ImageArchiveStore:
    return ImageArchiveStore(root=tmp_path / "archives")


# ─────────────────────── 1. MIME 检测 ───────────────────────

def test_detect_image_mime_common_types():
    assert detect_image_mime(JPEG_BYTES) == "image/jpeg"
    assert detect_image_mime(PNG_BYTES) == "image/png"
    assert detect_image_mime(UNKNOWN_BYTES) is None
    assert detect_image_mime(b"") is None


# ─────────────────────── 2. 归档存储 ───────────────────────

def test_store_save_returns_relative_path_and_persists(store, tmp_path):
    saved = store.save(
        message_id="msg/../../evil",
        attachment_index=0,
        data=JPEG_BYTES,
        mime_type="image/jpeg",
        sent_at=NOW,
    )
    assert saved.relative_path.startswith("attachments/2026/08/14/")
    assert ".." not in saved.relative_path
    assert saved.mime_type == "image/jpeg"
    assert saved.sha256
    stored_file = tmp_path / "archives" / saved.relative_path
    assert stored_file.read_bytes() == JPEG_BYTES


def test_store_dedup_same_message_index(store):
    first = store.save(
        message_id="msg-1", attachment_index=0, data=JPEG_BYTES,
        mime_type="image/jpeg", sent_at=NOW,
    )
    second = store.save(
        message_id="msg-1", attachment_index=0, data=JPEG_BYTES,
        mime_type="image/jpeg", sent_at=NOW,
    )
    assert second.relative_path == first.relative_path
    assert second.sha256 == first.sha256


def test_store_resolve_relative_path_rejects_escape(store, tmp_path):
    assert store.resolve_relative_path("attachments/2026/08/14/a/0-x.jpg").exists() is False
    for bad in ("/etc/passwd", "../secret", "a/../../secret"):
        with pytest.raises(ValueError):
            store.resolve_relative_path(bad)


# ─────────────────────── 3. 来源 URL 校验 ───────────────────────

def test_url_validator_https_public_only():
    assert _is_safe_https_url("https://media.example.com/img/token.jpg")
    assert not _is_safe_https_url("http://media.example.com/img.jpg")
    assert not _is_safe_https_url("https://localhost/x")
    assert not _is_safe_https_url("https://127.0.0.1/x")
    assert not _is_safe_https_url("https://192.168.1.1/x")
    assert not _is_safe_https_url("https://10.0.0.1/x")
    assert not _is_safe_https_url("https://user:pass@example.com/x")


# ─────────────────────── 4. 归档编排（下载→检测→落库） ───────────────────────

def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://media.example.com")


@pytest.mark.asyncio
async def test_archiver_downloads_and_archives(db: Database, store):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG_BYTES)

    db.enqueue_message(_make_message(attachments=[ImageAttachment(0, "remote_url", "https://media.example.com/img.jpg")]))
    archiver = AttachmentArchiver(
        db=db, store=store, client=_mock_client(handler),
        url_validator=lambda u: u.startswith("https://"),
    )
    try:
        n = await archiver.archive_message("msg-img-001")
    finally:
        await archiver.aclose()
    assert n == 1
    rows = db.list_attachment_rows("msg-img-001")
    assert len(rows) == 1
    row = rows[0]
    assert row["stored_path"] is not None
    assert row["mime_type"] == "image/jpeg"
    assert row["sha256"]
    assert row["analyzed_status"] == "PENDING"
    assert (store.root / row["stored_path"]).read_bytes() == JPEG_BYTES


@pytest.mark.asyncio
async def test_archiver_download_failure_marks_skipped(db: Database, store):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    db.enqueue_message(_make_message(attachments=[ImageAttachment(0, "remote_url", "https://media.example.com/missing.jpg")]))
    archiver = AttachmentArchiver(
        db=db, store=store, client=_mock_client(handler),
        url_validator=lambda u: u.startswith("https://"),
    )
    try:
        n = await archiver.archive_message("msg-img-001")
    finally:
        await archiver.aclose()
    assert n == 0
    row = db.list_attachment_rows("msg-img-001")[0]
    assert row["analyzed_status"] == "SKIPPED"
    assert row["error"]


@pytest.mark.asyncio
async def test_archiver_rejects_non_https_source(db: Database, store):
    db.enqueue_message(_make_message(attachments=[ImageAttachment(0, "remote_url", "http://media.example.com/x.jpg")]))
    archiver = AttachmentArchiver(db=db, store=store, client=_mock_client(lambda r: httpx.Response(200)))
    try:
        n = await archiver.archive_message("msg-img-001")
    finally:
        await archiver.aclose()
    assert n == 0
    row = db.list_attachment_rows("msg-img-001")[0]
    assert row["analyzed_status"] == "SKIPPED"


@pytest.mark.asyncio
async def test_archiver_rejects_local_path_in_production(db: Database, store, tmp_path):
    src = tmp_path / "local.jpg"
    src.write_bytes(JPEG_BYTES)
    db.enqueue_message(_make_message(attachments=[ImageAttachment(0, "local_path", str(src))]))
    archiver = AttachmentArchiver(db=db, store=store, allow_local=False)
    try:
        n = await archiver.archive_message("msg-img-001")
    finally:
        await archiver.aclose()
    assert n == 0
    row = db.list_attachment_rows("msg-img-001")[0]
    assert row["analyzed_status"] == "SKIPPED"
    assert "本地路径" in row["error"]


@pytest.mark.asyncio
async def test_archiver_data_url_ok(db: Database, store):
    import base64

    data_url = "data:image/jpeg;base64," + base64.b64encode(JPEG_BYTES).decode()
    db.enqueue_message(_make_message(attachments=[ImageAttachment(0, "data_url", data_url)]))
    archiver = AttachmentArchiver(db=db, store=store)
    try:
        n = await archiver.archive_message("msg-img-001")
    finally:
        await archiver.aclose()
    assert n == 1
    assert db.list_attachment_rows("msg-img-001")[0]["stored_path"]


# ─────────────────────── 5. DB 元数据与归属回填 ───────────────────────

def test_enqueue_writes_attachment_metadata(db: Database):
    db.enqueue_message(_make_message(attachments=[ImageAttachment(0, "remote_url", "https://m/x.jpg")]))
    rows = db.list_attachment_rows("msg-img-001")
    assert len(rows) == 1
    assert rows[0]["source_type"] == "remote_url"
    assert rows[0]["source_ref"] == "https://m/x.jpg"
    assert rows[0]["ticket_id"] is None


def test_link_message_backfills_attachment_ticket(db: Database):
    db.enqueue_message(_make_message(attachments=[ImageAttachment(0, "remote_url", "https://m/x.jpg")]))
    db.link_message("msg-img-001", 42, "CREATE")
    rows = db.list_attachment_rows("msg-img-001")
    assert rows[0]["ticket_id"] == 42
    # 二次归属不改已回填值
    db.link_message("msg-img-001", 99, "EXECUTED")
    assert db.list_attachment_rows("msg-img-001")[0]["ticket_id"] == 42


# ─────────────────────── 6. 事件结构化提取 ───────────────────────

def _image_event(content=None, **extra):
    evt = {
        "type": "user_im_message_receive_group",
        "message_id": "msg-e-001",
        "conversation_id": "cid-g-001",
        "sender": "店长A",
        "sender_open_dingtalk_id": "oid-mgr",
        "content": content if content is not None else {"download_url": "https://media.example.com/a.jpg", "file_name": "fault.jpg"},
        "msg_type": "image",
        "create_time": "2026-08-14 10:00:00",
    }
    evt.update(extra)
    return evt


def test_image_event_extracts_structured_attachment():
    msg = normalize_event(_image_event(), group=None, id_map=None)
    assert msg is not None
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.source_type == "remote_url"
    assert att.source_ref == "https://media.example.com/a.jpg"
    assert att.file_name == "fault.jpg"
    assert att.attachment_index == 0


def test_image_event_with_list_content():
    evt = _image_event(content=[
        {"media_id": "media-1", "file_name": "a.jpg"},
        {"download_url": "https://media.example.com/b.jpg"},
    ])
    msg = normalize_event(evt, group=None, id_map=None)
    assert msg is not None
    assert [(a.source_type, a.source_ref) for a in msg.attachments] == [
        ("dingtalk_media", "media-1"),
        ("remote_url", "https://media.example.com/b.jpg"),
    ]


def test_plain_text_content_has_no_attachment():
    evt = _image_event(content="[图片]")
    msg = normalize_event(evt, group=None, id_map=None)
    assert msg is not None
    assert msg.content == "[图片]"
    assert msg.attachments == []


def test_rich_markdown_has_no_attachment():
    import json

    evt = _image_event(content=json.dumps({"markdown": "**门体照片**见附件"}), msg_type="rich")
    msg = normalize_event(evt, group=None, id_map=None)
    assert msg is not None
    assert msg.content == "**门体照片**见附件"
    assert msg.attachments == []
