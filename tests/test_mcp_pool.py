"""MCP 连接池：专属 task 托管、容量上限与 LRU、全忙排队、空闲回收、失效与关闭，脱离 SDK 单测。"""

import asyncio
from contextlib import AsyncExitStack

import pytest

from harness.adapters.mcp.pool import (
    ConnectionPool,
    McpConnectFailed,
    McpConnectionLost,
    PoolKey,
)

pytestmark = pytest.mark.anyio


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeConn:
    """假连接：closed 记录托管 task 退出时是否执行了清理。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False


class Opener:
    def __init__(self, *, fail: Exception | None = None, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.opened: list[FakeConn] = []

    async def __call__(self, stack: AsyncExitStack) -> FakeConn:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        conn = FakeConn(f"c{len(self.opened)}")

        async def close() -> None:
            conn.closed = True

        stack.push_async_callback(close)
        self.opened.append(conn)
        return conn


def _pool(
    clock: FakeClock,
    *,
    max_connections: int = 2,
    idle: float = 300,
) -> ConnectionPool[FakeConn]:
    return ConnectionPool(
        max_connections=max_connections,
        idle_seconds=idle,
        connect_timeout=1.0,
        clock=clock,
    )


def _key(server_id: int, partition: str = "ws:1", rev: int = 1) -> PoolKey:
    return PoolKey(server_id=server_id, config_rev=rev, partition=partition)


async def test_reuses_connection_per_key_and_isolates_partitions() -> None:
    pool = _pool(FakeClock(), max_connections=4)
    opener = Opener()

    first = await pool.acquire(_key(1), opener)
    pool.release(first)
    second = await pool.acquire(_key(1), opener)
    pool.release(second)
    other_partition = await pool.acquire(_key(1, "user:2"), opener)
    pool.release(other_partition)
    new_rev = await pool.acquire(_key(1, rev=2), opener)
    pool.release(new_rev)

    assert first.client is second.client
    assert len({id(first.client), id(other_partition.client), id(new_rev.client)}) == 3
    assert pool.size == 3
    await pool.aclose()
    assert all(conn.closed for conn in opener.opened)


async def test_capacity_evicts_least_recently_used_idle() -> None:
    clock = FakeClock()
    pool = _pool(clock, max_connections=2)
    opener = Opener()

    a = await pool.acquire(_key(1), opener)
    pool.release(a)
    clock.now = 1
    b = await pool.acquire(_key(2), opener)
    pool.release(b)
    clock.now = 2
    c = await pool.acquire(_key(3), opener)
    pool.release(c)
    await asyncio.sleep(0)

    assert set(pool.keys()) == {_key(2), _key(3)}
    assert opener.opened[0].closed is True
    await pool.aclose()


async def test_waits_when_all_connections_busy() -> None:
    pool = _pool(FakeClock(), max_connections=1)
    opener = Opener()
    busy = await pool.acquire(_key(1), opener)

    waiter = asyncio.create_task(pool.acquire(_key(2), opener))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    assert pool.size == 1

    pool.release(busy)
    lease = await asyncio.wait_for(waiter, 1)
    assert lease.client.name == "c1"
    assert opener.opened[0].closed is True
    pool.release(lease)
    await pool.aclose()


async def test_idle_reaping_skips_busy_connections() -> None:
    clock = FakeClock()
    pool = _pool(clock, max_connections=4, idle=10)
    opener = Opener()
    idle = await pool.acquire(_key(1), opener)
    pool.release(idle)
    busy = await pool.acquire(_key(2), opener)

    clock.now = 11
    assert pool.reap_idle() == 1
    await asyncio.sleep(0)

    assert pool.keys() == [_key(2)]
    assert opener.opened[0].closed is True
    assert opener.opened[1].closed is False
    pool.release(busy)
    await pool.aclose()


async def test_reaper_task_closes_idle_connections() -> None:
    """后台回收任务按 idle_seconds 回收空闲连接（真实时钟，回收间隔为 idle 的一半）。"""
    pool: ConnectionPool[FakeConn] = ConnectionPool(
        max_connections=2,
        idle_seconds=0.05,
        connect_timeout=1.0,
    )
    opener = Opener()
    lease = await pool.acquire(_key(1), opener)
    pool.release(lease)

    for _ in range(50):
        await asyncio.sleep(0.02)
        if pool.size == 0:
            break

    assert pool.size == 0
    assert opener.opened[0].closed is True
    await pool.aclose()


async def test_connect_failure_is_reported_and_not_cached() -> None:
    pool = _pool(FakeClock())
    failing = Opener(fail=McpConnectFailed("地址不合法", retryable=False))

    with pytest.raises(McpConnectFailed, match="地址不合法") as info:
        await pool.acquire(_key(1), failing)
    assert info.value.retryable is False
    await asyncio.sleep(0)
    assert pool.size == 0

    with pytest.raises(McpConnectFailed, match="建立连接失败") as info:
        await pool.acquire(_key(1), Opener(fail=OSError("refused")))
    assert info.value.retryable is True

    ok = await pool.acquire(_key(1), Opener())
    pool.release(ok)
    await pool.aclose()


async def test_connect_timeout() -> None:
    pool: ConnectionPool[FakeConn] = ConnectionPool(
        max_connections=1,
        idle_seconds=300,
        connect_timeout=0.05,
    )

    with pytest.raises(McpConnectFailed, match="超时"):
        await pool.acquire(_key(1), Opener(delay=1))
    await asyncio.sleep(0)
    assert pool.size == 0
    await pool.aclose()


async def test_lease_run_detects_background_disconnect() -> None:
    """托管 task 先结束（连接在后台断开）时，调用方立即得到 McpConnectionLost，不等读超时。"""
    pool = _pool(FakeClock())
    lease = await pool.acquire(_key(1), Opener())
    started = asyncio.Event()

    async def hang() -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    call = asyncio.create_task(lease.run(hang()))
    await started.wait()
    lease._connection.task.cancel()  # 模拟连接在后台断开

    with pytest.raises(McpConnectionLost):
        await asyncio.wait_for(call, 1)
    pool.release(lease)
    await asyncio.sleep(0)
    assert pool.size == 0
    await pool.aclose()


async def test_invalidate_closes_idle_and_retires_busy() -> None:
    pool = _pool(FakeClock(), max_connections=4)
    opener = Opener()
    idle = await pool.acquire(_key(1, "ws:1"), opener)
    pool.release(idle)
    busy = await pool.acquire(_key(1, "user:2"), opener)
    other = await pool.acquire(_key(2), opener)
    pool.release(other)

    assert pool.invalidate(1) == 2
    await asyncio.sleep(0)
    assert pool.keys() == [_key(2)]
    assert opener.opened[0].closed is True
    assert opener.opened[1].closed is False  # 忙连接归还后才关

    pool.release(busy)
    await asyncio.sleep(0)
    assert opener.opened[1].closed is True
    await pool.aclose()


async def test_aclose_is_shielded_and_rejects_new_acquire() -> None:
    pool = _pool(FakeClock())
    opener = Opener()
    lease = await pool.acquire(_key(1), opener)
    pool.release(lease)

    closer = asyncio.create_task(pool.aclose())
    await asyncio.sleep(0)
    closer.cancel()
    await asyncio.gather(closer, return_exceptions=True)
    await asyncio.sleep(0.01)

    assert opener.opened[0].closed is True
    with pytest.raises(McpConnectFailed, match="已关闭"):
        await pool.acquire(_key(1), opener)


async def test_waiting_for_capacity_is_bounded() -> None:
    """全部连接都忙时排队等待，最多等 connect_timeout，到时报不可重试的建连失败。"""
    pool: ConnectionPool[FakeConn] = ConnectionPool(
        max_connections=1,
        idle_seconds=300,
        connect_timeout=0.1,
    )
    busy = await pool.acquire(_key(1), Opener())

    with pytest.raises(McpConnectFailed, match="等待空闲连接超时") as info:
        await pool.acquire(_key(2), Opener())
    assert info.value.retryable is False
    pool.release(busy)
    await pool.aclose()
