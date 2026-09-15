"""乱序保护基础单测：(sent_at, message_id) 稳定版本顺序。

覆盖计划书 14.5：
- sent_at 不同：早的消息严格在前
- sent_at 相同：message_id 字典序决定先后（稳定、可复现）
- 迟到消息（早于当前版本）比较判定正确
- 时区统一：naive datetime 与 aware datetime 可比较
"""

from datetime import datetime, timedelta

from ordering import TZ, is_after, order_key, parse_naive_dt


def test_sent_at_dominates():
    """sent_at 更早 → 严格在前，与 message_id 无关。"""
    early = datetime(2026, 8, 11, 10, 0, 0, tzinfo=TZ)
    late = datetime(2026, 8, 11, 10, 1, 0, tzinfo=TZ)
    assert is_after(late, "msg-z", early, "msg-a") is True
    assert is_after(early, "msg-z", late, "msg-a") is False


def test_same_sent_at_message_id_tiebreak():
    """同一秒内多条消息：message_id 字典序稳定排序。"""
    t = datetime(2026, 8, 11, 10, 0, 0, tzinfo=TZ)
    # msg-b 应排在 msg-a 之后
    assert order_key(t, "msg-b") > order_key(t, "msg-a")


def test_same_message_id_same_key():
    """相同 (sent_at, message_id) 产生相同键（幂等比较基础）。"""
    t = datetime(2026, 8, 11, 10, 0, 0, tzinfo=TZ)
    assert order_key(t, "msg-a") == order_key(t, "msg-a")


def test_late_message_detected():
    """迟到消息（业务版本已前进）→ is_after 判定为 False。"""
    current_sent_at = datetime(2026, 8, 11, 12, 0, 0, tzinfo=TZ)
    current_msg_id = "msg-current"
    late_sent_at = datetime(2026, 8, 11, 11, 0, 0, tzinfo=TZ)
    late_msg_id = "msg-late"
    # 迟到消息不晚于当前版本 → 不能覆盖当前值
    assert is_after(late_sent_at, late_msg_id, current_sent_at, current_msg_id) is False


def test_naive_dt_treated_as_shanghai():
    """naive datetime 按 Asia/Shanghai 绑定，与 aware 可比较。"""
    naive = datetime(2026, 8, 11, 10, 0, 0)  # 无时区
    aware = datetime(2026, 8, 11, 10, 0, 0, tzinfo=TZ)
    assert order_key(naive, "m") == order_key(aware, "m")


def test_parse_naive_dt_shanghai():
    """create_time 字符串解析为东八区 aware datetime。"""
    dt = parse_naive_dt("2026-08-11 10:36:01")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=8)


def test_parse_naive_dt_invalid():
    assert parse_naive_dt("not-a-time") is None
    assert parse_naive_dt(None) is None


def test_ordering_transitive():
    """排序键满足传递性（用于稳定版本链）。"""
    t1 = datetime(2026, 8, 11, 9, 0, 0, tzinfo=TZ)
    t2 = datetime(2026, 8, 11, 10, 0, 0, tzinfo=TZ)
    t3 = datetime(2026, 8, 11, 11, 0, 0, tzinfo=TZ)
    keys = [order_key(t1, "a"), order_key(t2, "a"), order_key(t3, "a")]
    assert keys == sorted(keys)
    assert is_after(t3, "a", t1, "a") and is_after(t2, "a", t1, "a")
