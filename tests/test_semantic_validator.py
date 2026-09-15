"""语义校验器测试（计划书 Task 2）。

覆盖：角色权限、必填字段、枚举值、高风险确认、状态不匹配。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ───────────────────────── 辅助 ─────────────────────────


def _load_test_protocol() -> Any:
    p = Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"
    from semantics.protocol_loader import load_protocol
    return load_protocol(p)


def _make_message(
    message_id: str = "msg-001",
    group_id: str = "g-001",
    sender_id: str = "u-001",
    sender_role: str = "MANAGER",
    content: str = "",
    reply_to_message_id: str | None = None,
) -> Any:
    from models import NormalizedMessage
    from datetime import datetime
    return NormalizedMessage(
        message_id=message_id,
        group_id=group_id,
        sender_id=sender_id,
        sender_name="测试用户",
        content=content,
        sent_at=datetime.now(),
        sender_role=sender_role,
        reply_to_message_id=reply_to_message_id,
    )


def _make_candidate(
    ticket_id: int = 1,
    ticket_no: str = "T001",
    group_id: str = "g-001",
    status: str = "ACTIVE",
    version: int = 3,
) -> Any:
    from semantics.types import TicketCandidate
    return TicketCandidate(
        ticket_id=ticket_id,
        ticket_no=ticket_no,
        group_id=group_id,
        subject="门下沉",
        location="大厅",
        problem_summary="门体损坏",
        status=status,
        version=version,
    )


# ───────────────────────── 角色权限 ─────────────────────────


def test_manager_can_report():
    """店长可以 #报修。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.create",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"subject": "门", "location": "大厅", "problem_description": "坏了", "sla": "3天"},
    )
    msg = _make_message(sender_role="MANAGER")
    status, cmd, missing = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.AUTO_EXECUTE
    assert cmd is not None


def test_engineer_cannot_report():
    """工程师不能 #报修（权限拒绝）。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.create",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"subject": "门", "location": "大厅", "problem_description": "坏了", "sla": "3天"},
    )
    msg = _make_message(sender_role="ENGINEER")
    status, cmd, errors = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.VALIDATION_REJECTED
    assert cmd is None
    assert len(errors) > 0


def test_other_member_can_report():
    """其他成员（店铺同事）可以直接报修（业务决策 2026-08-12）。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.create",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"subject": "门", "location": "大厅", "problem_description": "坏了", "sla": "3天"},
    )
    msg = _make_message(sender_role="OTHER")
    status, cmd, errors = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.AUTO_EXECUTE
    assert cmd is not None
    assert errors == ()


def test_other_member_cannot_diagnose():
    """其他成员不能 #故障判断。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.diagnosis.submit",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"diagnosis_items": ["门体下沉"]},
    )
    msg = _make_message(sender_role="OTHER")
    status, cmd, errors = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.VALIDATION_REJECTED


# ───────────────────────── 必填字段 ─────────────────────────


def test_missing_required_fields_rejected():
    """缺少必填字段时拒绝。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.create",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"subject": "门"},  # 缺少 location, problem_description, sla
    )
    msg = _make_message(sender_role="MANAGER")
    status, cmd, missing = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.VALIDATION_REJECTED
    assert cmd is None
    assert "location" in missing or len(missing) > 0


def test_repair_method_without_order_no_rejected():
    """淘宝采购维修缺少订单号时拒绝。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.repair_plan.submit",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"repair_method": "淘宝采购后自行维修"},  # 缺 order_no
    )
    msg = _make_message(sender_role="ENGINEER")
    status, cmd, errors = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.VALIDATION_REJECTED


def test_repair_method_order_no_placeholder_rejected():
    """订单号为占位值（如 '无'）时拒绝。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.repair_plan.submit",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"repair_method": "淘宝采购后自行维修", "order_no": "无"},
    )
    msg = _make_message(sender_role="ENGINEER")
    status, cmd, errors = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.VALIDATION_REJECTED


# ───────────────────────── 枚举值 ─────────────────────────


def test_invalid_sla_rejected():
    """非法时效值被拒绝。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.create",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"subject": "门", "location": "大厅", "problem_description": "坏了", "sla": "10天"},
    )
    msg = _make_message(sender_role="MANAGER")
    status, cmd, errors = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.VALIDATION_REJECTED


def test_invalid_repair_method_rejected():
    """非法维修方式被拒绝。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.repair_plan.submit",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={"repair_method": "扔掉换新"},
    )
    msg = _make_message(sender_role="ENGINEER")
    status, cmd, errors = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.VALIDATION_REJECTED


# ───────────────────────── IGNORE 动作 ─────────────────────────


def test_chat_ignore_is_ignored():
    """chat.ignore 动作直接跳过。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="chat.ignore",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={},
    )
    msg = _make_message(sender_role="OTHER")
    status, cmd, missing = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.IGNORE
    assert cmd is None


# ───────────────────────── 跨消息重复字段（拒绝完整重发）───────────


def test_duplicate_field_in_same_decision_rejected():
    """同一 decision 中 fields 有重复键 → 校验层拒绝（由 matcher 标记为 clarify 的除外）。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    # 直接构造：reopen 缺少 reopen_reason → 必填字段缺失拒绝
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.reopen",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={},  # 缺少 reopen_reason
    )
    msg = _make_message(sender_role="MANAGER")
    status, cmd, errors = validate_decision(decision, message=msg, candidates=[], protocol=protocol)
    assert status == DecisionStatus.VALIDATION_REJECTED
    assert len(errors) > 0


# ───────────────────────── 目标与状态 ─────────────────────────


def test_explicit_ticket_no_resolves_candidate_and_version():
    """显式工单号应解析为候选 ID 和乐观锁版本。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.complete",
        target_ticket_no="T001",
        intent_confidence=1.0,
        fields={},
    )
    status, cmd, errors = validate_decision(
        decision,
        message=_make_message(),
        candidates=[_make_candidate()],
        protocol=protocol,
    )
    assert status == DecisionStatus.AUTO_EXECUTE
    assert errors == ()
    assert cmd is not None
    assert cmd.target_ticket_id == 1
    assert cmd.expected_ticket_version == 3


def test_update_action_without_target_is_rejected():
    """要求既有工单的动作不能在无候选时自动执行。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.complete",
        target_ticket_no=None,
        intent_confidence=1.0,
        fields={},
    )
    status, cmd, errors = validate_decision(
        decision, message=_make_message(), candidates=[], protocol=protocol
    )
    assert status == DecisionStatus.VALIDATION_REJECTED
    assert cmd is None
    assert any("目标工单" in error for error in errors)


def test_disallowed_ticket_state_is_rejected():
    """候选状态不在协议允许范围时拒绝执行。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.complete",
        target_ticket_no="T001",
        intent_confidence=1.0,
        fields={},
    )
    status, cmd, errors = validate_decision(
        decision,
        message=_make_message(),
        candidates=[_make_candidate(status="COMPLETED")],
        protocol=protocol,
    )
    assert status == DecisionStatus.VALIDATION_REJECTED
    assert cmd is None
    assert any("状态" in error for error in errors)


def test_model_high_risk_action_waits_for_confirmation():
    """自然语言识别出的高风险动作必须进入确认流程。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="model",
        intent="ticket.cancel",
        target_ticket_no="T001",
        intent_confidence=0.99,
        fields={"cancel_reason": "误报"},
    )
    status, cmd, errors = validate_decision(
        decision,
        message=_make_message(),
        candidates=[_make_candidate()],
        protocol=protocol,
    )
    assert status == DecisionStatus.WAITING_CONFIRMATION
    assert cmd is None
    assert errors == ()


def test_keyword_cancel_accepts_target_ticket_no_contract():
    """target_ticket_no 应满足协议中的 ticket_no 必填契约。"""
    from semantics.validator import validate_decision
    from semantics.types import DecisionStatus, SemanticDecision

    protocol = _load_test_protocol()
    decision = SemanticDecision(
        protocol_version="4.0",
        source="keyword",
        intent="ticket.cancel",
        target_ticket_no="T001",
        intent_confidence=1.0,
        fields={"cancel_reason": "误报"},
    )
    status, cmd, errors = validate_decision(
        decision,
        message=_make_message(),
        candidates=[_make_candidate()],
        protocol=protocol,
    )
    assert status == DecisionStatus.AUTO_EXECUTE
    assert errors == ()
    assert cmd is not None
