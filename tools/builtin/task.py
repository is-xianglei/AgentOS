import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from task.schemas import TaskResponse
from task.service import TaskService
from tools.base import BaseTool, ToolContext


# ---------------------------------------------------------------------------
# 共享 helper:TaskCreate / TaskGet / TaskUpdate 都返回同一份任务 JSON
# ---------------------------------------------------------------------------
def _task_data(task) -> dict[str, Any]:
    """将任务实体序列化为可直接 json.dumps 的字典。"""
    return TaskResponse.model_validate(task).model_dump(mode="json")


# TaskList 文本中的状态标记;未知状态显示为 [?]。
_STATUS_MARKERS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}


# ---------------------------------------------------------------------------
# TaskCreate:创建任务
# ---------------------------------------------------------------------------
class TaskCreateInput(BaseModel):
    subject: str = Field(description="任务主题(必填)")
    description: str | None = Field(default=None, description="任务描述")
    owner: str | None = Field(
        default=None,
        description="兼容字段,系统使用当前运行身份作为负责人,忽略调用方提交的值",
    )
    blocked_by: list[int] | None = Field(
        default=None,
        description="阻塞当前任务的任务ID列表",
    )


class TaskCreateTool(BaseTool):
    name = "TaskCreate"
    description = "在当前会话中创建一个新任务,返回该任务的详细信息。"
    input_model = TaskCreateInput

    async def run(self, args: TaskCreateInput, ctx: ToolContext) -> str:
        service = TaskService(ctx.db, bus=ctx.bus)
        task = await service.create(
            session_id=ctx.session_id,
            subject=args.subject,
            description=args.description or "",
            owner=ctx.principal_name or "lead",
            blocked_by=args.blocked_by or [],
        )
        return json.dumps(_task_data(task), ensure_ascii=False)


# ---------------------------------------------------------------------------
# TaskGet:按 id 查询单个任务详情
# ---------------------------------------------------------------------------
class TaskGetInput(BaseModel):
    id: int = Field(description="要查询的任务ID(必填)")


class TaskGetTool(BaseTool):
    name = "TaskGet"
    description = "按任务ID查询当前会话中某个任务的详细信息;任务不存在时返回提示。"
    input_model = TaskGetInput

    async def run(self, args: TaskGetInput, ctx: ToolContext) -> str:
        service = TaskService(ctx.db)
        task = await service.get(ctx.session_id, args.id)
        if task is None:
            return f"任务不存在: #{args.id}"
        return json.dumps(_task_data(task), ensure_ascii=False)


# ---------------------------------------------------------------------------
# TaskUpdate:更新任务字段/状态/阻塞关系
# ---------------------------------------------------------------------------
class TaskUpdateInput(BaseModel):
    id: int = Field(description="要更新的任务ID(必填)")
    subject: str | None = Field(default=None, description="任务主题")
    description: str | None = Field(default=None, description="任务描述")
    status: Literal["pending", "in_progress", "completed"] | None = Field(
        default=None, description="任务状态"
    )
    owner: str | None = Field(
        default=None,
        description="兼容字段,系统使用当前运行身份,忽略调用方提交的负责人名称",
    )
    blocked_by: list[int] | None = Field(
        default=None,
        description="阻塞任务列表(整体替换),与 add_blocked_by/remove_blocked_by 互斥",
    )
    add_blocked_by: list[int] | None = Field(
        default=None,
        description="追加到阻塞任务列表的任务ID",
    )
    remove_blocked_by: list[int] | None = Field(
        default=None,
        description="从阻塞任务列表移除的任务ID",
    )

    @model_validator(mode="after")
    def validate_blocked_by(self):
        # blocked_by(整体替换)与 add/remove(增量)互斥
        uses_incremental = self.add_blocked_by is not None or self.remove_blocked_by is not None
        if self.blocked_by is not None and uses_incremental:
            raise ValueError("blocked_by 与 add_blocked_by/remove_blocked_by 不能同时使用")
        return self


class TaskUpdateTool(BaseTool):
    name = "TaskUpdate"
    description = "更新当前会话中某个任务的字段、状态或阻塞关系,返回更新后的任务详情。"
    input_model = TaskUpdateInput

    async def run(self, args: TaskUpdateInput, ctx: ToolContext) -> str:
        service = TaskService(ctx.db, bus=ctx.bus)
        # 调用方提交了 owner 才改负责人,且只能改成当前运行身份;未提交则不改。
        task = await service.update(
            session_id=ctx.session_id,
            task_id=args.id,
            subject=args.subject,
            description=args.description,
            status=args.status,
            owner=ctx.principal_name if args.owner is not None else None,
            blocked_by=args.blocked_by,
            add_blocked_by=args.add_blocked_by,
            remove_blocked_by=args.remove_blocked_by,
        )
        return json.dumps(_task_data(task), ensure_ascii=False)


# ---------------------------------------------------------------------------
# TaskList:列出本会话所有任务
# ---------------------------------------------------------------------------
class TaskListInput(BaseModel):
    """TaskList 无需任何入参。"""


class TaskListTool(BaseTool):
    name = "TaskList"
    description = "列出当前会话中的所有任务,以带状态标记的文本形式返回。"
    input_model = TaskListInput

    async def run(self, args: TaskListInput, ctx: ToolContext) -> str:
        service = TaskService(ctx.db)
        tasks = await service.list_by_session(ctx.session_id)
        if not tasks:
            return "(no tasks)"

        lines = []
        for task in tasks:
            marker = _STATUS_MARKERS.get(task.status, "[?]")
            blocked = f" (blocked by: {list(task.blocked_by)})" if task.blocked_by else ""
            lines.append(f"{marker} #{task.id}: {task.subject}{blocked}")
        return "\n".join(lines)
