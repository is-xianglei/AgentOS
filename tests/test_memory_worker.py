"""Memory Worker 随 lifespan 运行的契约测试：不连数据库，Runner 与周期锁用替身。

- 唤醒后立即开始下一轮认领，不等轮询间隔；
- 关闭时停止认领、限时等待在途 Job，超时取消，之后交还本 Worker 的租约并关闭周期锁；
- 保留清理到点先抢周期锁，抢到才执行，游标从锁里读、清理成功后写回；没抢到或失败都等满一个周期；
- 进程内单例：start / stop / wake 的幂等与重复启动报错；lifespan 接线。
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from core.config import settings
from memory.jobs import worker as worker_module
from memory.jobs.maintenance_lock import LocalRetentionCoordinator
from memory.jobs.retention import MemoryRetentionResult
from memory.jobs.worker import MemoryWorker
from memory.repository import MemoryPhysicalDeleteCounts

pytestmark = pytest.mark.anyio


class _FakeRunner:
    """替身 Job Runner：按脚本返回每轮认领数，可让某一轮挂起模拟在途 Job。"""

    def __init__(self, claims: list[int] | None = None, *, hang: bool = False) -> None:
        self.worker_id = "test-worker"
        self.claims = list(claims or [])
        self.hang = hang
        self.calls = 0
        self.released = 0
        self.in_flight = asyncio.Event()
        self.cancelled = False
        self.claimed = asyncio.Event()

    async def run_once(self) -> int:
        self.calls += 1
        self.claimed.set()
        if self.hang:
            self.in_flight.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self.claims.pop(0) if self.claims else 0

    async def release_leases(self) -> int:
        self.released += 1
        return 1


@dataclass
class _FakeRetention:
    results: list[int] = field(default_factory=lambda: [7])
    starts: list[int] = field(default_factory=list)
    fail: bool = False

    async def run_once(self, *, after_space_id: int) -> MemoryRetentionResult:
        self.starts.append(after_space_id)
        if self.fail:
            raise RuntimeError("清理失败")
        return MemoryRetentionResult(
            spaces_scanned=1,
            counts=MemoryPhysicalDeleteCounts(),
            next_space_id=self.results.pop(0) if self.results else 0,
        )


class _RecordingCoordinator(LocalRetentionCoordinator):
    def __init__(self, *, grant: bool = True) -> None:
        super().__init__()
        self.grant = grant
        self.acquired_ttls: list[int] = []
        self.closed = False

    async def try_acquire(self, ttl_seconds: int) -> bool:
        self.acquired_ttls.append(ttl_seconds)
        return self.grant and await super().try_acquire(ttl_seconds)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def memory_worker_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_extraction_enabled", True)
    monkeypatch.setattr(settings, "memory_dream_enabled", True)
    monkeypatch.setattr(settings, "memory_retention_enabled", False)
    monkeypatch.setattr(settings, "memory_retention_interval_seconds", 3600)
    # 轮询间隔放长：测试里认领只能靠唤醒或首轮触发。
    monkeypatch.setattr(settings, "memory_worker_poll_seconds", 60.0)


def _worker(
    runner: _FakeRunner,
    *,
    retention: _FakeRetention | None = None,
    coordinator: _RecordingCoordinator | None = None,
) -> MemoryWorker:
    return MemoryWorker(
        runner=runner,  # type: ignore[arg-type]
        retention_runner=retention or _FakeRetention(),  # type: ignore[arg-type]
        coordinator=coordinator or _RecordingCoordinator(),
    )


async def _until(predicate, *, timeout: float = 2.0) -> None:
    """等到条件成立；超时说明行为不符合预期。"""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


# ---------- Job 消费与唤醒 ----------


async def test_wake_starts_next_claim_without_waiting_for_poll() -> None:
    runner = _FakeRunner()
    worker = _worker(runner)
    worker.start()
    try:
        await _until(lambda: runner.calls == 1)
        # 首轮空闲后进入 60 秒的轮询等待，只有唤醒能让它立刻再认领。
        await asyncio.sleep(0.01)
        assert runner.calls == 1
        worker.wake()
        await _until(lambda: runner.calls == 2)
    finally:
        await worker.stop()


async def test_busy_queue_claims_again_immediately() -> None:
    runner = _FakeRunner(claims=[3, 2])
    worker = _worker(runner)
    worker.start()
    try:
        # 认领到任务的两轮之后紧接着第三轮（空闲），全程不等轮询间隔。
        await _until(lambda: runner.calls == 3)
    finally:
        await worker.stop()


async def test_claim_failure_is_logged_and_loop_continues(caplog) -> None:
    class _FailingRunner(_FakeRunner):
        async def run_once(self) -> int:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("数据库暂不可用")
            return 0

    runner = _FailingRunner()
    worker = _worker(runner)
    worker.start()
    try:
        await _until(lambda: runner.calls == 1)
        worker.wake()
        await _until(lambda: runner.calls == 2)
    finally:
        await worker.stop()
    assert "Memory Worker认领任务失败" in caplog.text


async def test_extraction_and_dream_disabled_skips_claims(monkeypatch) -> None:
    monkeypatch.setattr(settings, "memory_extraction_enabled", False)
    monkeypatch.setattr(settings, "memory_dream_enabled", False)
    runner = _FakeRunner()
    worker = _worker(runner)
    worker.start()
    await asyncio.sleep(0.01)
    await worker.stop()
    assert runner.calls == 0


# ---------- 关闭 ----------


async def test_stop_waits_for_idle_worker_then_releases_and_closes() -> None:
    runner = _FakeRunner()
    coordinator = _RecordingCoordinator()
    worker = _worker(runner, coordinator=coordinator)
    worker.start()
    await _until(lambda: runner.calls == 1)

    await worker.stop(grace_seconds=1.0)

    assert runner.released == 1
    assert coordinator.closed is True


async def test_stop_cancels_in_flight_job_after_grace_then_releases() -> None:
    runner = _FakeRunner(hang=True)
    coordinator = _RecordingCoordinator()
    worker = _worker(runner, coordinator=coordinator)
    worker.start()
    await runner.in_flight.wait()

    await worker.stop(grace_seconds=0.01)

    assert runner.cancelled is True
    # 执行任务停下之后才交还租约，交还后不会再有本 Worker 的写入。
    assert runner.released == 1
    assert coordinator.closed is True


async def test_stop_release_failure_only_logs(caplog) -> None:
    class _ReleaseFails(_FakeRunner):
        async def release_leases(self) -> int:
            raise RuntimeError("连接已断开")

    coordinator = _RecordingCoordinator()
    worker = _worker(_ReleaseFails(), coordinator=coordinator)
    worker.start()
    await worker.stop(grace_seconds=1.0)

    assert "Memory Worker交还在途任务失败" in caplog.text
    assert coordinator.closed is True


async def test_stop_without_start_still_closes_coordinator() -> None:
    coordinator = _RecordingCoordinator()
    runner = _FakeRunner()
    await _worker(runner, coordinator=coordinator).stop()

    assert runner.released == 0
    assert coordinator.closed is True


async def test_start_twice_raises() -> None:
    worker = _worker(_FakeRunner())
    worker.start()
    try:
        with pytest.raises(RuntimeError, match="已经启动"):
            worker.start()
    finally:
        await worker.stop()


# ---------- 保留清理的周期锁 ----------


async def test_retention_runs_with_shared_cursor_when_lock_acquired(monkeypatch) -> None:
    monkeypatch.setattr(settings, "memory_retention_enabled", True)
    coordinator = _RecordingCoordinator()
    await coordinator.save_cursor(42)
    retention = _FakeRetention(results=[57])
    worker = _worker(_FakeRunner(), retention=retention, coordinator=coordinator)

    await worker._run_retention_if_due()

    # 锁的过期时间等于清理间隔；从共享游标继续，清理成功后写回下一批起点。
    assert coordinator.acquired_ttls == [3600]
    assert retention.starts == [42]
    assert await coordinator.load_cursor() == 57


async def test_retention_skipped_when_another_process_holds_lock(monkeypatch) -> None:
    monkeypatch.setattr(settings, "memory_retention_enabled", True)
    coordinator = _RecordingCoordinator(grant=False)
    retention = _FakeRetention()
    worker = _worker(_FakeRunner(), retention=retention, coordinator=coordinator)

    await worker._run_retention_if_due()
    # 没抢到锁也要等满一个周期再试，不在同一周期内反复抢。
    await worker._run_retention_if_due()

    assert coordinator.acquired_ttls == [3600]
    assert retention.starts == []


async def test_retention_failure_keeps_cursor_and_waits_full_period(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "memory_retention_enabled", True)
    coordinator = _RecordingCoordinator()
    await coordinator.save_cursor(42)
    retention = _FakeRetention(fail=True)
    worker = _worker(_FakeRunner(), retention=retention, coordinator=coordinator)

    await worker._run_retention_if_due()
    await worker._run_retention_if_due()

    assert retention.starts == [42]
    assert await coordinator.load_cursor() == 42
    assert "Memory保留策略清理失败" in caplog.text


async def test_retention_lock_error_does_not_stop_job_claims(monkeypatch, caplog) -> None:
    class _BrokenCoordinator(_RecordingCoordinator):
        async def try_acquire(self, ttl_seconds: int) -> bool:
            raise ConnectionError("Redis 不可达")

    monkeypatch.setattr(settings, "memory_retention_enabled", True)
    runner = _FakeRunner()
    worker = _worker(runner, coordinator=_BrokenCoordinator())
    worker.start()
    try:
        await _until(lambda: runner.calls == 1)
    finally:
        await worker.stop()
    assert "Memory保留策略清理失败" in caplog.text


async def test_retention_disabled_never_touches_lock() -> None:
    coordinator = _RecordingCoordinator()
    worker = _worker(_FakeRunner(), coordinator=coordinator)

    await worker._run_retention_if_due()

    assert coordinator.acquired_ttls == []


async def test_local_coordinator_blocks_until_ttl_elapses(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("memory.jobs.maintenance_lock.time.monotonic", lambda: clock[0])
    coordinator = LocalRetentionCoordinator()

    assert await coordinator.try_acquire(60) is True
    assert await coordinator.try_acquire(60) is False
    clock[0] += 60
    assert await coordinator.try_acquire(60) is True


# ---------- 进程内单例与 lifespan 接线 ----------


@pytest.fixture
def no_global_worker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(worker_module, "_worker", None)
    yield
    assert worker_module._worker is None


async def test_module_start_stop_and_wake(monkeypatch, no_global_worker) -> None:
    runner = _FakeRunner()
    coordinator = _RecordingCoordinator()
    monkeypatch.setattr(worker_module, "MemoryJobRunner", lambda **_: runner)
    monkeypatch.setattr(worker_module, "MemoryRetentionRunner", lambda **_: _FakeRetention())
    monkeypatch.setattr(worker_module, "build_retention_coordinator", lambda: coordinator)

    worker_module.start_memory_worker()
    with pytest.raises(RuntimeError, match="已经启动"):
        worker_module.start_memory_worker()
    await _until(lambda: runner.calls == 1)
    worker_module.wake_memory_worker()
    await _until(lambda: runner.calls == 2)
    await worker_module.stop_memory_worker()

    assert runner.released == 1
    assert coordinator.closed is True
    # 停止后再次停止、唤醒都不报错。
    await worker_module.stop_memory_worker()
    worker_module.wake_memory_worker()


def test_build_coordinator_requires_redis_url(monkeypatch) -> None:
    from core.errors import AgentException
    from memory.jobs.maintenance_lock import build_retention_coordinator

    monkeypatch.setattr(settings, "disable_redis_events", False)
    monkeypatch.setattr(settings, "redis_url", "")
    with pytest.raises(AgentException, match="AGENTOS_REDIS_URL"):
        build_retention_coordinator()

    monkeypatch.setattr(settings, "disable_redis_events", True)
    assert isinstance(build_retention_coordinator(), LocalRetentionCoordinator)


def test_lifespan_starts_worker_and_stops_it_after_executions() -> None:
    """lifespan 在 yield 前启动 Worker；退出时先停本进程执行，再停 Worker，之后才关 MCP 与 Redis。"""
    import inspect

    import main

    source = inspect.getsource(main.lifespan)
    assert source.index("start_memory_worker()") < source.index("yield")
    order = [
        source.index("await shutdown_executions()"),
        source.index("await stop_memory_worker()"),
        source.index("await close_mcp_runtime()"),
        source.index("await close_run_event_store()"),
    ]
    assert order == sorted(order)
