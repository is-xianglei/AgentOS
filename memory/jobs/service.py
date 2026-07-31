import logging
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import settings
from core.errors import AgentException
from database.engine import AsyncSessionLocal
from llm.client import LLMClient
from memory.models import MemoryJobRecord
from session.models import SessionRecord, SessionTurnRecord
from memory.jobs.repository import MemoryJobRepository
from memory.repository import MemoryRepository
from memory.jobs.dream import (
    DreamApplyResult,
    MemoryDreamFailure,
    MemoryDreamResult,
    MemoryDreamService,
    PreparedDream,
)
from memory.jobs.extraction import (
    MemoryApplyResult,
    MemoryExtractionFailure,
    MemoryExtractionResult,
    MemoryExtractionService,
    PreparedMemoryExtraction,
)
from memory.rollout_service import MemoryRolloutService

logger = logging.getLogger(__name__)

_RETRY_DELAYS_SECONDS = (5, 30, 120, 600, 1800)


class MemoryJobService:
    """在业务事务中可靠创建 Memory Job。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.job_repo = MemoryJobRepository(db)
        self.memory_repo = MemoryRepository(db)

    async def enqueue_extract(
        self,
        session: SessionRecord,
        turn: SessionTurnRecord,
    ) -> MemoryJobRecord:
        """为真正完成的 Turn 幂等入队一次提取任务。"""
        if (
            turn.status != "completed"
            or turn.completed_message_id is None
            or turn.session_id != session.id
            or session.user_id != turn.user_id
            or session.workspace_id != turn.workspace_id
        ):
            # Memory 是可降级旁路，业务校验失败必须用 AgentException，避免打断主对话事务。
            raise AgentException.message(
                "只有归属一致且已完成的交互轮次才能创建提取Job",
                {
                    "session_id": session.id,
                    "turn_id": str(turn.id),
                    "turn_status": turn.status,
                },
            )

        space = await self.memory_repo.get_or_create_space_for_update(
            turn.workspace_id,
            turn.user_id,
        )
        prompt_version = settings.memory_extractor_prompt_version
        mode = MemoryRolloutService.extraction_mode()
        idempotency_key = f"extract:{session.id}:{turn.id}:{prompt_version}"
        if mode == "shadow":
            # 有意偏离文档 §9.1 的幂等键格式：shadow Job 只记录对比摘要、不写 Memory 数据，
            # 追加后缀使灰度切换窗口内同一 Turn 的 active 提取不被 shadow 记录挡住而丢失。
            idempotency_key = f"{idempotency_key}:shadow"
        job = await self.job_repo.enqueue_extract(
            space_id=space.id,
            session_id=session.id,
            turn_id=turn.id,
            idempotency_key=idempotency_key,
            max_attempts=settings.memory_job_max_attempts,
            model=settings.memory_extractor_model or settings.anthropic_model,
            prompt_version=prompt_version,
            base_catalog_version=space.catalog_version,
            payload={"mode": mode},
        )
        return job

    async def record_extract_success_and_maybe_enqueue_dream(
        self,
        job: MemoryJobRecord,
        *,
        completed_at: datetime,
    ) -> MemoryJobRecord | None:
        """按不同 Session 计数，并在同一事务内执行 Dream 五项门控。"""
        if job.job_type != "extract" or job.session_id is None:
            raise RuntimeError("只有来源完整的提取Job才能推进Dream门控")

        space = await self.memory_repo.get_space_for_worker(job.space_id, for_update=True)
        if space is None:
            raise RuntimeError("提取Job所属Memory空间不存在")
        since = space.last_dream_at or datetime.min.replace(tzinfo=UTC)
        already_counted = await self.job_repo.has_succeeded_extract_for_session_since(
            space.id,
            job.session_id,
            since,
        )
        if not already_counted:
            await self.memory_repo.increment_sessions_since_dream(
                space.workspace_id,
                space.user_id,
                space.id,
            )

        if not settings.memory_dream_enabled:
            return None
        if not MemoryRolloutService.dream_enabled(space.user_id):
            return None
        active_count = await self.memory_repo.count_active_items(
            space.workspace_id,
            space.user_id,
            space.id,
        )
        if active_count < settings.memory_dream_min_items:
            return None
        if space.sessions_since_dream < settings.memory_dream_min_sessions:
            return None
        if (
            space.last_dream_at is not None
            and (completed_at - space.last_dream_at).total_seconds()
            < settings.memory_dream_interval_seconds
        ):
            return None
        if (
            space.last_scan_at is not None
            and (completed_at - space.last_scan_at).total_seconds()
            < settings.memory_dream_scan_interval_seconds
        ):
            return None

        await self.memory_repo.mark_dream_scan(
            space.workspace_id,
            space.user_id,
            space,
            scanned_at=completed_at,
        )
        if await self.job_repo.has_live_dream(space.id):
            return None

        scan_window_seconds = max(settings.memory_dream_scan_interval_seconds, 1)
        scan_window = int(completed_at.timestamp()) // scan_window_seconds
        prompt_version = settings.memory_dream_prompt_version
        idempotency_key = f"dream:{space.id}:{space.catalog_version}:{prompt_version}:{scan_window}"
        job = await self.job_repo.enqueue_dream(
            space_id=space.id,
            idempotency_key=idempotency_key,
            max_attempts=settings.memory_job_max_attempts,
            model=(
                settings.memory_dream_model
                or settings.memory_extractor_model
                or settings.anthropic_model
            ),
            prompt_version=prompt_version,
            base_catalog_version=space.catalog_version,
            payload={},
        )
        return job


class MemoryJobRunner:
    """用明确的短事务边界执行可恢复 Memory Job。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        llm: LLMClient | None = None,
        worker_id: str | None = None,
        claim_scope: tuple[int, int] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.llm = llm or LLMClient()
        self.worker_id = worker_id or self._default_worker_id()
        self.claim_scope = claim_scope

    async def run_once(self, *, job_id: UUID | None = None) -> int:
        """认领一批 Job 并逐个执行，返回本轮认领数量。"""
        claimed_jobs = await self._claim(job_id=job_id)
        for claimed_job_id, job_type in claimed_jobs:
            await self._execute_claimed(claimed_job_id, job_type)
        return len(claimed_jobs)

    async def _claim(self, *, job_id: UUID | None) -> tuple[tuple[UUID, str], ...]:
        """优先认领提取任务；队列为空时每次只认领少量 Dream。"""
        if job_id is not None:
            return await self._claim_type(
                job_type="extract",
                lease_seconds=settings.memory_job_lease_seconds,
                limit=1,
                job_id=job_id,
                payload_mode=None,
            )

        if settings.memory_extraction_enabled:
            extracted = await self._claim_type(
                job_type="extract",
                lease_seconds=settings.memory_job_lease_seconds,
                limit=settings.memory_job_claim_batch_size,
                job_id=None,
                payload_mode=None,
            )
            if extracted:
                return extracted
        if settings.memory_dream_enabled:
            return await self._claim_type(
                job_type="dream",
                lease_seconds=settings.memory_dream_lease_seconds,
                limit=1,
                job_id=None,
                payload_mode=None,
            )
        return ()

    async def _claim_type(
        self,
        *,
        job_type: str,
        lease_seconds: float,
        limit: int,
        job_id: UUID | None,
        payload_mode: str | None = None,
    ) -> tuple[tuple[UUID, str], ...]:
        """短事务 Claim，提交后才允许进入读取和模型调用阶段。"""
        async with self.session_factory() as db:
            try:
                workspace_id, user_id = self.claim_scope or (None, None)
                jobs = await MemoryJobRepository(db).claim_available(
                    worker_id=self.worker_id,
                    lease_seconds=lease_seconds,
                    limit=limit,
                    job_id=job_id,
                    job_type=job_type,
                    payload_mode=payload_mode,
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
                claimed = tuple((job.id, job.job_type) for job in jobs)
                await db.commit()
                return claimed
            except Exception:
                await db.rollback()
                raise

    async def _execute_claimed(self, job_id: UUID, job_type: str) -> bool:
        if job_type == "dream":
            return await self._execute_dream_claimed(job_id)

        started_at = time.monotonic()
        try:
            prepared = await self._prepare(job_id)
            result = await self._extract_without_transaction(prepared)
            applied = await self._apply(
                job_id,
                result,
                mode=prepared.mode,
            )
            logger.info(
                "Memory提取任务执行成功",
                extra={
                    "job_id": str(job_id),
                    "created_count": len(applied.created_ids),
                    "updated_count": len(applied.updated_ids),
                    "rejected_count": applied.rejected_count,
                    "duration_seconds": round(time.monotonic() - started_at, 6),
                },
            )
            return True
        except MemoryExtractionFailure as exc:
            await self._record_failure(job_id, exc)
            logger.warning(
                "Memory提取任务执行失败",
                extra={
                    "job_id": str(job_id),
                    "error_code": exc.code,
                    "retryable": exc.retryable,
                    "duration_seconds": round(time.monotonic() - started_at, 6),
                },
            )
            return False
        except Exception as exc:
            failure = MemoryExtractionFailure(
                "unexpected_error",
                type(exc).__name__,
                retryable=True,
            )
            await self._record_failure(job_id, failure)
            logger.exception(
                "Memory提取任务发生未预期异常",
                extra={
                    "job_id": str(job_id),
                    "error_type": type(exc).__name__,
                    "duration_seconds": round(time.monotonic() - started_at, 6),
                },
            )
            return False

    async def _execute_dream_claimed(self, job_id: UUID) -> bool:
        started_at = time.monotonic()
        try:
            prepared = await self._prepare_dream(job_id)
            result = await self._dream_without_transaction(prepared)
            applied = await self._apply_dream(job_id, prepared, result)
            logger.info(
                "Memory Dream任务执行成功",
                extra={
                    "job_id": str(job_id),
                    "changed_count": len(applied.changed_item_ids),
                    "operation_count": applied.operation_count,
                    "duration_seconds": round(time.monotonic() - started_at, 6),
                },
            )
            return True
        except MemoryDreamFailure as exc:
            await self._record_failure(job_id, exc)
            logger.warning(
                "Memory Dream任务执行失败",
                extra={
                    "job_id": str(job_id),
                    "error_code": exc.code,
                    "retryable": exc.retryable,
                    "duration_seconds": round(time.monotonic() - started_at, 6),
                },
            )
            return False
        except Exception as exc:
            failure = MemoryDreamFailure(
                "unexpected_error",
                type(exc).__name__,
                retryable=True,
            )
            await self._record_failure(job_id, failure)
            logger.exception(
                "Memory Dream任务发生未预期异常",
                extra={
                    "job_id": str(job_id),
                    "error_type": type(exc).__name__,
                    "duration_seconds": round(time.monotonic() - started_at, 6),
                },
            )
            return False

    async def _prepare(self, job_id: UUID) -> PreparedMemoryExtraction:
        async with self.session_factory() as db:
            try:
                prepared = await MemoryExtractionService(db, self.llm).prepare(
                    job_id,
                    self.worker_id,
                )
                await db.commit()
                return prepared
            except Exception:
                await db.rollback()
                raise

    async def _extract_without_transaction(
        self,
        prepared: PreparedMemoryExtraction,
    ) -> MemoryExtractionResult:
        """此方法运行时不存在打开的数据库 Session 或行锁。"""
        return await MemoryExtractionService.extract_with_llm(prepared, self.llm)

    async def _prepare_dream(self, job_id: UUID) -> PreparedDream:
        async with self.session_factory() as db:
            try:
                prepared = await MemoryDreamService(db, self.llm).prepare(
                    job_id,
                    self.worker_id,
                )
                await db.commit()
                return prepared
            except Exception:
                await db.rollback()
                raise

    async def _dream_without_transaction(self, prepared: PreparedDream) -> MemoryDreamResult:
        """此方法运行时不存在打开的数据库 Session 或行锁。"""
        return await MemoryDreamService.complete_with_llm(prepared, self.llm)

    async def _apply(
        self,
        job_id: UUID,
        result: MemoryExtractionResult,
        *,
        mode: str = "active",
    ) -> MemoryApplyResult:
        async with self.session_factory() as db:
            try:
                service = MemoryExtractionService(db, self.llm)
                if mode == "shadow":
                    job = await service.job_repo.require_owned_lease(job_id, self.worker_id)
                    if (job.payload or {}).get("mode") != "shadow":
                        raise MemoryExtractionFailure(
                            "mode_changed",
                            "影子提取模式与Job冻结配置不一致",
                            retryable=False,
                        )
                    applied = MemoryApplyResult(
                        created_ids=(),
                        updated_ids=(),
                        unchanged_ids=(),
                        rejected_count=0,
                        catalog_version=job.base_catalog_version,
                        mode="shadow",
                    )
                    job.payload = {
                        **(job.payload or {}),
                        "result": {
                            "candidate_count": len(result.memories),
                            "catalog_version": job.base_catalog_version,
                        },
                    }
                    await service.job_repo.mark_succeeded(job, self.worker_id)
                    await db.commit()
                    return applied

                applied = await service.apply(job_id, self.worker_id, result)
                job = await service.job_repo.require_owned_lease(job_id, self.worker_id)
                if (job.payload or {}).get("mode", "active") != mode:
                    raise MemoryExtractionFailure(
                        "mode_changed",
                        "提取模式在模型执行期间发生变化",
                        retryable=False,
                    )
                job.payload = {**(job.payload or {}), "result": applied.to_payload()}
                completed_at = datetime.now(UTC)
                await MemoryJobService(db).record_extract_success_and_maybe_enqueue_dream(
                    job,
                    completed_at=completed_at,
                )
                await service.job_repo.mark_succeeded(
                    job,
                    self.worker_id,
                    finished_at=completed_at,
                )
                await db.commit()
                return applied
            except Exception:
                await db.rollback()
                raise

    async def _apply_dream(
        self,
        job_id: UUID,
        prepared: PreparedDream,
        result: MemoryDreamResult,
    ) -> DreamApplyResult:
        async with self.session_factory() as db:
            try:
                service = MemoryDreamService(db, self.llm)
                applied = await service.apply(
                    job_id,
                    self.worker_id,
                    prepared,
                    result,
                )
                job = await service.job_repo.require_owned_lease(job_id, self.worker_id)
                job.payload = {**(job.payload or {}), "result": applied.to_payload()}
                await service.job_repo.mark_succeeded(job, self.worker_id)
                await db.commit()
                return applied
            except Exception:
                await db.rollback()
                raise

    async def _record_failure(
        self,
        job_id: UUID,
        failure: MemoryExtractionFailure | MemoryDreamFailure,
    ) -> None:
        async with self.session_factory() as db:
            try:
                repo = MemoryJobRepository(db)
                job = await repo.require_owned_lease(job_id, self.worker_id)
                delay_index = min(max(job.attempts - 1, 0), len(_RETRY_DELAYS_SECONDS) - 1)
                # 重试退避基于 DB 时钟，与认领条件的 func.now() 使用同一时间源。
                await repo.mark_failed(
                    job,
                    self.worker_id,
                    retryable=failure.retryable,
                    available_at=func.now()
                    + timedelta(seconds=_RETRY_DELAYS_SECONDS[delay_index]),
                    error_code=failure.code[:64],
                    error_message=str(failure)[:1000],
                )
                await db.commit()
            except Exception as exc:
                await db.rollback()
                # 此处兜底涵盖 Lease 失效、DB 连接故障和提交失败等所有异常，
                # 只能如实记录后放弃；Job 将等待 Lease 到期由其他 Worker 接管。
                logger.exception(
                    "记录Memory任务失败状态时发生异常，任务将等待Lease到期后重新认领",
                    extra={
                        "job_id": str(job_id),
                        "worker_id": self.worker_id,
                        "record_error_type": type(exc).__name__,
                        "original_error_code": failure.code,
                        "original_retryable": failure.retryable,
                    },
                )

    @staticmethod
    def _default_worker_id() -> str:
        instance = settings.instance_id or socket.gethostname()
        return f"{instance}:{os.getpid()}:{uuid4().hex[:8]}"[:120]
