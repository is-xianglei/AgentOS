from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.permission import PermissionRuleRecord
from models.session import SessionMessage, SessionRecord, SessionSnapshot
from models.task import TaskRecord
from models.team import (
    SubAgentRunRecord,
    TeamMemberRecord,
    TeamMessageRecord,
    TeamRecord,
)
from models.tool import ToolCallRecord

# 会话软删除时需一并标记的子表(均以 session_id 关联)。
# 物理删除靠外键 ON DELETE CASCADE 清理,软删除只改标记,故须在此显式登记。
_SESSION_CHILD_MODELS = (
    SessionMessage,
    SessionSnapshot,
    ToolCallRecord,
    TaskRecord,
    TeamRecord,
    TeamMemberRecord,
    TeamMessageRecord,
    SubAgentRunRecord,
    PermissionRuleRecord,
)


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

    async def delete(self, session: SessionRecord) -> None:
        """软删除单个会话:标记会话本身并级联标记其所有子表记录。"""
        await self._soft_delete_ids([session.id])

    async def delete_by_ids(self, ids: list[int]) -> int:
        """按 id 批量软删除会话,返回实际标记的会话行数。级联同上。"""
        if not ids:
            return 0
        return await self._soft_delete_ids(ids)

    async def _soft_delete_ids(self, ids: list[int]) -> int:
        """把给定会话及其全部子表记录标记为已删除(is_deleted / deleted_at)。

        子表以 session_id 关联,逐表 bulk UPDATE;已删除的行跳过,避免覆盖首次删除时间。
        返回实际新标记的会话行数(已处于删除态的不计入)。
        """
        now = datetime.now(timezone.utc)
        for model in _SESSION_CHILD_MODELS:
            await self.db.execute(
                update(model)
                .where(model.session_id.in_(ids), model.is_deleted.is_(False))
                .values(is_deleted=True, deleted_at=now)
            )
        result = await self.db.execute(
            update(SessionRecord)
            .where(SessionRecord.id.in_(ids), SessionRecord.is_deleted.is_(False))
            .values(is_deleted=True, deleted_at=now)
        )
        await self.db.flush()
        return result.rowcount or 0

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
