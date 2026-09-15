"""受控命令定义与回复文案（计划书 §11.8、Task 9）。

执行器映射必须来自 ALLOWED_EXECUTORS，使用静态字典，不使用 eval / 反射。
"""

from __future__ import annotations

from typing import Any

# 协议允许的执行器（与 protocol_loader 校验一致）
ALLOWED_EXECUTORS = frozenset({
    "create_ticket", "add_ticket_detail", "submit_diagnosis",
    "submit_repair_plan", "submit_timeout_reason", "complete_ticket",
    "confirm_complete_ticket", "reject_complete_ticket",
    "cancel_ticket", "stop_ticket", "reopen_ticket", "query_ticket", "select_ticket",
    "clarify", "confirm_pending_action", "reject_pending_action",
    "correct_pending_action", "ignore_message",
    "submit_special_case", "submit_negotiate",
})

# 意图 → 中文可读文案（用户看到的提示/回执用，避免出现 system.clarify 这类英文 ID）
INTENT_LABELS: dict[str, str] = {
    "ticket.create": "报修建单",
    "ticket.add_detail": "补充工单信息",
    "ticket.diagnosis.submit": "记录故障判断",
    "ticket.repair_plan.submit": "提交维修方式/订单号",
    "ticket.timeout_reason.submit": "提交超时原因",
    "ticket.special_case.submit": "登记特殊情况",
    "ticket.negotiate.submit": "设为待商榷",
    "ticket.complete": "报完工",
    "ticket.confirm_complete": "店长确认完成",
    "ticket.reject_complete": "店长反馈未修好",
    "ticket.cancel": "取消工单",
    "ticket.stop": "停修工单",
    "ticket.reopen": "重开工单",
    "ticket.query": "查询工单",
    "ticket.select": "选择工单",
    "system.clarify": "需要您澄清",
    "system.confirm_pending_action": "确认待办",
    "system.reject_pending_action": "拒绝待办",
    "system.correct_pending_action": "修正待办",
    "chat.ignore": "忽略（闲聊）",
}


def intent_label(intent: str) -> str:
    """意图 ID → 中文文案；未映射时原样返回。"""
    return INTENT_LABELS.get(intent, intent)


# 工单状态枚举 → 中文（编号纠错提示 / 校验器共用）
TICKET_STATUS_LABELS: dict[str, str] = {
    "ACTIVE": "处理中",
    "ACTIVE_OVERDUE": "已超时",
    "PENDING_CONFIRM": "待店长确认",
    "PENDING_NEGOTIATION": "待商榷",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
    "STOPPED": "已停修",
}


def ticket_status_label(status: str) -> str:
    """工单状态 → 中文；未映射时原样返回。"""
    return TICKET_STATUS_LABELS.get(status, status)


def wrong_number_text(ticket_no: str, candidates: list[Any] | None = None,
                      suggestion: str | None = None) -> str:
    """编号不存在时的纠错文案（需求 2026-08-24 #1）。

    candidates 为本群全部工单快照（含终态），只展示前 5 条「短编号(状态)」。
    """
    lines = [f"⚠️ 未找到工单「{ticket_no}」，请核对编号后重发。"]
    if suggestion:
        lines.append(f"您是否想指：「{suggestion}」？")
    if candidates:
        preview = " ".join(
            f"{c.ticket_no.rsplit('-', 1)[-1]}({ticket_status_label(c.status)})"
            for c in candidates[:5]
        )
        lines.append(f"本群工单：{preview}")
    return chr(10).join(lines)


def _deadline_text(ticket: dict[str, Any]) -> str:
    """工单截止文案；待商榷工单（无 deadline）显示「暂不设时效」。"""
    deadline = ticket.get("current_deadline_at")
    return f"预计完成：{deadline}" if deadline else "时效：待商榷（暂不设截止时间）"


def _sla_text(ticket: dict[str, Any]) -> str:
    """工单时效文案，明确提示时效天数。"""
    sla_days = ticket.get("sla_days")
    if sla_days and sla_days > 0:
        return f"时效：{sla_days}天"
    return "时效：待商榷"


# 纯告知类意图：执行成功后不在群里回执（需求 2026-08-24 #2「只回复需要操作或确认的内容」）。
# 这些动作的留痕在 messages / 导出 md 中可查，无需打扰群成员。
SILENT_INTENTS = frozenset({
    "ticket.add_detail",
    "ticket.diagnosis.submit",
    "ticket.repair_plan.submit",
    "ticket.timeout_reason.submit",
    "ticket.cancel",
    "ticket.stop",
    "ticket.reopen",
    "ticket.select",
})


def reply_text(intent: str, ticket: dict[str, Any] | None, fields: dict[str, Any]) -> str:
    """根据意图与执行后的工单生成群内回复。

    原则（2026-08-24）：只在需要某方作出操作或确认时发声；
    纯告知类成功回执一律静默（SILENT_INTENTS），返回空串由调用方跳过发送。
    """
    ticket_no = ticket["ticket_no"] if ticket else ""
    if intent == "ticket.create" and ticket:
        # 建单 = 启动工程师响应时钟，需要工程师作出响应 → 保留一行式回执
        line = f"✅ 已建单 {ticket_no}｜{ticket['subject']}｜{_sla_text(ticket)}"
        deadline = ticket.get("current_deadline_at")
        if deadline:
            line += f"｜截止 {deadline}"
        return line
    if intent == "ticket.complete":
        # 工程师报完工 → 待店长确认（需要店长作出确认操作 → 必须回复）
        if ticket is not None and ticket.get("status") == "PENDING_CONFIRM":
            return (
                f"🔧 工程师已报完工：{ticket_no}。"
                f"请店长回复「确认修好」完成工单；如未修好请回复「没修好」。"
            )
        # 店长本人直接完成 → 终态闭环一行
        return f"✅ 工单 {ticket_no} 已完成"
    if intent == "ticket.confirm_complete":
        return f"✅ 工单 {ticket_no} 维修完成（店长已确认）"
    if intent == "ticket.reject_complete":
        reason = str(fields.get("reject_reason") or "").strip()
        base = f"❌ 工单 {ticket_no} 店长反馈未修好，请工程师继续处理"
        return f"{base}：{reason}" if reason else f"{base}。"
    if intent == "ticket.special_case.submit" and ticket:
        # 特殊情况登记回执：确认系统已「接住」答复并暂停计时（2026-08-26）。
        # 回执必须发声——否则回复方无从得知暂停是否生效（此前静默忽略的教训）。
        reason = str(fields.get("special_case_reason") or "").strip() or "未说明"
        expected = str(fields.get("expected_resume_at") or "").strip()
        line = f"⏸️ 工单 {ticket_no} 已登记特殊情况：{reason}"
        if expected:
            line += f"｜预计恢复：{expected}"
        return line + "。暂停期间时效与催办暂停，恢复处理后继续计时。"
    if intent == "ticket.negotiate.submit" and ticket:
        reason = str(fields.get("negotiate_reason") or "").strip() or "未说明"
        return f"⏸️ 工单 {ticket_no} 已设为“待商榷”（原因：{reason}）。期间时效与催办暂停，完工后可直接完成。"
    if intent in SILENT_INTENTS:
        return ""
    if intent == "ticket.query" and ticket:
        return (
            f"📋 {ticket_no}  {ticket_status_label(ticket.get('status', ''))}\n"
            f"主题:{ticket['subject']}｜位置:{ticket['location']}\n"
            f"问题:{ticket['problem_description'][:80]}\n"
            f"{_deadline_text(ticket)}"
        )
    return "已处理"
