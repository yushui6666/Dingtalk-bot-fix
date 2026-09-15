"""系统运行入口（计划书 Task 11）。

启动：初始化数据库 → 构建管道 → 并发启动「监听 + 收件箱 Worker + 调度器」。
用法::

    # 影子模式（只记录语义决策，不执行）
    python main.py --mode SHADOW

    # 辅助模式（模型来源动作一律待确认）
    python main.py --mode ASSISTED

    # 生产模式（按协议确认策略执行，默认）
    python main.py --mode PRODUCTION --duration 3600
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import os
import subprocess
import sys
from pathlib import Path

from logger import get_logger, setup_logging

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent


def _load_env_file() -> None:
    """简单加载项目 .env（不覆盖已有环境变量）。

    必须在 import config 之前调用，否则 config 里的 LLM_API_KEY 等
    环境变量读取不到。
    """
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# 模块加载即加载 .env，保证 config 能读到 LLM_API_KEY 等环境变量
_load_env_file()

import config as _config  # noqa: E402  — 需在 _load_env_file 之后导入
from config import (  # noqa: E402  — 需在 _load_env_file 之后导入
    DWS_CMD,
    GROUPS,
    LLM_API_KEY,
    LLM_ENABLED,
    LOG_DIR,
    load_groups,
    set_groups_config,
)


def _dws_sender(target_id: str, text: str) -> None:
    """通过 dws CLI 发消息（同步，低吞吐场景够用）。

    target_id 支持 "user:<userId>" 前缀走单聊（响应 SLA 升级提醒），
    其余按群 openconversation_id 发送。

    失败语义（2026-08-24 修复）：dws 非零退出码 / 超时 / 启动异常一律抛
    RuntimeError，由 Notifier 据此标记 FAILED 或留 PENDING 重试；
    此前只记 warning 正常返回，导致 Outbox 把失败误标 SENT。
    """
    if target_id.startswith("user:"):
        cmd = [DWS_CMD, "chat", "message", "send",
               "--user", target_id[len("user:"):], "--text", text]
    else:
        cmd = [DWS_CMD, "chat", "message", "send", "--group", target_id, "--text", text]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        logger.warning("dws 发送异常 target=%s err=%s", target_id, exc)
        raise RuntimeError(f"dws 发送异常: {exc}") from exc
    if proc.returncode != 0:
        logger.warning("dws 发送失败 target=%s stderr=%s", target_id, proc.stderr[:200])
        raise RuntimeError(f"dws 退出码 {proc.returncode}: {proc.stderr[:200]}")


def _build_pipeline(mode: str):
    from db import Database
    from notifier import Notifier
    from pipeline import MessageProcessingPipeline, RuntimeMode
    from routing.pending_actions import PendingActionService
    from routing.ticket_contexts import TicketContextStore
    from routing.ticket_router import TicketRouter
    from semantics.protocol_loader import load_protocol
    from tickets.executor import TicketCommandExecutor
    from tickets.repository import TicketRepository

    db = Database()
    db.init_schema()

    protocol_path = _PROJECT_ROOT / "protocols" / "ticket_semantics.v4.json"
    protocol = load_protocol(protocol_path)

    repo = TicketRepository(db)
    router = TicketRouter()
    context = TicketContextStore(db)
    pending = PendingActionService(db)
    executor = TicketCommandExecutor(db, repo)
    notifier = Notifier(
        db, _dws_sender,
        enabled=mode != "SHADOW",
    )

    archiver = None
    if _config.IMAGE_ARCHIVE_ENABLED:
        from images.archive import AttachmentArchiver

        archiver = AttachmentArchiver(db=db)
        logger.info("图片附件归档已启用 max_bytes=%d", _config.IMAGE_MAX_BYTES)

    classifier = None
    if LLM_ENABLED and LLM_API_KEY:
        from semantics.classifier import SemanticClassifier
        from semantics.model_client import OpenAICompatibleModelClient

        client = OpenAICompatibleModelClient()
        classifier = SemanticClassifier(client=client, protocol=protocol)
        logger.info("云端模型已启用 model=%s base=%s", client.model, client.base_url)
    else:
        logger.info("云端模型未启用，仅走关键词快路径")

    pipeline = MessageProcessingPipeline(
        db=db,
        repo=repo,
        protocol=protocol,
        router=router,
        context=context,
        pending=pending,
        executor=executor,
        notifier=notifier,
        classifier=classifier,
        archiver=archiver,
        mode=RuntimeMode(mode),
    )
    return db, pipeline, notifier, archiver


async def main(
    mode: str,
    duration: int | None,
    group_filter: str | None,
    *,
    test_config: bool = False,
    groups_config: str | None = None,
) -> None:
    from event_listener import run_listeners
    from workers.inbox_worker import InboxWorker
    from workers.scheduler import SchedulerWorker

    # 群配置切换（--test 用测试群配置，--groups-config 指定文件）
    if test_config or groups_config:
        set_groups_config(test=test_config, path=groups_config)

    db, pipeline, notifier, archiver = _build_pipeline(mode)

    async def inbox_handler(msg) -> None:
        """监听回调：快速入箱，不在回调内调用模型。"""
        enqueued = db.enqueue_message(msg)
        logger.info("消息入箱 %s enqueued=%s", msg.brief(), enqueued)

    import config as _config

    groups = _config.GROUPS
    if group_filter:
        groups = [g for g in _config.GROUPS if g["store_name"] == group_filter]
    load_groups()

    # 把配置群同步到数据库（幂等，工单序号 ticket_seq 保留），
    # 保证建单时 executor 的 db.get_group() 能取到群配置
    for g in groups:
        db.upsert_group(g)

    worker = InboxWorker(
        db=db,
        pipeline=pipeline,
        notifier=notifier,
        group_ids=[g["group_id"] for g in groups],
    )
    scheduler = SchedulerWorker(db=db, notifier=notifier)

    tasks = [
        asyncio.create_task(run_listeners(groups, inbox_handler)),
        asyncio.create_task(worker.run()),
    ]
    if mode != "SHADOW":
        tasks.append(asyncio.create_task(scheduler.run()))
    logger.info("系统启动 mode=%s groups=%d", mode, len(groups))

    try:
        if duration:
            await asyncio.sleep(duration)
        else:
            while True:  # 默认常驻，直到外部信号取消
                await asyncio.sleep(3600)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if archiver is not None:
            await archiver.aclose()
        # 兜底：趁事件循环仍存活强制回收一次，让仍被引用链（异常回溯环等）
        # 挂住的子进程 transport 在此刻完成关闭；否则解释器退出阶段 GC 会
        # 刷屏「RuntimeError: Event loop is closed」（2026-08-26 停机日志）。
        gc.collect()
        await asyncio.sleep(0)  # 让 transport.close() 排队的回调跑一拍
        db.close()
        logger.info("系统停止")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="钉钉报修工单系统")
    parser.add_argument("--mode", choices=["SHADOW", "ASSISTED", "PRODUCTION"],
                        default="PRODUCTION", help="运行模式")
    parser.add_argument("--duration", type=int, default=None, help="运行秒数（默认长驻）")
    parser.add_argument("--group", default=None, help="仅监听指定群名")
    parser.add_argument("--test", action="store_true",
                        help="使用测试群配置 data/group-test.json（与 --groups-config 互斥）")
    parser.add_argument("--groups-config", default=None,
                        help="指定群配置文件路径（优先于 --test）")
    args = parser.parse_args()

    setup_logging(level="INFO", log_dir=LOG_DIR)
    try:
        asyncio.run(main(
            args.mode, args.duration, args.group,
            test_config=args.test,
            groups_config=args.groups_config,
        ))
    except KeyboardInterrupt:
        sys.exit(0)
