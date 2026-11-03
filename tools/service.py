import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import ORCHESTRATOR_ACTOR, Actor
from hooks import HookContext, HookEvent, get_hook_registry
from tools.base import ToolContext
from tools.context import ToolExecutionContext
from tools.models import ToolCallRecord, ToolRecord
from tools.policy import (
    ToolProfile,
    can_bind_tool,
    can_call_tool,
    get_tool_policy,
    requires_skill_allowlist,
)
from tools.registry import ToolRegistry, build_tool_registry
from tools.repository import ToolCatalogRepository, ToolRepository
from tools.shell_policy import ShellAccess

logger = logging.getLogger(__name__)


def _profile_from_actor(actor: Actor) -> ToolProfile:
    """兼容旧调用点的 actor -> profile 映射。

    新运行时应显式传入 profile；这里仅为直接使用 ToolService 的旧代码保留
    安全默认值。显示名称永远不参与能力判定。
    """
    role = actor.role.value
    if role == "subagent":
        return "subagent"
    if role == "teammate":
        return "teammate"
    return "lead"


def _principal_from_actor(actor: Actor) -> str:
    return "lead" if actor.role.value == "orchestrator" else actor.name


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

    async def require_bindable_tool_ids(
        self,
        tool_ids: list[int],
        *,
        profile: ToolProfile | None = None,
    ) -> list[int]:
        """校验工具存在、可运行，并按执行档位过滤可绑定工具。

        profile 为空时保留旧行为，供系统内部目录同步与兼容调用使用；用户 Agent
        创建和更新必须显式传入 ``user_agent``，避免把控制平面工具绑定到普通 Agent。
        """
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
        # 未指定 profile 时不做档位过滤。
        if profile is None:
            return normalized

        forbidden = [
            record for record in records if not can_bind_tool(record.runtime_name, profile)
        ]
        if forbidden:
            raise AgentException.message(
                "当前Agent类型不允许绑定这些工具",
                {
                    "profile": profile,
                    "tool_ids": [record.id for record in forbidden],
                    "tool_names": [record.runtime_name for record in forbidden],
                },
                status_code=403,
            )
        return normalized

    async def get_runtime_names(
        self,
        tool_ids: list[int],
        *,
        profile: ToolProfile | None = None,
    ) -> tuple[str, ...]:
        normalized = await self.require_bindable_tool_ids(tool_ids, profile=profile)
        records = await self.repo.list_by_ids(normalized)
        names_by_id = {record.id: record.runtime_name for record in records}
        return tuple(names_by_id[tool_id] for tool_id in normalized)

    async def require_runtime_names(self, runtime_names: list[str]) -> tuple[str, ...]:
        normalized = list(dict.fromkeys(runtime_names))
        records = await self.repo.list_by_runtime_names(normalized)
        records_by_name = {record.runtime_name: record for record in records}
        registry_names = build_tool_registry().names
        # 目录中不存在、已停用、或代码注册表中没有实现，任一情况都视为不可用。
        unavailable: list[str] = []
        for name in normalized:
            record = records_by_name.get(name)
            if record is None or not record.is_enabled or name not in registry_names:
                unavailable.append(name)
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

    async def _open_call_record(
        self,
        record: ToolCallRecord | None,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        message_id: int | None,
        agent_id: int | None,
    ) -> ToolCallRecord:
        """新建 running 调用记录；已有记录（如审批后恢复）则回到 running 并覆盖入参。"""
        if record is None:
            return await self.start_call(
                session_id,
                tool_name,
                input_args,
                message_id=message_id,
                agent_id=agent_id,
            )
        record.input_args = input_args
        record.status = "running"
        await self.db.flush()
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
        profile: ToolProfile | None = None,
        principal_name: str | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> str:
        """执行工具并记录调用状态。

        这里是三类 actor(orchestrator / subagent / teammate)工具执行的唯一交汇点,
        因此把 PreToolUse / PostToolUse 两个 hook 挂在此处,一处覆盖全部产出者。

        record 非空时复用既有记录(如审批通过后复用之前的 awaiting 记录),
        回到 running 并以最新 input_args 覆盖(可能被审批时的 updated_input 修改过);
        否则新建一条 running 记录。
        """
        actor = actor or ORCHESTRATOR_ACTOR
        if execution_context is not None:
            # 网关创建时冻结的上下文优先级高于兼容参数，避免调用方借助一次
            # ``run`` 临时替换 profile、主体或资源白名单。
            resolved_profile = execution_context.profile
            resolved_principal = execution_context.principal_name
            agent_id = execution_context.agent_id
            allowed_skill_names = execution_context.allowed_skill_names
            shell_access = execution_context.shell_access
            shell_tmp_root = execution_context.shell_tmp_root
        else:
            resolved_profile = profile or _profile_from_actor(actor)
            resolved_principal = principal_name or _principal_from_actor(actor)
        hooks = get_hook_registry()

        # 注册表裁剪只影响模型可见 schema，不能作为最终安全边界。任何旧
        # 配置、直接调用或恢复路径都必须在网关再次经过能力策略。
        policy = get_tool_policy(tool_name)
        # 未声明策略的自定义工具保留旧的直接 ToolService 兼容路径；一旦
        # 运行时显式携带 profile，则未知工具也必须先登记策略才能调用。
        capability_check_required = profile is not None or policy.category != "unknown"
        denied: str | None = None
        if capability_check_required and not can_call_tool(tool_name, resolved_profile):
            denied = f"工具能力策略拒绝执行: tool={tool_name}, profile={resolved_profile}"
        elif policy.requires_bound_skill and requires_skill_allowlist(resolved_profile):
            # Skill 的参数名是 ``name``，SkillResource / SkillRun 使用
            # ``skill_name``。统一在执行网关做资产边界校验，不能只依赖
            # 工具自身的错误文本或模型可见 schema。lead 使用工作区级目录，
            # 其他运行身份必须由父运行时显式下发白名单。
            skill_arg = "name" if tool_name == "Skill" else "skill_name"
            requested_skill = input_args.get(skill_arg)
            if (
                allowed_skill_names is None
                or not isinstance(requested_skill, str)
                or requested_skill not in allowed_skill_names
            ):
                denied = (
                    "工具能力策略拒绝执行: 当前运行身份只能访问已绑定的 skill"
                    f" (skill={requested_skill!r})"
                )
        # 被策略拒绝的调用同样落一条失败记录并触发 PostToolUse，便于审计；
        # 拒绝理由作为工具结果回给模型，不抛异常。
        if denied is not None:
            record = await self._open_call_record(
                record,
                session_id,
                tool_name,
                input_args,
                message_id,
                agent_id,
            )
            await self.tool_repo.fail(record, denied)
            await self.db.flush()
            await hooks.trigger(
                HookContext(
                    event=HookEvent.POST_TOOL_USE,
                    session_id=session_id,
                    actor=actor,
                    turn_id=turn_id,
                    tool_name=tool_name,
                    tool_input=input_args,
                    tool_output=denied,
                    is_error=True,
                )
            )
            return denied

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
        # 多个 hook 的结果按注册顺序合并：updated_input 依次覆盖，block 取第一个。
        blocked: str | None = None
        for outcome in pre_outcomes:
            if outcome.updated_input is not None:
                input_args = outcome.updated_input
            if blocked is None and outcome.block is not None:
                blocked = outcome.block

        # 先落库再判断拦截，被拦截的调用也留有记录。
        record = await self._open_call_record(
            record,
            session_id,
            tool_name,
            input_args,
            message_id,
            agent_id,
        )

        if blocked is not None:
            # 被 PreToolUse hook 拦截:不执行工具,把拦截理由作为结果回给模型。
            await self.tool_repo.fail(record, blocked)
            await self.db.flush()
            return blocked

        # 三态日志只记工具名与结果,不记入参:入参可能含敏感路径、命令或密钥。
        logger.info(
            "工具开始执行",
            extra={
                "session_id": session_id,
                "turn_id": str(turn_id),
                "tool_name": tool_name,
            },
        )
        started_at = time.perf_counter()
        try:
            tool = self.registry.get(tool_name)
            ctx = ToolContext(
                session_id=session_id,
                db=self.db,
                bus=bus,
                turn_id=turn_id,
                user_id=user_id,
                workspace_id=workspace_id,
                principal_name=resolved_principal,
                profile=resolved_profile,
                allowed_tool_names=self.registry.names,
                allowed_skill_names=allowed_skill_names,
                shell_access=shell_access,
                shell_tmp_root=shell_tmp_root,
            )
            output = await tool.run_with_dict(input_args, ctx)
        except Exception as exc:
            # 失败先记日志再落库,避免后续落库或 hook 抛错吞掉现场;不改变原有 raise 时序。
            logger.exception(
                "工具执行失败",
                extra={
                    "session_id": session_id,
                    "turn_id": str(turn_id),
                    "tool_name": tool_name,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                },
            )
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
        logger.info(
            "工具执行完成",
            extra={
                "session_id": session_id,
                "turn_id": str(turn_id),
                "tool_name": tool_name,
                "status": "success",
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        )

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
