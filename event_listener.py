"""多群消息监听器：dws event +listen-im 子进程 + NDJSON 逐行消费。

- 每个群一个独立监听子进程（符合 dws event 规范：不同群拆独立进程）。
- 通过 asyncio 并发启动多个群的监听。
- 断线重连：子进程非正常退出时按退避序列（1s→2s→4s→…→60s 封顶）重启。
- ready marker（`[event] ready ...`）检测通过后才认为监听生效。
- 每行事件 → event_normalizer.normalize_event → 注入的 handler 回调。

关键节点日志：订阅就绪 / 收到事件 / 子进程退出 / 重连退避。
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

from config import DWS_CMD, LOG_DIR, USER_ID_MAP
from event_normalizer import normalize_event
from logger import get_logger
from models import NormalizedMessage

logger = get_logger(__name__)

# 事件回调：async (msg) -> None
MessageHandler = Callable[[NormalizedMessage], Awaitable[None]]

# 退避序列（秒），到上限后保持
_RETRY_BACKOFF = (1, 2, 4, 8, 16, 30, 60)
_MAX_BACKOFF = 60


def _build_listen_cmd(group_id: str, chat_query: Optional[str] = None) -> list[str]:
    cmd = [
        DWS_CMD, "event", "+listen-im",
        "--kind", "group",
        "-f", "ndjson",
        "--duration", "0",
        "--max-events", "0",
    ]
    if chat_query:
        cmd += ["--chat-query", chat_query]
    else:
        cmd += ["--chat-id", group_id]
    return cmd


class GroupListener:
    """单群监听器。"""

    def __init__(
        self,
        group: dict,
        handler: MessageHandler,
        chat_query: Optional[str] = None,
        id_map: Optional[dict[str, str]] = None,
    ) -> None:
        self.group = group
        self.handler = handler
        self.chat_query = chat_query or group.get("store_name")
        self.id_map = id_map if id_map is not None else USER_ID_MAP
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stop_requested = False
        self._ready_seen = False

    async def run(self) -> None:
        """阻塞监听直到 stop() 被调用或无法恢复。"""
        attempt = 0
        while not self._stop_requested:
            cmd = _build_listen_cmd(self.group["group_id"], self.chat_query)
            logger.info(
                "启动监听群 group_id=%s store=%s cmd=%s",
                self.group["group_id"], self.group.get("store_name"), " ".join(cmd),
            )
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await self._consume()
            except asyncio.CancelledError:
                # 任务被取消（如外部优雅停机）：终止子进程后重新抛出
                await self._terminate_proc()
                raise
            except Exception as exc:
                logger.error("监听进程异常 group_id=%s err=%s", self.group["group_id"], exc)
            finally:
                await self._terminate_proc()
                self._proc = None

            if self._stop_requested:
                logger.info("监听已停止 group_id=%s", self.group["group_id"])
                return

            attempt += 1
            backoff = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
            logger.warning(
                "监听进程退出，准备重连 group_id=%s attempt=%d backoff=%ds",
                self.group["group_id"], attempt, backoff,
            )
            await asyncio.sleep(backoff)

    async def _consume(self) -> None:
        """消费子进程输出，直到进程结束。

        Phase 1 实测（2026-08-11）：dws listen-im 的流分工为
        - stdout：NDJSON 事件数据（`{` 开头，json 行）
        - stderr：`[event] ready/exited/bus` 等元信息（含就绪标记）
        两个流必须同时消费：ready 标记在 stderr，事件数据在 stdout。
        """
        assert self._proc is not None and self._proc.stdout is not None
        self._ready_seen = False
        stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            async for raw_line in self._proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                if line.startswith("{"):
                    await self._dispatch_line(line)
                else:
                    logger.debug("监听 stdout 非事件行 group_id=%s line=%r",
                                 self.group["group_id"], line)
        finally:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            if not self._ready_seen:
                logger.warning("监听进程未就绪即退出 group_id=%s", self.group["group_id"])

    async def _drain_stderr(self) -> None:
        """消费 stderr：ready/exited/bus 元信息。就绪标记写回共享状态。"""
        assert self._proc is not None and self._proc.stderr is not None
        async for raw_line in self._proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            if "[event] ready" in line:
                self._ready_seen = True
                logger.info(
                    "监听订阅就绪 group_id=%s line=%s",
                    self.group["group_id"], line,
                )
            elif "[event] exited" in line:
                logger.info("监听子进程退出 group_id=%s line=%s", self.group["group_id"], line)
            elif line.startswith("[event]"):
                logger.debug("监听元信息 group_id=%s line=%s", self.group["group_id"], line)
            else:
                logger.debug("监听 stderr 未识别输出 group_id=%s line=%r",
                             self.group["group_id"], line)

    async def _dispatch_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("NDJSON 解析失败 line=%r", line[:200])
            return

        # 规范 3.2：收到原始事件（DEBUG，不落正文）
        logger.debug(
            "收到原始事件 event_id=%s type=%s message_id=%s",
            event.get("event_id"), event.get("type"), event.get("message_id"),
        )

        msg = normalize_event(event, self.group, self.id_map)
        if msg is None:
            return
        if msg.is_self:
            # 系统账号回流：业务层忽略，但记录幂等结果，避免重复消费
            logger.debug("忽略系统账号消息 message_id=%s", msg.message_id)
            return
        logger.info("收到群消息 %s", msg.brief())
        await self.handler(msg)

    @staticmethod
    def _close_transport(proc: Optional[asyncio.subprocess.Process]) -> None:
        """显式关闭子进程 transport（含 stdout/stderr 管道）。

        只 terminate+wait 不会关闭 transport；残留对象若被引用链（异常回溯
        环等）拖到事件循环关闭之后才 GC，__del__ 里关管道会刷屏
        「RuntimeError: Event loop is closed」（2026-08-26 停机日志）。
        """
        if proc is None:
            return
        transport = getattr(proc, "_transport", None)
        if transport is not None and not transport.is_closing():
            transport.close()

    async def _terminate_proc(self) -> None:
        """终止当前监听子进程（若存活）。防止取消任务后残留进程占用订阅。"""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._close_transport(proc)
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=3)
        logger.info("监听子进程已终止 group_id=%s pid=%s", self.group["group_id"], proc.pid)
        self._close_transport(proc)

    async def stop(self) -> None:
        """优雅停止：请求退出并终止子进程。"""
        self._stop_requested = True
        await self._terminate_proc()


async def run_listeners(
    groups: list[dict],
    handler: MessageHandler,
    chat_query_map: Optional[dict[str, str]] = None,
) -> None:
    """并发启动多群监听，直到任一停止（通常配合外部 signal 停止）。"""
    chat_query_map = chat_query_map or {}
    listeners = [
        GroupListener(g, handler, chat_query=chat_query_map.get(g["group_id"]))
        for g in groups
    ]
    logger.info("启动多群监听 count=%d", len(listeners))
    await asyncio.gather(*(lst.run() for lst in listeners))


# ───────────────────────── 自测入口 ─────────────────────────
# python event_listener.py --duration 60
# 监听 config.GROUPS，60 秒后退出，收到的消息打印 INFO 日志。
if __name__ == "__main__":
    import argparse

    from logger import setup_logging

    parser = argparse.ArgumentParser(description="监听群消息自测")
    parser.add_argument("--duration", type=int, default=60, help="监听时长（秒）")
    parser.add_argument("--group", type=str, default=None, help="指定群名（默认全部配置群）")
    args = parser.parse_args()

    setup_logging(level="INFO", log_dir=LOG_DIR)

    from config import GROUPS, load_groups

    load_groups()
    groups = GROUPS
    if args.group:
        groups = [g for g in GROUPS if g["store_name"] == args.group]

    async def handler(msg: NormalizedMessage) -> None:
        logger.info("自测收到消息 role=%s sender=%s content=%s",
                    msg.sender_role, msg.sender_name, msg.content[:50])

    async def main() -> None:
        task = asyncio.create_task(run_listeners(groups, handler))
        try:
            await asyncio.sleep(args.duration)
        finally:
            # 触发优雅停止（简化：取消监听任务）
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(main())
