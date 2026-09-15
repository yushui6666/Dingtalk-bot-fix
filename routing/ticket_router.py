"""多工单路由（计划书 §6）。

路由优先级（§6.1）：
  明确工单编号 > 钉钉回复/引用 > 用户短期选择上下文 > 活动工单语义候选匹配 > 创建待确认动作

语义自动路由条件（§6.3）：
  auto_route_threshold=0.90，minimum_score_gap=0.15，clarify_threshold=0.65；
  新建与补充冲突门槛 0.85（自然语言报修命中任一候选 ≥0.85 时必须确认新建还是补充）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from models import NormalizedMessage
from semantics.types import (
    RouteDecision,
    RouteResult,
    SemanticDecision,
    TicketCandidate,
    TicketScore,
)

# 消息归属 link_type（§11.5 扩展）
LINK_EXPLICIT = "EXPLICIT"
LINK_QUOTE = "QUOTE"
LINK_SUFFIX = "SUFFIX"
LINK_CONTEXT = "CONTEXT"
LINK_SEMANTIC = "SEMANTIC"
LINK_SINGLE = "SINGLE"
LINK_CREATE = "CREATE"
LINK_CONFIRMED = "CONFIRMED"


@dataclass(frozen=True)
class RoutingConfig:
    auto_route_threshold: float = 0.90
    clarify_threshold: float = 0.65
    minimum_score_gap: float = 0.15
    create_conflict_threshold: float = 0.85


class TicketRouter:
    def __init__(self, config: RoutingConfig | None = None) -> None:
        self._config = config or RoutingConfig()

    def route(
        self,
        *,
        message: NormalizedMessage,
        decision: SemanticDecision,
        candidates: list[TicketCandidate],
        quoted_ticket_id: int | None = None,
        selected_ticket_id: int | None = None,
    ) -> RouteResult:
        """按优先级确定消息归属。候选为空时不可能路由到既有工单。"""
        group_candidates = [c for c in candidates if c.group_id == message.group_id]
        candidate_ids = tuple(c.ticket_id for c in group_candidates)
        no_map = {c.ticket_no: c for c in group_candidates}
        id_map = {c.ticket_id: c for c in group_candidates}

        # 0. chat.ignore 不走路由
        if decision.intent == "chat.ignore":
            return RouteResult(RouteDecision.CLARIFY, None, candidate_ids, None, 0.0)

        # 1. 明确工单编号（§6.1 最高优先级）
        if decision.target_ticket_no:
            target = no_map.get(decision.target_ticket_no)
            if target is not None:
                return RouteResult(RouteDecision.ROUTED, target.ticket_id, candidate_ids, LINK_EXPLICIT, 1.0)

        # 2. 钉钉回复/引用
        if quoted_ticket_id is not None and quoted_ticket_id in id_map:
            return RouteResult(RouteDecision.ROUTED, quoted_ticket_id, candidate_ids, LINK_QUOTE, 1.0)

        # 2.5 短编号后缀匹配：消息里的数字（如「005」「5号」）唯一对应某候选工单 → 归属
        suffix_target = self._route_by_suffix(message, decision, group_candidates)
        if suffix_target is not None:
            return RouteResult(RouteDecision.ROUTED, suffix_target.ticket_id, candidate_ids, LINK_SUFFIX, 1.0)

        # 3. 用户短期选择上下文。
        #    注意：ticket.create（新建工单）不受选单上下文影响——用户选过工单后再发
        #    新报修，语义是新建而非补充到已选工单（否则会触发「不得绑定既有目标工单」）。
        #    故 ticket.create 跳过本步，直接走第 5 步 _route_create。
        if (
            decision.intent != "ticket.create"
            and selected_ticket_id is not None
            and selected_ticket_id in id_map
        ):
            return RouteResult(RouteDecision.ROUTED, selected_ticket_id, candidate_ids, LINK_CONTEXT, 1.0)

        # 4. 语义候选匹配（多候选评分）
        if decision.intent != "ticket.create" and decision.candidate_scores and group_candidates:
            routed = self._route_by_scores(decision.candidate_scores, id_map, no_map)
            if routed is not None:
                return routed

        # 5. 新建与补充冲突（自然语言报修命中高相似候选 → 澄清）
        if decision.intent == "ticket.create":
            return self._route_create(decision, id_map, group_candidates)

        # 6. 单候选兜底
        if len(group_candidates) == 1:
            return RouteResult(RouteDecision.ROUTED, group_candidates[0].ticket_id, candidate_ids, LINK_SINGLE, 0.0)

        # 7. 歧义 → 澄清
        return RouteResult(RouteDecision.CLARIFY, None, candidate_ids, None, 0.0)

    def _route_by_suffix(
        self,
        message: NormalizedMessage,
        decision: SemanticDecision,
        group_candidates: list[TicketCandidate],
    ) -> TicketCandidate | None:
        """消息中的短编号（如「005」）唯一对应某候选工单的后缀 → 归属它。

        只对需要既有工单的动作生效（排除新建）；数字至少 2 位，避免「3天内」
        的「3」误命中「-003」。工单号形如 店名-主题-时效-005，取末段做后缀。
        """
        if decision.intent == "ticket.create":
            return None
        for num in re.findall(r"\d{2,}", message.content or ""):
            normalized = num.lstrip("0")
            matches = [
                c for c in group_candidates
                if c.ticket_no.rsplit("-", 1)[-1].lstrip("0") == normalized
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    def _route_by_scores(
        self,
        scores: tuple[TicketScore, ...],
        id_map: dict[int, TicketCandidate],
        no_map: dict[str, TicketCandidate],
    ) -> RouteResult | None:
        """按评分排序，仅当 top 达阈值且与次高分差足够才自动归属。"""
        ordered = sorted(
            (s for s in scores if s.ticket_no in no_map),
            key=lambda s: s.score,
            reverse=True,
        )
        if not ordered:
            return None
        top = ordered[0]
        second_score = ordered[1].score if len(ordered) > 1 else 0.0
        cfg = self._config
        if top.score >= cfg.auto_route_threshold and (top.score - second_score) >= cfg.minimum_score_gap:
            candidate = no_map[top.ticket_no]
            return RouteResult(
                RouteDecision.ROUTED, candidate.ticket_id, tuple(id_map), LINK_SEMANTIC, top.score
            )
        return None

    def _route_create(
        self,
        decision: SemanticDecision,
        id_map: dict[int, TicketCandidate],
        group_candidates: list[TicketCandidate],
    ) -> RouteResult:
        """ticket.create：显式 #报修 直接新建；自然语言与活动候选高度相似 → 澄清新建/补充。"""
        candidate_ids = tuple(id_map)
        # 显式关键词（source=keyword 且 evidence 为 #报修）→ 直接新建
        if decision.source == "keyword":
            return RouteResult(RouteDecision.CREATE, None, candidate_ids, LINK_CREATE, 0.0)

        # 自然语言报修：看候选评分是否命中冲突门槛
        if decision.candidate_scores:
            for s in decision.candidate_scores:
                if s.ticket_no in {c.ticket_no for c in group_candidates}:
                    if s.score >= self._config.create_conflict_threshold:
                        return RouteResult(RouteDecision.CLARIFY, None, candidate_ids, None, s.score)
        return RouteResult(RouteDecision.CREATE, None, candidate_ids, LINK_CREATE, 0.0)
