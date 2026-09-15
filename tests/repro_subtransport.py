"""复现脚本：未关闭的子进程 transport 存活到事件循环关闭之后。

对应生产现象（2026-08-26）：日志打印「系统停止」、进程退出阶段刷屏
「Exception ignored in BaseSubprocessTransport.__del__ … RuntimeError: Event loop is closed」。

机制：子进程 transport 及其管道从未显式关闭，且被引用链（异常回溯环 /
残留任务等）拖到 asyncio.run 关闭循环之后才被 GC；__del__ 里再关管道时
循环已关闭，即报错。本脚本用全局引用模拟这条存活链，用短命子进程避免
遗留僵尸进程。

用法：
    python3.12 repro_subtransport.py no-close   # 预期刷屏 RuntimeError: Event loop is closed
    python3.12 repro_subtransport.py close      # 显式关闭后预期安静
"""
import asyncio
import gc
import sys

LEAK = []


async def one(mode: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "/bin/sleep", "1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if mode == "close":            # 新代码行为：用完立即显式关闭 transport
        transport = getattr(proc, "_transport", None)
        if transport is not None and not transport.is_closing():
            transport.close()
    else:                          # 旧代码行为：从不关闭，靠引用链拖过关机
        LEAK.append(proc)


async def main(mode: str) -> None:
    await asyncio.gather(*(one(mode) for _ in range(5)))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "no-close"
    asyncio.run(main(mode))        # 返回时事件循环已关闭
    print(f"loop closed; python={sys.version.split()[0]} mode={mode}")
    LEAK.clear()
    gc.collect()                   # 此刻才回收仍存活的 transport
    print("done")
