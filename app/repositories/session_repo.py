from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import SessionMessage, SessionRecord, SessionSnapshot


class SessionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        title: str,
        model_name: str | None,
        system_prompt: str | None,
        metadata: dict[str, Any],
    ) -> SessionRecord:
        session = SessionRecord(
            title=title,
            status="idle",
            model_name=model_name,
            system_prompt=system_prompt,
            extra=metadata,
        )
        self.db.add(session)
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def list(self) -> list[SessionRecord]:
        stmt = select(SessionRecord).order_by(desc(SessionRecord.last_active_at), desc(SessionRecord.id))
        return list(await self.db.scalars(stmt))

    async def get(self, session_id: int) -> SessionRecord | None:
        return await self.db.get(SessionRecord, session_id)

    async def get_for_update(self, session_id: int) -> SessionRecord | None:
        stmt = select(SessionRecord).where(SessionRecord.id == session_id).with_for_update()
        return (await self.db.scalars(stmt)).first()

    async def update_status(self, session: SessionRecord, status: str) -> SessionRecord:
        session.status = status
        session.last_active_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def add_message(
        self,
        session_id: int,
        role: str,
        content: Any,
        token_estimate: int,
    ) -> SessionMessage:
        message = SessionMessage(
            session_id=session_id,
            role=role,
            content=content,
            token_estimate=token_estimate,
        )
        self.db.add(message)
        await self.db.flush()
        await self.db.refresh(message)
        return message

    async def list_messages(self, session_id: int) -> list[SessionMessage]:
        stmt = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.id)
        )
        return list(await self.db.scalars(stmt))

    async def add_snapshot(
        self,
        session_id: int,
        messages: list[Any],
        summary: str,
        snapshot_type: str = "compact",
    ) -> SessionSnapshot:
        snapshot = SessionSnapshot(
            session_id=session_id,
            snapshot_type=snapshot_type,
            messages=messages,
            summary=summary,
        )
        self.db.add(snapshot)
        await self.db.flush()
        await self.db.refresh(snapshot)
        return snapshot

    async def latest_snapshot(self, session_id: int) -> SessionSnapshot | None:
        stmt = (
            select(SessionSnapshot)
            .where(SessionSnapshot.session_id == session_id)
            .order_by(desc(SessionSnapshot.id))
            .limit(1)
        )
        return (await self.db.scalars(stmt)).first()
