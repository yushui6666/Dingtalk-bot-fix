"""按 docs/测试用例.md 跑 4 套场景（每套独立新 DB，真实模型 + 关键词快路径）。

说明：测试消息需要「店长/工程师」身份，无法用工程部AI账号代发（系统过滤自己
账号），故在进程内用真实 pipeline 模拟各角色发送，验证每套场景的预期行为。
每套场景独立数据库，互不干扰。

用法::

    python scripts/run_testcases.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from db import Database  # noqa: E402
from models import NormalizedMessage  # noqa: E402
from notifier import Notifier  # noqa: E402
from pipeline import MessageProcessingPipeline, RuntimeMode  # noqa: E402
from routing.pending_actions import PendingActionService  # noqa: E402
from routing.ticket_contexts import TicketContextStore  # noqa: E402
from routing.ticket_router import TicketRouter  # noqa: E402
from semantics.evaluator import _load_env_file  # noqa: E402
from semantics.protocol_loader import load_protocol  # noqa: E402
from tickets.executor import TicketCommandExecutor  # noqa: E402
from tickets.repository import TicketRepository  # noqa: E402

GROUP = {"group_id": "G1", "store_name": "测试群（示例）",
         "manager_ids": ["uid-mgr"], "engineer_ids": ["uid-eng"], "other_member_ids": []}
MG = ("uid-mgr", "MANAGER", "店长")
EG = ("uid-eng", "ENGINEER", "工程师")
O1, O2 = "9990000000000000001", "9990000000000000002"


class Scenario:
    """一套独立场景：独立临时 DB + 真实 pipeline。"""

    def __init__(self, title: str) -> None:
        self.title = title
        _load_env_file(_PROJECT_ROOT / ".env")
        tmp = tempfile.mkdtemp()
        self.db = Database(Path(tmp) / "s.db")
        self.db.init_schema()
        self.db.upsert_group(GROUP)
        protocol = load_protocol(_PROJECT_ROOT / "protocols" / "ticket_semantics.v4.json")
        repo = TicketRepository(self.db)
        router = TicketRouter()
        context = TicketContextStore(self.db)
        pending = PendingActionService(self.db)
        executor = TicketCommandExecutor(self.db, repo)
        self.replies: list[str] = []
        notifier = Notifier(self.db, lambda t, x: self.replies.append(x.replace("\n", " ⏎ ")))
        from semantics.classifier import SemanticClassifier
        from semantics.model_client import OpenAICompatibleModelClient

        self.classifier = SemanticClassifier(
            client=OpenAICompatibleModelClient(), protocol=protocol)
        self.pipeline = MessageProcessingPipeline(
            db=self.db, repo=repo, protocol=protocol, router=router, context=context,
            pending=pending, executor=executor, notifier=notifier,
            classifier=self.classifier, mode=RuntimeMode.PRODUCTION,
        )
        self.results: list[tuple[str, bool, str]] = []
        print(f"\n===== {title} =====")
        print(f"模型: {self.classifier._client.model}")

    def send(self, message_id: str, text: str, actor, tag: str) -> str:
        sender_id, role, name = actor
        msg = NormalizedMessage(
            message_id=message_id, group_id="G1", sender_id=sender_id, sender_name=name,
            content=text, message_type="text", sent_at=datetime.now(), sender_role=role,
        )
        self.db.enqueue_message(msg)
        row = self.db.connect().execute(
            "SELECT * FROM inbox_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        self.replies.clear()
        asyncio.run(self.pipeline.process(dict(row)))
        result = self.db.connect().execute(
            "SELECT processed_result FROM inbox_messages WHERE message_id=?", (message_id,)
        ).fetchone()["processed_result"]
        print(f"  [{result}] {name}：{text[:36]}")
        for r in self.replies[:3]:
            print(f"      ⮑ {r[:78]}")
        return result

    def check(self, tag: str, ok: bool, detail: str = "") -> None:
        self.results.append((tag, ok, detail))
        print(f"  {'✅' if ok else '❌'} {tag} {detail}")

    def close(self) -> None:
        self.db.close()

    def report(self) -> None:
        passed = sum(1 for _, ok, _ in self.results if ok)
        print(f"  → 通过 {passed}/{len(self.results)}")
        return passed


def scenario1() -> None:
    """关键词完整主链路。"""
    s = Scenario("场景 1 · 关键词完整主链路")
    s.send("r1", "#报修\n主题：博物馆奇妙夜\n位置：一楼大厅消防门\n问题描述：门下沉明显，开门剐蹭\n时效：3天", MG, "报修")
    t1 = s.db.list_active_tickets("G1")[0]["ticket_no"]
    s.check("S1 建单", "ACTIVE" in s.db.get_ticket_by_no(t1)["status"], f"→ {t1}")
    s.send("r2", "#故障判断\n故障判断：门体明显下沉\n故障判断：上侧合页松动", EG, "诊断")
    diag = s.db.connect().execute("SELECT items_json FROM diagnosis_versions WHERE is_current=1").fetchone()
    s.check("S1 诊断", diag and "合页" in diag["items_json"])
    s.send("r3", f"#维修方式\n维修方式：淘宝采购后自行维修\n订单号：{O1}", EG, "维修方式+订单")
    s.check("S1 订单1登记", s.db.get_order_monitor(O1) is not None)
    s.send("r4", f"单号 {O2}", MG, "裸单号")
    s.check("S1 订单2登记", s.db.get_order_monitor(O2) is not None)
    s.send("r5", "门修好了，试了下正常，能用", MG, "完工")
    s.check("S1 完工", s.db.get_ticket_by_no(t1)["status"] == "COMPLETED")
    s.report(); s.close()


def scenario2() -> None:
    """自然语言 + 多工单 + 查询 + 选单。"""
    s = Scenario("场景 2 · 自然语言 + 多工单 + 选单")
    s.send("r1", "收银机死机了，屏幕不亮，位置在前台，麻烦3天内修", MG, "自然语言建单")
    # 第二张用关键词建单（自然语言第二张会因 §6.5 新建/补充冲突走澄清，属已知行为）
    s.send("r2", "#报修\n主题：二楼门锁\n位置：二楼仓库\n问题描述：锁芯卡死打不开\n时效：3天", MG, "第二张建单")
    act = s.db.list_active_tickets("G1")
    s.check("S2 多工单并存", len(act) == 2, f"→ {[x['ticket_no'] for x in act]}")
    s.send("r3", "#查询工单", MG, "查询")
    s.check("S2 查询列出", any("当前活动工单" in r for r in s.replies))
    cash = next((t for t in act if "收银机" in t["ticket_no"]), act[0])
    s.send("r4", f"#选择工单 {cash['ticket_no']}", MG, "选工单")
    s.send("r5", f"单号 {O1}", MG, "裸单号")
    mon = s.db.get_order_monitor(O1)
    s.check("S2 订单归到收银机", mon is not None and mon["ticket_no"] == cash["ticket_no"])
    s.report(); s.close()


def scenario3() -> None:
    """诊断 + 两订单混合 + 口语完工。"""
    s = Scenario("场景 3 · 诊断 + 两订单混合 + 口语完工")
    s.send("r1", "博物馆奇妙夜，第一个房间的消防门下沉明显，开门时剐蹭，麻烦3天内修好", MG, "自然语言建单")
    t3 = s.db.list_active_tickets("G1")[0]["ticket_no"]
    s.send("r2", f"估计是铰链坏了，采购了2个，单号是{O1}和{O2}", EG, "诊断+两订单")
    s.check("S3 两订单都登记",
            s.db.get_order_monitor(O1) is not None and s.db.get_order_monitor(O2) is not None)
    diag3 = s.db.connect().execute(
        "SELECT items_json FROM diagnosis_versions WHERE is_current=1 ORDER BY id DESC LIMIT 1").fetchone()
    s.check("S3 诊断记录", diag3 and "铰链" in diag3["items_json"])
    s.send("r3", "两个合页都换上了，门现在开关顺畅，修好了", EG, "口语完工")
    s.check("S3 完工", s.db.get_ticket_by_no(t3)["status"] == "COMPLETED")
    s.report(); s.close()


def scenario4() -> None:
    """取消确认 / 缺省时效 / 越权 / 歧义。"""
    s = Scenario("场景 4 · 取消确认 / 缺省时效 / 越权 / 歧义")
    s.send("r1", "#报修\n主题：仓库卷帘门\n位置：仓库入口\n问题描述：拉不动", MG, "建单(无时效)")
    t4 = s.db.list_active_tickets("G1")[0]
    s.check("S4 缺省时效1天", t4["sla_days"] == 1)
    s.send("r2", "把仓库卷帘门那张取消了吧，是误报", MG, "取消")
    s.send("r3", "确认", MG, "确认")
    s.check("S4 取消成功", s.db.get_ticket(t4["id"])["status"] == "CANCELLED")
    s.send("r4", "#报修\n主题：办公室风扇\n位置：办公室\n问题描述：不转", EG, "工程师报修")
    eng = s.db.connect().execute(
        "SELECT processed_result FROM inbox_messages WHERE message_id='r4'").fetchone()
    s.check("S4 工程师报修被拒", eng["processed_result"] == "REJECTED")
    s.send("r5", "#报修\n主题：办公室风扇\n位置：办公室\n问题描述：不转\n#完毕", MG, "双关键词歧义")
    s.check("S4 歧义澄清", any("有歧义" in r for r in s.replies))
    s.report(); s.close()


def main() -> None:
    scenario1()
    scenario2()
    scenario3()
    scenario4()


if __name__ == "__main__":
    main()
