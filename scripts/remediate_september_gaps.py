"""9 月核查缺口善后（2026-09-03）。

生产实证（dingtalk_script/data/tickets.db）：
- #67（商场24-零号特工-003）滞留 PENDING_CONFIRM：店长「003确认修好」
  被误判为 ticket.complete 而未关闭；同根因另有 #66（商场21CC-009）、
  #69（商场21CC-010）滞留（「009修好了」误关 -011，「010修好了」被拒）。
- #73（大悦城-博物馆-004）、#76（大悦城-勇者斯巴达-007）维修方式版本数为 0：
  工程师原文即维修动作（「004更换吸铁石」「007需要重新绑绳子…」）被拒绝。
- order_monitor 脏行：「011」「淘宝采购吸铁石」（模型把短编号/中文短语
  当订单号，已同步共享表，需本地删除；共享表残留行另行清理）。

做法：全部走 TicketCommandExecutor（与线上执行语义完全一致：
版本号 CAS、责任方切换、特殊情况恢复、完成通知进 Outbox），可重入。
默认 dry-run 只打印计划；--execute 先备份 DB 再落库。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import Database  # noqa: E402
from models import NormalizedMessage  # noqa: E402
from semantics.types import ValidatedCommand  # noqa: E402
from tickets.repository import TicketRepository  # noqa: E402
from tickets.executor import TicketCommandExecutor, RESULT_OK  # noqa: E402

DB_PATH = ROOT / "data" / "tickets.db"

# （工单 id，店长确认消息）：消息发送者须为店长，工单须为 PENDING_CONFIRM
CONFIRMS: list[tuple[int, str]] = [
    (67, "msgt0bLRdHV4ik8I5IlqxVvWQ=="),  # 商场24 003确认修好
    (66, "msgLSJ7fPYuB2Tt+sFn1Gbk6A=="),  # 商场21CC 009修好了
    (69, "msgpNXOrgq+bGHZg8Aib3Jnrw=="),  # 商场21CC 010修好了
]

# （工单 id，工程师维修消息）：原文剥离开头编号即维修方式
REPAIRS: list[tuple[int, str]] = [
    (73, "msgnGwnDRavqY+SwkZf2ouB/Q=="),  # 004更换吸铁石
    (76, "msg7O9b/fNARwFznMkDK0LC7w=="),  # 007需要重新绑绳子，多的话需要木工
]

GARBAGE_ORDERS = ["011", "淘宝采购吸铁石"]

_PREFIX_RE = re.compile(r"^\s*(?:工单|#|第)?\s*\d{1,4}(?![\dA-Za-z-])\s*[:：，,、\s]*")


def _to_message(row: dict) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=row["message_id"],
        group_id=row["group_id"],
        sender_id=row["sender_id"],
        sender_name=row["sender_id"],
        content=row["content"],
        message_type=row.get("message_type", "text"),
        sent_at=datetime.strptime(row["sent_at"], "%Y-%m-%d %H:%M:%S"),
        sender_role=row["sender_role"],
        reply_to_message_id=row.get("reply_to_message_id"),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="落库（默认 dry-run）")
    args = ap.parse_args()

    db = Database(str(DB_PATH))
    repo = TicketRepository(db)
    executor = TicketCommandExecutor(db, repo)

    inbox = {r["message_id"]: r for r in
             (dict(x) for x in db.connect().execute("SELECT * FROM inbox_messages").fetchall())}

    plan: list[str] = []
    errors: list[str] = []

    # ── 1. 确认关闭滞留单 ──
    confirm_cmds: list[tuple[ValidatedCommand, NormalizedMessage, int]] = []
    for tid, mid in CONFIRMS:
        t = db.get_ticket(tid)
        m = inbox.get(mid)
        if t is None:
            errors.append(f"工单 {tid} 不存在，跳过")
            continue
        if t["status"] != "PENDING_CONFIRM":
            plan.append(f"SKIP 关闭 #{tid}：状态已是 {t['status']}（无需处理）")
            continue
        if m is None or m["sender_role"] != "MANAGER":
            errors.append(f"工单 #{tid}：确认消息 {mid} 缺失或非店长发出，跳过")
            continue
        msg = _to_message(m)
        cmd = ValidatedCommand(
            message_id=mid, group_id=m["group_id"], actor_id=m["sender_id"],
            actor_role="MANAGER", intent="ticket.confirm_complete",
            target_ticket_id=tid, expected_ticket_version=None,
            fields={}, source="model",
        )
        confirm_cmds.append((cmd, msg, tid))
        plan.append(f"CLOSE #{tid} {t['ticket_no']} ← 店长「{m['content']}」({mid[:12]}…)")

    # ── 2. 补维修方式 ──
    repair_cmds: list[tuple[ValidatedCommand, NormalizedMessage, int]] = []
    for tid, mid in REPAIRS:
        t = db.get_ticket(tid)
        m = inbox.get(mid)
        if t is None:
            errors.append(f"工单 {tid} 不存在，跳过")
            continue
        existing = db.connect().execute(
            "SELECT COUNT(*) FROM repair_method_versions WHERE ticket_id=? AND is_current=1",
            (tid,),
        ).fetchone()[0]
        if existing:
            plan.append(f"SKIP 补维修 #{tid}：已有 {existing} 个现行版本")
            continue
        if m is None:
            errors.append(f"工单 #{tid}：消息 {mid} 缺失，跳过")
            continue
        method = _PREFIX_RE.sub("", m["content"] or "").strip()[:500]
        if len(method) < 2:
            errors.append(f"工单 #{tid}：消息无法提炼维修方式，跳过")
            continue
        msg = _to_message(m)
        cmd = ValidatedCommand(
            message_id=mid, group_id=m["group_id"], actor_id=m["sender_id"],
            actor_role=m["sender_role"], intent="ticket.repair_plan.submit",
            target_ticket_id=tid, expected_ticket_version=None,
            fields={"repair_method": method}, source="model",
        )
        repair_cmds.append((cmd, msg, tid))
        plan.append(f"REPAIR #{tid} {t['ticket_no']} ←「{method}」({mid[:12]}…)")

    # ── 3. 脏订单行 ──
    garbage_found: list[str] = []
    for oid in GARBAGE_ORDERS:
        row = db.get_order_monitor(oid)
        if row is not None:
            garbage_found.append(oid)
            plan.append(f"DELETE 脏订单 {oid}（ticket={row.get('ticket_no')}）")
        else:
            plan.append(f"SKIP 脏订单 {oid}：已不存在")

    print("\n".join(plan))
    if errors:
        print("\n".join(f"ERROR {e}" for e in errors))

    if not args.execute:
        print(f"\n[dry-run] 共 {len(confirm_cmds) + len(repair_cmds) + len(garbage_found)} 项待执行，加 --execute 落库")
        db.close()
        return 0 if not errors else 1

    # ── 落库：先备份 ──
    backup = DB_PATH.with_name(f"tickets.db.bak_20260903_remediate_{datetime.now():%H%M%S}")
    shutil.copy2(DB_PATH, backup)
    print(f"\n[backup] {backup}")

    # 脏订单行直接删除（备查：备份库保留）
    for oid in garbage_found:
        db.connect().execute("DELETE FROM order_monitor WHERE order_id=?", (oid,))
        db.connect().commit()
        print(f"[done] DELETE 脏订单 {oid}")

    for cmd, msg, tid in repair_cmds + confirm_cmds:
        # 幂等：同一消息已 APPLIED 则跳过
        if db.execution_status(f"direct:{cmd.message_id}") == "APPLIED":
            print(f"[skip] {cmd.message_id[:12]}… 已执行过")
            continue
        result = executor.execute(cmd, message=msg)
        print(f"[{'done' if result.status == RESULT_OK else 'FAIL'}] {cmd.intent} #{tid} → {result.status}")
        if result.status != RESULT_OK:
            errors.append(f"{cmd.intent} #{tid} 执行返回 {result.status}")

    db.close()
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
