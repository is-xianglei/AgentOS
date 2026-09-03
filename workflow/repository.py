from datetime import UTC, datetime

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from workflow.models import WorkflowAgentRecord, WorkflowRecord


class WorkflowRepository:
    """Workflow 域持久化，仅操作 workflows 与 workflow_agents 两张表。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _with_members(
        stmt: Select[tuple[WorkflowRecord]],
    ) -> Select[tuple[WorkflowRecord]]:
        # 成员连同其 Agent 一次预载，响应侧需要成员名称，避免逐个懒加载。
        return stmt.options(
            selectinload(WorkflowRecord.agents).selectinload(WorkflowAgentRecord.agent),
        )

    async def create(
        self,
        *,
        workspace_id: int,
        name: str,
        description: str | None,
        mode: str,
        is_enabled: bool,
        created_by_user_id: int,
    ) -> WorkflowRecord:
        record = WorkflowRecord(
            workspace_id=workspace_id,
            name=name,
            description=description,
            mode=mode,
            is_enabled=is_enabled,
            created_by_user_id=created_by_user_id,
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def get(self, workspace_id: int, workflow_id: int) -> WorkflowRecord | None:
        stmt = self._with_members(
            select(WorkflowRecord).where(
                WorkflowRecord.id == workflow_id,
                WorkflowRecord.workspace_id == workspace_id,
            )
        ).execution_options(populate_existing=True)
        return (await self.db.scalars(stmt)).first()

    async def get_for_update(
        self,
        workspace_id: int,
        workflow_id: int,
    ) -> WorkflowRecord | None:
        stmt = self._with_members(
            select(WorkflowRecord)
            .where(
                WorkflowRecord.id == workflow_id,
                WorkflowRecord.workspace_id == workspace_id,
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
        exclude_workflow_id: int | None = None,
    ) -> WorkflowRecord | None:
        stmt = select(WorkflowRecord).where(
            WorkflowRecord.workspace_id == workspace_id,
            WorkflowRecord.name == name,
        )
        if exclude_workflow_id is not None:
            stmt = stmt.where(WorkflowRecord.id != exclude_workflow_id)
        return (await self.db.scalars(stmt)).first()

    async def list_and_count(
        self,
        workspace_id: int,
        *,
        keyword: str | None = None,
        is_enabled: bool | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[WorkflowRecord], int]:
        filters = [WorkflowRecord.workspace_id == workspace_id]
        if keyword:
            pattern = f"%{keyword.strip()}%"
            filters.append(
                or_(
                    WorkflowRecord.name.ilike(pattern),
                    WorkflowRecord.description.ilike(pattern),
                )
            )
        if is_enabled is not None:
            filters.append(WorkflowRecord.is_enabled.is_(is_enabled))
        total = await self.db.scalar(select(func.count(WorkflowRecord.id)).where(*filters))
        stmt = self._with_members(
            select(WorkflowRecord)
            .where(*filters)
            .order_by(WorkflowRecord.updated_at.desc(), WorkflowRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self.db.scalars(stmt)), int(total or 0)

    async def update(self, record: WorkflowRecord) -> WorkflowRecord:
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def soft_delete(self, record: WorkflowRecord) -> None:
        now = datetime.now(UTC)
        record.is_deleted = True
        record.deleted_at = now
        await self.db.execute(
            update(WorkflowAgentRecord)
            .where(
                WorkflowAgentRecord.workflow_id == record.id,
                WorkflowAgentRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        await self.db.flush()

    async def replace_members(
        self,
        workflow_id: int,
        members: dict[int, int],
    ) -> None:
        """整体替换成员集；members 为 {agent_id: sort_order}。

        已软删的旧绑定若再次出现在目标集合中直接复活并更新 sort_order，
        沿用唯一约束 (workflow_id, agent_id) 而不新插重复行。
        """
        stmt = (
            select(WorkflowAgentRecord)
            .where(WorkflowAgentRecord.workflow_id == workflow_id)
            .execution_options(include_deleted=True)
        )
        existing = list(await self.db.scalars(stmt))
        existing_by_agent = {item.agent_id: item for item in existing}
        now = datetime.now(UTC)

        for agent_id, item in existing_by_agent.items():
            if agent_id in members:
                item.is_deleted = False
                item.deleted_at = None
                item.sort_order = members[agent_id]
            elif not item.is_deleted:
                item.is_deleted = True
                item.deleted_at = now

        new_records = [
            WorkflowAgentRecord(workflow_id=workflow_id, agent_id=agent_id, sort_order=sort_order)
            for agent_id, sort_order in members.items()
            if agent_id not in existing_by_agent
        ]
        if new_records:
            self.db.add_all(new_records)
        await self.db.flush()
