"""Q1 补充:实测 AsyncExitStack 与 TaskGroup 为什么都不能用于 effect 拆除。"""
import asyncio
import time
from contextlib import AsyncExitStack

log: list[str] = []


def cb(name: str, delay: float, fail: bool = False):
    async def _cb() -> None:
        await asyncio.sleep(delay)
        log.append(name)
        if fail:
            raise RuntimeError(f"{name} boom")
    _cb.__name__ = name
    return _cb


async def exitstack_timing() -> None:
    log.clear()
    stack = AsyncExitStack()
    for n in ("e1", "e2", "e3"):
        stack.push_async_callback(cb(n, 0.05))
    t0 = time.perf_counter()
    await stack.aclose()
    dt = time.perf_counter() - t0
    print(f"[ExitStack] 顺序={log} 耗时={dt:.3f}s → 纯串行 LIFO,无并发(3×0.05s 全额叠加)")
    assert log == ["e3", "e2", "e1"] and dt > 0.14


async def exitstack_error() -> None:
    log.clear()
    stack = AsyncExitStack()
    stack.push_async_callback(cb("e1", 0))
    stack.push_async_callback(cb("e2", 0, fail=True))
    stack.push_async_callback(cb("e3", 0))
    try:
        await stack.aclose()
        raise AssertionError("应抛 RuntimeError")
    except RuntimeError:
        print(f"[ExitStack] 中途抛错后仍执行完: {log};但异常形态是单异常+__context__ 链,"
              f"多个失败只会layer嵌套,不是 ExceptionGroup")
        assert log == ["e3", "e2", "e1"]


async def taskgroup_cancels_siblings() -> None:
    """TaskGroup:一个 effect 拆除立即抛错 → 兄弟 effect 被取消,其剩余 disposer 丢失。"""
    log.clear()

    async def fast_fail() -> None:
        raise RuntimeError("立即失败的 effect")

    async def slow_effect() -> None:
        try:
            await asyncio.sleep(0.05)
            log.append("slow-disposer-1")
            await asyncio.sleep(0.05)
            log.append("slow-disposer-2")
        except asyncio.CancelledError:
            log.append("slow-CANCELLED")
            raise

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(fast_fail())
            tg.create_task(slow_effect())
    except* RuntimeError:
        pass
    print(f"[TaskGroup] log={log} → 兄弟 effect 半途被取消,disposer 丢失;拆除路径禁用 TaskGroup")
    assert "slow-CANCELLED" in log and "slow-disposer-1" not in log


async def main() -> None:
    await exitstack_timing()
    await exitstack_error()
    await taskgroup_cancels_siblings()


asyncio.run(main())
