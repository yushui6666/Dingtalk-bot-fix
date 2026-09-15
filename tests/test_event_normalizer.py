"""事件标准化单测：字段映射、缺字段丢弃、系统账号过滤、角色解析。"""

from datetime import datetime

import pytest

from config import GROUPS, LISTENER_USER_ID, USER_ID_MAP
from event_normalizer import normalize_event
from models import ROLE_LEADER, ROLE_SYSTEM, ROLE_UNKNOWN

# Phase 0 实测的真实事件样例（脱敏保留结构）
REAL_EVENT = {
    "type": "user_im_message_receive_group",
    "event_id": "fec9a70f2ddd43ad9de29b8b4ac86733",
    "timestamp": 1786415762243,
    "subscribe_id": "subId-c1973b128cb643968b07a40e315e376a",
    "message_id": "msg0F8pMh9Quen8TndDnKl9+Q==",
    "conversation_id": "cid_demo_group_c==",
    "sender": "工程师D",
    "sender_open_dingtalk_id": "oid_engineer_d",
    "content": "收到",
    "create_time": "2026-08-11 10:36:01",
    "event_time": 1786415761476,
}

TEST_GROUP = GROUPS[0]


def test_real_event_normalized():
    msg = normalize_event(REAL_EVENT, TEST_GROUP, USER_ID_MAP)
    assert msg is not None
    assert msg.message_id == "msg0F8pMh9Quen8TndDnKl9+Q=="
    assert msg.group_id == "cid_demo_group_c=="
    # sender_id 取事件 openDingtalkId
    assert msg.sender_id == "oid_engineer_d"
    assert msg.sender_name == "工程师D"
    assert msg.content == "收到"
    assert msg.message_type == "text"
    assert msg.is_self is False
    # create_time 解析成功
    assert msg.sent_at == datetime(2026, 8, 11, 10, 36, 1)


def test_role_mapped_via_user_id():
    """工程师D openDingtalkId → userId → LEADER（同时配置为工程师/工程负责人，最高优先级）。"""
    msg = normalize_event(REAL_EVENT, TEST_GROUP, USER_ID_MAP)
    assert msg is not None
    assert msg.sender_role == ROLE_LEADER


def test_system_account_filtered():
    """工程部AI（系统账号）回流 → is_self=True，角色 SYSTEM。"""
    evt = dict(REAL_EVENT)
    evt["sender"] = "工程部AI"
    evt["sender_open_dingtalk_id"] = LISTENER_USER_ID
    msg = normalize_event(evt, TEST_GROUP, USER_ID_MAP)
    assert msg is not None
    assert msg.is_self is True
    assert msg.sender_role == ROLE_SYSTEM


@pytest.mark.parametrize("missing", ["message_id", "conversation_id", "sender_open_dingtalk_id"])
def test_missing_required_field_dropped(missing):
    evt = dict(REAL_EVENT)
    evt.pop(missing)
    assert normalize_event(evt, TEST_GROUP, USER_ID_MAP) is None


def test_bad_create_time_dropped():
    evt = dict(REAL_EVENT)
    evt["create_time"] = "not-a-time"
    assert normalize_event(evt, TEST_GROUP, USER_ID_MAP) is None


def test_unknown_sender_role():
    evt = dict(REAL_EVENT)
    evt["sender"] = "路人甲"
    evt["sender_open_dingtalk_id"] = "oid-unknown-person"
    msg = normalize_event(evt, TEST_GROUP, USER_ID_MAP)
    assert msg is not None
    assert msg.sender_role == ROLE_UNKNOWN
