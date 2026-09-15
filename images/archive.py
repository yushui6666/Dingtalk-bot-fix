"""图片附件安全归档（计划书 §10.6、Task 4A · 存储层）。

消息到达时只归档图片、不调用视觉模型；工单结束后由分析层统一消费
（用户决策 2026-08-14）。

设计要点：
- 归档顺序固定：下载/读取 → 检测真实 MIME 与大小 → SHA-256 → fsync →
  原子重命名 → 回填 DB 记录。
- 仅允许受信任来源：HTTPS 临时 URL / 测试 data URL；钉钉媒体 ID 的真实
  下载接口待事件字段确认后通过 DingTalkMediaResolver 适配，当前 SKIP。
- 生产模式拒绝本地路径、内网/回环/链路本地地址、未知 MIME、超限图片、
  重定向下载（一律不跟随）。
- 持久化文件名只由消息 ID 清洗值、附件序号、摘要前缀和真实扩展名生成，
  不使用用户原始文件名。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from config import (
    IMAGE_ALLOW_LOCAL_SOURCES,
    IMAGE_ALLOWED_MIME_TYPES,
    IMAGE_ARCHIVE_DIR,
    IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
    IMAGE_MAX_BYTES,
    IMAGE_MAX_COUNT_PER_MESSAGE,
)
from db import Database
from logger import get_logger

logger = get_logger(__name__)

# MIME → 扩展名（只从检测后的 MIME 映射，不信任原始文件名）
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

# 真实类型检测的魔数前缀（Python 3.13+ 已移除 imghdr，PIL 未引入，手写最小集）
_MAGIC_CHECKS = (
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/gif", b"GIF87a"),
    ("image/gif", b"GIF89a"),
    ("image/webp", b"RIFF"),
    ("image/bmp", b"BM"),
)


def detect_image_mime(data: bytes) -> str | None:
    """按魔数检测图片真实 MIME；无法识别返回 None。"""
    if not data:
        return None
    for mime, magic in _MAGIC_CHECKS:
        if data.startswith(magic):
            # webp 还需校验 RIFF 后 4 字节为 WEBP
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return None


def _safe_path_component(value: str) -> str:
    """消息 ID 清洗为安全的路径分量（去掉路径分隔符、点与 ../ 风险）。"""
    cleaned = re.sub(r"[^A-Za-z0-9=_+@-]", "_", value).strip("_-")[:80]
    return cleaned or "msg"


@dataclass(frozen=True)
class StoredImage:
    relative_path: str
    mime_type: str
    byte_size: int
    sha256: str


class ImageArchiveStore:
    """图片本地归档：原子写入 + 路径安全 + 同 (message_id, index) 去重。"""

    def __init__(self, root: Path | str = IMAGE_ARCHIVE_DIR) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def save(
        self,
        *,
        message_id: str,
        attachment_index: int,
        data: bytes,
        mime_type: str,
        sent_at: datetime,
    ) -> StoredImage:
        digest = hashlib.sha256(data).hexdigest()
        ext = _MIME_EXT.get(mime_type, ".bin")
        safe_id = _safe_path_component(message_id)
        subdir = sent_at.strftime("attachments/%Y/%m/%d")
        folder = self._root / subdir / safe_id
        folder.mkdir(parents=True, exist_ok=True)

        target = folder / f"{attachment_index}-{digest[:8]}{ext}"
        if not target.exists():
            tmp = folder / f".{attachment_index}-{digest[:8]}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)

        relative = Path(subdir) / safe_id / target.name
        return StoredImage(relative.as_posix(), mime_type, len(data), digest)

    def resolve_relative_path(self, relative_path: str) -> Path:
        """相对路径 → 绝对路径；拒绝绝对路径、`..` 与越界（符号链接逃逸兜底）。"""
        p = Path(relative_path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"非法相对路径: {relative_path!r}")
        resolved = (self._root / p).resolve()
        root = self._root.resolve()
        if not str(resolved).startswith(str(root) + os.sep):
            raise ValueError(f"路径越界: {relative_path!r}")
        return resolved


def _host_is_private(host: str) -> bool:
    """字面 IP 检查私网/回环/链路本地/保留地址。域名不做 DNS 解析（见模块注释）。"""
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # 非字面 IP，交给 DNS（SSRF 硬化留待后续）
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def _is_safe_https_url(url: str) -> bool:
    """仅允许受信任 HTTPS 来源，拒绝带凭据、私网/回环/链路本地地址的 URL。"""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname or ""
    if not host:
        return False
    return not _host_is_private(host)


def _decode_data_url(url: str) -> bytes:
    """解析 data:image/...;base64,... → 字节；格式非法抛 ValueError。"""
    if not url.startswith("data:"):
        raise ValueError("非 data URL")
    header, _, payload = url.partition(",")
    if ";base64" not in header:
        raise ValueError("仅支持 base64 编码的 data URL")
    return base64.b64decode(payload, validate=False)


class DingTalkMediaResolver:
    """通过 dws CLI 下载钉钉媒体（mediaId → 字节）。

    调用 `dws chat message download-media`，需要 mediaId + messageId +
    openConversationId。实测：resource-id 必须带 `$` 前缀（content 里的原始值）；
    dws 把文件写到 --output 指定路径，返回 downloadUrl。
    """

    def __init__(
        self,
        *,
        dws_cmd: str | None = None,
        timeout_seconds: float = 60.0,
        tmp_dir: str | Path | None = None,
    ) -> None:
        from config import DWS_CMD

        self._dws_cmd = dws_cmd or DWS_CMD
        self._timeout = timeout_seconds
        self._tmp_dir = Path(tmp_dir) if tmp_dir else None

    async def resolve(self, media_id: str, message_id: str, conversation_id: str) -> bytes:
        import asyncio
        import tempfile

        # 保留完整 mediaId（含 $ 前缀，dws 实测必需）
        media_id = media_id.strip()
        tmp_dir = self._tmp_dir or Path(tempfile.gettempdir())
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out_file = tmp_dir / f"dd_media_{media_id.lstrip('$')[:40]}_{message_id[-8:]}.bin"

        proc = await asyncio.create_subprocess_exec(
            self._dws_cmd,
            "chat", "message", "download-media",
            "--type", "mediaId",
            "--resource-id", media_id,
            "--message-id", message_id,
            "--open-conversation-id", conversation_id,
            "--output", str(out_file),
            "--format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()  # 回收子进程，避免僵尸
            raise ValueError("钉钉媒体下载超时")
        finally:
            # 显式关闭 transport：communicate() 只读到 EOF，不会关闭子进程
            # transport 本身；残留到事件循环关闭后会刷屏 Event loop is closed
            transport = getattr(proc, "_transport", None)
            if transport is not None and not transport.is_closing():
                transport.close()
        if proc.returncode != 0:
            raise ValueError(
                f"dws 下载失败 code={proc.returncode} stderr={stderr.decode('utf-8', 'replace')[:200]}"
            )
        out = stdout.decode("utf-8", "replace").strip()
        if out.startswith("{"):
            try:
                import json as _json

                obj = _json.loads(out)
            except Exception:
                obj = None
            if obj is not None and not obj.get("success"):
                raise ValueError(f"dws 下载失败: {str(obj)[:200]}")
        if not out_file.exists():
            raise ValueError(f"dws 下载后文件不存在 path={out_file}")
        data = out_file.read_bytes()
        out_file.unlink(missing_ok=True)
        return data


class AttachmentArchiver:
    """消息附件的下载与归档编排：失败只记 error，不抛到业务流程。

    client 可注入 httpx.MockTransport（测试用）；url_validator 可注入以
    覆盖默认的 HTTPS/私网校验。
    """

    def __init__(
        self,
        *,
        db: Database,
        store: ImageArchiveStore | None = None,
        client: Any = None,
        media_resolver: Any = None,
        enabled: bool = True,
        max_bytes: int = IMAGE_MAX_BYTES,
        max_count: int = IMAGE_MAX_COUNT_PER_MESSAGE,
        allowed_mime: tuple[str, ...] = IMAGE_ALLOWED_MIME_TYPES,
        allow_local: bool = IMAGE_ALLOW_LOCAL_SOURCES,
        url_validator: Callable[[str], bool] | None = None,
    ) -> None:
        self._db = db
        self._store = store or ImageArchiveStore()
        self._media_resolver = media_resolver or DingTalkMediaResolver()
        self._enabled = enabled
        self._max_bytes = max_bytes
        self._max_count = max_count
        self._allowed_mime = set(allowed_mime)
        self._allow_local = allow_local
        self._url_ok = url_validator or _is_safe_https_url
        if client is None:
            import httpx

            client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
            )
        self._client = client

    async def archive_message(self, message_id: str) -> int:
        """归档某条消息的全部待归档附件，返回成功条数（失败不抛出）。"""
        if not self._enabled:
            return 0
        rows = self._db.list_attachment_rows(message_id)
        if not rows:
            return 0
        if len(rows) > self._max_count:
            logger.warning("附件数量超限 msg=%s count=%d max=%d", message_id, len(rows), self._max_count)
        archived = 0
        for row in rows:
            if row.get("stored_path") or row.get("analyzed_status") == "SKIPPED":
                continue
            try:
                data, mime = await self._resolve_bytes(row)
                sent_at = datetime.now()
                stored = self._store.save(
                    message_id=message_id,
                    attachment_index=row["attachment_index"],
                    data=data,
                    mime_type=mime,
                    sent_at=sent_at,
                )
                self._db.update_attachment_archived(
                    row["id"],
                    stored_path=stored.relative_path,
                    sha256=stored.sha256,
                    byte_size=stored.byte_size,
                    mime_type=stored.mime_type,
                )
                archived += 1
                logger.info(
                    "图片归档完成 msg=%s index=%s path=%s sha=%s mime=%s bytes=%d",
                    message_id, row["attachment_index"], stored.relative_path,
                    stored.sha256[:8], stored.mime_type, stored.byte_size,
                )
            except Exception as exc:
                logger.warning(
                    "图片归档失败 msg=%s index=%s source=%s err=%s",
                    message_id, row["attachment_index"], row["source_type"], exc,
                )
                self._db.mark_attachment_failed(row["id"], str(exc))
        return archived

    async def _resolve_bytes(self, row: dict[str, Any]) -> tuple[bytes, str]:
        source_type = row["source_type"]
        source_ref = row["source_ref"]
        if source_type == "remote_url":
            data = await self._download(source_ref)
        elif source_type == "dingtalk_media":
            data = await self._media_resolver.resolve(
                media_id=source_ref,
                message_id=row["message_id"],
                conversation_id=self._group_id_for_message(row["message_id"]),
            )
        elif source_type == "data_url":
            data = _decode_data_url(source_ref)
        elif source_type == "local_path":
            if not self._allow_local:
                raise ValueError("生产模式拒绝本地路径图片来源")
            data = Path(source_ref).read_bytes()
        else:
            raise ValueError(f"无法识别的附件来源: {source_type}")

        if not data:
            raise ValueError("图片内容为空")
        if len(data) > self._max_bytes:
            raise ValueError(f"图片超限 size={len(data)} max={self._max_bytes}")
        mime = detect_image_mime(data) or row.get("declared_mime_type")
        if mime not in self._allowed_mime:
            raise ValueError(f"图片类型不允许 mime={mime}")
        return data, mime

    def _group_id_for_message(self, message_id: str) -> str:
        """由附件行的消息查群 ID（用于 dws 下载）；查不到返回空。"""
        try:
            row = self._db.get_inbox_message(message_id)
            if row and row.get("group_id"):
                return row["group_id"]
        except Exception:
            pass
        return ""

    async def _download(self, url: str) -> bytes:
        if not self._url_ok(url):
            raise ValueError("仅允许受信任 HTTPS 来源（拒绝私网/回环/链路本地）")
        response = await self._client.get(url)
        if response.status_code != 200:
            raise ValueError(f"下载失败 status={response.status_code} url={url[:80]}")
        return response.content

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:
                logger.warning("归档客户端关闭异常 err=%s", exc)
