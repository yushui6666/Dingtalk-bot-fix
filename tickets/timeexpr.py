"""中文相对时间解析（特殊情况「预计恢复」用，2026-08-26）。

用户被引导回复「特殊情况：原因；预计恢复：时间」，时间是自然语言
（如「一小时内」「明天 14:00」「8月28日」）。本模块把常见表达解析为
绝对 datetime；解析失败返回 None（调用方按无预计时间处理，改走
提交 24h 后每日跟进）。

支持的表达（按优先级）：
- 相对时长：N小时(内/后)、N个小时、半小时、N分钟(内/后)、N天(后/内)
- 绝对日期时间：YYYY-MM-DD [HH:MM[:SS]]、M月D日[ HH:MM]、今天/明天/后天 [HH:MM/HH点]
- 当日时刻：HH:MM / HH点（已过则按次日同一时刻理解）
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_NUM_CN = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}

_REL_HOURS = re.compile(r"^(?:(\d+)|([一二两三四五六七八九十半]))\s*个?\s*小时(?:钟)?(?:内|后)?$")
_REL_MINUTES = re.compile(r"^(?:(\d+)|([一二三四五六七八九十]))\s*分钟(?:内|后)?$")
_REL_DAYS = re.compile(r"^(?:(\d+)|([一二两三四五六七八九十]))\s*天(?:后|内)?$")
_ABS_DATETIME = re.compile(
    r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?\s*"
    r"(?:(\d{1,2})[点:：时](\d{1,2})?分?)?$"
)
_MONTH_DAY = re.compile(r"^(\d{1,2})月(\d{1,2})[日号]\s*(?:(\d{1,2})[点:：时](\d{1,2})?分?)?$")
_DAY_OFFSET = re.compile(r"^(今天|明天|后天)\s*(?:(\d{1,2})[点:：时](\d{1,2})?分?)?$")
_CLOCK = re.compile(r"^(\d{1,2})[点:：时](\d{1,2})?分?$")


def _cn_num(text: str) -> float | None:
    if text is None:
        return None
    if text in _NUM_CN:
        return _NUM_CN[text]
    try:
        return float(text)
    except ValueError:
        return None


def _clamp_hour(hour: float, minute: float) -> tuple[int, int] | None:
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def parse_resume_time(text: str, now: datetime) -> datetime | None:
    """把「预计恢复」的自然语言时间解析为绝对时间；失败返回 None。"""
    cleaned = (text or "").strip().replace("：", ":")
    if not cleaned:
        return None

    if m := _REL_HOURS.match(cleaned):
        n = _cn_num(m.group(1) or m.group(2))
        if n is not None:
            return now + timedelta(hours=n)
    if m := _REL_MINUTES.match(cleaned):
        n = _cn_num(m.group(1) or m.group(2))
        if n is not None:
            return now + timedelta(minutes=n)
    if m := _REL_DAYS.match(cleaned):
        n = _cn_num(m.group(1) or m.group(2))
        if n is not None:
            return now + timedelta(days=n)
    if m := _ABS_DATETIME.match(cleaned):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hm = _clamp_hour(int(m.group(4) or 0), int(m.group(5) or 0)) if m.group(4) else (0, 0)
        if hm is None:
            return None
        try:
            return datetime(y, mo, d, hm[0], hm[1])
        except ValueError:
            return None
    if m := _MONTH_DAY.match(cleaned):
        mo, d = int(m.group(1)), int(m.group(2))
        hm = _clamp_hour(int(m.group(3) or 0), int(m.group(4) or 0)) if m.group(3) else (0, 0)
        if hm is None:
            return None
        try:
            candidate = datetime(now.year, mo, d, hm[0], hm[1])
        except ValueError:
            return None
        if candidate < now and mo < now.month:
            candidate = candidate.replace(year=now.year + 1)
        return candidate
    if m := _DAY_OFFSET.match(cleaned):
        offset = {"今天": 0, "明天": 1, "后天": 2}[m.group(1)]
        hm = _clamp_hour(int(m.group(2) or 0), int(m.group(3) or 0)) if m.group(2) else None
        base = now + timedelta(days=offset)
        if hm is None:
            # 纯「明天/后天」无时刻 → 次日零点起（今天则按次日零点，避免立即到期）
            return (now + timedelta(days=max(offset, 1))).replace(hour=0, minute=0, second=0, microsecond=0)
        if offset == 0 and datetime.combine(base.date(), datetime.min.time()).replace(
                hour=hm[0], minute=hm[1]) < now:
            base += timedelta(days=1)
        return base.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if m := _CLOCK.match(cleaned):
        hm = _clamp_hour(int(m.group(1)), int(m.group(2) or 0))
        if hm is None:
            return None
        candidate = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return None
