"""pytest 路径配置与插件注册。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


# v4.0: 注册 asyncio 标记（Task 4 model_client 异步测试需要）
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")


@pytest.fixture(scope="session")
def _empty_dotenv(tmp_path_factory):
    """会话级空 .env 替身：令 _read_dotenv_value_live 对任意键恒返回 None。"""
    p = tmp_path_factory.mktemp("dotenv_isolated") / ".env"
    p.write_text("", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def isolate_response_sla_switch(monkeypatch, _empty_dotenv):
    """响应SLA总开关与宿主机环境解耦（2026-08-26 测试修复）。

    背景：config.is_response_sla_enabled() 综合三路来源——①模块常量、
    ②进程启动时外部环境（_INITIAL_ENV）、③项目 .env 实时值。测试若不显式
    钉住开关，结果随宿主机漂移：本机 .env=false 时启用路径 6 个测试必红，
    强制导出 RESPONSE_SLA_ENABLED=true 时停用路径 2 个测试反红，两套场景
    在任何单一配置下都无法同时全绿。

    本夹具把②③两路隔离为空，使开关完全由测试显式设置的
    ``config.RESPONSE_SLA_ENABLED`` 决定（回落到模块常量一路）：

    - 需要「开」（生产默认语义）：无需任何操作，本夹具默认置 True；
    - 需要「关」：monkeypatch.setattr(config, "RESPONSE_SLA_ENABLED", False)
      （现有 test_response_sla_disabled_suppresses_all_reminders /
      test_response_sla_reenabled_resumes_reminders 即此写法）。

    同时防跨测试污染：scan_response_sla 内部 refresh_response_sla_enabled()
    会把解析结果回写进模块常量，monkeypatch 保证每个用例结束后还原导入期
    状态。仅动测试层，生产行为零改动。
    """
    import os

    import config as config_mod

    monkeypatch.setattr(config_mod, "RESPONSE_SLA_ENABLED", True)
    monkeypatch.delenv("RESPONSE_SLA_ENABLED", raising=False)
    monkeypatch.delitem(config_mod._INITIAL_ENV, "RESPONSE_SLA_ENABLED", raising=False)
    monkeypatch.setattr(config_mod, "_DOTENV_PATH", _empty_dotenv)
