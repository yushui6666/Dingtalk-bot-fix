"""Keyword-first manager feedback classification and LLM guidance generation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from logger import get_logger

logger = get_logger(__name__)


class ManagerFeedback(StrEnum):
    UNSOLVED = "UNSOLVED"
    RESOLVED = "RESOLVED"
    HANDOFF = "HANDOFF"
    UNCLEAR = "UNCLEAR"


_HANDOFF_WORDS = ("联系工程师", "找工程师", "安排工程师", "工程师上门", "转工程师")
_UNSOLVED_WORDS = ("还是不行", "仍然不行", "还没解决", "没有解决", "没修好", "又出现", "还是有问题")
_RESOLVED_WORDS = ("已经修好", "修好了", "已解决", "恢复正常", "已经正常", "可以用了")


class HybridTroubleshootingService:
    def __init__(self, model_client: Any | None = None) -> None:
        self._client = model_client

    async def classify_feedback(
        self, *, message: dict[str, Any], ticket: dict[str, Any]
    ) -> ManagerFeedback:
        text = str(message.get("content") or "").strip()
        if any(word in text for word in _HANDOFF_WORDS):
            return ManagerFeedback.HANDOFF
        if any(word in text for word in _UNSOLVED_WORDS):
            return ManagerFeedback.UNSOLVED
        if any(word in text for word in _RESOLVED_WORDS):
            return ManagerFeedback.RESOLVED
        if not text or self._client is None:
            return ManagerFeedback.UNCLEAR

        schema = {
            "type": "object",
            "properties": {
                "feedback": {
                    "type": "string",
                    "enum": [item.value for item in ManagerFeedback],
                }
            },
            "required": ["feedback"],
            "additionalProperties": False,
        }
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "判断店长对当前报修排障的反馈。只分类为："
                        "UNSOLVED（未解决或新现象）、RESOLVED（已解决）、"
                        "HANDOFF（要求工程师处理）、UNCLEAR（无关或无法判断）。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"工单：{ticket.get('subject', '')}；"
                        f"故障：{ticket.get('problem_description', '')}；"
                        f"店长消息：{text}"
                    ),
                },
            ]
        }
        try:
            result = await self._client.complete_json(
                payload=payload,
                schema=schema,
                idempotency_key=f"ticket-feedback:{message.get('message_id', '')}",
                append_output_example=False,
            )
            return ManagerFeedback(str(result.get("feedback") or "UNCLEAR"))
        except Exception as exc:
            logger.warning("工单反馈分类失败 msg=%s err=%s", message.get("message_id"), exc)
            return ManagerFeedback.UNCLEAR

    async def generate_guidance(
        self,
        *,
        ticket: dict[str, Any],
        message: dict[str, Any],
        retrieved_context: dict[str, Any],
    ) -> str:
        fallback = (
            "请先确认设备供电、连接和开关状态，并观察是否有报错或异响；"
            "把检查结果或现场照片发到群里，我会继续判断。"
            "如果希望转人工，请回复“联系工程师”。"
        )
        if self._client is None:
            return fallback
        documents = retrieved_context.get("documents") or []
        schema = {
            "type": "object",
            "properties": {"guidance": {"type": "string", "minLength": 1}},
            "required": ["guidance"],
            "additionalProperties": False,
        }
        knowledge_note = (
            "当前没有接入知识库，请只依据工单和消息给出通用、安全、可操作的检查步骤。"
            if not documents
            else f"检索资料：{documents}"
        )
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是门店报修排障助手。给店长 1 到 3 个简短检查步骤，"
                        "避免宣称已经修复；需要专业拆修时建议联系工程师。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"主题：{ticket.get('subject', '')}\n"
                        f"位置：{ticket.get('location', '')}\n"
                        f"故障：{ticket.get('problem_description', '')}\n"
                        f"最新反馈：{message.get('content', '')}\n{knowledge_note}"
                    ),
                },
            ]
        }
        try:
            result = await self._client.complete_json(
                payload=payload,
                schema=schema,
                idempotency_key=f"ticket-guidance:{message.get('message_id', '')}",
                append_output_example=False,
            )
            guidance = str(result.get("guidance") or "").strip()
            return guidance or fallback
        except Exception as exc:
            logger.warning("维修指导生成失败 msg=%s err=%s", message.get("message_id"), exc)
            return fallback
