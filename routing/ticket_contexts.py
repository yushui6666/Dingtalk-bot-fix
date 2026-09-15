"""用户短期工单上下文（计划书 §6.4、§11.6）。

以 (group_id, user_id) 为唯一当前上下文，保存选中工单、消息顺序键和过期时间。
清除条件：换选另一张工单、工单完成/取消、过期、用户取消选择。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from db import Database
from logger import get_logger

logger = get_logger(__name__)

_CONTEXT_TTL_MINUTES = 30


class TicketContextStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def select(
        self,
        group_id: str,
        user_id: str,
        ticket_id: int,
        *,
        order_key: str,
        now: datetime,
    ) -> None:
        """用户选择工单，建立 30 分钟上下文。"""
        expires_at = (now + timedelta(minutes=_CONTEXT_TTL_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        self._db.set_ticket_context(
            group_id, user_id, ticket_id, order_key, expires_at, now=now_str
        )
        logger.info("设置工单上下文 group=%s user=%s ticket_id=%s", group_id, user_id, ticket_id)

    def get_active(self, group_id: str, user_id: str, now: datetime) -> int | None:
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        row = self._db.get_ticket_context(group_id, user_id, now_str)
        return row["ticket_id"] if row else None

    def clear(self, group_id: str, user_id: str) -> None:
        self._db.clear_ticket_context(group_id, user_id)

    def clear_by_ticket(self, ticket_id: int) -> int:
        """工单完成/取消后清除所有指向该工单的上下文。"""
        return self._db.clear_contexts_by_ticket(ticket_id)
