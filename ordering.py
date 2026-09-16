"""业务事件顺序与乱序保护基础（计划书 14.5）。

核心规则：所有会改变业务状态的消息使用 (sent_at, message_id) 作为
稳定版本顺序。sent_at 相同时，message_id 字典序决定先后，保证
同一秒内多条消息也有确定、可复现的次序。

本模块只提供排序与比较基础，供 Phase 2+ 业务层使用：
- order_key()      构造稳定排序键
- is_after()       判断消息 A 是否严格晚于消息 B
- parse_naive_dt() 统一解析时间字符串为 Asia/Shanghai aware datetime

说明：
- 事件 create_time 为本地时区字符串（Phase 0 实测），统一按
  Asia/Shanghai 处理，避免乱序比较跨时区出错。
- 迟到消息是否"只归档不生效"由具体业务（Phase 3-5）判断，
  本模块不承载业务语义。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from logger import get_logger

logger = get_logger(__name__)

# 统一业务时区（计划书 14.5）
TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 兼容 create_time 的多种字符串格式
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
)


def parse_naive_dt(raw: str) -> Optional[datetime]:
    """把本地时区字符串解析为 Asia/Shanghai aware datetime。

    Phase 0 实测 create_time 为 "2026-08-11 10:36:01"（无时区后缀），
    语义为本地（东八区）时间，因此直接绑定 TZ。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    logger.warning("时间解析失败 value=%r", raw)
    return None


def order_key(sent_at: datetime, message_id: str) -> tuple[str, str]:
    """构造稳定排序键 (sent_at_iso, message_id)。

    - sent_at 若无时区，按 Asia/Shanghai 绑定后转 ISO 字符串。
    - 返回值可直接用于 sorted() / 数据库版本比较。
    """
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=TZ)
    return (sent_at.isoformat(), message_id)


def is_after(
    a_sent_at: datetime,
    a_message_id: str,
    b_sent_at: datetime,
    b_message_id: str,
) -> bool:
    """判断消息 A 是否严格晚于消息 B（(sent_at, message_id) 字典序）。"""
    return order_key(a_sent_at, a_message_id) > order_key(b_sent_at, b_message_id)


class OrderedCommitGate:
    """群级有序提交闸门，同时允许闸门前的识别阶段并行。

    ``message_ids`` 必须按 ``(sent_at, message_id)`` 稳定顺序传入。
    每条消息完成模型识别后先调用 :meth:`wait_turn`，只有前序消息完成短事务
    提交后才继续路由和执行；前序失败时也必须调用 :meth:`mark_done` 释放后续，
    避免一个坏消息把整个群卡死。
    """

    def __init__(self, message_ids: list[str]) -> None:
        self._keys = list(dict.fromkeys(message_ids))
        self._index = {key: index for index, key in enumerate(self._keys)}
        self._turns = [asyncio.Event() for _ in self._keys]
        if self._turns:
            self._turns[0].set()
        self._done: set[str] = set()
        self._next = 0

    async def wait_turn(self, message_id: str) -> None:
        """等待该消息轮到自己提交；不在当前闸门中的消息不阻塞。"""
        index = self._index.get(message_id)
        if index is not None:
            await self._turns[index].wait()

    def mark_done(self, message_id: str) -> None:
        """标记一条消息已结束提交（成功、忽略或失败均可），释放后续。"""
        if message_id not in self._index or message_id in self._done:
            return
        self._done.add(message_id)
        while self._next < len(self._keys) and self._keys[self._next] in self._done:
            self._next += 1
            if self._next < len(self._keys):
                self._turns[self._next].set()
