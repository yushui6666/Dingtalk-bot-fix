"""把本地工单库增量同步到 Notion「完整工单表」。

逻辑：
- 读取 data/knowledge_base/notion_import_tickets.csv（export_tickets_csv.py 的产物）
- 通过 Notion API 查询现有工单号，只创建缺失的页面（增量，不改动已有行）
- 属性对齐数据库 schema：工单号(title)/店铺(select)/状态(select)/报修人/主题/位置/
  问题描述/时效/报修时间(date)/时效结束时间(date)/工单完成时间(date)/订单号/订单最后状态/关闭人/关闭角色
- 页面正文 = CSV「页面正文」列（聊天记录/故障判断/维修方式/超时/图片解析），按块写入

用法::

    python scripts/sync_tickets_to_notion.py            # 默认增量同步
    python scripts/sync_tickets_to_notion.py --dry-run  # 只看差异不写入

凭据：~/.workbuddy/mcp.json 里 notion-ops 连接器的 OPENAPI_MCP_HEADERS。
"""
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from pathlib import Path

BASE = Path("/Users/example/Desktop/钉钉消息/dingtalk_script")
CSV_PATH = BASE / "data/knowledge_base/notion_import_tickets.csv"
DB_ID = "3c1a8f85-5ce9-8106-97a5-e5c03ff6783c"  # 完整工单表

_DATE_COLS = ("报修时间", "时效结束时间", "工单完成时间")
_TEXT_COLS = ("报修人", "主题", "位置", "问题描述", "时效", "订单号", "订单最后状态", "关闭人", "关闭角色")


def _headers() -> dict:
    cfg = json.loads((Path.home() / ".workbuddy/mcp.json").read_text())
    return json.loads(cfg["mcpServers"]["notion-ops"]["env"]["OPENAPI_MCP_HEADERS"])


_H = _headers()

# ── 传输层：走 WorkBuddy MCP 连接器代理（本机透明代理会拦截直连 api.notion.com 的建页请求）──
import os

_SESSION = {"id": None}
_seq = {"n": 10}


def _cfg() -> dict:
    raw = os.environ.get("CODEBUDDY_MCP_CONFIG")
    if raw:
        return json.loads(raw)["mcpServers"]["connector-proxy"]
    return json.loads((Path.home() / ".workbuddy/mcp.json")).get("connector-proxy", {})


def _rpc(method: str, params: dict | None = None, notify: bool = False):
    cfg = _cfg()
    body: dict = {"jsonrpc": "2.0", "method": method}
    if not notify:
        _seq["n"] += 1
        body["id"] = _seq["n"]
    if params is not None:
        body["params"] = params
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    h.update(cfg.get("headers", {}))
    if _SESSION["id"]:
        h["mcp-session-id"] = _SESSION["id"]
    req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(),
                                 headers=h, method="POST")
    last = None
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    _SESSION["id"] = sid
                raw = r.read().decode()
            for line in raw.split("\n"):
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return json.loads(raw)
        except Exception as e:  # 网络抖动：短退避重试
            last = e
            time.sleep(min(2 ** attempt, 20))
    raise last or RuntimeError("MCP 代理重试耗尽")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 读操作直连（查询一直正常）


def _ensure_session():
    if _SESSION["id"] is None:
        _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "ticket-sync", "version": "1.0"}})
        _rpc("notifications/initialized", notify=True)


def _tool(name: str, args: dict) -> dict:
    """调用 MCP 工具，返回 result；工具级错误抛 RuntimeError。"""
    _ensure_session()
    res = _rpc("tools/call", {"name": f"notion_{name}", "arguments": args})
    if res is None or "result" not in res:
        raise RuntimeError(f"MCP 调用失败: {str(res)[:200]}")
    meta = res["result"].get("_meta", {})
    if meta.get("tool_error_code"):
        texts = "".join(c.get("text", "") for c in res["result"].get("content", []))
        raise RuntimeError(f"工具错误 {meta['tool_error_code']}: {texts[:200]}")
    return res["result"]


def _unwrap(content_result: dict) -> dict:
    """MCP content 里的 JSON 文本 → dict。"""
    for c in content_result.get("content", []):
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:
                pass
    return {}


def api(path: str, payload: dict | None = None, method: str | None = None):
    """按 path/method 分发到 MCP 工具，带退避重试。"""
    last = None
    for attempt in range(8):
        try:
            return _api_once(path, payload, method)
        except Exception as e:      # 网络/工具级错误：退避重试
            last = e
            time.sleep(min(3 * attempt + 2, 20))
    raise last or RuntimeError("重试耗尽")


def _api_once(path: str, payload: dict | None = None, method: str | None = None):
    """单次调用：读操作走直连（稳定），写操作走 MCP 代理（直连建页会被透明代理拦截）。"""
    is_read = (path.startswith("databases/") and path.endswith("/query")) or (payload is None and method is None)
    if is_read:
        url = f"https://api.notion.com/v1/{path}"
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=_H,
                                     method=method or ("POST" if data else "GET"))
        with _OPENER.open(req, timeout=60) as r:
            return json.load(r)
    if path == "pages" and (method or "POST") == "POST":
        return _unwrap(_tool("post-page", {"parent": payload["parent"], "properties": payload["properties"]}))
    if path.startswith("pages/") and method == "PATCH":
        page_id = path.split("/")[1]
        args = {"page_id": page_id}
        if payload.get("archived") is not None:
            args["archived"] = payload["archived"]
        if payload.get("properties"):
            args["properties"] = payload["properties"]
        return _unwrap(_tool("patch-page", args))
    if path.startswith("databases/") and path.endswith("/query"):
        db_id = path.split("/")[1]
        return _unwrap(_tool("query-data-source", {"data_source_id": db_id, **(payload or {})}))
    if path.startswith("blocks/") and path.endswith("/children"):
        block_id = path.split("/")[1]
        return _unwrap(_tool("patch-block-children", {"block_id": block_id, "children": payload["children"]}))
    raise RuntimeError(f"未支持的 API 路径: {path} {method}")


def _to_date(v: str):
    """'2026-08-12 19:18' → Notion date；'无' → None"""
    v = (v or "").strip()
    if not v or v == "无":
        return None
    try:
        if len(v) >= 16:  # 带时间
            return {"start": f"{v.replace(' ', 'T')}:00+08:00"}
        return {"start": v}
    except Exception:
        return None


def _props(row: dict) -> dict:
    p: dict = {"工单号": {"title": [{"text": {"content": row["工单命名"]}}]}}
    if row.get("店铺") and row["店铺"] != "无":
        p["店铺"] = {"select": {"name": row["店铺"]}}
    if row.get("状态") and row["状态"] != "无":
        p["状态"] = {"select": {"name": row["状态"]}}
    for col in _TEXT_COLS:
        v = (row.get(col) or "").strip()
        if v:
            p[col] = {"rich_text": [{"text": {"content": v[:2000]}}]}
    for col in _DATE_COLS:
        d = _to_date(row.get(col, ""))
        if d:
            p[col] = {"date": d}
    return p


def _blocks_from_body(body: str) -> list[dict]:
    """页面正文 → Notion blocks（## 转二级标题，其余段落）。"""
    blocks = []
    for line in body.split("\n"):
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("## "):
            btype, text = "heading_2", line[3:]
        elif line.startswith("# "):
            btype, text = "heading_1", line[2:]
        else:
            btype, text = "paragraph", line
        # 单块 2000 字上限，超长拆多段 rich_text
        chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [""]
        rich = [{"type": "text", "text": {"content": c}} for c in chunks]
        blocks.append({"object": "block", "type": btype, btype: {"rich_text": rich}})
    return blocks


def _append_blocks(page_id: str, blocks: list[dict]):
    for i in range(0, len(blocks), 90):
        api(f"blocks/{page_id}/children", {"children": blocks[i:i + 90]})
        time.sleep(0.4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="最多同步 N 条（0=不限制）")
    args = ap.parse_args()

    with CSV_PATH.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    existing, cursor = set(), None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = api(f"databases/{DB_ID}/query", payload)
        for r in res["results"]:
            existing.add("".join(t["plain_text"] for t in r["properties"]["工单号"]["title"]))
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]

    missing = [r for r in rows if r["工单命名"] not in existing]
    print(f"本地 {len(rows)} 条 / Notion 已有 {len(existing)} 条 / 待补 {len(missing)} 条")

    if args.dry_run:
        for r in missing:
            print("  [dry]", r["工单命名"], "|", r["状态"])
        return

    # 多轮重试：故障窗按分钟计，单轮跑不完就下一轮接着补漏
    pending = missing[:args.limit] if args.limit else missing
    ok_titles: set[str] = set()
    for pass_no in range(1, 4):
        if not pending:
            break
        if pass_no > 1:
            print(f"—— 第 {pass_no} 轮，补漏 {len(pending)} 条 ——")
            time.sleep(60)
        nxt = []
        for r in pending:
            try:
                page = api("pages", {"parent": {"database_id": DB_ID}, "properties": _props(r)})
                blocks = _blocks_from_body(r.get("页面正文") or "")
                if blocks:
                    _append_blocks(page["id"], blocks)
                ok += 1
                ok_titles.add(r["工单命名"])
                print(f"  ✓ {r['工单命名']}")
            except Exception as e:
                nxt.append(r)
                print(f"  ✗ {r['工单命名']}: {e}")
            time.sleep(0.9)
        pending = nxt
        fail = len(pending)

    print(f"完成：成功 {ok} / 失败 {fail}")
    if pending:
        for r in pending:
            print(f"  [未同步] {r['工单命名']}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
