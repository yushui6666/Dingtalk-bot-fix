"""收件箱工作器（计划书 §8.2、Task 6）。

跨群并行、群内串行：
- 每个群一个独立协程，组间并发 —— 某群的慢模型调用不阻塞其他群。
- 同一群内按 `(sent_at, message_id)` 顺序逐条处理（后条等前条落定）。
- 由 supervisor 保持群协程存活，并周期性刷新群列表（支持动态加群）。
- 启动时把崩溃残留的 PROCESSING 重置回 RECEIVED。

并发安全：所有数据库写操作是同步且原子的，模型调用发生在事务之外，
故单连接跨协程共享不会出现写冲突。
"""

from __future__ import annotations

import asyncio
from typing import Any

from db import Database
from logger import get_logger

logger = get_logger(__name__)


class InboxWorker:
    def __init__(
        self,
        *,
        db: Database,
        pipeline: Any,
        notifier: Any,
        poll_interval: float = 0.5,
        batch: int = 5,
        group_ids: list[str] | None = None,
        supervise_interval: float = 3.0,
    ) -> None:
        self._db = db
        self._pipeline = pipeline
        self._notifier = notifier
        self._poll_interval = poll_interval
        self._batch = batch
        self._group_ids = list(group_ids) if group_ids is not None else None
        self._supervise_interval = supervise_interval

    async def run(self) -> None:
        self._db.inbox_reset_stale()
        logger.info("收件箱工作器启动 poll_interval=%ss batch=%d", self._poll_interval, self._batch)
        tasks: dict[str, asyncio.Task] = {}
        try:
            while True:
                current = self._current_group_ids()
                for group_id in current:
                    if group_id not in tasks or tasks[group_id].done():
                        tasks[group_id] = asyncio.create_task(self._group_loop(group_id))
                # 清理已消失群的协程
                for gid in list(tasks):
                    if gid not in current:
                        tasks[gid].cancel()
                        tasks.pop(gid, None)
                await asyncio.sleep(self._supervise_interval)
        except asyncio.CancelledError:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            logger.info("收件箱工作器停止")
            raise

    def _current_group_ids(self) -> list[str]:
        if self._group_ids is not None:
            return self._group_ids
        return self._db.list_group_ids()

    async def _group_loop(self, group_id: str) -> None:
        """单群串行消费；空队列时小睡，重试消息到期后自然被再次拉取。"""
        logger.info("群处理协程启动 group=%s", group_id)
        while True:
            try:
                items = self._db.inbox_next_due_for_group(group_id, limit=self._batch)
                if not items:
                    await asyncio.sleep(self._poll_interval)
                    continue
                for item in items:
                    await self._pipeline.process(item)
                self._notifier.flush()
            except asyncio.CancelledError:
                logger.info("群处理协程停止 group=%s", group_id)
                raise
            except Exception as exc:
                logger.error("群处理异常 group=%s err=%s", group_id, exc)
                await asyncio.sleep(self._poll_interval)
