from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import RuntimeEvent
from models.task import TaskRecord
from repositories.task_repo import TaskRepository
from schemas.task import TaskResponse
from session.service import SessionService

VALID_TASK_STATUSES = {"pending", "in_progress", "completed"}


class TaskService:
    def __init__(self, db: AsyncSession, bus: StreamBus | None = None):
        """初始化任务服务依赖。

        bus 缺省为 None:REST 只读端点与 TaskGet/TaskList 不需要推送,不传即可。
        经 TaskCreate/TaskUpdate 工具调用时由 ctx.bus 注入,变更后 emit 全量快照。
        """
        self.repo = TaskRepository(db)
        self.session_service = SessionService(db)
        self.bus = bus

    async def emit_snapshot(self, session_id: int, reason: str | None = None) -> None:
        """拉当前 session 全量任务,emit 一帧 task_snapshot。bus 缺省则静默跳过。

        读取的是当前事务内的数据(可能未 commit),与主循环回合末尾的落库一致;
        若该回合后续异常回滚,前端会先看到一个乐观快照,由下次操作或首帧 baseline 自愈。
        """
        if self.bus is None:
            return
        tasks: list[TaskRecord] = await self.repo.list_by_session(session_id)
        payload = [TaskResponse.model_validate(t).model_dump(mode="json") for t in tasks]
        await self.bus.emit(RuntimeEvent.task_snapshot(session_id, payload, reason))

    async def list_by_session(self, session_id: int) -> list[TaskRecord]:
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
    ) -> TaskRecord:
        """创建任务。"""
        await self.session_service.get_required(session_id)
        self._validate_status("pending")
        normalized_blocked_by: list[int] = await self._validate_blocked_by(session_id, blocked_by or [])
        task: TaskRecord = await self.repo.create(
            session_id=session_id,
            subject=subject,
            description=description,
            owner=owner,
            blocked_by=normalized_blocked_by,
        )
        await self.emit_snapshot(session_id, "created")
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
    ) -> TaskRecord:
        """更新任务字段。"""
        await self.session_service.get_required(session_id)
        task: TaskRecord = await self.repo.get(session_id, task_id)
        if task is None:
            raise AgentException.message("任务不存在")
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
        updated: TaskRecord = await self.repo.update(
            task,
            subject=subject,
            description=description,
            status=status,
            owner=owner,
            blocked_by=normalized_blocked_by,
        )
        if status == "completed":
            await self.repo.remove_blocker_from_others(session_id, task_id)
        # 级联解锁后再拉快照:被摘除 blocker 的下游任务的新 blocked_by 天然进快照。
        await self.emit_snapshot(session_id, "updated")
        return updated

    def _validate_status(self, status: str) -> None:
        """校验任务状态是否合法。"""
        if status not in VALID_TASK_STATUSES:
            raise AgentException.message("任务状态无效")

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
            raise AgentException.message("任务不能阻塞自己")
        existing = {task.id for task in await self.repo.get_many(session_id, normalized)}
        missing = [task_id for task_id in normalized if task_id not in existing]
        if missing:
            raise AgentException.message("阻塞任务不存在", {"missing": missing})
        return normalized
