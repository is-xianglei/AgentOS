import asyncio
import logging
import signal
import time

from core.config import settings
from db.engine import AsyncSessionLocal
from services.memory_job_service import MemoryJobRunner
from services.memory_retention_service import MemoryRetentionRunner

logger = logging.getLogger(__name__)


class MemoryWorker:
    """持续认领并处理 PostgreSQL 中的 Memory Job。"""

    def __init__(
        self,
        runner: MemoryJobRunner | None = None,
        retention_runner: MemoryRetentionRunner | None = None,
    ) -> None:
        if runner is None:
            runner = MemoryJobRunner(session_factory=AsyncSessionLocal)
        self.runner = runner
        self.retention_runner = retention_runner or MemoryRetentionRunner(
            session_factory=getattr(runner, "session_factory", AsyncSessionLocal)
        )
        self._next_retention_at = 0.0
        self.stop_event = asyncio.Event()

    async def run(self) -> None:
        """收到终止信号后停止 Claim，并让当前 Job 尽力完成。"""
        self._install_signal_handlers()
        logger.info("Memory Worker已启动", extra={"worker_id": self.runner.worker_id})
        while not self.stop_event.is_set():
            await self._run_retention_if_due()
            extraction_enabled = settings.memory_extraction_enabled
            dream_enabled = settings.memory_dream_enabled
            if not (extraction_enabled or dream_enabled):
                await self._wait_for_next_poll()
                continue
            try:
                claimed = await self.runner.run_once()
            except Exception:
                logger.exception("Memory Worker认领任务失败")
                await self._wait_for_next_poll()
                continue
            if claimed == 0:
                await self._wait_for_next_poll()
        logger.info("Memory Worker已停止", extra={"worker_id": self.runner.worker_id})

    def request_stop(self) -> None:
        self.stop_event.set()

    async def _run_retention_if_due(self) -> None:
        """按单调时钟节流维护任务，失败不阻断 Job 消费。"""
        if not settings.memory_retention_enabled:
            return
        now = time.monotonic()
        if now < self._next_retention_at:
            return
        self._next_retention_at = now + settings.memory_retention_interval_seconds
        try:
            result = await self.retention_runner.run_once()
        except Exception:
            logger.exception("Memory保留策略清理失败")
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
        try:
            await asyncio.wait_for(
                self.stop_event.wait(),
                timeout=settings.memory_worker_poll_seconds,
            )
        except TimeoutError:
            pass

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_stop)
            except NotImplementedError:
                signal.signal(signum, lambda *_: self.request_stop())


async def main() -> None:
    await MemoryWorker().run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
