"""JSON-safe state contracts used by the dispatcher and ticket graphs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from models import ImageAttachment, NormalizedMessage


class DispatcherState(TypedDict, total=False):
    inbox_item: dict[str, Any]
    message: dict[str, Any]
    dispatch_context: dict[str, Any]
    processed_status: str
    target_ticket_id: int
    ticket_thread_id: str
    ticket_agent_result: dict[str, Any]
    agent_error: str
    error: str
    failed_node: str
    graph_path: list[str]


class TicketAgentState(TypedDict, total=False):
    ticket_id: int
    ticket_no: str
    group_id: str
    agent_stage: str
    incoming_event: dict[str, Any]
    ticket_snapshot: dict[str, Any]
    event_kind: str
    feedback: str
    conversation_summary: str
    retrieval_query: str
    retrieved_context: dict[str, Any]
    guidance: str
    run_outcome: str
    error: str


def message_to_state(message: NormalizedMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "group_id": message.group_id,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "sender_role": message.sender_role,
        "content": message.content,
        "message_type": message.message_type,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "is_self": message.is_self,
        "reply_to_message_id": message.reply_to_message_id,
        "attachments": [
            {
                "attachment_index": item.attachment_index,
                "source_type": item.source_type,
                "source_ref": item.source_ref,
                "file_name": item.file_name,
                "declared_mime_type": item.declared_mime_type,
            }
            for item in message.attachments
        ],
    }


def message_from_state(value: dict[str, Any]) -> NormalizedMessage:
    sent_at = value.get("sent_at")
    return NormalizedMessage(
        message_id=str(value["message_id"]),
        group_id=str(value["group_id"]),
        sender_id=str(value["sender_id"]),
        sender_name=str(value.get("sender_name") or ""),
        sender_role=str(value.get("sender_role") or "UNKNOWN"),
        content=str(value.get("content") or ""),
        message_type=str(value.get("message_type") or "text"),
        sent_at=datetime.fromisoformat(sent_at) if sent_at else None,
        is_self=bool(value.get("is_self")),
        reply_to_message_id=value.get("reply_to_message_id"),
        attachments=[ImageAttachment(**item) for item in value.get("attachments", [])],
    )
