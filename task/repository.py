from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from task.models import TaskRecord


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

    # ----- 任务认领(多实例安全) -----
    # 供 team 域子代理认领任务;原先写在 team repository 里直接操作本域表,
    # 现收归本域,team 侧改为经 TaskRepository 调用。

    async def list_claimable_tasks(self, session_id: int) -> list[TaskRecord]:
        """查本会话可认领的任务: 待处理、无负责人、无阻塞。"""
        stmt = (
            select(TaskRecord)
            .where(
                TaskRecord.session_id == session_id,
                TaskRecord.status == "pending",
                or_(TaskRecord.owner.is_(None), TaskRecord.owner == "agent"),
                or_(TaskRecord.blocked_by.is_(None), TaskRecord.blocked_by == []),
            )
            .order_by(TaskRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def claim_task(
        self,
        session_id: int,
        task_id: int,
        owner: str,
    ) -> tuple[bool, str]:
        """用条件 UPDATE 实现多实例安全认领。

        语义: UPDATE tasks SET owner=:owner, status='in_progress'
        WHERE id=:task_id AND session_id=:session_id
          AND status='pending' AND (owner IS NULL OR owner='agent')
          AND (blocked_by IS NULL OR blocked_by='[]');
        原子性由这条单语句的 WHERE 条件保证,与方法所在类无关;
        根据 rowcount 判断是否认领成功,返回 (是否成功, 原因)。
        """
        stmt = (
            update(TaskRecord)
            .where(
                TaskRecord.id == task_id,
                TaskRecord.session_id == session_id,
                TaskRecord.status == "pending",
                or_(TaskRecord.owner.is_(None), TaskRecord.owner == "agent"),
                or_(TaskRecord.blocked_by.is_(None), TaskRecord.blocked_by == []),
            )
            .values(owner=owner, status="in_progress")
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        if result.rowcount == 1:
            return True, "claimed"
        return False, "unavailable"
