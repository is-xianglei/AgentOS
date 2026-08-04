from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agent.models import AgentRecord, AgentSkillRecord, AgentToolRecord

BindingRecord = TypeVar("BindingRecord", AgentToolRecord, AgentSkillRecord)


class AgentRepository:
    """Agent 域持久化，仅操作 Agent 及其绑定表。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _with_bindings(
        stmt: Select[tuple[AgentRecord]],
    ) -> Select[tuple[AgentRecord]]:
        return stmt.options(
            selectinload(AgentRecord.tool_bindings),
            selectinload(AgentRecord.skill_bindings),
        )

    async def create(
        self,
        *,
        workspace_id: int,
        name: str,
        description: str | None,
        system_prompt: str,
        model_name: str | None,
        is_enabled: bool,
        created_by_user_id: int,
    ) -> AgentRecord:
        record = AgentRecord(
            workspace_id=workspace_id,
            name=name,
            description=description,
            system_prompt=system_prompt,
            model_name=model_name,
            is_enabled=is_enabled,
            created_by_user_id=created_by_user_id,
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def get(self, workspace_id: int, agent_id: int) -> AgentRecord | None:
        stmt = self._with_bindings(
            select(AgentRecord).where(
                AgentRecord.id == agent_id,
                AgentRecord.workspace_id == workspace_id,
            )
        ).execution_options(populate_existing=True)
        return (await self.db.scalars(stmt)).first()

    async def get_for_update(self, workspace_id: int, agent_id: int) -> AgentRecord | None:
        stmt = self._with_bindings(
            select(AgentRecord)
            .where(
                AgentRecord.id == agent_id,
                AgentRecord.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def get_by_name(
        self,
        workspace_id: int,
        name: str,
        *,
        exclude_agent_id: int | None = None,
    ) -> AgentRecord | None:
        stmt = select(AgentRecord).where(
            AgentRecord.workspace_id == workspace_id,
            AgentRecord.name == name,
        )
        if exclude_agent_id is not None:
            stmt = stmt.where(AgentRecord.id != exclude_agent_id)
        return (await self.db.scalars(stmt)).first()

    async def list_and_count(
        self,
        workspace_id: int,
        *,
        keyword: str | None = None,
        is_enabled: bool | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[AgentRecord], int]:
        filters = [AgentRecord.workspace_id == workspace_id]
        if keyword:
            pattern = f"%{keyword.strip()}%"
            filters.append(
                or_(
                    AgentRecord.name.ilike(pattern),
                    AgentRecord.description.ilike(pattern),
                )
            )
        if is_enabled is not None:
            filters.append(AgentRecord.is_enabled.is_(is_enabled))
        total = await self.db.scalar(select(func.count(AgentRecord.id)).where(*filters))
        stmt = self._with_bindings(
            select(AgentRecord)
            .where(*filters)
            .order_by(AgentRecord.updated_at.desc(), AgentRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self.db.scalars(stmt)), int(total or 0)

    async def update(self, record: AgentRecord) -> AgentRecord:
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def soft_delete(self, record: AgentRecord) -> None:
        now = datetime.now(UTC)
        record.is_deleted = True
        record.deleted_at = now
        await self.db.execute(
            update(AgentToolRecord)
            .where(
                AgentToolRecord.agent_id == record.id,
                AgentToolRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        await self.db.execute(
            update(AgentSkillRecord)
            .where(
                AgentSkillRecord.agent_id == record.id,
                AgentSkillRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        await self.db.flush()

    async def replace_tool_bindings(
        self,
        agent_id: int,
        tool_ids: list[int],
    ) -> None:
        existing = await self._list_bindings(AgentToolRecord, agent_id)
        await self._replace_bindings(
            existing,
            target_ids=tool_ids,
            target_attr="tool_id",
            create=lambda target_id: AgentToolRecord(agent_id=agent_id, tool_id=target_id),
        )

    async def replace_skill_bindings(
        self,
        agent_id: int,
        skill_ids: list[int],
    ) -> None:
        existing = await self._list_bindings(AgentSkillRecord, agent_id)
        await self._replace_bindings(
            existing,
            target_ids=skill_ids,
            target_attr="skill_id",
            create=lambda target_id: AgentSkillRecord(agent_id=agent_id, skill_id=target_id),
        )

    async def soft_delete_skill_bindings(
        self,
        workspace_id: int,
        skill_id: int,
    ) -> None:
        now = datetime.now(UTC)
        agent_ids = select(AgentRecord.id).where(
            AgentRecord.workspace_id == workspace_id,
            AgentRecord.is_deleted.is_(False),
        )
        await self.db.execute(
            update(AgentSkillRecord)
            .where(
                AgentSkillRecord.agent_id.in_(agent_ids),
                AgentSkillRecord.skill_id == skill_id,
                AgentSkillRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        await self.db.flush()

    async def _list_bindings(
        self,
        model: type[BindingRecord],
        agent_id: int,
    ) -> list[BindingRecord]:
        stmt = (
            select(model)
            .where(model.agent_id == agent_id)
            .execution_options(include_deleted=True)
        )
        return list(await self.db.scalars(stmt))

    async def _replace_bindings(
        self,
        existing: list[BindingRecord],
        *,
        target_ids: list[int],
        target_attr: str,
        create: Callable[[int], BindingRecord],
    ) -> None:
        desired = set(target_ids)
        existing_by_target = {getattr(item, target_attr): item for item in existing}
        now = datetime.now(UTC)

        for target_id, item in existing_by_target.items():
            if target_id in desired:
                item.is_deleted = False
                item.deleted_at = None
            elif not item.is_deleted:
                item.is_deleted = True
                item.deleted_at = now

        new_records = [
            create(target_id)
            for target_id in desired
            if target_id not in existing_by_target
        ]
        if new_records:
            self.db.add_all(new_records)
        await self.db.flush()
