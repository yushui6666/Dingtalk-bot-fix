"""待确认动作服务（计划书 §5.4、§11.7、Task 8）。

同一 (group_id, user_id) 至多一条 WAITING（数据库部分唯一索引兜底）。
新动作会 SUPERSEDED 旧动作；确认/拒绝/修正用带版本 CAS 的原子更新。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from db import Database
from logger import get_logger
from semantics.types import PendingAction, PendingActionDraft, PendingActionStatus

logger = get_logger(__name__)

_PENDING_TTL_MINUTES = 30


def _row_to_pending(row: dict[str, Any]) -> PendingAction:
    return PendingAction(
        id=row["id"],
        source_message_id=row["source_message_id"],
        group_id=row["group_id"],
        user_id=row["user_id"],
        intent=row["intent"],
        candidate_ticket_ids=row["candidate_ticket_ids"],
        fields=row["fields"],
        expected_ticket_versions=row["expected_versions"],
        status=PendingActionStatus(row["status"]),
        version=row["version"],
        expires_at=datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S"),
    )


class PendingActionService:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_waiting(self, group_id: str, user_id: str) -> PendingAction | None:
        row = self._db.get_waiting_pending(group_id, user_id)
        return _row_to_pending(row) if row else None

    def get(self, pending_id: int) -> PendingAction | None:
        row = self._db.get_pending(pending_id)
        return _row_to_pending(row) if row else None

    def create_or_supersede(self, draft: PendingActionDraft, now: datetime) -> PendingAction:
        """新待确认动作：先 SUPERSEDE 旧 WAITING，再创建。"""
        expires_at = (now + timedelta(minutes=_PENDING_TTL_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        self._db.supersede_waiting(draft.group_id, draft.user_id, now=now.strftime("%Y-%m-%d %H:%M:%S"))
        pending_id = self._db.create_pending(
            source_message_id=draft.source_message_id,
            group_id=draft.group_id,
            user_id=draft.user_id,
            intent=draft.decision.intent,
            candidate_ticket_ids=tuple(draft.expected_ticket_versions.keys()),
            fields=dict(draft.decision.fields),
            expected_versions=dict(draft.expected_ticket_versions),
            expires_at=expires_at,
        )
        logger.info("创建待确认动作 id=%s intent=%s group=%s user=%s",
                    pending_id, draft.decision.intent, draft.group_id, draft.user_id)
        pending = self.get(pending_id)
        assert pending is not None
        return pending

    def resolve(
        self,
        pending_id: int,
        expected_version: int,
        status: PendingActionStatus,
        confirmation_message_id: str,
        *,
        now: datetime,
    ) -> bool:
        """CAS 解决待确认动作。返回 False = 版本冲突或已非 WAITING。"""
        return self._db.resolve_pending(
            pending_id,
            expected_version,
            status.value,
            confirmation_message_id,
            now=now.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def expire_due(self, now: datetime) -> int:
        return self._db.expire_due_pendings(now.strftime("%Y-%m-%d %H:%M:%S"))
