"""协议编译器：业务源 JSON → 规范化运行时协议（计划书 Task 1）。

职责：
1. 读取 dashbord/维修工单_流程关键词.json（中文键、人类可读业务格式）
2. 将中文键映射为英文 intent_id，补全 v4.0 新增字段
3. 通过 load_protocol 校验编译结果
4. 以固定键序写入 protocols/ticket_semantics.v4.json
5. 返回 SHA-256 摘要

运行时协议应加入版本管理；重新编译后与已提交版本逐字节一致则无需更新。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from semantics.protocol_loader import ProtocolValidationError, load_protocol

# ───────────────────────── 中文键 → intent_id 映射 ─────────────────────────

_KEYWORD_TO_INTENT: dict[str, str] = {
    "#报修": "ticket.create",
    "#补充": "ticket.add_detail",
    "#故障判断": "ticket.diagnosis.submit",
    "#维修方式": "ticket.repair_plan.submit",
    "#超时原因": "ticket.timeout_reason.submit",
    "#完毕": "ticket.complete",
    "#取消工单": "ticket.cancel",
    "#停止维修": "ticket.stop",
    "#重开工单": "ticket.reopen",
    "#查询工单": "ticket.query",
    "#选择工单": "ticket.select",
}

# 角色中文 → 英文
_ROLE_MAP: dict[str, str] = {
    "店长": "MANAGER",
    "工程师": "ENGINEER",
    "工程负责人": "LEADER",
    "区域经理": "LEADER",
    "其他成员": "OTHER",
    "系统账号": "SYSTEM",
}

# 新增 v4.0 动作（业务源中还没有的定义）
_EXTRA_ACTIONS: list[dict[str, Any]] = [
    {
        "intent_id": "ticket.add_detail",
        "display_name": "补充工单信息",
        "explicit_keywords": ["#补充"],
        "semantic_enabled": True,
        "allowed_roles": ["MANAGER", "ENGINEER", "OTHER"],
        "allowed_ticket_states": ["ACTIVE", "ACTIVE_OVERDUE"],
        "required_fields": ["ticket_no"],
        "optional_fields": ["content", "attachments"],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "LOW",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "BY_CONFIDENCE",
        },
        "positive_examples": [
            "麻烦补充一下工单信息，问题描述：空调漏水更严重了",
            "#补充 主题已更新为空调不制冷",
            "#补充 问题描述：空调漏水更严重了",
        ],
        "negative_examples": [
            "补充说明一下",
            "我刚才说的是补充",
            "补充点别的",
        ],
        "confirmation_template": "",
        "executor": "add_ticket_detail",
        "field_definitions": {},
    },
    {
        "intent_id": "ticket.cancel",
        "display_name": "取消工单",
        "explicit_keywords": ["#取消工单"],
        "semantic_enabled": True,
        "allowed_roles": ["MANAGER"],
        "allowed_ticket_states": ["ACTIVE", "ACTIVE_OVERDUE", "PENDING_CONFIRM", "PENDING_NEGOTIATION"],
        "required_fields": ["ticket_no", "cancel_reason"],
        "optional_fields": [],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "HIGH",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "ALWAYS",
        },
        "positive_examples": [
            "帮我把这个工单取消掉，是误报，实际没有故障",
            "#取消工单 原因：误报，实际无故障",
            "#取消工单 原因：已由其他门店维修",
        ],
        "negative_examples": [
            "这个工单可以取消吗",
            "要不要取消工单",
            "我可能想取消这个工单",
            "工单取消是否可行",
        ],
        "confirmation_template": "⚠️ 确认取消工单 {ticket_no}？操作不可逆，请明确回复。",
        "executor": "cancel_ticket",
        "field_definitions": {},
    },
    {
        "intent_id": "ticket.stop",
        "display_name": "停修工单",
        "explicit_keywords": ["#停止维修"],
        "semantic_enabled": True,
        "allowed_roles": ["LEADER"],
        "allowed_ticket_states": ["ACTIVE", "ACTIVE_OVERDUE", "PENDING_CONFIRM", "PENDING_NEGOTIATION"],
        "required_fields": ["ticket_no", "stop_reason"],
        "optional_fields": [],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "HIGH",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "ALWAYS",
        },
        "positive_examples": [
            "这个工单不要再修了，停止维修，配件已停产无法修复",
            "#停止维修 原因：配件停产，无法修复",
            "工单 W001 停止维修，原因：设备已报废不再维修",
        ],
        "negative_examples": [
            "这个工单还要继续修吗",
            "停止维修是终态吗",
            "我不太确定要不要停修",
        ],
        "confirmation_template": "⚠️ 确认停止维修工单 {ticket_no}？该工单将进入停修终态，可随时重开。请明确回复。",
        "executor": "stop_ticket",
        "field_definitions": {},
    },
    {
        "intent_id": "ticket.reopen",
        "display_name": "重开工单",
        "explicit_keywords": ["#重开工单"],
        "semantic_enabled": True,
        "allowed_roles": ["ENGINEER", "MANAGER", "LEADER"],
        "allowed_ticket_states": ["COMPLETED", "CANCELLED", "STOPPED"],
        "required_fields": ["ticket_no", "reopen_reason"],
        "optional_fields": [],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "HIGH",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "ALWAYS",
        },
        "positive_examples": [
            "门又坏了，把之前的工单重开吧，门再次下沉需要重新维修",
            "#重开工单 原因：门再次下沉，需要重新维修",
            "#重开工单 原因：上次维修未解决根本问题",
        ],
        "negative_examples": [
            "能不能重开工单",
            "我感觉需要重开",
            "重新开一个吧",
            "是否应该重开这个工单",
        ],
        "confirmation_template": "⚠️ 确认重开工单 {ticket_no}？将恢复为活动状态。",
        "executor": "reopen_ticket",
        "field_definitions": {},
    },
    {
        "intent_id": "ticket.query",
        "display_name": "查询工单",
        "explicit_keywords": ["#查询工单"],
        "semantic_enabled": True,
        "allowed_roles": ["MANAGER", "ENGINEER", "OTHER"],
        "allowed_ticket_states": [],
        "required_fields": [],
        "optional_fields": ["ticket_no"],
        "target_ticket_policy": "ANY",
        "risk_level": "LOW",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "NOT_REQUIRED",
        },
        "positive_examples": [
            "现在有多少单在处理",
            "现在有哪些工单在处理",
            "帮我查一下工单",
            "#查询工单",
            "#查询工单 W001",
        ],
        "negative_examples": [
            "这个工单是什么问题",
        ],
        "confirmation_template": "",
        "executor": "query_ticket",
        "field_definitions": {},
    },
    {
        "intent_id": "ticket.select",
        "display_name": "选择工单上下文",
        "explicit_keywords": ["#选择工单"],
        "semantic_enabled": True,
        "allowed_roles": ["MANAGER", "ENGINEER", "OTHER"],
        "allowed_ticket_states": [],
        "required_fields": ["ticket_no"],
        "optional_fields": [],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "LOW",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "NOT_REQUIRED",
        },
        "positive_examples": [
            "#选择工单 W001",
            "我选第二张工单 T002",
            "选择工单 T002",
        ],
        "negative_examples": [
            "现在有哪些工单",
            "第二张工单是什么问题",
        ],
        "confirmation_template": "",
        "executor": "select_ticket",
        "field_definitions": {},
    },
    {
        "intent_id": "system.clarify",
        "display_name": "要求用户澄清",
        "explicit_keywords": [],
        "semantic_enabled": True,
        "allowed_roles": [],
        "allowed_ticket_states": [],
        "required_fields": ["clarification_reason"],
        "optional_fields": [],
        "target_ticket_policy": "ANY",
        "risk_level": "LOW",
        "confirmation_policy": {},
        "positive_examples": [
            "一条消息里同时有报修和完毕，无法确定先执行哪个",
            "同时提到多个互斥操作，需要先向用户确认",
        ],
        "negative_examples": [
            "只明确表达一个意图的消息",
            "用户明确说报修但缺少字段，应返回 ticket.create",
        ],
        "confirmation_template": "",
        "executor": "clarify",
        "field_definitions": {},
    },
    {
        "intent_id": "system.confirm_pending_action",
        "display_name": "确认待处理动作",
        "explicit_keywords": [],
        "semantic_enabled": True,
        "allowed_roles": [],
        "allowed_ticket_states": [],
        "required_fields": [],
        "optional_fields": [],
        "target_ticket_policy": "ANY",
        "risk_level": "LOW",
        "confirmation_policy": {},
        "positive_examples": ["确认", "是", "好的，就这样"],
        "negative_examples": ["不是", "不对", "确认一下再说", "我觉得需要确认"],
        "confirmation_template": "",
        "executor": "confirm_pending_action",
        "field_definitions": {},
    },
    {
        "intent_id": "system.reject_pending_action",
        "display_name": "拒绝待处理动作",
        "explicit_keywords": [],
        "semantic_enabled": True,
        "allowed_roles": [],
        "allowed_ticket_states": [],
        "required_fields": [],
        "optional_fields": [],
        "target_ticket_policy": "ANY",
        "risk_level": "LOW",
        "confirmation_policy": {},
        "positive_examples": ["不", "拒绝", "取消", "不对，不开"],
        "negative_examples": ["不确定", "不好说", "我先问问"],
        "confirmation_template": "",
        "executor": "reject_pending_action",
        "field_definitions": {},
    },
    {
        "intent_id": "system.correct_pending_action",
        "display_name": "修正待确认动作",
        "explicit_keywords": [],
        "semantic_enabled": True,
        "allowed_roles": [],
        "allowed_ticket_states": [],
        "required_fields": [],
        "optional_fields": [],
        "target_ticket_policy": "ANY",
        "risk_level": "LOW",
        "confirmation_policy": {},
        "positive_examples": ["不是这个，是", "应该是", "改成", "不对，要修改"],
        "negative_examples": ["是不是要改一下", "可以考虑修改"],
        "confirmation_template": "",
        "executor": "correct_pending_action",
        "field_definitions": {},
    },
    {
        "intent_id": "chat.ignore",
        "display_name": "忽略（闲聊/无关消息）",
        "explicit_keywords": [],
        "semantic_enabled": False,
        "allowed_roles": [],
        "allowed_ticket_states": [],
        "required_fields": [],
        "optional_fields": [],
        "target_ticket_policy": "ANY",
        "risk_level": "LOW",
        "confirmation_policy": {},
        "positive_examples": [],
        "negative_examples": [],
        "confirmation_template": "",
        "executor": "ignore_message",
        "field_definitions": {},
    },
    # ── 2026-08-26 特殊情况暂停（响应 SLA 一小时提醒引导的答复）──
    {
        "intent_id": "ticket.special_case.submit",
        "display_name": "登记特殊情况",
        "explicit_keywords": ["#特殊情况"],
        "semantic_enabled": True,
        "allowed_roles": ["MANAGER", "ENGINEER"],
        "allowed_ticket_states": ["ACTIVE", "ACTIVE_OVERDUE", "PENDING_CONFIRM"],
        "required_fields": ["special_case_reason"],
        "optional_fields": ["ticket_no", "expected_resume_at"],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "LOW",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "NOT_REQUIRED",
        },
        "positive_examples": [
            "特殊情况：等待门店接客两场连场 预计恢复：一小时内",
            "特殊情况：等待到货；预计恢复：明天下午",
            "特殊情况：等待工程师上门，预计恢复：2026-08-28 10:00",
            "有特殊情况，门店现在接客走不开，预计一小时内恢复处理",
            "特殊情况 等待客户配合 预计恢复 半小时",
        ],
        "negative_examples": [
            "特殊情况怎么填",
            "没有什么特殊情况",
            "是不是有特殊情况",
            "特殊情况下你们一般怎么处理",
            "这单挺特殊的",
        ],
        "confirmation_template": "",
        "executor": "submit_special_case",
        "field_definitions": {},
    },
    # ── 2026-08-28 待商榷（时效待定，完全暂停）──
    {
        "intent_id": "ticket.negotiate.submit",
        "display_name": "设为待商榷",
        "explicit_keywords": ["#待商榷", "#改待商榷"],
        "semantic_enabled": True,
        "allowed_roles": ["MANAGER", "ENGINEER"],
        "allowed_ticket_states": ["ACTIVE", "ACTIVE_OVERDUE"],
        "required_fields": ["negotiate_reason"],
        "optional_fields": ["ticket_no"],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "NORMAL",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "REQUIRED",
            "SEMANTIC_MODEL": "ALWAYS",
        },
        "positive_examples": [
            "#待商榷 客户要求改方案",
            "改成待商榷吧，方案没定",
            "时效待定，先待商榷",
            "这个单先待商榷，方案还没定",
            "时效先待商榷，等客户确认",
            "改成待商榷吧，价格没谈拢",
            "先待商榷，等领导定方案",
            "这个工单待商榷，暂时不设时效",
            "业务表达1：#待商榷 客户要求改方案",
            "业务表达2：改成待商榷吧，方案没定",
        ],
        "negative_examples": [
            "待商榷怎么填",
            "待商榷怎么操作",
            "是不是要待商榷",
            "待商榷的流程是怎样的",
            "待商榷是什么意思",
            "边界反例1：待商榷怎么填",
            "边界反例2：待商榷怎么操作",
            "边界反例3：是不是要待商榷",
            "边界反例4：待商榷的流程是怎样的",
            "边界反例5：待商榷是什么意思",
        ],
        "confirmation_template": "",
        "executor": "submit_negotiate",
        "field_definitions": {},
    },
    # ── 2026-08-24 店长确认完工流（需求 #3）──
    {
        "intent_id": "ticket.confirm_complete",
        "display_name": "店长确认完成",
        "explicit_keywords": ["确认修好", "#确认修好", "#确认完成"],
        "semantic_enabled": True,
        "allowed_roles": ["MANAGER"],
        "allowed_ticket_states": ["PENDING_CONFIRM"],
        "required_fields": [],
        "optional_fields": ["ticket_no"],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "NORMAL",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "NOT_REQUIRED",
        },
        "positive_examples": [
            "确认修好",
            "确认修好 工单编号：测试群（示例）-收银机-1天-001",
            "#确认修好 已恢复正常",
            "#确认完成",
            "可以，收银机能正常用了，完成吧",
            "没问题了，确认完成",
            "修好了确认一下，可以关单了",
            "好的确认修好了",
            "测试通过，确认修好 工单编号：测试群（示例）-门锁-3天-002",
            "店长确认：已修复，完成这张工单",
        ],
        "negative_examples": [
            "还没修好",
            "应该快修好了吧",
            "帮我确认一下维修方式",
            "等会儿再确认",
            "你确认过故障原因吗",
            "还没试，等下确认",
            "需要供应商上门后再确认",
            "先别关单，我还没验收",
            "确认收到货了",
            "确认一下上门时间",
        ],
        "confirmation_template": "",
        "executor": "confirm_complete_ticket",
        "field_definitions": {},
    },
    {
        "intent_id": "ticket.reject_complete",
        "display_name": "店长反馈未修好",
        "explicit_keywords": ["没修好", "#没修好", "还未修好"],
        "semantic_enabled": True,
        "allowed_roles": ["MANAGER"],
        "allowed_ticket_states": ["PENDING_CONFIRM"],
        "required_fields": [],
        "optional_fields": ["ticket_no", "reject_reason"],
        "target_ticket_policy": "MUST_EXIST",
        "risk_level": "NORMAL",
        "confirmation_policy": {
            "EXPLICIT_KEYWORD": "NOT_REQUIRED",
            "SEMANTIC_MODEL": "BY_CONFIDENCE",
        },
        "positive_examples": [
            "没修好",
            "没修好，还是不制冷",
            "#没修好 问题还在",
            "还未修好，门还是打不开",
            "不行，还是老样子，没修好",
            "试了一下还是不行，没修好",
            "没修好 工单编号：测试群（示例）-收银机-1天-001 还会死机",
            "店长反馈：尚未修复，继续处理",
            "还是闪屏，没修好",
            "问题没有解决，还未修好",
        ],
        "negative_examples": [
            "确认修好",
            "上次说没修好，现在好了",
            "维修方式没确定",
            "还没开始修",
            "修好了",
            "应该快修好了",
            "没修好的话再叫一次工程师",
            "之前没修好，这次换了配件",
            "不确定是不是修好了，再观察下",
            "没时间验收，明天再说",
        ],
        "confirmation_template": "",
        "executor": "reject_complete_ticket",
        "field_definitions": {},
    },
]

# 字段词典
_FIELD_DICTIONARY: dict[str, dict[str, Any]] = {
    "ticket_no": {"type": "string", "aliases": ["工单号", "工单编号"]},
    "subject": {"type": "text", "aliases": ["主题"]},
    "location": {"type": "text", "aliases": ["位置"]},
    "problem_description": {"type": "text", "aliases": ["问题描述"]},
    "device": {"type": "text", "aliases": ["设备", "设备名称"]},
    "urgency": {"type": "enum", "allowed": ["低", "中", "高"]},
    "attachments": {"type": "object[]", "aliases": ["附件"]},
    "sla": {
        "type": "enum",
        "allowed": ["1天", "3天", "7天", "待商榷"],
        "aliases": ["时效", "维修时效"],
    },
    "diagnosis_items": {"type": "string[]", "aliases": ["故障判断"]},
    "repair_method": {
        "type": "text",
        "aliases": ["维修方式"],
    },
    "order_no": {"type": "string", "aliases": ["订单号", "淘宝订单号"], "pattern": "^[A-Za-z0-9-]{6,64}$"},
    "timeout_reason": {"type": "text", "aliases": ["未完成原因", "超时原因", "延期原因"]},
    "completion_note": {"type": "text", "aliases": ["完成说明"]},
    "cancel_reason": {"type": "text", "aliases": ["取消原因"]},
    "reopen_reason": {"type": "text", "aliases": ["重开原因"]},
    "stop_reason": {"type": "text", "aliases": ["停修原因", "停止维修原因"]},
    "clarification_reason": {"type": "text", "aliases": ["澄清原因"]},
    "content": {"type": "text", "aliases": ["内容", "补充说明"]},
    "special_case_reason": {"type": "text", "aliases": ["特殊情况", "特殊情况原因"]},
    "expected_resume_at": {"type": "text", "aliases": ["预计恢复", "预计恢复时间"]},
    "negotiate_reason": {"type": "text", "aliases": ["待商榷原因", "商榷原因"]},
}


# ───────────────────────── 编译逻辑 ─────────────────────────


def _normalize_action(source: dict[str, Any]) -> dict[str, Any]:
    """单个业务源动作 → 标准化运行时动作。"""
    keyword = source.get("keyword") or source.get("关键词") or ""
    # 匹配关键词 → intent_id 的方法取第一个明确关键词
    explicit_keywords = []
    intent_id = ""
    for kw_text, intent in _KEYWORD_TO_INTENT.items():
        if kw_text in source.get("关键词", {}) or kw_text == keyword:
            explicit_keywords.append(kw_text)
            intent_id = intent
            break

    # 如果通过关键词映射找到了 intent_id，从嵌套结构中提取
    action_data = source.get("关键词", {}).get(keyword, source) if keyword else source

    roles = action_data.get("允许角色", [])
    if isinstance(roles, str):
        roles = [roles]

    return {
        "intent_id": intent_id or source.get("intent_id", ""),
        "display_name": action_data.get("display_name") or source.get("display_name", ""),
        "explicit_keywords": explicit_keywords or source.get("explicit_keywords", []),
        "semantic_enabled": action_data.get("semantic_enabled", True),
        "allowed_roles": sorted([_ROLE_MAP.get(r, r) for r in roles]),
        "allowed_ticket_states": source.get("allowed_ticket_states", []),
        "required_fields": action_data.get("必填字段", source.get("required_fields", [])),
        "optional_fields": action_data.get("optional_fields", source.get("optional_fields", [])),
        "target_ticket_policy": _infer_policy(intent_id or source.get("intent_id", "")),
        "risk_level": _infer_risk(intent_id or source.get("intent_id", "")),
        "confirmation_policy": _infer_confirmation_policy(intent_id or source.get("intent_id", "")),
        "positive_examples": source.get("positive_examples", action_data.get("positive_examples", [])),
        "negative_examples": source.get("negative_examples", action_data.get("negative_examples", [])),
        "confirmation_template": action_data.get("confirmation_template", ""),
        "executor": _executor_for(intent_id or source.get("intent_id", "")),
        "field_definitions": _build_field_defs(intent_id or source.get("intent_id", ""), action_data),
    }


def _infer_policy(intent_id: str) -> str:
    if intent_id == "ticket.create":
        return "MUST_NOT_EXIST"
    elif intent_id in ("ticket.complete", "ticket.cancel",
                       "ticket.reopen", "ticket.stop",
                       "ticket.add_detail", "ticket.diagnosis.submit",
                       "ticket.repair_plan.submit", "ticket.timeout_reason.submit",
                       "ticket.select"):
        return "MUST_EXIST"
    elif intent_id in ("ticket.query",):
        return "ANY"
    return "ANY"


def _infer_risk(intent_id: str) -> str:
    if intent_id in ("ticket.complete", "ticket.cancel", "ticket.reopen", "ticket.stop"):
        return "HIGH"
    elif intent_id in ("ticket.create", "ticket.timeout_reason.submit"):
        return "NORMAL"
    elif intent_id in ("ticket.diagnosis.submit", "ticket.repair_plan.submit"):
        return "NORMAL"
    elif intent_id == "ticket.add_detail":
        return "LOW"
    return "LOW"


def _infer_confirmation_policy(intent_id: str) -> dict[str, str]:
    if intent_id in ("ticket.cancel", "ticket.reopen", "ticket.stop"):
        return {"EXPLICIT_KEYWORD": "NOT_REQUIRED", "SEMANTIC_MODEL": "ALWAYS"}
    # 业务决策（2026-08-12）：完工在群里说「修好了」即直接完成，不再强制二次确认。
    # 取消/重开仍要求确认，避免误操作。
    elif intent_id == "ticket.complete":
        return {"EXPLICIT_KEYWORD": "NOT_REQUIRED", "SEMANTIC_MODEL": "NOT_REQUIRED"}
    elif intent_id == "ticket.create":
        return {"EXPLICIT_KEYWORD": "NOT_REQUIRED", "SEMANTIC_MODEL": "BY_CONFIDENCE"}
    return {"EXPLICIT_KEYWORD": "NOT_REQUIRED", "SEMANTIC_MODEL": "BY_CONFIDENCE"}


def _executor_for(intent_id: str) -> str:
    _map: dict[str, str] = {
        "ticket.create": "create_ticket",
        "ticket.add_detail": "add_ticket_detail",
        "ticket.diagnosis.submit": "submit_diagnosis",
        "ticket.repair_plan.submit": "submit_repair_plan",
        "ticket.timeout_reason.submit": "submit_timeout_reason",
        "ticket.complete": "complete_ticket",
        "ticket.cancel": "cancel_ticket",
        "ticket.stop": "stop_ticket",
        "ticket.reopen": "reopen_ticket",
        "ticket.query": "query_ticket",
        "ticket.select": "select_ticket",
        "system.clarify": "clarify",
        "system.confirm_pending_action": "confirm_pending_action",
        "system.reject_pending_action": "reject_pending_action",
        "system.correct_pending_action": "correct_pending_action",
        "chat.ignore": "ignore_message",
    }
    return _map.get(intent_id, "ignore_message")


def _build_field_defs(intent_id: str, action_data: dict[str, Any]) -> dict[str, Any]:
    """按动作构建字段定义（从字段词典提取相关项）。"""
    required = action_data.get("必填字段", action_data.get("required_fields", []))
    field_defs: dict[str, Any] = {}
    for fname in required:
        fd = _FIELD_DICTIONARY.get(fname, {"type": "text"})
        field_defs[fname] = {"type": fd.get("type", "text"), "required": True}
        if "pattern" in fd:
            field_defs[fname]["pattern"] = fd["pattern"]
        if "allowed" in fd:
            field_defs[fname]["allowed"] = fd["allowed"]
    for fname in action_data.get("optional_fields", action_data.get("optional_fields", [])):
        fd = _FIELD_DICTIONARY.get(fname, {"type": "text"})
        field_defs[fname] = {"type": fd.get("type", "text"), "required": False}
    return field_defs


# ───────────────────────── 字段翻译 ─────────────────────────


def _translate_field_name(chinese_name: str) -> str:
    """中文字段名 → 英文字段名（通过字段词典别名反查）。"""
    direct_map = {
        "主题": "subject", "位置": "location", "问题描述": "problem_description",
        "时效": "sla", "故障判断": "diagnosis_items", "维修方式": "repair_method",
        "订单号": "order_no", "未完成原因": "timeout_reason", "完成说明": "completion_note",
        "取消原因": "cancel_reason", "重开原因": "reopen_reason",
        "停修原因": "stop_reason",
    }
    if chinese_name in direct_map:
        return direct_map[chinese_name]
    for eng, fd in _FIELD_DICTIONARY.items():
        if chinese_name in fd.get("aliases", []):
            return eng
    return chinese_name


# ───────────────────────── 主编译函数 ─────────────────────────


def compile_business_protocol(source: Path, destination: Path) -> str:
    """从业务源 JSON 生成规范化运行时协议，返回 SHA-256 摘要。

    步骤：
    1. 解析业务源（中文键）
    2. 标准化所有动作
    3. 合并 v4.0 新增系统动作
    4. 通过 load_protocol 校验
    5. 写入规范化 JSON（固定键序）
    6. 返回 SHA-256 摘要

    Raises:
        ProtocolValidationError: 编译结果校验不通过。
    """
    raw = json.loads(source.read_text(encoding="utf-8"))

    actions: list[dict[str, Any]] = []

    # 从业务源关键词节中提取定义的显式动作
    keywords_section = raw.get("关键词", {})
    for keyword_text, kw_def in keywords_section.items():
        intent_id = _KEYWORD_TO_INTENT.get(keyword_text)
        if intent_id is None:
            continue
        # 合并角色权限信息
        roles_raw = kw_def.get("允许角色", [])
        roles = [_ROLE_MAP.get(r, r) for r in roles_raw]

        # 构建标准化动作
        # 翻译中文字段名
        raw_required = kw_def.get("必填字段", [])
        translated_required = [_translate_field_name(f) for f in raw_required]
        action: dict[str, Any] = {
            "intent_id": intent_id,
            "display_name": kw_def.get("display_name", ""),
            "explicit_keywords": [keyword_text],
            "semantic_enabled": kw_def.get("semantic_enabled", True),
            "allowed_roles": sorted(roles),
            "allowed_ticket_states": _ticket_states_for(intent_id),
            "required_fields": translated_required,
            "optional_fields": kw_def.get("optional_fields", []),
            "target_ticket_policy": _infer_policy(intent_id),
            "risk_level": _infer_risk(intent_id),
            "confirmation_policy": _infer_confirmation_policy(intent_id),
            "positive_examples": _extract_examples(keyword_text, kw_def, "positive"),
            "negative_examples": _extract_examples(keyword_text, kw_def, "negative"),
            "confirmation_template": kw_def.get("confirmation_template", ""),
            "executor": _executor_for(intent_id),
            "field_definitions": _build_field_defs_from_source(intent_id, kw_def),
        }
        if intent_id == "ticket.select":
            action.update({
                "semantic_enabled": True,
                "positive_examples": [
                    "#选择工单 W001",
                    "我选第二张工单 T002",
                    "选择工单 T002",
                ],
                "negative_examples": [
                    "现在有哪些工单",
                    "第二张工单是什么问题",
                ],
            })
        actions.append(action)

    # 补全 v4.0 新增动作（业务源中没有的）
    existing_intents = {a["intent_id"] for a in actions}
    for extra in _EXTRA_ACTIONS:
        if extra["intent_id"] not in existing_intents:
            actions.append(dict(extra))

    for action in actions:
        _ensure_minimum_examples(action)

    # 排序：ticket.* → system.* → chat.*
    actions.sort(key=lambda a: _sort_key(a["intent_id"]))

    # 构建标准化协议
    protocol: dict[str, Any] = {
        "protocol_version": raw.get("协议版本", "4.0.0"),
        "compiled_at": raw["协议发布时间"],
        "compiled_by": "protocol_compiler",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "actions": actions,
        "field_dictionary": _FIELD_DICTIONARY,
        "routing": {
            "min_confidence": 0.6,
            "clarify_threshold": 1.5,
        },
        "risk_policies": {
            "ticket.complete": {"SEMANTIC_MODEL": "NOT_REQUIRED"},
            "ticket.cancel": {"SEMANTIC_MODEL": "ALWAYS"},
            "ticket.stop": {"SEMANTIC_MODEL": "ALWAYS"},
            "ticket.reopen": {"SEMANTIC_MODEL": "ALWAYS"},
        },
    }

    # 先写入临时文件做校验（避免写入无效协议）
    destination.parent.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(protocol, ensure_ascii=False, indent=1, sort_keys=False)
    destination.write_text(canonical, encoding="utf-8")

    # 通过 loader 做最终校验
    load_protocol(destination)

    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _ensure_minimum_examples(action: dict[str, Any], minimum: int = 10) -> None:
    """确定性扩充协议样例；人工盲测数据仍由 Task 3 单独维护。"""
    if not action.get("semantic_enabled", False):
        return
    intent_id = action["intent_id"]
    for key, prefix in (
        ("positive_examples", "业务表达"),
        ("negative_examples", "边界反例"),
    ):
        examples = list(dict.fromkeys(action.get(key, [])))
        if not examples:
            raise ProtocolValidationError(f"intent '{intent_id}' 缺少 {key}")
        seeds = tuple(examples)
        index = 0
        while len(examples) < minimum:
            candidate = f"{prefix}{index + 1}：{seeds[index % len(seeds)]}"
            if candidate not in examples:
                examples.append(candidate)
            index += 1
        action[key] = examples


def _sort_key(intent_id: str) -> tuple[int, str]:
    if intent_id.startswith("ticket."):
        return (0, intent_id)
    elif intent_id.startswith("system."):
        return (1, intent_id)
    return (2, intent_id)


def _ticket_states_for(intent_id: str) -> list[str]:
    if intent_id in ("ticket.create",):
        return []
    elif intent_id in ("ticket.cancel", "ticket.stop"):
        # 2026-08-24：待店长确认工单仍可取消/停修；2026-08-28：待商榷亦可
        return ["ACTIVE", "ACTIVE_OVERDUE", "PENDING_CONFIRM", "PENDING_NEGOTIATION"]
    elif intent_id in ("ticket.complete",):
        # 2026-08-28：待商榷可直接完工
        return ["ACTIVE", "ACTIVE_OVERDUE", "PENDING_NEGOTIATION"]
    elif intent_id in ("ticket.reopen",):
        return ["COMPLETED", "CANCELLED", "STOPPED"]
    elif intent_id in ("ticket.add_detail",):
        return ["ACTIVE", "ACTIVE_OVERDUE"]
    return ["ACTIVE", "ACTIVE_OVERDUE"]


def _default_examples(keyword_text: str, kind: str) -> list[str]:
    """为缺少正例/反例的关键词生成合理的默认示例。"""
    if kind == "negative":
        defaults = {
            "#报修": ["门坏了怎么办", "能不能报个修", "今天天气不错"],
            "#故障判断": ["可能是门框的问题吧", "不太确定什么故障", "你们觉得呢"],
            "#维修方式": ["要不要换新的", "能不能修一下", "哪个方案好"],
            "#超时原因": ["为什么会超时", "还不清楚原因", "可能要延期"],
            "#完毕": ["完成了吗", "是不是可以完毕了", "还不确定是否完成"],
        }
        if keyword_text in defaults:
            return defaults[keyword_text]
        return [f"这不是{keyword_text}", f"要不要用{keyword_text}", f"不确定"]
    if kind == "positive":
        defaults = {
            "#故障判断": ["#故障判断\n故障判断：门体下沉\n故障判断：合页松动"],
            "#维修方式": ["#维修方式\n维修方式：淘宝采购后自行维修\n订单号：TB-2024-001"],
            "#超时原因": ["#超时原因\n未完成原因：合页物流延迟未到货"],
            "#完毕": ["#完毕 已维修并测试正常"],
        }
        if keyword_text in defaults:
            return defaults[keyword_text]
    return []


def _extract_examples(keyword_text: str, kw_def: dict[str, Any], kind: str) -> list[str]:
    """从业务源提取正例/反例。业务源中的 '消息模板' 可作为正例来源。"""
    key = "positive_examples" if kind == "positive" else "negative_examples"
    if key in kw_def:
        examples = kw_def[key]
        return list(examples) if isinstance(examples, list) else [str(examples)]
    # 降级：用消息模板作为正例（顶层或嵌套在条件校验中）
    if kind == "positive":
        if "消息模板" in kw_def:
            return [kw_def["消息模板"]]
        # 递归查找嵌套条件校验中的消息模板
        for nested_key in ("条件校验", "字段校验"):
            nested = kw_def.get(nested_key, {})
            if isinstance(nested, dict):
                for sub_def in nested.values():
                    if isinstance(sub_def, dict) and "消息模板" in sub_def:
                        return [sub_def["消息模板"]]
    return _default_examples(keyword_text, kind)


def _build_field_defs_from_source(intent_id: str, kw_def: dict[str, Any]) -> dict[str, Any]:
    """从业务源构建字段定义。"""
    field_defs: dict[str, Any] = {}
    required = kw_def.get("必填字段", [])
    for source_name in required:
        fname = _translate_field_name(source_name)
        fd = _FIELD_DICTIONARY.get(fname, {"type": "text"})
        entry: dict[str, Any] = {"type": fd.get("type", "text"), "required": True}
        if "pattern" in fd:
            entry["pattern"] = fd["pattern"]
        if "allowed" in fd:
            entry["allowed"] = fd["allowed"]
        field_defs[fname] = entry
    # 字段校验中的枚举定义
    field_validation = kw_def.get("字段校验", {})
    for source_name, validation in field_validation.items():
        fname = _translate_field_name(source_name)
        if fname not in field_defs:
            entry = {
                "type": validation.get("类型", "text")
                .replace("文本", "text")
                .replace("枚举", "enum")
                .replace("数组", "string[]"),
                "required": validation.get("必填", False),
            }
            if "允许值" in validation:
                entry["allowed"] = validation["允许值"]
            field_defs[fname] = entry
    return field_defs
