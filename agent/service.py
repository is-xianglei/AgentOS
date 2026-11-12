from dataclasses import dataclass
from typing import Any, TypedDict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models import AgentRecord
from agent.repository import AgentRepository, McpBindingRow
from core.errors import AgentException
from harness.domain.mcp import bound_model_names, mcp_source
from harness.domain.tool import ToolBinding
from mcp_server.service import McpServerService
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
class AgentMcpBindingInput:
    """一条待写入的 MCP 绑定：tool_names 是 MCP 原名白名单，None 表示该 server 的全部工具。"""

    server_id: int
    tool_names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class AgentMcpBinding:
    """运行时用的一条 MCP 绑定：来源 Tool.source（mcp:{server_key}）与模型可见名白名单。

    tool_names 为 None 表示该 server 的全部工具；原名已按 server_key 换算成模型名。
    """

    source: str
    tool_names: frozenset[str] | None = None


@dataclass(frozen=True)
class AgentRuntimeConfig:
    agent_id: int
    system_prompt: str
    model_name: str | None
    tool_runtime_names: tuple[str, ...]
    skill_ids: tuple[int, ...]
    skill_names: tuple[str, ...]
    # 供 workflow 编排复用:name 作成员运行标识,description 作 LLM 路由的判断依据。
    name: str
    description: str | None
    # 仍可用（已启用的共享 server）的 MCP 绑定，按来源排序。带默认值：黄金测试按全部旧字段构造本类型。
    mcp_bindings: tuple[AgentMcpBinding, ...] = ()

    def tool_binding(self) -> ToolBinding:
        """这个 Agent 可见哪些作用域工具：只有绑定的 server，没有绑定时一个都不可见。"""
        return ToolBinding.servers(
            {binding.source: binding.tool_names for binding in self.mcp_bindings}
        )


@dataclass(frozen=True)
class DelegateCatalogEntry:
    """默认 Agent 系统提示里「可委派 Agent 目录」的一项。"""

    agent_id: int
    name: str
    description: str | None
    # 只作给模型看的能力摘要;委派时 get_runtime_config 会对绑定重新做严格校验。
    tool_names: tuple[str, ...]


class AgentDefinitionService:
    """Agent 定义、工作区权限和 Tool/Skill 绑定的业务入口。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = AgentRepository(db)
        self.workspace_service = WorkspaceService(db)
        self.tool_catalog_service = ToolCatalogService(db)
        self.skill_service = SkillService(db)
        self.mcp_server_service = McpServerService(db)

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
        normalized_tool_ids = await self.tool_catalog_service.require_bindable_tool_ids(
            tool_ids,
            profile="user_agent",
        )
        normalized_skill_ids = await self.skill_service.require_bindable_skill_ids(
            workspace_id,
            skill_ids,
        )
        # 名称预检查无法阻止并发创建同名 Agent，由唯一索引兜底并转成 409。
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

        # updates 只包含客户端显式提交的字段，字段名与 AgentRecord 列名一致。
        for field_name, value in updates.items():
            setattr(agent, field_name, value)
        # 预检查之后仍可能有并发请求抢先占用同名，由唯一索引兜底并转成 409。
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
        normalized_ids = await self.tool_catalog_service.require_bindable_tool_ids(
            tool_ids,
            profile="user_agent",
        )
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

    async def replace_mcp_bindings(
        self,
        workspace_id: int,
        agent_id: int,
        actor_user_id: int,
        bindings: list[AgentMcpBindingInput],
    ) -> AgentRecord:
        """全量替换 Agent 的 MCP server 绑定；权限与行锁同 replace_tool_bindings。

        只能绑定本工作区已启用的共享 server，校验由 mcp_server 域的 require_usable 负责；
        同一 server 不能出现两次，工具白名单写空列表报错（绑定全部工具请传 None）。
        """
        await self._require_write_access(workspace_id, actor_user_id)
        agent = await self._get_required_for_update(workspace_id, agent_id)
        rows = self._normalize_mcp_bindings(bindings)
        await self.mcp_server_service.require_usable(
            workspace_id,
            [row.server_id for row in rows],
            actor_user_id=actor_user_id,
        )
        await self.repo.replace_mcp_bindings(agent.id, rows)
        return await self._get_required(workspace_id, agent.id)

    @staticmethod
    def _normalize_mcp_bindings(bindings: list[AgentMcpBindingInput]) -> list[McpBindingRow]:
        """校验并规范化绑定：server 不重复、白名单非空且去重（保留顺序）。"""
        seen: set[int] = set()
        duplicated: list[int] = []
        rows: list[McpBindingRow] = []
        for binding in bindings:
            if binding.server_id in seen:
                duplicated.append(binding.server_id)
                continue
            seen.add(binding.server_id)
            tool_names: list[str] | None = None
            if binding.tool_names is not None:
                if not binding.tool_names:
                    # 空列表若当成「全部」就是放权，当成「一个都不给」又等于没绑定，两种理解都容易出错。
                    raise AgentException.message(
                        "MCP 工具白名单不能为空列表；要绑定该 server 的全部工具请传 null",
                        {"server_id": binding.server_id},
                        status_code=400,
                    )
                tool_names = list(dict.fromkeys(binding.tool_names))
            rows.append(McpBindingRow(server_id=binding.server_id, tool_names=tool_names))
        if duplicated:
            raise AgentException.message(
                "同一个 MCP server 只能绑定一次",
                {"server_ids": sorted(set(duplicated))},
                status_code=400,
            )
        return rows

    async def unbind_mcp_server(self, server_id: int) -> None:
        """供 mcp_server 域删除 server 时软删全部 Agent 对它的绑定；下一回合起这些 Agent 就看不到它。"""
        await self.repo.soft_delete_mcp_bindings_for_server(server_id)

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
        tool_names = await self.tool_catalog_service.get_runtime_names(
            tool_ids,
            profile="user_agent",
        )
        # skill_ids 保留全部绑定(含禁用),skill_names 只含已启用的,作为运行时可调用白名单。
        skill_names = await self.skill_service.get_enabled_names_by_ids(workspace_id, skill_ids)
        # 有可用 Skill 时自动补上 Skill 系列工具，否则模型无法调用 Skill；
        # 合并时去重并保持原有顺序。
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
            name=agent.name,
            description=agent.description,
            mcp_bindings=await self._runtime_mcp_bindings(workspace_id, agent),
        )

    async def _runtime_mcp_bindings(
        self,
        workspace_id: int,
        agent: AgentRecord,
    ) -> tuple[AgentMcpBinding, ...]:
        """把绑定换成运行时形状：server_id 换成来源 mcp:{server_key}，原名换成模型名。

        只留仍可用的 server（已启用的共享 server）：停用的 server 暂时不可见，但绑定保留，
        重新启用后恢复；这里不报错，免得一个 server 被停用就让整个 Agent 跑不起来。
        """
        if not agent.mcp_bindings:
            return ()
        keys = await self.mcp_server_service.enabled_shared_keys(
            workspace_id,
            [binding.server_id for binding in agent.mcp_bindings],
        )
        result: list[AgentMcpBinding] = []
        for binding in agent.mcp_bindings:
            server_key = keys.get(binding.server_id)
            if server_key is None:
                continue
            result.append(
                AgentMcpBinding(
                    source=mcp_source(server_key),
                    tool_names=bound_model_names(server_key, binding.tool_names),
                )
            )
        return tuple(sorted(result, key=lambda item: item.source))

    async def list_delegate_catalog(self, workspace_id: int) -> tuple[DelegateCatalogEntry, ...]:
        """返回工作区内全部已启用的 Agent,供默认 Agent 的可委派目录使用;不分页、不检索。

        工具摘要只列仍启用的工具:个别 Agent 的绑定失效时不能让整份目录失败,
        否则默认 Agent 的整轮对话都会被拖垮;失效绑定留到委派时由运行配置校验报错。
        """
        records = await self.repo.list_enabled(workspace_id)
        tool_ids: list[int] = []
        for record in records:
            for binding in record.tool_bindings:
                tool_ids.append(binding.tool_id)
        tool_names_by_id = await self.tool_catalog_service.get_enabled_runtime_names_by_ids(
            tool_ids
        )
        entries: list[DelegateCatalogEntry] = []
        for record in records:
            # 与 get_runtime_config 一致按工具 ID 排序,摘要顺序与运行时白名单一致。
            tool_names: list[str] = []
            for tool_id in sorted(binding.tool_id for binding in record.tool_bindings):
                tool_name = tool_names_by_id.get(tool_id)
                if tool_name is not None:
                    tool_names.append(tool_name)
            entries.append(
                DelegateCatalogEntry(
                    agent_id=record.id,
                    name=record.name,
                    description=record.description,
                    tool_names=tuple(tool_names),
                )
            )
        return tuple(entries)

    async def remove_skill_bindings(self, workspace_id: int, skill_id: int) -> None:
        """供Skill域删除资产时清理本域绑定。"""
        await self.repo.soft_delete_skill_bindings(workspace_id, skill_id)

    async def require_bindable_agent_ids(
        self,
        workspace_id: int,
        agent_ids: list[int],
    ) -> list[int]:
        """校验Agent均存在且属于当前工作区，返回去重后的ID；供Workflow域绑定成员时复用。"""
        # 去重但保留传入顺序，成员顺序由调用方决定。
        normalized = list(dict.fromkeys(agent_ids))
        records = await self.repo.list_by_ids(workspace_id, normalized)
        existing_ids = {record.id for record in records}
        missing: list[int] = []
        for agent_id in normalized:
            if agent_id not in existing_ids:
                missing.append(agent_id)
        if missing:
            raise AgentException.message(
                "绑定的Agent不存在",
                {"agent_ids": missing},
                status_code=404,
            )
        return normalized

    async def build_snapshot(self, workspace_id: int, agent_id: int) -> dict[str, Any]:
        """生成 Agent 快照，供市场上架使用。

        tool_keys 取绑定的工具（目录里已删除的工具不再出现）；skills 取绑定的全部 Skill
        的完整快照，已禁用的也算。不带 model_name、is_enabled 和各类 ID。
        两个列表都排好序，同样的内容总是得到同样的快照。
        """
        agent = await self._get_required(workspace_id, agent_id)

        tool_ids: list[int] = []
        for binding in agent.tool_bindings:
            tool_ids.append(binding.tool_id)
        tool_keys_by_id = await self.tool_catalog_service.get_tool_keys_by_ids(tool_ids)
        tool_keys = sorted(tool_keys_by_id.values())

        skills: list[dict[str, Any]] = []
        for binding in agent.skill_bindings:
            skill = await self.skill_service.build_snapshot(workspace_id, binding.skill_id)
            skills.append(skill)
        skills.sort(key=lambda skill: skill["name"])

        return {
            "agent": {
                "name": agent.name,
                "description": agent.description,
                "system_prompt": agent.system_prompt,
                "tool_keys": tool_keys,
            },
            "skills": skills,
        }

    async def is_name_taken(self, workspace_id: int, name: str) -> bool:
        """工作区内是否已有同名的有效 Agent；已停用的算，已软删除的不算，与唯一索引口径一致。"""
        existing = await self.repo.get_by_name(workspace_id, name)
        return existing is not None

    async def filter_existing_ids(self, workspace_id: int, agent_ids: list[int]) -> set[int]:
        """返回其中仍存在（未软删除）的 Agent ID，已停用的也算存在；不存在的不报错。"""
        records = await self.repo.list_by_ids(workspace_id, list(dict.fromkeys(agent_ids)))
        existing_ids: set[int] = set()
        for record in records:
            existing_ids.add(record.id)
        return existing_ids

    async def filter_existing_ids_across_workspaces(self, agent_ids: list[int]) -> set[int]:
        """不限工作区，返回其中仍存在（未软删除）的 Agent ID，已停用的也算存在。

        供市场统计安装次数：安装记录分布在各个工作区，要一次批量确认装出来的 Agent 还在。
        """
        existing_ids = await self.repo.list_existing_ids(list(dict.fromkeys(agent_ids)))
        return set(existing_ids)

    async def overwrite_for_upgrade(
        self,
        workspace_id: int,
        agent_id: int,
        actor_user_id: int,
        *,
        description: str | None,
        system_prompt: str,
        tool_ids: list[int],
        skill_ids: list[int],
    ) -> AgentRecord:
        """市场升级时覆盖描述、系统提示词和全部工具、Skill 绑定；名称、启用状态和模型保留本地设置。

        直接组合现有的更新方法和绑定替换方法，权限、行锁和绑定校验都沿用它们的。
        """
        updates: AgentUpdateFields = {
            "description": description,
            "system_prompt": system_prompt,
        }
        await self.update_agent(workspace_id, agent_id, actor_user_id, updates)
        await self.replace_tool_bindings(workspace_id, agent_id, actor_user_id, tool_ids)
        return await self.replace_skill_bindings(workspace_id, agent_id, actor_user_id, skill_ids)

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
        """加行锁读取，串行化同一 Agent 的并发更新、删除与绑定替换。"""
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
