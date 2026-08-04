from dataclasses import dataclass
from typing import TypedDict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models import AgentRecord
from agent.repository import AgentRepository
from core.errors import AgentException
from skill.service import SkillService
from tools.service import ToolCatalogService
from workspace.service import WorkspaceService


class AgentUpdateFields(TypedDict, total=False):
    name: str
    description: str | None
    system_prompt: str
    model_name: str | None
    is_enabled: bool


@dataclass(frozen=True)
class AgentPage:
    items: tuple[AgentRecord, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class AgentRuntimeConfig:
    agent_id: int
    system_prompt: str
    model_name: str | None
    tool_runtime_names: tuple[str, ...]
    skill_ids: tuple[int, ...]
    skill_names: tuple[str, ...]


class AgentService:
    """Agent 定义、工作区权限和 Tool/Skill 绑定的业务入口。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = AgentRepository(db)
        self.workspace_service = WorkspaceService(db)
        self.tool_catalog_service = ToolCatalogService(db)
        self.skill_service = SkillService(db)

    async def create_agent(
        self,
        *,
        workspace_id: int,
        actor_user_id: int,
        name: str,
        description: str | None,
        system_prompt: str,
        model_name: str | None,
        is_enabled: bool,
        tool_ids: list[int],
        skill_ids: list[int],
    ) -> AgentRecord:
        await self._require_write_access(workspace_id, actor_user_id)
        await self._require_name_available(workspace_id, name)
        normalized_tool_ids = await self.tool_catalog_service.require_bindable_tool_ids(tool_ids)
        normalized_skill_ids = await self.skill_service.require_bindable_skill_ids(
            workspace_id,
            skill_ids,
        )
        try:
            agent = await self.repo.create(
                workspace_id=workspace_id,
                name=name,
                description=description,
                system_prompt=system_prompt,
                model_name=model_name,
                is_enabled=is_enabled,
                created_by_user_id=actor_user_id,
            )
        except IntegrityError as exc:
            raise AgentException.message("Agent名称已存在", status_code=409) from exc

        await self.repo.replace_tool_bindings(agent.id, normalized_tool_ids)
        await self.repo.replace_skill_bindings(agent.id, normalized_skill_ids)
        return await self._get_required(workspace_id, agent.id)

    async def list_agents(
        self,
        workspace_id: int,
        actor_user_id: int,
        *,
        keyword: str | None = None,
        is_enabled: bool | None = None,
        limit: int,
        offset: int,
    ) -> AgentPage:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        records, total = await self.repo.list_and_count(
            workspace_id,
            keyword=keyword,
            is_enabled=is_enabled,
            limit=limit,
            offset=offset,
        )
        return AgentPage(tuple(records), total, limit, offset)

    async def get_agent(
        self,
        workspace_id: int,
        agent_id: int,
        actor_user_id: int,
    ) -> AgentRecord:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        return await self._get_required(workspace_id, agent_id)

    async def update_agent(
        self,
        workspace_id: int,
        agent_id: int,
        actor_user_id: int,
        updates: AgentUpdateFields,
    ) -> AgentRecord:
        await self._require_write_access(workspace_id, actor_user_id)
        agent = await self._get_required_for_update(workspace_id, agent_id)
        if "name" in updates and updates["name"] != agent.name:
            await self._require_name_available(
                workspace_id,
                updates["name"],
                exclude_agent_id=agent.id,
            )

        for field_name, value in updates.items():
            setattr(agent, field_name, value)
        try:
            await self.repo.update(agent)
        except IntegrityError as exc:
            raise AgentException.message("Agent名称已存在", status_code=409) from exc
        return await self._get_required(workspace_id, agent.id)

    async def delete_agent(
        self,
        workspace_id: int,
        agent_id: int,
        actor_user_id: int,
    ) -> None:
        await self._require_write_access(workspace_id, actor_user_id)
        agent = await self._get_required_for_update(workspace_id, agent_id)
        await self.repo.soft_delete(agent)

    async def replace_tool_bindings(
        self,
        workspace_id: int,
        agent_id: int,
        actor_user_id: int,
        tool_ids: list[int],
    ) -> AgentRecord:
        await self._require_write_access(workspace_id, actor_user_id)
        agent = await self._get_required_for_update(workspace_id, agent_id)
        normalized_ids = await self.tool_catalog_service.require_bindable_tool_ids(tool_ids)
        await self.repo.replace_tool_bindings(agent.id, normalized_ids)
        return await self._get_required(workspace_id, agent.id)

    async def replace_skill_bindings(
        self,
        workspace_id: int,
        agent_id: int,
        actor_user_id: int,
        skill_ids: list[int],
    ) -> AgentRecord:
        await self._require_write_access(workspace_id, actor_user_id)
        agent = await self._get_required_for_update(workspace_id, agent_id)
        normalized_ids = await self.skill_service.require_bindable_skill_ids(
            workspace_id,
            skill_ids,
        )
        await self.repo.replace_skill_bindings(agent.id, normalized_ids)
        return await self._get_required(workspace_id, agent.id)

    async def require_runnable(self, agent_id: int, workspace_id: int) -> AgentRecord:
        """按可信工作区读取可运行Agent，供Session和运行时复用。"""
        agent = await self._get_required(workspace_id, agent_id)
        if not agent.is_enabled:
            raise AgentException.message("Agent已停用", status_code=409)
        return agent

    async def get_runtime_config(
        self,
        workspace_id: int,
        agent_id: int,
    ) -> AgentRuntimeConfig:
        agent = await self.require_runnable(agent_id, workspace_id)
        tool_ids = sorted(binding.tool_id for binding in agent.tool_bindings)
        skill_ids = sorted(binding.skill_id for binding in agent.skill_bindings)
        tool_names = await self.tool_catalog_service.get_runtime_names(tool_ids)
        skill_names = await self.skill_service.get_names_by_ids(workspace_id, skill_ids)
        if skill_names:
            skill_tool_names = await self.tool_catalog_service.require_runtime_names(
                ["Skill", "SkillResource", "SkillRun"]
            )
            tool_names = tuple(dict.fromkeys((*tool_names, *skill_tool_names)))
        return AgentRuntimeConfig(
            agent_id=agent.id,
            system_prompt=agent.system_prompt,
            model_name=agent.model_name,
            tool_runtime_names=tool_names,
            skill_ids=tuple(skill_ids),
            skill_names=skill_names,
        )

    async def remove_skill_bindings(self, workspace_id: int, skill_id: int) -> None:
        """供Skill域删除资产时清理本域绑定。"""
        await self.repo.soft_delete_skill_bindings(workspace_id, skill_id)

    async def _get_required(self, workspace_id: int, agent_id: int) -> AgentRecord:
        agent = await self.repo.get(workspace_id, agent_id)
        if agent is None:
            raise AgentException.message("Agent不存在", status_code=404)
        return agent

    async def _get_required_for_update(
        self,
        workspace_id: int,
        agent_id: int,
    ) -> AgentRecord:
        agent = await self.repo.get_for_update(workspace_id, agent_id)
        if agent is None:
            raise AgentException.message("Agent不存在", status_code=404)
        return agent

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
        exclude_agent_id: int | None = None,
    ) -> None:
        existing = await self.repo.get_by_name(
            workspace_id,
            name,
            exclude_agent_id=exclude_agent_id,
        )
        if existing is not None:
            raise AgentException.message("Agent名称已存在", status_code=409)
