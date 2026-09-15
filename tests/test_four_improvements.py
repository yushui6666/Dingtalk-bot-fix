"""四项改进专项测试（2026-08-24）。

需求 #1 编号纠错：显式编号不存在/状态不允许时硬拒绝，不静默归属其他工单。
需求 #2 静默化：纯告知回执取消；建单/完工确认/错误澄清保留。
需求 #3 店长确认流：工程师报完工→PENDING_CONFIRM→店长确认/驳回；确认期聊天归档。
需求 #4 响应 SLA：1h 提醒责任方，4h 升级（群内+单聊），均每周期只发一次不循环。
"""

from __future__ import annotations

from datetime import datetime, timedelta
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

_PROTOCOL_PATH = Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"

GROUP = {
    "group_id": "G1", "store_name": "测试群（示例）",
    "manager_ids": ["uid-mgr"], "engineer_ids": ["uid-eng"],
    "other_member_ids": [],
    "engineering_leader_id": "uid-baisong",
    "regional_manager_id": "uid-regional",
}


class FakeClassifier:
    """# 语法走关键词匹配，其余返回 chat.ignore。"""

    def __init__(self, protocol=None) -> None:
        self.responses = {}
        self.protocol = protocol

    async def classify(self, message, candidates=None, pending_action=None, history=None):
        from semantics.types import SemanticDecision

        if message.message_id in self.responses:
            return self.responses[message.message_id]
        text = (message.content or "").strip()
        if text.startswith("#") and self.protocol is not None:
            from semantics.keyword_matcher import match_keyword as mk

            kw = mk(text, self.protocol)
            if kw is not None and kw.intent != "system.clarify":
                return SemanticDecision(
                    protocol_version=kw.protocol_version, source="SEMANTIC_MODEL",
                    intent=kw.intent, target_ticket_no=kw.target_ticket_no,
                    intent_confidence=0.95, fields=dict(kw.fields),
                    missing_fields=kw.missing_fields, evidence=kw.evidence,
                )
        return SemanticDecision(
            protocol_version="4.0.0", source="SEMANTIC_MODEL",
            intent="chat.ignore", target_ticket_no=None, intent_confidence=0.0,
        )


class RecordingNotifier(Notifier):
    """记录群发与单聊目标的通知器。"""

    def __init__(self, db) -> None:
        super().__init__(db, lambda target, text: self.calls.append((target, text)))
        self.calls: list[tuple[str, str]] = []


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.upsert_group(GROUP)
    protocol = load_protocol(_PROTOCOL_PATH)
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


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _ticket(db: Database, suffix: str) -> dict:
    rows = db.list_group_tickets("G1")
    matches = [r for r in rows if r["ticket_no"].endswith(suffix)]
    assert matches, f"未找到以 {suffix} 结尾的工单"
    return matches[0]


async def _make_two_tickets(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    await env.process("#报修\n主题：门锁\n位置：二楼\n问题描述：打不开\n时效：3天", "m2")


# ───────────────────────── 需求 #1 编号纠错 ─────────────────────────


@pytest.mark.asyncio
async def test_wrong_number_hard_rejects_without_side_effect(env):
    """编号写错：报错并列出候选，绝不静默归属到其他活动工单执行完成。"""
    await _make_two_tickets(env)
    status = await env.process(
        "#完毕 工单编号：测试群（示例）-收银机-1天-099", "mX", role="ENGINEER", sender="uid-eng"
    )
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='mX'"
    ).fetchone()
    assert row["processed_result"] == "REJECTED"
    # 两张工单都未被误完成
    assert all(t["status"] in ("ACTIVE", "ACTIVE_OVERDUE") for t in db_all(env))
    # 有明确纠错文案（calls 元素为 (target, text)，取第 2 位文本断言）
    assert any("未找到工单" in t and "099" in t for _, t in env.notifier.calls)


@pytest.mark.asyncio
async def test_wrong_number_lists_candidates_and_suggestion(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    await env.process("#报修\n主题：门锁\n位置：二楼\n问题描述：打不开\n时效：3天", "m2")
    await env.process(
        "#补充 工单编号：测试群（示例）-门锁-3天-009 内容：x", "mX",
        role="ENGINEER", sender="uid-eng",
    )
    texts = [t for _, t in env.notifier.calls]
    assert any("未找到工单" in t and "-009" in t for t in texts)
    # 候选短编号提示（001/002）
    assert any("本群工单" in t and "001" in t and "002" in t for t in texts)


@pytest.mark.asyncio
async def test_number_of_completed_ticket_rejected_for_complete(env):
    """对已完成工单再发「#完毕」→ 明确提示状态不符，而非静默跳过。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    no = "测试群（示例）-收银机-1天-001"
    # 店长直接完成（店长发起即完成）
    await env.process(f"#完毕 工单编号:{no}", "mC")
    assert _ticket(env.db, "-001")["status"] == "COMPLETED"
    # 工程师再对同一编号发完毕 → 状态不符拒绝
    await env.process(f"#完毕 工单编号:{no}", "mX", role="ENGINEER", sender="uid-eng")
    row = env.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='mX'"
    ).fetchone()
    assert row["processed_result"] == "REJECTED"
    assert any("已完成" in t and "不能执行" in t for _, t in env.notifier.calls)


def db_all(env):
    return env.db.list_group_tickets("G1")


# ───────────────────── 需求 #2 + #3 完成确认流 ─────────────────────


@pytest.mark.asyncio
async def test_engineer_complete_goes_to_pending_confirm(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    no = "测试群（示例）-收银机-1天-001"
    await env.process(f"#完毕 工单编号:{no}", "m2", role="ENGINEER", sender="uid-eng")
    t = _ticket(env.db, "-001")
    assert t["status"] == "PENDING_CONFIRM"
    assert t["closed_at"] is None
    # 责任方切到店长侧（驱动确认 SLA）
    assert t["waiting_side"] == "MANAGER_SIDE" and t["waiting_since"]
    # 群内出现请店长确认的话术
    assert any("请店长回复「确认修好」" in txt for _, txt in env.notifier.calls)


@pytest.mark.asyncio
async def test_manager_keyword_confirms_completion(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    no = "测试群（示例）-收银机-1天-001"
    await env.process(f"#完毕 工单编号:{no}", "m2", role="ENGINEER", sender="uid-eng")
    # 店长 plain text（不写编号，单候选兜底）：线上由 AI 判为确认完成，
    # 测试桩用显式决策模拟模型输出，target_ticket_no 留空验证单候选回填
    env.classifier.responses["m3"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.confirm_complete", target_ticket_no=None, intent_confidence=0.95,
    )
    await env.process("确认修好", "m3")
    t = _ticket(env.db, "-001")
    assert t["status"] == "COMPLETED"
    assert t["closed_at"] is not None
    assert t["completed_confirm_by"] == "uid-mgr"
    assert t["completed_confirm_at"] is not None


@pytest.mark.asyncio
async def test_manager_rejects_back_to_active(env):
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    no = "测试群（示例）-收银机-1天-001"
    await env.process(f"#完毕 工单编号:{no}", "m2", role="ENGINEER", sender="uid-eng")
    # 店长 plain text「没修好，还是死机」：线上由 AI 判为驳回完工（自然语言
    # 带逗号后缀，非 # 关键词语法），测试桩用显式决策模拟模型输出
    env.classifier.responses["m3"] = SemanticDecision(
        protocol_version="4.0.0", source="SEMANTIC_MODEL",
        intent="ticket.reject_complete", target_ticket_no=None, intent_confidence=0.95,
    )
    await env.process("没修好，还是死机", "m3")
    t = _ticket(env.db, "-001")
    assert t["status"] == "ACTIVE"
    assert any("店长反馈未修好" in txt for _, txt in env.notifier.calls)


@pytest.mark.asyncio
async def test_manager_own_completion_is_direct(env):
    """店长本人报完工 → 直接 COMPLETED（用户决策 Q3）。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    no = "测试群（示例）-收银机-1天-001"
    await env.process(f"#完毕 工单编号:{no}", "m2")  # 默认 MANAGER
    assert _ticket(env.db, "-001")["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_confirm_window_archives_chat_into_ticket(env):
    """待店长确认期间的所有聊天（含闲聊）归档进工单。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    no = "测试群（示例）-收银机-1天-001"
    await env.process(f"#完毕 工单编号:{no}", "m2", role="ENGINEER", sender="uid-eng")
    tid = _ticket(env.db, "-001")["id"]
    # 闲聊（FakeClassifier 返回 chat.ignore）也应入档
    status = await env.process("今天天气不错哈哈哈", "m3", role="OTHER", sender="uid-other")
    row = env.db.connect().execute(
        "SELECT content FROM messages WHERE ticket_id=? AND message_id='m3'", (tid,)
    ).fetchone()
    assert row is not None and "天气" in row["content"]
    link = env.db.connect().execute(
        "SELECT link_type FROM message_ticket_links WHERE message_id='m3'"
    ).fetchone()
    assert link is not None and link["link_type"] == "CONFIRM_WINDOW"


# ───────────────────────── 需求 #4 响应 SLA ─────────────────────────


def _set_waiting(db: Database, ticket_id: int, side: str, since: datetime) -> None:
    conn = db.connect()
    conn.execute(
        "UPDATE tickets SET waiting_side=?, waiting_since=?, status='ACTIVE' WHERE id=?",
        (side, since.strftime("%Y-%m-%d %H:%M:%S"), ticket_id),
    )


def _insert_bare_ticket(
    db: Database, *, store: str = "测试群（示例）", created_at: str | None = None
) -> int:
    conn = db.connect()
    if created_at is None:
        created_at = conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
    cur = conn.execute(
        """INSERT INTO tickets (ticket_no, group_id, store_name, reporter_id, subject,
               location, problem_description, sla_days, status, version, created_at)
           VALUES (?, 'G1', ?, 'r', 's', 'l', 'p', 1, 'ACTIVE', 1, ?)""",
        (f"{store}-主题-1天-{db.next_ticket_seq('G1'):03d}", store, created_at),
    )
    return cur.lastrowid


def _sla_env(tmp_path):
    db = Database(tmp_path / "sla.db")
    db.init_schema()
    db.upsert_group(GROUP)
    notifier = RecordingNotifier(db)
    from workers.scheduler import SchedulerWorker

    worker = SchedulerWorker(db=db, notifier=notifier, interval=60)
    return db, notifier, worker


@pytest.mark.asyncio
async def test_response_sla_level1_and_level2(tmp_path, monkeypatch):
    from workers.scheduler import SchedulerWorker  # noqa: F401

    # .env 自动加载后会注入真实工程总监A userId；本测试验证「未配置 env → 回退群配置」路径
    import config as config_mod

    monkeypatch.setattr(config_mod, "RESPONSE_SLA_ENGINEER_ESCALATE_USER_ID", "")
    db, notifier, worker = _sla_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    # 超 1h 不满 4h：只一级提醒工程师
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(minutes=70))
    worker.scan(now)
    texts = [t for tgt, t in notifier.calls]
    assert any("无工程师响应" in t for t in texts)
    assert not any(t.startswith("user:") for tgt, t in notifier.calls)

    # 超 4h：二级升级（群内 + 单聊工程总监A）
    notifier.calls.clear()
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(hours=5))
    worker.scan(now)
    group_texts = [t for tgt, t in notifier.calls if not tgt.startswith("user:")]
    user_targets = [tgt for tgt, t in notifier.calls if tgt.startswith("user:")]
    assert any("已升级提醒工程总监A" in t for t in group_texts)
    assert "user:uid-baisong" in user_targets


@pytest.mark.asyncio
async def test_response_sla_manager_side_escalates_to_regional(tmp_path):
    db, notifier, worker = _sla_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    _set_waiting(db, tid, "MANAGER_SIDE", now - timedelta(hours=5))
    worker.scan(now)
    user_targets = [tgt for tgt, t in notifier.calls if tgt.startswith("user:")]
    assert "user:uid-regional" in user_targets
    assert any("已升级提醒区域经理" in t for tgt, t in notifier.calls if not tgt.startswith("user:"))


@pytest.mark.asyncio
async def test_response_sla_dm_once_per_waiting_cycle(tmp_path):
    """升级群提醒与单聊均每个等待周期只发一次，不循环（用户决策 2026-08-26）。"""
    db, notifier, worker = _sla_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(hours=5))
    worker.scan(now)
    assert sum(1 for tgt, _ in notifier.calls if tgt.startswith("user:")) == 1

    # 超 8h（跨两个原 bucket）：群内升级与单聊均不再重复
    notifier.calls.clear()
    worker.scan(now + timedelta(hours=3))
    group_escalations = [
        t for tgt, t in notifier.calls
        if not tgt.startswith("user:") and "已升级提醒" in t
    ]
    dm_again = [tgt for tgt, _ in notifier.calls if tgt.startswith("user:")]
    assert group_escalations == []  # 群内每周期只发一次
    assert dm_again == []         # 单聊每周期只发一次


@pytest.mark.asyncio
async def test_response_sla_dedupes_within_same_cycle(tmp_path):
    db, notifier, worker = _sla_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(minutes=70))
    worker.scan(now)
    first = len(notifier.calls)
    worker.scan(now + timedelta(minutes=10))  # 同周期重复扫描 → 不重发
    assert len(notifier.calls) == first


@pytest.mark.asyncio
async def test_response_sla_pending_confirm_prompts_manager(env):
    """PENDING_CONFIRM 的店长确认超时走响应 SLA 文案。"""
    await env.process("#报修\n主题：收银机\n位置：前台\n问题描述：死机\n时效：1天", "m1")
    no = "测试群（示例）-收银机-1天-001"
    await env.process(f"#完毕 工单编号:{no}", "m2", role="ENGINEER", sender="uid-eng")
    t = _ticket(env.db, "-001")
    # 把等待起点拨回到 2 小时前，触发一级提醒
    past = datetime.now() - timedelta(hours=2)
    env.db.connect().execute(
        "UPDATE tickets SET waiting_since=? WHERE id=?",
        (past.strftime("%Y-%m-%d %H:%M:%S"), t["id"]),
    )
    from workers.scheduler import SchedulerWorker

    worker = SchedulerWorker(db=env.db, notifier=env.notifier, interval=60)
    worker.scan(datetime.now())
    texts = [txt for _, txt in env.notifier.calls]
    assert any("请店长回复「确认修好」" in txt and "已等待" in txt for txt in texts)
    assert any(
        all(part in txt for part in (
            "特殊情况", "等待到货", "等待工程师上门", "等待客户配合",
            "等待第三方", "预计恢复",
        ))
        for txt in texts
    )


# ─────────── 一次性分界：存量工单豁免响应 SLA（用户决策 2026-08-26） ───────────


@pytest.mark.asyncio
async def test_response_sla_ignores_tickets_created_before_cutoff(tmp_path):
    """分界时刻前建单的存量工单永不触发响应 SLA：即使超 4h 等待也零发声。"""
    db, notifier, worker = _sla_env(tmp_path)
    tid = _insert_bare_ticket(db, created_at="2026-08-25 10:00:00")
    now = datetime.now()
    _set_waiting(db, tid, "MANAGER_SIDE", now - timedelta(hours=5))
    worker.scan(now)
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_response_sla_boundary_created_at_is_inclusive(tmp_path):
    """created_at 恰等于分界时刻 → 正常参与响应 SLA（>= 含边界）。"""
    import config as config_mod

    db, notifier, worker = _sla_env(tmp_path)
    tid = _insert_bare_ticket(db, created_at=config_mod.RESPONSE_SLA_EFFECTIVE_FROM)
    now = datetime.now()
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(minutes=70))
    worker.scan(now)
    assert any("无工程师响应" in t for _, t in notifier.calls)


# ─────────── 总开关：RESPONSE_SLA_ENABLED（2026-08-26） ───────────


@pytest.mark.asyncio
async def test_response_sla_disabled_suppresses_all_reminders(tmp_path, monkeypatch):
    """RESPONSE_SLA_ENABLED=false 时，整条响应 SLA 静默：1h/4h/单聊/循环均不触发。"""
    import config as config_mod

    monkeypatch.setattr(config_mod, "RESPONSE_SLA_ENABLED", False)
    db, notifier, worker = _sla_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(hours=5))
    worker.scan(now)
    assert notifier.calls == []
    # 超 8h 循环也不应产生群内升级
    worker.scan(now + timedelta(hours=3))
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_response_sla_reenabled_resumes_reminders(tmp_path, monkeypatch):
    """关闭后重新置为 true → 下一轮扫描即恢复提醒（无需重启）。"""
    import config as config_mod

    monkeypatch.setattr(config_mod, "RESPONSE_SLA_ENABLED", False)
    db, notifier, worker = _sla_env(tmp_path)
    tid = _insert_bare_ticket(db)
    now = datetime.now()
    _set_waiting(db, tid, "ENGINEER_SIDE", now - timedelta(minutes=70))
    worker.scan(now)
    assert notifier.calls == []

    monkeypatch.setattr(config_mod, "RESPONSE_SLA_ENABLED", True)
    worker.scan(now)
    assert any("无工程师响应" in t for _, t in notifier.calls)
