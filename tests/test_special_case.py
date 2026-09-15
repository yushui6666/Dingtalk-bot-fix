"""特殊情况暂停计时专项测试（2026-08-26）。

场景：响应 SLA 一小时提醒引导责任方回复
「特殊情况：原因；预计恢复：时间」后：
1. 识别为 ticket.special_case.submit 并群内回执；
2. 暂停期间时效 SLA / 响应 SLA / 签收后每日催均不再催办；
3. 预计恢复时间过期后每日跟进提醒一次；
4. 再次声明 → 旧暂停关闭、新暂停生效；
5. 实际业务动作恢复 → 关闭暂停并按实际暂停时长顺延截止时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db import Database
from models import NormalizedMessage
from pipeline import MessageProcessingPipeline, RuntimeMode
from routing.pending_actions import PendingActionService
from routing.ticket_contexts import TicketContextStore
from routing.ticket_router import TicketRouter
from semantics.protocol_loader import load_protocol
from semantics.types import SemanticDecision
from tickets.executor import TicketCommandExecutor
from tickets.repository import TicketRepository
from workers.scheduler import SchedulerWorker

from test_four_improvements import (
    FakeClassifier,
    GROUP,
    RecordingNotifier,
    _insert_bare_ticket,
    _set_waiting,
    _ticket,
)

_FMT = "%Y-%m-%d %H:%M:%S"


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture()
def env(tmp_path):
    from pathlib import Path

    protocol_path = (
        Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"
    )
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.upsert_group(GROUP)
    protocol = load_protocol(protocol_path)
    repo = TicketRepository(db)
    context = TicketContextStore(db)
    pending = PendingActionService(db)
    executor = TicketCommandExecutor(db, repo)
    notifier = RecordingNotifier(db)
    classifier = FakeClassifier(protocol=protocol)
    pipeline = MessageProcessingPipeline(
        db=db, repo=repo, protocol=protocol, router=TicketRouter(),
        context=context, pending=pending, executor=executor,
        notifier=notifier, classifier=classifier, mode=RuntimeMode.PRODUCTION,
    )

    async def process(text, message_id, role="MANAGER", sender="uid-mgr"):
        msg = NormalizedMessage(
            message_id=message_id, group_id="G1", sender_id=sender, sender_name="u",
            content=text, message_type="text", sent_at=datetime.now(), sender_role=role,
        )
        db.enqueue_message(msg)
        row = db.connect().execute(
            "SELECT * FROM inbox_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        return await pipeline.process(dict(row))

    yield SimpleNamespace(db=db, notifier=notifier, process=process, classifier=classifier)
    db.close()


def _sc_env(tmp_path):
    db = Database(tmp_path / "sc.db")
    db.init_schema()
    db.upsert_group(GROUP)
    notifier = RecordingNotifier(db)
    worker = SchedulerWorker(db=db, notifier=notifier, interval=60)
    return db, notifier, worker


def _add_case(db, tid, *, msg_id="msg-spc", started=None, expected=None, reason="等待到货"):
    now_str = (started or datetime.now()).strftime(_FMT)
    return db.add_special_case(
        tid, msg_id, reason, "一小时内",
        expected.strftime(_FMT) if expected else None,
        "uid-eng", now_str,
    )


def _active_case(db, tid):
    return db.connect().execute(
        "SELECT * FROM ticket_special_cases WHERE ticket_id=? AND resumed_at IS NULL",
        (tid,),
    ).fetchone()


# ───────────────────────── 1. 识别 + 回执 ─────────────────────────


@pytest.mark.asyncio
async def test_special_case_reply_pauses_and_receives_receipt(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t = _ticket(env.db, "-001")
    env.classifier.responses["m2"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.special_case.submit", target_ticket_no=None,
        intent_confidence=0.9,
        fields={
            "special_case_reason": "等待门店接客两场连场",
            "expected_resume_at": "一小时内",
        },
    )
    status = await env.process(
        "特殊情况：等待门店接客两场连场 预计恢复：一小时内", "m2",
        role="ENGINEER", sender="uid-eng",
    )
    assert status == "COMPLETED"
    case = _active_case(env.db, t["id"])
    assert case is not None
    assert case["reason"] == "等待门店接客两场连场"
    # 「一小时内」被解析为绝对时间
    assert case["expected_resume_at"] is not None
    texts = [txt for _, txt in env.notifier.calls]
    assert any("特殊情况" in txt and "等待门店接客两场连场" in txt for txt in texts)
    # 声明暂停不切换责任方（等待方仍是工程师侧）
    after = env.db.get_ticket(t["id"])
    assert after["waiting_side"] == t["waiting_side"]


# ───────────────────────── 2. 暂停期间停止催办 ─────────────────────────


def test_sla_reminder_paused_while_special_case_active(tmp_path):
    db, notifier, worker = _sc_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    past = (now - timedelta(hours=2)).strftime(_FMT)
    db.connect().execute(
        "UPDATE tickets SET current_deadline_at=?, sla_days=1 WHERE id=?", (past, tid)
    )
    _add_case(db, tid, started=now - timedelta(minutes=5),
              expected=now + timedelta(hours=1))
    worker.scan(now)
    assert not any("已超时效" in t for _, t in notifier.calls)

    db.close_active_special_case(tid, "msg-resume", now.strftime(_FMT))
    worker.scan(now)
    assert any("已超时效" in t for _, t in notifier.calls)


def test_response_sla_paused_while_special_case_active(tmp_path):
    db, notifier, worker = _sc_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(hours=5))
    _add_case(db, tid, started=now - timedelta(minutes=10),
              expected=now + timedelta(hours=1))
    worker.scan(now)
    assert not any("无工程师响应" in t for _, t in notifier.calls)
    assert not any("已升级提醒" in t for _, t in notifier.calls)

    db.close_active_special_case(tid, "msg-resume", now.strftime(_FMT))
    worker.scan(now)
    assert any("无工程师响应" in t for _, t in notifier.calls)


# ───────────────────────── 3. 预计恢复到期每日跟进 ─────────────────────────


def test_special_case_follow_up_disabled(tmp_path):
    """每日跟进已按用户决策彻底停用（2026-08-27）：预计恢复过期、
    无法解析且超 24h、多日多次扫描，均不再发任何跟进提醒；
    暂停期间彻底静默，恢复只能由业务动作或再次声明触发。"""
    db, notifier, worker = _sc_env(tmp_path)
    now = datetime.now()
    tid_due = _insert_bare_ticket(db)
    _add_case(db, tid_due, msg_id="spc-due",
              started=now - timedelta(hours=3), expected=now - timedelta(hours=1))
    tid_stale = _insert_bare_ticket(db)
    _add_case(db, tid_stale, msg_id="spc-stale",
              started=now - timedelta(hours=30), expected=None)
    worker.scan(now)
    worker.scan(now + timedelta(minutes=10))
    worker.scan(now + timedelta(days=2))
    banned = ("请回复进展", "已暂停超过")
    assert not any(b in t for _, t in notifier.calls for b in banned)


# ───────────────────────── 4. 再次声明 = 续期 ─────────────────────────


@pytest.mark.asyncio
async def test_renewal_closes_previous_case(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t = _ticket(env.db, "-001")
    for i, (msg_id, reason) in enumerate([
        ("m2", "等待门店接客两场连场"),
        ("m3", "等待第三方配件"),
    ]):
        env.classifier.responses[msg_id] = SemanticDecision(
            protocol_version="4.0.0", source="SEMANTIC_MODEL",
            intent="ticket.special_case.submit", target_ticket_no=None,
            intent_confidence=0.9,
            fields={"special_case_reason": reason, "expected_resume_at": "一小时内"},
        )
        await env.process(f"特殊情况：{reason} 预计恢复：一小时内", msg_id,
                          role="ENGINEER", sender="uid-eng")
    cases = env.db.connect().execute(
        "SELECT * FROM ticket_special_cases WHERE ticket_id=? ORDER BY id", (t["id"],)
    ).fetchall()
    assert len(cases) == 2
    assert cases[0]["resumed_at"] is not None
    assert cases[1]["resumed_at"] is None
    assert cases[1]["reason"] == "等待第三方配件"


# ───────────────────────── 5. 业务动作恢复 + 截止时间顺延 ─────────────────────────


@pytest.mark.asyncio
async def test_resume_action_closes_case_and_shifts_deadline(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    t = _ticket(env.db, "-001")
    env.classifier.responses["m2"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.special_case.submit", target_ticket_no=None,
        intent_confidence=0.9,
        fields={"special_case_reason": "等待门店接客两场连场", "expected_resume_at": "一小时内"},
    )
    await env.process("特殊情况：等待门店接客两场连场 预计恢复：一小时内", "m2",
                      role="ENGINEER", sender="uid-eng")
    tid = t["id"]
    case = _active_case(env.db, tid)
    assert case is not None

    # 把暂停起点拨回 2 小时前，模拟实际暂停 2h
    env.db.connect().execute(
        "UPDATE ticket_special_cases SET submitted_at=? WHERE id=?",
        ((datetime.now() - timedelta(hours=2)).strftime(_FMT), case["id"]),
    )
    before = datetime.strptime(env.db.get_ticket(tid)["current_deadline_at"], _FMT)

    env.classifier.responses["m3"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.diagnosis.submit", target_ticket_no=None,
        intent_confidence=0.9,
        fields={"diagnosis_items": ["电源故障"]},
    )
    await env.process("#故障判断\n故障判断：电源故障", "m3",
                      role="ENGINEER", sender="uid-eng")

    assert _active_case(env.db, tid) is None
    after = datetime.strptime(env.db.get_ticket(tid)["current_deadline_at"], _FMT)
    shifted = (after - before).total_seconds()
    assert timedelta(seconds=7140) <= (after - before) <= timedelta(seconds=7260)


# ───────────────────────── 6. 相对时间解析 ─────────────────────────


def test_parse_resume_time_variants():
    from tickets.timeexpr import parse_resume_time

    now = datetime(2026, 8, 26, 16, 0, 0)
    cases = {
        "一小时内": now + timedelta(hours=1),
        "1小时": now + timedelta(hours=1),
        "2小时后": now + timedelta(hours=2),
        "半小时": now + timedelta(minutes=30),
        "40分钟": now + timedelta(minutes=40),
        "3天": now + timedelta(days=3),
        "3天后": now + timedelta(days=3),
        "明天 14:00": datetime(2026, 8, 27, 14, 0, 0),
        "明天14点": datetime(2026, 8, 27, 14, 0, 0),
        "后天": datetime(2026, 8, 28, 0, 0, 0),
        "2026-08-27 10:00": datetime(2026, 8, 27, 10, 0, 0),
        "8月28日": datetime(2026, 8, 28, 0, 0, 0),
        "18:30": datetime(2026, 8, 26, 18, 30, 0),
    }
    for text, expected in cases.items():
        assert parse_resume_time(text, now) == expected, text

    # 已过时刻按次日同一时刻理解
    assert parse_resume_time("10:00", now) == datetime(2026, 8, 27, 10, 0, 0)
    # 解析不了 → None
    assert parse_resume_time("尽快", now) is None
    assert parse_resume_time("", now) is None


# ───────────────── 7. 真·停表：暂停段不计入响应 SLA（2026-08-27） ─────────────────


def test_pause_freezes_response_sla_accrual(tmp_path):
    """停表语义：等待 30min 后暂停 70min，解除时有效等待仍≈30min，
    解除瞬间不得补发提醒；真实等待满 1h 才发。"""
    db, notifier, worker = _sc_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(minutes=100))
    _add_case(db, tid, started=now - timedelta(minutes=70),
              expected=now + timedelta(hours=1))
    worker.scan(now)
    assert not any("无工程师响应" in t for _, t in notifier.calls)  # 暂停中豁免

    closed = db.close_active_special_case(tid, "msg-resume", now.strftime(_FMT))
    assert closed is not None
    ws = db.connect().execute(
        "SELECT waiting_since FROM tickets WHERE id=?", (tid,)
    ).fetchone()["waiting_since"]
    assert ws == (now - timedelta(minutes=30)).strftime(_FMT)  # 停表后移 70min

    worker.scan(now)
    assert not any("无工程师响应" in t for _, t in notifier.calls)

    worker.scan(now + timedelta(minutes=31))
    assert any("无工程师响应" in t for _, t in notifier.calls)


def test_close_keeps_waiting_when_cycle_started_after_pause(tmp_path):
    """守卫：等待周期起于暂停开始之后（恢复动作重置的新周期）→ 不顺延原值。
    （当前实现本就不顺延；此用例钉住边界防未来回归。）"""
    db, _, _ = _sc_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    _add_case(db, tid, started=now - timedelta(hours=2))
    fresh = (now - timedelta(minutes=10)).strftime(_FMT)
    _set_waiting(db, tid, "ENGINEER_SIDE", datetime.strptime(fresh, _FMT))
    db.close_active_special_case(tid, "msg-resume", now.strftime(_FMT))
    ws = db.connect().execute(
        "SELECT waiting_since FROM tickets WHERE id=?", (tid,)
    ).fetchone()["waiting_since"]
    assert ws == fresh
