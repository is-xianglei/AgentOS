from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from memory.models import MemoryJobRecord, MemorySpaceRecord


class MemoryJobRepository:
    """持久 Memory Job 的入队、认领和状态迁移。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def enqueue_extract(
        self,
        *,
        space_id: int,
        session_id: int,
        turn_id: UUID,
        idempotency_key: str,
        max_attempts: int,
        model: str | None,
        prompt_version: str,
        base_catalog_version: int,
        payload: dict[str, Any] | None = None,
        available_at: datetime | None = None,
    ) -> MemoryJobRecord:
        """幂等创建提取 Job，并返回本次创建或先前已存在的记录。

        available_at 未显式传入时使用 DB 时钟，与认领条件的 func.now() 同源，
        避免应用时钟偏快导致刚入队的 Job 无法被就地认领。
        """
        job_id = uuid4()
        stmt = (
            insert(MemoryJobRecord)
            .values(
                id=job_id,
                job_type="extract",
                space_id=space_id,
                session_id=session_id,
                turn_id=turn_id,
                idempotency_key=idempotency_key,
                status="pending",
                attempts=0,
                max_attempts=max_attempts,
                available_at=available_at if available_at is not None else func.now(),
                model=model,
                prompt_version=prompt_version,
                base_catalog_version=base_catalog_version,
                payload=payload or {},
            )
            .on_conflict_do_nothing(
                index_elements=[MemoryJobRecord.idempotency_key],
            )
            .returning(MemoryJobRecord)
        )
        job = (await self.db.scalars(stmt)).first()
        if job is not None:
            return job

        existing_stmt = select(MemoryJobRecord).where(
            MemoryJobRecord.idempotency_key == idempotency_key,
        )
        existing = (await self.db.scalars(existing_stmt)).first()
        if existing is None:
            raise RuntimeError("幂等创建Memory Job后无法重新读取")
        return existing

    async def enqueue_dream(
        self,
        *,
        space_id: int,
        idempotency_key: str,
        max_attempts: int,
        model: str | None,
        prompt_version: str,
        base_catalog_version: int,
        payload: dict[str, Any] | None = None,
        available_at: datetime | None = None,
    ) -> MemoryJobRecord:
        """幂等创建 Dream Job，并返回本次创建或先前已存在的记录。"""
        stmt = (
            insert(MemoryJobRecord)
            .values(
                id=uuid4(),
                job_type="dream",
                space_id=space_id,
                session_id=None,
                turn_id=None,
                idempotency_key=idempotency_key,
                status="pending",
                attempts=0,
                max_attempts=max_attempts,
                # 与提取入队一致：未显式传入时用 DB 时钟作为唯一时间源。
                available_at=available_at if available_at is not None else func.now(),
                model=model,
                prompt_version=prompt_version,
                base_catalog_version=base_catalog_version,
                payload=payload or {},
            )
            .on_conflict_do_nothing(
                index_elements=[MemoryJobRecord.idempotency_key],
            )
            .returning(MemoryJobRecord)
        )
        job = (await self.db.scalars(stmt)).first()
        if job is not None:
            return job

        existing_stmt = select(MemoryJobRecord).where(
            MemoryJobRecord.idempotency_key == idempotency_key,
        )
        existing = (await self.db.scalars(existing_stmt)).first()
        if existing is None:
            raise RuntimeError("幂等创建Dream Job后无法重新读取")
        return existing

    async def has_live_dream(self, space_id: int) -> bool:
        """判断 Space 是否已有仍可执行或正在执行的 Dream Job。"""
        stmt = (
            select(MemoryJobRecord.id)
            .where(
                MemoryJobRecord.space_id == space_id,
                MemoryJobRecord.job_type == "dream",
                MemoryJobRecord.status.in_(("pending", "retry", "running")),
                MemoryJobRecord.attempts < MemoryJobRecord.max_attempts,
                MemoryJobRecord.is_deleted.is_(False),
            )
            .limit(1)
        )
        return (await self.db.scalar(stmt)) is not None

    async def has_succeeded_extract_for_session_since(
        self,
        space_id: int,
        session_id: int,
        since: datetime,
    ) -> bool:
        """判断 Session 在指定时间后是否已有成功提取。"""
        stmt = (
            select(MemoryJobRecord.id)
            .where(
                MemoryJobRecord.space_id == space_id,
                MemoryJobRecord.session_id == session_id,
                MemoryJobRecord.job_type == "extract",
                MemoryJobRecord.status == "succeeded",
                MemoryJobRecord.finished_at.is_not(None),
                MemoryJobRecord.finished_at >= since,
                MemoryJobRecord.is_deleted.is_(False),
            )
            .limit(1)
        )
        return (await self.db.scalar(stmt)) is not None

    async def update_dream_snapshot(
        self,
        job: MemoryJobRecord,
        *,
        base_catalog_version: int,
        revision_ids: list[int],
    ) -> MemoryJobRecord:
        """保存 Dream 读取时的 Catalog 版本和 Revision ID 集合。"""
        if job.job_type != "dream":
            raise RuntimeError("只有Dream Job可以保存Dream快照")
        job.base_catalog_version = base_catalog_version
        job.payload = {**(job.payload or {}), "revision_ids": list(revision_ids)}
        await self.db.flush()
        await self.db.refresh(job)
        return job

    async def merge_payload(
        self,
        job: MemoryJobRecord,
        values: dict[str, Any],
    ) -> MemoryJobRecord:
        """合并只含引用和审计摘要的 Job Payload。"""
        job.payload = {**(job.payload or {}), **values}
        await self.db.flush()
        await self.db.refresh(job)
        return job

    async def get_by_id(
        self,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> MemoryJobRecord | None:
        """按 Job ID 读取记录，供受信 Worker 使用。"""
        stmt = select(MemoryJobRecord).where(
            MemoryJobRecord.id == job_id,
            MemoryJobRecord.is_deleted.is_(False),
        )
        if for_update:
            stmt = stmt.with_for_update(of=MemoryJobRecord)
        return (await self.db.scalars(stmt)).first()

    async def get_by_id_for_scope(
        self,
        workspace_id: int,
        user_id: int,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> MemoryJobRecord | None:
        """在可信工作区和用户边界内读取 Job。"""
        stmt = (
            select(MemoryJobRecord)
            .join(MemorySpaceRecord, MemoryJobRecord.space_id == MemorySpaceRecord.id)
            .where(
                MemoryJobRecord.id == job_id,
                MemoryJobRecord.is_deleted.is_(False),
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.is_deleted.is_(False),
            )
        )
        if for_update:
            stmt = stmt.with_for_update(of=MemoryJobRecord)
        return (await self.db.scalars(stmt)).first()

    async def claim_available(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        limit: int,
        job_id: UUID | None = None,
        job_type: str | None = None,
        payload_mode: str | None = None,
        workspace_id: int | None = None,
        user_id: int | None = None,
        now: datetime | None = None,
    ) -> list[MemoryJobRecord]:
        """使用 SKIP LOCKED 认领当前可执行 Job。

        调用方必须在返回后立即提交短事务，不能在持有行锁时调用 LLM。
        now 未显式传入时使用 DB 时钟，与 available_at 的写入时间源保持一致。
        """
        claimed_at: datetime | ColumnElement[datetime] = (
            now if now is not None else func.now()
        )
        scope_conditions = []
        if job_id is not None:
            scope_conditions.append(MemoryJobRecord.id == job_id)
        if job_type is not None:
            scope_conditions.append(MemoryJobRecord.job_type == job_type)
        if payload_mode is not None:
            scope_conditions.append(MemoryJobRecord.payload["mode"].as_string() == payload_mode)
        if (workspace_id is None) != (user_id is None):
            raise ValueError("workspace_id 和 user_id 必须同时提供")
        if workspace_id is not None and user_id is not None:
            scoped_space_ids = select(MemorySpaceRecord.id).where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.is_deleted.is_(False),
            )
            scope_conditions.append(MemoryJobRecord.space_id.in_(scoped_space_ids))
        await self.db.execute(
            update(MemoryJobRecord)
            .where(
                MemoryJobRecord.status == "running",
                MemoryJobRecord.lease_until.is_not(None),
                MemoryJobRecord.lease_until <= func.now(),
                MemoryJobRecord.attempts >= MemoryJobRecord.max_attempts,
                MemoryJobRecord.is_deleted.is_(False),
                *scope_conditions,
            )
            .values(
                status="dead",
                finished_at=func.now(),
                worker_id=None,
                lease_until=None,
                last_error_code="attempts_exhausted",
                last_error_message="Lease到期且已达到最大尝试次数",
            )
        )
        available_condition = and_(
            MemoryJobRecord.status.in_(("pending", "retry")),
            MemoryJobRecord.available_at <= func.now(),
            or_(
                MemoryJobRecord.lease_until.is_(None),
                MemoryJobRecord.lease_until <= func.now(),
            ),
        )
        expired_running_condition = and_(
            MemoryJobRecord.status == "running",
            MemoryJobRecord.lease_until.is_not(None),
            MemoryJobRecord.lease_until <= func.now(),
        )
        conditions = [
            or_(available_condition, expired_running_condition),
            MemoryJobRecord.attempts < MemoryJobRecord.max_attempts,
            MemoryJobRecord.is_deleted.is_(False),
            *scope_conditions,
        ]

        stmt = (
            select(MemoryJobRecord)
            .where(*conditions)
            .order_by(MemoryJobRecord.available_at, MemoryJobRecord.id)
            .limit(limit)
            .with_for_update(of=MemoryJobRecord, skip_locked=True)
        )
        jobs = list(await self.db.scalars(stmt))
        lease_until = claimed_at + timedelta(seconds=lease_seconds)
        for job in jobs:
            job.status = "running"
            job.worker_id = worker_id
            job.lease_until = lease_until
            job.attempts += 1
            if job.started_at is None:
                job.started_at = claimed_at
            job.finished_at = None
            job.last_error_code = None
            job.last_error_message = None

        await self.db.flush()
        # DB 时钟表达式在 flush 后过期，refresh 读回真实时间戳供进程内后续判断。
        for job in jobs:
            await self.db.refresh(job)
        return jobs

    async def get_owned_lease(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        for_update: bool = True,
    ) -> MemoryJobRecord | None:
        """重新读取由指定 Worker 持有且尚未过期的 Job。"""
        stmt = select(MemoryJobRecord).where(
            MemoryJobRecord.id == job_id,
            MemoryJobRecord.status == "running",
            MemoryJobRecord.worker_id == worker_id,
            MemoryJobRecord.lease_until.is_not(None),
            MemoryJobRecord.lease_until > func.now(),
            MemoryJobRecord.is_deleted.is_(False),
        )
        if for_update:
            stmt = stmt.with_for_update(of=MemoryJobRecord)
        return (await self.db.scalars(stmt)).first()

    async def require_owned_lease(
        self,
        job_id: UUID,
        worker_id: str,
    ) -> MemoryJobRecord:
        """锁定并校验 Lease；过期或易主时拒绝继续落库。"""
        job = await self.get_owned_lease(job_id, worker_id, for_update=True)
        if job is None:
            raise RuntimeError("Memory Job Lease已过期或不属于当前Worker")
        return job

    async def mark_succeeded(
        self,
        job: MemoryJobRecord,
        worker_id: str,
        *,
        finished_at: datetime | None = None,
    ) -> MemoryJobRecord:
        """将持有 Lease 的 Job 标记为成功并清理 Lease。

        finished_at 未显式传入时用 DB 时钟落库；Lease 归属的权威校验在
        require_owned_lease 的 SQL 中完成，此处仅做进程内预检。
        """
        self._assert_lease_owner(job, worker_id, datetime.now(UTC))
        job.status = "succeeded"
        job.finished_at = finished_at if finished_at is not None else func.now()
        job.last_error_code = None
        job.last_error_message = None
        job.worker_id = None
        job.lease_until = None
        await self.db.flush()
        await self.db.refresh(job)
        return job

    async def mark_failed(
        self,
        job: MemoryJobRecord,
        worker_id: str,
        *,
        retryable: bool,
        available_at: datetime | ColumnElement[datetime],
        error_code: str,
        error_message: str,
        failed_at: datetime | None = None,
    ) -> MemoryJobRecord:
        """按可重试性和尝试上限进入 retry 或 dead，并释放 Lease。

        available_at 支持传入 func.now() + interval 形式的 DB 时钟表达式；
        failed_at 未显式传入时同样用 DB 时钟落库。
        """
        self._assert_lease_owner(job, worker_id, datetime.now(UTC))
        should_retry = retryable and job.attempts < job.max_attempts
        job.status = "retry" if should_retry else "dead"
        job.available_at = available_at
        if should_retry:
            job.finished_at = None
        else:
            job.finished_at = failed_at if failed_at is not None else func.now()
        job.last_error_code = error_code
        job.last_error_message = error_message
        job.worker_id = None
        job.lease_until = None
        await self.db.flush()
        await self.db.refresh(job)
        return job

    @staticmethod
    def _assert_lease_owner(
        job: MemoryJobRecord,
        worker_id: str,
        now: datetime,
    ) -> None:
        """拒绝非所有者、非运行态或 Lease 已过期的状态更新。"""
        owns_active_lease = (
            job.status == "running"
            and job.worker_id == worker_id
            and job.lease_until is not None
            and job.lease_until > now
        )
        if not owns_active_lease:
            raise RuntimeError("Memory Job Lease已过期或不属于当前Worker")
