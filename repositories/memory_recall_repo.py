from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import (
    MemoryItemRecord,
    MemoryRevisionRecord,
    MemorySpaceRecord,
    TurnMemoryContextRecord,
)
from session.models import SessionMessage, SessionTurnRecord
from repositories.memory_repo import MemoryCatalogEntry


@dataclass(frozen=True)
class MemoryLexicalCandidate:
    """PostgreSQL 词法降级候选及其 trigram 相似度。"""

    entry: MemoryCatalogEntry
    trigram_similarity: float


class MemoryRecallRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_context(
        self,
        workspace_id: int,
        user_id: int,
        turn_id: UUID,
        *,
        session_id: int | None = None,
    ) -> TurnMemoryContextRecord | None:
        """按可信租户边界读取已冻结的 Turn Memory Context。"""
        conditions = [
            SessionTurnRecord.id == turn_id,
            SessionTurnRecord.workspace_id == workspace_id,
            SessionTurnRecord.user_id == user_id,
            SessionTurnRecord.is_deleted.is_(False),
            TurnMemoryContextRecord.is_deleted.is_(False),
        ]
        if session_id is not None:
            conditions.append(SessionTurnRecord.session_id == session_id)
        stmt = (
            select(TurnMemoryContextRecord)
            .join(
                SessionTurnRecord,
                TurnMemoryContextRecord.turn_id == SessionTurnRecord.id,
            )
            .where(*conditions)
        )
        return (await self.db.scalars(stmt)).first()

    async def list_recent_user_texts(
        self,
        workspace_id: int,
        user_id: int,
        session_id: int,
        through_message_id: int,
        *,
        limit: int,
    ) -> list[str]:
        """读取最近原始用户请求，不包含 Hook continuation 或 Recall 注入。"""
        stmt = (
            select(SessionMessage.content)
            .join(
                SessionTurnRecord,
                SessionTurnRecord.started_message_id == SessionMessage.id,
            )
            .where(
                SessionTurnRecord.session_id == session_id,
                SessionTurnRecord.workspace_id == workspace_id,
                SessionTurnRecord.user_id == user_id,
                SessionTurnRecord.is_deleted.is_(False),
                SessionMessage.session_id == session_id,
                SessionMessage.turn_id == SessionTurnRecord.id,
                SessionMessage.id <= through_message_id,
                SessionMessage.role == "user",
                SessionMessage.is_deleted.is_(False),
            )
            .order_by(SessionMessage.id.desc())
            .limit(limit)
        )
        values = list(await self.db.scalars(stmt))
        return [value for value in reversed(values) if isinstance(value, str)]

    async def get_catalog_revisions(
        self,
        workspace_id: int,
        user_id: int,
        entries: list[MemoryCatalogEntry],
    ) -> list[MemoryRevisionRecord]:
        """按冻结 Catalog 的 ID 和版本读取不可变 Revision。"""
        if not entries:
            return []
        expected_pairs = [
            (MemoryRevisionRecord.memory_id == entry.id)
            & (MemoryRevisionRecord.revision == entry.version)
            for entry in entries
        ]
        stmt = (
            select(MemoryRevisionRecord)
            .join(MemoryItemRecord, MemoryRevisionRecord.memory_id == MemoryItemRecord.id)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.is_deleted.is_(False),
                or_(*expected_pairs),
            )
            .execution_options(include_deleted=True)
        )
        records = list(await self.db.scalars(stmt))
        by_pair = {(record.memory_id, record.revision): record for record in records}
        return [
            by_pair[(entry.id, entry.version)]
            for entry in entries
            if (entry.id, entry.version) in by_pair
        ]

    async def list_surfaced_revision_ids(
        self,
        workspace_id: int,
        user_id: int,
        session_id: int,
        *,
        exclude_turn_id: UUID,
    ) -> list[int]:
        """列出同一 Session 先前已呈现过的 Revision ID，保序去重由 Service 完成。"""
        stmt = (
            select(TurnMemoryContextRecord.selected_revision_ids)
            .join(
                SessionTurnRecord,
                TurnMemoryContextRecord.turn_id == SessionTurnRecord.id,
            )
            .where(
                SessionTurnRecord.session_id == session_id,
                SessionTurnRecord.workspace_id == workspace_id,
                SessionTurnRecord.user_id == user_id,
                SessionTurnRecord.id != exclude_turn_id,
                SessionTurnRecord.is_deleted.is_(False),
                TurnMemoryContextRecord.is_deleted.is_(False),
            )
            .order_by(TurnMemoryContextRecord.id)
        )
        result: list[int] = []
        for values in await self.db.scalars(stmt):
            if not isinstance(values, list):
                continue
            result.extend(value for value in values if type(value) is int and value > 0)
        return result

    async def get_revisions_by_ids(
        self,
        workspace_id: int,
        user_id: int,
        revision_ids: list[int],
    ) -> list[MemoryRevisionRecord]:
        """按租户边界读取历史 Revision，包括已经归档的 Item。"""
        if not revision_ids:
            return []
        stmt = (
            select(MemoryRevisionRecord)
            .join(MemoryItemRecord, MemoryRevisionRecord.memory_id == MemoryItemRecord.id)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemoryRevisionRecord.id.in_(revision_ids),
            )
            .execution_options(include_deleted=True)
        )
        records = list(await self.db.scalars(stmt))
        by_id = {record.id: record for record in records}
        return [by_id[revision_id] for revision_id in revision_ids if revision_id in by_id]

    async def search_lexical_candidates(
        self,
        workspace_id: int,
        user_id: int,
        query: str,
        catalog_entries: tuple[MemoryCatalogEntry, ...],
        *,
        limit: int,
    ) -> list[MemoryLexicalCandidate]:
        """在本 Turn 的 Catalog ID 和版本内计算 pg_trgm 相似度。"""
        if not catalog_entries:
            return []
        catalog_pairs = [
            (MemoryItemRecord.id == entry.id) & (MemoryItemRecord.version == entry.version)
            for entry in catalog_entries
        ]
        searchable = func.lower(MemoryItemRecord.name + literal(" ") + MemoryItemRecord.description)
        similarity = func.similarity(searchable, query.lower()).label("trigram_similarity")
        stmt = (
            select(
                MemoryItemRecord.id,
                MemoryItemRecord.version,
                MemoryItemRecord.memory_key,
                MemoryItemRecord.memory_type,
                MemoryItemRecord.name,
                MemoryItemRecord.description,
                similarity,
            )
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.is_deleted.is_(False),
                MemoryItemRecord.status == "active",
                MemoryItemRecord.is_deleted.is_(False),
                or_(*catalog_pairs),
            )
            .order_by(similarity.desc(), MemoryItemRecord.memory_key, MemoryItemRecord.id)
            .limit(limit)
        )
        rows = (await self.db.execute(stmt)).all()
        return [
            MemoryLexicalCandidate(
                entry=MemoryCatalogEntry(
                    id=row[0],
                    version=row[1],
                    memory_key=row[2],
                    memory_type=row[3],
                    name=row[4],
                    description=row[5],
                ),
                trigram_similarity=float(row[6] or 0),
            )
            for row in rows
        ]

    async def freeze_context(
        self,
        workspace_id: int,
        user_id: int,
        turn_id: UUID,
        *,
        space_id: int | None,
        catalog_version: int,
        selected_revision_ids: list[int],
        rendered_catalog: str,
        rendered_memories: str,
        selector_status: str,
        degraded_reason: str | None,
        byte_count: int,
    ) -> TurnMemoryContextRecord:
        """短事务锁定 Turn；并发冻结时返回最先成功写入的 Context。"""
        turn_stmt = (
            select(SessionTurnRecord)
            .where(
                SessionTurnRecord.id == turn_id,
                SessionTurnRecord.workspace_id == workspace_id,
                SessionTurnRecord.user_id == user_id,
                SessionTurnRecord.is_deleted.is_(False),
            )
            .with_for_update(of=SessionTurnRecord)
        )
        turn = (await self.db.scalars(turn_stmt)).first()
        if turn is None:
            raise RuntimeError("冻结Memory上下文时找不到可信交互轮次")

        existing = await self.get_context(workspace_id, user_id, turn_id)
        if existing is not None:
            return existing

        context = TurnMemoryContextRecord(
            turn_id=turn_id,
            space_id=space_id,
            catalog_version=catalog_version,
            selected_revision_ids=selected_revision_ids,
            rendered_catalog=rendered_catalog,
            rendered_memories=rendered_memories,
            selector_status=selector_status,
            degraded_reason=degraded_reason,
            byte_count=byte_count,
        )
        self.db.add(context)
        await self.db.flush()
        turn.memory_context_id = context.id
        await self.db.flush()
        await self.db.refresh(context)
        return context
