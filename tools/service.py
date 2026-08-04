from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.event_bus import StreamBus
from core.errors import AgentException
from core.events import ORCHESTRATOR_ACTOR, Actor
from hooks import HookContext, HookEvent, get_hook_registry
from tools.models import ToolCallRecord, ToolRecord
from tools.repository import ToolCatalogRepository, ToolRepository
from tools.base import ToolContext
from tools.registry import ToolRegistry, build_tool_registry
from tools.shell_policy import ShellAccess


@dataclass(frozen=True)
class ToolCatalogPage:
    items: tuple[ToolRecord, ...]
    total: int
    limit: int
    offset: int


class ToolCatalogService:
    """提供内置工具目录同步、查询与绑定校验。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ToolCatalogRepository(db)

    async def sync_builtins(self, registry: ToolRegistry | None = None) -> None:
        active_registry = registry or build_tool_registry()
        for tool in active_registry.tools:
            await self.repo.sync_builtin(
                tool_key=tool.name,
                name=tool.name,
                description=tool.description,
                runtime_name=tool.name,
                input_schema=tool.input_model.model_json_schema(),
            )
        await self.repo.mark_missing_deleted(set(active_registry.names))
        await self.db.flush()

    async def list_tools(
        self,
        *,
        keyword: str | None,
        is_enabled: bool | None,
        limit: int,
        offset: int,
    ) -> ToolCatalogPage:
        records, total = await self.repo.list_page(
            keyword=keyword,
            is_enabled=is_enabled,
            limit=limit,
            offset=offset,
        )
        return ToolCatalogPage(tuple(records), total, limit, offset)

    async def get_required(self, tool_id: int) -> ToolRecord:
        record = await self.repo.get_by_id(tool_id)
        if record is None:
            raise AgentException.message("工具不存在", status_code=404)
        return record

    async def require_bindable_tool_ids(self, tool_ids: list[int]) -> list[int]:
        normalized = list(dict.fromkeys(tool_ids))
        records = await self.repo.list_by_ids(normalized)
        records_by_id = {record.id: record for record in records}
        missing = [tool_id for tool_id in normalized if tool_id not in records_by_id]
        if missing:
            raise AgentException.message(
                "绑定的工具不存在",
                {"tool_ids": missing},
                status_code=404,
            )
        unavailable = [record.id for record in records if not record.is_enabled]
        if unavailable:
            raise AgentException.message(
                "绑定的工具已停用",
                {"tool_ids": unavailable},
                status_code=409,
            )
        registry_names = build_tool_registry().names
        missing_runtime = [
            record.id for record in records if record.runtime_name not in registry_names
        ]
        if missing_runtime:
            raise AgentException.message(
                "绑定的工具实现不存在",
                {"tool_ids": missing_runtime},
                status_code=409,
            )
        return normalized

    async def get_runtime_names(self, tool_ids: list[int]) -> tuple[str, ...]:
        normalized = await self.require_bindable_tool_ids(tool_ids)
        records = await self.repo.list_by_ids(normalized)
        names_by_id = {record.id: record.runtime_name for record in records}
        return tuple(names_by_id[tool_id] for tool_id in normalized)

    async def require_runtime_names(self, runtime_names: list[str]) -> tuple[str, ...]:
        normalized = list(dict.fromkeys(runtime_names))
        records = await self.repo.list_by_runtime_names(normalized)
        records_by_name = {record.runtime_name: record for record in records}
        registry_names = build_tool_registry().names
        unavailable = [
            name
            for name in normalized
            if name not in records_by_name
            or not records_by_name[name].is_enabled
            or name not in registry_names
        ]
        if unavailable:
            raise AgentException.message(
                "Agent绑定的Skill基础工具不可用",
                {"runtime_names": unavailable},
                status_code=409,
            )
        return tuple(normalized)


class ToolService:
    def __init__(self, db: AsyncSession, registry: ToolRegistry):
        """初始化工具执行器依赖。"""
        self.db = db
        self.registry = registry
        self.tool_repo = ToolRepository(db)
        self.catalog_repo = ToolCatalogRepository(db)

    async def start_call(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        *,
        message_id: int | None = None,
        agent_id: int | None = None,
    ) -> ToolCallRecord:
        """创建工具调用记录，并尽可能关联数据库工具目录。"""
        tool_record = await self.catalog_repo.get_by_runtime_name(tool_name)
        return await self.tool_repo.start(
            session_id,
            tool_name,
            input_args,
            message_id=message_id,
            tool_id=tool_record.id if tool_record else None,
            agent_id=agent_id,
        )

    async def get_interaction_call_required(
        self,
        tool_call_id: int,
        session_id: int,
    ) -> ToolCallRecord:
        """按可信会话读取人工交互关联的工具调用。"""
        record = await self.tool_repo.get(tool_call_id)
        if record is None or record.session_id != session_id:
            raise AgentException.message("人工交互关联的工具调用不存在")
        return record

    async def run(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        message_id: int | None = None,
        bus: StreamBus | None = None,
        record: ToolCallRecord | None = None,
        actor: Actor | None = None,
        turn_id: UUID | None = None,
        user_id: int | None = None,
        workspace_id: int | None = None,
        shell_access: ShellAccess = ShellAccess.FULL,
        shell_tmp_root: Path | None = None,
        agent_id: int | None = None,
        allowed_skill_names: frozenset[str] | None = None,
    ) -> str:
        """执行工具并记录调用状态。

        这里是三类 actor(orchestrator / subagent / teammate)工具执行的唯一交汇点,
        因此把 PreToolUse / PostToolUse 两个 hook 挂在此处,一处覆盖全部产出者。

        record 非空时复用既有记录(如审批通过后复用之前的 awaiting 记录),
        回到 running 并以最新 input_args 覆盖(可能被审批时的 updated_input 修改过);
        否则新建一条 running 记录。
        """
        actor = actor or ORCHESTRATOR_ACTOR
        hooks = get_hook_registry()

        # PreToolUse:可拦截(block)或改写入参(updated_input)。permission 已在
        # 更高层(orchestrator _execute_tool_uses)裁决过,此处 hook 只能更严,不能提权。
        pre_outcomes = await hooks.trigger(
            HookContext(
                event=HookEvent.PRE_TOOL_USE,
                session_id=session_id,
                actor=actor,
                turn_id=turn_id,
                tool_name=tool_name,
                tool_input=input_args,
            )
        )
        for outcome in pre_outcomes:
            if outcome.updated_input is not None:
                input_args = outcome.updated_input
        blocked = next((o.block for o in pre_outcomes if o.block is not None), None)

        if record is None:
            record = await self.start_call(
                session_id,
                tool_name,
                input_args,
                message_id=message_id,
                agent_id=agent_id,
            )
        else:
            record.input_args = input_args
            record.status = "running"
            await self.db.flush()

        if blocked is not None:
            # 被 PreToolUse hook 拦截:不执行工具,把拦截理由作为结果回给模型。
            await self.tool_repo.fail(record, blocked)
            await self.db.flush()
            return blocked

        try:
            tool = self.registry.get(tool_name)
            ctx = ToolContext(
                session_id=session_id,
                db=self.db,
                bus=bus,
                turn_id=turn_id,
                user_id=user_id,
                workspace_id=workspace_id,
                allowed_tool_names=self.registry.names,
                allowed_skill_names=allowed_skill_names,
                shell_access=shell_access,
                shell_tmp_root=shell_tmp_root,
            )
            # 开始执行工具
            output = await tool.run_with_dict(input_args, ctx)
        except Exception as exc:
            await self.tool_repo.fail(record, str(exc))
            await self.db.flush()
            # 让"工具失败告警" hook 能观测到;触发后原样上抛。
            await hooks.trigger(
                HookContext(
                    event=HookEvent.POST_TOOL_USE,
                    session_id=session_id,
                    actor=actor,
                    turn_id=turn_id,
                    tool_name=tool_name,
                    tool_input=input_args,
                    tool_output=str(exc),
                    is_error=True,
                )
            )
            raise
        await self.tool_repo.succeed(record, output)
        await self.db.flush()

        # PostToolUse:观察副作用 / 检查输出(返回值不改写结果,只做观测与副作用)。
        await hooks.trigger(
            HookContext(
                event=HookEvent.POST_TOOL_USE,
                session_id=session_id,
                actor=actor,
                turn_id=turn_id,
                tool_name=tool_name,
                tool_input=input_args,
                tool_output=output,
            )
        )
        return output
