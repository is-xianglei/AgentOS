from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from db.engine import AsyncSessionLocal
from memory.repository import MemoryPhysicalDeleteCounts, MemoryRepository

_ARCHIVED_ITEM_DAYS_KEY = "archived_item_retention_days"
_TERMINAL_JOB_DAYS_KEY = "terminal_job_retention_days"
_MAX_RETENTION_DAYS = 36_500


@dataclass(frozen=True)
class MemoryRetentionResult:
    """一次保留策略扫描的范围和物理删除计数。"""

    spaces_scanned: int
    counts: MemoryPhysicalDeleteCounts
    next_space_id: int = 0


class MemoryRetentionService:
    """按 Space 设置或全局默认值批量执行 Memory 保留策略。"""

    def __init__(self, db: AsyncSession):
        self.repo = MemoryRepository(db)

    async def cleanup_batch(
        self,
        *,
        now: datetime | None = None,
        batch_size: int | None = None,
        space_id: int | None = None,
        after_space_id: int = 0,
    ) -> MemoryRetentionResult:
        """分别限制 Item 和 Job 删除批量，并扫描所有 Space 防止饥饿。"""
        effective_now = now or datetime.now(UTC)
        effective_batch_size = (
            batch_size if batch_size is not None else settings.memory_retention_batch_size
        )
        if effective_batch_size < 1:
            raise ValueError("Memory保留策略批量大小必须大于0")

        remaining_items = effective_batch_size
        remaining_jobs = effective_batch_size
        counts = MemoryPhysicalDeleteCounts()
        spaces_scanned = 0
        current_space_id = after_space_id
        page_size = min(max(effective_batch_size, 100), 500)

        while remaining_items > 0 or remaining_jobs > 0:
            spaces = await self.repo.list_spaces_for_retention(
                after_space_id=current_space_id,
                limit=page_size,
                space_id=space_id,
            )
            if not spaces:
                break
            for space in spaces:
                spaces_scanned += 1
                current_space_id = space.id
                if remaining_items > 0:
                    item_days = self._retention_days(
                        space.settings,
                        _ARCHIVED_ITEM_DAYS_KEY,
                        settings.memory_archived_item_retention_days,
                    )
                    item_counts = await self.repo.purge_archived_items_before(
                        space.id,
                        cutoff=effective_now - timedelta(days=item_days),
                        limit=remaining_items,
                    )
                    remaining_items -= item_counts.items
                    counts += item_counts
                if remaining_jobs > 0:
                    job_days = self._retention_days(
                        space.settings,
                        _TERMINAL_JOB_DAYS_KEY,
                        settings.memory_terminal_job_retention_days,
                    )
                    job_counts = await self.repo.purge_terminal_jobs_before(
                        space.id,
                        cutoff=effective_now - timedelta(days=job_days),
                        limit=remaining_jobs,
                    )
                    remaining_jobs -= job_counts.jobs
                    counts += job_counts
                if remaining_items == 0 and remaining_jobs == 0:
                    return MemoryRetentionResult(
                        spaces_scanned=spaces_scanned,
                        counts=counts,
                        next_space_id=0 if space_id is not None else current_space_id,
                    )
            if len(spaces) < page_size:
                break

        return MemoryRetentionResult(
            spaces_scanned=spaces_scanned,
            counts=counts,
            next_space_id=0,
        )

    @staticmethod
    def _retention_days(
        space_settings: dict[str, Any],
        key: str,
        default: int,
    ) -> int:
        value = space_settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        if not 0 <= value <= _MAX_RETENTION_DAYS:
            return default
        return value


class MemoryRetentionRunner:
    """为独立 Worker 提供一次一事务的保留策略执行入口。"""

    def __init__(self, *, session_factory: Any = AsyncSessionLocal) -> None:
        self.session_factory = session_factory
        self._after_space_id = 0

    async def run_once(self) -> MemoryRetentionResult:
        async with self.session_factory() as db:
            try:
                result = await MemoryRetentionService(db).cleanup_batch(
                    after_space_id=self._after_space_id,
                )
                await db.commit()
                self._after_space_id = result.next_space_id
                return result
            except Exception:
                await db.rollback()
                raise
