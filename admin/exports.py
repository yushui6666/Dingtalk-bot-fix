"""管理后台 Excel 导出构建器（只读，不写任何数据）。

提供两类导出，均返回 ``(文件字节, 建议文件名)``：

- :func:`build_sla_workbook` —— 工单 SLA 状态。口径 A：直接取 ``tickets.status``，
  与群内时效提醒同源；不按截止时间实时推算（那会把「待商榷」误判为超时）。
- :func:`build_engineer_performance_workbook` —— 工程师绩效。按**实际提交人**
  （``diagnosis_versions`` / ``repair_method_versions`` 的 engineer_id）归属，
  而非门店群配置的 ``groups.engineer_ids``（那是群名单，一店两人会串账）。

绩效归属的关键事实（2026-08 实测，见「问题台账」sheet）：

- 版本表里的 ``engineer_id`` 是 **openDingtalkId**（34 位），与 ``groups.engineer_ids``
  的 userId（17 位）**不是同一套 ID 体系**，必须经 ``scripts/export_tickets_md.NAME_MAP``
  （门店 CSV 69 条 + 内置兜底）解析，否则认不出人。
- ``order_monitor`` **没有登记人字段**，所以订单侧无法回捞归属。
- ``responsibility_cycles.claimed_at`` **从未被写过**（106 条非空 0 条），响应时长要靠
  ``closed_by_message_id`` join ``messages.sent_at`` 反算。
- 库是活库，同一月份的绩效数字会随操作变动，故表头带快照时间。
"""
from __future__ import annotations

import io
import json
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# ── 口径常量 ────────────────────────────────────────────────────────────
SLA_STATUS_LABELS = {
    "ACTIVE": "进行中（未超时）",
    "ACTIVE_OVERDUE": "已超时",
    "PENDING_CONFIRM": "待确认",
    "PENDING_NEGOTIATION": "时效暂停（待商榷）",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
    "STOPPED": "已停修",
}
SLA_STATUS_ORDER = [
    "ACTIVE_OVERDUE", "ACTIVE", "PENDING_CONFIRM",
    "PENDING_NEGOTIATION", "STOPPED", "COMPLETED", "CANCELLED",
]
SLA_STATUS_FILLS = {
    "ACTIVE_OVERDUE": "F8CBAD",
    "PENDING_NEGOTIATION": "E4DFEC",
    "COMPLETED": "E2EFDA",
    "CANCELLED": "D9D9D9",
    "STOPPED": "F2DCDB",
}
TEST_STORE_PREFIXES = ("测试群（示例）", "测试群")

# 被视为「提交」的意图
SUBMIT_INTENTS = ("ticket.diagnosis.submit", "ticket.repair_plan.submit")
# 非人员的 engineer_id 标记
SYSTEM_ENGINEER_MARKS = ("admin-manual", "system-backfill")

# 无法归属的成因标记（绩效「工单明细」与「问题台账」共用）
UNATTR_MARKS = {
    "A": ("🟡 A 只报订单号/字段为空",
          "提交已执行，但 fields 无维修方式/诊断内容 → 按设计不落版本表；可经 action_executions 回捞提交人"),
    "B": ("🔴 B 内容有却未落库",
          "动作 APPLIED 且 fields 有实质内容，版本表却零行 → 系统缺陷，需修"),
    "C": ("🟠 C 判为提交但动作未执行",
          "意图已判对，却无对应执行记录（悬空，或被误判为建单）"),
    "D": ("🟠 D 只说自然语言",
          "消息被判为 ticket.complete 等 → 不产生版本记录"),
    "E": ("⚪ E 无可归属发言",
          "工程师无被记录的提交（含 0 条消息的孤立单）"),
    "SYS": ("🔵 SYS 仅后台人工记录",
            "记录 engineer_id 为 admin-manual/system-backfill，非群内提交"),
}
MARK_FILLS = {
    "A": "FFF2CC", "B": "FFC7CE", "C": "FCE4D6",
    "D": "FCE4D6", "E": "F2F2F2", "SYS": "DDEBF7",
}


def last_month_str(today: Optional[Any] = None) -> str:
    """上一个自然月，YYYY-MM。today 可注入便于测试。"""
    d = today or datetime.now().date()
    prev_month_end = d.replace(day=1) - timedelta(days=1)
    return f"{prev_month_end.year}-{prev_month_end.month:02d}"


def _readonly_conn(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _ts(value: Optional[str]) -> Optional[datetime]:
    """宽容解析库里的时间戳（含 'YYYY-MM-DD HH:MM:SS' 与 ISO 'T' 形式）。"""
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 2].strip(), fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(str(value).replace("T", " "))
    except Exception:
        return None


def _name_resolver():
    """返回 (解析函数, 是否已知姓名)。

    优先复用 ``scripts/export_tickets_md.NAME_MAP``（门店 CSV + 内置兜底）。
    该模块不可用时退化为「原样显示 ID」，绝不因姓名解析失败而中断导出。
    """
    try:
        import sys
        base_dir = Path(__file__).resolve().parent.parent
        if str(base_dir) not in sys.path:
            sys.path.insert(0, str(base_dir))
        from scripts.export_tickets_md import NAME_MAP

        def resolve(oid: Optional[str]) -> Optional[str]:
            if not oid:
                return None
            return NAME_MAP.get(oid)

        return resolve
    except Exception:
        def resolve(oid: Optional[str]) -> Optional[str]:
            return str(oid) if oid else None

        return resolve


# ══════════════════════════════════════════════════════════════════════
#  工单 SLA 状态导出
# ══════════════════════════════════════════════════════════════════════
def build_sla_workbook(db_path: Path, month: Optional[str] = None) -> tuple[bytes, str]:
    """生成「工单 SLA 状态」xlsx。month 缺省=上一个自然月，按报修时间归属。"""
    import re

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    month = (month or last_month_str()).strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month 需为 YYYY-MM 格式")

    # 列结构与线上既有版本保持一致（2026-09-14 上线），不得擅自增删列。
    # 已知命名瑕疵：「延期次数」实际数的是 timeout_cycles 行数（含 WAITING_REASON
    # 待补理由），只有 status='EXTENDED' 才是真正批准的延期。改名需用户批准，
    # 故此处仅在说明行里点明，不改列名。
    columns = [
        ("工单号", 38), ("门店", 26), ("主题", 20), ("时效(天)", 9),
        ("报修时间", 17), ("时效截止", 17), ("SLA状态", 18), ("完成情况", 10),
        ("延期次数", 9), ("工程师", 16), ("完成时间", 17),
    ]

    conn = _readonly_conn(db_path)
    try:
        not_test = " AND ".join(f"store_name NOT LIKE '{p}%'" for p in TEST_STORE_PREFIXES)
        order_case = "CASE status " + " ".join(
            f"WHEN '{s}' THEN {i}" for i, s in enumerate(SLA_STATUS_ORDER)
        ) + f" ELSE {len(SLA_STATUS_ORDER)} END"
        rows = conn.execute(
            f"""SELECT id, ticket_no, store_name, subject, sla_days, status,
                       created_at, current_deadline_at, closed_at, group_id
                FROM tickets
                WHERE substr(created_at, 1, 7) = ? AND {not_test}
                ORDER BY {order_case}, created_at""",
            (month,),
        ).fetchall()

        def engineers(group_id: Optional[str]) -> str:
            if not group_id:
                return ""
            r = conn.execute("SELECT engineer_ids FROM groups WHERE group_id=?", (group_id,)).fetchone()
            if not r or not r[0]:
                return ""
            try:
                ids = json.loads(r[0])
            except Exception:
                return ""
            resolve = _name_resolver()
            return "、".join(resolve(i) or i for i in ids)

        def cycles(ticket_id: int) -> tuple[int, int, int]:
            c = conn.execute(
                "SELECT COUNT(*) t,"
                " SUM(CASE WHEN status='EXTENDED' THEN 1 ELSE 0 END) e,"
                " SUM(CASE WHEN status='WAITING_REASON' THEN 1 ELSE 0 END) w"
                " FROM timeout_cycles WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            return (c[0] or 0, c[1] or 0, c[2] or 0)

        data, counts = [], {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            tot, _ext, _wait = cycles(r["id"])
            if r["status"] == "COMPLETED":
                cl, dl = _ts(r["closed_at"]), _ts(r["current_deadline_at"])
                comp = "按时" if (cl and dl and cl <= dl) else ("超期" if cl and dl else "无法判断")
            else:
                comp = ""
            data.append([
                r["ticket_no"], r["store_name"], r["subject"],
                r["sla_days"] if r["sla_days"] else "无时效",
                (r["created_at"] or "")[:16].replace("T", " "),
                (r["current_deadline_at"] or "")[:16].replace("T", " ") or "—",
                SLA_STATUS_LABELS.get(r["status"], r["status"]), comp,
                tot, engineers(r["group_id"]),
                (r["closed_at"] or "")[:16].replace("T", " "),
            ])
    finally:
        conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "工单SLA状态"
    ncol = len(columns)
    last_col = get_column_letter(ncol)
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill(fill_type="solid", fgColor="99CCFF")

    ws.merge_cells(f"A1:{last_col}1")
    ws["A1"] = f"{month} 工单 SLA 状态"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 26

    summary = " · ".join(
        f"{SLA_STATUS_LABELS.get(s, s)} {counts[s]}"
        for s in SLA_STATUS_ORDER if s in counts
    ) or "无工单"
    ws.merge_cells(f"A2:{last_col}2")
    ws["A2"] = (
        f"共 {len(data)} 张（按报修时间归属 {month}，不含测试群）｜{summary}｜"
        "SLA状态取自工单状态列，与群内时效提醒同源；"
        "「完成情况」按 完成时间≤时效截止 判定（被延期过的单以延期后截止比较）；"
        "注：「延期次数」实际为超时周期数，含系统催补理由的次数，仅其中 status=EXTENDED 才是真正批准的延期。"
    )
    ws["A2"].font = Font(size=10, color="666666")
    ws["A2"].alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[2].height = 30

    hr = 3
    for c, (name, width) in enumerate(columns, start=1):
        cell = ws.cell(row=hr, column=c, value=name)
        cell.font = Font(bold=True, size=11)
        cell.fill = hdr_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.row_dimensions[hr].height = 22

    left_top = Alignment(horizontal="left", vertical="top", wrap_text=True)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, row in enumerate(data):
        r = hr + 1 + i
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.border = border
            cell.font = Font(size=10)
            cell.alignment = center if c in (4, 7, 8, 9, 10, 11) else left_top
        st = rows[i]["status"]
        if st in SLA_STATUS_FILLS:
            ws.cell(row=r, column=7).fill = PatternFill(fill_type="solid", fgColor=SLA_STATUS_FILLS[st])
    ws.freeze_panes = f"A{hr + 1}"

    if not data:
        ws.merge_cells(start_row=hr + 1, start_column=1, end_row=hr + 1, end_column=ncol)
        c = ws.cell(row=hr + 1, column=1, value=f"{month} 无工单")
        c.font = Font(size=10)
        c.border = border

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"工单SLA状态_{month}.xlsx"


# ══════════════════════════════════════════════════════════════════════
#  工程师绩效导出
# ══════════════════════════════════════════════════════════════════════
def _classify_unattributed(conn, ticket_id: int, version_ids: list[str]) -> tuple[str, str]:
    """判定一张认不出人的工单属哪类成因，返回 (标记, 说明)。"""
    if any(x in SYSTEM_ENGINEER_MARKS for x in version_ids):
        return "SYS", UNATTR_MARKS["SYS"][1]

    decisions = [
        (r["intent"], r["message_id"])
        for r in conn.execute(
            """SELECT sd.intent, m.message_id FROM messages m
               JOIN semantic_decisions sd ON sd.message_id = m.message_id
               WHERE m.ticket_id = ? AND m.sender_role = 'ENGINEER'""",
            (ticket_id,),
        )
    ]
    acts, has_content = [], False
    for _intent, mid in decisions:
        for a in conn.execute(
            "SELECT intent, command_json FROM action_executions WHERE source_message_id=?", (mid,)
        ):
            if a["intent"] not in SUBMIT_INTENTS:
                continue
            acts.append(a)
            try:
                fields = json.loads(a["command_json"] or "{}").get("fields") or {}
            except Exception:
                fields = {}
            if any(fields.get(k) for k in ("diagnosis_items", "repair_method")):
                has_content = True
    submit_decided = [i for i, _ in decisions if i in SUBMIT_INTENTS]

    if acts and has_content:
        mark = "B"
    elif acts:
        mark = "A"
    elif submit_decided:
        mark = "C"
    elif decisions:
        mark = "D"
    else:
        mark = "E"
    return mark, UNATTR_MARKS[mark][1]


def build_engineer_performance_workbook(
    db_path: Path, month: Optional[str] = None
) -> tuple[bytes, str]:
    """生成「工程师绩效」xlsx（按实际提交人归属，每人一张明细 sheet）。"""
    import re

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.hyperlink import Hyperlink

    def _fmt_names(seq: list) -> str:
        """姓名去重展示；未解析出的 openDingtalkId 合并成 ?（多个记 ?×N）。"""
        resolved = list(dict.fromkeys(n for n in seq if n))
        unknown = sum(1 for n in seq if not n)
        if unknown == 1:
            resolved.append("?")
        elif unknown > 1:
            resolved.append(f"?×{unknown}")
        return "、".join(resolved) or "—"

    month = (month or last_month_str()).strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month 需为 YYYY-MM 格式")

    snapshot = datetime.now().strftime("%Y-%m-%d %H:%M")
    resolve = _name_resolver()
    conn = _readonly_conn(db_path)
    try:
        tickets = [
            dict(r) for r in conn.execute(
                "SELECT id, ticket_no, store_name, sla_days, status, created_at,"
                " closed_at, current_deadline_at, initial_deadline_at FROM tickets"
                " WHERE substr(created_at, 1, 7) = ? ORDER BY created_at",
                (month,),
            )
        ]
        rows = []
        for t in tickets:
            d_ids = [r[0] for r in conn.execute(
                "SELECT engineer_id FROM diagnosis_versions WHERE ticket_id=?", (t["id"],))]
            r_ids = [r[0] for r in conn.execute(
                "SELECT engineer_id FROM repair_method_versions WHERE ticket_id=?", (t["id"],))]
            all_ids = [x for x in d_ids + r_ids if x]

            d_names = [resolve(x) for x in d_ids if x]
            r_names = [resolve(x) for x in r_ids if x]
            people = sorted({n for n in (d_names + r_names) if n})

            mark, remark = "", ""
            if not people:
                mark, remark = _classify_unattributed(conn, t["id"], all_ids)

            cl, dl = _ts(t["closed_at"]), _ts(t["current_deadline_at"])
            if t["status"] == "COMPLETED":
                verdict = "按时" if (cl and dl and cl <= dl) else "超期"
            elif t["status"] == "ACTIVE_OVERDUE":
                verdict = "超时中"
            elif t["status"] == "CANCELLED":
                verdict = "已取消"
            else:
                verdict = "时效暂停"

            cycles = []
            for c in conn.execute(
                """SELECT rc.waiting_since, rc.due_at, m.sent_at AS closed
                   FROM responsibility_cycles rc
                   LEFT JOIN messages m ON m.message_id = rc.closed_by_message_id
                   WHERE rc.ticket_id = ? AND rc.waiting_side = 'ENGINEER_SIDE'
                   ORDER BY rc.waiting_since""",
                (t["id"],),
            ):
                ws_, du, xc = _ts(c["waiting_since"]), _ts(c["due_at"]), _ts(c["closed"])
                cycles.append({
                    "h": ((xc - ws_).total_seconds() / 3600) if (xc and ws_) else None,
                    "late": (bool(du and xc > du) if (du and xc) else None),
                })

            # 展示用：admin-manual 等非人员标记显示成「后台人工」，但不进归属
            d_disp = ["后台人工" if i in SYSTEM_ENGINEER_MARKS else resolve(i)
                      for i in d_ids if i]
            r_disp = ["后台人工" if i in SYSTEM_ENGINEER_MARKS else resolve(i)
                      for i in r_ids if i]

            parts = []
            if d_disp:
                parts.append("判断:" + _fmt_names(d_disp))
            if r_disp:
                parts.append("维修:" + _fmt_names(r_disp))

            rows.append({
                "no": t["ticket_no"], "store": t["store_name"], "sla": t["sla_days"],
                "status": SLA_STATUS_LABELS.get(t["status"], t["status"]),
                "verdict": verdict, "people": people, "mark": mark, "remark": remark,
                "basis": "；".join(parts) if parts else "—",
                "dn": {n for n in d_names if n}, "rn": {n for n in r_names if n},
                "created": (t["created_at"] or "")[:16],
                "init": (t["initial_deadline_at"] or "")[:16],
                "cur": (t["current_deadline_at"] or "")[:16],
                "closed": (t["closed_at"] or "")[:16],
                "cycles": cycles,
                "msg_count": conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE ticket_id=?", (t["id"],)).fetchone()[0],
            })

        # 按人汇总
        per: dict[str, dict] = {}
        for x in rows:
            for p in x["people"]:
                a = per.setdefault(p, dict(单=0, 按时=0, 超期=0, 超时中=0, 已完成=0,
                                           周期=0, 已响应=0, 未响应=0, 超期响应=0, 时长=[]))
                a["单"] += 1
                if x["verdict"] in ("按时", "超期"):
                    a["已完成"] += 1
                    a["按时" if x["verdict"] == "按时" else "超期"] += 1
                elif x["verdict"] == "超时中":
                    a["超时中"] += 1
                for c in x["cycles"]:
                    a["周期"] += 1
                    if c["h"] is None:
                        a["未响应"] += 1
                    else:
                        a["已响应"] += 1
                        a["时长"].append(c["h"])
                        if c["late"]:
                            a["超期响应"] += 1

        # 问题台账：按成因归组，附实际证据值
        ledger: list[tuple[str, str, str, str, str]] = []
        by_mark: dict[str, list[dict]] = {}
        for x in rows:
            if x["mark"]:
                by_mark.setdefault(x["mark"], []).append(x)
        for mark in ("B", "A", "C", "D", "E", "SYS"):
            group = by_mark.get(mark)
            if not group:
                continue
            names = " / ".join(g["no"] for g in group)
            evidence = "；".join(
                f"{g['no']}（{'、'.join(g['people']) or '无可归属人'}，{g['verdict']}，消息{g['msg_count']}条）"
                for g in group
            )
            suggestion = {
                "B": "TDD 排查写入丢失路径（唯一系统缺陷，优先）",
                "A": "可经 action_executions 提交人回捞归属；另需给订单号加格式校验",
                "C": "查悬空原因；分类器对『故障判断：』等强前缀应优先判提交",
                "D": "流程侧引导规范格式，非代码缺陷",
                "E": "归为『无法评估』不参与考核；0 消息孤立单需查消息归属",
                "SYS": "标注为人工处理，不计入工程师绩效",
            }[mark]
            ledger.append((mark, UNATTR_MARKS[mark][0], names, evidence, suggestion))

        # 附加：带强前缀却被判成别的意图的消息（未必影响归属，但同类缺陷）
        misrouted = []
        for t in tickets:
            for r in conn.execute(
                """SELECT sd.intent, substr(m.content,1,40) c FROM messages m
                   JOIN semantic_decisions sd ON sd.message_id = m.message_id
                   WHERE m.ticket_id = ? AND m.sender_role='ENGINEER'
                     AND (m.content LIKE '%故障判断%' OR m.content LIKE '%维修方式%')
                     AND sd.intent NOT IN (?,?)""",
                (t["id"], *SUBMIT_INTENTS),
            ):
                misrouted.append((t["ticket_no"], r["intent"], r["c"]))
        if misrouted:
            ledger.append((
                "X", "🟠 X 强前缀消息被误判意图",
                f"{len(misrouted)} 条",
                "；".join(f"{no}→{it}" for no, it, _ in misrouted[:6]),
                "同 C：分类器强前缀识别问题",
            ))
    finally:
        conn.close()

    # ── 写 Excel ──────────────────────────────────────────────────────
    wb = Workbook()
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill(fill_type="solid", fgColor="99CCFF")

    def write_header(ws, row, cols):
        for i, (name, width) in enumerate(cols, start=1):
            c = ws.cell(row=row, column=i, value=name)
            c.font = Font(bold=True, size=10)
            c.fill = hdr_fill
            c.border = border
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = width

    ws = wb.active
    ws.title = "按人汇总"
    ncol = 14
    ws["A1"] = f"{month} 工程师绩效（按实际提交人归属）　快照 {snapshot}"
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells(f"A1:{get_column_letter(ncol)}1")
    unattributed = [x for x in rows if x["mark"]]
    order = sorted(per.items(), key=lambda kv: (-kv[1]["已完成"], -kv[1]["已响应"], kv[0]))
    rows_by_person: dict[str, list[dict]] = {}
    for x in rows:
        for p in x["people"]:
            rows_by_person.setdefault(p, []).append(x)

    def _sheet_name(raw: Any, used: set) -> str:
        """姓名 → 合法且不重复的 sheet 名（Excel 限 31 字符，禁 : \\ / ? * [ ]）。"""
        name = re.sub(r"[\[\]:*?/\\]", "_", str(raw)).strip()[:31] or "未命名"
        base, k = name, 2
        while name in used:
            suffix = f"({k})"
            name = base[: 31 - len(suffix)] + suffix
            k += 1
        used.add(name)
        return name

    _used = {"按人汇总", "未归属工单", "问题台账"}
    sheet_of = {p: _sheet_name(p, _used) for p, _ in order}

    notes = [
        "⚠️ 勿直接用于考核。口径：按时按【延期后的当前截止】判定；工程师侧响应窗口恒 4h；按实际提交人归属。",
        "① 每人一张 sheet，该 sheet 的表就是这名工程师的相关工单（点本表姓名可跳转）；"
        "无法归属的工单见「未归属工单」，成因见「问题台账」。",
        f"② 无法归属 {len(unattributed)}/{len(rows)} 张（{len(unattributed) * 100 // max(len(rows),1)}%）"
        "已打标记，已排除在各人 sheet 之外。",
        "③ 『超期响应率』衡量的是『多久回了第一句话』——关掉计时的是『故障判断：…』回话而非修好，可被回话刷绿。",
        "④ 一单多人参与时对每人都计入，合计行可能重复计单，仅供参考。",
        "⑤ 库为活库，同一月份数字随操作变动；本表为某一时刻快照。",
        "⑥ 归属靠 diagnosis_versions/repair_method_versions 的 engineer_id（openDingtalkId），"
        "经 NAME_MAP 解析姓名；order_monitor 无登记人字段，订单侧无法回捞。",
    ]
    for i, n in enumerate(notes):
        c = ws.cell(row=2 + i, column=1, value=n)
        c.font = Font(size=9, color="CC0000" if i in (0, 2) else "666666")
        ws.merge_cells(start_row=2 + i, start_column=1, end_row=2 + i, end_column=ncol)
        ws.row_dimensions[2 + i].height = 13
    hr = 2 + len(notes) + 1
    write_header(ws, hr, [
        ("工程师", 10), ("参与单", 8), ("工单按时率", 11), ("已完成", 8), ("按时", 7), ("超期", 7),
        ("超时中", 8), ("超期响应率", 11), ("已响应周期", 10), ("准时", 7), ("超期", 7),
        ("未响应", 8), ("平均响应h", 10), ("中位响应h", 10),
    ])
    rr = hr + 1
    for p, a in order:
        r1 = f"{a['按时'] / a['已完成'] * 100:.1f}%" if a["已完成"] else "无数据"
        r2 = f"{a['超期响应'] / a['已响应'] * 100:.1f}%" if a["已响应"] else "无数据"
        vals = [p, a["单"], r1, a["已完成"], a["按时"], a["超期"], a["超时中"], r2, a["已响应"],
                a["已响应"] - a["超期响应"], a["超期响应"], a["未响应"],
                round(statistics.mean(a["时长"]), 1) if a["时长"] else "—",
                round(statistics.median(a["时长"]), 1) if a["时长"] else "—"]
        for i, v in enumerate(vals, 1):
            c = ws.cell(row=rr, column=i, value=v)
            c.border = border
            c.font = Font(size=10)
        nc = ws.cell(row=rr, column=1)
        # 站内跳转必须用 location（写成 "#Sheet!A1" 会被 openpyxl 当成外部目标，点了打不开）
        nc.hyperlink = Hyperlink(ref=nc.coordinate, location=f"'{sheet_of[p]}'!A1")
        nc.font = Font(size=10, color="0563C1", underline="single")
        rr += 1
    if per:
        g = lambda k: sum(a[k] for a in per.values())  # noqa: E731
        allh = [h for a in per.values() for h in a["时长"]]
        total = ["合计", g("单"),
                 f"{g('按时') / g('已完成') * 100:.1f}%" if g("已完成") else "—",
                 g("已完成"), g("按时"), g("超期"), g("超时中"),
                 f"{g('超期响应') / g('已响应') * 100:.1f}%" if g("已响应") else "—",
                 g("已响应"), g("已响应") - g("超期响应"), g("超期响应"), g("未响应"),
                 round(statistics.mean(allh), 1) if allh else "—",
                 round(statistics.median(allh), 1) if allh else "—"]
        for i, v in enumerate(total, 1):
            c = ws.cell(row=rr, column=i, value=v)
            c.border = border
            c.font = Font(size=10, bold=True)
    ws.freeze_panes = f"A{hr + 1}"

    # ── 每个工程师一张 sheet：该 sheet 的表就是这名工程师的相关工单 ─────
    per_cols = [
        ("工单号", 40), ("门店", 22), ("时效(天)", 7), ("工单状态", 16), ("完成判定", 10),
        ("本人角色", 10), ("同单其他人", 12), ("报修时间", 17), ("延期后截止", 17),
        ("完成时间", 17), ("工程师侧周期", 12), ("已响应", 8), ("响应判定", 9),
        ("响应明细", 30), ("归属依据", 22),
    ]
    ncol_p = len(per_cols)
    verdict_fill = {
        "按时": "E2EFDA", "超期": "F8CBAD", "超时中": "F8CBAD",
        "已取消": "D9D9D9", "时效暂停": "E4DFEC",
    }
    for p, a in order:
        wsp = wb.create_sheet(sheet_of[p])
        mine = sorted(rows_by_person[p], key=lambda x: x["created"])
        r1 = f"{a['按时'] / a['已完成'] * 100:.1f}%" if a["已完成"] else "无数据"
        r2 = f"{a['超期响应'] / a['已响应'] * 100:.1f}%" if a["已响应"] else "无数据"
        wsp["A1"] = (f"{p}　{month} 绩效明细（本表即该工程师的相关工单，共 {len(mine)} 张）"
                     f"　快照 {snapshot}")
        wsp["A1"].font = Font(bold=True, size=13)
        wsp.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol_p)

        avg = round(statistics.mean(a["时长"]), 1) if a["时长"] else None
        avg_txt = f"{avg}h" if avg is not None else "—（本月无工程师侧响应周期）"
        kpi = (f"工单按时率 {r1}（按时 {a['按时']} / 超期 {a['超期']} / 已完成 {a['已完成']}）　"
               f"超期响应率 {r2}（超期 {a['超期响应']} / 已响应周期 {a['已响应']}）　"
               f"参与 {a['单']} 单（超时中 {a['超时中']}）　未响应周期 {a['未响应']}　"
               f"平均响应 {avg_txt}")
        c = wsp.cell(row=2, column=1, value=kpi)
        c.font = Font(bold=True, size=10, color="1F4E79")
        wsp.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol_p)

        c = wsp.cell(row=3, column=1, value=(
            "口径：完成判定按【延期后的当前截止】；响应窗口恒 4h；『响应』= 关掉工程师侧计时的回话"
            "（通常是『故障判断：…』），不等于修好；周期=该单在工程师侧等待的次数。"
            "本人角色=本条归属依据里本人承担的部分。"))
        c.font = Font(size=9, color="666666")
        wsp.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncol_p)

        write_header(wsp, 5, per_cols)
        jj = 6
        for x in mine:
            cyc = x["cycles"]
            responded = [t for t in cyc if t["h"] is not None]
            late = [t for t in cyc if t["late"]]
            if p in x["dn"] and p in x["rn"]:
                role = "判断+维修"
            elif p in x["dn"]:
                role = "仅判断"
            else:
                role = "仅维修"
            others = "、".join(sorted(set(x["people"]) - {p})) or "—"
            detail = "；".join(
                (f"{t['h']:.1f}h" + ("超期" if t["late"] else "准时"))
                if t["h"] is not None else "未响应"
                for t in cyc
            ) or "—"
            vals = [
                x["no"], x["store"], x["sla"], x["status"], x["verdict"], role, others,
                x["created"], x["cur"], x["closed"], len(cyc), len(responded),
                "超期" if late else ("准时" if responded else ("未响应" if cyc else "—")),
                detail, x["basis"],
            ]
            for i, v in enumerate(vals, 1):
                cell = wsp.cell(row=jj, column=i, value=v)
                cell.border = border
                cell.font = Font(size=10)
            if x["verdict"] in verdict_fill:
                wsp.cell(row=jj, column=5).fill = PatternFill(
                    "solid", fgColor=verdict_fill[x["verdict"]])
            if late:
                wsp.cell(row=jj, column=13).font = Font(size=10, color="CC0000", bold=True)
                wsp.cell(row=jj, column=14).font = Font(size=10, color="CC0000")
            jj += 1
        if not mine:
            wsp.cell(row=6, column=1, value="（本月无工单）").font = Font(size=10)
        wsp.freeze_panes = "A6"
        wsp.auto_filter.ref = f"A5:{get_column_letter(ncol_p)}{max(jj - 1, 5)}"

    ws2 = wb.create_sheet("未归属工单")
    ws2["A1"] = f"无法归属到具体工程师的工单（共 {len(unattributed)} 张）：问题标记列标出成因"
    ws2["A1"].font = Font(bold=True, size=12)
    ws2.merge_cells("A1:N1")
    write_header(ws2, 3, [
        ("问题标记", 22), ("工单号", 40), ("门店", 20), ("时效", 6), ("工单状态", 10),
        ("完成判定", 9), ("归属处理人", 12), ("归属依据", 20), ("报修时间", 17),
        ("延期后截止", 17), ("完成时间", 17), ("工程师侧响应", 12), ("响应判定", 9),
        ("标记说明", 60),
    ])
    j = 4
    for x in unattributed:
        for c0 in (x["cycles"] or [{"h": None, "late": None}]):
            vals = [
                UNATTR_MARKS[x["mark"]][0] if x["mark"] else "", x["no"], x["store"], x["sla"],
                x["status"], x["verdict"], "、".join(x["people"]) or "（无法归属）", x["basis"],
                x["created"], x["cur"], x["closed"],
                f"{c0['h']:.1f}h" if c0["h"] is not None else ("未响应" if x["cycles"] else "—"),
                ("准时" if c0["late"] is False else ("超期" if c0["late"] else "—")),
                x["remark"],
            ]
            for i, v in enumerate(vals, 1):
                cell = ws2.cell(row=j, column=i, value=v)
                cell.border = border
                cell.font = Font(size=10)
            if x["mark"]:
                ws2.cell(row=j, column=1).fill = PatternFill("solid", fgColor=MARK_FILLS[x["mark"]])
                ws2.cell(row=j, column=7).font = Font(size=10, color="CC0000", bold=True)
            if c0["late"] is True:
                ws2.cell(row=j, column=13).font = Font(size=10, color="CC0000")
            j += 1
    ws2.freeze_panes = "A4"

    ws3 = wb.create_sheet("问题台账")
    ws3["A1"] = f"{month} 绩效归属问题台账（自动生成，快照 {snapshot}）"
    ws3["A1"].font = Font(bold=True, size=13)
    ws3.merge_cells("A1:E1")
    write_header(ws3, 3, [
        ("编号", 10), ("类别", 30), ("涉及工单", 46), ("现象与证据", 60), ("建议处理", 32),
    ])
    for i, entry in enumerate(ledger, start=4):
        for k, v in enumerate(entry, 1):
            c = ws3.cell(row=i, column=k, value=v)
            c.border = border
            c.font = Font(size=10)
            c.alignment = Alignment(vertical="top", wrap_text=True)
        ws3.row_dimensions[i].height = 32
    if not ledger:
        c = ws3.cell(row=4, column=1, value=f"{month} 未发现归属问题")
        c.font = Font(size=10)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"工程师绩效_{month}.xlsx"
