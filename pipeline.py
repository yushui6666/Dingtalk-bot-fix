"""消息处理管道（计划书 §7、Task 11）。

编排：Inbox → 待确认回复识别 → 云端模型语义识别（全 AI，关键词快路径已永久停用） →
语义决策审计 → 候选快照 → 路由（编号>引用>上下文>语义>单候选）→ 校验 →
待确认/执行 → Outbox。

运行模式门禁（§16）：
- SHADOW：只记录语义决策，不归属、不建单、不发确认。
- ASSISTED：模型来源动作一律待确认。
- PRODUCTION：按协议确认策略执行。
"""

from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from config import (
    LANGGRAPH_CHECKPOINT_PATH,
    LANGGRAPH_ENABLED,
    LLM_ENABLED,
    LLM_MAX_ATTEMPTS,
    LLM_RETRY_DELAYS_SECONDS,
)
from db import Database
from logger import get_logger
from models import (
    ROLE_MANAGER,
    ROLE_SYSTEM,
    TICKET_ACTIVE,
    TICKET_NEGOTIATING,
    TICKET_OVERDUE,
    TICKET_PENDING_CONFIRM,
    NormalizedMessage,
)
from ordering import OrderedCommitGate, parse_naive_dt
from routing.pending_actions import PendingActionService
from routing.ticket_contexts import TicketContextStore
from routing.ticket_router import (
    LINK_CONTEXT,
    LINK_SINGLE,
    TicketRouter,
    RoutingConfig,
)
from semantics.classifier import normalize_semantic_decision
from semantics.protocol_loader import TicketProtocol
from semantics.types import (
    DecisionStatus,
    PendingActionDraft,
    PendingActionStatus,
    RouteDecision,
    SemanticDecision,
)
from semantics.validator import validate_decision
from store_parts import build_vocab as _build_store_vocab
from tickets.commands import wrong_number_text
from tickets.executor import (
    RESULT_INTERNAL_ERROR,
    RESULT_OK,
    TicketCommandExecutor,
)
from tickets.repository import TicketRepository

logger = get_logger(__name__)

_COMMIT_GATE: ContextVar[OrderedCommitGate | None] = ContextVar(
    "message_commit_gate", default=None
)

_INBOX_COMPLETED = "COMPLETED"
_INBOX_RETRY = "RETRY_PENDING"
_INBOX_DEAD = "DEAD_LETTER"


def _accepts_parts_vocab(classifier: Any) -> bool:
    """分类器是否接受 parts_vocab 关键字（测试桩大多不接收，避免破坏既有测试）。"""
    fn = getattr(classifier, "classify", None)
    if fn is None:
        return False
    try:
        import inspect

        sig = inspect.signature(fn)
        return "parts_vocab" in sig.parameters
    except (TypeError, ValueError):
        return False

# 这些意图自带编号容错（自拉全量/新建/交互式选择），不做前置编号硬校验
_NUMBER_TOLERANT_INTENTS = frozenset({
    "ticket.create", "ticket.select", "ticket.query", "ticket.reopen",
})
# 店长确认/驳回完工：候选只取 PENDING_CONFIRM 工单
_CONFIRM_COMPLETE_INTENTS = frozenset({"ticket.confirm_complete", "ticket.reject_complete"})

# ── 路由候选的「在途非活动」状态（2026-09-14）──
# 事故：北京商场14大悦城品牌B店唯一工单为待商榷（PENDING_NEGOTIATION，
# 2026-08-28 起不算活动工单），工程师群里发「001已完成」→ 候选=0 →
# route=CLARIFY → 误回「当前没有可操作的活动工单，请先创建工单。」
# 而协议 ticket.complete/cancel/stop 的 allowed_ticket_states 早已含
# PENDING_NEGOTIATION（2026-08-28 用户决策「待商榷可直接完工」）——
# 属实现层缺口：协议放行、路由不可达。
# 修法：候选状态按协议 allowed_ticket_states 取，但只放开下表列出的、
# 已有明确产品结论的在途状态；未列入的意图保持「只要活动工单」不变。
# 有意未放开（待产品决策，勿擅动）：
#   ticket.special_case.submit × PENDING_CONFIRM/PENDING_NEGOTIATION
#   —— 2026-08-28 遗留「特殊情况对 PENDING_CONFIRM 路由不可达」/ 2026-09-10
#   方案「问题二」，用户尚未拍板，保持只解释不登记。
_ROUTING_IN_FLIGHT_STATES: dict[str, tuple[str, ...]] = {
    # 待商榷可直接完工（2026-08-28 用户决策）
    "ticket.complete": (TICKET_NEGOTIATING,),
    # 待店长确认/待商榷均可取消、停修
    # （2026-08-24「待店长确认工单仍可取消/停修」+ 2026-08-28 待商榷同族）
    "ticket.cancel": (TICKET_PENDING_CONFIRM, TICKET_NEGOTIATING),
    "ticket.stop": (TICKET_PENDING_CONFIRM, TICKET_NEGOTIATING),
}


def _suggest_ticket_no(wrong_no: str, ticket_nos: list[str]) -> str | None:
    """编号纠错的近似建议：末段数字一致优先，其次 difflib 相似度。"""
    import difflib

    wrong_suffix = wrong_no.rsplit("-", 1)[-1].lstrip("0") or "0"
    for no in ticket_nos:
        if no.rsplit("-", 1)[-1].lstrip("0") == wrong_suffix and len(wrong_suffix) >= 2:
            return no
    matches = difflib.get_close_matches(wrong_no, ticket_nos, n=1, cutoff=0.72)
    return matches[0] if matches else None


class RuntimeMode(StrEnum):
    SHADOW = "SHADOW"
    ASSISTED = "ASSISTED"
    PRODUCTION = "PRODUCTION"


def _is_model_fallback(decision: SemanticDecision) -> bool:
    return any(e.startswith("model_fallback:") for e in decision.evidence)


class MessageProcessingPipeline:
    def __init__(
        self,
        *,
        db: Database,
        repo: TicketRepository,
        protocol: TicketProtocol,
        router: TicketRouter,
        context: TicketContextStore,
        pending: PendingActionService,
        executor: TicketCommandExecutor,
        notifier: Any,
        classifier: Any | None = None,
        archiver: Any | None = None,
        mode: RuntimeMode = RuntimeMode.PRODUCTION,
        llm_enabled: bool = LLM_ENABLED,
        max_attempts: int = LLM_MAX_ATTEMPTS,
        retry_delays: tuple[float, ...] = tuple(LLM_RETRY_DELAYS_SECONDS),
    ) -> None:
        self._db = db
        self._repo = repo
        self._protocol = protocol
        self._router = router
        self._context = context
        self._pending = pending
        self._executor = executor
        self._notifier = notifier
        self._classifier = classifier
        self._archiver = archiver
        self._mode = mode
        self._llm_enabled = llm_enabled and classifier is not None
        self._max_attempts = max_attempts
        self._retry_delays = retry_delays
        self._vision_tasks: list[asyncio.Task] = []
        self._graph_enabled = LANGGRAPH_ENABLED
        self._graph_runtime: Any | None = None
        self._graph_runtime_lock = asyncio.Lock()

    async def process(
        self,
        item: dict[str, Any],
        *,
        commit_gate: OrderedCommitGate | None = None,
    ) -> str:
        """处理消息；可选群级闸门让识别并行、提交按消息顺序执行。"""
        token = _COMMIT_GATE.set(commit_gate)
        try:
            if not self._graph_enabled:
                return await self._process_without_graph(item)
            if self._graph_runtime is None:
                async with self._graph_runtime_lock:
                    if self._graph_runtime is None:
                        from graphs.runtime import GraphRuntime

                        model_client = (
                            getattr(self._classifier, "model_client", None)
                            if self._classifier is not None
                            else None
                        )
                        self._graph_runtime = GraphRuntime(
                            pipeline=self,
                            checkpoint_path=LANGGRAPH_CHECKPOINT_PATH,
                            model_client=model_client,
                        )
            return await self._graph_runtime.process(item)
        finally:
            _COMMIT_GATE.reset(token)
            if commit_gate is not None:
                # 正常路径已在业务提交后释放；这里兜底模型异常、重试等情况。
                commit_gate.mark_done(str(item.get("message_id") or ""))

    @property
    def graph_runtime(self) -> Any | None:
        return self._graph_runtime

    async def aclose(self) -> None:
        if self._graph_runtime is not None:
            await self._graph_runtime.aclose()

    async def _process_without_graph(self, item: dict[str, Any]) -> str:
        """迁移期开关使用的原流程；业务实现仍与 Graph 节点共用。"""
        msg = self.graph_prepare_message(item)
        await self.graph_archive_attachments(msg)
        try:
            status = await self.graph_process_business(item, msg)
            logger.info("消息处理完成 msg=%s status=%s", msg.message_id, status)
            return status
        except Exception as exc:
            return self.graph_handle_exception(item, msg, exc)

    # ─────────────────────── LangGraph 节点适配 ───────────────────────
    def graph_prepare_message(self, item: dict[str, Any]) -> NormalizedMessage:
        msg = _row_to_message(item)
        # 从 DB 恢复附件（入箱时已写 message_attachments 元数据）
        try:
            att_rows = self._db.list_attachment_rows(msg.message_id)
            if att_rows:
                from models import ImageAttachment

                msg.attachments = [
                    ImageAttachment(
                        attachment_index=r["attachment_index"],
                        source_type=r["source_type"],
                        source_ref=r["source_ref"],
                        file_name=r.get("file_name"),
                        declared_mime_type=r.get("declared_mime_type"),
                    )
                    for r in att_rows
                ]
        except Exception as exc:
            logger.warning("附件恢复失败 msg=%s err=%s", msg.message_id, exc)
        self._db.inbox_set_status(msg.message_id, "PROCESSING")
        logger.info(
            "消息处理开始 msg=%s group=%s sender=%s role=%s type=%s",
            msg.message_id, msg.group_id, msg.sender_id[:8], msg.sender_role, msg.message_type,
        )
        return msg

    async def graph_archive_attachments(self, msg: NormalizedMessage) -> None:
        if self._mode != RuntimeMode.SHADOW:
            await self._archive_attachments(msg)

    def graph_load_dispatch_context(self, msg: NormalizedMessage) -> dict[str, Any]:
        active = self._repo.snapshot_candidates(msg.group_id)
        pending = self._pending.get_waiting(msg.group_id, msg.sender_id)
        return {
            "pending_action_id": pending.id if pending is not None else None,
            "quoted_ticket_id": (
                self._db.get_quoted_ticket_id(msg.reply_to_message_id)
                if msg.reply_to_message_id else None
            ),
            "selected_ticket_id": self._context.get_active(
                msg.group_id, msg.sender_id, datetime.now()
            ),
            "candidate_ticket_ids": [item.ticket_id for item in active],
        }

    async def graph_process_business(
        self, item: dict[str, Any], msg: NormalizedMessage
    ) -> str:
        return await self._handle(msg, item)

    def graph_resolve_agent_ticket(self, msg: NormalizedMessage) -> int | None:
        """在既有业务处理后定位逻辑 Agent；不改变原工单路由结果。"""
        if self._mode == RuntimeMode.SHADOW:
            return None
        link = self._db.get_message_link(msg.message_id)
        if link is not None:
            # 建单消息只把 Agent 指向新工单，**不得**顺手改写用户的选单上下文。
            # 2026-09-16 修复：LangGraph 迁移（96cad38）曾在此对 link_type=CREATE
            # 调用 _context.select，导致建单后 30 分钟内所有未编号消息被静默绑定到
            # 「最新建的那张单」，绕过了「多候选 → 请选择具体工单」的澄清流程
            # （与 README「单群多工单」和本函数 docstring 承诺的"不改变路由结果"冲突）。
            # 选单上下文只能由用户显式动作建立：ticket.select、无编号补充时的
            # 唯一候选兜底，以及归属问询后的选择。
            return int(link["ticket_id"])

        candidates = self._repo.snapshot_candidates(
            msg.group_id,
            statuses=(TICKET_ACTIVE, TICKET_OVERDUE, TICKET_PENDING_CONFIRM, TICKET_NEGOTIATING),
        )
        candidate_ids = {item.ticket_id for item in candidates}
        if msg.reply_to_message_id:
            quoted = self._db.get_quoted_ticket_id(msg.reply_to_message_id)
            if quoted in candidate_ids:
                return quoted

        decision = self._db.get_latest_semantic_decision(msg.message_id)
        target_no = str((decision or {}).get("target_ticket_no") or "")
        if target_no:
            exact = [item for item in candidates if item.ticket_no == target_no]
            if len(exact) == 1:
                return exact[0].ticket_id

        mentioned = _mentioned_ticket_numbers(msg.content)
        if mentioned:
            suffix_matches = [
                item for item in candidates
                if item.ticket_no.rsplit("-", 1)[-1].lstrip("0") in mentioned
            ]
            if len(suffix_matches) == 1:
                return suffix_matches[0].ticket_id

        selected = self._context.get_active(msg.group_id, msg.sender_id, datetime.now())
        if selected in candidate_ids:
            return selected
        if len(candidates) == 1:
            return candidates[0].ticket_id
        return None

    def graph_build_ticket_event(self, message: dict[str, Any]) -> dict[str, Any]:
        event = dict(message)
        message_id = str(event.get("message_id") or "")
        link = self._db.get_message_link(message_id)
        decision = self._db.get_latest_semantic_decision(message_id)
        event["link_type"] = link.get("link_type") if link else None
        event["semantic_intent"] = decision.get("intent") if decision else None
        event["semantic_fields"] = decision.get("fields") if decision else {}
        return event

    def graph_load_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        return self._db.get_ticket(ticket_id)

    def graph_persist_agent_event(self, ticket_id: int, event: dict[str, Any]) -> None:
        """把未被业务命令消费的排障反馈归档到对应工单。"""
        message_id = str(event.get("message_id") or "")
        if not message_id or self._db.get_message_link(message_id) is not None:
            return
        sent_at = str(event.get("sent_at") or "")
        try:
            sent_at_db = datetime.fromisoformat(sent_at).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            sent_at_db = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._db.transaction("langgraph_agent_event"):
            self._db.link_message(message_id, ticket_id, "AGENT_CONTEXT", 1.0)
            self._db.add_ticket_message(
                message_id,
                ticket_id,
                str(event.get("sender_id") or ""),
                str(event.get("sender_role") or "UNKNOWN"),
                str(event.get("content") or ""),
                str(event.get("message_type") or "text"),
                sent_at_db,
            )

    def graph_notify_agent(self, group_id: str, text: str, message_id: str) -> None:
        send = getattr(self._notifier, "send_agent_message", None)
        if send is not None:
            send(group_id, text, message_id=message_id)
        else:
            self._notifier.send_group_now(group_id, text, message_id=message_id)

    def graph_handle_exception(
        self,
        item: dict[str, Any],
        msg: NormalizedMessage | None,
        exc: Exception,
    ) -> str:
        msg = msg or _row_to_message(item)
        logger.error("消息处理异常 message_id=%s err=%s", msg.message_id, exc)
        return self._retry_or_dead(item, msg, str(exc))

    def graph_record_completion(self, state: dict[str, Any]) -> None:
        message = state.get("message") or {}
        logger.info(
            "LangGraph 消息处理完成 msg=%s status=%s ticket_thread=%s path=%s",
            message.get("message_id"),
            state.get("processed_status"),
            state.get("ticket_thread_id") or "-",
            " -> ".join(state.get("graph_path") or ()),
        )
        if state.get("agent_error"):
            logger.warning(
                "Ticket Agent 未完成 msg=%s err=%s",
                message.get("message_id"),
                state["agent_error"],
            )

    # ─────────────────────── 主流程 ───────────────────────
    async def _handle(self, msg: NormalizedMessage, item: dict[str, Any]) -> str:
        gate = _COMMIT_GATE.get()
        if msg.attachments:
            if gate is not None:
                await gate.wait_turn(msg.message_id)
            try:
                if self._mode == RuntimeMode.SHADOW:
                    return self._complete(item, msg, "SHADOW")
                # 图片消息（含附件）→ 补图归属；视觉解析仍异步执行，不占提交锁。
                return self._handle_image_attachment(item, msg)
            finally:
                if gate is not None:
                    gate.mark_done(msg.message_id)

        pending = self._pending.get_waiting(msg.group_id, msg.sender_id)

        decision = await self._decide(msg)
        # 本地确定性后修正（幂等）：选单快路径/关键词路径同样受益，
        # 模型路径在 classifier 内已做过一次（2026-09-03）。
        decision = normalize_semantic_decision(decision, msg)
        logger.info(
            "语义决策 msg=%s source=%s intent=%s conf=%.2f target_no=%s missing=%s",
            msg.message_id, decision.source, decision.intent, decision.intent_confidence,
            decision.target_ticket_no, decision.missing_fields or "-",
        )

        if gate is not None:
            await gate.wait_turn(msg.message_id)
        try:
            return await self._handle_decided(item, msg, decision, pending)
        finally:
            if gate is not None:
                gate.mark_done(msg.message_id)

    async def _handle_decided(
        self,
        item: dict[str, Any],
        msg: NormalizedMessage,
        decision: SemanticDecision,
        pending: Any,
    ) -> str:
        # 识别阶段可能与其他消息并行，轮到本消息提交时重新读取最新 Pending，
        # 避免使用识别前快照处理已被前序消息创建/替换的确认动作。
        pending = self._pending.get_waiting(msg.group_id, msg.sender_id)

        # SHADOW 只记录语义决策：不解决既有 Pending，不路由、校验、建 Pending 或发消息。
        if self._mode == RuntimeMode.SHADOW:
            self._save_decision(msg, decision)
            logger.info("影子模式：只记录不执行 message_id=%s intent=%s", msg.message_id, decision.intent)
            return self._complete(item, msg, "SHADOW")

        if pending is not None:
            resolved = await self._resolve_pending_reply(item, msg, pending, decision)
            if resolved is not None:
                return resolved

        # 模型降级 → 重试/死信（关键词快路径已永久停用，无本地兜底）
        if _is_model_fallback(decision):
            return self._retry_or_dead(item, msg, "模型调用失败")

        # 语义决策审计
        self._save_decision(msg, decision)

        # ── 待店长确认窗口：该群存在 PENDING_CONFIRM 工单时，
        # 所有成员消息（含闲聊）强制归档到最早的待确认工单，作为完工沟通记录
        # （需求 2026-08-24 #3「维修完成的聊天需要记录到工单内」）。──
        pending_confirm = self._db.get_group_pending_confirm_tickets(msg.group_id)
        if pending_confirm and msg.sender_role != ROLE_SYSTEM:
            t0 = pending_confirm[0]
            self._db.link_message(msg.message_id, t0["id"], "CONFIRM_WINDOW", 0.0)
            self._db.add_ticket_message(
                msg.message_id, t0["id"], msg.sender_id, msg.sender_role,
                msg.content or "", msg.message_type,
                msg.sent_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
            if decision.intent == "chat.ignore":
                return self._complete(item, msg, "ARCHIVED")
        if decision.intent == "chat.ignore":
            return self._complete(item, msg, "IGNORED")

        # 需要澄清：消息有歧义（如同时含多个业务动作）→ 直接问用户，不建待确认
        if decision.intent == "system.clarify":
            return self._handle_clarify(item, msg, decision)

        candidates = self._repo.snapshot_candidates(
            msg.group_id, self._routing_candidate_states(decision.intent)
        )
        # 重开及指定编号查询需定位终态工单（STOPPED/COMPLETED/CANCELLED）。
        # 无编号查询仍只列活动工单，保持原有查询语义。
        if decision.intent == "ticket.reopen" or (
            decision.intent == "ticket.query" and decision.target_ticket_no
        ):
            candidates = self._repo.snapshot_group_tickets(msg.group_id)

        # 店长确认/驳回完工：候选只取待店长确认工单（含编号定位与单候选兜底）
        if decision.intent in _CONFIRM_COMPLETE_INTENTS:
            candidates = [
                c for c in self._repo.snapshot_group_tickets(msg.group_id)
                if c.status == TICKET_PENDING_CONFIRM
            ]

        # ── 编号硬校验（需求 2026-08-24 #1）：显式编号必须存在且状态允许，
        # 否则明确报错，绝不静默 fall-through 归属到其他工单。──
        if decision.target_ticket_no and decision.intent not in _NUMBER_TOLERANT_INTENTS:
            full_tickets = self._repo.snapshot_group_tickets(msg.group_id)
            exact = next(
                (c for c in full_tickets if c.ticket_no == decision.target_ticket_no), None
            )
            if exact is None:
                suggestion = _suggest_ticket_no(
                    decision.target_ticket_no, [c.ticket_no for c in full_tickets]
                )
                self._notifier.send_group_now(
                    msg.group_id,
                    wrong_number_text(decision.target_ticket_no, full_tickets, suggestion),
                    message_id=msg.message_id,
                )
                logger.info("编号纠错拒绝 msg=%s target=%s", msg.message_id, decision.target_ticket_no)
                return self._complete(item, msg, "REJECTED")
            action_def = self._protocol.get_action(decision.intent)
            states = tuple(action_def.allowed_ticket_states) if action_def else ()
            if states and exact.status not in states:
                from tickets.commands import intent_label as _intent_label
                from tickets.commands import ticket_status_label as _status_label

                self._notifier.send_group_now(
                    msg.group_id,
                    f"⚠️ 工单「{exact.ticket_no}」当前状态「{_status_label(exact.status)}」，"
                    f"不能执行「{_intent_label(decision.intent)}」。"
                    f"如需继续处理请 #重开工单 并说明原因。",
                    message_id=msg.message_id,
                )
                return self._complete(item, msg, "REJECTED")

        # 选择工单：建立用户上下文（不走执行器）
        if decision.intent == "ticket.select":
            return self._handle_select(item, msg, decision, candidates)

        # 查询工单：列活动工单（不走执行器）
        if decision.intent == "ticket.query":
            return self._handle_query(item, msg, decision, candidates)

        # 路由
        route = self._router.route(
            message=msg,
            decision=decision,
            candidates=candidates,
            quoted_ticket_id=self._quoted_ticket_id(msg),
            selected_ticket_id=self._context.get_active(msg.group_id, msg.sender_id, datetime.now()),
        )
        logger.info(
            "消息路由 msg=%s route=%s link=%s target_ticket_id=%s candidates=%d",
            msg.message_id, route.decision.value, route.link_type,
            route.target_ticket_id, len(route.candidate_ticket_ids),
        )

        # ── 店长完工确认兜底（2026-09-03）：ticket.complete 在活动候选中
        #    无明确归属（CLARIFY / 单候选兜底会误关无关工单），但消息短编号
        #    唯一命中待确认工单时，按确认完工执行。实证 09-01：「009修好了」
        #    经单候选误关 -011，「010修好了」因无候选被拒，#66/#69 滞留。
        #    有显式「确认」措辞的消息已在 classifier 归一化，此处覆盖无确认词
        #    的「NNN修好了」形态；无编号/多编号歧义时保持原路由不变。──
        if (
            decision.intent == "ticket.complete"
            and msg.sender_role == ROLE_MANAGER
            and (route.decision != RouteDecision.ROUTED or route.link_type == LINK_SINGLE)
        ):
            fallback_decision = self._manager_complete_fallback(msg)
            if fallback_decision is not None:
                decision = fallback_decision
                candidates = [
                    c for c in self._repo.snapshot_group_tickets(msg.group_id)
                    if c.status == TICKET_PENDING_CONFIRM
                ]
                route = self._router.route(
                    message=msg,
                    decision=decision,
                    candidates=candidates,
                    quoted_ticket_id=self._quoted_ticket_id(msg),
                    selected_ticket_id=self._context.get_active(msg.group_id, msg.sender_id, datetime.now()),
                )
                logger.info(
                    "店长确认兜底命中 msg=%s target=%s",
                    msg.message_id, decision.target_ticket_no,
                )

        # ── 特殊情况归属守卫（2026-08-28）：消息开头显式提到无法解析的短编号
        #    （如「007今日胶未干…」而本群无 …-007）时，禁止静默落到选单上下文/
        #    单候选工单登记暂停——错误停表完全静默且会冻结无关工单，宁可多问一轮。
        #    显式编号/引用/尾缀命中等已被印证的归属不受影响。──
        if (
            route.decision == RouteDecision.ROUTED
            and decision.intent == "ticket.special_case.submit"
            and route.link_type in (LINK_CONTEXT, LINK_SINGLE)
        ):
            unresolved_no = _leading_unresolved_no(msg.content, candidates)
            if unresolved_no is not None:
                target_c = next(
                    (c for c in candidates if c.ticket_id == route.target_ticket_id), None
                )
                active_list = "、".join(c.ticket_no for c in candidates)
                suffix_hint = (
                    target_c.ticket_no.rsplit("-", 1)[-1] if target_c is not None else "工单号"
                )
                text = (
                    f"⚠️ 消息开头提到编号「{unresolved_no}」，但当前群活动工单中没有对应编号"
                    f"（{active_list}）。为避免记错工单，这条特殊情况未登记。"
                )
                text += self._explain_non_active_reference(msg, unresolved_no)
                text += (
                    f"如确认记到该工单，请带上工单号重发，"
                    f"如「{suffix_hint} 特殊情况：原因；预计恢复：时间」。"
                )
                self._notifier.send_group_now(
                    msg.group_id, text, message_id=msg.message_id
                )
                logger.info(
                    "特殊情况前导编号未解析 msg=%s no=%s", msg.message_id, unresolved_no
                )
                return self._complete(item, msg, "REJECTED")

        if route.decision == RouteDecision.CLARIFY:
            return self._create_clarify_pending(item, msg, decision, candidates, route)

        # 路由已确定目标（引用/上下文/语义/单候选）但消息未写编号 → 回填到 decision，
        # 使 validator 的必填 ticket_no 与状态校验能通过。
        if (
            route.decision == RouteDecision.ROUTED
            and decision.target_ticket_no is None
            and route.target_ticket_id is not None
        ):
            target_candidate = next(
                (c for c in candidates if c.ticket_id == route.target_ticket_id), None
            )
            if target_candidate is not None:
                decision = replace(decision, target_ticket_no=target_candidate.ticket_no)

        # 新建动作：不绑定任何既有工单（模型可能因候选列表而误带 target）
        if route.decision == RouteDecision.CREATE and decision.target_ticket_no is not None:
            decision = replace(decision, target_ticket_no=None)

        # 校验
        validate_candidates = [c for c in candidates if c.ticket_id == route.target_ticket_id]
        status, cmd, errors = validate_decision(
            decision, message=msg, candidates=validate_candidates, protocol=self._protocol
        )
        logger.info(
            "消息校验 msg=%s status=%s errors=%s",
            msg.message_id, status.value, "；".join(errors) if errors else "-",
        )
        if status == DecisionStatus.VALIDATION_REJECTED:
            # 自然语言分步补充：ticket.create 因缺少必填字段被拒 → 创建待补充草稿，下条消息可直接补缺失字段
            if decision.intent == "ticket.create" and _is_missing_fields_error(errors):
                return self._handle_incomplete_create(item, msg, decision, errors)
            return self._reject(item, msg, errors)
        if status == DecisionStatus.IGNORE:
            return self._complete(item, msg, "IGNORED")

        # 协议确认策略（如模型来源 complete/cancel/reopen ALWAYS）→ 待确认
        if status == DecisionStatus.WAITING_CONFIRMATION:
            return self._create_confirm_pending(item, msg, decision, cmd, route, validate_candidates)

        if self._mode == RuntimeMode.ASSISTED and decision.source == "SEMANTIC_MODEL":
            return self._create_confirm_pending(item, msg, decision, cmd, route, validate_candidates)

        # 执行
        result = self._executor.execute(cmd, message=msg)
        logger.info(
            "动作执行 msg=%s intent=%s result=%s ticket_id=%s version=%s",
            msg.message_id, cmd.intent, result.status, result.ticket_id, result.ticket_version,
        )
        self._complete(item, msg, "EXECUTED" if result.status == RESULT_OK else result.status)
        if result.status == RESULT_OK:
            # 维修方式带订单号 → 登记订单监控 + 共享表（v4.1 起不再自动延期，签收后计时）
            self._handle_order_submitted(cmd, result, msg)
            self._notifier.flush()
        else:
            self._notifier.send_group_now(
                msg.group_id, f"工单操作未完成（{result.status}），请重试或联系管理员。",
                message_id=msg.message_id,
            )
        return _INBOX_COMPLETED

    # ─────────────────────── 订单提交处理 ───────────────────────
    def _handle_order_submitted(self, cmd: Any, result: Any, msg: NormalizedMessage) -> None:
        """识别到订单号：登记订单到监控与共享表（不再自动延期，2026-08-14 用户决策）。

        等货期间工单时效照常计算，超时由工程师回 #超时原因；调度器监测到
        订单签收后开始计时维修并每日提醒直至完成。
        """
        from config import ORDER_STORE_TABLE_PATH
        from reconciling.order_store import append_order_row

        if cmd.intent != "ticket.repair_plan.submit" or result.status != RESULT_OK:
            return
        raw_order_nos = cmd.fields.get("order_nos") or [cmd.fields.get("order_no")]
        # 防御：模型可能返回 null/None → str(None)会变成"None"，必须先判空再转字符串
        order_nos: list[str] = []
        for _o in raw_order_nos:
            if _o is None:
                continue
            _s = str(_o).strip()
            if not _s or _s.lower() in ("none", "null", "nil"):
                continue
            order_nos.append(_s)
        if not order_nos or result.ticket_id is None:
            return
        ticket = self._db.get_ticket(result.ticket_id)
        if ticket is None:
            return

        registered: list[str] = []
        already_registered: list[str] = []
        for order_no in order_nos:
            # 已登记过 → 跳过，不重复登记
            if self._db.get_order_monitor(order_no) is not None:
                logger.info("订单已登记过，跳过 order=%s ticket=%s", order_no, ticket["ticket_no"])
                already_registered.append(order_no)
                continue
            self._db.upsert_order_monitor(
                order_id=order_no, ticket_id=result.ticket_id,
                store=ticket["store_name"], ticket_no=ticket["ticket_no"],
            )
            # 共享表写失败不阻断登记（2026-08-25 兜底）：订单留在 xlsx_synced=0，
            # 由调度器 scan_shared_table_resync 周期补写。
            try:
                append_order_row(
                    ORDER_STORE_TABLE_PATH, order_id=order_no,
                    store=ticket["store_name"], ticket_no=ticket["ticket_no"],
                )
            except Exception as exc:
                logger.error(
                    "共享表写入失败，待调度器补同步 order=%s ticket=%s err=%s",
                    order_no, ticket["ticket_no"], exc,
                )
            else:
                # 追加成功或该行本已存在 → 均视为已同步
                self._db.mark_order_xlsx_synced(order_no)
            registered.append(order_no)

        if not registered:
            # 全部订单已登记过 → 回执说明，不静默
            if already_registered:
                self._notifier.send_group_now(
                    msg.group_id,
                    f"📦 订单 {'、'.join(already_registered)} 已在其他工单登记过，无需重复登记。",
                    message_id=msg.message_id,
                )
            return

        # 静默化（2026-08-24 #2）：订单登记成功属纯告知，不再回执；
        # 后续「已发货/已签收/已关闭」状态变化由调度器按需通知。
        logger.info("订单已登记 orders=%s ticket=%s", "、".join(registered), ticket["ticket_no"])

    # ─────────────────────── 决策 ───────────────────────
    def _routing_candidate_states(self, intent: str) -> tuple[str, ...] | None:
        """本次意图的路由候选状态集合（None = 沿用「只要活动工单」）。

        以协议 ``allowed_ticket_states`` 为准，再与
        :data:`_ROUTING_IN_FLIGHT_STATES` 列出的「已有产品结论的在途状态」
        取交集——协议没放行的状态不会因本表被放开（协议仍是唯一事实来源），
        表里没列的意图也不会被顺带放宽。
        """
        extra = _ROUTING_IN_FLIGHT_STATES.get(intent)
        if not extra:
            return None
        action = self._protocol.get_action(intent)
        allowed = tuple(action.allowed_ticket_states) if action else ()
        states = tuple(s for s in extra if s in allowed)
        if not states:
            return None
        # 活动工单恒为候选；在途状态去重后追加
        return tuple(dict.fromkeys((TICKET_ACTIVE, TICKET_OVERDUE, *states)))

    async def _decide(self, msg: NormalizedMessage) -> SemanticDecision:
        # 关键词快路径已**永久停用**（2026-08-20 用户决策，2026-09-16 明确为永久）：
        # 所有消息（含 #报修 等显式关键词）统一交由云端模型判断，此处不再回退到
        # match_keyword。模型不可用时由 _is_model_fallback 走重试/死信，不会静默
        # 降级为本地规则。semantics/keyword_matcher.py 仅保留给离线评测
        # （semantics/evaluator.py、semantics/run_eval.py）与单测使用。
        # 订单提交已全面交由 AI 判断（避免纯数字手机号/资产号被本地正则误判为淘宝订单号）
        # 原本地订单号快路径已移除， bare order 等由模型识别为 ticket.repair_plan.submit
        # 候选选择快路径：消息是「2」「选2」「第二个」→ 选第 N 个活动工单
        selection_no = _extract_selection_number(msg.content)
        if selection_no is not None:
            candidates = self._repo.snapshot_candidates(msg.group_id)
            if 1 <= selection_no <= len(candidates):
                target = candidates[selection_no - 1]
                logger.info(
                    "选择快路径命中 msg=%s no=%d → %s",
                    msg.message_id, selection_no, target.ticket_no,
                )
                return SemanticDecision(
                    protocol_version=self._protocol.protocol_version,
                    source="local",
                    intent="ticket.select",
                    target_ticket_no=target.ticket_no,
                    intent_confidence=1.0,
                    evidence=(f"selection:{selection_no}",),
                )
        if not self._llm_enabled:
            logger.warning("模型未启用 msg=%s 走降级 retry（全AI架构下LLM为必选）", msg.message_id)
            return SemanticDecision(
                protocol_version=self._protocol.protocol_version,
                source="SEMANTIC_MODEL",
                intent="chat.ignore",
                target_ticket_no=None,
                intent_confidence=0.0,
                evidence=(f"model_fallback:llm_disabled:{msg.message_id}",),
            )
        candidates = self._repo.snapshot_candidates(msg.group_id)
        pending = self._pending.get_waiting(msg.group_id, msg.sender_id)
        history = self._db.list_recent_group_messages(
            msg.group_id, limit=8, exclude_message_id=msg.message_id
        )
        # 候选备件词表（辅助模型选取贴合该店该主题的备件名；权威过滤在执行期）
        parts_vocab = self._build_parts_vocab(msg.group_id, candidates)
        logger.info("调用云端模型 msg=%s candidates=%d history=%d parts_vocab=%d",
                    msg.message_id, len(candidates), len(history), len(parts_vocab or {}))
        classify_kwargs: dict[str, Any] = {}
        if parts_vocab and _accepts_parts_vocab(self._classifier):
            classify_kwargs["parts_vocab"] = parts_vocab
        return await self._classifier.classify(
            msg, candidates=candidates, pending_action=pending, history=history,
            **classify_kwargs,
        )

    def _build_parts_vocab(self, group_id: str, candidates) -> dict:
        """按群门店 + 候选工单主题构造备件候选词表；门店无表/异常静默返回 {}。"""
        try:
            group = self._db.get_group(group_id) if group_id else None
            store_name = (group or {}).get("store_name") if group else None
            subjects = [c.subject for c in candidates if getattr(c, "subject", None)]
            if not store_name or not subjects:
                return {}
            return _build_store_vocab(store_name, subjects)
        except Exception as exc:  # 词表只是辅助，任何失败都不得阻塞模型调用
            logger.warning("构造备件词表失败 group=%s err=%s", group_id, exc)
            return {}

    async def _archive_attachments(self, msg: NormalizedMessage) -> None:
        """归档消息图片（存储层）。失败只记日志，绝不阻塞业务处理。"""
        if self._archiver is None:
            return
        try:
            archived = await self._archiver.archive_message(msg.message_id)
            if archived:
                logger.info("消息图片归档完成 msg=%s archived=%d", msg.message_id, archived)
        except Exception as exc:
            logger.warning("消息图片归档异常 msg=%s err=%s", msg.message_id, exc)

    def _save_decision(self, msg: NormalizedMessage, decision: SemanticDecision) -> None:
        self._db.save_semantic_decision(
            msg.message_id,
            protocol_version=decision.protocol_version,
            source=decision.source,
            intent=decision.intent,
            target_ticket_no=decision.target_ticket_no,
            confidence=decision.intent_confidence,
            fields=decision.fields,
            missing_fields=decision.missing_fields,
            evidence=decision.evidence,
        )

    # ─────────────────────── 待确认回复 ───────────────────────
    async def _resolve_pending_reply(
        self, item: dict[str, Any], msg: NormalizedMessage, pending: Any, decision: SemanticDecision
    ) -> str | None:
        # 自然语言分步补充：上一条因缺字段的 ticket.create 草稿，当前消息补充缺失字段
        if pending.intent == "ticket.create":
            supplement = await self._try_supplement_create_pending(item, msg, pending, decision)
            if supplement is not None:
                return supplement
        if decision.intent == "system.confirm_pending_action":
            logger.info("待确认动作确认 msg=%s pending=%s intent=%s", msg.message_id, pending.id, pending.intent)
            return self._confirm_pending(item, msg, pending)
        if decision.intent == "system.reject_pending_action":
            logger.info("待确认动作拒绝 msg=%s pending=%s intent=%s", msg.message_id, pending.id, pending.intent)
            return self._reject_pending(item, msg, pending)
        if decision.intent == "system.correct_pending_action":
            logger.info("待确认动作修正 msg=%s pending=%s intent=%s", msg.message_id, pending.id, pending.intent)
            return self._correct_pending(item, msg, pending, decision)
        # 归属问询 pending：用户在「请选择工单」后回复数字/编号 → 归属到所选工单执行
        if len(pending.candidate_ticket_ids) > 1 and decision.intent == "ticket.select":
            logger.info("归属选择 msg=%s pending=%s target=%s",
                        msg.message_id, pending.id, decision.target_ticket_no)
            return self._assign_pending_target(item, msg, pending, decision)
        return None

    def _resolve_assign_targets(
        self, pending: Any, decision: SemanticDecision, msg: NormalizedMessage
    ) -> tuple[list[int], list[str]]:
        """归属问询目标解析（2026-08-27 设计 A）。

        分层：① 全编号精确（兼容现状）→ ② 消息原文纯数字 token：
        尾缀唯一匹配优先（去前导零对齐末段），其次按展示序号解释。
        返回 (目标 id 列表去重保序, 无法识别的 token 列表)；调用方保证原子性。
        """
        no_map: dict[str, int] = {}
        suffix_counts: dict[str, int] = {}
        suffix_owner: dict[str, int] = {}
        valid_ids: list[int] = []
        for tid in pending.candidate_ticket_ids:
            t = self._db.get_ticket(tid)
            if t is None:
                continue
            valid_ids.append(tid)
            no_map[t["ticket_no"]] = tid
            suf = t["ticket_no"].rsplit("-", 1)[-1].lstrip("0")
            if suf:
                suffix_counts[suf] = suffix_counts.get(suf, 0) + 1
                suffix_owner[suf] = tid

        target_no = decision.target_ticket_no
        if target_no and target_no in no_map:
            return [no_map[target_no]], []

        targets: list[int] = []
        unknown: list[str] = []
        seen: set[int] = set()
        # 仅收独立的数字段：字母数字混排串（订单号/资产号）不算编号
        for tok in re.findall(r"(?<![A-Za-z0-9])(\d{1,4})(?![A-Za-z0-9])", msg.content or ""):
            suf = tok.lstrip("0")
            tid: int | None = None
            if suf and suffix_counts.get(suf) == 1:
                tid = suffix_owner[suf]
            elif 1 <= int(tok) <= len(valid_ids):
                tid = valid_ids[int(tok) - 1]
            if tid is None or tid in seen:
                if tid is None and tok not in unknown:
                    unknown.append(tok)
                continue
            seen.add(tid)
            targets.append(tid)
        return targets, unknown

    def _assign_pending_target(
        self, item: dict[str, Any], msg: NormalizedMessage, pending: Any, decision: SemanticDecision
    ) -> str:
        """归属问询后用户选择工单：把待归属动作落到所选工单执行。"""
        targets, unknown = self._resolve_assign_targets(pending, decision, msg)
        if not targets or unknown:
            # 原子拒绝：任一编号无法识别即整批不执行（不猜），pending 存活可重选
            hint = f"\n无法识别：{'、'.join(unknown)}" if unknown else ""
            self._notifier.send_group_now(
                msg.group_id, f"没有找到对应工单，请重新选择。{hint}", message_id=msg.message_id
            )
            return self._complete(item, msg, "REJECTED")

        # 执行前逐张预检版本与状态允许集；任一冲突整体拒绝（镜像单目标语义）
        action_def = self._protocol.get_action(pending.intent)
        allowed_states = tuple(action_def.allowed_ticket_states) if action_def else ()
        checked: list[tuple[int, dict[str, Any]]] = []
        for tid in targets:
            ticket = self._db.get_ticket(tid)
            if ticket is None:
                continue
            expected = pending.expected_ticket_versions.get(tid)
            if (
                (expected is not None and ticket["version"] != expected)
                or (allowed_states and ticket["status"] not in allowed_states)
            ):
                self._notifier.send_group_now(
                    msg.group_id, "该工单状态已更新，请重新选择。", message_id=msg.message_id
                )
                return self._complete(item, msg, "REJECTED")
            checked.append((tid, ticket))
        if not checked:
            self._notifier.send_group_now(
                msg.group_id, "没有找到对应工单，请重新选择。", message_id=msg.message_id
            )
            return self._complete(item, msg, "REJECTED")
        targets = [tid for tid, _ in checked]

        if not self._pending.resolve(
            pending.id, pending.version, PendingActionStatus.CONFIRMED,
            msg.message_id, now=datetime.now()
        ):
            return self._complete(item, msg, "REJECTED")

        # 多选（2026-08-27 用户决策「一次读取多个」）：逐张执行同一待归属动作；
        # 成功保持静默（用户裁定不加成功回执），失败沿用既有兜底文案一次。
        results: list[Any] = []
        cmds: list[Any] = []
        for tid in targets:
            cmd = _command_from_pending(msg, pending, tid)
            results.append(
                self._executor.execute(
                    cmd, message=msg, pending_action_id=pending.id, pending_version=pending.version
                )
            )
            cmds.append(cmd)
        failures = [r for r in results if r.status != RESULT_OK]
        for cmd, result in zip(cmds, results):
            if result.status == RESULT_OK:
                self._handle_order_submitted(cmd, result, msg)
        if not failures:
            return self._complete_execution_result(item, msg, results[0])
        first_fail = failures[0].status
        self._complete(item, msg, "EXECUTED" if len(failures) < len(results) else first_fail)
        self._notifier.send_group_now(
            msg.group_id,
            f"工单操作未完成（{first_fail}），请重试或联系管理员。",
            message_id=msg.message_id,
        )
        return _INBOX_COMPLETED

    def _confirm_pending(self, item: dict[str, Any], msg: NormalizedMessage, pending: Any) -> str:
        """确认待执行动作：校验工单版本未变后执行。"""
        # 建单确认：无既有目标，直接按 pending 字段新建
        if pending.intent == "ticket.create":
            cmd = _command_from_pending(msg, pending, None)
            if not self._pending.resolve(
                pending.id, pending.version, PendingActionStatus.CONFIRMED,
                msg.message_id, now=datetime.now()
            ):
                return self._complete(item, msg, "REJECTED")
            result = self._executor.execute(
                cmd, message=msg, pending_action_id=pending.id, pending_version=pending.version
            )
            if result.status == RESULT_INTERNAL_ERROR:
                self._create_retry_pending(msg, pending)
            return self._complete_execution_result(item, msg, result)

        target_ids = pending.candidate_ticket_ids
        target_id = target_ids[0] if len(target_ids) == 1 else None
        if target_id is None:
            return self._create_clarify_pending_from(
                item, msg, pending.intent, pending.fields, pending.candidate_ticket_ids,
                "确认前请明确工单编号",
            )
        ticket = self._db.get_ticket(target_id)
        if ticket is None:
            return self._complete(item, msg, "REJECTED")
        # 版本冲突 → 重新确认
        expected = pending.expected_ticket_versions.get(target_id)
        if expected is not None and ticket["version"] != expected:
            self._notifier.send_group_now(
                msg.group_id, "该工单状态已更新，请重新确认后再操作。", message_id=msg.message_id
            )
            return self._complete(item, msg, "REJECTED")
        if not self._pending.resolve(
            pending.id, pending.version, PendingActionStatus.CONFIRMED, msg.message_id, now=datetime.now()
        ):
            return self._complete(item, msg, "REJECTED")

        cmd = _command_from_pending(msg, pending, target_id)
        result = self._executor.execute(
            cmd, message=msg, pending_action_id=pending.id, pending_version=pending.version
        )
        if result.status == RESULT_INTERNAL_ERROR:
            self._create_retry_pending(msg, pending)
        return self._complete_execution_result(item, msg, result)

    def _create_retry_pending(self, msg: NormalizedMessage, pending: Any) -> None:
        """执行器内部失败时保留可再次确认的草稿。"""
        decision = SemanticDecision(
            protocol_version=self._protocol.protocol_version,
            source="PENDING_RETRY",
            intent=pending.intent,
            target_ticket_no=None,
            intent_confidence=1.0,
            fields=dict(pending.fields),
            requires_confirmation=True,
            evidence=("executor_failure_retry",),
        )
        draft = PendingActionDraft(
            source_message_id=msg.message_id,
            group_id=pending.group_id,
            user_id=pending.user_id,
            decision=decision,
            expected_ticket_versions=dict(pending.expected_ticket_versions),
            expires_at=datetime.now(),
        )
        self._pending.create_or_supersede(draft, datetime.now())

    def _reject_pending(self, item: dict[str, Any], msg: NormalizedMessage, pending: Any) -> str:
        resolved = self._pending.resolve(
            pending.id, pending.version, PendingActionStatus.REJECTED, msg.message_id, now=datetime.now()
        )
        if not resolved:
            self._notifier.send_group_now(
                msg.group_id, "该待办已处理或已失效，请重新发起操作。", message_id=msg.message_id
            )
            return self._complete(item, msg, "REJECTED")
        self._notifier.send_group_now(msg.group_id, "已取消该操作。", message_id=msg.message_id)
        return self._complete(item, msg, "REJECTED")

    def _correct_pending(
        self, item: dict[str, Any], msg: NormalizedMessage, pending: Any, decision: SemanticDecision
    ) -> str:
        """修正待确认动作字段后重新执行。"""
        merged = dict(pending.fields)
        merged.update(decision.fields)
        target_ids = pending.candidate_ticket_ids
        target_id = target_ids[0] if len(target_ids) == 1 else None
        if target_id is None:
            return self._create_clarify_pending_from(
                item, msg, pending.intent, merged, pending.candidate_ticket_ids, "请明确工单编号",
            )
        if not self._pending.resolve(
            pending.id, pending.version, PendingActionStatus.CONFIRMED, msg.message_id, now=datetime.now()
        ):
            self._notifier.send_group_now(
                msg.group_id, "该待办已处理或已失效，请重新发起操作。", message_id=msg.message_id
            )
            return self._complete(item, msg, "REJECTED")
        cmd = _command_from_pending(msg, pending, target_id, fields=merged)
        result = self._executor.execute(
            cmd, message=msg, pending_action_id=pending.id, pending_version=pending.version
        )
        return self._complete_execution_result(item, msg, result)

    # ─────────────────────── 澄清/确认待办 ───────────────────────
    def _handle_clarify(
        self, item: dict[str, Any], msg: NormalizedMessage, decision: SemanticDecision
    ) -> str:
        """消息有歧义 → 用可读文案直接请用户澄清，不建待确认。"""
        text = "⚠️ 这条消息有歧义，我无法确定你要做什么。"
        if decision.evidence:
            text += f"\n涉及：{'、'.join(decision.evidence)}"
        text += "\n请明确说明要执行的操作（如「报修」「完成」「取消」等）。"
        self._notifier.send_group_now(msg.group_id, text, message_id=msg.message_id)
        return self._complete(item, msg, "CLARIFY")

    def _create_clarify_pending(
        self,
        item: dict[str, Any],
        msg: NormalizedMessage,
        decision: SemanticDecision,
        candidates: list[Any],
        route: Any,
    ) -> str:
        ids = tuple(c.ticket_id for c in candidates)
        if not ids:
            text = "当前没有可操作的活动工单，请先创建工单。"
            self._notifier.send_group_now(msg.group_id, text, message_id=msg.message_id)
            return self._complete(item, msg, "CLARIFY")
        # 多候选归属问询 → 建立 pending，记录待归属动作与候选工单，
        # 使后续用户回复数字/编号能被识别为「归属选择」而非选单上下文。
        versions = {c.ticket_id: c.version for c in candidates}
        draft = PendingActionDraft(
            source_message_id=msg.message_id,
            group_id=msg.group_id,
            user_id=msg.sender_id,
            decision=decision,
            expected_ticket_versions=versions,
            expires_at=datetime.now(),
        )
        self._pending.create_or_supersede(draft, datetime.now())
        lines = "\n".join(f"{i + 1}. {c.ticket_no}（{c.subject}）" for i, c in enumerate(candidates))
        text = f"消息对应多张工单，请选择或提供编号：\n{lines}"
        self._notifier.send_group_now(msg.group_id, text, message_id=msg.message_id)
        return self._complete(item, msg, "CLARIFY")

    def _create_clarify_pending_from(
        self, item: dict[str, Any], msg: NormalizedMessage, intent: str, fields: dict[str, Any],
        candidate_ids: tuple[int, ...], text: str,
    ) -> str:
        self._notifier.send_group_now(msg.group_id, text, message_id=msg.message_id)
        return self._complete(item, msg, "CLARIFY")

    def _create_confirm_pending(
        self,
        item: dict[str, Any],
        msg: NormalizedMessage,
        decision: SemanticDecision,
        cmd: Any,
        route: Any,
        validate_candidates: list[Any],
    ) -> str:
        """协议确认策略 / 辅助模式：创建待确认动作。"""
        target_id = cmd.target_ticket_id if cmd is not None else route.target_ticket_id
        target = next((c for c in validate_candidates if c.ticket_id == target_id), None)
        versions = {target.ticket_id: target.version} if target else {}
        draft = PendingActionDraft(
            source_message_id=msg.message_id,
            group_id=msg.group_id,
            user_id=msg.sender_id,
            decision=decision,
            expected_ticket_versions=versions,
            expires_at=datetime.now(),
        )
        self._pending.create_or_supersede(draft, datetime.now())
        ticket_no = decision.target_ticket_no or (target.ticket_no if target else "该工单")
        from tickets.commands import intent_label

        label = intent_label(decision.intent)
        self._notifier.send_group_now(
            msg.group_id, f"确认执行「{label}」（{ticket_no}）？回复「确认」继续。",
            message_id=msg.message_id,
        )
        return self._complete(item, msg, "WAITING_CONFIRMATION")

    def _manager_complete_fallback(self, msg: NormalizedMessage) -> SemanticDecision | None:
        """店长「NNN修好了」确认兜底：短编号唯一命中待确认工单 → confirm_complete 决策。

        约束：消息中恰好一个短编号（≥2 位）唯一对应一张 PENDING_CONFIRM 工单
        时才兜底；无编号、多编号、尾缀歧义一律返回 None（保持原路由，
        避免把「008修好了」类直接完工误转为确认）。
        """
        numbers = re.findall(r"\d{2,}", msg.content or "")
        normalized = [n.lstrip("0") for n in numbers]
        if len(normalized) != 1 or not normalized[0]:
            return None
        pending = [
            c for c in self._repo.snapshot_group_tickets(msg.group_id)
            if c.status == TICKET_PENDING_CONFIRM
        ]
        matches = [
            c for c in pending
            if c.ticket_no.rsplit("-", 1)[-1].lstrip("0") == normalized[0]
        ]
        if len(matches) != 1:
            return None
        return SemanticDecision(
            protocol_version=self._protocol.protocol_version,
            source="local",
            intent="ticket.confirm_complete",
            target_ticket_no=matches[0].ticket_no,
            intent_confidence=0.9,
            fields={},
            evidence=(f"manager_complete_fallback:{numbers[0]}",),
        )

    def _explain_non_active_reference(self, msg: NormalizedMessage, hint: str) -> str:
        """编号指向本群非活动工单（待确认/终态）时返回解释性文案，否则空串。

        编号解析失败常因目标不在活动候选集（如待店长确认、已完结），
        与其笼统报「没有找到」，不如直接说明该编号对应哪张单、什么状态
        （2026-08-28：事故「财阀的007号单」指向待店长确认工单）。
        """
        from tickets.commands import ticket_status_label as _status_label

        target, _status = _resolve_ticket_reference(
            hint, self._repo.snapshot_group_tickets(msg.group_id)
        )
        if target is None or target.status in ("ACTIVE", "ACTIVE_OVERDUE"):
            return ""
        return (
            f"编号对应工单 {target.ticket_no}（{_status_label(target.status)}），"
            f"不是当前可操作的活动工单。"
        )

    def _handle_select(
        self, item: dict[str, Any], msg: NormalizedMessage, decision: SemanticDecision, candidates: list[Any]
    ) -> str:
        target = None
        resolve_status = "NOT_FOUND"
        if decision.target_ticket_no:
            # 编号解析（2026-08-28）：全编号精确 → 短编号尾缀唯一匹配。
            # 此前仅精确匹配，「007号单」即使本群存在 …-007 也会误报没有找到。
            target, resolve_status = _resolve_ticket_reference(
                decision.target_ticket_no, candidates
            )
            # 编号印证守卫（2026-08-28）：模型可能违反提示词把用户短编号
            # （如「007号单」）展开成候选完整编号（如 …-005）。消息存在显式
            # 工单指代且与解析目标尾号不符 → 按「没有找到」拒绝，绝不静默切错。
            mentioned = _mentioned_ticket_numbers(msg.content)
            if (
                target is not None
                and mentioned
                and target.ticket_no.rsplit("-", 1)[-1].lstrip("0") not in mentioned
            ):
                logger.info(
                    "选单编号印证失败 msg=%s mentioned=%s target=%s",
                    msg.message_id, sorted(mentioned), target.ticket_no,
                )
                target, resolve_status = None, "MISMATCH"
        elif len(candidates) == 1 and not _mentioned_ticket_numbers(msg.content):
            # 单候选兜底仅适用于无编号指代（如「就切到这张」）；消息显式提到
            # 「XX号单」但模型未返回编号 → 不猜测，明确拒绝（2026-08-28）。
            target = candidates[0]
        if target is None:
            text = (
                "该编号匹配多张工单，请提供完整工单编号。"
                if resolve_status == "AMBIGUOUS"
                else "没有找到该工单，请检查编号。"
            )
            if decision.target_ticket_no:
                text += self._explain_non_active_reference(msg, decision.target_ticket_no)
            self._notifier.send_group_now(
                msg.group_id, text, message_id=msg.message_id
            )
            return self._complete(item, msg, "REJECTED")
        self._context.select(
            msg.group_id, msg.sender_id, target.ticket_id,
            order_key=f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}|{msg.message_id}",
            now=datetime.now(),
        )
        # 选单成功需回执确认（用户要求 2026-08-25）：让用户明确知道后续消息归属哪张工单
        self._notifier.send_group_now(
            msg.group_id,
            f"✅ 已切换到工单 {target.ticket_no}（{target.subject}），"
            f"30 分钟内你的消息默认归到这张工单。",
            message_id=msg.message_id,
        )
        return self._complete(item, msg, "EXECUTED")

    def _handle_query(
        self, item: dict[str, Any], msg: NormalizedMessage, decision: SemanticDecision, candidates: list[Any]
    ) -> str:
        """查询工单：无编号时列出当前活动工单。"""
        if decision.target_ticket_no:
            # 编号解析与选单一致（2026-08-28）：全编号精确 → 尾缀唯一，
            # 「查007号单」可命中已完结的 …-007，不再误报没有找到。
            target, resolve_status = _resolve_ticket_reference(
                decision.target_ticket_no, candidates
            )
            if target is None:
                text = (
                    "该编号匹配多张工单，请提供完整工单编号。"
                    if resolve_status == "AMBIGUOUS"
                    else "没有找到该工单，请检查编号。"
                )
                self._notifier.send_group_now(
                    msg.group_id, text, message_id=msg.message_id
                )
                return self._complete(item, msg, "REJECTED")
            ticket = self._db.get_ticket(target.ticket_id)
            from tickets.commands import ticket_status_label as _status_label

            deadline = ticket.get("current_deadline_at")
            deadline_text = (
                f"预计完成：{deadline}" if deadline else "时效：待商榷（暂不设截止时间）"
            )
            text = (
                f"📋 {ticket['ticket_no']}  {_status_label(ticket['status'])}\n"
                f"主题：{ticket['subject']}\n位置：{ticket['location']}\n"
                f"问题：{ticket['problem_description'][:80]}\n"
                f"{deadline_text}"
            )
            self._notifier.send_group_now(msg.group_id, text, message_id=msg.message_id)
            return self._complete(item, msg, "EXECUTED")

        if not candidates:
            self._notifier.send_group_now(
                msg.group_id, "当前没有活动工单。", message_id=msg.message_id
            )
            return self._complete(item, msg, "EXECUTED")

        lines = "\n".join(
            f"- {c.ticket_no}：{c.subject} @ {c.location}（{c.status}）"
            + (f" — {c.problem_summary}" if getattr(c, 'problem_summary', '') else "")
            for c in candidates
        )
        self._notifier.send_group_now(
            msg.group_id, f"当前活动工单：\n{lines}", message_id=msg.message_id
        )
        return self._complete(item, msg, "EXECUTED")

    def _handle_image_attachment(self, item: dict[str, Any], msg: NormalizedMessage) -> str:
        """图片消息：归档后归属到工单（引用/选单上下文/单候选），并触发多模态解析。

        图片消息与文本一样是普通消息，不单独发送回执；归属工单后由
        视觉模型解析内容写入附件表，供工单记录/导出展示。
        """
        candidates = self._repo.snapshot_candidates(msg.group_id)
        target_id = None

        # 1. 钉钉回复/引用某条消息 → 归属被引用消息关联的工单
        quoted_id = self._quoted_ticket_id(msg)
        if quoted_id is not None:
            target_id = quoted_id

        # 2. 用户 30 分钟内选过的工单（上下文）
        if target_id is None:
            target_id = self._context.get_active(msg.group_id, msg.sender_id, datetime.now())

        # 3. 只有一张活动工单 → 直接归属
        if target_id is None and len(candidates) == 1:
            target_id = candidates[0].ticket_id

        if target_id is None:
            # 无法确定归属：让用户选择，但仍需解析图片内容（不阻塞工单归属）
            self._schedule_vision_analysis(msg.message_id)
            if candidates:
                lines = "\n".join(
                    f"{i + 1}. {c.ticket_no}（{c.subject}）" for i, c in enumerate(candidates)
                )
                self._notifier.send_group_now(
                    msg.group_id,
                    f"收到图片，请选择归属工单：\n{lines}\n回复数字即可。",
                    message_id=msg.message_id,
                )
            else:
                self._notifier.send_group_now(
                    msg.group_id, "收到图片，但当前没有活动工单可归属。", message_id=msg.message_id
                )
            return self._complete(item, msg, "COMPLETED")

        self._db.backfill_attachment_ticket(msg.message_id, target_id)
        logger.info("图片归属工单 msg=%s ticket_id=%s", msg.message_id, target_id)

        # 触发多模态解析（异步，失败不阻塞）
        self._schedule_vision_analysis(msg.message_id)
        return self._complete(item, msg, "EXECUTED")

    def _schedule_vision_analysis(self, message_id: str) -> None:
        """触发图片多模态解析（后台任务，失败不阻塞业务）。"""
        try:
            from images.vision import VisionAnalyzer

            async def _run() -> None:
                try:
                    analyzer = VisionAnalyzer(db=self._db)
                    await analyzer.analyze_message(message_id)
                except Exception as exc:
                    logger.warning("图片解析异常 msg=%s err=%s", message_id, exc)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(_run())
                self._vision_tasks.append(task)
                task.add_done_callback(self._remove_vision_task)
            else:
                asyncio.run(_run())
        except Exception as exc:
            logger.warning("图片解析任务调度失败 msg=%s err=%s", message_id, exc)

    def _remove_vision_task(self, task: asyncio.Task) -> None:
        try:
            self._vision_tasks.remove(task)
        except ValueError:
            pass

    # ─────────────────────── 收尾 ───────────────────────
    def _quoted_ticket_id(self, msg: NormalizedMessage) -> int | None:
        if not msg.reply_to_message_id:
            return None
        return self._db.get_quoted_ticket_id(msg.reply_to_message_id)

    def _retry_or_dead(self, item: dict[str, Any], msg: NormalizedMessage, error: str) -> str:
        attempts = (item.get("attempts") or 0) + 1
        if attempts >= self._max_attempts:
            self._db.inbox_set_status(
                msg.message_id, _INBOX_DEAD, last_error=error, attempts=attempts
            )
            self._notifier.send_group_now(
                msg.group_id, "智能识别暂时不可用，本条消息未执行，请稍后重试或联系管理员。",
                message_id=msg.message_id,
            )
            return _INBOX_DEAD
        delay = self._retry_delays[min(attempts - 1, len(self._retry_delays) - 1)]
        next_at = _add_seconds(datetime.now(), delay)
        self._db.inbox_set_status(
            msg.message_id, _INBOX_RETRY, attempts=attempts,
            last_error=error, next_attempt_at=next_at,
        )
        logger.info("模型失败，进入重试 message_id=%s attempt=%d/%d delay=%ss",
                    msg.message_id, attempts, self._max_attempts, delay)
        return _INBOX_RETRY

    def _handle_incomplete_create(
        self, item: dict[str, Any], msg: NormalizedMessage, decision: SemanticDecision, errors: tuple[str, ...]
    ) -> str:
        """ticket.create 因缺字段被拒 → 创建待补充草稿，下条消息可直接补缺失字段。"""
        draft = PendingActionDraft(
            source_message_id=msg.message_id,
            group_id=msg.group_id,
            user_id=msg.sender_id,
            decision=decision,
            expected_ticket_versions={},
            expires_at=datetime.now(),
        )
        self._pending.create_or_supersede(draft, datetime.now())
        self._db.inbox_set_status(msg.message_id, _INBOX_COMPLETED, processed_result="REJECTED")
        self._db.record_processed_event(msg.message_id, msg.group_id, "REJECTED")
        self._save_decision(msg, decision)
        text = "无法执行：" + "；".join(errors) + "\n请直接回复缺失的信息（如 时效：3天）即可补齐，无需重发全部内容。"
        self._notifier.send_group_now(msg.group_id, text, message_id=msg.message_id)
        return _INBOX_COMPLETED

    async def _try_supplement_create_pending(
        self, item: dict[str, Any], msg: NormalizedMessage, pending: Any, decision: SemanticDecision
    ) -> str | None:
        """尝试将当前消息作为对 pending ticket.create 草稿的补充。"""
        # 仅当 pending 是 ticket.create 且存储了部分字段时才尝试补充
        if pending.intent != "ticket.create":
            return None
        # 若当前决策是无关意图（如查询/取消），不视为补充，让正常流程处理
        if decision.intent not in ("ticket.create", "chat.ignore", "system.clarify"):
            # 但若消息本身含有可补充字段（如单独的时效），仍尝试提取
            supplement = _extract_supplement_fields(msg.content)
            if not supplement:
                return None
            # 视为补充，构造补充决策
            decision = SemanticDecision(
                protocol_version=self._protocol.protocol_version,
                source="SEMANTIC_MODEL",
                intent="ticket.create",
                target_ticket_no=None,
                intent_confidence=0.8,
                fields=supplement,
                evidence=("supplement_extract",),
            )
        # 合并草稿字段与当前决策字段
        merged: dict[str, Any] = dict(pending.fields or {})
        # 当前决策字段覆盖草稿
        for k, v in (decision.fields or {}).items():
            if v not in (None, "", []):
                merged[k] = v
        # 本地兜底提取：裸 "3天"/"待商榷" 等时效
        local = _extract_supplement_fields(msg.content)
        for k, v in local.items():
            if k not in merged or not merged[k]:
                merged[k] = v
        if not merged:
            return None
        # 若合并后仍无实质新信息（如用户发闲聊），不消耗草稿
        new_keys = set(merged.keys()) - set(pending.fields.keys() or {})
        has_new_value = any(merged.get(k) != (pending.fields or {}).get(k) for k in merged)
        if not new_keys and not has_new_value:
            # 若当前决策是 ticket.create 且有字段，至少算有补充意图
            if decision.intent != "ticket.create":
                return None
        # 构造合成的 ticket.create 决策
        synthetic = SemanticDecision(
            protocol_version=decision.protocol_version,
            source="SEMANTIC_MODEL",
            intent="ticket.create",
            target_ticket_no=None,
            intent_confidence=max(0.6, decision.intent_confidence),
            fields=merged,
            evidence=tuple(list(pending.fields.keys()) + ["supplement"]),
        )
        # 校验合成决策
        status, cmd, errors = validate_decision(
            synthetic, message=msg, candidates=[], protocol=self._protocol
        )
        if status == DecisionStatus.VALIDATION_REJECTED and _is_missing_fields_error(errors):
            # 仍缺字段 → 更新草稿并再次提示
            draft = PendingActionDraft(
                source_message_id=msg.message_id,
                group_id=msg.group_id,
                user_id=msg.sender_id,
                decision=synthetic,
                expected_ticket_versions={},
                expires_at=datetime.now(),
            )
            self._pending.create_or_supersede(draft, datetime.now())
            self._db.inbox_set_status(msg.message_id, _INBOX_COMPLETED, processed_result="REJECTED")
            self._db.record_processed_event(msg.message_id, msg.group_id, "REJECTED")
            self._save_decision(msg, synthetic)
            text = "无法执行：" + "；".join(errors) + "\n请继续补充缺失信息。"
            self._notifier.send_group_now(msg.group_id, text, message_id=msg.message_id)
            return _INBOX_COMPLETED
        if status == DecisionStatus.VALIDATION_REJECTED:
            # 其他校验失败（如权限），按正常拒绝处理，不消耗草稿的补充逻辑
            return None
        if status == DecisionStatus.IGNORE:
            return None
        if self._mode == RuntimeMode.ASSISTED:
            self._save_decision(msg, synthetic)
            return self._create_confirm_pending(item, msg, synthetic, cmd, None, [])
        # 校验通过 → 先执行；只在建单成功后才终结草稿。
        self._save_decision(msg, synthetic)
        result = self._executor.execute(cmd, message=msg)
        logger.info("补充建单执行 msg=%s result=%s ticket_id=%s", msg.message_id, result.status, result.ticket_id)
        if result.status == RESULT_OK:
            self._pending.resolve(
                pending.id, pending.version, PendingActionStatus.CONFIRMED,
                msg.message_id, now=datetime.now(),
            )
        else:
            retry_draft = PendingActionDraft(
                source_message_id=msg.message_id,
                group_id=msg.group_id,
                user_id=msg.sender_id,
                decision=synthetic,
                expected_ticket_versions={},
                expires_at=datetime.now(),
            )
            self._pending.create_or_supersede(retry_draft, datetime.now())
        return self._complete_execution_result(item, msg, result)

    def _reject(self, item: dict[str, Any], msg: NormalizedMessage, errors: tuple[str, ...]) -> str:
        self._db.inbox_set_status(msg.message_id, _INBOX_COMPLETED, processed_result="REJECTED")
        self._db.record_processed_event(msg.message_id, msg.group_id, "REJECTED")
        text = "无法执行：" + "；".join(errors)
        self._notifier.send_group_now(msg.group_id, text, message_id=msg.message_id)
        return _INBOX_COMPLETED

    def _complete_execution_result(self, item: dict[str, Any], msg: NormalizedMessage, result: Any) -> str:
        """按执行器真实结果终结收件箱，并统一投递成功/失败反馈。"""
        processed_result = "EXECUTED" if result.status == RESULT_OK else result.status
        self._complete(item, msg, processed_result)
        if result.status == RESULT_OK:
            self._notifier.flush()
        else:
            self._notifier.send_group_now(
                msg.group_id,
                f"工单操作未完成（{result.status}），请重试或联系管理员。",
                message_id=msg.message_id,
            )
        return _INBOX_COMPLETED

    def _complete(self, item: dict[str, Any] | None, msg: NormalizedMessage | None, result: str) -> str:
        if item is not None:
            self._db.inbox_set_status(item["message_id"], _INBOX_COMPLETED, processed_result=result)
        # 除影子模式外，所有终态都记幂等台账（防同 message_id 重复处理）
        if msg is not None and result != "SHADOW":
            self._db.record_processed_event(msg.message_id, msg.group_id, result)
        return _INBOX_COMPLETED


def _is_missing_fields_error(errors: tuple[str, ...] | list[str]) -> bool:
    """是否全为缺字段错误（可通过补充解决）。"""
    if not errors:
        return False
    return all("缺少" in e for e in errors)


def _extract_supplement_fields(content: str) -> dict[str, Any]:
    """从补充消息中本地兜底提取可补字段（时效/主题等），与模型互补。"""
    if not content:
        return {}
    fields: dict[str, Any] = {}
    text = content.strip()
    # 时效：裸 "3天" / "时效：3天" / "时效(7天)"
    m = re.search(r"时效\s*[:：]?\s*(1天|3天|7天|待商榷)", text)
    if m:
        fields["sla"] = m.group(1)
    elif re.fullmatch(r"\s*(1天|3天|7天|待商榷)\s*", text):
        fields["sla"] = text.strip()
    elif re.search(r"(?:^|\s)(1天|3天|7天|待商榷)(?:\s|$|，|。|；)", text):
        # 裸时效词在短消息中（如"3天"）
        mm = re.search(r"(1天|3天|7天|待商榷)", text)
        if mm and len(text.strip()) <= 10:
            fields["sla"] = mm.group(1)
    # 主题/位置/问题描述 的显式键值（若用户补充时带键名）
    for key, field in [("主题", "subject"), ("位置", "location"), ("问题描述", "problem_description")]:
        mm = re.search(rf"{key}\s*[:：]\s*([^\n，；;]+)", text)
        if mm:
            val = mm.group(1).strip()
            if val:
                fields[field] = val
    return fields


# ─────────────────────── 工具 ───────────────────────

# 订单号识别：从消息中提取全部淘宝订单号（纯数字 15+ 位，或含字母连字符的订单串）
_ORDER_NO_RE = re.compile(r"(?<![A-Za-z0-9-])[A-Za-z0-9-]{6,64}(?![A-Za-z0-9-])")
_STRIP_PUNCT = "，。！？、；：…,.;:!? "

# 诊断提示词：判断消息其余部分像不像故障判断
_DIAGNOSIS_CUES = ("判断", "估计", "应该", "可能", "坏", "故障", "漏", "打不开", "不亮", "失灵", "松动", "断开", "断")


def _extract_order_numbers(content: str) -> list[str]:
    """从消息中提取全部订单号（去重、保序）。至少含 6 个数字才算。"""
    result: list[str] = []
    for token in _ORDER_NO_RE.findall(content or ""):
        if sum(ch.isdigit() for ch in token) >= 6 and token not in result:
            result.append(token)
    return result


def _extract_diagnosis_text(content: str, order_numbers: list[str]) -> str | None:
    """从消息里剥离订单号/采购表述后，按标点切段，保留像故障判断的段落。"""
    text = content or ""
    for ono in order_numbers:
        text = text.replace(ono, " ")
    for word in ("淘宝订单号", "订单号", "单号", "采购", "买了", "购买", "下单"):
        text = text.replace(word, " ")
    segments = re.split(r"[，。！？、；：,\n]", text)
    kept = [
        seg.strip(_STRIP_PUNCT)
        for seg in segments
        if len(seg.strip(_STRIP_PUNCT)) >= 2
        and any(cue in seg for cue in _DIAGNOSIS_CUES)
    ]
    return "；".join(kept) if kept else None


_CHINESE_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                   "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _resolve_ticket_reference(hint: str, candidates: list[Any]) -> tuple[Any | None, str]:
    """工单编号引用解析（2026-08-28）：① 全编号精确 → ② 短编号尾缀唯一匹配。

    返回 (候选, 状态)，状态 ∈ EXACT / SUFFIX / AMBIGUOUS / NOT_FOUND。
    尾缀规则与 TicketRouter._route_by_suffix 对齐：工单号形如 店名-主题-时效-005，
    取末段去前导零对齐，数字至少 2 位。
    """
    hint = (hint or "").strip()
    exact = next((c for c in candidates if c.ticket_no == hint), None)
    if exact is not None:
        return exact, "EXACT"
    for num in re.findall(r"\d{2,}", hint):
        normalized = num.lstrip("0")
        matches = [
            c for c in candidates
            if c.ticket_no.rsplit("-", 1)[-1].lstrip("0") == normalized
        ]
        if len(matches) == 1:
            return matches[0], "SUFFIX"
        if len(matches) > 1:
            return None, "AMBIGUOUS"
    return None, "NOT_FOUND"


_TICKET_REF_RE = re.compile(r"(\d{2,4})(?!\s*号?机)[^，。,、；;\dA-Za-z]{0,4}工?单")


def _mentioned_ticket_numbers(content: str) -> set[str]:
    """消息中「编号+单」形态的显式工单指代（如「007号单」「007那张单」）。

    返回去前导零后的编号集合。仅匹配紧跟「单」结尾的 2-4 位数字段；
    「12号机」等设备指代不算工单编号。用于选单编号印证与单候选兜底防误切
    （2026-08-28：事故「财阀的007号单」被静默切到 …-005）。
    """
    return {m.lstrip("0") for m in _TICKET_REF_RE.findall(content or "")}


_LEADING_NO_RE = re.compile(r"^\s*(?:工单|#|第|-)?\s*(\d{2,4})(?![\dA-Za-z-])")


def _leading_unresolved_no(content: str, candidates: list[Any]) -> str | None:
    """消息开头显式短编号未指向任何候选工单时返回该编号（特殊情况归属守卫）。

    「独立」= 后面不紧跟数字/字母/连字符，排除订单号、资产号等长串；
    「007今日胶未干…」而本群无尾缀 007 的活动工单 → 返回 "007"。
    """
    m = _LEADING_NO_RE.match(content or "")
    if not m:
        return None
    normalized = m.group(1).lstrip("0")
    if not normalized:
        return None
    for c in candidates:
        if c.ticket_no.rsplit("-", 1)[-1].lstrip("0") == normalized:
            return None
    return m.group(1)


def _extract_selection_number(content: str) -> int | None:
    """消息是否是一个候选序号选择（如「2」「选2」「2号」「第二个」），是则返回序号。"""
    text = (content or "").strip()
    m = re.fullmatch(r"(?:选|选择|第)?\s*(\d{1,2})\s*(?:号|个|张)?", text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 20:
            return n
    m = re.fullmatch(r"(?:选|选择|第)?\s*([一二两三四五六七八九十]{1,2})\s*(?:号|个|张)?", text)
    if m:
        digits = [d for d in m.group(1) if d in _CHINESE_DIGITS]
        if len(digits) == 1:
            n = _CHINESE_DIGITS[digits[0]]
            if 1 <= n <= 20:
                return n
    return None


def _row_to_message(row: dict[str, Any]) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=row["message_id"],
        group_id=row["group_id"],
        sender_id=row["sender_id"],
        sender_name=row.get("sender_id", ""),
        content=row.get("content", ""),
        message_type=row.get("message_type", "text"),
        sent_at=parse_naive_dt(row.get("sent_at", "")) or datetime.now(),
        sender_role=row.get("sender_role", "UNKNOWN"),
        reply_to_message_id=row.get("reply_to_message_id"),
    )


def _command_from_pending(msg: NormalizedMessage, pending: Any, target_id: int,
                          fields: dict[str, Any] | None = None) -> Any:
    from semantics.types import ValidatedCommand

    return ValidatedCommand(
        message_id=msg.message_id,
        group_id=msg.group_id,
        actor_id=msg.sender_id,
        actor_role=msg.sender_role,
        intent=pending.intent,
        target_ticket_id=target_id,
        expected_ticket_version=pending.expected_ticket_versions.get(target_id),
        fields=dict(fields if fields is not None else pending.fields),
        source="model",
    )


def _add_seconds(now: datetime, seconds: float) -> str:
    from datetime import timedelta

    return (now + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
