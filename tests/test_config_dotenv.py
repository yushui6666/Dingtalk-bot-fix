"""config._load_dotenv：无依赖 .env 加载器（2026-08-25）。

规则：只填充当前进程环境中不存在的键（真实环境变量优先）；
支持整行 # 注释与成对引号剥离；文件不存在时静默返回空。
"""

import os
from pathlib import Path

from config import _load_dotenv


def test_load_dotenv_sets_missing_keys(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# 注释行\nA_KEY=1\nB_KEY=\"quoted value\"\nC_KEY='single'\nBAD_LINE_NO_EQUALS\n",
        encoding="utf-8",
    )
    for k in ("A_KEY", "B_KEY", "C_KEY"):
        os.environ.pop(k, None)
    loaded = _load_dotenv(p)
    assert loaded == {"A_KEY": "1", "B_KEY": "quoted value", "C_KEY": "single"}
    assert os.environ["A_KEY"] == "1"
    assert os.environ["B_KEY"] == "quoted value"
    assert os.environ["C_KEY"] == "single"


def test_load_dotenv_real_env_wins(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("D_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("D_KEY", "from_env")
    loaded = _load_dotenv(p)
    assert "D_KEY" not in loaded
    assert os.environ["D_KEY"] == "from_env"


def test_load_dotenv_missing_file_is_silent(tmp_path):
    loaded = _load_dotenv(Path(tmp_path) / "nope.env")
    assert loaded == {}
