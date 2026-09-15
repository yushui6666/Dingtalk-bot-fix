"""静默补单收尾：去重 + 手动补充。

背景：系统停机期间（2026-08-24 13:50 之后）群消息补录完成，但：
1. 部分消息用户已手动建单/处理（商场20 #报修→工单51、商场21CC订单/采购→已登记）→ 去重；
2. 少数自然语言补充消息因云端模型瞬时异常未落地 → 用系统 executor 按协议意图手动执行（静默，不外发）。

本脚本只写本地数据库（tickets.db），不调用任何 dws 发送命令。

用法::

    python scripts/backfill_supplement.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

for env_path in (_PROJECT_ROOT / ".env",):
    if not env_path.exists():
        continue
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)

from db import Database  # noqa: E402
from logger import get_logger  # noqa: E402
from pipeline import _row_to_message  # noqa: E402
from semantics.types import ValidatedCommand  # noqa: E402
from tickets.executor import TicketCommandExecutor, RESULT_OK  # noqa: E402
from tickets.repository import TicketRepository  # noqa: E402

logger = get_logger(__name__)

# ───────────────────────── 手动补充动作（model 不可靠时的确定性兜底） ─────────────────────────
SUPPLEMENT_ACTIONS = [
    {
        # 商场21CC-财阀继承人-007：工程师诊断（工程师A）
        "message_id": "msgiM4D6AUUT/RGpjKmG1ObIA==",
        "intent": "ticket.diagnosis.submit",
        "ticket_id": 54,
        "fields": {"diagnosis_items": ["重新换了一块，还是不行，应该是线断了。后续上门查看"]},
    },
    {
        # 北京商场14大悦城-001：工程师决定找供应商亿兆维修（变压器）
        "message_id": "msgPxIhIjzpkMVb/3nLpvlzpg==",
        "intent": "ticket.repair_plan.submit",
        "ticket_id": 49,
        "fields": {"repair_method": "需要供应商维修"},
    },
    {
        # 北京商场18龙湖店-投影仪-001：店长补充维修决策说明（投影机+伸缩杆采购）
        "message_id": "msgOSyo5Tk31VMp03pDE2bbow==",
        "intent": "ticket.add_detail",
        "ticket_id": 48,
        "fields": {"content": ""},  # 内容取消息原文（message.content）
    },
]

# ───────────────────────── 去重：用户已手动处理，标记跳过 ─────────────────────────
DUPLICATE_MESSAGES = [
    "msg8rrsXM4xy0yd+RdEeCHL8g==",   # 商场21CC 裸订单号 → 已登记到工单41（msg_wang_order_*）
    "msgQkm9bHtGOwNmSLYDBiE1Eg==",   # 商场21CC 003采购 → 已处理（msg_wang_003caigou）
    "msg/lUMks19YERH0hl5yqIoyA==",   # 商场20 故障判断 → 已 DIAG 到工单51（msg_tianhong_diag_*）
]

DUP_TICKET_ID = 52          # 商场20-越狱-002（补录重复建单）→ 取消并标记 duplicate_of 51
DUP_ORIGIN_ID = 51          # 商场20-越狱-001（用户手动已建）
DUP_CREATE_MSG = "msg87Lm5nbYLVgBNNZrYV7MMQ=="  # 商场20 #报修 官方消息（链接改挂到 51）


def _mark_duplicate_messages(db: Database) -> None:
    conn = db.connect()
    for mid in DUPLICATE_MESSAGES:
        row = db.get_inbox_message(mid)
        if row and row["status"] != "COMPLETED":
            conn.execute(
                "UPDATE inbox_messages SET status='COMPLETED', processed_result='DUPLICATE_MANUAL'"
                " WHERE message_id=?", (mid,),
            )
            print(f"[去重] {mid} → DUPLICATE_MANUAL")
        else:
            print(f"[去重] {mid} 已 COMPLETED，跳过")


def _cancel_dup_ticket(db: Database, executor: TicketCommandExecutor) -> None:
    ticket = db.get_ticket(DUP_TICKET_ID)
    if ticket is None:
        print(f"[去重] 工单 {DUP_TICKET_ID} 不存在，跳过")
        return
    if ticket["status"] == "CANCELLED":
        print(f"[去重] 工单 {DUP_TICKET_ID} 已 CANCELLED，跳过")
        return
    cmd = ValidatedCommand(
        message_id="sys:backfill:dedup:cancel",
        group_id=ticket["group_id"],
        actor_id="system-backfill",
        actor_role="SYSTEM",
        intent="ticket.cancel",
        target_ticket_id=DUP_TICKET_ID,
        expected_ticket_version=ticket["version"],
        fields={"cancel_reason": f"与工单{DUP_ORIGIN_ID}重复（用户已手动建单）"},
        source="manual",
    )
    result = executor.execute(cmd, message=None)
    print(f"[去重] 取消工单 {DUP_TICKET_ID}（{ticket['ticket_no']}）→ {result.status}")
    if result.status == RESULT_OK:
        conn = db.connect()
        conn.execute(
            "UPDATE tickets SET duplicate_of_ticket_id=? WHERE id=?",
            (DUP_ORIGIN_ID, DUP_TICKET_ID),
        )
        conn.commit()
        print(f"[去重] 标记 duplicate_of_ticket_id={DUP_ORIGIN_ID}")
    # 原 #报修 官方消息的链接改挂到正确工单
    link = db.connect().execute(
        "SELECT 1 FROM message_ticket_links WHERE message_id=? AND ticket_id=?",
        (DUP_CREATE_MSG, DUP_TICKET_ID),
    ).fetchone()
    if link:
        db.connect().execute(
            "UPDATE message_ticket_links SET ticket_id=? WHERE message_id=?",
            (DUP_ORIGIN_ID, DUP_CREATE_MSG),
        )
        db.connect().commit()
        print(f"[去重] {DUP_CREATE_MSG} 链接改挂 工单{DUP_TICKET_ID} → 工单{DUP_ORIGIN_ID}")


def _run_supplements(db: Database, executor: TicketCommandExecutor) -> None:
    for action in SUPPLEMENT_ACTIONS:
        mid = action["message_id"]
        row = db.get_inbox_message(mid)
        if row is None:
            print(f"[补充] {mid} 不在收件箱，跳过")
            continue
        if row["status"] == "COMPLETED" and row.get("processed_result") == "EXECUTED":
            print(f"[补充] {mid} 已 EXECUTED，跳过")
            continue
        msg = _row_to_message(row)
        ticket = db.get_ticket(action["ticket_id"])
        if ticket is None:
            print(f"[补充] {mid} 目标工单 {action['ticket_id']} 不存在，跳过")
            continue
        cmd = ValidatedCommand(
            message_id=mid,
            group_id=row["group_id"],
            actor_id=row["sender_id"],
            actor_role=row["sender_role"],
            intent=action["intent"],
            target_ticket_id=action["ticket_id"],
            expected_ticket_version=ticket["version"],
            fields=dict(action["fields"]),
            source="manual",
        )
        result = executor.execute(cmd, message=msg)
        new_status = "EXECUTED" if result.status == RESULT_OK else result.status
        db.connect().execute(
            "UPDATE inbox_messages SET status='COMPLETED', processed_result=?,"
            " last_error=NULL WHERE message_id=?",
            (new_status, mid),
        )
        db.connect().commit()
        print(
            f"[补充] {mid} intent={action['intent']} ticket={ticket['ticket_no']} "
            f"→ {result.status}（收件箱 {new_status}）"
        )


def main() -> None:
    db = Database()
    db.init_schema()
    repo = TicketRepository(db)
    executor = TicketCommandExecutor(db, repo)

    print("── 去重阶段 ──")
    _mark_duplicate_messages(db)
    _cancel_dup_ticket(db, executor)

    print("\n── 补充阶段 ──")
    _run_supplements(db, executor)

    db.close()
    print("\n完成（全程静默：未向任何群/用户发送消息）")


if __name__ == "__main__":
    main()
