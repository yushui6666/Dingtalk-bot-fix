"""共享接口契约类型（计划书 15.1）。

⚠️ 核心规则：
- 本文件类型为后续所有任务的唯一共享契约，不得在各模块重复定义同义结构。
- 数据库状态字符串、协议 intent_id、执行器名称和以上类型的字段名必须逐项一致。
- 任何字段调整必须先修改协议 Schema 和共享类型测试，再修改消费者。
- 所有 dataclass 均为 frozen=True（不可变，线程/协程安全）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


# ───────────────────────── 决策状态枚举 ─────────────────────────


class DecisionStatus(StrEnum):
    """路由管线返回的决策状态。"""
    AUTO_EXECUTE = "AUTO_EXECUTE"                  # 直接执行，无需确认
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"  # 需用户确认或选择后才执行
    IGNORE = "IGNORE"                              # 闲聊、系统消息等不产生业务动作
    VALIDATION_REJECTED = "VALIDATION_REJECTED"    # 校验失败（权限/状态/字段），已回群告知
    RETRY_PENDING = "RETRY_PENDING"                # 模型调用失败，进入重试队列


class PendingActionStatus(StrEnum):
    """待确认动作的状态流转。"""
    WAITING = "WAITING"        # 已向用户提问，等待回应
    CONFIRMED = "CONFIRMED"    # 用户确认，已执行
    REJECTED = "REJECTED"      # 用户拒绝
    EXPIRED = "EXPIRED"        # 超时未回应
    SUPERSEDED = "SUPERSEDED"  # 被后一条待确认动作覆盖


class RouteDecision(StrEnum):
    """工单路由决策结果。"""
    ROUTED = "ROUTED"    # 已确定归属工单
    CLARIFY = "CLARIFY"  # 存在歧义，需要用户澄清
    CREATE = "CREATE"    # 判定为新建工单（当前无匹配工单或 create 动作）


# ───────────────────────── 路由与工单候选 ─────────────────────────


@dataclass(frozen=True)
class TicketCandidate:
    """多工单路由中的候选工单快照（不可变，由路由层冻结）。"""
    ticket_id: int
    ticket_no: str
    group_id: str
    subject: str
    location: str
    problem_summary: str
    status: str
    version: int             # 乐观版本号


@dataclass(frozen=True)
class TicketScore:
    """路由评分中的单工单得分。"""
    ticket_no: str
    score: float


# ───────────────────────── 语义决策 ─────────────────────────


@dataclass(frozen=True)
class SemanticDecision:
    """语义识别器（关键词或模型）产出的结构化决策。

    本类型作为 keyword_matcher / model_classifier 的统一输出，
    消费者仅读取字段，不由消费者修改。
    """
    protocol_version: str          # 运行时协议版本哈希或摘要
    source: str                    # "keyword" | "model" | "fallback"
    intent: str                    # 协议 intent_id，如 "ticket.create"
    target_ticket_no: str | None   # 消息中明确提及的工单编号，无则 None
    intent_confidence: float       # [0.0, 1.0]
    fields: dict[str, Any] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    candidate_scores: tuple[TicketScore, ...] = ()
    evidence: tuple[str, ...] = ()  # 匹配依据（关键词命中/模型引用原文片段）
    requires_confirmation: bool = False  # 模型判断需二次确认


@dataclass(frozen=True)
class ValidatedCommand:
    """经 validator 校验通过的确定性命令，可直接发送给执行器。"""
    message_id: str
    group_id: str
    actor_id: str
    actor_role: str
    intent: str
    target_ticket_id: int | None       # 解析后的目标工单 ID，None 表示新建或无目标
    expected_ticket_version: int | None
    fields: dict[str, Any]
    source: str                        # "keyword" | "model"


@dataclass(frozen=True)
class RouteResult:
    """路由器的产出：消息应归属哪张工单（或需要澄清/新建）。"""
    decision: RouteDecision
    target_ticket_id: int | None
    candidate_ticket_ids: tuple[int, ...]
    link_type: str | None              # "explicit_no" / "reply" / "user_context" / "semantic_top1" / …
    routing_score: float


# ───────────────────────── 待确认动作 ─────────────────────────


@dataclass(frozen=True)
class PendingAction:
    """已持久化的待确认动作行（从 DB 读取）。"""
    id: int
    source_message_id: str
    group_id: str
    user_id: str
    intent: str
    candidate_ticket_ids: tuple[int, ...]
    fields: dict[str, Any]
    expected_ticket_versions: dict[int, int]
    status: PendingActionStatus
    version: int
    expires_at: datetime


@dataclass(frozen=True)
class PendingActionDraft:
    """待持久化的确认动作草稿（路由层产出 → 收件箱 worker 写入）。"""
    source_message_id: str
    group_id: str
    user_id: str
    decision: SemanticDecision
    expected_ticket_versions: dict[int, int]
    expires_at: datetime


# ───────────────────────── 收件箱与执行结果 ─────────────────────────


@dataclass(frozen=True)
class InboxMessage:
    """收件箱中的消息行（NormalizedMessage + 处理元信息）。"""
    message: 'NormalizedMessage'  # noqa: F821 — 避免循环导入，运行时由 models 提供
    status: str                   # "PENDING" / "PROCESSING" / "DONE" / "FAILED"
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class CommandResult:
    """执行器返回的结果。"""
    status: str                     # "OK" / "REJECTED" / "INTERNAL_ERROR"
    ticket_id: int | None
    ticket_version: int | None
    notification_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class NotificationDelivery:
    """Outbox 通知投递记录（从 DB 读取的只读视图）。"""
    id: int
    dedupe_key: str
    ticket_id: int | None
    target_type: str               # "group" | "private"
    target_id: str                 # group_id 或 userId
    status: str


# ───────────────────────── 云端模型客户端接口 ─────────────────────────


class ModelClient(Protocol):
    """OpenAI-compatible 云端模型客户端协议。

    Task 4 实现，后续 Task 注入；测试时用 mock 替换。
    """
    async def complete_json(
        self,
        *,
        payload: dict[str, Any],
        schema: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]: ...
