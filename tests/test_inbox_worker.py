"""Inbox Worker 并发测试：跨群并行、群内串行（计划书 §8.2）。

用一个带延迟的分类器探测最大并发模型调用数：两条分属不同群的自然语言
消息应同时进入模型调用（max_active >= 2）；同群两条则严格串行（max_active == 1）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from db import Database
from models import NormalizedMessage
from notifier import Notifier
from pipeline import MessageProcessingPipeline, RuntimeMode
from routing.pending_actions import PendingActionService
from routing.ticket_contexts import TicketContextStore
from routing.ticket_router import TicketRouter
from semantics.protocol_loader import load_protocol
from semantics.types import SemanticDecision
from tickets.executor import TicketCommandExecutor
from tickets.repository import TicketRepository
from workers.inbox_worker import InboxWorker

_PROTOCOL_PATH = Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"

GROUPS = [
    {"group_id": "G1", "store_name": "店A",
     "manager_ids": ["uid-mgr"], "engineer_ids": [], "other_member_ids": []},
    {"group_id": "G2", "store_name": "店B",
     "manager_ids": ["uid-mgr"], "engineer_ids": [], "other_member_ids": []},
]


class SlowClassifier:
    """记录同时进行的模型调用数。兼容全AI架构：#报修 等也走模型。"""

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def classify(self, message, candidates=None, pending_action=None, history=None) -> SemanticDecision:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        # 全AI下 #报修 也走模型：模拟 AI 正确解析建单
        text = (message.content or "").strip()
        if text.startswith("#报修"):
            # 简易解析：提取主题/位置/问题描述/时效
            import re as _re
            subject = _re.search(r"主题[：:]\s*([^\n]+)", text)
            location = _re.search(r"位置[：:]\s*([^\n]+)", text)
            desc = _re.search(r"问题描述[：:]\s*([^\n]+)", text)
            sla_m = _re.search(r"时效[：:]\s*(1天|3天|7天|待商榷)", text)
            fields: dict = {}
            if subject:
                fields["subject"] = subject.group(1).strip()
            if location:
                fields["location"] = location.group(1).strip()
            if desc:
                fields["problem_description"] = desc.group(1).strip()
            if sla_m:
                fields["sla"] = sla_m.group(1).strip()
            return SemanticDecision(protocol_version="4.0.0", source="SEMANTIC_MODEL",
                                     intent="ticket.create", target_ticket_no=None,
                                     intent_confidence=0.95, fields=fields)
        return SemanticDecision(protocol_version="4.0.0", source="SEMANTIC_MODEL",
                                 intent="chat.ignore", target_ticket_no=None, intent_confidence=0.0)


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "worker.db")
    db.init_schema()
    for g in GROUPS:
        db.upsert_group(g)
    protocol = load_protocol(_PROTOCOL_PATH)
    repo = TicketRepository(db)
    router = TicketRouter()
    context = TicketContextStore(db)
    pending = PendingActionService(db)
    executor = TicketCommandExecutor(db, repo)
    notifier = Notifier(db, lambda target, text: None)
    classifier = SlowClassifier()
    pipeline = MessageProcessingPipeline(
        db=db, repo=repo, protocol=protocol, router=router, context=context,
        pending=pending, executor=executor, notifier=notifier, classifier=classifier,
        mode=RuntimeMode.PRODUCTION,
    )
    yield SimpleNamespace(db=db, classifier=classifier, pipeline=pipeline,
                          protocol=protocol, notifier=notifier)
    db.close()


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _enqueue(env, group_id: str, text: str, message_id: str) -> None:
    msg = NormalizedMessage(
        message_id=message_id, group_id=group_id, sender_id="uid-mgr", sender_name="u",
        content=text, message_type="text", sent_at=datetime.now(), sender_role="MANAGER",
    )
    env.db.enqueue_message(msg)


async def _run_worker_for(env, seconds: float, group_ids: list[str]) -> InboxWorker:
    worker = InboxWorker(
        db=env.db, pipeline=env.pipeline, notifier=env.notifier,
        poll_interval=0.02, batch=5, group_ids=group_ids,
    )
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return worker


@pytest.mark.asyncio
async def test_different_groups_parallel_model_calls(env):
    """两条不同群的自然语言消息应并发进模型（max_active >= 2）。"""
    _enqueue(env, "G1", "帮我查一下工单", "m1")
    _enqueue(env, "G2", "随便聊聊天气", "m2")
    await _run_worker_for(env, 0.6, ["G1", "G2"])
    statuses = {
        r["message_id"]: (r["status"], r["processed_result"])
        for r in env.db.connect().execute(
            "SELECT message_id, status, processed_result FROM inbox_messages"
        ).fetchall()
    }
    print("  statuses:", statuses)
    assert all(status == "COMPLETED" for status, _ in statuses.values())
    assert env.classifier.max_active >= 2, f"并发不足: max_active={env.classifier.max_active}"


@pytest.mark.asyncio
async def test_same_group_serial_processing(env):
    """同群两条自然语言消息应串行（max_active == 1）。"""
    _enqueue(env, "G1", "帮我查一下工单", "m1")
    _enqueue(env, "G1", "随便聊聊天气", "m2")
    await _run_worker_for(env, 0.6, ["G1"])
    statuses = {
        r["message_id"]: (r["status"], r["processed_result"])
        for r in env.db.connect().execute(
            "SELECT message_id, status, processed_result FROM inbox_messages"
        ).fetchall()
    }
    print("  statuses:", statuses)
    assert all(status == "COMPLETED" for status, _ in statuses.values())
    assert env.classifier.max_active <= 1, f"同群未串行: max_active={env.classifier.max_active}"


@pytest.mark.asyncio
async def test_group_keyword_messages_processed_while_model_slow(env):
    """某群模型慢，不应阻塞另一群的建单（全AI架构下均为模型调用，但按群并行）。"""
    _enqueue(env, "G2", "帮我查一下工单（模型慢）", "m1")  # G2 走慢模型
    _enqueue(env, "G1", "#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m2")  # G1 全AI建单
    await _run_worker_for(env, 0.6, ["G1", "G2"])
    m2 = env.db.connect().execute(
        "SELECT status, processed_result FROM inbox_messages WHERE message_id='m2'"
    ).fetchone()
    assert m2["status"] == "COMPLETED"
    tickets = env.db.list_active_tickets("G1")
    assert len(tickets) == 1, "G1 建单应不被 G2 的慢模型阻塞（按群并行）"
