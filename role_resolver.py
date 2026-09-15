"""角色识别：事件 openDingtalkId → userId → 角色。

背景（Phase 0 实测 + 用户确认）：
- 事件提供 sender_open_dingtalk_id；角色配置与私聊发送用 userId。
- 通过 id_mapper 把发送人 openDingtalkId 映射为 userId 后匹配角色列表。
- 系统监听账号（工程部AI）恒为 SYSTEM，直接按 openDingtalkId 判断。
"""

from __future__ import annotations

from typing import Optional

from config import LISTENER_USER_ID
from id_mapper import to_user_id
from logger import get_logger
from models import (
    ROLE_ENGINEER,
    ROLE_LEADER,
    ROLE_MANAGER,
    ROLE_OTHER,
    ROLE_SYSTEM,
    ROLE_UNKNOWN,
)

logger = get_logger(__name__)


def resolve_role(
    group: Optional[dict],
    open_dingtalk_id: str,
    id_map: Optional[dict[str, str]] = None,
) -> str:
    """按群配置解析角色。

    优先级：系统账号 > 工程负责人/区域经理(LEADER) > 店长 > 工程师 > 其他成员 > 未配置。
    group 的 manager_ids/engineer_ids 均为 userId；
    engineering_leader_id / regional_manager_id 为单个 userId（可能为空）。
    """
    if open_dingtalk_id == LISTENER_USER_ID:
        return ROLE_SYSTEM

    if group is None:
        logger.debug("群配置缺失，按未配置处理 sender=%s", open_dingtalk_id)
        return ROLE_UNKNOWN

    user_id = to_user_id(open_dingtalk_id, id_map)
    if user_id is None:
        # 无映射：可能为外部账号或未配置成员
        logger.debug(
            "发送人无 userId 映射，按未配置处理 group=%s oid=%s",
            group.get("group_id"), open_dingtalk_id,
        )
        return ROLE_UNKNOWN

    leader_ids = {
        gid for gid in (
            group.get("engineering_leader_id"),
            group.get("regional_manager_id"),
        ) if gid
    }
    if user_id in leader_ids:
        return ROLE_LEADER
    if user_id in group.get("manager_ids", []):
        return ROLE_MANAGER
    if user_id in group.get("engineer_ids", []):
        return ROLE_ENGINEER
    if user_id in group.get("other_member_ids", []):
        return ROLE_OTHER
    return ROLE_UNKNOWN


def validate_role_overlap(group: dict) -> None:
    """启动校验：同一 userId 不得同时是店长和工程师。

    违规时抛 ValueError（上层应停止监听该群）。
    """
    overlap = set(group.get("manager_ids", [])) & set(group.get("engineer_ids", []))
    if overlap:
        logger.critical(
            "角色重叠 group=%s store=%s overlap=%s —— 程序停止",
            group["group_id"], group.get("store_name"), list(overlap),
        )
        raise ValueError(f"角色重叠: {overlap}")
    logger.debug("角色重叠校验通过 group=%s", group.get("group_id"))
