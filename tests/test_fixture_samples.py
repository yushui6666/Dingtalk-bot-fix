"""样本库驱动的标准化测试：基于 tests/fixtures/events.json 覆盖各角色/媒体/异常场景。"""

import json
from pathlib import Path

import pytest

from config import GROUPS, LISTENER_USER_ID, USER_ID_MAP
from event_normalizer import normalize_event
from models import (
    MSG_FILE,
    MSG_IMAGE,
    MSG_RICH,
    MSG_TEXT,
    ROLE_ENGINEER,
    ROLE_MANAGER,
    ROLE_OTHER,
    ROLE_SYSTEM,
    ROLE_UNKNOWN,
)

FIXTURES = Path(__file__).parent / "fixtures" / "events.json"
DATA = json.loads(FIXTURES.read_text(encoding="utf-8"))
TEXT_SAMPLES = DATA["文本样本"]
MEDIA_SAMPLES = DATA["媒体样本_待真实验证"]
BAD_SAMPLES = DATA["异常样本"]

# 样本用的群配置（id 为占位，与 fixtures 的 conversation_id 对齐）
FIXTURE_GROUP = {
    "group_id": "cid-test-group-001",
    "store_name": "样本测试店",
    "manager_ids": ["uid-manager-a"],
    "engineer_ids": ["uid-engineer-b"],
    "other_member_ids": ["uid-other-c"],
    "engineering_leader_id": "uid-leader",
    "regional_manager_id": "uid-regional",
}
FIXTURE_ID_MAP = {
    "oid-manager-a": "uid-manager-a",
    "oid-engineer-b": "uid-engineer-b",
    "oid-other-c": "uid-other-c",
    "oid-listener": LISTENER_USER_ID,
}


def _normalize(key: str, source: dict) -> object:
    """按 key 取样本并标准化。"""
    return normalize_event(source[key], FIXTURE_GROUP, FIXTURE_ID_MAP)


@pytest.mark.parametrize(
    "key,expected_role",
    [
        ("店长_报修", ROLE_MANAGER),
        ("店长_普通沟通", ROLE_MANAGER),
        ("店长_完毕", ROLE_MANAGER),
        ("工程师_故障判断", ROLE_ENGINEER),
        ("工程师_维修方式_采购", ROLE_ENGINEER),
        ("工程师_超时原因", ROLE_ENGINEER),
        ("其他成员_普通沟通", ROLE_OTHER),
        ("未知成员_未配置", ROLE_UNKNOWN),
    ],
)
def test_text_role_mapping(key, expected_role):
    """文本样本按 openDingtalkId→userId 映射解析角色。"""
    msg = _normalize(key, TEXT_SAMPLES)
    assert msg is not None, f"{key} 应标准化成功"
    assert msg.sender_role == expected_role, f"{key} 角色应为 {expected_role}"


def test_system_account_loop_filtered():
    """系统账号回流 → is_self=True，角色 SYSTEM。"""
    msg = _normalize("系统账号_回流", TEXT_SAMPLES)
    assert msg is not None
    assert msg.is_self is True
    assert msg.sender_role == ROLE_SYSTEM


def test_report_keyword_content_preserved():
    """#报修 文本完整保留（含换行字段），供 Phase 2 解析。"""
    msg = _normalize("店长_报修", TEXT_SAMPLES)
    assert "#报修" in msg.content
    assert "主题：博物馆奇妙夜" in msg.content
    assert "时效：3天" in msg.content
    assert msg.message_type == MSG_TEXT


@pytest.mark.parametrize(
    "key,expected_type",
    [
        ("图片消息", MSG_IMAGE),
        ("文件消息", MSG_FILE),
        ("富文本消息", MSG_RICH),
    ],
)
def test_media_type_detection(key, expected_type):
    """媒体类型检测（样本结构为构造，真实字段待验证）。"""
    msg = _normalize(key, MEDIA_SAMPLES)
    assert msg is not None
    assert msg.message_type == expected_type


def test_rich_text_extracted():
    """富文本 content 为 JSON 时抽取可读文本（计划书 6.1）。"""
    evt = dict(MEDIA_SAMPLES["富文本消息"])
    evt["content"] = json.dumps({"markdown": "**门体照片**见附件"}, ensure_ascii=False)
    msg = normalize_event(evt, FIXTURE_GROUP, FIXTURE_ID_MAP)
    assert msg is not None
    # 抽取 markdown 字段原文；markdown 标记剥离属展示层，不在标准化阶段处理
    assert msg.content == "**门体照片**见附件"


@pytest.mark.parametrize(
    "key",
    ["缺message_id", "缺conversation_id", "缺sender_open_dingtalk_id", "时间格式非法"],
)
def test_invalid_samples_dropped(key):
    """缺必填字段或时间非法 → 返回 None，不进入业务。"""
    assert normalize_event(BAD_SAMPLES[key], FIXTURE_GROUP, FIXTURE_ID_MAP) is None


def test_duplicate_message_id_not_marked_special():
    """重复 message_id 事件本身标准化正常，幂等由 processed_events 层负责（P1-3）。"""
    msg = _normalize("重复message_id", BAD_SAMPLES)
    assert msg is not None
    assert msg.message_id == "msg-dup-001"
