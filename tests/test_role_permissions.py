"""角色识别单测：各角色解析、外部账号、角色重叠校验。"""

import pytest

from config import LISTENER_USER_ID, USER_ID_MAP
from models import (
    ROLE_ENGINEER,
    ROLE_LEADER,
    ROLE_MANAGER,
    ROLE_OTHER,
    ROLE_SYSTEM,
    ROLE_UNKNOWN,
)
from role_resolver import resolve_role, validate_role_overlap

NEI_YU_QING_OID = "oid_engineer_d"
NEI_YU_QING_UID = "9000000000000001"
YUSHUI_OID = "oid_manager_b"  # 外部测试账号，无 userId

GROUP_WITH_ALL = {
    "group_id": "g1",
    "manager_ids": ["uid-manager-1"],
    "engineer_ids": ["uid-engineer-1"],
    "other_member_ids": ["uid-other-1"],
    "engineering_leader_id": "uid-leader-1",
    "regional_manager_id": "uid-regional-1",
}


def test_system_account():
    assert resolve_role(GROUP_WITH_ALL, LISTENER_USER_ID, USER_ID_MAP) == ROLE_SYSTEM


def test_manager_via_mapping():
    # openDingtalkId → userId 映射后命中 manager
    id_map = {"oid-manager": "uid-manager-1"}
    assert resolve_role(GROUP_WITH_ALL, "oid-manager", id_map) == ROLE_MANAGER


def test_engineer_via_mapping():
    id_map = {"oid-engineer": "uid-engineer-1"}
    assert resolve_role(GROUP_WITH_ALL, "oid-engineer", id_map) == ROLE_ENGINEER


def test_leader_via_engineering_leader():
    """工程负责人（engineering_leader_id）→ LEADER，优先级高于店长/工程师。"""
    id_map = {"oid-leader": "uid-leader-1"}
    assert resolve_role(GROUP_WITH_ALL, "oid-leader", id_map) == ROLE_LEADER


def test_leader_via_regional_manager():
    """区域经理（regional_manager_id）→ LEADER。"""
    id_map = {"oid-regional": "uid-regional-1"}
    assert resolve_role(GROUP_WITH_ALL, "oid-regional", id_map) == ROLE_LEADER


def test_leader_takes_precedence_over_manager():
    """同一 userId 同时是店长和工程负责人时 → LEADER（最高优先级）。"""
    group = dict(GROUP_WITH_ALL)
    group["manager_ids"] = ["uid-leader-1"]
    id_map = {"oid-leader": "uid-leader-1"}
    assert resolve_role(group, "oid-leader", id_map) == ROLE_LEADER


def test_other_via_mapping():
    id_map = {"oid-other": "uid-other-1"}
    assert resolve_role(GROUP_WITH_ALL, "oid-other", id_map) == ROLE_OTHER


def test_no_mapping_unknown():
    # 无 userId 映射（外部账号如 店长B）→ UNKNOWN
    assert resolve_role(GROUP_WITH_ALL, YUSHUI_OID, USER_ID_MAP) == ROLE_UNKNOWN


def test_no_group_config():
    assert resolve_role(None, "anything", USER_ID_MAP) == ROLE_UNKNOWN


def test_mapping_to_nonexistent_user():
    # 映射存在但 userId 不在任何角色列表
    id_map = {"oid-x": "uid-not-listed"}
    assert resolve_role(GROUP_WITH_ALL, "oid-x", id_map) == ROLE_UNKNOWN


def test_role_overlap_raises():
    bad = {"group_id": "g2", "manager_ids": ["same-uid"], "engineer_ids": ["same-uid"]}
    with pytest.raises(ValueError):
        validate_role_overlap(bad)


def test_role_overlap_ok():
    validate_role_overlap(GROUP_WITH_ALL)  # 不抛异常
