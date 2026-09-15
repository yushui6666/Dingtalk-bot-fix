"""工单命令执行器（计划书 §9、Task 9）。

在一个 SQLite 短事务内完成：
  唯一 action_executions → 业务变更(CAS) → 消息归属 → 消息落库 →
  processed_events 幂等 → 通知 Outbox 预写 → 执行终态 APPLIED。

乐观版本冲突返回 CommandResult(REJECTED)，不覆盖他人更新。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from db import Database
from logger import get_logger
from models import (
    ROLE_MANAGER,
    TICKET_ACTIVE,
    TICKET_CANCELLED,
    TICKET_COMPLETED,
    TICKET_NEGOTIATING,
    TICKET_PENDING_CONFIRM,
    TICKET_STOPPED,
    NormalizedMessage,
)
from semantics.types import CommandResult, ValidatedCommand
from store_parts import build_pool, keep_only_matched
from tickets.commands import SILENT_INTENTS, reply_text
from tickets.repository import TicketRepository
from tickets.timeexpr import parse_resume_time

logger = get_logger(__name__)

LINK_CREATE = "CREATE"
LINK_EXECUTED = "EXECUTED"
RESULT_OK = "OK"
RESULT_REJECTED = "REJECTED"
RESULT_INTERNAL_ERROR = "INTERNAL_ERROR"

# 只读/终态意图：不参与责任方切换（计划书 §9.3：仅有效人工业务消息切换）。
# 2026-08-24：complete 的责任方切换由 _execute_complete 按角色显式处理
# （工程师报完工 → 切等店长；店长直接完成 → 关闭全部周期），不走 finalize 通用切换。
_READONLY_INTENTS = frozenset({"ticket.query", "ticket.select", "ticket.cancel", "ticket.stop"})
# 不参与 finalize 通用责任方切换的意图（自行处理或无需切换）
_NO_SWITCH_INTENTS = frozenset({
    "ticket.confirm_complete", "ticket.complete",
    "ticket.negotiate.submit",
    # 特殊情况声明（2026-08-26）：等待方答复「暂时无法推进」，责任仍在原方；
    # 暂停期间响应 SLA 由调度器豁免，不需要把责任切给对面。
    "ticket.special_case.submit",
})

# 这些业务动作代表「实际恢复处理」：到达即关闭生效中的特殊情况暂停，
# 并按实际暂停时长顺延截止时间（2026-08-26）。
_SPECIAL_CASE_RESUMING_INTENTS = frozenset({
    "ticket.add_detail", "ticket.diagnosis.submit", "ticket.repair_plan.submit",
    "ticket.complete", "ticket.confirm_complete", "ticket.reject_complete",
    "ticket.reopen",
})


class TicketCommandExecutor:
    def __init__(self, db: Database, repo: TicketRepository) -> None:
        self._db = db
        self._repo = repo

    def execute(
        self,
        command: ValidatedCommand,
        *,
        message: NormalizedMessage | None = None,
        pending_action_id: int | None = None,
        pending_version: int | None = None,
    ) -> CommandResult:
        """执行命令（需在事务外调用，内部自行开启短事务）。"""
        # 幂等键含目标工单：多选归属（2026-08-27）对同一 pending 逐张执行，
        # 缺目标维度会让第二张起命中「已 APPLIED」捷径被静默跳过。
        dedupe_key = (
            f"direct:{command.message_id}"
            if pending_action_id is None
            else f"confirm:{pending_action_id}:{pending_version}"
                 f":{command.target_ticket_id if command.target_ticket_id is not None else 'none'}"
        )
        # 已有执行记录：只有 APPLIED 才能返回成功，失败/未完成不能伪装成成功。
        existing_status = self._db.execution_status(dedupe_key)
        if existing_status is not None:
            logger.info("执行记录已存在 message_id=%s status=%s", command.message_id, existing_status)
            if existing_status == "APPLIED":
                return CommandResult(RESULT_OK, command.target_ticket_id, None, ())
            if existing_status in {RESULT_REJECTED, RESULT_INTERNAL_ERROR}:
                return CommandResult(existing_status, command.target_ticket_id, None, ())
            return CommandResult(RESULT_INTERNAL_ERROR, command.target_ticket_id, None, ())

        logger.info(
            "动作执行开始 msg=%s intent=%s target_ticket_id=%s expected_version=%s",
            command.message_id, command.intent, command.target_ticket_id, command.expected_ticket_version,
        )
        try:
            with self._db.transaction(f"execute:{command.intent}"):
                inserted = self._db.insert_execution(
                    dedupe_key=dedupe_key,
                    source_message_id=command.message_id,
                    confirmation_message_id=message.message_id if message else None,
                    pending_action_id=pending_action_id,
                    intent=command.intent,
                    target_ticket_id=command.target_ticket_id,
                    command_json={
                        "intent": command.intent,
                        "fields": command.fields,
                        "target_ticket_id": command.target_ticket_id,
                    },
                )
                # 事务内存在同 key 执行记录（异常重入）→ 直接放弃
                if not inserted:
                    logger.warning("执行记录已存在 dedupe_key=%s", dedupe_key)
                    status = self._db.execution_status(dedupe_key)
                    if status == "APPLIED":
                        return CommandResult(RESULT_OK, command.target_ticket_id, None, ())
                    return CommandResult(
                        status if status in {RESULT_REJECTED, RESULT_INTERNAL_ERROR}
                        else RESULT_INTERNAL_ERROR,
                        command.target_ticket_id,
                        None,
                        (),
                    )

                result = self._dispatch(command, message)
                if result.status == RESULT_OK:
                    self._db.mark_execution_applied(dedupe_key)
                else:
                    self._db.mark_execution_result(dedupe_key, result.status)
                return result
        except Exception as exc:
            logger.error("执行失败 intent=%s message_id=%s err=%s",
                         command.intent, command.message_id, exc)
            return CommandResult(RESULT_INTERNAL_ERROR, command.target_ticket_id, None, ())

    # ─────────────────────── 分发 ───────────────────────
    def _dispatch(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        if command.target_ticket_id is not None and command.expected_ticket_version is not None:
            ticket = self._db.get_ticket(command.target_ticket_id)
            if ticket is not None and ticket["version"] != command.expected_ticket_version:
                logger.info(
                    "执行版本冲突 intent=%s ticket_id=%s expected=%s actual=%s",
                    command.intent, command.target_ticket_id,
                    command.expected_ticket_version, ticket["version"],
                )
                return CommandResult(
                    RESULT_REJECTED, command.target_ticket_id, ticket["version"], ()
                )
        handler = _HANDLERS.get(command.intent)
        if handler is None:
            logger.warning("无执行器 intent=%s", command.intent)
            return CommandResult(RESULT_REJECTED, None, None, ())
        return handler(self, command, message)

    # ─────────────────────── 各意图执行器 ───────────────────────
    def _merge_problem_description(self, fields: dict) -> str:
        """合并 device + problem_description，避免信息丢失。

        模型按提示词拆分为 device(物品) + problem_description(故障)，但 DB 只有
        problem_description 一列。历史工单因只存后者导致显示为“漏气”“损坏两个”等截断。
        规则：
        - 无 device 直接返回 problem_description
        - desc 已以 device 开头 → 去重，避免“卷帘门卷帘门不升”
        - 否则直接拼接 device+desc（中文无空格，如 地砖+损坏两个=地砖损坏两个）
        """
        device = (fields.get("device") or "").strip()
        desc = (fields.get("problem_description") or "").strip()
        if not device:
            return desc
        if not desc:
            return device
        if desc.startswith(device):
            return desc
        # 避免中间重复？desc 已包含 device 但不在开头，也视作已包含则不重复
        # 但为了不丢失，通常仍需拼接；如"漏气"不含"书架气泵"，需要拼接
        if device in desc:
            return desc
        return f"{device}{desc}"

    def _execute_create(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        group = self._db.get_group(command.group_id)
        if group is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        fields = command.fields
        sla_label = fields.get("sla")
        if not sla_label:
            # sla 已设为必填（2026-08-19）；绕过校验走到这里视为拒绝
            return CommandResult(RESULT_REJECTED, None, None, ())
        ticket_id = self._repo.create_ticket(
            group=group,
            reporter_id=command.actor_id,
            subject=fields.get("subject") or "未命名",
            location=fields.get("location") or "",
            problem_description=self._merge_problem_description(fields),
            sla_label=sla_label,
            now=self._now(),
        )
        ticket = self._db.get_ticket(ticket_id)
        self._finalize(command, ticket, LINK_CREATE, message)
        return CommandResult(RESULT_OK, ticket_id, ticket["version"], ())

    def _execute_add_detail(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        # 字段优先于消息原文：归属选择流程中 message 是用户的编号回复，
        # 待补内容来自创建 pending 时的原始决策草稿（2026-08-27）
        content = command.fields.get("content", "") or (message.content if message else "") or ""
        new_desc = (ticket["problem_description"] or "")
        if content:
            new_desc = f"{new_desc}\n{content}".strip()
        ok = self._db.update_ticket_cas(
            ticket["id"], ticket["version"],
            "problem_description=?, last_business_event_at=?, last_business_message_id=?",
            (new_desc, self._now(), command.message_id),
        )
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        updated = self._db.get_ticket(ticket["id"])
        self._finalize(command, updated, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_submit_version(
        self,
        command: ValidatedCommand,
        message: NormalizedMessage | None,
        *,
        kind: str,
    ) -> CommandResult:
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        fields = command.fields
        suppress_notify = False
        if kind == "diagnosis":
            _diag = fields.get("diagnosis_items", [])
            if isinstance(_diag, str):
                _diag = [_diag] if _diag.strip() else []
            self._db.add_diagnosis_version(ticket["id"], command.message_id,
                                           list(_diag), command.actor_id)
        elif kind == "repair":
            repair_method = fields.get("repair_method", "")
            if repair_method:
                self._db.add_repair_method_version(
                    ticket["id"], command.message_id,
                    repair_method, fields.get("order_no"), command.actor_id)
            else:
                # 只发订单号（无维修方式，如裸单号/诊断+订单消息）：不写空的维修方式版本，
                # 也不发「已记录维修方式」通知（订单登记+延期由 pipeline 负责并已回执）
                suppress_notify = True
            # 备件登记（2026-09-09）：仅记录，不动库存。
            # 权威过滤：按该工单「店 × subject」池 + 该店工具池 canonicalize，
            # 只保留命中规范名；全部未命中 → 静默跳过（不写表、不回执）。
            raw_parts = fields.get("parts") or []
            if raw_parts:
                pool = build_pool(ticket.get("store_name") or "", ticket.get("subject") or "")
                matched = keep_only_matched(raw_parts, pool)
                if matched:
                    self._db.add_part_version(
                        ticket["id"], command.message_id,
                        matched,
                        raw_text=str(message.content if message else "") or None,
                        engineer_id=command.actor_id,
                    )
            # 同一消息顺带提交了故障判断（如「估计是铰链坏了，单号是…」）→ 一并记录
            diagnosis_items = fields.get("diagnosis_items")
            if isinstance(diagnosis_items, str):
                diagnosis_items = [diagnosis_items] if diagnosis_items.strip() else []
            if diagnosis_items:
                self._db.add_diagnosis_version(
                    ticket["id"], command.message_id,
                    [str(x) for x in diagnosis_items], command.actor_id)
        elif kind == "timeout":
            # 计划书 §4.8：仅当存在尚未解释的超时周期时接受原因
            if not self._db.add_timeout_cycle_reason(
                ticket["id"], command.message_id,
                fields.get("timeout_reason", ""), command.actor_id,
            ):
                logger.info("无未解释超时周期，拒绝原因提交 ticket=%s msg=%s",
                            ticket["ticket_no"], command.message_id)
                return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        # 业务变更 → 推进版本与顺序游标
        ok = self._bump_version(ticket, command)
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        updated = self._db.get_ticket(ticket["id"])
        self._finalize(command, updated, LINK_EXECUTED, message, notify=not suppress_notify)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_complete(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        """报完工（2026-08-24 需求 #3）。

        - 店长本人报完工 → 直接 COMPLETED（用户决策：店长发起即完成）；
        - 工程师（或其他允许角色）报完工 → 进入 PENDING_CONFIRM 待店长确认，
          等待责任方切到店长方，由响应 SLA 提醒店长在时限内确认。
        """
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        if command.actor_role == ROLE_MANAGER:
            ok = self._db.update_ticket_cas(
                ticket["id"], ticket["version"],
                "status=?, closed_at=?, last_business_event_at=?, last_business_message_id=?",
                (TICKET_COMPLETED, self._now(), self._now(), command.message_id),
            )
            if not ok:
                return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
            self._db.close_responsibility_cycles(ticket["id"], command.message_id)
            self._db.clear_contexts_by_ticket(ticket["id"])
            updated = self._db.get_ticket(ticket["id"])
            self._finalize(command, updated, LINK_EXECUTED, message)
            return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

        # 工程师报完工 → 待店长确认（closed_at 留空，终态由确认动作写入）
        ok = self._db.update_ticket_cas(
            ticket["id"], ticket["version"],
            "status=?, last_business_event_at=?, last_business_message_id=?",
            (TICKET_PENDING_CONFIRM, self._now(), command.message_id),
        )
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        sent_at = message.sent_at.strftime("%Y-%m-%d %H:%M:%S") if message else self._now()
        # 显式把等待责任方切到店长方（关闭旧周期、开启 MANAGER_SIDE 周期，
        # due_at=+4h，供响应 SLA 提醒店长确认）
        self._db.switch_responsibility(
            ticket["id"], command.actor_role, command.message_id, sent_at,
        )
        updated = self._db.get_ticket(ticket["id"])
        self._finalize(command, updated, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_confirm_complete(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        """店长「确认修好」：PENDING_CONFIRM → COMPLETED 终态。"""
        ticket = self._require_ticket(command)
        if ticket is None or ticket["status"] != TICKET_PENDING_CONFIRM:
            return CommandResult(RESULT_REJECTED,
                                 ticket["id"] if ticket else None,
                                 ticket["version"] if ticket else None, ())
        ok = self._db.update_ticket_cas(
            ticket["id"], ticket["version"],
            "status=?, closed_at=?, completed_confirm_by=?, completed_confirm_at=?,"
            " last_business_event_at=?, last_business_message_id=?",
            (TICKET_COMPLETED, self._now(), command.actor_id, self._now(),
             self._now(), command.message_id),
        )
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        self._db.close_responsibility_cycles(ticket["id"], command.message_id)
        self._db.clear_contexts_by_ticket(ticket["id"])
        updated = self._db.get_ticket(ticket["id"])
        self._finalize(command, updated, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_reject_complete(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        """店长「没修好」：PENDING_CONFIRM → ACTIVE，交还工程师继续处理。"""
        ticket = self._require_ticket(command)
        if ticket is None or ticket["status"] != TICKET_PENDING_CONFIRM:
            return CommandResult(RESULT_REJECTED,
                                 ticket["id"] if ticket else None,
                                 ticket["version"] if ticket else None, ())
        # 驳回理由不落 tickets 列（表无此列），保留在消息记录与群内通知中
        ok = self._db.update_ticket_cas(
            ticket["id"], ticket["version"],
            "status=?, last_business_event_at=?, last_business_message_id=?",
            (TICKET_ACTIVE, self._now(), command.message_id),
        )
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        # 责任方切换由 _finalize 按发送者角色（MANAGER→等工程师方）处理
        updated = self._db.get_ticket(ticket["id"])
        self._finalize(command, updated, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_cancel(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        ok = self._db.update_ticket_cas(
            ticket["id"], ticket["version"],
            "status=?, closed_at=?, cancelled_at=?, cancelled_by=?, cancel_reason=?,"
            " last_business_event_at=?, last_business_message_id=?",
            (TICKET_CANCELLED, self._now(), self._now(), command.actor_id,
             command.fields.get("cancel_reason", ""), self._now(), command.message_id),
        )
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        self._db.close_responsibility_cycles(ticket["id"], command.message_id)
        self._db.clear_contexts_by_ticket(ticket["id"])
        updated = self._db.get_ticket(ticket["id"])
        self._finalize(command, updated, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_stop(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        """#停止维修：工程负责人确认不再维修 → STOPPED 终态，强制记录停修原因。"""
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        ok = self._db.update_ticket_cas(
            ticket["id"], ticket["version"],
            "status=?, closed_at=?, stopped_at=?, stopped_by=?, stop_reason=?,"
            " last_business_event_at=?, last_business_message_id=?",
            (TICKET_STOPPED, self._now(), self._now(), command.actor_id,
             command.fields.get("stop_reason", ""), self._now(), command.message_id),
        )
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        self._db.close_responsibility_cycles(ticket["id"], command.message_id)
        self._db.clear_contexts_by_ticket(ticket["id"])
        updated = self._db.get_ticket(ticket["id"])
        self._finalize(command, updated, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_reopen(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        ok = self._db.update_ticket_cas(
            ticket["id"], ticket["version"],
            "status=?, closed_at=NULL, reopen_count=reopen_count+1,"
            " waiting_side='NONE', waiting_since=NULL, current_responsibility_cycle_id=NULL,"
            " last_business_event_at=?, last_business_message_id=?",
            (TICKET_ACTIVE, self._now(), command.message_id),
        )
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        # 计划书 §9.1：重开后建立新的责任周期；同时清理 SLA 去重键，
        # 让重开后的工单可再次收到到期/超时提醒并按需要建立超时周期
        self._db.close_responsibility_cycles(ticket["id"], command.message_id)
        self._db.clear_ticket_sla_dedupe(ticket["id"])
        updated = self._db.get_ticket(ticket["id"])
        self._finalize(command, updated, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_query(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_OK, None, None, ())
        self._finalize(command, ticket, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, ticket["id"], ticket["version"], ())

    def _execute_select(self, command: ValidatedCommand, message: NormalizedMessage | None) -> CommandResult:
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        # 建立用户上下文（默认 30 分钟）
        from datetime import datetime, timedelta

        now = datetime.now()
        self._db.set_ticket_context(
            command.group_id, command.actor_id, ticket["id"],
            f"{now.strftime('%Y-%m-%d %H:%M:%S')}|{command.message_id}",
            (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            now=now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._finalize(command, ticket, LINK_EXECUTED, message)
        return CommandResult(RESULT_OK, ticket["id"], ticket["version"], ())

    # ─────────────────────── 公共步骤 ───────────────────────
    def _require_ticket(self, command: ValidatedCommand) -> dict[str, Any] | None:
        if command.target_ticket_id is None:
            return None
        return self._db.get_ticket(command.target_ticket_id)

    def _execute_special_case(
        self, command: ValidatedCommand, message: NormalizedMessage | None
    ) -> CommandResult:
        """登记特殊情况暂停（2026-08-26）。

        响应 SLA 一小时提醒引导责任方回复「特殊情况：原因；预计恢复：时间」：
        - 关闭既有生效中的暂停（续期场景），并按上一段实际暂停时长顺延截止；
        - 新建暂停记录（预计时间解析为绝对时间，失败仅存原文）；
        - 不切换责任方、不改状态；暂停期间调度器豁免全部催办。
        """
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        reason = str(command.fields.get("special_case_reason") or "").strip()
        if not reason:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        expected_text = str(command.fields.get("expected_resume_at") or "").strip()
        now_str = self._now()
        try:
            expected_at = parse_resume_time(
                expected_text, datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
            )
        except ValueError:
            expected_at = None
        expected_at_str = (
            expected_at.strftime("%Y-%m-%d %H:%M:%S") if expected_at else None
        )

        self._close_and_true_up_special_case(ticket["id"], command.message_id, now_str)
        self._db.add_special_case(
            ticket["id"], command.message_id, reason,
            expected_text or None, expected_at_str,
            command.actor_id, now_str,
        )
        ok = self._bump_version(ticket, command)
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        updated = self._db.get_ticket(ticket["id"])
        # 回执需携带本次抽取的原因/预计时间 → 直接写入 Outbox 正文
        receipt = reply_text(command.intent, updated, command.fields)
        self._finalize(command, updated, LINK_EXECUTED, message, payload_text=receipt or None)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _execute_negotiate(
        self, command: ValidatedCommand, message: NormalizedMessage | None
    ) -> CommandResult:
        """设为待商榷（2026-08-28）：ACTIVE/OVERDUE → PENDING_NEGOTIATION，需确认。

        清空 deadline / sla_days，冻结 waiting_since（完全暂停），切状态后版本+1。
        """
        ticket = self._require_ticket(command)
        if ticket is None:
            return CommandResult(RESULT_INTERNAL_ERROR, None, None, ())
        if ticket["status"] not in (TICKET_ACTIVE, "ACTIVE_OVERDUE"):
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        reason = str(command.fields.get("negotiate_reason") or "").strip()
        if not reason:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        # 乐观锁切状态
        ok = self._db.update_ticket_cas(
            ticket["id"], ticket["version"], "status=?, current_deadline_at=?, sla_days=?",
            (TICKET_NEGOTIATING, None, 0),
        )
        if not ok:
            return CommandResult(RESULT_REJECTED, ticket["id"], ticket["version"], ())
        # 记录业务经办 message
        self._db.add_ticket_message(
            command.message_id, ticket["id"], command.actor_id, command.actor_role,
            f"设为待商榷：{reason}", "text",
            self._now(),
        )
        updated = self._db.get_ticket(ticket["id"])
        receipt = f"⏸️ 工单 {updated['ticket_no']} 已设为“待商榷”（原因：{reason}）。期间时效与催办暂停，完工后可直接完成。"
        self._finalize(command, updated, LINK_EXECUTED, message, payload_text=receipt)
        return CommandResult(RESULT_OK, updated["id"], updated["version"], ())

    def _close_and_true_up_special_case(

        self, ticket_id: int, resume_message_id: str, resumed_at: str
    ) -> None:
        """关闭生效中的特殊情况暂停；活动工单按实际暂停时长顺延截止时间。

        只顺延 ACTIVE 且有时效的工单：已发生的超时（ACTIVE_OVERDUE）不冲销。
        待商榷（无截止）只留痕不动时间。响应 SLA 的等待起点平移由
        db.close_active_special_case 统一停表结算（2026-08-27：暂停段不计入），
        本处专注截止时钟；暂停前已流逝的等待不冲销原则不变。
        """
        closed = self._db.close_active_special_case(ticket_id, resume_message_id, resumed_at)
        if closed is None:
            return
        ticket = self._db.get_ticket(ticket_id)
        if (
            ticket is None
            or ticket["status"] != TICKET_ACTIVE
            or not ticket["current_deadline_at"]
            or not ticket["sla_days"]
        ):
            return
        try:
            started = datetime.strptime(closed["submitted_at"], "%Y-%m-%d %H:%M:%S")
            ended = datetime.strptime(resumed_at, "%Y-%m-%d %H:%M:%S")
            deadline = datetime.strptime(ticket["current_deadline_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return
        if ended <= started:
            return
        new_deadline = deadline + (ended - started)
        self._db.set_ticket_deadline(
            ticket_id, new_deadline.strftime("%Y-%m-%d %H:%M:%S")
        )
        logger.info(
            "特殊情况暂停结束并顺延截止 ticket_id=%s paused=%.1fh new_deadline=%s",
            ticket_id, (ended - started).total_seconds() / 3600,
            new_deadline.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _bump_version(self, ticket: dict[str, Any], command: ValidatedCommand) -> bool:
        return self._db.update_ticket_cas(
            ticket["id"], ticket["version"],
            "last_business_event_at=?, last_business_message_id=?",
            (self._now(), command.message_id),
        )

    def _finalize(
        self,
        command: ValidatedCommand,
        ticket: dict[str, Any],
        link_type: str,
        message: NormalizedMessage | None,
        notify: bool = True,
        payload_text: str | None = None,
    ) -> None:
        """消息归属 + 消息落库 + 幂等记录 + 通知预写（同事务）。

        payload_text：回执需携带本次抽取字段（如特殊情况原因）时直接写入
        Outbox 正文——通用渲染 reply_text(intent, ticket, {}) 拿不到 fields。
        """
        self._db.link_message(command.message_id, ticket["id"], link_type, 0.0)
        if message is not None:
            self._db.add_ticket_message(
                command.message_id, ticket["id"], command.actor_id, command.actor_role,
                message.content, message.message_type,
                message.sent_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
            # 计划书 §9.3：业务动作（非查询/选择）按消息发送方角色切换责任方
            if (
                command.intent not in _READONLY_INTENTS
                and command.intent not in _NO_SWITCH_INTENTS
            ):
                self._db.switch_responsibility(
                    ticket["id"], command.actor_role, command.message_id,
                    message.sent_at.strftime("%Y-%m-%d %H:%M:%S"),
                )
        self._db.record_processed_event(command.message_id, command.group_id, "EXECUTED")
        # 实际业务动作到达 → 关闭生效中的特殊情况暂停并顺延截止（2026-08-26）
        if command.intent in _SPECIAL_CASE_RESUMING_INTENTS:
            self._close_and_true_up_special_case(ticket["id"], command.message_id, self._now())
        # 静默化（2026-08-24 #2）：纯告知类成功回执不进 Outbox
        if notify and command.intent not in SILENT_INTENTS:
            self._db.insert_notification(
                dedupe_key=f"exec:{command.message_id}",
                ticket_id=ticket["id"],
                notification_type=command.intent,
                target_type="group",
                target_id=command.group_id,
                payload_text=payload_text,
            )

    def _now(self) -> str:
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_HANDLERS: dict[str, Callable[..., CommandResult]] = {
    "ticket.create": TicketCommandExecutor._execute_create,
    "ticket.add_detail": TicketCommandExecutor._execute_add_detail,
    "ticket.diagnosis.submit": lambda self, c, m: self._execute_submit_version(c, m, kind="diagnosis"),
    "ticket.repair_plan.submit": lambda self, c, m: self._execute_submit_version(c, m, kind="repair"),
    "ticket.timeout_reason.submit": lambda self, c, m: self._execute_submit_version(c, m, kind="timeout"),
    "ticket.special_case.submit": TicketCommandExecutor._execute_special_case,
    "ticket.negotiate.submit": TicketCommandExecutor._execute_negotiate,
    "ticket.complete": TicketCommandExecutor._execute_complete,
    "ticket.confirm_complete": TicketCommandExecutor._execute_confirm_complete,
    "ticket.reject_complete": TicketCommandExecutor._execute_reject_complete,
    "ticket.cancel": TicketCommandExecutor._execute_cancel,
    "ticket.stop": TicketCommandExecutor._execute_stop,
    "ticket.reopen": TicketCommandExecutor._execute_reopen,
    "ticket.query": TicketCommandExecutor._execute_query,
    "ticket.select": TicketCommandExecutor._execute_select,
}
