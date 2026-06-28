from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import TaskRecord


class TaskRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_by_session(self, session_id: int) -> list[TaskRecord]:
        stmt = select(TaskRecord).where(TaskRecord.session_id == session_id).order_by(TaskRecord.id)
        return list(await self.db.scalars(stmt))

    async def create(
        self,
        session_id: int,
        subject: str,
        description: str,
        owner: str,
        blocked_by: list[int],
    ) -> TaskRecord:
        task = TaskRecord(
            session_id=session_id,
            subject=subject,
            description=description,
            owner=owner,
            blocked_by=blocked_by,
            status="pending",
        )
        self.db.add(task)
        await self.db.flush()
        await self.db.refresh(task)
        return task

    async def get(self, session_id: int, task_id: int) -> TaskRecord | None:
        stmt = select(TaskRecord).where(
            TaskRecord.session_id == session_id,
            TaskRecord.id == task_id,
        )
        return (await self.db.scalars(stmt)).first()

    async def get_many(self, session_id: int, task_ids: list[int]) -> list[TaskRecord]:
        if not task_ids:
            return []
        stmt = select(TaskRecord).where(
            TaskRecord.session_id == session_id,
            TaskRecord.id.in_(task_ids),
        )
        return list(await self.db.scalars(stmt))

    async def update(
        self,
        task: TaskRecord,
        subject: str | None = None,
        description: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        blocked_by: list[int] | None = None,
    ) -> TaskRecord:
        if subject is not None:
            task.subject = subject
        if description is not None:
            task.description = description
        if status is not None:
            task.status = status
        if owner is not None:
            task.owner = owner
        if blocked_by is not None:
            task.blocked_by = blocked_by
        await self.db.flush()
        await self.db.refresh(task)
        return task

    async def remove_blocker_from_others(self, session_id: int, blocker_id: int) -> None:
        for task in await self.list_by_session(session_id):
            if blocker_id in task.blocked_by:
                task.blocked_by = [item for item in task.blocked_by if item != blocker_id]
        await self.db.flush()
