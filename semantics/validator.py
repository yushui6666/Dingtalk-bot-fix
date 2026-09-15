"""语义决策校验器（计划书 Task 2）。

validate_decision() 对 match_keyword 或 model_classifier 产出的
SemanticDecision 做二次校验（权限、必填字段、枚举值、订单号规则），
输出 (DecisionStatus, ValidatedCommand | None, errors)。

确定性校验——不调用模型，不访问数据库（candidates 由调用方注入）。
"""

from __future__ import annotations

import re
from typing import Any

from semantics.protocol_loader import TicketProtocol
from semantics.types import (
    DecisionStatus,
    SemanticDecision,
    TicketCandidate,
    ValidatedCommand,
)

from models import (
    ROLE_ENGINEER,
    ROLE_LEADER,
    ROLE_MANAGER,
    NormalizedMessage,
    role_label,
)

# 订单号占位值（v3.0 遗留规则，仍适用）
_ORDER_NO_PLACEHOLDERS = frozenset({"无", "暂无", "稍后补", "不知道"})

# 订单号正则（去空白后 6-64 位字母数字连字符）
_ORDER_NO_PATTERN = re.compile(r"^[A-Za-z0-9-]{6,64}$")

# 维修方式已改为工程师自由填入（原五种枚举已废弃，仅保留兼容校验，2026-08-26）
# 保留集合仅用于历史文档/提示，不再作为强校验；自由文本只要非空即可
_ALLOWED_REPAIR_METHODS = frozenset({
    "淘宝采购后自行维修",
    "需要供应商维修",
    "需要木工维修",
    "需要工程师上门",
    "远程视频维修",
})

# SLA 允许值（「待商榷」= 不设时效、仅记录）
_ALLOWED_SLA = frozenset({"1天", "3天", "7天", "待商榷"})

# 字段名 → 中文（与协议 field_definitions 的 aliases 对齐，用于回执展示）
_FIELD_LABELS = {
    "ticket_no": "工单编号",
    "subject": "主题",
    "location": "位置",
    "problem_description": "问题描述",
    "device": "设备",
    "urgency": "紧急程度",
    "attachments": "附件",
    "sla": "时效",
    "diagnosis_items": "故障判断",
    "repair_method": "维修方式",
    "order_no": "订单号",
    "timeout_reason": "未完成原因",
    "completion_note": "完成说明",
    "cancel_reason": "取消原因",
    "reopen_reason": "重开原因",
    "stop_reason": "停修原因",
    "special_case_reason": "特殊情况原因",
    "negotiate_reason": "待商榷原因",
    "expected_resume_at": "预计恢复时间",
}

# 工单状态枚举 → 中文
_TICKET_STATUS_LABELS = {
    "ACTIVE": "进行中",
    "ACTIVE_OVERDUE": "已超时",
    "PENDING_CONFIRM": "待店长确认",
    "PENDING_NEGOTIATION": "待商榷",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
    "STOPPED": "已停修",
}


def _field_label(field_name: str) -> str:
    """字段名 → 中文；未映射时原样返回。"""
    return _FIELD_LABELS.get(field_name, field_name)


def _ticket_status_label(status: str) -> str:
    """工单状态 → 中文；未映射时原样返回。"""
    return _TICKET_STATUS_LABELS.get(status, status)


def _validate_role(action: Any, message: NormalizedMessage) -> bool:
    """校验用户角色是否在动作的允许角色列表中。

    LEADER（工程负责人/区域经理）拥有超集权限：可执行 LEADER 专属动作，
    也可执行店长/工程师允许的所有动作（v4.2：兼任工程负责人的工程师不丢失既有权限）。
    """
    if not action.allowed_roles:
        return True  # 无限制（如 system.* / chat.ignore）
    if message.sender_role == ROLE_LEADER:
        if ROLE_LEADER in action.allowed_roles:
            return True
        return any(
            r in action.allowed_roles for r in (ROLE_MANAGER, ROLE_ENGINEER)
        )
    return message.sender_role in action.allowed_roles


def _validate_required_fields(
    action: Any,
    fields: dict[str, Any],
    target_ticket_no: str | None,
) -> list[str]:
    """校验必填字段是否存在且非空。"""
    missing: list[str] = []
    for fname in action.required_fields:
        if fname == "ticket_no":
            if not target_ticket_no:
                missing.append(fname)
            continue
        val = fields.get(fname)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(fname)
        elif isinstance(val, list) and not val:
            missing.append(fname)
    return missing


def _validate_enum(field_name: str, value: Any, action: Any) -> str | None:
    """校验枚举字段值是否合法。返回错误消息或 None。"""
    fd = action.field_definitions.get(field_name, {})
    allowed = fd.get("allowed")
    if allowed and isinstance(allowed, list):
        if value not in allowed:
            return f"{_field_label(field_name)} '{value}' 无效，可选：{('、'.join(str(a) for a in allowed))}"
    return None


def _validate_order_no(fields: dict[str, Any]) -> str | None:
    """淘宝采购维修方式时校验订单号（自由文本后按关键词匹配）。"""
    repair_method = str(fields.get("repair_method", "") or "")
    # 自由文本：只要包含 淘宝/采购 即视为淘宝采购场景
    if "淘宝" not in repair_method and "采购" not in repair_method:
        # 兼容旧枚举的精确值
        if repair_method != "淘宝采购后自行维修":
            return None

    order_no = fields.get("order_no", "")
    if not order_no:
        return "维修方式涉及淘宝采购时必须提供订单号"

    # 去除空白
    cleaned = "".join(order_no.split())
    if not cleaned:
        return "订单号不能为空"

    if cleaned in _ORDER_NO_PLACEHOLDERS:
        return f"订单号不能使用占位值 '{order_no}'"

    if not _ORDER_NO_PATTERN.match(cleaned):
        return f"订单号 '{order_no}' 格式不合法（需要 6-64 位字母数字连字符）"

    return None


def _dispatch_reason_field(fields: dict[str, Any], intent: str) -> None:
    """将通用 '原因' 字段按 intent 分发到具体字段名。

    #取消工单 → cancel_reason
    #重开工单 → reopen_reason
    #停止维修 → stop_reason
    """
    if "reason" not in fields:
        return
    reason = fields.pop("reason")
    if intent == "ticket.cancel":
        fields["cancel_reason"] = reason
    elif intent == "ticket.reopen":
        fields["reopen_reason"] = reason
    elif intent == "ticket.stop":
        fields["stop_reason"] = reason
    # 其他 intent 不需要 reason 字段


def _build_validated_command(
    decision: SemanticDecision,
    message: NormalizedMessage,
    target_ticket_id: int | None,
    expected_ticket_version: int | None,
    fields: dict[str, Any],
) -> ValidatedCommand:
    return ValidatedCommand(
        message_id=message.message_id,
        group_id=message.group_id,
        actor_id=message.sender_id,
        actor_role=message.sender_role,
        intent=decision.intent,
        target_ticket_id=target_ticket_id,
        expected_ticket_version=expected_ticket_version,
        fields=fields,
        source=decision.source,
    )


def _resolve_target_candidate(
    decision: SemanticDecision,
    action: Any,
    message: NormalizedMessage,
    candidates: list[TicketCandidate],
) -> tuple[TicketCandidate | None, list[str]]:
    """按协议目标策略解析当前群内候选工单。"""
    errors: list[str] = []
    group_candidates = [candidate for candidate in candidates if candidate.group_id == message.group_id]
    target: TicketCandidate | None = None

    if decision.target_ticket_no:
        target = next(
            (
                candidate
                for candidate in group_candidates
                if candidate.ticket_no == decision.target_ticket_no
            ),
            None,
        )
        if target is None:
            errors.append(f"目标工单 '{decision.target_ticket_no}' 不存在或不属于当前群")
    elif action.target_ticket_policy == "MUST_EXIST":
        if len(group_candidates) == 1:
            target = group_candidates[0]
        elif not group_candidates:
            errors.append("该动作需要目标工单，但当前没有可用候选")
        else:
            errors.append("该动作需要唯一目标工单，请明确提供工单编号")

    if action.target_ticket_policy == "MUST_NOT_EXIST" and decision.target_ticket_no:
        errors.append("该动作不得绑定既有目标工单")

    if target and action.allowed_ticket_states and target.status not in action.allowed_ticket_states:
        errors.append(
            f"该工单当前状态「{_ticket_status_label(target.status)}」，不能执行「{decision.intent}」，"
            f"仅限状态为 {('、'.join(_ticket_status_label(s) for s in action.allowed_ticket_states))} 的工单"
        )

    return target, errors


def _requires_confirmation(decision: SemanticDecision, action: Any) -> bool:
    """根据决策来源和协议确认策略判断是否进入确认流程。"""
    if decision.requires_confirmation:
        return True
    source_key = "EXPLICIT_KEYWORD" if decision.source == "keyword" else "SEMANTIC_MODEL"
    return action.confirmation_policy.get(source_key) == "ALWAYS"


def validate_decision(
    decision: SemanticDecision,
    *,
    message: NormalizedMessage,
    candidates: list[TicketCandidate],
    protocol: TicketProtocol,
) -> tuple[DecisionStatus, ValidatedCommand | None, tuple[str, ...]]:
    """校验语义决策，返回 (状态, 命令, 错误列表)。

    校验顺序：intent 查找 → ignore 快路径 → 角色权限 → 必填字段 →
    枚举值 → 订单号规则。

    candidates 是路由层冻结的当前群候选快照，用于目标、状态和版本校验。
    """
    action = protocol.get_action(decision.intent)
    if action is None:
        return (
            DecisionStatus.VALIDATION_REJECTED,
            None,
            (f"未知 intent_id: {decision.intent}",),
        )

    # chat.ignore 快路径
    if decision.intent == "chat.ignore":
        return (DecisionStatus.IGNORE, None, ())

    # system.* 校验（非 ignore 的系统动作）
    if decision.intent.startswith("system.") and decision.intent != "system.clarify":
        return (DecisionStatus.IGNORE, None, ())

    # system.clarify → 直接返回等待确认
    if decision.intent == "system.clarify":
        return (DecisionStatus.WAITING_CONFIRMATION, None, decision.missing_fields)

    fields = dict(decision.fields)
    # 兼容：模型把 diagnosis_items 误返回为字符串 → 归一为数组
    if "diagnosis_items" in fields and isinstance(fields["diagnosis_items"], str):
        v = fields["diagnosis_items"].strip()
        fields["diagnosis_items"] = [v] if v else []

    # 兼容：parts 归一为字符串数组（顿号/逗号分隔串 → 拆分），并剔除空项。
    # 权威的规范名过滤在执行期（executor 按店×主题池 canonicalize）完成，这里只做形态归一。
    if "parts" in fields:
        _parts_raw = fields["parts"]
        if _parts_raw is None:
            fields["parts"] = []
        elif isinstance(_parts_raw, str):
            fields["parts"] = [p for p in re.split(r"[、，,；;]", _parts_raw) if p.strip()]
        elif isinstance(_parts_raw, list):
            fields["parts"] = [str(p).strip() for p in _parts_raw if p is not None and str(p).strip()]
        else:
            fields["parts"] = []

    # 字段分发：通用 "原因" → 按 intent 映射到具体字段
    _dispatch_reason_field(fields, decision.intent)

    errors: list[str] = []

    # 1. 角色权限
    if not _validate_role(action, message):
        from tickets.commands import intent_label

        allowed = [role_label(r) for r in action.allowed_roles]
        errors.append(
            f"{role_label(message.sender_role)}没有「{intent_label(decision.intent)}」权限，"
            f"该操作仅限{('、'.join(allowed))}执行"
        )

    # 2. 必填字段
    missing = _validate_required_fields(action, fields, decision.target_ticket_no)
    for mf in missing:
        label = _field_label(mf)
        if mf == "sla":
            errors.append(f"缺少「{label}」，请补充：时效：1天/3天/7天（或 待商榷）")
        elif mf == "subject":
            errors.append(f"缺少「{label}」，请补充：主题：xxx（无主题请写 无主题）")
        else:
            errors.append(f"缺少「{label}」，请对照标准补充")

    # 3. 枚举值校验
    for fname, fvalue in fields.items():
        err = _validate_enum(fname, fvalue, action)
        if err:
            errors.append(err)

    # SLA 特殊枚举校验（字段名可能在不同 action 中）
    if "sla" in fields and fields["sla"] not in _ALLOWED_SLA:
        if "sla" not in [f for f in errors if "sla" in f]:
            errors.append(f"时效 '{fields['sla']}' 无效，可选：1天、3天、7天、待商榷")

    # 维修方式已改为自由文本（2026-08-26）：只要非空即可，不再枚举校验
    if "repair_method" in fields:
        _rm = str(fields["repair_method"] or "").strip()
        if not _rm:
            if "repair_method" not in [f for f in errors if "repair_method" in f]:
                errors.append("维修方式不能为空")
        # 超长兜底（避免一次性贴几千字）
        elif len(_rm) > 500:
            errors.append("维修方式过长（请控制在500字以内）")

    # 4. 订单号校验（淘宝采购特例）——自由文本下按关键词匹配
    _rm_text = str(fields.get("repair_method") or "")
    if "淘宝" in _rm_text or "采购" in _rm_text:
        order_err = _validate_order_no(fields)
        if order_err:
            errors.append(order_err)

    # 4.1 维修计划至少需要维修方式或订单号其一（避免非枚举描述被剥离后空执行，产生“订单 None”误登记）
    if decision.intent == "ticket.repair_plan.submit":
        has_method = bool(fields.get("repair_method") and str(fields["repair_method"]).strip())
        raw_order = fields.get("order_no")
        has_order = bool(raw_order is not None and str(raw_order).strip() and str(raw_order).strip().lower() not in ("none", "null", "nil"))
        # 兼容部分模型返回 order_nos 数组（虽然白名单已过滤，但历史数据可能携带）
        if not has_order and isinstance(fields.get("order_nos"), list):
            has_order = any(o is not None and str(o).strip() and str(o).strip().lower() not in ("none", "null", "nil") for o in fields["order_nos"])
        if not has_method and not has_order:
            errors.append("请明确维修方式（工程师自由填入，如：更换喇叭、木板打孔、淘宝采购等）或提供订单号")

    # 5. 目标工单、群归属和状态校验
    target, target_errors = _resolve_target_candidate(decision, action, message, candidates)
    errors.extend(target_errors)

    if errors:
        return (DecisionStatus.VALIDATION_REJECTED, None, tuple(errors))

    if _requires_confirmation(decision, action):
        return (DecisionStatus.WAITING_CONFIRMATION, None, ())

    # 全部通过
    cmd = _build_validated_command(
        decision,
        message,
        target.ticket_id if target else None,
        target.version if target else None,
        fields,
    )
    return (DecisionStatus.AUTO_EXECUTE, cmd, ())
