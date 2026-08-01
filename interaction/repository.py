from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from interaction.models import (
    InteractionRequestRecord,
    RuntimeSuspensionRecord,
)


class InteractionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_suspension(
        self,
        session_id: int,
        turn_id: UUID,
        continuation_payload: dict[str, Any],
        resolution_policy: dict[str, Any],
    ) -> RuntimeSuspensionRecord:
        record = RuntimeSuspensionRecord(
            session_id=session_id,
            turn_id=turn_id,
            continuation_payload=continuation_payload,
            resolution_policy=resolution_policy,
            status="pending",
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def create_request(
        self,
        suspension: RuntimeSuspensionRecord,
        *,
        kind: str,
        request_payload: dict[str, Any],
        tool_call_id: int | None,
        tool_use_id: str | None,
        schema_version: int,
        sequence: int,
    ) -> InteractionRequestRecord:
        record = InteractionRequestRecord(
            suspension_id=suspension.id,
            session_id=suspension.session_id,
            turn_id=suspension.turn_id,
            tool_call_id=tool_call_id,
            tool_use_id=tool_use_id,
            kind=kind,
            schema_version=schema_version,
            sequence=sequence,
            status="pending",
            request_payload=request_payload,
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def get_suspension(self, suspension_id: UUID) -> RuntimeSuspensionRecord | None:
        stmt = (
            select(RuntimeSuspensionRecord)
            .where(RuntimeSuspensionRecord.id == suspension_id)
            .options(selectinload(RuntimeSuspensionRecord.requests))
        )
        return (await self.db.scalars(stmt)).first()

    async def get_suspension_for_update(
        self, suspension_id: UUID
    ) -> RuntimeSuspensionRecord | None:
        stmt = (
            select(RuntimeSuspensionRecord)
            .where(RuntimeSuspensionRecord.id == suspension_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def get_active_suspension_for_update(
        self, session_id: int
    ) -> RuntimeSuspensionRecord | None:
        stmt = (
            select(RuntimeSuspensionRecord)
            .where(
                RuntimeSuspensionRecord.session_id == session_id,
                RuntimeSuspensionRecord.status.in_(("pending", "resuming")),
            )
            .order_by(RuntimeSuspensionRecord.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return (await self.db.scalars(stmt)).first()

    async def get_pending_by_session(
        self, session_id: int
    ) -> RuntimeSuspensionRecord | None:
        stmt = (
            select(RuntimeSuspensionRecord)
            .where(
                RuntimeSuspensionRecord.session_id == session_id,
                RuntimeSuspensionRecord.status.in_(("pending", "resuming")),
            )
            .options(selectinload(RuntimeSuspensionRecord.requests))
            .order_by(RuntimeSuspensionRecord.created_at.desc())
            .limit(1)
        )
        return (await self.db.scalars(stmt)).first()

    async def list_requests(
        self,
        suspension_id: UUID,
        status: str | None = None,
        *,
        for_update: bool = False,
    ) -> list[InteractionRequestRecord]:
        stmt = select(InteractionRequestRecord).where(
            InteractionRequestRecord.suspension_id == suspension_id
        )
        if status is not None:
            stmt = stmt.where(InteractionRequestRecord.status == status)
        stmt = stmt.order_by(InteractionRequestRecord.sequence)
        if for_update:
            stmt = stmt.with_for_update()
        return list(await self.db.scalars(stmt))

    async def get_request(self, request_id: UUID) -> InteractionRequestRecord | None:
        return await self.db.get(InteractionRequestRecord, request_id)

    async def lock_pending_request(
        self,
        request_id: UUID,
        session_id: int | None = None,
    ) -> InteractionRequestRecord | None:
        stmt = select(InteractionRequestRecord).where(
            InteractionRequestRecord.id == request_id,
            InteractionRequestRecord.status == "pending",
        )
        if session_id is not None:
            stmt = stmt.where(InteractionRequestRecord.session_id == session_id)
        stmt = stmt.with_for_update()
        return (await self.db.scalars(stmt)).first()

    async def count_pending_requests(self, suspension_id: UUID) -> int:
        stmt = select(func.count()).select_from(InteractionRequestRecord).where(
            InteractionRequestRecord.suspension_id == suspension_id,
            InteractionRequestRecord.status == "pending",
        )
        return int((await self.db.scalar(stmt)) or 0)

    async def resolve_request(
        self,
        request: InteractionRequestRecord,
        response_payload: dict[str, Any],
        responded_by: int | None,
    ) -> InteractionRequestRecord:
        request.status = "resolved"
        request.response_payload = response_payload
        request.responded_by = responded_by
        request.responded_at = datetime.now(UTC)
        await self.db.flush()
        await self.db.refresh(request)
        return request

    async def cancel_request(
        self,
        request: InteractionRequestRecord,
        responded_by: int | None,
    ) -> InteractionRequestRecord:
        request.status = "cancelled"
        request.responded_by = responded_by
        request.responded_at = datetime.now(UTC)
        await self.db.flush()
        await self.db.refresh(request)
        return request

    async def update_suspension_status(
        self,
        suspension: RuntimeSuspensionRecord,
        status: str,
        *,
        terminal: bool = False,
    ) -> RuntimeSuspensionRecord:
        suspension.status = status
        suspension.version += 1
        suspension.resolved_at = datetime.now(UTC) if terminal else None
        await self.db.flush()
        await self.db.refresh(suspension)
        return suspension

    async def update_continuation(
        self,
        suspension: RuntimeSuspensionRecord,
        continuation_payload: dict[str, Any],
    ) -> RuntimeSuspensionRecord:
        suspension.continuation_payload = continuation_payload
        suspension.version += 1
        await self.db.flush()
        await self.db.refresh(suspension)
        return suspension
