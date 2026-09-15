"""全局配置。

以计划书第 17 节为基础，填入 Phase 0 实测数据（2026-08-11）。

⚠️ 角色分配为占位配置，待用户确认后更新：
- 店长 / 工程师 / 工程负责人 / 区域经理 均为 openDingtalkId。
- 监听账号 = 「工程部AI」= LISTENER_USER_ID。
"""

from __future__ import annotations

import json
import os as _os
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)


def _load_dotenv(path: Path | None = None) -> dict[str, str]:
    """极简 .env 加载器（无第三方依赖，2026-08-25）。

    - 读取项目根目录 .env（可用 path 参数覆盖，测试用）；
    - 只设置当前进程环境中**不存在**的键：真实环境变量优先于 .env；
    - 支持 KEY=VALUE、整行 # 注释；值两侧成对引号会被剥离。
    必须在本模块任何 ``_os.environ.get`` 之前调用。
    """
    env_path = path or (Path(__file__).resolve().parent / ".env")
    loaded: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return loaded
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in _os.environ:
            _os.environ[key] = value
            loaded[key] = value
    return loaded


_INITIAL_ENV = dict(_os.environ)
_DOTENV_PATH = Path(__file__).resolve().parent / ".env"
_load_dotenv()

# ───────────────────────── 路径 ─────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "tickets.db"
ARCHIVE_DIR = BASE_DIR / "archives"
STORE_TABLE_DIR = BASE_DIR / "data" / "stores"
SUMMARY_OUTPUT_PATH = BASE_DIR / "data" / "summary" / "维修工单汇总.json"
LOG_DIR = BASE_DIR / "logs"
LOG_LEVEL = "INFO"

# LangGraph 只负责编排现有业务流程；tickets.db 仍是业务真相源。
LANGGRAPH_ENABLED = _os.environ.get("LANGGRAPH_ENABLED", "true").lower() in (
    "true", "1", "yes", "on",
)
_langgraph_checkpoint_path = Path(
    _os.environ.get("LANGGRAPH_CHECKPOINT_PATH", "data/langgraph-checkpoints.sqlite")
)
LANGGRAPH_CHECKPOINT_PATH = (
    _langgraph_checkpoint_path
    if _langgraph_checkpoint_path.is_absolute()
    else BASE_DIR / _langgraph_checkpoint_path
)
# RAG 尚未定型：保留开关与接口，当前默认使用空检索器。
RAG_ENABLED = _os.environ.get("RAG_ENABLED", "false").lower() in (
    "true", "1", "yes", "on",
)

# 群与成员配置文件（50 群/几百人规模用，可被环境变量 GROUPS_CONFIG_PATH 覆盖）
# - 生产：data/groups.json（全部门店群）
# - 测试：data/group-test.json（测试群，通过 --test 或 GROUPS_CONFIG_PATH 切换）
DEFAULT_GROUPS_CONFIG_PATH = Path(
    _os.environ.get(
        "GROUPS_CONFIG_PATH",
        str(BASE_DIR / "data" / "groups.json"),
    )
)
GROUPS_TEST_CONFIG_PATH = Path(
    _os.environ.get(
        "GROUPS_TEST_CONFIG_PATH",
        str(BASE_DIR / "data" / "group-test.json"),
    )
)

# 淘宝对账导入源文件（「淘宝订单自动下载与地址对账」工具产出，可被环境变量覆盖）
TAOBAO_ORDER_DETAIL_XLSX = Path(
    _os.environ.get(
        "TAOBAO_ORDER_DETAIL_XLSX",
        "/Users/example/Desktop/淘宝对账/订单地址明细.xlsx",
    )
)
TAOBAO_PENDING_XLSX = Path(
    _os.environ.get(
        "TAOBAO_PENDING_XLSX",
        "/Users/example/Desktop/淘宝对账/待人工处理.xlsx",
    )
)

# 订单↔门店共享表：报修工单提交订单号时写入，另一个 AI 每天回传订单状态
# （可被环境变量 ORDER_STORE_TABLE_PATH 覆盖）
ORDER_STORE_TABLE_PATH = Path(
    _os.environ.get(
        "ORDER_STORE_TABLE_PATH",
        "/Users/example/Desktop/淘宝对账/订单门店状态表.xlsx",
    )
)

# 共享表写入失败后的兜底：调度器每隔多少秒重试未同步订单（可被环境变量覆盖）。
# 登记时共享表被 Excel/WPS 占用等导致写失败 → 订单保持 xlsx_synced=0，
# 调度器 scan_shared_table_resync 按此间隔重试直至写入成功。
SHARED_TABLE_RESYNC_INTERVAL_SECONDS = int(
    _os.environ.get("SHARED_TABLE_RESYNC_INTERVAL_SECONDS", "300")
)

# 订单到货签收后每天提醒一次，直到工单完成（用户决策 2026-08-14）：
# 下单不再自动延期，等货期间照常算时效，超时由工程师回 #超时原因。
ORDER_RECEIVE_REMIND_DAILY = True

# ───────────────────────── 系统监听账号 ─────────────────────────
# 工程部AI 的 openDingtalkId（Phase 0 实测）
LISTENER_USER_ID = "oid_listener_bot"
LISTENER_USER_NAME = "工程部AI"

# ───────────────────────── dws CLI 路径 ─────────────────────────
# 监听/发送/看板同步都通过 dws CLI；默认取 PATH，可显式指定完整路径
# （本机安装位置：/Users/example/.workbuddy/binaries/node/cli-connector-packages/bin/dws）
DWS_CMD = _os.environ.get("DWS_CMD", "dws")

# ───────────────────────── 群配置 ─────────────────────────
# 群与成员配置独立保存在 JSON 文件（结构：{"groups":[...], "user_id_map":{...}}），
# 便于 50 群/几百人规模维护；GROUPS 与 USER_ID_MAP 由此加载，保持导出名不变。
# - 默认（生产）读取 data/groups.json
# - 测试模式（main.py --test）读取 data/group-test.json
# 角色分配（用户确认 2026-08-11）：工程师D=工程师，暂代工程负责人+区域经理；店长B=店长
#   店长B 为外部测试账号，无真实 userId，使用测试占位 userId "external-test-user"
# 测试群（用户确认 2026-08-13）：店长A=店长，工程师D=工程师
# 角色列表一律使用 userId；事件只提供 openDingtalkId，运行时经 USER_ID_MAP 映射后匹配。

# 当前生效的群配置文件（默认为生产配置；调用 set_groups_config() 可切换）
GROUPS_CONFIG_PATH = DEFAULT_GROUPS_CONFIG_PATH

# 是否处于测试模式（读取 group-test.json）
_is_test_mode = False


def _load_groups_config(path: Path | None = None) -> tuple[list[dict], dict[str, str]]:
    """从群配置文件加载群配置与 userId 映射。"""
    source = path or GROUPS_CONFIG_PATH
    if not source.exists():
        raise SystemExit(f"[config] 群配置文件缺失: {source}")
    raw = json.loads(source.read_text(encoding="utf-8"))
    groups = raw.get("groups") or []
    user_id_map = raw.get("user_id_map") or {}
    if not isinstance(groups, list) or not isinstance(user_id_map, dict):
        raise SystemExit(f"[config] 群配置格式错误: {source}")
    logger.info("群配置已从文件加载 path=%s groups=%d users=%d",
                source, len(groups), len(user_id_map))
    return groups, user_id_map


def set_groups_config(*, test: bool = False, path: str | None = None) -> None:
    """切换群配置来源。

    Args:
        test: True 使用测试配置文件 data/group-test.json。
        path: 显式指定群配置文件路径（优先于 test）；
              传空字符串/None 且非 test 时恢复默认生产配置。
    """
    global GROUPS_CONFIG_PATH, USER_ID_MAP, GROUPS, _is_test_mode
    if path:
        GROUPS_CONFIG_PATH = Path(path)
    elif test:
        GROUPS_CONFIG_PATH = GROUPS_TEST_CONFIG_PATH
    else:
        GROUPS_CONFIG_PATH = DEFAULT_GROUPS_CONFIG_PATH
    _is_test_mode = test
    GROUPS, USER_ID_MAP = _load_groups_config()
    _validate_groups()
    logger.info("群配置已切换 test=%s path=%s", _is_test_mode, GROUPS_CONFIG_PATH)


GROUPS, USER_ID_MAP = _load_groups_config()

# 备用：历史测试群成员（cidBkhYa...）通讯录已解析，正式角色配置时可参考：
#   工程师A(中级工程师)  oid=oid_engineer_a  userId=900000000000000002
#   工程师B(中级工程师)  oid=oid_engineer_b  userId=90000000000000001
#   工程总监A(工程总监)    oid=oid_director_a  userId=900000000000000001
#   设计主管A(设计主管)    oid=oid_designer_a  userId=90000000000000003

# 启动时校验：同一 userId 不得同时出现在店长和工程师列表
def _validate_groups() -> None:
    for g in GROUPS:
        overlap = set(g["manager_ids"]) & set(g["engineer_ids"])
        if overlap:
            raise SystemExit(
                f"[config] 角色重叠 group={g['group_id']} overlap={overlap}，"
                f"同一 openDingtalkId 不能同时是店长和工程师"
            )
        if not g["store_name"]:
            raise SystemExit(f"[config] 群 {g['group_id']} 缺少 store_name")


# ───────────────────────── 关键词 ─────────────────────────
# ⚠️ v3.0 硬编码常量——v4.0 起关键词和角色权限由语义协议 JSON 统一管理，
# 不再从 config 读取。以下常量在 Task 2 (keyword_matcher) 落成后废弃，
# 新代码统一走 protocol_loader 和 TicketProtocol。
REPORT_KEYWORD = "#报修"
DIAGNOSIS_KEYWORD = "#故障判断"
REPAIR_METHOD_KEYWORD = "#维修方式"
TIMEOUT_REASON_KEYWORD = "#超时原因"
COMPLETE_KEYWORD = "#完毕"

KEYWORDS = [REPORT_KEYWORD, DIAGNOSIS_KEYWORD, REPAIR_METHOD_KEYWORD,
            TIMEOUT_REASON_KEYWORD, COMPLETE_KEYWORD]

# 角色 → 允许关键词（v3.0 硬编码；v4.0 改从协议 allowed_roles/actions 派生）
ROLE_PERMISSIONS = {
    "MANAGER": [REPORT_KEYWORD, COMPLETE_KEYWORD],
    "ENGINEER": [DIAGNOSIS_KEYWORD, REPAIR_METHOD_KEYWORD, TIMEOUT_REASON_KEYWORD, COMPLETE_KEYWORD],
    "OTHER": [],
    "SYSTEM": [],
}

# ───────────────────────── 工单同步到 AI 表格看板 ─────────────────────────
# 把本地 tickets 增量同步到钉钉 AI 表格「报修工单」表（Kanban 看板数据源）。
# 用户决策 2026-08-14：新建「报修工单」表 + 工单看板视图，定期同步状态。
AITABLE_SYNC_ENABLED = _os.environ.get("AITABLE_SYNC_ENABLED", "true").lower() in ("true", "1", "yes")
AITABLE_SYNC_INTERVAL_SECONDS = int(_os.environ.get("AITABLE_SYNC_INTERVAL_SECONDS", "120"))
AITABLE_SYNC_BASE_ID = _os.environ.get("AITABLE_SYNC_BASE_ID", "base_demo_0001")
AITABLE_SYNC_TABLE_ID = _os.environ.get("AITABLE_SYNC_TABLE_ID", "6tIXGR3")
# 同步时自动删除线上已不存在的工单（镜像删除，本地库为真相源）
AITABLE_SYNC_PRUNE = _os.environ.get("AITABLE_SYNC_PRUNE", "true").lower() in ("true", "1", "yes")

# ───────────────────────── 计时参数 ─────────────────────────
SIDE_REPLY_TIMEOUT_HOURS = 4
WEEKEND_ESCALATION_DEFER_HOUR = 9
BACKGROUND_SCAN_INTERVAL_SECONDS = 60

# ───────────────────────── 响应 SLA（2026-08-24 需求 #4） ─────────────────────────
# 收到门店报修/需门店配合后：责任方须在 1 小时内响应；
# 超 1h AI 提醒责任方；超 4h 群内升级提醒责任方+升级对象（并单聊升级对象）。
# 工程师侧升级对象＝工程总监A（工程总监）；店长侧升级对象＝该群区域经理。
# 总开关（2026-08-26）：RESPONSE_SLA_ENABLED=false 时整条响应 SLA 链路静默
# （含 1h 提醒、4h 升级、单聊），不影响时效 SLA 与到货签收等其他提醒。
RESPONSE_SLA_ENABLED = _os.environ.get("RESPONSE_SLA_ENABLED", "true").lower() in (
    "true", "1", "yes", "on",
)


def _read_dotenv_value_live(key: str) -> str | None:
    """直接重读 .env 文件的当前值（不经过 _os.environ 缓存），供热切换使用。"""
    try:
        lines = _DOTENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k != key:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        return v
    return None


def is_response_sla_enabled() -> bool:
    """响应 SLA 是否启用（支持 .env 热修改，无需重启）。

    优先级：
    1. 单测中对 RESPONSE_SLA_ENABLED 的 monkeypatch 优先（检测 PYTEST 标识）；
    2. 若进程启动前外部环境已显式设置 RESPONSE_SLA_ENABLED（在 _INITIAL_ENV 中），
       则以外部环境为准（容器/ systemd 注入优先）；
    3. 否则实时重读 .env 文件当前值；
    4. 都不存在时回退到模块常量。
    """
    # 单测兼容：pytest 运行时 monkeypatch 对常量的赋值应优先生效
    if "PYTEST_CURRENT_TEST" in _os.environ:
        # 若当前常量与 live 文件值不一致，说明测试已 monkeypatch，尊重常量
        live_for_test = _read_dotenv_value_live("RESPONSE_SLA_ENABLED")
        if live_for_test is not None:
            live_bool = live_for_test.lower() in ("true", "1", "yes", "on")
            if bool(RESPONSE_SLA_ENABLED) != live_bool:
                return bool(RESPONSE_SLA_ENABLED)
    if "RESPONSE_SLA_ENABLED" in _INITIAL_ENV:
        return _INITIAL_ENV["RESPONSE_SLA_ENABLED"].lower() in ("true", "1", "yes", "on")
    live = _read_dotenv_value_live("RESPONSE_SLA_ENABLED")
    if live is not None:
        return live.lower() in ("true", "1", "yes", "on")
    return bool(RESPONSE_SLA_ENABLED)


def refresh_response_sla_enabled() -> bool:
    """将 RESPONSE_SLA_ENABLED 同步为当前 is_response_sla_enabled() 的值并返回。"""
    global RESPONSE_SLA_ENABLED
    RESPONSE_SLA_ENABLED = is_response_sla_enabled()
    return RESPONSE_SLA_ENABLED
RESPONSE_SLA_FIRST_HOURS = float(_os.environ.get("RESPONSE_SLA_FIRST_HOURS", "1"))
RESPONSE_SLA_ESCALATE_HOURS = float(_os.environ.get("RESPONSE_SLA_ESCALATE_HOURS", "4"))
# 工程师侧升级对象 userId（工程总监A）；本地无法核实姓名↔userId 映射，
# 默认回退用各群 engineering_leader_id；此处设置环境变量可全局覆盖。
RESPONSE_SLA_ENGINEER_ESCALATE_USER_ID = _os.environ.get(
    "RESPONSE_SLA_ENGINEER_ESCALATE_USER_ID", ""
)
RESPONSE_SLA_ENGINEER_ESCALATE_NAME = _os.environ.get(
    "RESPONSE_SLA_ENGINEER_ESCALATE_NAME", "工程总监A"
)
# 一次性分界（用户决策 2026-08-26）：此前建单的存量工单一律不参与响应 SLA，
# 仅分界时刻之后新建的工单纳入。分界取值晚于全部存量单的最后建单时间
# （2026-08-26 14:43:19）。存量工单自然完结后本条件恒空转；如需彻底移除该措施，
# 删除本常量并去掉 scheduler.scan_response_sla 中对应的过滤条件即可。
RESPONSE_SLA_EFFECTIVE_FROM = "2026-08-26 15:00:00"

# SLA 提醒：时效临近到期前 N 小时提醒一次；超时后再提醒一次
# （2026-08-30 用户决策：到期提示提前量由 1 小时改为 6 小时）
SLA_REMIND_BEFORE_HOURS = 6
SLA_SCAN_INTERVAL_SECONDS = 60

SLA_OPTIONS = {"1天": 1, "3天": 3, "7天": 7}

REPAIR_METHODS = [
    "淘宝采购后自行维修",
    "需要供应商维修",
    "需要木工维修",
    "需要工程师上门",
    "远程视频维修",
]

ORDER_NO_PLACEHOLDERS = {"无", "暂无", "稍后补", "不知道"}

# ───────────────────────── 云端模型（Task 4 §10.1） ─────────────────────────
# 通过环境变量注入，不在代码或日志中写入 API Key。
# 支持 OpenAI-compatible Chat Completions 接口。
import os as _os

LLM_BASE_URL = _os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = _os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_SECONDS = float(_os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
LLM_RESPONSE_FORMAT = _os.environ.get("LLM_RESPONSE_FORMAT", "auto").lower()
if LLM_RESPONSE_FORMAT not in {"auto", "json_schema", "json_object"}:
    raise ValueError(
        "LLM_RESPONSE_FORMAT 必须是 auto、json_schema 或 json_object"
    )
LLM_MAX_ATTEMPTS = int(_os.environ.get("LLM_MAX_ATTEMPTS", "3"))
LLM_RETRY_DELAYS_SECONDS = [2, 10]  # 计划书 §10.3

# API Key 只从环境变量读取，绝不写入日志或协议 JSON
LLM_API_KEY = _os.environ.get("LLM_API_KEY", "")

# 是否启用云端模型语义匹配（关闭时自然语言走降级路径）
LLM_ENABLED = _os.environ.get("LLM_ENABLED", "true").lower() in ("true", "1", "yes")

# ───────────────────────── 视觉模型（图片多模态解析） ─────────────────────────
# OpenAI 兼容的视觉模型（图片 base64 传入），支持按需切换供应商
VISION_ENABLED = _os.environ.get("VISION_ENABLED", "false").lower() in ("true", "1", "yes")
VISION_BASE_URL = _os.environ.get("VISION_BASE_URL", LLM_BASE_URL)
VISION_MODEL = _os.environ.get("VISION_MODEL", LLM_MODEL)
VISION_API_KEY = _os.environ.get("VISION_API_KEY", LLM_API_KEY)
VISION_TIMEOUT_SECONDS = float(_os.environ.get("VISION_TIMEOUT_SECONDS", "60"))
VISION_PROMPT = _os.environ.get(
    "VISION_PROMPT",
    "请描述这张图片中的内容，重点说明与设备故障/损坏相关的信息。"
    "如果看不出故障信息，如实说明。",
)

# ───────────────────────── 图片附件归档（计划书 §10.6 Task 4A · 存储层） ─────────────────────────
# 消息到达时只归档图片、不调用视觉模型；工单结束后统一分析（用户决策 2026-08-14）。
IMAGE_ARCHIVE_ENABLED = _os.environ.get("IMAGE_ARCHIVE_ENABLED", "true").lower() in ("true", "1", "yes")
# 注意：归档文件相对路径本身以 attachments/ 开头（save() 固定拼接），
# 因此根目录默认即 ARCHIVE_DIR，物理路径形如 archives/attachments/2026/08/20/<msgId>/0-<sha8>.jpg
IMAGE_ARCHIVE_DIR = Path(_os.environ.get("IMAGE_ARCHIVE_DIR", str(ARCHIVE_DIR)))
IMAGE_MAX_BYTES = int(_os.environ.get("IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))
IMAGE_MAX_COUNT_PER_MESSAGE = int(_os.environ.get("IMAGE_MAX_COUNT_PER_MESSAGE", "3"))
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = float(_os.environ.get("IMAGE_DOWNLOAD_TIMEOUT_SECONDS", "15"))
IMAGE_ALLOWED_MIME_TYPES = tuple(
    m.strip()
    for m in _os.environ.get(
        "IMAGE_ALLOWED_MIME_TYPES", "image/jpeg,image/png,image/webp,image/gif,image/bmp"
    ).split(",")
    if m.strip()
)
# 测试/开发可允许本地文件路径作为图片来源；生产保持拒绝
IMAGE_ALLOW_LOCAL_SOURCES = _os.environ.get("IMAGE_ALLOW_LOCAL_SOURCES", "false").lower() in ("true", "1", "yes")


def load_groups() -> list[dict]:
    """加载群配置（当前为常量，后续可改为文件/数据库来源）。"""
    _validate_groups()
    logger.info(
        "群配置加载完成 count=%d store_names=%s listener=%s",
        len(GROUPS),
        [g["store_name"] for g in GROUPS],
        LISTENER_USER_NAME,
    )
    return GROUPS


if __name__ == "__main__":
    # 快速自检：python config.py
    from logger import setup_logging

    setup_logging(level="INFO", log_dir=LOG_DIR)
    load_groups()
    print(json.dumps(GROUPS, ensure_ascii=False, indent=2))
