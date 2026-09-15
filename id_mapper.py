"""ID 映射：openDingtalkId ↔ userId。

背景（Phase 0 实测 + 用户确认 2026-08-11）：
- 事件流只提供 sender_open_dingtalk_id（openDingtalkId）。
- 群成员列表同样只返回 openDingtalkId。
- 私聊发送（企业内部成员）必须用 --user <userId>；--open-dingtalk-id 仅用于外部联系人/机器人/跨组织身份，企业内部单聊会 FAILED。
- 正式环境群成员均为同一组织成员，角色配置与私聊发送统一用 userId。

因此系统需要 openDingtalkId → userId 映射：
- 角色识别：事件 sender_open_dingtalk_id → userId → 匹配 manager_ids/engineer_ids（userId）。
- 私聊发送：直接使用配置中的 userId。

当前使用静态映射（Phase 1 测试用，见 config.USER_ID_MAP）；
正式环境由启动流程调用通讯录接口自动解析并缓存（TODO Phase 1 收尾：contact 批量解析）。
"""

from __future__ import annotations

from typing import Optional

from config import USER_ID_MAP
from logger import get_logger

logger = get_logger(__name__)


def to_user_id(
    open_dingtalk_id: str,
    id_map: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """openDingtalkId → userId；无映射返回 None。"""
    if open_dingtalk_id is None:
        return None
    m = id_map if id_map is not None else USER_ID_MAP
    uid = m.get(open_dingtalk_id)
    if uid is None:
        logger.debug("openDingtalkId 无 userId 映射 id=%s", open_dingtalk_id)
    return uid


def to_open_dingtalk_id(
    user_id: str,
    id_map: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """userId → openDingtalkId（反向查找）。"""
    m = id_map if id_map is not None else USER_ID_MAP
    for oid, uid in m.items():
        if uid == user_id:
            return oid
    return None
