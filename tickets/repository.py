"""工单仓储：创建、编号、候选快照与生命周期（计划书 §9、§11）。

- 工单编号格式：``{店名}-{主题}-{时效}-{序号:03d}``（§9.2），序号每群独立递增。
- 候选快照：把活动工单冻结为 :class:`TicketCandidate`，供路由/分类器消费。
- 所有写操作必须由调用方在 :class:`Database.transaction` 内执行，
  并尽量使用乐观版本 CAS（update_cas）。
"""

from __future__ import annotations

from typing import Any

from db import Database
from logger import get_logger
from models import TICKET_ACTIVE, TICKET_NEGOTIATING
from semantics.types import TicketCandidate

logger = get_logger(__name__)

_SLA_LABELS = {"1天": 1, "3天": 3, "7天": 7, "待商榷": 0}


class TicketRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ─────────────────────── 创建 ───────────────────────
    def create_ticket(
        self,
        *,
        group: dict[str, Any],
        reporter_id: str,
        subject: str,
        location: str,
        problem_description: str,
        sla_label: str,
        now: str,
    ) -> int:
        """创建工单（需在事务内调用）。返回工单 id。

        sla_label 为协议枚举（1天/3天/7天/待商榷）。待商榷 = 不设时效，
        仅记录（sla_days=0，deadline 置空）。
        """
        sla_days = _SLA_LABELS.get(sla_label, 1)  # 未识别标签兜底 1 天
        deadline = None if sla_days == 0 else _add_days(now, sla_days)
        seq = self._db.next_ticket_seq(group["group_id"])
        store_name = group.get("store_name") or "门店"
        ticket_no = f"{store_name}-{subject}-{sla_label}-{seq:03d}"
        ticket_id = self._db.insert_ticket({
            "ticket_no": ticket_no,
            "group_id": group["group_id"],
            "store_name": store_name,
            "reporter_id": reporter_id,
            "subject": subject,
            "location": location,
            "problem_description": problem_description,
            "sla_days": sla_days,
            "initial_deadline_at": deadline,
            "current_deadline_at": deadline,
            "status": TICKET_NEGOTIATING if sla_days == 0 else TICKET_ACTIVE,
        })
        logger.info("创建工单 id=%s ticket_no=%s group=%s", ticket_id, ticket_no, group["group_id"])
        return ticket_id

    # ─────────────────────── 候选快照 ───────────────────────
    def snapshot_candidates(
        self, group_id: str, statuses: tuple[str, ...] | None = None
    ) -> list[TicketCandidate]:
        """当前群工单 → 路由候选快照。

        ``statuses=None``（默认）＝ 只取活动工单（ACTIVE/ACTIVE_OVERDUE），
        即「待商榷不算活动」的既有语义；显式传状态集合时按集合取，供
        pipeline 按协议 allowed_ticket_states 纳入待商榷/待店长确认等
        在途状态（2026-09-14，见 db.list_tickets_by_statuses 注释）。
        """
        rows = (
            self._db.list_active_tickets(group_id)
            if statuses is None
            else self._db.list_tickets_by_statuses(group_id, tuple(statuses))
        )
        return [
            TicketCandidate(
                ticket_id=r["id"],
                ticket_no=r["ticket_no"],
                group_id=r["group_id"],
                subject=r["subject"],
                location=r["location"],
                problem_summary=(r["problem_description"] or "")[:80],
                status=r["status"],
                version=r["version"],
            )
            for r in rows
        ]

    def snapshot_group_tickets(self, group_id: str) -> list[TicketCandidate]:
        """群内全部工单（含 STOPPED/COMPLETED/CANCELLED 等终态）→ 候选快照。

        仅供 #重开工单 等需要定位终态工单的动作使用，避免重开无法路由。
        """
        rows = self._db.list_group_tickets(group_id)
        return [
            TicketCandidate(
                ticket_id=r["id"],
                ticket_no=r["ticket_no"],
                group_id=r["group_id"],
                subject=r["subject"],
                location=r["location"],
                problem_summary=(r["problem_description"] or "")[:80],
                status=r["status"],
                version=r["version"],
            )
            for r in rows
        ]

    def get_candidate(self, group_id: str, ticket_no: str) -> TicketCandidate | None:
        for c in self.snapshot_candidates(group_id):
            if c.ticket_no == ticket_no:
                return c
        return None

    # ─────────────────────── 读取 ───────────────────────
    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        return self._db.get_ticket(ticket_id)

    def get_ticket_by_no(self, ticket_no: str) -> dict[str, Any] | None:
        return self._db.get_ticket_by_no(ticket_no)

    # ─────────────────────── 生命周期（CAS 乐观更新） ───────────────────────
    def update_cas(
        self, ticket_id: int, expected_version: int, set_clause: str, params: tuple[Any, ...]
    ) -> bool:
        """乐观版本更新，返回是否成功（False = 版本冲突）。"""
        return self._db.update_ticket_cas(ticket_id, expected_version, set_clause, params)

    def touch_business_event(
        self, ticket_id: int, expected_version: int, sent_at: str, message_id: str
    ) -> bool:
        """推进 last_business_event 游标（随业务变更同一事务调用）。"""
        return self.update_cas(
            ticket_id,
            expected_version,
            "last_business_event_at=?, last_business_message_id=?",
            (sent_at, message_id),
        )


def _add_days(now_str: str, days: int) -> str:
    """now_str（%Y-%m-%d %H:%M:%S）加 N 天，返回同格式字符串。"""
    from datetime import datetime, timedelta

    dt = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    return (dt + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
