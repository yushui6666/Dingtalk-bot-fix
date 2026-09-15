"""关键词匹配器测试（计划书 Task 2）。

TDD：先写会失败的测试，再实现 keyword_matcher.py。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ───────────────────────── 辅助 ─────────────────────────


def _load_test_protocol() -> Any:
    """加载编译后的运行时协议供测试使用。"""
    p = Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"
    from semantics.protocol_loader import load_protocol
    return load_protocol(p)


# ───────────────────────── 关键词边界 ─────────────────────────


def test_keyword_boundary_not_exact_prefix():
    """'#完毕了吗' 中 #完毕 不是独立关键词，不应命中。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    assert match_keyword("#完毕了吗", protocol) is None


def test_keyword_boundary_with_ticket_no():
    """'#完毕 T001' 应命中 ticket.complete 并提取工单编号。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    result = match_keyword("#完毕 T001", protocol)
    assert result is not None
    assert result.intent == "ticket.complete"
    assert result.target_ticket_no == "T001"  # 工单编号在 target_ticket_no 字段


def test_keyword_boundary_space_after():
    """关键词后接空格时分离，仍命中。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    result = match_keyword("#报修 测试", protocol)
    assert result is not None
    assert result.intent == "ticket.create"


def test_keyword_not_at_start():
    """关键词不在消息开头不命中。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    assert match_keyword("请帮忙 #报修 主题：门", protocol) is None


def test_keyword_leading_whitespace():
    """去除首尾空白后关键词仍命中。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    result = match_keyword("  \t #报修 主题：门 位置：大厅 问题描述：坏了 时效：3天", protocol)
    assert result is not None
    assert result.intent == "ticket.create"


# ───────────────────────── 字段解析 ─────────────────────────


def test_parse_required_fields():
    """#报修 完整字段正确解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#报修 主题：博物馆奇妙夜 位置：第一个房间的门 问题描述：门下沉明显，开门时发生剐蹭 时效：3天"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.fields["subject"] == "博物馆奇妙夜"
    assert result.fields["location"] == "第一个房间的门"
    assert result.fields["problem_description"] == "门下沉明显，开门时发生剐蹭"
    assert result.fields["sla"] == "3天"


def test_parse_diagnosis_fields():
    """#故障判断 多行字段正确解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#故障判断\n故障判断：门体明显下沉\n故障判断：上侧合页存在松动"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.intent == "ticket.diagnosis.submit"
    assert len(result.fields.get("diagnosis_items", [])) == 2
    assert "门体明显下沉" in result.fields["diagnosis_items"]
    assert "上侧合页存在松动" in result.fields["diagnosis_items"]


def test_parse_repair_method_with_order_no():
    """#维修方式 含订单号正确解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#维修方式\n维修方式：淘宝采购后自行维修\n订单号：TB-2024-001"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.intent == "ticket.repair_plan.submit"
    assert result.fields.get("repair_method") == "淘宝采购后自行维修"
    assert result.fields.get("order_no") == "TB-2024-001"


def test_parse_timeout_reason():
    """#超时原因 字段解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#超时原因\n未完成原因：新合页物流延迟，未能按期到货"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.intent == "ticket.timeout_reason.submit"
    assert result.fields.get("timeout_reason") == "新合页物流延迟，未能按期到货"


def test_parse_cancel():
    """#取消工单 字段解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#取消工单\n原因：误报，实际无故障"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.intent == "ticket.cancel"
    assert result.fields.get("cancel_reason") == "误报，实际无故障"


def test_parse_protocol_cancel_example():
    """协议正式示例中的字段别名和位置式编号均应正确解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    result = match_keyword("#取消工单 工单编号：W001 取消原因：误报", protocol)
    assert result is not None
    assert result.target_ticket_no == "W001"
    assert result.fields == {"cancel_reason": "误报"}
    assert result.missing_fields == ()


def test_parse_positional_ticket_no_before_fields():
    """位置式工单号不得污染紧随其后的字段名。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    result = match_keyword("#取消工单 T001 原因：误报", protocol)
    assert result is not None
    assert result.target_ticket_no == "T001"
    assert result.fields == {"cancel_reason": "误报"}
    assert result.missing_fields == ()


def test_parse_reopen():
    """#重开工单 字段解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#重开工单\n原因：门再次下沉，需要重新维修"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.intent == "ticket.reopen"
    assert result.fields.get("reopen_reason") == "门再次下沉，需要重新维修"


def test_parse_protocol_reopen_example():
    """协议正式重开示例中的字段别名应正确解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    result = match_keyword("#重开工单 工单编号：W001 重开原因：复发", protocol)
    assert result is not None
    assert result.target_ticket_no == "W001"
    assert result.fields == {"reopen_reason": "复发"}
    assert result.missing_fields == ()


# ───────────────────────── 结肠/冒号归一 ─────────────────────────


def test_fullwidth_colon_normalized():
    """全角冒号 ： 归一化为半角 ：，字段仍可解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#报修\n主题：门下沉\n位置：门口\n问题描述：剐蹭\n时效：3天"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.fields["subject"] == "门下沉"


# ───────────────────────── 重复字段冲突 ─────────────────────────


def test_duplicate_field_different_values_rejected():
    """同一必填字段重复且值不一致时返回 system.clarify。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#报修 主题：门 位置：大厅 问题描述：坏了 时效：3天 时效：7天"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.intent == "system.clarify"


# ───────────────────────── 双关键词冲突 ─────────────────────────


def test_two_keywords_triggers_clarify():
    """一条消息出现两个不同业务关键词时返回 system.clarify。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#报修 主题：门 位置：大厅 问题描述：坏了 时效：3天 #完毕"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.intent == "system.clarify"


# ───────────────────────── 单行格式 ─────────────────────────


def test_single_line_keyword():
    """关键词和字段在同一行也正确解析。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    msg = "#报修 主题：门 位置：大厅 问题描述：坏了 时效：3天"
    result = match_keyword(msg, protocol)
    assert result is not None
    assert result.intent == "ticket.create"


# ───────────────────────── 源标识 ─────────────────────────


def test_matcher_source_is_keyword():
    """匹配器产出的 source 始终为 'keyword'。"""
    from semantics.keyword_matcher import match_keyword

    protocol = _load_test_protocol()
    result = match_keyword("#报修 主题：门 位置：大厅 问题描述：坏了 时效：3天", protocol)
    assert result is not None
    assert result.source == "keyword"
    assert result.intent_confidence == 1.0
