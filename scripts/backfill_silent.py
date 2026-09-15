"""静默补单：把 dws 拉取的历史群消息补录进系统并按正常 pipeline 处理。

场景：系统停机期间（2026-08-24 13:50 之后）的群消息漏处理，
用 dws chat +chat-messages 拉回（data/exports/silent_backfill/*.json）后，
本脚本把消息补录 inbox 并重放 pipeline。

静默保证：Notifier(enabled=False) 影子模式，所有通知/回复只记审计状态，
**不会向任何群或用户实际发送消息**。

用法::

    python scripts/backfill_silent.py [--json-dir data/exports/silent_backfill]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 与 main.py 一致：先加载 .env 再 import config
for env_path in (_PROJECT_ROOT / ".env",):
    if not env_path.exists():
        continue
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)

from config import (  # noqa: E402
    GROUPS,
    IMAGE_ARCHIVE_ENABLED,
    LLM_API_KEY,
    LLM_ENABLED,
    USER_ID_MAP,
)
from db import Database  # noqa: E402
from event_normalizer import normalize_event  # noqa: E402
from logger import get_logger  # noqa: E402
from notifier import Notifier  # noqa: E402
from pipeline import MessageProcessingPipeline, RuntimeMode  # noqa: E402
from routing.pending_actions import PendingActionService  # noqa: E402
from routing.ticket_contexts import TicketContextStore  # noqa: E402
from routing.ticket_router import TicketRouter  # noqa: E402
from semantics.protocol_loader import load_protocol  # noqa: E402
from tickets.executor import TicketCommandExecutor  # noqa: E402
from tickets.repository import TicketRepository  # noqa: E402

logger = get_logger(__name__)

DEFAULT_JSON_DIR = _PROJECT_ROOT / "data" / "exports" / "silent_backfill"


def _group_by_id() -> dict[str, dict]:
    return {g["group_id"]: g for g in GROUPS}


def _build_pipeline(db: Database):
    from semantics.classifier import SemanticClassifier
    from semantics.model_client import OpenAICompatibleModelClient

    protocol = load_protocol(_PROJECT_ROOT / "protocols" / "ticket_semantics.v4.json")
    repo = TicketRepository(db)
    router = TicketRouter()
    context = TicketContextStore(db)
    pending = PendingActionService(db)
    executor = TicketCommandExecutor(db, repo)
    notifier = Notifier(db, None, enabled=False)  # 影子：不实际外发（静默补单）
    classifier = None
    if LLM_ENABLED and LLM_API_KEY:
        client = OpenAICompatibleModelClient()
        classifier = SemanticClassifier(client=client, protocol=protocol)
    archiver = None
    if IMAGE_ARCHIVE_ENABLED:
        from images.archive import AttachmentArchiver

        archiver = AttachmentArchiver(db=db)
    return MessageProcessingPipeline(
        db=db, repo=repo, protocol=protocol, router=router, context=context,
        pending=pending, executor=executor, notifier=notifier,
        classifier=classifier, archiver=archiver, mode=RuntimeMode.PRODUCTION,
    )


def _dws_message_to_event(raw: dict) -> dict:
    """dws chat +chat-messages 的 message → normalize_event 期望的事件 dict。"""
    return {
        "message_id": raw["messageId"],
        "conversation_id": raw["conversationId"],
        "sender_open_dingtalk_id": raw["senderId"],
        "sender": raw.get("sender") or raw.get("senderId"),
        "content": raw.get("text") or raw.get("content") or "",
        "create_time": raw.get("createTime"),
        "msg_type": raw.get("msgType") or raw.get("type"),
    }


async def run(json_dir: Path) -> None:
    db = Database()
    db.init_schema()
    # 群配置同步入库（建单时 executor.get_group 需要）
    for g in GROUPS:
        db.upsert_group(g)

    pipeline = _build_pipeline(db)
    groups_by_id = _group_by_id()

    json_files = sorted(json_dir.glob("*.json"))
    if not json_files:
        print(f"[提示] {json_dir} 下没有 JSON 文件")
        return

    stats = {"new": 0, "dup": 0, "failed": 0, "skipped": 0}
    details: list[str] = []
    for path in json_files:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            print(f"[跳过] 读取失败 {path.name}: {exc}")
            stats["skipped"] += 1
            continue
        for raw in data.get("messages", []):
            try:
                event = _dws_message_to_event(raw)
                group = groups_by_id.get(event["conversation_id"])
                msg = normalize_event(event, group=group, id_map=USER_ID_MAP)
                if msg is None:
                    print(f"[跳过] 标准化失败 mid={event.get('message_id')}")
                    stats["skipped"] += 1
                    continue
                enqueued = db.enqueue_message(msg)
                if not enqueued:
                    stats["dup"] += 1
                    print(f"[重复] 已在库 mid={msg.message_id} group={msg.group_id[:16]}…")
                    continue
                row = db.get_inbox_message(msg.message_id)
                status = await pipeline.process(row)
                stats["new"] += 1
                note = (
                    f"[补录] {msg.sent_at:%m-%d %H:%M} {group.get('store_name', '?') if group else '?'} "
                    f"{msg.sender_role:<4} {msg.content[:36]!r} → {status}"
                )
                print(note)
                details.append(note)
            except Exception as exc:
                stats["failed"] += 1
                print(f"[异常] mid={raw.get('messageId')} err={exc}")
                logger.exception("补录处理异常 mid=%s", raw.get("messageId"))

    print("\n=== 补录汇总 ===")
    print(f"新增处理 {stats['new']} 条，已在库跳过 {stats['dup']} 条，失败 {stats['failed']} 条，跳过 {stats['skipped']} 条")
    print("（影子模式：全程未向任何群/用户发送消息，仅本地落库与审计）")
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="静默补单：历史群消息补录并重放 pipeline（不外发）")
    parser.add_argument("--json-dir", type=Path, default=DEFAULT_JSON_DIR, help="拉取消息 JSON 目录")
    args = parser.parse_args()
    asyncio.run(run(args.json_dir))
