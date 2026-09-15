"""协议加载器与编译器测试（计划书 Task 1）。

TDD 驱动：先写非法协议被拒绝的失败测试，再实现加载器。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ───────────────────────── 测试辅助 ─────────────────────────


def write_json(tmp_path: Path, data: dict[str, Any]) -> Path:
    """写入临时 JSON 文件并返回路径。"""
    p = tmp_path / "protocol.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def minimal_protocol(**overrides: Any) -> dict[str, Any]:
    """构造最小合法协议，通过 overrides 注入非法字段供负面测试。"""
    positive_examples = [f"#报修 合法正例 {index}" for index in range(10)]
    negative_examples = [f"非报修反例 {index}" for index in range(10)]
    base: dict[str, Any] = {
        "protocol_version": "4.0.0",
        "compiled_at": "2026-08-11T00:00:00",
        "compiled_by": "test",
        "source_sha256": "sha256:deadbeef",
        "actions": [
            {
                "intent_id": "ticket.create",
                "display_name": "新建维修工单",
                "explicit_keywords": ["#报修"],
                "semantic_enabled": True,
                "allowed_roles": ["MANAGER"],
                "allowed_ticket_states": [],
                "required_fields": ["subject", "location", "problem_description", "sla"],
                "optional_fields": ["device", "urgency"],
                "target_ticket_policy": "MUST_NOT_EXIST",
                "risk_level": "NORMAL",
                "confirmation_policy": {
                    "EXPLICIT_KEYWORD": "NOT_REQUIRED",
                    "SEMANTIC_MODEL": "BY_CONFIDENCE",
                },
                "positive_examples": positive_examples,
                "negative_examples": negative_examples,
                "confirmation_template": "",
                "executor": "create_ticket",
                "field_definitions": {
                    "subject": {"type": "text", "required": True},
                    "location": {"type": "text", "required": True},
                    "problem_description": {"type": "text", "required": True},
                    "sla": {"type": "enum", "required": True, "allowed": ["1天", "3天", "7天"]},
                },
            }
        ],
        "field_dictionary": {
            "subject": {"type": "text", "aliases": ["主题"]},
            "location": {"type": "text", "aliases": ["位置"]},
            "problem_description": {"type": "text", "aliases": ["问题描述"]},
            "sla": {"type": "enum", "allowed": ["1天", "3天", "7天"]},
        },
        "routing": {
            "min_confidence": 0.6,
            "clarify_threshold": 1.5,
        },
        "risk_policies": {
            "ticket.complete": {"SEMANTIC_MODEL": "ALWAYS"},
            "ticket.cancel": {"SEMANTIC_MODEL": "ALWAYS"},
            "ticket.reopen": {"SEMANTIC_MODEL": "ALWAYS"},
        },
    }
    base.update(overrides)
    return base


# ───────────────────────── 负面测试：非法协议被拒绝 ─────────────────────────


def test_protocol_rejects_unknown_executor(tmp_path):
    """未知执行器被拒绝。"""
    protocol = minimal_protocol()
    protocol["actions"][0]["executor"] = "delete_everything"
    from semantics.protocol_loader import ProtocolValidationError, load_protocol

    with pytest.raises(ProtocolValidationError):
        load_protocol(write_json(tmp_path, protocol))


def test_protocol_rejects_duplicate_keywords(tmp_path):
    """同一关键词重复出现被拒绝。"""
    protocol = minimal_protocol()
    protocol["actions"].append(dict(protocol["actions"][0]))
    protocol["actions"][1]["intent_id"] = "ticket.add_detail"
    protocol["actions"][1]["explicit_keywords"] = ["#报修"]  # 与第一个动作重复
    from semantics.protocol_loader import ProtocolValidationError, load_protocol

    with pytest.raises(ProtocolValidationError):
        load_protocol(write_json(tmp_path, protocol))


def test_protocol_rejects_empty_examples(tmp_path):
    """semantic_enabled=True 但正例/反例不足被拒绝。"""
    protocol = minimal_protocol()
    protocol["actions"][0]["positive_examples"] = []
    protocol["actions"][0]["negative_examples"] = []
    from semantics.protocol_loader import ProtocolValidationError, load_protocol

    with pytest.raises(ProtocolValidationError):
        load_protocol(write_json(tmp_path, protocol))


def test_protocol_rejects_fewer_than_ten_examples_per_class(tmp_path):
    """semantic_enabled=True 的动作正反例必须各不少于 10 条。"""
    protocol = minimal_protocol()
    protocol["actions"][0]["positive_examples"] = protocol["actions"][0]["positive_examples"][:9]
    from semantics.protocol_loader import ProtocolValidationError, load_protocol

    with pytest.raises(ProtocolValidationError):
        load_protocol(write_json(tmp_path, protocol))


def test_protocol_rejects_missing_intent_id(tmp_path):
    """缺少 intent_id 被拒绝。"""
    protocol = minimal_protocol()
    del protocol["actions"][0]["intent_id"]
    from semantics.protocol_loader import ProtocolValidationError, load_protocol

    with pytest.raises(ProtocolValidationError):
        load_protocol(write_json(tmp_path, protocol))


def test_protocol_rejects_unknown_role(tmp_path):
    """allowed_roles 含非法角色值被拒绝。"""
    protocol = minimal_protocol()
    protocol["actions"][0]["allowed_roles"] = ["SUPERADMIN"]
    from semantics.protocol_loader import ProtocolValidationError, load_protocol

    with pytest.raises(ProtocolValidationError):
        load_protocol(write_json(tmp_path, protocol))


def test_protocol_rejects_invalid_target_ticket_policy(tmp_path):
    """非法 target_ticket_policy 被拒绝。"""
    protocol = minimal_protocol()
    protocol["actions"][0]["target_ticket_policy"] = "MAYBE_EXISTS"
    from semantics.protocol_loader import ProtocolValidationError, load_protocol

    with pytest.raises(ProtocolValidationError):
        load_protocol(write_json(tmp_path, protocol))


def test_protocol_rejects_missing_executor(tmp_path):
    """缺少 executor 字段被拒绝。"""
    protocol = minimal_protocol()
    del protocol["actions"][0]["executor"]
    from semantics.protocol_loader import ProtocolValidationError, load_protocol

    with pytest.raises(ProtocolValidationError):
        load_protocol(write_json(tmp_path, protocol))


# ───────────────────────── 正向测试：合法协议正常加载 ─────────────────────────


def test_load_valid_minimal_protocol(tmp_path):
    """合法最小协议可以成功加载并使用。"""
    from semantics.protocol_loader import load_protocol

    protocol = minimal_protocol()
    tp = load_protocol(write_json(tmp_path, protocol))

    assert tp.protocol_version == "4.0.0"
    assert len(tp.actions) == 1
    assert tp.actions[0].intent_id == "ticket.create"
    assert tp.actions[0].executor == "create_ticket"
    assert "#报修" in tp.actions[0].explicit_keywords
    assert tp.actions[0].allowed_roles == ("MANAGER",)

    # 通过关键词查找动作
    action = tp.find_by_keyword("#报修 主题：门 位置：大厅")
    assert action is not None
    assert action.intent_id == "ticket.create"

    # 非关键词不应命中
    assert tp.find_by_keyword("门坏了需要修") is None

    # get_action
    assert tp.get_action("ticket.create") is not None
    assert tp.get_action("ticket.nonexistent") is None


def test_compiled_protocol_matches_v4_contract(tmp_path):
    """生产协议满足版本、角色权限和语义样例门槛。"""
    from semantics.protocol_loader import load_protocol

    protocol_path = Path(__file__).parents[1] / "protocols" / "ticket_semantics.v4.json"
    protocol = load_protocol(protocol_path)

    assert protocol.protocol_version == "4.0.0"
    for intent_id in ("ticket.add_detail", "ticket.query", "ticket.select"):
        action = protocol.get_action(intent_id)
        assert action is not None
        assert "OTHER" in action.allowed_roles

    select = protocol.get_action("ticket.select")
    assert select is not None and select.semantic_enabled

    add_detail = protocol.get_action("ticket.add_detail")
    reopen = protocol.get_action("ticket.reopen")
    timeout_reason = protocol.get_action("ticket.timeout_reason.submit")
    assert add_detail is not None and add_detail.risk_level == "LOW"
    assert reopen is not None and reopen.target_ticket_policy == "MUST_EXIST"
    assert timeout_reason is not None
    assert protocol.get_action("ticket.timeout.submit") is None

    for action in protocol.actions:
        assert set(action.field_definitions).issubset(
            set(protocol.field_dictionary)
        ), action.intent_id

    for action in protocol.actions:
        if action.semantic_enabled:
            assert len(action.positive_examples) >= 10, action.intent_id
            assert len(action.negative_examples) >= 10, action.intent_id


def test_compiler_is_reproducible_and_matches_committed_protocol(tmp_path):
    """同一业务源重复编译结果逐字节一致，并等于已提交运行时协议。"""
    from semantics.protocol_compiler import compile_business_protocol

    project_root = Path(__file__).parents[1]
    source = project_root / "dashbord" / "维修工单_流程关键词.json"
    assert source.exists(), f"业务源文件不存在: {source}"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    compile_business_protocol(source, first)
    compile_business_protocol(source, second)

    committed = project_root / "protocols" / "ticket_semantics.v4.json"
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == committed.read_bytes()


def test_protocol_keyword_boundary(tmp_path):
    """关键词边界：'#报修了吗' 不命中，'#报修' 命中。"""
    from semantics.protocol_loader import load_protocol

    protocol = minimal_protocol()
    tp = load_protocol(write_json(tmp_path, protocol))

    assert tp.find_by_keyword("#报修了吗") is None, "不同关键词开头不应命中"
    action = tp.find_by_keyword("#报修 主题：门 位置：大厅 问题描述：坏了 时效：3天")
    assert action is not None
    assert action.intent_id == "ticket.create"
