from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.service import AgentService
from core.errors import AgentException
from workflow.models import WorkflowMode, WorkflowRecord
from workflow.repository import WorkflowRepository
from workspace.service import WorkspaceService


class WorkflowUpdateFields(TypedDict, total=False):
    name: str
    description: str | None
    mode: WorkflowMode
    is_enabled: bool


@dataclass(frozen=True)
class WorkflowMemberInput:
    """保存期的成员输入，仅携带绑定关系与顺序。"""

    agent_id: int
    sort_order: int = 0


@dataclass(frozen=True)
class WorkflowRunMember:
    """运行期成员视图，已按 (sort_order, agent_id) 升序。"""

    agent_id: int
    sort_order: int


@dataclass(frozen=True)
class WorkflowModeMeta:
    mode: WorkflowMode
    name: str
    description: str


@dataclass(frozen=True)
class WorkflowPage:
    items: tuple[WorkflowRecord, ...]
    total: int
    limit: int
    offset: int


# 三种模式的静态元数据，不落库；差异全部体现在运行时编排器。
_MODE_METAS: tuple[WorkflowModeMeta, ...] = (
    WorkflowModeMeta(
        mode=WorkflowMode.SEQUENTIAL,
        name="顺序执行",
        description="成员按 sort_order 依次执行，后一个成员同时收到用户原始提问与前一个成员的输出。",
    ),
    WorkflowModeMeta(
        mode=WorkflowMode.PARALLEL,
        name="并行执行",
        description="全部成员并发执行同一提问，最终由 LLM 汇总各成员结果；至少需要 2 个成员。",
    ),
    WorkflowModeMeta(
        mode=WorkflowMode.LLM_ROUTING,
        name="智能路由",
        description="由 LLM 依据成员描述挑选唯一一个成员执行，其余成员不运行；至少需要 2 个成员。",
    ),
)


class WorkflowService:
    """Workflow 定义、成员绑定与工作区权限的业务入口。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = WorkflowRepository(db)
        self.workspace_service = WorkspaceService(db)
        self.agent_service = AgentService(db)

    async def create_workflow(
        self,
        *,
        workspace_id: int,
        actor_user_id: int,
        name: str,
        description: str | None,
        mode: WorkflowMode,
        is_enabled: bool,
        members: Sequence[WorkflowMemberInput],
    ) -> WorkflowRecord:
        await self._require_write_access(workspace_id, actor_user_id)
        await self._require_name_available(workspace_id, name)
        normalized_members = await self._require_bindable_members(workspace_id, members)
        try:
            workflow = await self.repo.create(
                workspace_id=workspace_id,
                name=name,
                description=description,
                mode=mode.value,
                is_enabled=is_enabled,
                created_by_user_id=actor_user_id,
            )
        except IntegrityError as exc:
            raise AgentException.message("Workflow名称已存在", status_code=409) from exc

        await self.repo.replace_members(workflow.id, normalized_members)
        return await self._get_required(workspace_id, workflow.id)

    async def list_workflows(
        self,
        workspace_id: int,
        actor_user_id: int,
        *,
        keyword: str | None = None,
        is_enabled: bool | None = None,
        limit: int,
        offset: int,
    ) -> WorkflowPage:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        records, total = await self.repo.list_and_count(
            workspace_id,
            keyword=keyword,
            is_enabled=is_enabled,
            limit=limit,
            offset=offset,
        )
        return WorkflowPage(tuple(records), total, limit, offset)

    async def get_workflow(
        self,
        workspace_id: int,
        workflow_id: int,
        actor_user_id: int,
    ) -> WorkflowRecord:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        return await self._get_required(workspace_id, workflow_id)

    async def update_workflow(
        self,
        workspace_id: int,
        workflow_id: int,
        actor_user_id: int,
        updates: WorkflowUpdateFields,
        *,
        members: Sequence[WorkflowMemberInput] | None = None,
    ) -> WorkflowRecord:
        await self._require_write_access(workspace_id, actor_user_id)
        workflow = await self._get_required_for_update(workspace_id, workflow_id)
        if "name" in updates and updates["name"] != workflow.name:
            await self._require_name_available(
                workspace_id,
                updates["name"],
                exclude_workflow_id=workflow.id,
            )
        normalized_members = (
            await self._require_bindable_members(workspace_id, members)
            if members is not None
            else None
        )

        for field_name, value in updates.items():
            setattr(workflow, field_name, value.value if isinstance(value, WorkflowMode) else value)
        try:
            await self.repo.update(workflow)
        except IntegrityError as exc:
            raise AgentException.message("Workflow名称已存在", status_code=409) from exc
        if normalized_members is not None:
            await self.repo.replace_members(workflow.id, normalized_members)
        return await self._get_required(workspace_id, workflow.id)

    async def delete_workflow(
        self,
        workspace_id: int,
        workflow_id: int,
        actor_user_id: int,
    ) -> None:
        await self._require_write_access(workspace_id, actor_user_id)
        workflow = await self._get_required_for_update(workspace_id, workflow_id)
        await self.repo.soft_delete(workflow)

    async def require_runnable(self, workflow_id: int, workspace_id: int) -> WorkflowRecord:
        """按可信工作区读取可运行Workflow，供Session和编排运行时复用。"""
        workflow = await self._get_required(workspace_id, workflow_id)
        if not workflow.is_enabled:
            raise AgentException.message("Workflow已停用", status_code=409)
        return workflow

    async def get_run_members(
        self,
        workflow_id: int,
        workspace_id: int,
    ) -> tuple[WorkflowRunMember, ...]:
        """返回可运行Workflow的成员序列，按 (sort_order, agent_id) 升序。

        成员 Agent 自身是否可运行由运行时逐个经 AgentService.require_runnable 校验，
        这里只负责编排顺序，不重复查询 Agent。
        """
        workflow = await self.require_runnable(workflow_id, workspace_id)
        ordered = sorted(workflow.agents, key=lambda item: (item.sort_order, item.agent_id))
        return tuple(
            WorkflowRunMember(agent_id=item.agent_id, sort_order=item.sort_order)
            for item in ordered
        )

    @staticmethod
    def list_modes() -> tuple[WorkflowModeMeta, ...]:
        """三种内置编排模式的静态元数据，不查库。"""
        return _MODE_METAS

    async def _require_bindable_members(
        self,
        workspace_id: int,
        members: Sequence[WorkflowMemberInput],
    ) -> dict[int, int]:
        """校验成员非空、无重复且均属于本工作区，返回 {agent_id: sort_order}。"""
        if not members:
            raise AgentException.message("Workflow至少需要绑定1个成员Agent")
        agent_ids = [member.agent_id for member in members]
        duplicated = sorted({agent_id for agent_id in agent_ids if agent_ids.count(agent_id) > 1})
        if duplicated:
            raise AgentException.message(
                "同一Agent不能在Workflow中重复绑定",
                {"agent_ids": duplicated},
            )
        await self.agent_service.require_bindable_agent_ids(workspace_id, agent_ids)
        return {member.agent_id: member.sort_order for member in members}

    async def _get_required(self, workspace_id: int, workflow_id: int) -> WorkflowRecord:
        workflow = await self.repo.get(workspace_id, workflow_id)
        if workflow is None:
            raise AgentException.message("Workflow不存在", status_code=404)
        return workflow

    async def _get_required_for_update(
        self,
        workspace_id: int,
        workflow_id: int,
    ) -> WorkflowRecord:
        workflow = await self.repo.get_for_update(workspace_id, workflow_id)
        if workflow is None:
            raise AgentException.message("Workflow不存在", status_code=404)
        return workflow

    async def _require_write_access(self, workspace_id: int, actor_user_id: int) -> None:
        await self.workspace_service.require_workspace_role(
            workspace_id,
            actor_user_id,
            {"owner", "admin"},
        )

    async def _require_name_available(
        self,
        workspace_id: int,
        name: str,
        *,
        exclude_workflow_id: int | None = None,
    ) -> None:
        existing = await self.repo.get_by_name(
            workspace_id,
            name,
            exclude_workflow_id=exclude_workflow_id,
        )
        if existing is not None:
            raise AgentException.message("Workflow名称已存在", status_code=409)
