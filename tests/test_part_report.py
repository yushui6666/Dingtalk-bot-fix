"""备件登记（2026-09-09）测试：parts 作为 repair_plan 可选字段，仅记录。

规格：docs/superpowers/specs/2026-09-09-spare-parts-report-design.md §8
关键约束：
- 只在该「店 × 主题」备件表 + 该店「工具」池里匹配，记录规范名；
- 未命中 / 无表 / 候选外值 → 静默丢弃，不写表、不回执、不催填；
- 不做库存增减。
"""
import pytest

from db import Database
from semantics.types import ValidatedCommand
from tickets.executor import RESULT_OK, TicketCommandExecutor
from tickets.repository import TicketRepository

# 模拟门店备件池 {规范名: {别名集合}}。店"商场15店"主题"特工"（工具池并入）
POOL = {
    "继电器DC12V": {"继电器", "继电器12v"},
    "干簧管": {"干簧管磁控", "磁簧管"},
    "锤子": {"铁锤"},  # 工具池件（场景固定并入）
}
# 跨店店（"商场02店"）特有的件，绝不能在商场15店匹配到
OTHER_STORE_ONLY = {"阻尼铰链": {"商场02铰链"}}


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "part.db")
    database.init_schema()
    database.upsert_group({"group_id": "G1", "store_name": "商场15店"})
    yield database
    database.close()


def _ticket(db, subject="特工", store="商场15店"):
    with db.transaction("seed_ticket"):
        return db.insert_ticket({
            "ticket_no": "T1", "group_id": "G1", "store_name": store,
            "reporter_id": "u1", "subject": subject, "location": "前台",
            "problem_description": "坏了", "sla_days": 1,
            "initial_deadline_at": "2026-09-09 10:00:00",
            "current_deadline_at": "2026-09-09 10:00:00", "status": "ACTIVE",
        })


def _executor(db, monkeypatch):
    # 让 store_parts 在该测试下使用受控池，避免依赖真实 data/stores 文件
    monkeypatch.setattr(
        "tickets.executor.build_pool",
        lambda store, subject: dict(POOL) if store == "商场15店" else dict(OTHER_STORE_ONLY),
    )
    return TicketCommandExecutor(db, TicketRepository(db))


def _part_command(message_id, fields, ticket_id):
    return ValidatedCommand(
        message_id=message_id, group_id="G1", actor_id="u1", actor_role="ENGINEER",
        intent="ticket.repair_plan.submit", target_ticket_id=ticket_id,
        expected_ticket_version=1, fields=fields, source="SEMANTIC_MODEL",
    )


def test_exact_name_writes_current_and_overwrites_previous(db, monkeypatch):
    ticket_id = _ticket(db)
    ex = _executor(db, monkeypatch)
    r1 = ex.execute(_part_command("m1", {"repair_method": "更换", "parts": ["继电器DC12V"]}, ticket_id))
    assert r1.status == RESULT_OK
    # 第二条用最新版本号，否则被乐观锁拒绝
    cur_version = db.get_ticket(ticket_id)["version"]
    cmd2 = _part_command("m2", {"repair_method": "更换", "parts": ["干簧管"]}, ticket_id)
    cmd2 = cmd2.__class__(
        message_id=cmd2.message_id, group_id=cmd2.group_id, actor_id=cmd2.actor_id,
        actor_role=cmd2.actor_role, intent=cmd2.intent, target_ticket_id=ticket_id,
        expected_ticket_version=cur_version, fields=cmd2.fields, source=cmd2.source,
    )
    r2 = ex.execute(cmd2)
    assert r2.status == RESULT_OK
    cur = db.get_current_part_version(ticket_id)
    assert cur is not None
    assert cur["parts"] == ["干簧管"]          # m2 覆盖 m1
    assert cur["is_current"] == 1
    hist = db.list_part_versions(ticket_id)
    assert [v["parts"] for v in hist] == [["继电器DC12V"], ["干簧管"]]
    assert [v["is_current"] for v in hist] == [0, 1]


def test_alias_normalizes_to_canonical_name(db, monkeypatch):
    ticket_id = _ticket(db)
    ex = _executor(db, monkeypatch)
    ex.execute(_part_command("m1", {"repair_method": "换", "parts": ["继电器"]}, ticket_id))  # 别名
    cur = db.get_current_part_version(ticket_id)
    assert cur is not None
    assert cur["parts"] == ["继电器DC12V"]     # 规范名，非口头词


def test_tool_pool_part_matches(db, monkeypatch):
    ticket_id = _ticket(db)
    ex = _executor(db, monkeypatch)
    ex.execute(_part_command("m1", {"repair_method": "用锤子敲", "parts": ["锤子"]}, ticket_id))
    cur = db.get_current_part_version(ticket_id)
    assert cur is not None
    assert cur["parts"] == ["锤子"]


def test_out_of_pool_part_silently_dropped_but_repair_recorded(db, monkeypatch):
    ticket_id = _ticket(db)
    ex = _executor(db, monkeypatch)
    r = ex.execute(_part_command(
        "m1", {"repair_method": "更换某奇件", "parts": ["不存在备件"]}, ticket_id))
    assert r.status == RESULT_OK
    # part_versions 未写入（静默跳过）
    assert db.get_current_part_version(ticket_id) is None
    assert db.list_part_versions(ticket_id) == []
    # 维修方式仍正常登记（不受备件被丢弃影响）
    rm_row = db._conn.execute(
        "SELECT * FROM repair_method_versions WHERE ticket_id=?", (ticket_id,)
    ).fetchone()
    assert rm_row is not None and rm_row["repair_method"] == "更换某奇件"


def test_no_parts_field_no_part_write(db, monkeypatch):
    ticket_id = _ticket(db)
    ex = _executor(db, monkeypatch)
    r = ex.execute(_part_command("m1", {"repair_method": "直接换"}, ticket_id))
    assert r.status == RESULT_OK
    assert db.list_part_versions(ticket_id) == []


def test_multiple_parts_joined_by_dun(db, monkeypatch):
    ticket_id = _ticket(db)
    ex = _executor(db, monkeypatch)
    ex.execute(_part_command(
        "m1", {"repair_method": "换", "parts": ["继电器DC12V", "干簧管"]}, ticket_id))
    cur = db.get_current_part_version(ticket_id)
    assert cur is not None
    assert cur["parts"] == ["继电器DC12V", "干簧管"]
    # parts 列以顿号连接存储（spec §4）
    row = db._conn.execute(
        "SELECT parts FROM part_versions WHERE ticket_id=?", (ticket_id,)).fetchone()
    assert row["parts"] == "继电器DC12V、干簧管"


def test_cross_store_part_not_matched(db, monkeypatch):
    # 商场15店工单注入的是商场15店池（POOL）；商场02店的"阻尼铰链"不在池内 → 不记
    ticket_id = _ticket(db, subject="特工", store="商场15店")
    ex = _executor(db, monkeypatch)
    r = ex.execute(_part_command("m1", {"repair_method": "换", "parts": ["阻尼铰链"]}, ticket_id))
    assert r.status == RESULT_OK
    assert db.list_part_versions(ticket_id) == []


def test_export_md_shows_current_parts_below_repair_method(db, monkeypatch):
    ticket_id = _ticket(db)
    ex = _executor(db, monkeypatch)
    ex.execute(_part_command("m1", {"repair_method": "更换继电器", "parts": ["继电器DC12V"]}, ticket_id))
    from scripts.export_tickets_md import render_ticket
    t = db.get_ticket(ticket_id)
    md = render_ticket(t, db._conn)
    assert "## 本次使用备件" in md
    assert "备件：继电器DC12V" in md
    # 位于「当前维修方式 / 维修方式历史」之后
    assert md.index("## 维修方式历史") < md.index("## 本次使用备件")


def test_raw_text_and_engineer_recorded(db, monkeypatch):
    # raw_text 来自消息 content；engineer_id 来自 command.actor_id
    ticket_id = _ticket(db)
    ex = _executor(db, monkeypatch)
    from models import NormalizedMessage
    msg = NormalizedMessage(
        message_id="m1", group_id="G1", sender_id="u1", sender_name="张工",
        content="更换继电器", sender_role="ENGINEER",
    )
    cmd = ValidatedCommand(
        message_id="m1", group_id="G1", actor_id="工号A", actor_role="ENGINEER",
        intent="ticket.repair_plan.submit", target_ticket_id=ticket_id,
        expected_ticket_version=1, fields={"repair_method": "更换继电器", "parts": ["继电器DC12V"]},
        source="SEMANTIC_MODEL",
    )
    ex.execute(cmd, message=msg)
    cur = db.get_current_part_version(ticket_id)
    assert cur is not None
    assert cur["raw_text"] == "更换继电器"
    assert cur["engineer_id"] == "工号A"
    assert cur["parts"] == ["继电器DC12V"]
