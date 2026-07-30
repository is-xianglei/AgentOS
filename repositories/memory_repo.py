from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from models.memory import (
    MemoryItemRecord,
    MemoryJobRecord,
    MemoryRevisionRecord,
    MemorySourceRecord,
    MemorySpaceRecord,
    TurnMemoryContextRecord,
)
from session.models import SessionTurnRecord


@dataclass(frozen=True)
class MemoryCatalogEntry:
    """Catalog 查询的稳定投影。"""

    id: int
    version: int
    memory_key: str
    memory_type: str
    name: str
    description: str


@dataclass(frozen=True)
class MemoryCatalogSnapshot:
    """同一 SQL 一致性视图中的 Catalog 版本和条目。"""

    space_id: int | None
    catalog_version: int
    entries: tuple[MemoryCatalogEntry, ...]


@dataclass(frozen=True)
class MemoryDreamEntry:
    """Dream 读取的 active Memory 和当前 Revision 稳定投影。"""

    item_id: int
    revision_id: int
    version: int
    memory_key: str
    memory_type: str
    name: str
    description: str
    body: str
    source_kind: str
    last_used_at: datetime | None
    use_count: int


@dataclass(frozen=True)
class MemoryExportBundle:
    """单个租户 Space 的完整可导出数据库投影。"""

    space: MemorySpaceRecord | None
    items: tuple[MemoryItemRecord, ...]
    revisions: tuple[MemoryRevisionRecord, ...]
    sources: tuple[MemorySourceRecord, ...]
    jobs: tuple[MemoryJobRecord, ...]
    contexts: tuple[TurnMemoryContextRecord, ...]


@dataclass(frozen=True)
class MemoryPhysicalDeleteCounts:
    """物理删除及其级联影响的明确计数。"""

    spaces: int = 0
    items: int = 0
    revisions: int = 0
    sources: int = 0
    jobs: int = 0
    contexts: int = 0

    def __add__(self, other: MemoryPhysicalDeleteCounts) -> MemoryPhysicalDeleteCounts:
        return MemoryPhysicalDeleteCounts(
            spaces=self.spaces + other.spaces,
            items=self.items + other.items,
            revisions=self.revisions + other.revisions,
            sources=self.sources + other.sources,
            jobs=self.jobs + other.jobs,
            contexts=self.contexts + other.contexts,
        )


class MemoryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_space(
        self,
        workspace_id: int,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> MemorySpaceRecord | None:
        """按可信租户边界查询 Space。"""
        stmt = select(MemorySpaceRecord).where(
            MemorySpaceRecord.workspace_id == workspace_id,
            MemorySpaceRecord.user_id == user_id,
            MemorySpaceRecord.is_deleted.is_(False),
        )
        if for_update:
            stmt = stmt.with_for_update(of=MemorySpaceRecord)
        return (await self.db.scalars(stmt)).first()

    async def get_space_for_worker(
        self,
        space_id: int,
        *,
        for_update: bool = False,
    ) -> MemorySpaceRecord | None:
        """按 Job 中的可信 Space ID 读取空间，供受信 Worker 建立租户作用域。"""
        stmt = select(MemorySpaceRecord).where(
            MemorySpaceRecord.id == space_id,
            MemorySpaceRecord.is_deleted.is_(False),
        )
        if for_update:
            stmt = stmt.with_for_update(of=MemorySpaceRecord)
        return (await self.db.scalars(stmt)).first()

    async def get_or_create_space_for_update(
        self,
        workspace_id: int,
        user_id: int,
    ) -> MemorySpaceRecord:
        """并发安全地创建或锁定当前用户的 Space。"""
        stmt = (
            insert(MemorySpaceRecord)
            .values(
                workspace_id=workspace_id,
                user_id=user_id,
                catalog_version=0,
                sessions_since_dream=0,
                settings={},
            )
            .on_conflict_do_nothing(
                index_elements=[
                    MemorySpaceRecord.workspace_id,
                    MemorySpaceRecord.user_id,
                ],
                index_where=text("is_deleted = false"),
            )
        )
        await self.db.execute(stmt)
        space = await self.get_space(workspace_id, user_id, for_update=True)
        if space is None:
            raise RuntimeError("创建Memory空间后无法重新读取")
        return space

    async def list_items(
        self,
        workspace_id: int,
        user_id: int,
        *,
        status: str | None,
        memory_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[MemoryItemRecord], int]:
        """分页查询当前 Space 的 Memory。"""
        conditions = self._scope_conditions(workspace_id, user_id)
        if status is not None:
            conditions.append(MemoryItemRecord.status == status)
        if memory_type is not None:
            conditions.append(MemoryItemRecord.memory_type == memory_type)

        count_stmt = (
            select(func.count(MemoryItemRecord.id))
            .select_from(MemoryItemRecord)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(*conditions)
        )
        total = int((await self.db.scalar(count_stmt)) or 0)
        stmt = (
            select(MemoryItemRecord)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(*conditions)
            .order_by(MemoryItemRecord.updated_at.desc(), MemoryItemRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self.db.scalars(stmt)), total

    async def search_items(
        self,
        workspace_id: int,
        user_id: int,
        *,
        query: str,
        memory_type: str | None,
        limit: int,
    ) -> list[MemoryItemRecord]:
        """在稳定标识、标题和摘要中执行大小写不敏感的词法搜索。"""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conditions = self._scope_conditions(workspace_id, user_id)
        conditions.extend(
            [
                MemoryItemRecord.status == "active",
                or_(
                    MemoryItemRecord.memory_key.ilike(pattern, escape="\\"),
                    MemoryItemRecord.name.ilike(pattern, escape="\\"),
                    MemoryItemRecord.description.ilike(pattern, escape="\\"),
                ),
            ]
        )
        if memory_type is not None:
            conditions.append(MemoryItemRecord.memory_type == memory_type)
        stmt = (
            select(MemoryItemRecord)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(*conditions)
            .order_by(MemoryItemRecord.updated_at.desc(), MemoryItemRecord.id.desc())
            .limit(limit)
        )
        return list(await self.db.scalars(stmt))

    async def get_item(
        self,
        workspace_id: int,
        user_id: int,
        memory_id: int,
        *,
        for_update: bool = False,
    ) -> MemoryItemRecord | None:
        """在可信租户边界内查询单条 Memory。"""
        stmt = (
            select(MemoryItemRecord)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                *self._scope_conditions(workspace_id, user_id),
                MemoryItemRecord.id == memory_id,
            )
        )
        if for_update:
            stmt = stmt.with_for_update(of=MemoryItemRecord)
        return (await self.db.scalars(stmt)).first()

    async def get_active_item_by_key(
        self,
        workspace_id: int,
        user_id: int,
        memory_key: str,
        *,
        for_update: bool = False,
    ) -> MemoryItemRecord | None:
        """按 active memory_key 查询同一 Space 内的记录。"""
        stmt = (
            select(MemoryItemRecord)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                *self._scope_conditions(workspace_id, user_id),
                MemoryItemRecord.memory_key == memory_key,
                MemoryItemRecord.status == "active",
            )
        )
        if for_update:
            stmt = stmt.with_for_update(of=MemoryItemRecord)
        return (await self.db.scalars(stmt)).first()

    async def get_current_revision(
        self,
        workspace_id: int,
        user_id: int,
        item: MemoryItemRecord,
    ) -> MemoryRevisionRecord | None:
        """按可信租户边界读取 Item 当前版本的不可变修订。"""
        stmt = (
            select(MemoryRevisionRecord)
            .join(MemoryItemRecord, MemoryRevisionRecord.memory_id == MemoryItemRecord.id)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                *self._scope_conditions(workspace_id, user_id),
                MemoryRevisionRecord.memory_id == item.id,
                MemoryRevisionRecord.revision == item.version,
            )
        )
        return (await self.db.scalars(stmt)).first()

    async def count_active_items(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
    ) -> int:
        """按可信租户边界统计锁定 Space 内的 active Memory 数量。"""
        stmt = (
            select(func.count(MemoryItemRecord.id))
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                *self._scope_conditions(workspace_id, user_id),
                MemoryItemRecord.space_id == space_id,
                MemoryItemRecord.status == "active",
            )
        )
        return int((await self.db.scalar(stmt)) or 0)

    async def list_active_dream_entries(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
    ) -> list[MemoryDreamEntry]:
        """确定性读取 Dream 所需 active Item 及其当前 Revision。"""
        stmt = (
            select(
                MemoryItemRecord.id,
                MemoryRevisionRecord.id,
                MemoryItemRecord.version,
                MemoryItemRecord.memory_key,
                MemoryItemRecord.memory_type,
                MemoryItemRecord.name,
                MemoryItemRecord.description,
                MemoryItemRecord.body,
                MemoryItemRecord.source_kind,
                MemoryItemRecord.last_used_at,
                MemoryItemRecord.use_count,
            )
            .select_from(MemoryItemRecord)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .join(
                MemoryRevisionRecord,
                and_(
                    MemoryRevisionRecord.memory_id == MemoryItemRecord.id,
                    MemoryRevisionRecord.revision == MemoryItemRecord.version,
                ),
            )
            .where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.id == space_id,
                MemorySpaceRecord.is_deleted.is_(False),
                MemoryItemRecord.status == "active",
                MemoryItemRecord.is_deleted.is_(False),
                MemoryRevisionRecord.is_deleted.is_(False),
            )
            .order_by(MemoryItemRecord.memory_key.asc(), MemoryItemRecord.id.asc())
        )
        rows = (await self.db.execute(stmt)).all()
        return [
            MemoryDreamEntry(
                item_id=row[0],
                revision_id=row[1],
                version=row[2],
                memory_key=row[3],
                memory_type=row[4],
                name=row[5],
                description=row[6],
                body=row[7],
                source_kind=row[8],
                last_used_at=row[9],
                use_count=row[10],
            )
            for row in rows
        ]

    async def get_active_items_by_ids_for_update(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
        item_ids: list[int],
    ) -> list[MemoryItemRecord]:
        """按固定顺序锁定同一租户 Space 内的 active Item。"""
        if not item_ids:
            return []
        stmt = (
            select(MemoryItemRecord)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.id == space_id,
                MemorySpaceRecord.is_deleted.is_(False),
                MemoryItemRecord.id.in_(set(item_ids)),
                MemoryItemRecord.status == "active",
                MemoryItemRecord.is_deleted.is_(False),
            )
            .order_by(MemoryItemRecord.id.asc())
            .with_for_update(of=MemoryItemRecord)
        )
        return list(await self.db.scalars(stmt))

    async def get_revision_by_id_for_scope(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
        revision_id: int,
    ) -> MemoryRevisionRecord | None:
        """在显式租户边界内读取一条不可变 Revision。"""
        stmt = (
            select(MemoryRevisionRecord)
            .join(MemoryItemRecord, MemoryRevisionRecord.memory_id == MemoryItemRecord.id)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.id == space_id,
                MemorySpaceRecord.is_deleted.is_(False),
                MemoryItemRecord.is_deleted.is_(False),
                MemoryRevisionRecord.id == revision_id,
                MemoryRevisionRecord.is_deleted.is_(False),
            )
        )
        return (await self.db.scalars(stmt)).first()

    async def create_item(
        self,
        workspace_id: int,
        user_id: int,
        *,
        space: MemorySpaceRecord,
        memory_key: str,
        memory_type: str,
        name: str,
        description: str,
        body: str,
        source_kind: str,
    ) -> MemoryItemRecord:
        """创建稳定 Memory 记录。"""
        self._assert_space_scope(space, workspace_id, user_id)
        item = MemoryItemRecord(
            space_id=space.id,
            memory_key=memory_key,
            memory_type=memory_type,
            name=name,
            description=description,
            body=body,
            status="active",
            version=1,
            source_kind=source_kind,
            use_count=0,
        )
        self.db.add(item)
        await self.db.flush()
        await self.db.refresh(item)
        return item

    async def update_item(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
        item: MemoryItemRecord,
        *,
        memory_type: str,
        name: str,
        description: str,
        body: str,
    ) -> MemoryItemRecord:
        """更新稳定记录并推进乐观锁版本。"""
        self._assert_item_scope(space, item, workspace_id, user_id)
        item.memory_type = memory_type
        item.name = name
        item.description = description
        item.body = body
        item.version += 1
        await self.db.flush()
        await self.db.refresh(item)
        return item

    async def update_item_state(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
        item: MemoryItemRecord,
        *,
        status: str,
        superseded_by_id: int | None,
    ) -> MemoryItemRecord:
        """更新 Dream 状态和替代关系，并推进 Item 版本。"""
        self._assert_item_scope(space, item, workspace_id, user_id)
        if status not in {"active", "superseded", "archived"}:
            raise RuntimeError("Memory状态无效")
        if (status == "superseded") != (superseded_by_id is not None):
            raise RuntimeError("superseded状态必须且只能指定替代Memory")

        if superseded_by_id is not None:
            if superseded_by_id == item.id:
                raise RuntimeError("Memory不能替代自身")
            replacement_stmt = (
                select(MemoryItemRecord.id)
                .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
                .where(
                    MemorySpaceRecord.workspace_id == workspace_id,
                    MemorySpaceRecord.user_id == user_id,
                    MemorySpaceRecord.id == space.id,
                    MemorySpaceRecord.is_deleted.is_(False),
                    MemoryItemRecord.id == superseded_by_id,
                    MemoryItemRecord.status == "active",
                    MemoryItemRecord.is_deleted.is_(False),
                )
            )
            if (await self.db.scalar(replacement_stmt)) is None:
                raise RuntimeError("替代Memory不属于当前租户Space或不是active状态")

        item.status = status
        item.superseded_by_id = superseded_by_id
        item.version += 1
        await self.db.flush()
        await self.db.refresh(item)
        return item

    async def restore_item_from_revision(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
        item: MemoryItemRecord,
        revision: MemoryRevisionRecord,
    ) -> MemoryItemRecord:
        """按历史 Revision 恢复 Item 内容并以新版本重新激活。"""
        self._assert_item_scope(space, item, workspace_id, user_id)
        scoped_revision = await self.get_revision_by_id_for_scope(
            workspace_id,
            user_id,
            space.id,
            revision.id,
        )
        if scoped_revision is None or scoped_revision.memory_id != item.id:
            raise RuntimeError("Memory修订不属于当前租户Space中的目标Memory")

        item.memory_type = scoped_revision.memory_type
        item.name = scoped_revision.name
        item.description = scoped_revision.description
        item.body = scoped_revision.body
        item.status = "active"
        item.superseded_by_id = None
        item.version += 1
        await self.db.flush()
        await self.db.refresh(item)
        return item

    async def soft_delete_item(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
        item: MemoryItemRecord,
    ) -> None:
        """归档并软删除 Memory，保留 Revision 和来源审计。"""
        self._assert_item_scope(space, item, workspace_id, user_id)
        item.status = "archived"
        item.version += 1
        item.is_deleted = True
        item.deleted_at = datetime.now(UTC)
        await self.db.flush()

    async def create_revision(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
        item: MemoryItemRecord,
        *,
        actor_type: str,
        actor_id: str | None,
        run_id: UUID | None = None,
    ) -> MemoryRevisionRecord:
        """按 Item 当前内容追加不可变 Revision。"""
        self._assert_item_scope(
            space,
            item,
            workspace_id,
            user_id,
            allow_deleted=True,
        )
        revision = MemoryRevisionRecord(
            memory_id=item.id,
            revision=item.version,
            memory_type=item.memory_type,
            name=item.name,
            description=item.description,
            body=item.body,
            actor_type=actor_type,
            actor_id=actor_id,
            run_id=run_id,
        )
        self.db.add(revision)
        await self.db.flush()
        await self.db.refresh(revision)
        return revision

    async def create_source(
        self,
        workspace_id: int,
        user_id: int,
        *,
        space: MemorySpaceRecord,
        item: MemoryItemRecord,
        revision: MemoryRevisionRecord,
        source_kind: str,
        session_id: int | None = None,
        turn_id: UUID | None = None,
        from_message_id: int | None = None,
        to_message_id: int | None = None,
        source_excerpt: str | None = None,
    ) -> MemorySourceRecord:
        """追加一条来源证据。"""
        self._assert_item_scope(
            space,
            item,
            workspace_id,
            user_id,
            allow_deleted=True,
        )
        if revision.memory_id != item.id:
            raise RuntimeError("Memory修订不属于目标Memory")
        source = MemorySourceRecord(
            memory_id=item.id,
            revision_id=revision.id,
            session_id=session_id,
            turn_id=turn_id,
            from_message_id=from_message_id,
            to_message_id=to_message_id,
            source_kind=source_kind,
            source_excerpt=source_excerpt,
        )
        self.db.add(source)
        await self.db.flush()
        await self.db.refresh(source)
        return source

    async def create_source_if_absent(
        self,
        workspace_id: int,
        user_id: int,
        *,
        space: MemorySpaceRecord,
        item: MemoryItemRecord,
        revision: MemoryRevisionRecord,
        source_kind: str,
        session_id: int,
        turn_id: UUID,
        message_id: int,
        source_excerpt: str | None = None,
    ) -> MemorySourceRecord | None:
        """幂等追加单消息来源；重复投递时不破坏当前事务。"""
        self._assert_item_scope(
            space,
            item,
            workspace_id,
            user_id,
            allow_deleted=True,
        )
        if revision.memory_id != item.id:
            raise RuntimeError("Memory修订不属于目标Memory")
        stmt = (
            insert(MemorySourceRecord)
            .values(
                memory_id=item.id,
                revision_id=revision.id,
                session_id=session_id,
                turn_id=turn_id,
                from_message_id=message_id,
                to_message_id=message_id,
                source_kind=source_kind,
                source_excerpt=source_excerpt,
            )
            .on_conflict_do_nothing(
                constraint="uq_memory_sources_provenance",
            )
            .returning(MemorySourceRecord)
        )
        return (await self.db.scalars(stmt)).first()

    async def list_revisions(
        self,
        workspace_id: int,
        user_id: int,
        memory_id: int,
    ) -> list[MemoryRevisionRecord]:
        """按租户边界列出不可变修订，最新版本在前。"""
        stmt = (
            select(MemoryRevisionRecord)
            .join(MemoryItemRecord, MemoryRevisionRecord.memory_id == MemoryItemRecord.id)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                *self._scope_conditions(workspace_id, user_id),
                MemoryRevisionRecord.memory_id == memory_id,
            )
            .order_by(MemoryRevisionRecord.revision.desc())
        )
        return list(await self.db.scalars(stmt))

    async def list_sources(
        self,
        workspace_id: int,
        user_id: int,
        memory_id: int,
    ) -> list[MemorySourceRecord]:
        """按租户边界列出来源证据，最新记录在前。"""
        stmt = (
            select(MemorySourceRecord)
            .join(MemoryItemRecord, MemorySourceRecord.memory_id == MemoryItemRecord.id)
            .join(MemorySpaceRecord, MemoryItemRecord.space_id == MemorySpaceRecord.id)
            .where(
                *self._scope_conditions(workspace_id, user_id),
                MemorySourceRecord.memory_id == memory_id,
            )
            .order_by(MemorySourceRecord.id.desc())
        )
        return list(await self.db.scalars(stmt))

    async def get_export_bundle(
        self,
        workspace_id: int,
        user_id: int,
        *,
        space: MemorySpaceRecord | None,
    ) -> MemoryExportBundle:
        """按明确租户边界读取仍保留的全部 Memory 数据。"""
        contexts_stmt = (
            select(TurnMemoryContextRecord)
            .join(
                SessionTurnRecord,
                TurnMemoryContextRecord.turn_id == SessionTurnRecord.id,
            )
            .where(
                SessionTurnRecord.workspace_id == workspace_id,
                SessionTurnRecord.user_id == user_id,
            )
            .order_by(TurnMemoryContextRecord.created_at.asc(), TurnMemoryContextRecord.id.asc())
            .execution_options(include_deleted=True)
        )
        contexts = tuple(await self.db.scalars(contexts_stmt))
        if space is None:
            return MemoryExportBundle(None, (), (), (), (), contexts)
        self._assert_space_scope(space, workspace_id, user_id)

        items_stmt = (
            select(MemoryItemRecord)
            .where(MemoryItemRecord.space_id == space.id)
            .order_by(MemoryItemRecord.id.asc())
            .execution_options(include_deleted=True)
        )
        items = tuple(await self.db.scalars(items_stmt))
        item_ids = [item.id for item in items]

        revisions: tuple[MemoryRevisionRecord, ...] = ()
        sources: tuple[MemorySourceRecord, ...] = ()
        if item_ids:
            revisions_stmt = (
                select(MemoryRevisionRecord)
                .where(MemoryRevisionRecord.memory_id.in_(item_ids))
                .order_by(
                    MemoryRevisionRecord.memory_id.asc(),
                    MemoryRevisionRecord.revision.asc(),
                )
                .execution_options(include_deleted=True)
            )
            revisions = tuple(await self.db.scalars(revisions_stmt))
            sources_stmt = (
                select(MemorySourceRecord)
                .where(MemorySourceRecord.memory_id.in_(item_ids))
                .order_by(MemorySourceRecord.memory_id.asc(), MemorySourceRecord.id.asc())
                .execution_options(include_deleted=True)
            )
            sources = tuple(await self.db.scalars(sources_stmt))

        jobs_stmt = (
            select(MemoryJobRecord)
            .where(MemoryJobRecord.space_id == space.id)
            .order_by(MemoryJobRecord.created_at.asc(), MemoryJobRecord.id.asc())
            .execution_options(include_deleted=True)
        )
        return MemoryExportBundle(
            space=space,
            items=items,
            revisions=revisions,
            sources=sources,
            jobs=tuple(await self.db.scalars(jobs_stmt)),
            contexts=contexts,
        )

    async def get_catalog_version(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
    ) -> int | None:
        """从数据库重新读取 Catalog 版本，用于多查询导出结束校验。"""
        stmt = select(MemorySpaceRecord.catalog_version).where(
            MemorySpaceRecord.id == space_id,
            MemorySpaceRecord.workspace_id == workspace_id,
            MemorySpaceRecord.user_id == user_id,
            MemorySpaceRecord.is_deleted.is_(False),
        )
        return await self.db.scalar(stmt)

    async def purge_space(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
    ) -> MemoryPhysicalDeleteCounts:
        """物理删除 Space；冻结上下文需先显式删除，其余数据依赖外键级联。"""
        self._assert_space_scope(space, workspace_id, user_id)
        tenant_turn_ids = select(SessionTurnRecord.id).where(
            SessionTurnRecord.workspace_id == workspace_id,
            SessionTurnRecord.user_id == user_id,
        )
        item_ids = select(MemoryItemRecord.id).where(MemoryItemRecord.space_id == space.id)
        item_ids = item_ids.execution_options(include_deleted=True)
        counts = MemoryPhysicalDeleteCounts(
            spaces=1,
            items=int(
                (
                    await self.db.scalar(
                        select(func.count())
                        .select_from(item_ids.subquery())
                        .execution_options(include_deleted=True)
                    )
                )
                or 0
            ),
            revisions=int(
                (
                    await self.db.scalar(
                        select(func.count(MemoryRevisionRecord.id))
                        .where(MemoryRevisionRecord.memory_id.in_(item_ids))
                        .execution_options(include_deleted=True)
                    )
                )
                or 0
            ),
            sources=int(
                (
                    await self.db.scalar(
                        select(func.count(MemorySourceRecord.id))
                        .where(MemorySourceRecord.memory_id.in_(item_ids))
                        .execution_options(include_deleted=True)
                    )
                )
                or 0
            ),
            jobs=int(
                (
                    await self.db.scalar(
                        select(func.count(MemoryJobRecord.id))
                        .where(MemoryJobRecord.space_id == space.id)
                        .execution_options(include_deleted=True)
                    )
                )
                or 0
            ),
            contexts=int(
                (
                    await self.db.scalar(
                        select(func.count(TurnMemoryContextRecord.id))
                        .where(TurnMemoryContextRecord.turn_id.in_(tenant_turn_ids))
                        .execution_options(include_deleted=True)
                    )
                )
                or 0
            ),
        )
        await self.db.execute(
            delete(TurnMemoryContextRecord).where(
                TurnMemoryContextRecord.turn_id.in_(tenant_turn_ids)
            )
        )
        result = await self.db.execute(
            delete(MemorySpaceRecord).where(
                MemorySpaceRecord.id == space.id,
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("彻底删除Memory空间时检测到并发变更")
        await self.db.flush()
        return counts

    async def list_spaces_for_retention(
        self,
        *,
        after_space_id: int,
        limit: int,
        space_id: int | None = None,
    ) -> list[MemorySpaceRecord]:
        """按稳定游标分页扫描需要执行保留策略的 Space。"""
        conditions = [
            MemorySpaceRecord.id > after_space_id,
            MemorySpaceRecord.is_deleted.is_(False),
        ]
        if space_id is not None:
            conditions.append(MemorySpaceRecord.id == space_id)
        stmt = (
            select(MemorySpaceRecord)
            .where(*conditions)
            .order_by(MemorySpaceRecord.id.asc())
            .limit(limit)
        )
        return list(await self.db.scalars(stmt))

    async def purge_archived_items_before(
        self,
        space_id: int,
        *,
        cutoff: datetime,
        limit: int,
    ) -> MemoryPhysicalDeleteCounts:
        """锁定并物理清理一批超期归档 Item，明确统计级联数据。"""
        if limit <= 0:
            return MemoryPhysicalDeleteCounts()
        ids_stmt = (
            select(MemoryItemRecord.id)
            .where(
                MemoryItemRecord.space_id == space_id,
                MemoryItemRecord.status == "archived",
                MemoryItemRecord.updated_at <= cutoff,
            )
            .order_by(MemoryItemRecord.updated_at.asc(), MemoryItemRecord.id.asc())
            .limit(limit)
            .with_for_update(of=MemoryItemRecord, skip_locked=True)
            .execution_options(include_deleted=True)
        )
        item_ids = list(await self.db.scalars(ids_stmt))
        if not item_ids:
            return MemoryPhysicalDeleteCounts()
        revision_count = int(
            (
                await self.db.scalar(
                    select(func.count(MemoryRevisionRecord.id))
                    .where(MemoryRevisionRecord.memory_id.in_(item_ids))
                    .execution_options(include_deleted=True)
                )
            )
            or 0
        )
        source_count = int(
            (
                await self.db.scalar(
                    select(func.count(MemorySourceRecord.id))
                    .where(MemorySourceRecord.memory_id.in_(item_ids))
                    .execution_options(include_deleted=True)
                )
            )
            or 0
        )
        await self.db.execute(delete(MemoryItemRecord).where(MemoryItemRecord.id.in_(item_ids)))
        await self.db.flush()
        return MemoryPhysicalDeleteCounts(
            items=len(item_ids),
            revisions=revision_count,
            sources=source_count,
        )

    async def purge_terminal_jobs_before(
        self,
        space_id: int,
        *,
        cutoff: datetime,
        limit: int,
    ) -> MemoryPhysicalDeleteCounts:
        """只物理清理 succeeded/dead 终态 Job，运行中任务永不匹配。"""
        if limit <= 0:
            return MemoryPhysicalDeleteCounts()
        ids_stmt = (
            select(MemoryJobRecord.id)
            .where(
                MemoryJobRecord.space_id == space_id,
                MemoryJobRecord.status.in_(("succeeded", "dead")),
                MemoryJobRecord.finished_at.is_not(None),
                MemoryJobRecord.finished_at <= cutoff,
            )
            .order_by(MemoryJobRecord.finished_at.asc(), MemoryJobRecord.id.asc())
            .limit(limit)
            .with_for_update(of=MemoryJobRecord, skip_locked=True)
            .execution_options(include_deleted=True)
        )
        job_ids = list(await self.db.scalars(ids_stmt))
        if not job_ids:
            return MemoryPhysicalDeleteCounts()
        await self.db.execute(delete(MemoryJobRecord).where(MemoryJobRecord.id.in_(job_ids)))
        await self.db.flush()
        return MemoryPhysicalDeleteCounts(jobs=len(job_ids))

    async def increment_catalog_version(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
    ) -> int:
        """在持有 Space 行锁时原子递增 Catalog 版本，返回递增后的新版本号。"""
        self._assert_space_scope(space, workspace_id, user_id)
        stmt = (
            update(MemorySpaceRecord)
            .where(MemorySpaceRecord.id == space.id)
            .values(catalog_version=MemorySpaceRecord.catalog_version + 1)
            .returning(MemorySpaceRecord.catalog_version)
        )
        new_version = await self.db.scalar(stmt)
        if new_version is None:
            raise RuntimeError("递增Catalog版本时未找到目标Space")
        # 同步 ORM 对象，避免后续读到旧值
        space.catalog_version = new_version
        return new_version

    async def touch_used_items(
        self,
        workspace_id: int,
        user_id: int,
        item_ids: list[int],
    ) -> None:
        """批量原子更新被注入正文的 Memory 使用热度，SQL 层自增避免并发丢失。

        时间取数据库时钟（事务开始时刻），与 Job 侧保持同一时间源，避免应用与
        数据库时钟偏差导致热度排序错乱。
        """
        if not item_ids:
            return
        scoped_space_ids = select(MemorySpaceRecord.id).where(
            MemorySpaceRecord.workspace_id == workspace_id,
            MemorySpaceRecord.user_id == user_id,
            MemorySpaceRecord.is_deleted.is_(False),
        )
        stmt = (
            update(MemoryItemRecord)
            .where(
                MemoryItemRecord.id.in_(item_ids),
                MemoryItemRecord.space_id.in_(scoped_space_ids),
            )
            .values(
                use_count=MemoryItemRecord.use_count + 1,
                last_used_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        await self.db.execute(stmt)

    async def increment_sessions_since_dream(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
    ) -> int:
        """锁定 Space 后记录一个自上次 Dream 起新完成的 Session。"""
        stmt = (
            select(MemorySpaceRecord)
            .where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.id == space_id,
                MemorySpaceRecord.is_deleted.is_(False),
            )
            .with_for_update(of=MemorySpaceRecord)
        )
        space = (await self.db.scalars(stmt)).first()
        if space is None:
            raise RuntimeError("Memory空间不属于当前用户或工作区")
        space.sessions_since_dream += 1
        await self.db.flush()
        await self.db.refresh(space)
        return space.sessions_since_dream

    async def finalize_dream(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
        *,
        completed_at: datetime,
        catalog_changed: bool,
    ) -> int:
        """在持有 Space 行锁时完成 Dream，并按需推进一次 Catalog 版本。"""
        self._assert_space_scope(space, workspace_id, user_id)
        values: dict[str, object] = {
            "last_dream_at": completed_at,
            "sessions_since_dream": 0,
        }
        if catalog_changed:
            # 与 increment_catalog_version 保持一致，在 SQL 层原子递增，
            # 使正确性不依赖调用方是否持有行锁。
            values["catalog_version"] = MemorySpaceRecord.catalog_version + 1
        stmt = (
            update(MemorySpaceRecord)
            .where(MemorySpaceRecord.id == space.id)
            .values(**values)
            .returning(MemorySpaceRecord.catalog_version)
        )
        new_version = await self.db.scalar(stmt)
        if new_version is None:
            raise RuntimeError("完成Dream时未找到目标Space")
        # 同步 ORM 对象，避免后续读到旧值
        space.last_dream_at = completed_at
        space.sessions_since_dream = 0
        space.catalog_version = new_version
        return new_version

    async def mark_dream_scan(
        self,
        workspace_id: int,
        user_id: int,
        space: MemorySpaceRecord,
        *,
        scanned_at: datetime,
    ) -> None:
        """在持有 Space 行锁时记录一次通过基础门控的 Dream 扫描。"""
        self._assert_space_scope(space, workspace_id, user_id)
        space.last_scan_at = scanned_at
        await self.db.flush()

    async def get_catalog_snapshot(
        self,
        workspace_id: int,
        user_id: int,
        *,
        limit: int,
    ) -> MemoryCatalogSnapshot:
        """用一个 SQL 一致性读取 Catalog 版本和 active 条目。"""
        join_condition = and_(
            MemoryItemRecord.space_id == MemorySpaceRecord.id,
            MemoryItemRecord.status == "active",
            MemoryItemRecord.is_deleted.is_(False),
        )
        stmt = (
            select(
                MemorySpaceRecord.id,
                MemorySpaceRecord.catalog_version,
                MemoryItemRecord.id,
                MemoryItemRecord.version,
                MemoryItemRecord.memory_key,
                MemoryItemRecord.memory_type,
                MemoryItemRecord.name,
                MemoryItemRecord.description,
            )
            .select_from(MemorySpaceRecord)
            .outerjoin(MemoryItemRecord, join_condition)
            .where(
                MemorySpaceRecord.workspace_id == workspace_id,
                MemorySpaceRecord.user_id == user_id,
                MemorySpaceRecord.is_deleted.is_(False),
            )
            .order_by(MemoryItemRecord.memory_key.asc(), MemoryItemRecord.id.asc())
            .limit(limit)
        )
        rows = (await self.db.execute(stmt)).all()
        if not rows:
            return MemoryCatalogSnapshot(space_id=None, catalog_version=0, entries=())

        entries = tuple(
            MemoryCatalogEntry(
                id=row[2],
                version=row[3],
                memory_key=row[4],
                memory_type=row[5],
                name=row[6],
                description=row[7],
            )
            for row in rows
            if row[2] is not None
        )
        return MemoryCatalogSnapshot(
            space_id=rows[0][0],
            catalog_version=rows[0][1],
            entries=entries,
        )

    def _scope_conditions(
        self,
        workspace_id: int,
        user_id: int,
    ) -> list[ColumnElement[bool]]:
        """生成所有 Item 查询共用的显式租户边界。"""
        return [
            MemorySpaceRecord.workspace_id == workspace_id,
            MemorySpaceRecord.user_id == user_id,
            MemorySpaceRecord.is_deleted.is_(False),
            MemoryItemRecord.is_deleted.is_(False),
        ]

    @staticmethod
    def _assert_space_scope(
        space: MemorySpaceRecord,
        workspace_id: int,
        user_id: int,
    ) -> None:
        """拒绝把已锁定对象用于其他租户边界。"""
        if space.workspace_id != workspace_id or space.user_id != user_id or space.is_deleted:
            raise RuntimeError("Memory空间不属于当前用户或工作区")

    @classmethod
    def _assert_item_scope(
        cls,
        space: MemorySpaceRecord,
        item: MemoryItemRecord,
        workspace_id: int,
        user_id: int,
        *,
        allow_deleted: bool = False,
    ) -> None:
        cls._assert_space_scope(space, workspace_id, user_id)
        if item.space_id != space.id or (item.is_deleted and not allow_deleted):
            raise RuntimeError("Memory不属于当前空间或已经删除")
