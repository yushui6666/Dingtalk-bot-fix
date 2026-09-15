"""停机时 asyncio 子进程 transport 清理回归测试。

背景（2026-08-26）：系统停止后在解释器退出阶段刷屏
「Exception ignored in BaseSubprocessTransport.__del__ … RuntimeError: Event loop is closed」。

根因：event_listener（dws listen-im）与图片媒体下载（dws download-media）
创建的子进程 transport 从不显式关闭；对象被异常回溯等引用链拖到事件循环
关闭之后才被 GC 回收，__del__ 里再关管道即报错。

契约：
- GroupListener._terminate_proc 终止子进程后必须显式关闭其 transport；
- run() 任务被取消（优雅停机路径）同样必须关闭；
- DingTalkMediaResolver 超时分支 kill 后必须 wait 回收，且任何退出路径
  （成功/失败/超时）都必须关闭 transport。
"""

import asyncio
import stat

import pytest

import event_listener
from event_listener import GroupListener
from images.archive import DingTalkMediaResolver

_GROUP = {"group_id": "cid-test", "store_name": "测试群"}


async def _spawn_spy(monkeypatch):
    """包装 asyncio.create_subprocess_exec，捕获测试期间创建的 Process。"""
    created = []
    orig = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await orig(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return created


def _transport_closed(proc) -> bool:
    return bool(proc._transport.is_closing())


def _write_script(tmp_path, body: str):
    script = tmp_path / "fake_dws.sh"
    script.write_text("#!/bin/sh\n" + body)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


@pytest.mark.asyncio
async def test_terminate_proc_closes_subprocess_transport():
    lst = GroupListener(_GROUP, handler=None)
    proc = await asyncio.create_subprocess_exec(
        "/bin/sleep", "30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    lst._proc = proc
    await lst._terminate_proc()

    assert proc.returncode is not None
    assert _transport_closed(proc), "终止后未显式关闭子进程 transport"


@pytest.mark.asyncio
async def test_run_task_cancel_closes_listener_transport(monkeypatch):
    created = await _spawn_spy(monkeypatch)
    monkeypatch.setattr(
        event_listener, "_build_listen_cmd",
        lambda group_id, chat_query=None: ["/bin/sleep", "30"],
    )

    lst = GroupListener(_GROUP, handler=None)
    task = asyncio.create_task(lst.run())
    try:
        for _ in range(200):
            if created:
                break
            await asyncio.sleep(0.01)
        assert created, "监听子进程未启动"
        await asyncio.sleep(0.05)  # 进入 _consume 等待 stdout 的阻塞态
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    proc = created[0]
    assert proc.returncode is not None
    assert _transport_closed(proc), "取消停机路径未关闭子进程 transport"


@pytest.mark.asyncio
async def test_media_resolver_timeout_reaps_and_closes_transport(tmp_path, monkeypatch):
    created = await _spawn_spy(monkeypatch)
    cmd_path = _write_script(tmp_path, "sleep 5\n")
    resolver = DingTalkMediaResolver(
        dws_cmd=cmd_path, timeout_seconds=0.2, tmp_dir=tmp_path)

    with pytest.raises(ValueError, match="超时"):
        await resolver.resolve("$abc123", "msg1234567890", "cid001")

    proc = created[0]
    for _ in range(100):  # 子进程回收由 child watcher 异步完成，留宽限
        if proc.returncode is not None:
            break
        await asyncio.sleep(0.01)
    assert proc.returncode is not None, "超时 kill 后未 wait 回收子进程"
    assert _transport_closed(proc), "超时分支未关闭子进程 transport"


@pytest.mark.asyncio
async def test_media_resolver_success_closes_transport(tmp_path, monkeypatch):
    created = await _spawn_spy(monkeypatch)
    body = (
        'out=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "--output" ]; then out="$2"; fi\n'
        '  shift\n'
        'done\n'
        'printf x > "$out"\n'
        "echo '{\"success\": true}'\n"
    )
    cmd_path = _write_script(tmp_path, body)
    resolver = DingTalkMediaResolver(
        dws_cmd=cmd_path, timeout_seconds=5, tmp_dir=tmp_path)

    data = await resolver.resolve("$abc123", "msg1234567890", "cid001")
    assert data == b"x"
    assert _transport_closed(created[0]), "成功路径未关闭子进程 transport"
