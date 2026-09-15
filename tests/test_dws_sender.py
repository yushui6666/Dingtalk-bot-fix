"""_dws_sender 命令构造与失败语义（2026-08-24 修复：失败必须抛异常）。"""

from __future__ import annotations

import os
import subprocess

import pytest

# main 在模块加载时会执行 _load_env_file() 把 .env 灌进 os.environ；
# 先快照再导入，测试后清理新增键，避免污染同进程后续测试
# （如 model_contract 的「无 API Key」场景）。
_PRE_IMPORT_ENV = frozenset(os.environ)

import main  # noqa: E402

_ENV_KEYS_ADDED = [k for k in os.environ if k not in _PRE_IMPORT_ENV]


@pytest.fixture(autouse=True)
def _cleanup_env_loaded_by_main():
    yield
    for key in _ENV_KEYS_ADDED:
        os.environ.pop(key, None)


class _Proc:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def test_user_target_builds_dash_user_command(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc(returncode=0)

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main._dws_sender("user:900000000000000001", "升级提醒")

    assert captured["cmd"] == [
        main.DWS_CMD, "chat", "message", "send",
        "--user", "900000000000000001", "--text", "升级提醒",
    ]


def test_group_target_builds_dash_group_command(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc(returncode=0)

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main._dws_sender("cidABC==", "群回执")

    assert captured["cmd"] == [
        main.DWS_CMD, "chat", "message", "send",
        "--group", "cidABC==", "--text", "群回执",
    ]


def test_nonzero_exit_raises_instead_of_silent_success(monkeypatch):
    """此前失败只记 warning 正常返回 → Outbox 误标 SENT；现在必须抛 RuntimeError。"""
    def fake_run(cmd, **kwargs):
        return _Proc(returncode=1, stderr='{"error": {"message": "cannot resolve --user"}}')

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cannot resolve"):
        main._dws_sender("user:123", "text")


def test_subprocess_failure_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="发送异常"):
        main._dws_sender("user:123", "text")
