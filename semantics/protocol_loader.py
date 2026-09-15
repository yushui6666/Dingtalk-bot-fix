"""运行时协议加载与校验（计划书 Task 1）。

加载器负责：
1. JSON 文件读取 + JSON Schema 结构校验
2. 执行器白名单校验（15 个标准执行器）
3. 关键词唯一性校验（跨动作无重复）
4. 正反例数量校验（semantic_enabled 动作至少各 10 条）
5. 角色值、target_ticket_policy、风险等级等枚举校验
6. 返回不可变的 TicketProtocol 对象
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema


# ───────────────────────── 错误类型 ─────────────────────────


class ProtocolValidationError(ValueError):
    """协议校验失败（结构/语义/业务规则任一不通过）。"""


# ───────────────────────── 执行器白名单 ─────────────────────────

ALLOWED_EXECUTORS = frozenset({
    "create_ticket", "add_ticket_detail", "submit_diagnosis",
    "submit_repair_plan", "submit_timeout_reason", "complete_ticket",
    "confirm_complete_ticket", "reject_complete_ticket",
    "cancel_ticket", "stop_ticket", "reopen_ticket", "query_ticket", "select_ticket",
    "clarify", "confirm_pending_action", "reject_pending_action",
    "correct_pending_action", "ignore_message",
    "submit_special_case", "submit_negotiate",
})

ALLOWED_ROLES = frozenset({"MANAGER", "ENGINEER", "LEADER", "OTHER", "SYSTEM"})

ALLOWED_TARGET_TICKET_POLICIES = frozenset({
    "MUST_EXIST", "MUST_NOT_EXIST", "ANY", "MUST_EXIST_OR_NONE",
})

ALLOWED_RISK_LEVELS = frozenset({"LOW", "NORMAL", "HIGH", "CRITICAL"})

MIN_EXAMPLES_PER_CLASS = 10


# ───────────────────────── 协议数据类 ─────────────────────────


@dataclass(frozen=True)
class ActionDefinition:
    """协议中的单个动作定义（只读）。"""
    intent_id: str
    display_name: str
    explicit_keywords: tuple[str, ...]
    semantic_enabled: bool
    allowed_roles: tuple[str, ...]
    allowed_ticket_states: tuple[str, ...]
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    target_ticket_policy: str
    risk_level: str
    confirmation_policy: dict[str, str]
    positive_examples: tuple[str, ...]
    negative_examples: tuple[str, ...]
    confirmation_template: str
    executor: str
    field_definitions: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class TicketProtocol:
    """运行时协议（只读）。所有消费者通过此对象读取动作定义和字段词典。"""
    protocol_version: str
    compiled_at: str
    compiled_by: str
    source_sha256: str
    actions: tuple[ActionDefinition, ...]
    field_dictionary: dict[str, dict[str, Any]]
    routing: dict[str, Any]
    risk_policies: dict[str, dict[str, str]]

    def get_action(self, intent_id: str) -> ActionDefinition | None:
        """按 intent_id 查找动作定义。"""
        for action in self.actions:
            if action.intent_id == intent_id:
                return action
        return None

    def find_by_keyword(self, text: str) -> ActionDefinition | None:
        """按显式关键词查找动作定义（首词匹配）。"""
        stripped = text.strip()
        for action in self.actions:
            for kw in action.explicit_keywords:
                if stripped.startswith(kw):
                    # 关键词必须是完整词（后接空格、换行或结束）
                    after = stripped[len(kw):]
                    if not after or after[0] in (' ', '\t', '\n', '\r'):
                        return action
        return None


# ───────────────────────── Schema 加载 ─────────────────────────

_PROTOCOL_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.schema.json"

_schema_cache: dict[str, Any] | None = None


def load_protocol_schema() -> dict[str, Any]:
    """加载协议 JSON Schema（带缓存）。"""
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(_PROTOCOL_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema_cache


# ───────────────────────── 语义校验 ─────────────────────────


def _validate_unique_keywords(actions: list[dict[str, Any]]) -> None:
    """跨动作关键词必须唯一。"""
    seen: dict[str, str] = {}
    for action in actions:
        for kw in action.get("explicit_keywords", []):
            kw = kw.strip()
            if not kw:
                continue
            if kw in seen:
                raise ProtocolValidationError(
                    f"关键词 '{kw}' 在 intent '{action['intent_id']}' 和 "
                    f"'{seen[kw]}' 中重复出现"
                )
            seen[kw] = action["intent_id"]


def _validate_executor_allowlist(actions: list[dict[str, Any]]) -> None:
    """所有 executor 必须在白名单内。"""
    for action in actions:
        executor = action.get("executor", "")
        if executor not in ALLOWED_EXECUTORS:
            raise ProtocolValidationError(
                f"intent '{action['intent_id']}' 的 executor '{executor}' "
                f"不在白名单中。允许值: {sorted(ALLOWED_EXECUTORS)}"
            )


def _validate_examples(actions: list[dict[str, Any]], minimum_per_class: int = MIN_EXAMPLES_PER_CLASS) -> None:
    """semantic_enabled=True 的动作至少需要 N 条正例和 N 条反例。"""
    for action in actions:
        if action.get("semantic_enabled", False):
            pos = action.get("positive_examples", [])
            neg = action.get("negative_examples", [])
            if len(pos) < minimum_per_class or len(neg) < minimum_per_class:
                raise ProtocolValidationError(
                    f"intent '{action['intent_id']}' semantic_enabled=True "
                    f"但正例={len(pos)}条／反例={len(neg)}条，至少需要各 {minimum_per_class} 条"
                )


def _validate_enum_fields(actions: list[dict[str, Any]]) -> None:
    """校验角色、target_ticket_policy、风险等级等枚举字段值。"""
    for action in actions:
        for role in action.get("allowed_roles", []):
            if role not in ALLOWED_ROLES:
                raise ProtocolValidationError(
                    f"intent '{action['intent_id']}' 有非法角色 '{role}'。"
                    f"允许值: {sorted(ALLOWED_ROLES)}"
                )
        policy = action.get("target_ticket_policy", "")
        if policy not in ALLOWED_TARGET_TICKET_POLICIES:
            raise ProtocolValidationError(
                f"intent '{action['intent_id']}' target_ticket_policy='{policy}' 非法。"
                f"允许值: {sorted(ALLOWED_TARGET_TICKET_POLICIES)}"
            )
        risk = action.get("risk_level", "")
        if risk not in ALLOWED_RISK_LEVELS:
            raise ProtocolValidationError(
                f"intent '{action['intent_id']}' risk_level='{risk}' 非法。"
                f"允许值: {sorted(ALLOWED_RISK_LEVELS)}"
            )


# ───────────────────────── 主加载器 ─────────────────────────


def load_protocol(path: Path) -> TicketProtocol:
    """加载并校验运行时协议 JSON 文件。

    校验顺序：JSON 解析 → JSON Schema → 关键词唯一 → 执行器白名单 → 正反例 → 枚举值。

    Raises:
        ProtocolValidationError: 任一校验不通过（含 jsonschema.ValidationError）。
        FileNotFoundError: 文件不存在。
        json.JSONDecodeError: JSON 解析失败。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    schema = load_protocol_schema()
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as e:
        raise ProtocolValidationError(str(e)) from e

    actions: list[dict[str, Any]] = raw.get("actions", [])
    _validate_unique_keywords(actions)
    _validate_executor_allowlist(actions)
    _validate_examples(actions)
    _validate_enum_fields(actions)

    # 构造不可变 ActionDefinition
    action_defs: list[ActionDefinition] = []
    for a in actions:
        action_defs.append(ActionDefinition(
            intent_id=a["intent_id"],
            display_name=a["display_name"],
            explicit_keywords=tuple(a.get("explicit_keywords", [])),
            semantic_enabled=a.get("semantic_enabled", False),
            allowed_roles=tuple(a.get("allowed_roles", [])),
            allowed_ticket_states=tuple(a.get("allowed_ticket_states", [])),
            required_fields=tuple(a.get("required_fields", [])),
            optional_fields=tuple(a.get("optional_fields", [])),
            target_ticket_policy=a.get("target_ticket_policy", "ANY"),
            risk_level=a.get("risk_level", "NORMAL"),
            confirmation_policy=a.get("confirmation_policy", {}),
            positive_examples=tuple(a.get("positive_examples", [])),
            negative_examples=tuple(a.get("negative_examples", [])),
            confirmation_template=a.get("confirmation_template", ""),
            executor=a.get("executor", ""),
            field_definitions=a.get("field_definitions", {}),
        ))

    return TicketProtocol(
        protocol_version=raw["protocol_version"],
        compiled_at=raw["compiled_at"],
        compiled_by=raw.get("compiled_by", ""),
        source_sha256=raw.get("source_sha256", ""),
        actions=tuple(action_defs),
        field_dictionary=raw.get("field_dictionary", {}),
        routing=raw.get("routing", {}),
        risk_policies=raw.get("risk_policies", {}),
    )
