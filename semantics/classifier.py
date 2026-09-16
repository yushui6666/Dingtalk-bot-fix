"""语义分类器（计划书 Task 4 §10）。

SemanticClassifier 负责：
1. 跳过已被关键词匹配器命中的消息（不浪费 API 调用）；
2. 组装协议驱动的提示词（动作子集 + 候选工单摘要 + 固定 Schema）；
3. 调用 ModelClient.complete_json（单次 HTTP）；
4. 过滤模型幻觉字段 + 校验枚举值；
5. 生成 SemanticDecision（失败时降级为 chat.ignore）。

安全要求（§10.4 提示注入防护）：
- 用户消息作为不可信数据字段传入，不做指令拼接；
- 模型输出只接受协议内 intent 和白名单字段；
- 本地重新校验，模型不能创造新动作/字段/状态。
"""

from __future__ import annotations

import re
from typing import Any

from semantics.protocol_loader import TicketProtocol
from semantics.types import SemanticDecision, TicketCandidate, TicketScore, PendingAction
from semantics.model_client import ModelTimeoutError, ModelResponseError

from models import ROLE_ENGINEER, ROLE_LEADER, ROLE_MANAGER, NormalizedMessage

from logger import get_logger

logger = get_logger(__name__)

# ───────────────────────── 字段白名单 ─────────────────────────
# 计划书 §4.5 字段词典：模型只能抽取这些键，其余一律视为幻觉丢弃
_KNOWN_FIELDS: frozenset[str] = frozenset({
    "subject", "location", "problem_description", "device", "urgency",
    "sla", "diagnosis_items", "repair_method", "order_no",
    "timeout_reason", "completion_note", "cancel_reason", "reopen_reason",
    "clarification_reason", "ticket_no", "content", "attachments",
    "special_case_reason", "expected_resume_at",
    "parts",
})

_INTENT_FIELD_OVERRIDES: dict[str, frozenset[str]] = {
    # sla 已改为可选（默认 1 天），但模型仍可抽取，不能当幻觉过滤
    "ticket.create": frozenset({"device", "urgency", "attachments", "sla"}),
    "ticket.add_detail": frozenset({
        "subject", "location", "problem_description", "device", "urgency",
        "attachments", "content",
    }),
    "ticket.diagnosis.submit": frozenset({"diagnosis_items"}),
    "ticket.repair_plan.submit": frozenset({"repair_method", "order_no", "parts"}),
    "ticket.timeout_reason.submit": frozenset({"timeout_reason"}),
    "ticket.special_case.submit": frozenset({"special_case_reason", "expected_resume_at"}),
    "ticket.complete": frozenset({"completion_note"}),
    "system.correct_pending_action": _KNOWN_FIELDS - {"ticket_no"},
}

# 允许的 SLA 枚举值（§4.5；「待商榷」= 不设时效、仅记录）
_ALLOWED_SLA = frozenset({"1天", "3天", "7天", "待商榷"})

# 允许的紧急度枚举值（§4.5）
_ALLOWED_URGENCY = frozenset({"低", "中", "高"})

# 维修方式已改为自由文本（2026-08-26），保留集合仅用于文档，不再强校验
_ALLOWED_REPAIR_METHODS = frozenset({
    "淘宝采购后自行维修",
    "需要供应商维修",
    "需要木工维修",
    "需要工程师上门",
    "远程视频维修",
})

# ───────────────────────── 本地归一化/兜底（2026-09-03）────────────────────────
# 生产实证（9 月核查）：模型对同构表达输出不稳定——
# 「004确认修好」→ ticket.confirm_complete（对）、「003确认修好」→ ticket.complete（错）；
# 「004更换吸铁石」→ repair_plan 但 fields={}（维修方式丢失）；
# 「011电磁阀坏了 淘宝重新采购」→ order_no="011"（短编号污染订单监控）。
# 以下归一化在模型输出后、校验前做确定性修正，不依赖再次调用模型。
_MANAGER_CONFIRM_RE = re.compile(r"确认\s*(修好|完成|完毕)")
_ORDER_NO_FORMAT_RE = re.compile(r"^[A-Za-z0-9-]{6,64}$")
# 消息开头工单短编号前缀（如「004更换吸铁石」「007：…」「#004 …」），剥离后取动作原文
_LEADING_TICKET_NO_PREFIX_RE = re.compile(r"^\s*(?:工单|#|第)?\s*\d{1,4}(?![\dA-Za-z-])\s*[:：，,、\s]*")


def _is_plausible_order_no(value: Any) -> bool:
    """订单号形态校验：6-64 位字母数字连字符，且满足任一条件：

    - 至少含 6 个数字（纯数字长单号，如 9990000000000000001）；
    - 含字母且长度≥6、至少含 3 个数字（字母单号，如 TB-2024-0001）。

    短编号（「011」）、中文短语（「淘宝采购吸铁石」）一律不算订单号，
    避免污染 order_monitor 与共享表（2026-09-01 两条脏行实证）。
    """
    if value is None:
        return False
    text = str(value).strip()
    if not _ORDER_NO_FORMAT_RE.match(text):
        return False
    digits = sum(ch.isdigit() for ch in text)
    if digits >= 6:
        return True
    return any(ch.isalpha() for ch in text) and len(text) >= 6 and digits >= 3


def normalize_semantic_decision(
    decision: SemanticDecision, message: NormalizedMessage
) -> SemanticDecision:
    """模型输出的确定性后修正（幂等，可重入）。

    1. 店长说「确认修好/确认完成/确认完毕」→ ticket.confirm_complete：
       ticket.complete 只允许 ACTIVE 系状态，待确认工单会被判「无活动工单」。
    2. order_no 形态守卫：非法值直接剔除（不进订单监控）。
    3. repair_plan 无维修方式且无有效订单号 → 用消息原文（剥离开头编号）
       回填 repair_method，口语化维修动作不再被「请明确维修方式」拒绝。
    """
    from semantics.types import SemanticDecision as _Decision

    intent = decision.intent
    fields = dict(decision.fields or {})
    missing = list(decision.missing_fields or [])
    evidence = list(decision.evidence or [])
    changed = False

    # 1. 店长确认归一化（仅显式「确认」措辞，避免误伤「008修好了」类直接完工）
    if (
        intent == "ticket.complete"
        and message.sender_role == ROLE_MANAGER
        and _MANAGER_CONFIRM_RE.search(message.content or "")
    ):
        intent = "ticket.confirm_complete"
        evidence.append("manager_confirm_normalized")
        changed = True
        logger.info(
            "店长确认归一化 message_id=%s complete→confirm_complete",
            getattr(message, "message_id", "?"),
        )

    # 2. 订单号形态守卫
    if "order_no" in fields and not _is_plausible_order_no(fields.get("order_no")):
        logger.info(
            "剔除非法订单号 order_no=%s message_id=%s",
            fields.get("order_no"), getattr(message, "message_id", "?"),
        )
        del fields["order_no"]
        changed = True

    # 3. 维修方式兜底回填
    if intent == "ticket.repair_plan.submit" and not str(fields.get("repair_method") or "").strip():
        raw_order = fields.get("order_no")
        if not _is_plausible_order_no(raw_order):
            text = _LEADING_TICKET_NO_PREFIX_RE.sub("", message.content or "").strip()
            if _is_plausible_order_no(text) and len(text) <= 64:
                # 裸订单号被模型漏抽 → 补回 order_no（如「订单号：5127…」判对但 fields 丢失时）
                fields["order_no"] = text
                changed = True
            elif len(text) >= 2:
                # 口语化维修动作原文即维修方式（「004更换吸铁石」→「更换吸铁石」）
                fields["repair_method"] = text[:500]
                if "repair_method" in missing:
                    missing.remove("repair_method")
                evidence.append("repair_method_fallback")
                changed = True
                logger.info(
                    "维修方式兜底回填 message_id=%s method=%s",
                    getattr(message, "message_id", "?"), fields["repair_method"][:40],
                )

    if not changed:
        return decision
    return _Decision(
        protocol_version=decision.protocol_version,
        source=decision.source,
        intent=intent,
        target_ticket_no=decision.target_ticket_no,
        intent_confidence=decision.intent_confidence,
        fields=fields,
        missing_fields=tuple(missing),
        candidate_scores=decision.candidate_scores,
        evidence=tuple(evidence),
        requires_confirmation=decision.requires_confirmation,
    )

_BUSINESS_ACTION_CUES: dict[str, tuple[str, ...]] = {
    "ticket.create": ("报修",),
    "ticket.diagnosis.submit": ("故障判断", "判断是", "应该是"),
    "ticket.repair_plan.submit": ("维修方式", "维修方案", "维修", "采购", "更换"),
    "ticket.timeout_reason.submit": ("超时原因", "没按时完成"),
    "ticket.special_case.submit": ("特殊情况",),
    "ticket.complete": ("完成工单", "直接完毕", "确认完毕"),
    "ticket.cancel": ("取消工单",),
    "ticket.stop": ("停止维修", "停修", "不再维修"),
    "ticket.reopen": ("重开工单",),
    "ticket.negotiate.submit": ("待商榷", "改待商榷", "时效待定"),
}


def _protected_identifier_guard(content: str, order_no: Any) -> str | None:
    """当模型抽取的订单号实际是消息中的手机号/资产号时返回防护原因。"""
    text = content or ""
    extracted = str(order_no or "").strip()
    if not extracted:
        return None

    normalized_mobile_order = re.sub(r"[\s-]", "", extracted)
    for match in re.finditer(r"(?<!\d)1[3-9]\d(?:[\s-]?\d){8}(?!\d)", text):
        normalized_mobile = re.sub(r"[\s-]", "", match.group(0))
        if normalized_mobile_order == normalized_mobile:
            return "mobile_number_guard"

    asset_pattern = re.compile(
        r"(?:资产(?:号|编号)|设备(?:号|编号))\s*[:：为是#]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9-]{5,63})",
        re.IGNORECASE,
    )
    if any(extracted.casefold() == match.group(1).casefold() for match in asset_pattern.finditer(text)):
        return "asset_number_guard"
    return None


class SemanticClassifier:
    """云端模型语义分类器。

    协议驱动的提示词组装 + 模型输出后处理（字段裁剪、枚举校验）。

    设计原则（§2.2）：
    - 模型只做理解和抽取，不做执行；
    - 本地规则引擎始终重新校验。
    """

    def __init__(
        self,
        *,
        client: Any,  # ModelClient Protocol（types.py）
        protocol: TicketProtocol,
    ) -> None:
        self._client = client
        self._protocol = protocol

    @property
    def model_client(self) -> Any:
        """复用同一模型客户端给 LangGraph 的反馈分类与排障建议节点。"""
        return self._client

    async def classify(
        self,
        message: NormalizedMessage,
        candidates: list[TicketCandidate] | None = None,
        pending_action: PendingAction | None = None,
        history: list[dict[str, Any]] | None = None,
        parts_vocab: dict[str, dict[str, set[str]]] | None = None,
    ) -> SemanticDecision:
        """对自然语言消息做语义分类。

        流程（§7 消息处理流程）：
        1. 关键词已命中 → 跳过模型（source 标记 keyword_already_matched）；
        2. 组装 prompt + schema（含最近群消息上下文、候选备件词表）；
        3. 单次 HTTP 调用模型；
        4. 校验 intent 在协议内；
        5. 过滤幻觉字段 + 枚举值校验；
        6. 返回 SemanticDecision。

        Args:
            message: 标准化群消息。
            candidates: 当前群的活动工单候选列表（多工单路由）。
            pending_action: 当前待确认动作（用于上下文）。
            history: 该群最近消息（时间正序），供模型理解「2」「005完成了」等
                依赖上文的表达。
            parts_vocab: 候选备件词表 {主题原文: {规范名: {别名集合}}}，辅助模型
                产出贴合该店该主题的备件词（辅助，权威过滤在执行期）。
        """
        candidates = candidates or []

        # 1. 关键词快路径已**永久停用**（2026-08-20 决策，2026-09-16 明确为永久）：
        # 任何消息都不再短路到本地 match_keyword，必须经过模型。若日后确实需要
        # 本地兜底，应作为显式配置项重新设计，而不是恢复此处的注释代码。

        action_cues = _find_business_action_cues(message.content)
        if len(action_cues) > 1:
            evidence = tuple(
                cue
                for _, cues in action_cues
                for cue in cues
            )
            return SemanticDecision(
                protocol_version=self._protocol.protocol_version,
                source="SEMANTIC_MODEL",
                intent="system.clarify",
                target_ticket_no=None,
                intent_confidence=1.0,
                fields={
                    "clarification_reason": "同一消息包含多个业务动作，请拆分后重发",
                },
                evidence=evidence,
            )

        # 2. 组装 prompt 和 schema
        payload = _build_payload(
            message, candidates, pending_action, self._protocol,
            history=history, parts_vocab=parts_vocab,
        )
        schema = _build_output_schema(self._protocol)

        # 3. 调用模型（单次，不内部重试）
        try:
            raw = await self._client.complete_json(
                payload=payload,
                schema=schema,
                idempotency_key=f"classify:{message.message_id}",
            )
        except ModelTimeoutError as exc:
            logger.warning("模型超时 message_id=%s err=%s", message.message_id, exc)
            return _fallback_decision(self._protocol, message.message_id, "timeout")
        except ModelResponseError as exc:
            logger.warning("模型响应异常 message_id=%s err=%s", message.message_id, exc)
            return _fallback_decision(self._protocol, message.message_id, "response_error")
        except Exception as exc:
            # 网络错误等未知异常
            logger.warning("模型调用失败 message_id=%s err=%s", message.message_id, exc)
            return _fallback_decision(self._protocol, message.message_id, "network_error")

        if not isinstance(raw, dict):
            logger.warning(
                "模型响应不是 JSON 对象 message_id=%s type=%s",
                message.message_id,
                type(raw).__name__,
            )
            return _fallback_decision(self._protocol, message.message_id, "response_error")

        # 4. 校验 intent 在协议白名单内（§10.4 提示注入防护）
        intent = raw.get("intent", "")
        known_intents = {a.intent_id for a in self._protocol.actions}
        if intent not in known_intents:
            logger.warning(
                "模型返回协议外 intent=%s message_id=%s（提示注入？）",
                intent,
                message.message_id,
            )
            return _fallback_decision(self._protocol, message.message_id, "unknown_intent")

        raw_fields_for_guard = raw.get("fields")
        order_no_for_guard = (
            raw_fields_for_guard.get("order_no")
            if isinstance(raw_fields_for_guard, dict)
            else None
        )
        protected_guard = (
            _protected_identifier_guard(message.content, order_no_for_guard)
            if intent == "ticket.repair_plan.submit"
            else None
        )
        if protected_guard:
            logger.info("过滤敏感编号订单误判 message_id=%s guard=%s", message.message_id, protected_guard)
            return SemanticDecision(
                protocol_version=self._protocol.protocol_version,
                source="SEMANTIC_MODEL",
                intent="chat.ignore",
                target_ticket_no=None,
                intent_confidence=1.0,
                fields={},
                evidence=(protected_guard,),
            )

        # 4.5 业务动作词兜底：消息明确是单个业务动作（如「报修」「报修一下」），
        #     但模型误判为 chat.ignore → 返回该业务动作，缺失字段交给 validator 引导补全。
        if intent == "chat.ignore" and len(action_cues) == 1:
            cue_intent, _cues = action_cues[0]
            if cue_intent in {a.intent_id for a in self._protocol.actions}:
                logger.info(
                    "业务动作词兜底 message_id=%s cue_intent=%s（模型判 ignore）",
                    message.message_id, cue_intent,
                )
                return normalize_semantic_decision(
                    SemanticDecision(
                        protocol_version=self._protocol.protocol_version,
                        source="SEMANTIC_MODEL",
                        intent=cue_intent,
                        target_ticket_no=None,
                        intent_confidence=0.6,
                        fields={},
                        missing_fields=tuple(
                            f for f in self._protocol.get_action(cue_intent).required_fields
                            if f != "ticket_no"
                        ),
                        evidence=tuple(_cues),
                    ),
                    message,
                )

        action = self._protocol.get_action(intent)
        if action is None:
            return _fallback_decision(self._protocol, message.message_id, "unknown_intent")

        # 5. 过滤幻觉字段（§10.4：模型不能创造新字段）
        raw_fields = raw.get("fields", {}) or {}
        if not isinstance(raw_fields, dict):
            return _fallback_decision(self._protocol, message.message_id, "response_error")
        allowed_fields = (
            set(action.required_fields)
            | set(action.optional_fields)
            | set(_INTENT_FIELD_OVERRIDES.get(intent, ()))
        )
        safe_fields: dict[str, Any] = {}
        for key, value in raw_fields.items():
            if key == "ticket_no" or key in allowed_fields:
                # 兼容模型把数组字段误返回为字符串而非数组：归一为 [string]
                if key == "diagnosis_items" and isinstance(value, str):
                    value = [value] if value.strip() else []
                if key == "parts" and isinstance(value, str):
                    # 备件名串（顿号/逗号分隔）→ 拆成数组
                    value = [v for v in re.split(r"[、，,；;]", value) if v.strip()]
                safe_fields[key] = value
            else:
                logger.debug(
                    "过滤幻觉字段 key=%s message_id=%s（提示注入？）",
                    key,
                    message.message_id,
                )

        # 5.1 清洗 order_no 的空/None 占位（模型可能返回 null → Python None → 误触发“None”订单）
        if "order_no" in safe_fields:
            _raw_on = safe_fields["order_no"]
            if _raw_on is None or not str(_raw_on).strip() or str(_raw_on).strip().lower() in ("none", "null", "nil"):
                logger.debug("order_no 空/占位值=%s，已剔除 message_id=%s", _raw_on, message.message_id)
                del safe_fields["order_no"]

        # 6. 枚举值校验（§4.5, §4.7）
        missing: list[str] = []

        if "sla" in safe_fields and safe_fields["sla"] not in _ALLOWED_SLA:
            logger.debug("sla 非法值=%s，移入 missing", safe_fields["sla"])
            missing.append("sla")
            del safe_fields["sla"]

        if "urgency" in safe_fields and safe_fields["urgency"] not in _ALLOWED_URGENCY:
            logger.debug("urgency 非法值=%s，移入 missing", safe_fields["urgency"])
            missing.append("urgency")
            del safe_fields["urgency"]

        # 维修方式自由文本：仅清洗空值/过长，不做枚举校验（2026-08-26）
        if "repair_method" in safe_fields:
            _rm_val = safe_fields["repair_method"]
            if _rm_val is None or not str(_rm_val).strip():
                logger.debug("repair_method 空值，已剔除 message_id=%s", message.message_id)
                del safe_fields["repair_method"]
                missing.append("repair_method")
            elif len(str(_rm_val).strip()) > 500:
                logger.debug("repair_method 过长，已截断 message_id=%s", message.message_id)
                safe_fields["repair_method"] = str(_rm_val).strip()[:500]

        # 置信度
        confidence = 0.0
        try:
            confidence = float(raw.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            logger.debug("confidence 非法=%s，设为 0", raw.get("confidence"))

        # 提取工单编号
        ticket_no = safe_fields.pop("ticket_no", None) or raw.get("ticket_no")
        if ticket_no is not None:
            ticket_no = str(ticket_no).strip() or None
        candidate_nos = {candidate.ticket_no for candidate in candidates}
        if ticket_no and ticket_no not in candidate_nos:
            # 重开/查询可指向已完结工单（不在活动候选内），保留编号交由校验层用全量候选判定；
            # 选单（2026-08-28）保留用户所写短编号原样（如「007」），由 pipeline
            # _handle_select 做尾缀解析或明确拒绝——此前被过滤为 None 后会静默
            # 落入单候选兜底，导致「007号单」被切到无关工单还回「✅ 已切换」。
            if intent in ("ticket.reopen", "ticket.query", "ticket.select"):
                logger.debug(
                    "保留候选集合外目标 ticket_no=%s intent=%s message_id=%s（交由校验层/选单解析判定）",
                    ticket_no,
                    intent,
                    message.message_id,
                )
            else:
                logger.debug(
                    "过滤候选集合外目标 ticket_no=%s message_id=%s",
                    ticket_no,
                    message.message_id,
                )
                ticket_no = None
                missing.append("ticket_no")

        # 候选评分（模型可选返回）
        candidate_scores: tuple[TicketScore, ...] = ()
        raw_scores = raw.get("candidate_scores")
        if isinstance(raw_scores, list):
            valid_scores: list[TicketScore] = []
            for item in raw_scores:
                if isinstance(item, dict):
                    tn = item.get("ticket_no", "")
                    sc = item.get("score", 0.0)
                    # 只保留当前群候选集合内的工单（§6.2：模型不能选群外工单）
                    if tn in candidate_nos:
                        try:
                            valid_scores.append(TicketScore(ticket_no=tn, score=float(sc)))
                        except (TypeError, ValueError):
                            pass
            if valid_scores:
                candidate_scores = tuple(valid_scores)

        evidence_raw = raw.get("evidence", [])
        evidence: tuple[str, ...] = ()
        if isinstance(evidence_raw, list):
            evidence = tuple(str(e) for e in evidence_raw if e)

        decision = SemanticDecision(
            protocol_version=self._protocol.protocol_version,
            source="SEMANTIC_MODEL",
            intent=intent,
            target_ticket_no=ticket_no,
            intent_confidence=confidence,
            fields=safe_fields,
            missing_fields=tuple(missing),
            candidate_scores=candidate_scores,
            evidence=evidence,
        )
        # 本地确定性后修正（店长确认归一化/订单号守卫/维修方式兜底，2026-09-03）
        return normalize_semantic_decision(decision, message)


def _find_business_action_cues(
    content: str,
) -> list[tuple[str, tuple[str, ...]]]:
    """识别同一消息中明确出现的多个业务动作词。"""
    matches: list[tuple[str, tuple[str, ...]]] = []
    for intent, cues in _BUSINESS_ACTION_CUES.items():
        matched = tuple(cue for cue in cues if cue in content)
        if matched:
            matches.append((intent, matched))
    return matches


# ───────────────────────── 降级决策 ─────────────────────────


def _fallback_decision(
    protocol: TicketProtocol,
    message_id: str,
    reason: str,
) -> SemanticDecision:
    """模型失败时的降级决策。

    §10.3：模型不可用时消息进入重试或死信。
    关键词快路径已永久停用（2026-09-16），因此**没有任何本地兜底**：
    模型不可用时包括 #报修 在内的全部消息都无法完成业务动作。
    """
    return SemanticDecision(
        protocol_version=protocol.protocol_version,
        source="SEMANTIC_MODEL",
        intent="chat.ignore",
        target_ticket_no=None,
        intent_confidence=0.0,
        evidence=(f"model_fallback:{reason}:{message_id}",),
    )


# ───────────────────────── prompt 组装 ─────────────────────────


def _build_payload(
    message: NormalizedMessage,
    candidates: list[TicketCandidate],
    pending_action: PendingAction | None,
    protocol: TicketProtocol,
    history: list[dict[str, Any]] | None = None,
    parts_vocab: dict[str, dict[str, set[str]]] | None = None,
) -> dict[str, Any]:
    """组装模型请求 payload（§10.2 模型输入）。

    精简策略（2026-08-12 优化，降低模型延迟）：
    - 动作摘要去掉 display_name / 可选字段 / target_policy / confirmation_policy
      （分类不需要，本地校验兜底）；
    - 按发送人角色裁剪动作子集（system.* 无角色限制始终保留）；
    - 每个动作只保留必填字段 + 一条截断正例。

    上下文（2026-08-13）：附带最近群消息，帮助模型理解「2」「005完成了」等
    依赖上文的表达。
    """
    sender_role = message.sender_role
    action_summaries: list[str] = []
    for a in protocol.actions:
        if not a.semantic_enabled:
            continue
        # 角色裁剪：无角色限制（system.*）保留；否则只保留该角色可触发的动作。
        # LEADER（工程负责人）为超集角色，额外兼容店长/工程师允许的动作，避免
        # 兼任工程负责人的工程师在自然语言路径下丢失既有语义能力。
        if a.allowed_roles and sender_role not in a.allowed_roles:
            if not (
                sender_role == ROLE_LEADER
                and any(r in a.allowed_roles for r in (ROLE_MANAGER, ROLE_ENGINEER))
            ):
                continue
        parts = [f"- {a.intent_id}"]
        if a.required_fields:
            parts.append("必填:" + ",".join(a.required_fields))
        if a.optional_fields:
            parts.append("可选:" + ",".join(a.optional_fields))
        # 字段提示（2026-09-03）：required/optional 为空的动作（如 repair_plan）
        # 模型不知道要抽什么字段，按 overrides 补全，避免维修方式系统性漏抽
        extra_hints = [
            f for f in _INTENT_FIELD_OVERRIDES.get(a.intent_id, ())
            if f not in (a.required_fields or []) and f not in (a.optional_fields or [])
            and f != "ticket_no"
        ]
        if extra_hints:
            parts.append("字段:" + ",".join(extra_hints))
        example = next((e for e in a.positive_examples if e), "")
        if example:
            parts.append("例:" + example.replace("\n", " ")[:48])
        action_summaries.append(" ".join(parts))

    # 候选工单摘要（§6.2：只发编号/主题/位置/状态/进展，带序号供「第N个」引用）
    candidate_lines = ""
    if candidates:
        candidate_lines = "\n当前活动工单（用户说「第N个」即指下面的第 N 项）：\n"
        for i, c in enumerate(candidates, 1):
            candidate_lines += (
                f"  {i}. {c.ticket_no}: {c.subject} @ {c.location} "
                f"summary={c.problem_summary} status={c.status} version={c.version}\n"
            )

    # 待确认动作上下文
    pending_line = ""
    if pending_action is not None:
        pending_line = (
            f"\n当前待确认动作: pending_id={pending_action.id} "
            f"intent={pending_action.intent} "
            f"candidates={pending_action.candidate_ticket_ids} "
            f"fields={pending_action.fields} version={pending_action.version}\n"
        )

    system_prompt = (
        "你是钉钉群报修工单的语义分析助手。"
        "根据用户消息判断意图(intent)和抽取字段(fields)。\n\n"
        f"发送人角色={sender_role}。协议版本={protocol.protocol_version}。\n"
        "可用的意图(id)：\n"
        + "\n".join(action_summaries)
        + "\n\n规则：\n"
        "- 只抽取用户明确表达的信息\n"
        "- 不要猜测位置、设备名称、时效等；时效(sla)只有用户明确说 1天/3天/7天/待商榷 时才填，否则留空让校验层提示补充\n"
        "- 用户说“改成待商榷/暂时不定/先待商榷吧/时效待定/改待商榷” → ticket.negotiate.submit，negotiate_reason 为原因原文（至少1字）\n"
        "- 字段键名使用英文字段名；用户可能写“1主题:xxx、2位置:xxx、3问题描述:xxx;可选:时效(7天)”或“#报修”前缀，编号/顿号/括号均为分隔装饰，请自动剥离后识别主题/位置/问题描述/时效，“#”为可选前缀，不影响意图\n"
        "- 报修时字段拆分：subject=场馆/空间名（如「博物馆奇妙夜」），"
        "location=更具体的发生位置（如「里面的那间房子」「一楼大厅」），"
        "device=具体损坏的物品（如「风扇」「消防门」），problem_description=故障描述。"
        "不要把整串修饰语都塞进 location，也不要把损坏物品当 subject\n"
        "- 若消息仅含淘宝订单号（6-64位字母数字连字符且至少含6个数字，如 TB-2024-0001 或 9990000000000000003）或含订单号+故障判断（如“估计是铰链坏了，单号是...”），且群内有活动工单，则视为 ticket.repair_plan.submit，提取 order_no/order_nos 并酌情提取 diagnosis_items；纯手机号（11位且以1开头）不要误判为订单号\n"
        "- 如果消息没有明确报修/诊断/完成等意图，返回 chat.ignore\n"
        "- 同一消息包含两个或更多业务动作时，即使后一个动作是未来或条件动作，也返回 system.clarify\n"
        "- 用户从候选工单中明确选择编号或序号时，返回 ticket.select\n"
        "- 用户回复裸短编号（如「003」）或多个编号/序号（如「003 007」「1 3」）时同样返回 ticket.select：ticket_no 字段原样保留用户所写文本（含空格与位数），不要猜测展开为完整编号、也不要截取部分；候选列表中没有对应编号时同样原样返回，绝不允许改用候选中的编号顶替\n"
        "- 用户说「第N个/第一个/第二个」指候选工单列表的第 N 项：据此确定目标工单，把该项的完整工单编号填入 ticket_no 字段，并返回用户实际意图（如「第一个完成了」→ ticket.complete 且 ticket_no=第1项编号）\n"
        "- 报修时若用户是在补充上一条未完成报修的缺失字段（如上一条因缺少时效/主题等被提示补充，且历史中最近一条系统消息明确要求补充），可结合历史中最近的未完成报修内容补齐缺失字段，但不得编造历史中不存在的字段；其他情况下当前报修的主题/位置/问题描述/时效必须来自当前消息原文，缺失字段留空让校验提示补充；历史还可用于理解「第N个」「它」等指代\n"
        "- 工程师使用‘可能’‘应该是’等保留表达给出具体故障判断时，仍可返回 ticket.diagnosis.submit，但应降低置信度\n"
        "- 疑问句但明确描述故障（含设备/位置/问题）时，仍返回 ticket.create；纯笼统询问、否定句返回 chat.ignore\n"
        "- 用户表示问题已解决、恢复正常、可以使用（如「正常了」「搞定了」「弄好了」「没问题了」「修好了」）→ 返回 ticket.complete\n"
        "- 店长说「确认修好/确认完成/确认完毕」（可带短编号如「003确认修好」）→ ticket.confirm_complete；工程师说「修好了/完成了」→ ticket.complete\n"
        "- 工程师描述具体维修动作（如「更换吸铁石」「重新绑绳子」「找焊工重新焊接」，可带工单短编号前缀如「004更换吸铁石」）→ ticket.repair_plan.submit：repair_method=动作原文（剥离开头编号）；order_no 只有消息含 6 位以上字母数字连字符订单串时才填，短编号（2-4 位数字）与中文短语绝不能填入 order_no\n"
        "- 用户回复「特殊情况：原因；预计恢复：时间」（通常是对系统一小时提醒的答复，声明等待到货/等待工程师上门/等待门店或客户配合/等待第三方等外部依赖暂时无法推进）→ 返回 ticket.special_case.submit：special_case_reason=原因原文，expected_resume_at=恢复时间原文（保留「一小时内」「明天下午」等用户原话，不要换算）；消息若以短编号开头（如「007今日胶未干」），该编号可能指其他工单：把编号放入 ticket_no 字段、不要并入 special_case_reason（原因从编号之后提取）\n"
        "- 取消工单(ticket.cancel)、重开工单(ticket.reopen) 只在用户非常确定时才返回\n"
    )

    if candidate_lines:
        system_prompt += candidate_lines
    if pending_line:
        system_prompt += pending_line

    # 本店可用备件清单（辅助模型产出贴合词；权威过滤在执行期）2026-09-09
    if parts_vocab:
        vocab_lines = "\n\n本店可用备件清单（记录本次维修「parts」用，按主题列出）：\n"
        for subj in parts_vocab:
            pool = parts_vocab[subj]
            if not pool:
                continue
            entries = []
            for canon, aliases in pool.items():
                if aliases:
                    entries.append(f"{canon}/{ '/'.join(sorted(aliases)) }")
                else:
                    entries.append(canon)
            vocab_lines += f"[{subj}] " + ", ".join(sorted(entries)) + "\n"
        vocab_lines += (
            "规则：parts 只能取上面清单里的规范名（即斜杠前的标准叫法）。"
            "若用户描述更换/采购了某个备件但清单里找不到对应项，把 parts 留空（[]）；"
            "绝不编造清单外的备件名。"
        )
        system_prompt += vocab_lines

    # 最近群消息上下文（供模型理解「2」「005完成了」「第二个」等依赖上文的表达）
    if history:
        history_lines = "\n最近群消息（供理解上下文，勿直接引用，当前消息是最后一句）：\n"
        for h in history:
            sender = h.get("sender_name") or h.get("sender_role") or h.get("sender_id") or "?"
            content = str(h.get("content") or "")[:120]
            history_lines += f"[{sender}] {content}\n"
        system_prompt += history_lines

    # 用户消息作为不可信数据字段（§10.4 提示注入防护）
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message.content},
        ],
    }


# ───────────────────────── 输出 Schema ─────────────────────────


def _build_output_schema(protocol: TicketProtocol) -> dict[str, Any]:
    """生成结构化输出的 JSON Schema（§4.9 模型固定输出）。

    模型只能返回协议内 intent 和固定字段结构。
    """
    intents = [a.intent_id for a in protocol.actions if a.semantic_enabled]
    return {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": intents,
                "description": "用户消息对应的意图 ID",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "置信度 [0.0, 1.0]",
            },
            "fields": {
                "type": "object",
                "description": "抽取的字段键值对",
                "properties": {
                    "subject": {"type": "string"},
                    "location": {"type": "string"},
                    "problem_description": {"type": "string"},
                    "device": {"type": "string"},
                    "urgency": {"type": "string", "enum": ["低", "中", "高"]},
                    "sla": {"type": "string", "enum": sorted(_ALLOWED_SLA)},
                    "diagnosis_items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "repair_method": {"type": "string"},
                    "order_no": {"type": "string"},
                    "parts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "本次维修使用的备件规范名（只能从可用备件清单中选；清单内没有就返回空数组）",
                    },
                    "timeout_reason": {"type": "string"},
                    "completion_note": {"type": "string"},
                    "cancel_reason": {"type": "string"},
                    "reopen_reason": {"type": "string"},
                    "clarification_reason": {"type": "string"},
                    "ticket_no": {"type": "string"},
                    "content": {"type": "string"},
                    "special_case_reason": {"type": "string"},
                    "expected_resume_at": {"type": "string"},
                },
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "原文中支持判断的片段",
            },
            "ticket_no": {
                "type": "string",
                "description": "消息中明确提及的工单编号，无则省略",
            },
            "candidate_scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticket_no": {"type": "string"},
                        "score": {"type": "number"},
                    },
                },
                "description": "对候选工单的评分（可选）",
            },
        },
        "required": ["intent", "confidence", "fields"],
    }
