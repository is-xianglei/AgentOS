from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationAppError
from app.repositories.task_repo import TaskRepository
from app.services.session_service import SessionService

VALID_TASK_STATUSES = {"pending", "in_progress", "completed"}


class TaskService:
    def __init__(self, db: AsyncSession):
        """初始化任务服务依赖。"""
        self.repo = TaskRepository(db)
        self.session_service = SessionService(db)

    async def list_by_session(self, session_id: int):
        """查询指定会话下的任务列表。"""
        await self.session_service.get_required(session_id)
        return await self.repo.list_by_session(session_id)

    async def create(
        self,
        session_id: int,
        subject: str,
        description: str = "",
        owner: str = "agent",
        blocked_by: list[int] | None = None,
    ):
        """创建任务。"""
        await self.session_service.get_required(session_id)
        self._validate_status("pending")
        normalized_blocked_by = await self._validate_blocked_by(session_id, blocked_by or [])
        task = await self.repo.create(
            session_id=session_id,
            subject=subject,
            description=description,
            owner=owner,
            blocked_by=normalized_blocked_by,
        )
        return task

    async def update(
        self,
        session_id: int,
        task_id: int,
        subject: str | None = None,
        description: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        blocked_by: list[int] | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ):
        """更新任务字段。"""
        await self.session_service.get_required(session_id)
        task = await self.repo.get(session_id, task_id)
        if task is None:
            raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
        if status is not None:
            self._validate_status(status)
        normalized_blocked_by = self._merge_blocked_by(
            current=list(task.blocked_by or []),
            replace=blocked_by,
            add=add_blocked_by,
            remove=remove_blocked_by,
        )
        if normalized_blocked_by is not None:
            normalized_blocked_by = await self._validate_blocked_by(
                session_id,
                normalized_blocked_by,
                current_task_id=task_id,
            )
        updated = await self.repo.update(
            task,
            subject=subject,
            description=description,
            status=status,
            owner=owner,
            blocked_by=normalized_blocked_by,
        )
        if status == "completed":
            await self.repo.remove_blocker_from_others(session_id, task_id)
        return updated

    def _validate_status(self, status: str) -> None:
        """校验任务状态是否合法。"""
        if status not in VALID_TASK_STATUSES:
            raise ValidationAppError("TASK_STATUS_INVALID", "任务状态无效")

    def _merge_blocked_by(
        self,
        current: list[int],
        replace: list[int] | None,
        add: list[int] | None,
        remove: list[int] | None,
    ) -> list[int] | None:
        """合并阻塞任务的整体替换与增量改动。"""
        if replace is None and add is None and remove is None:
            return None
        base = list(replace) if replace is not None else list(current)
        if add:
            base.extend(add)
        if remove:
            removal = set(remove)
            base = [item for item in base if item not in removal]
        return base

    async def _validate_blocked_by(
        self,
        session_id: int,
        blocked_by: list[int],
        current_task_id: int | None = None,
    ) -> list[int]:
        """校验阻塞任务列表是否合法。"""
        normalized = sorted(set(blocked_by))
        if current_task_id is not None and current_task_id in normalized:
            raise ValidationAppError("TASK_BLOCK_SELF", "任务不能阻塞自己")
        existing = {task.id for task in await self.repo.get_many(session_id, normalized)}
        missing = [task_id for task_id in normalized if task_id not in existing]
        if missing:
            raise ValidationAppError(
                "TASK_BLOCKER_NOT_FOUND",
                "阻塞任务不存在",
                {"missing": missing},
            )
        return normalized
