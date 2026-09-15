"""确定性关键词快路径匹配器（计划书 Task 2）。

match_keyword() 输入用户消息文本和已加载的 TicketProtocol，
输出 SemanticDecision（命中）或 None（无匹配）。
不依赖模型调用，不访问数据库，完全确定。
"""

from __future__ import annotations

import re
from typing import Any

from semantics.protocol_loader import TicketProtocol
from semantics.types import SemanticDecision, TicketScore

# 字段分隔符（全角/半角冒号归一化）
_COLON_PATTERN = re.compile(r"[：:]")
# 字段名：中文别名 → 英文键
_FIELD_ALIAS_MAP: dict[str, str] = {
    "工单编号": "ticket_no",
    "主题": "subject",
    "位置": "location",
    "问题描述": "problem_description",
    "时效": "sla",
    "故障判断": "diagnosis_items",
    "维修方式": "repair_method",
    "订单号": "order_no",
    "未完成原因": "timeout_reason",
    "取消原因": "cancel_reason",
    "重开原因": "reopen_reason",
    "停修原因": "stop_reason",
    "停止维修原因": "stop_reason",
    "原因": "reason",  # 通用 "原因"，需按 intent 再分发
    "内容": "content",
    "完成说明": "completion_note",
    "特殊情况": "special_case_reason",
    "特殊情况原因": "special_case_reason",
    "预计恢复": "expected_resume_at",
    "预计恢复时间": "expected_resume_at",
}


def _normalize_field_key(key: str) -> str:
    """中文字段名 → 英文键；未识别则原样返回。

    兼容用户手写前缀：`1主题`/`2.位置`/`一、主题` 等编号/符号前缀自动剥离。
    """
    cleaned = key.strip()
    # 剥离开头的数字/中文数字/序号符号（1/1./1、/一、/（1） 等）
    cleaned = re.sub(r'^[0-9０-９一二三四五六七八九十\.、，,;；\s\-#\(\)（）]+', '', cleaned)
    # 去掉末尾的序号残留
    cleaned = cleaned.strip()
    return _FIELD_ALIAS_MAP.get(cleaned, cleaned)


def _parse_fields(text: str) -> dict[str, Any]:
    """从消息正文解析键值对字段。

    支持：键：值（全角冒号）、键:值（半角冒号）。
    单行消息按 '键[：:]值' 模式逐段提取；多行按行处理。
    重复字段值不一致时标记 _duplicate_conflict。

    兼容性（2026-08-20）：
    - 用户常写 `1主题:xxx、2位置:xxx`、分号/顿号混用、`时效(7天)`/`可选:时效(7天)`；
      此处做归一化：顿号/逗号/分号→空格，括号时效→冒号，前缀数字剥离，并截掉关键词后到首个已知字段前的赘语（如“建立工单(店长)”）。
    """
    # ── 括号写法归一：时效(7天)/时效（7天）/可选:时效(7天) → 时效:7天 ──
    text = re.sub(r'可选\s*[:：]\s*', ' ', text)
    text = re.sub(r'时效\s*[\(（]\s*(1天|3天|7天|待商榷)\s*[\)）]', r'时效:\1', text)
    text = re.sub(r'\b可选\b', ' ', text)
    # 截掉首个已知字段前的赘语（如“建立工单(店长)”），避免污染第一个键
    first_field = re.search(r'(主题|位置|问题描述|时效|故障判断|维修方式|订单号|工单编号|未完成原因|取消原因|重开原因|停修原因|停止维修原因|原因|完成说明|内容|特殊情况|特殊情况原因|预计恢复|预计恢复时间)', text)
    if first_field:
        text = text[first_field.start():]

    fields: dict[str, Any] = {}

    if "\n" in text:
        lines = text.split("\n")
    else:
        lines = [text]

    for line in lines:
        line = line.strip()
        if not line:
            continue
        _parse_line_fields(line, fields)

    return fields


def _parse_line_fields(line: str, fields: dict[str, Any]) -> None:
    """从一行文本中提取所有 '键：值' 对（可能含多个）。"""
    pos = 0
    while pos < len(line):
        m = _COLON_PATTERN.search(line, pos)
        if m is None:
            break
        # 向前找键：从 pos 到冒号前
        before = line[pos:m.start()]
        # 向后找值：到下一个键的开始（或行尾）
        # 策略：从冒号后到下一个 " 键：" 模式前
        after_start = m.end()
        # 找到下一个 "任意文本：" 
        next_m = _COLON_PATTERN.search(line, after_start)
        if next_m:
            # 往前回溯到分隔符（空格/顿号/逗号/分号等），找到下一键名起始
            val_end = next_m.start()
            while val_end > after_start and line[val_end - 1] not in (' ', '\t', '、', '，', ',', '；', ';', '。'):
                val_end -= 1
            # 若回溯到紧邻冒号（说明无空格分隔，如 “勇者斯巴达、2位置:”），
            # 则需进一步剥离末尾的序号/分隔残留（如“、2”）
            value_raw = line[after_start:val_end].strip()
            # 去掉末尾残留的分隔符（顿号/逗号等）— 由下一键的编号导致
            # 注意：不剥离数字，避免把 W001/二楼302/纯数字订单号 等合法末尾数字误删
            value_raw = re.sub(r'[、，,；;\s]+$', '', value_raw).strip()
            pos = val_end
        else:
            value_raw = line[after_start:].strip()
            pos = len(line)

        key_raw = before.strip()
        if not key_raw or not value_raw:
            continue

        key = _normalize_field_key(key_raw)
        if key == "diagnosis_items":
            if "diagnosis_items" not in fields:
                fields["diagnosis_items"] = []
            fields["diagnosis_items"].append(value_raw)
        elif key in fields:
            if fields[key] != value_raw:
                fields.setdefault("_duplicate_conflict", True)
        else:
            fields[key] = value_raw


def _split_after_keyword(content: str, keyword: str) -> str:
    """提取关键词之后的字段文本。"""
    return content[len(keyword):].strip()


# 位置式工单编号：短 ASCII 编号（T001/W001）或 中文编号（店名-主题-时效-003，以 -数字 结尾）
_POSITIONAL_TICKET_RE = re.compile(
    r"^(?:[A-Za-z0-9_-]+|[一-龥A-Za-z0-9_-]+-\d{2,})$"
)


def _extract_positional_ticket_no(text: str) -> tuple[str | None, str]:
    """提取关键词后位于首个 token 的工单编号，并返回剩余字段正文。"""
    parts = text.split(maxsplit=1)
    first_token = parts[0] if parts else ""
    if not _POSITIONAL_TICKET_RE.fullmatch(first_token):
        return None, text
    remainder = parts[1] if len(parts) == 2 else ""
    return first_token, remainder


def match_keyword(content: str, protocol: TicketProtocol) -> SemanticDecision | None:
    """在消息文本中匹配显式关键词，返回 SemanticDecision 或 None。

    规则（计划书 §4.6）：
    - 去除首尾空白后关键词必须位于消息开头
    - 关键词后接空格/制表符/换行或消息结束（完整词边界）
    - 全角/半角冒号统一归一化
    - 同一必填字段重复出现且值不一致 → system.clarify
    - 同一消息出现两个不同业务关键词 → system.clarify
    """
    stripped = content.strip()
    if not stripped:
        return None

    # 查找所有命中的关键词（仅匹配开头，需词边界）
    hits: list[tuple[str, Any]] = []  # (keyword_text, action)
    for action in protocol.actions:
        for kw in action.explicit_keywords:
            if stripped.startswith(kw):
                after = stripped[len(kw):]
                # 词边界：关键词后需为空白/换行/冒号或结束，避免 "#完毕了吗" 误命中 "#完毕"
                if not after or after[0] in (' ', '\t', '\n', '\r', ':', '：'):
                    hits.append((kw, action))
                    break  # 每个 action 只记一次（第一个匹配的关键词）

    # 同时检测消息体中是否含有其他关键词（不在开头的也需检测双关键词）
    if hits:
        first_kw, _ = hits[0]
        all_keywords: set[str] = {kw for kw, _ in hits}
        for action in protocol.actions:
            for kw in action.explicit_keywords:
                if kw not in all_keywords and kw in stripped:
                    idx = stripped.index(kw)
                    # 确保不是开头关键词的一部分（排除前面已有更长的关键词匹配）
                    if idx > 0 and idx >= len(first_kw):
                        all_keywords.add(kw)
                        hits.append((kw, action))

    if not hits:
        return None

    # 两个不同的业务关键词 → clarify
    if len(hits) >= 2:
        intents = {a.intent_id for _, a in hits}
        if len(intents) >= 2:
            return SemanticDecision(
                protocol_version=protocol.protocol_version,
                source="keyword",
                intent="system.clarify",
                target_ticket_no=None,
                intent_confidence=1.0,
                evidence=tuple(kw for kw, _ in hits),
            )

    keyword_text, action = hits[0]
    after = _split_after_keyword(stripped, keyword_text)

    # 先提取位置式工单编号，避免其污染紧随其后的字段名。
    ticket_no, field_text = _extract_positional_ticket_no(after)

    # 解析字段
    fields = _parse_fields(field_text)

    # 提取字段式工单编号（如果有）
    if "ticket_no" in fields:
        ticket_no = fields.pop("ticket_no")

    # 重复字段冲突
    if fields.pop("_duplicate_conflict", False):
        return SemanticDecision(
            protocol_version=protocol.protocol_version,
            source="keyword",
            intent="system.clarify",
            target_ticket_no=None,
            intent_confidence=1.0,
            evidence=(keyword_text,),
        )

    # 处理通用 "原因" 字段：按意图分发
    if "reason" in fields:
        reason = fields.pop("reason")
        if action.intent_id == "ticket.cancel":
            fields["cancel_reason"] = reason
        elif action.intent_id == "ticket.reopen":
            fields["reopen_reason"] = reason
        elif action.intent_id == "ticket.stop":
            fields["stop_reason"] = reason

    # 计算 missing_fields
    missing = tuple(
        field_name
        for field_name in action.required_fields
        if field_name != "ticket_no" and field_name not in fields
        or field_name == "ticket_no" and not ticket_no
    )

    return SemanticDecision(
        protocol_version=protocol.protocol_version,
        source="keyword",
        intent=action.intent_id,
        target_ticket_no=ticket_no,
        intent_confidence=1.0,
        fields=fields,
        missing_fields=missing,
        evidence=(keyword_text,),
    )
