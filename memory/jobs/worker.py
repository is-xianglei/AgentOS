"""Memory 后台 Worker：随 API 进程的 lifespan 启动和关闭，每个进程各跑一份。

- Job 消费：每个进程都认领，memory_jobs 的 SKIP LOCKED 与租约保证同一个 Job 只由一个进程执行；
- 保留清理：每个进程到点都去抢周期锁，抢到的才执行，全集群每个周期只清理一次（maintenance_lock.py）；
- 关闭：停止认领，限时等待在途 Job，超时就取消，再把本进程认领的 Job 交还队列，其他进程可立即接手。
"""

import asyncio
import contextlib
import logging
import time

from core.config import settings
from database.engine import AsyncSessionLocal
from memory.jobs.maintenance_lock import RetentionCoordinator, build_retention_coordinator
from memory.jobs.retention import MemoryRetentionRunner
from memory.jobs.service import MemoryJobRunner

logger = logging.getLogger(__name__)

# 关闭时等待在途 Job 的上限，与 runtime/execution.py 停止本进程执行的等待时长一致。
STOP_GRACE_SECONDS = 5.0


class MemoryWorker:
    """持续认领并处理 memory_jobs，按周期执行保留清理。"""

    def __init__(
        self,
        *,
        runner: MemoryJobRunner,
        retention_runner: MemoryRetentionRunner,
        coordinator: RetentionCoordinator,
    ) -> None:
        self.runner = runner
        self.retention_runner = retention_runner
        self.coordinator = coordinator
        self._next_retention_at = 0.0
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Memory Worker已经启动")
        self._task = asyncio.create_task(self._run(), name="memory-worker")

    def wake(self) -> None:
        """有新 Job 入队时立即开始下一轮认领，不等轮询间隔。"""
        self._wake_event.set()

    async def stop(self, grace_seconds: float = STOP_GRACE_SECONDS) -> None:
        """停止认领并限时等待在途 Job；超时就取消，再交还本 Worker 认领的 Job，最后关闭 Redis 连接。"""
        try:
            task = self._task
            if task is None:
                return
            self._stop_event.set()
            self._wake_event.set()
            done, _ = await asyncio.wait({task}, timeout=grace_seconds)
            if not done:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # 执行任务已经停止，交还之后本 Worker 不会再写这些 Job。
            await self._release_leases()
        finally:
            await self.coordinator.aclose()

    async def _release_leases(self) -> None:
        worker_id = self.runner.worker_id
        try:
            released = await self.runner.release_leases()
        except Exception:
            logger.exception(
                "Memory Worker交还在途任务失败，任务将等待Lease到期后重新认领",
                extra={"worker_id": worker_id},
            )
            return
        if released:
            logger.info(
                "Memory Worker已交还在途任务",
                extra={"worker_id": worker_id, "released_count": released},
            )

    async def _run(self) -> None:
        worker_id = self.runner.worker_id
        logger.info("Memory Worker已启动", extra={"worker_id": worker_id})
        while not self._stop_event.is_set():
            # 保留清理不受提取/Dream 开关影响，两者都关闭时仍按周期执行。
            await self._run_retention_if_due()
            if not (settings.memory_extraction_enabled or settings.memory_dream_enabled):
                await self._wait_for_next_poll()
                continue
            try:
                claimed = await self.runner.run_once()
            except Exception:
                logger.exception("Memory Worker认领任务失败", extra={"worker_id": worker_id})
                await self._wait_for_next_poll()
                continue
            # 本轮认领到任务时立即进入下一轮，队列空闲才等待。
            if claimed == 0:
                await self._wait_for_next_poll()
        logger.info("Memory Worker已停止", extra={"worker_id": worker_id})

    async def _run_retention_if_due(self) -> None:
        """到点后抢周期锁，抢到才清理一批；失败只记日志，不阻断 Job 消费。"""
        if not settings.memory_retention_enabled:
            return
        now = time.monotonic()
        if now < self._next_retention_at:
            return
        interval = settings.memory_retention_interval_seconds
        # 先推进下次检查时间：没抢到锁或本次失败，都等满一个周期再试。
        self._next_retention_at = now + interval
        try:
            # 锁的过期时间等于清理间隔，执行完不释放，本周期内其他进程都抢不到。
            if not await self.coordinator.try_acquire(interval):
                logger.debug("本周期的Memory保留清理已由其他进程执行")
                return
            after_space_id = await self.coordinator.load_cursor()
            result = await self.retention_runner.run_once(after_space_id=after_space_id)
            # 清理批次提交成功后才推进游标，失败时下个周期重扫同一批 Space。
            await self.coordinator.save_cursor(result.next_space_id)
        except Exception:
            logger.exception("Memory保留策略清理失败", extra={"worker_id": self.runner.worker_id})
            return
        counts = result.counts
        logger.info(
            "Memory保留策略清理完成",
            extra={
                "spaces_scanned": result.spaces_scanned,
                "items_deleted": counts.items,
                "revisions_deleted": counts.revisions,
                "sources_deleted": counts.sources,
                "jobs_deleted": counts.jobs,
            },
        )

    async def _wait_for_next_poll(self) -> None:
        """等待一个轮询间隔；期间被唤醒或收到停止请求会立即返回。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._wake_event.wait(),
                timeout=settings.memory_worker_poll_seconds,
            )
        self._wake_event.clear()


_worker: MemoryWorker | None = None


def start_memory_worker() -> None:
    """lifespan 启动时调用：在本进程起一个 Memory Worker。"""
    global _worker
    if _worker is not None:
        raise RuntimeError("本进程的Memory Worker已经启动")
    worker = MemoryWorker(
        runner=MemoryJobRunner(session_factory=AsyncSessionLocal),
        retention_runner=MemoryRetentionRunner(session_factory=AsyncSessionLocal),
        coordinator=build_retention_coordinator(),
    )
    worker.start()
    _worker = worker


async def stop_memory_worker() -> None:
    """lifespan 退出时调用；本进程没有启动过 Worker 时什么也不做。"""
    global _worker
    worker, _worker = _worker, None
    if worker is not None:
        await worker.stop()


def wake_memory_worker() -> None:
    """唤醒本进程的 Memory Worker；没有运行中的 Worker 时什么也不做（如测试、脚本）。"""
    if _worker is not None:
        _worker.wake()
