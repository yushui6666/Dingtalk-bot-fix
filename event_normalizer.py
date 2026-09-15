"""事件标准化：dws NDJSON 事件 → NormalizedMessage。

- 缺失 message_id / group_id / sender_id / sent_at 的事件直接丢弃（WARNING 日志）。
- 系统账号（工程部AI）回流消息标记 is_self，业务层忽略。
- sender_id 统一取 sender_open_dingtalk_id（Phase 0 实测字段）。
- create_time 为本地时区字符串（如 "2026-08-11 10:36:01"），解析为 datetime。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from config import LISTENER_USER_ID
from logger import get_logger
from models import (
    MSG_FILE,
    MSG_IMAGE,
    MSG_RICH,
    MSG_TEXT,
    ImageAttachment,
    NormalizedMessage,
)
from role_resolver import resolve_role

logger = get_logger(__name__)

# 媒体消息类型映射（content 为可读描述；真实字段待样本验证，Phase 1 待办）
_KNOWN_MEDIA_TYPES = {"image", "file", "audio", "video", "rich"}

# 富文本可读字段优先级（dws 富文本常见结构；待真实样本确认）
_RICH_TEXT_KEYS = ("markdown", "text", "content")

_REQUIRED_FIELDS = ("message_id", "conversation_id", "sender_open_dingtalk_id")

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_sent_at(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # 毫秒时间戳（event_time），转 datetime
        try:
            return datetime.fromtimestamp(raw / 1000)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(raw).strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    logger.warning("sent_at 解析失败 value=%r", raw)
    return None


# 图片消息 content 渲染格式（dws 实测）：[图片消息](mediaId=$xxx) 注意：如需下载使用dws...
_IMAGE_MSG_RE = re.compile(r"\[图片消息\]\(\s*mediaId=([^)\s]+)\s*\)")

# 文件消息 content 渲染格式（防御性，待真实样本验证）
_FILE_MSG_RE = re.compile(r"\[文件消息\]\(\s*[^)]*\s*\)")


def _detect_message_type(event: dict[str, Any]) -> str:
    """按事件可选字段判断消息类型；缺省 text。

    实测：dws 图片消息的事件不带 msg_type 字段，content 渲染为
    `[图片消息](mediaId=$...)`。此处先探测显式字段，再回退到
    content 文本模式匹配。
    """
    for key in ("msg_type", "msgtype", "type"):
        v = event.get(key)
        if isinstance(v, str) and v in _KNOWN_MEDIA_TYPES:
            return v
    content = event.get("content")
    if isinstance(content, str):
        if _IMAGE_MSG_RE.search(content):
            return MSG_IMAGE
        if _FILE_MSG_RE.search(content):
            return MSG_FILE
    return MSG_TEXT


def _extract_rich_text(event: dict[str, Any]) -> str:
    """富文本事件的可读文本抽取。

    content 可能为 JSON 字符串（含 markdown/text 字段）或纯文本。
    真实结构待样本验证；当前做宽松兼容，无法解析时返回原值。
    """
    content = event.get("content")
    if not content:
        return ""
    if isinstance(content, dict):
        for key in _RICH_TEXT_KEYS:
            v = content.get(key)
            if isinstance(v, str):
                return v
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                return s
            if isinstance(obj, dict):
                for key in _RICH_TEXT_KEYS:
                    v = obj.get(key)
                    if isinstance(v, str):
                        return v
                return json.dumps(obj, ensure_ascii=False)
        return s
    return str(content)


def _extract_reply_to(raw_event: dict[str, Any]) -> Optional[str]:
    """v4.0：从原始事件提取钉钉回复/引用目标消息 ID。

    TODO(Phase 1 待验证)：以下候选字段需用真实 NDJSON 样本确认。
    dws listen-im 的引用消息常见字段包括 reply_to_message_id、
    original_message_id、quote_message_id 或 refer_message_id。
    当前按优先顺序试探，后续以真实样本为准。
    """
    for key in ("reply_to_message_id", "original_message_id", "quote_message_id", "refer_message_id"):
        v = raw_event.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ─────────────────────── 图片附件提取（v4.1 Task 4A） ───────────────────────
# 附件来源候选键（真实 dws 字段待样本验证，防御性探试）
_ATTACHMENT_SOURCE_KEYS = (
    "download_url", "url", "media_id", "picMediaId", "pictureUrl",
    "photo_url", "image_url", "fileId",
)
# 结构化 content 中携带附件数组的常见键
_ATTACHMENT_LIST_KEYS = ("attachments", "images", "files", "imageList", "mediaList")
# 附件对象里的名称/类型候选键
_ATTACHMENT_NAME_KEYS = ("file_name", "fileName", "name", "title")
_ATTACHMENT_MIME_KEYS = ("mime_type", "mimeType", "content_type", "file_type", "fileType")


def _as_json_dict(value: Any) -> Optional[dict[str, Any]]:
    """content 可能是 dict 或 JSON 字符串，统一转 dict；非结构化返回 None。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                return None
            return obj if isinstance(obj, dict) else None
    return None


def _source_from_item(item: dict[str, Any], raw_event: dict[str, Any]) -> tuple[str, str]:
    """从附件对象定位下载源，返回 (source_ref, source_type)。"""
    for key in _ATTACHMENT_SOURCE_KEYS:
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            ref = v.strip()
            if key in ("media_id", "picMediaId", "fileId"):
                return ref, "dingtalk_media"
            if ref.startswith("http://") or ref.startswith("https://"):
                return ref, "remote_url"
            return ref, "unknown"
    for key in _ATTACHMENT_SOURCE_KEYS:
        v = raw_event.get(key)
        if isinstance(v, str) and v.strip():
            ref = v.strip()
            if key in ("media_id", "picMediaId", "fileId"):
                return ref, "dingtalk_media"
            if ref.startswith(("http://", "https://")):
                return ref, "remote_url"
            return ref, "unknown"
    return "", ""


def _extract_attachments(raw_event: dict[str, Any], message_type: str) -> list[ImageAttachment]:
    """从图片/文件/富文本事件提取结构化附件（真实字段待样本验证，宽松兼容）。

    - 结构化 content（dict/list/JSON 字符串）→ 逐项定位下载源；
    - 纯文本 content（如 "[图片]"）→ 视为无附件，不产生记录；
    - 富文本只在显式附件数组/来源键出现时才产生附件。
    """
    if message_type not in (MSG_IMAGE, MSG_FILE, MSG_RICH):
        return []
    content = raw_event.get("content")
    # 文本渲染模式：dws 实测 content 形如 `[图片消息](mediaId=$xxx) 注意：...`
    if isinstance(content, str):
        img = _IMAGE_MSG_RE.search(content)
        if img:
            media_id = img.group(1).strip()
            return [
                ImageAttachment(
                    attachment_index=0,
                    source_type="dingtalk_media",
                    source_ref=media_id,
                )
            ]
        if _FILE_MSG_RE.search(content):
            # 文件消息：暂无法从渲染文本定位下载源，记录原文待核对
            return [
                ImageAttachment(
                    attachment_index=0,
                    source_type="unknown",
                    source_ref=content[:200],
                    file_name=None,
                )
            ]
        if "图片" in content or "图片消息" in content:
            return []
    candidates: list[Any] = []
    structured = False
    if isinstance(content, list):
        candidates = [c for c in content if isinstance(c, dict)]
        structured = True
    else:
        obj = _as_json_dict(content)
        if obj is not None:
            structured = True
            for key in _ATTACHMENT_LIST_KEYS:
                v = obj.get(key)
                if isinstance(v, list):
                    candidates = [c for c in v if isinstance(c, dict)]
                    break
            else:
                candidates = [obj]
    # 顶层事件字段直接携带来源（content 非结构化但事件有附件字段）
    if not candidates and not structured:
        for key in _ATTACHMENT_SOURCE_KEYS:
            if isinstance(raw_event.get(key), str) and raw_event.get(key, "").strip():
                candidates = [dict(raw_event)]
                break

    attachments: list[ImageAttachment] = []
    seen: set[str] = set()
    for item in candidates:
        ref, source_type = _source_from_item(item, raw_event)
        if not ref:
            # 结构化但找不到来源：图片/文件保留原文待核对（宁可记录不丢数据）；
            # 富文本只认显式附件来源，纯 markdown 不产生附件记录。
            if structured and message_type in (MSG_IMAGE, MSG_FILE):
                ref = json.dumps(item, ensure_ascii=False)
                source_type = "unknown"
            else:
                continue
        if ref in seen:
            continue
        seen.add(ref)
        file_name = next(
            (str(v) for k in _ATTACHMENT_NAME_KEYS
             if isinstance((v := item.get(k)), str) and v.strip()),
            None,
        )
        mime = next(
            (str(v) for k in _ATTACHMENT_MIME_KEYS
             if isinstance((v := item.get(k)), str) and v.strip()),
            None,
        )
        attachments.append(
            ImageAttachment(
                attachment_index=len(attachments),
                source_type=source_type,
                source_ref=ref,
                file_name=file_name,
                declared_mime_type=mime,
            )
        )
    return attachments


def normalize_event(
    raw_event: dict[str, Any],
    group: Optional[dict] = None,
    id_map: Optional[dict[str, str]] = None,
) -> Optional[NormalizedMessage]:
    """原始事件 → NormalizedMessage；无法标准化返回 None（已打日志）。

    参数：
        raw_event: dws event NDJSON 的一行（已 json.loads）。
        group: 群配置（用于角色解析），可为 None。
        id_map: openDingtalkId → userId 映射（角色识别用）。
    """
    for field in _REQUIRED_FIELDS:
        if not raw_event.get(field):
            logger.warning(
                "事件标准化失败：缺少必填字段 field=%s event_id=%s",
                field, raw_event.get("event_id"),
            )
            return None

    message_id = raw_event["message_id"]
    group_id = raw_event["conversation_id"]
    sender_id = raw_event["sender_open_dingtalk_id"]
    sender_name = raw_event.get("sender") or sender_id
    message_type = _detect_message_type(raw_event)
    if message_type == MSG_RICH:
        content = _extract_rich_text(raw_event)
    elif message_type in (MSG_IMAGE, MSG_FILE):
        content = raw_event.get("content") or f"[{message_type}]"
    else:
        content = raw_event.get("content") or ""
    sent_at = _parse_sent_at(raw_event.get("create_time") or raw_event.get("event_time"))
    reply_to_message_id = _extract_reply_to(raw_event)
    attachments = _extract_attachments(raw_event, message_type)

    if sent_at is None:
        logger.warning(
            "事件标准化失败：sent_at 不可用 message_id=%s", message_id,
        )
        return None

    is_self = sender_id == LISTENER_USER_ID

    msg = NormalizedMessage(
        message_id=message_id,
        group_id=group_id,
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        message_type=message_type,
        sent_at=sent_at,
        is_self=is_self,
        reply_to_message_id=reply_to_message_id,
        attachments=attachments,
        raw_event=raw_event,
    )
    msg.sender_role = resolve_role(group, sender_id, id_map) if not is_self else "SYSTEM"

    if is_self:
        logger.info("系统账号回流消息过滤 sender=%s %s", sender_name, msg.brief())
    else:
        logger.debug("事件标准化完成 role=%s %s", msg.sender_role, msg.brief())
    return msg
