import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import ORCHESTRATOR_ACTOR, Actor
from harness.app.ports import SubAgentLauncher
from harness.app.tool_set import ToolSet, platform_tools
from harness.domain.review import ToolPolicyLookup, check_capability
from harness.domain.tool import ScopedGrant, ShellAccess, ToolContext, ToolOutput, ToolProfile
from hooks import HookContext, HookEvent, get_hook_registry
from tools.context import ToolExecutionContext
from tools.models import ToolCallRecord, ToolRecord
from tools.repository import ToolCatalogRepository, ToolRepository

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


# 按 tool_key 解析时工具不可用的原因：not_found 目录里没有或代码里已没有实现，
# disabled 已停用，not_bindable 当前档位不允许绑定。
ToolUnavailableReason = Literal["not_found", "disabled", "not_bindable"]


@dataclass(frozen=True)
class ToolKeyResolution:
    """tool_key 的解析结果，两个字典的键都是 tool_key。"""

    # 可以绑定的工具：tool_key -> 工具 ID
    tool_ids: dict[str, int]
    # 不可用的工具：tool_key -> 原因
    unavailable: dict[str, ToolUnavailableReason]


class ToolCatalogService:
    """提供内置工具目录同步、查询与绑定校验。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ToolCatalogRepository(db)

    async def sync_builtins(self, tool_set: ToolSet | None = None) -> None:
        active_tool_set = tool_set or platform_tools()
        for tool in active_tool_set.tools:
            await self.repo.sync_builtin(
                tool_key=tool.name,
                name=tool.name,
                description=tool.description,
                runtime_name=tool.name,
                input_schema=tool.parameters,
            )
        await self.repo.mark_missing_deleted(set(active_tool_set.names))
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
        tool_set = platform_tools()
        missing_runtime = [
            record.id for record in records if record.runtime_name not in tool_set.names
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
            record
            for record in records
            if not tool_set.policy_for(record.runtime_name).can_bind(profile)
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

    async def get_enabled_runtime_names_by_ids(self, tool_ids: list[int]) -> dict[int, str]:
        """按工具 ID 取运行时名称,只含仍启用的工具;不做绑定校验也不抛错,仅供只读展示。"""
        records = await self.repo.list_by_ids(list(dict.fromkeys(tool_ids)))
        names_by_id: dict[int, str] = {}
        for record in records:
            if record.is_enabled:
                names_by_id[record.id] = record.runtime_name
        return names_by_id

    async def resolve_tool_keys(
        self,
        tool_keys: list[str],
        *,
        profile: ToolProfile,
    ) -> ToolKeyResolution:
        """把 tool_key 解析为可绑定的工具 ID；不可用的不抛错，而是连同原因一起返回。

        判定口径与 require_bindable_tool_ids 一致，并按同样的先后顺序检查：
        目录里没有（含已软删除）、已停用、代码注册表里没有实现、档位不允许绑定。
        “没有实现”对使用方来说和“目录里没有”一样，都归为 not_found。
        """
        normalized = list(dict.fromkeys(tool_keys))
        records = await self.repo.list_by_tool_keys(normalized)
        records_by_key = {record.tool_key: record for record in records}
        tool_set = platform_tools()

        tool_ids: dict[str, int] = {}
        unavailable: dict[str, ToolUnavailableReason] = {}
        for tool_key in normalized:
            record = records_by_key.get(tool_key)
            if record is None:
                unavailable[tool_key] = "not_found"
            elif not record.is_enabled:
                unavailable[tool_key] = "disabled"
            elif record.runtime_name not in tool_set.names:
                unavailable[tool_key] = "not_found"
            elif not tool_set.policy_for(record.runtime_name).can_bind(profile):
                unavailable[tool_key] = "not_bindable"
            else:
                tool_ids[tool_key] = record.id
        return ToolKeyResolution(tool_ids=tool_ids, unavailable=unavailable)

    async def get_tool_keys_by_ids(self, tool_ids: list[int]) -> dict[int, str]:
        """按工具 ID 取 tool_key，已停用的也返回；目录里已删除的工具不出现在结果中。"""
        records = await self.repo.list_by_ids(list(dict.fromkeys(tool_ids)))
        tool_keys: dict[int, str] = {}
        for record in records:
            tool_keys[record.id] = record.tool_key
        return tool_keys

    async def require_runtime_names(self, runtime_names: list[str]) -> tuple[str, ...]:
        normalized = list(dict.fromkeys(runtime_names))
        records = await self.repo.list_by_runtime_names(normalized)
        records_by_name = {record.runtime_name: record for record in records}
        registry_names = platform_tools().names
        # 目录中不存在、已停用、或代码里没有实现，任一情况都视为不可用。
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
    def __init__(self, db: AsyncSession, registry: ToolSet, policy_source: ToolPolicyLookup):
        """初始化工具执行器依赖；policy_source 是本回合的策略全集，由网关或运行时传入。"""
        self.db = db
        self.registry = registry
        # 能力策略按工具名查策略全集，而不是 registry：registry 是裁剪后的
        # 可见集合，被裁掉的工具若按它查会变成 unknown，从而跳过下面的能力检查。
        self.policy_source = policy_source
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

    # ---- 调用记录状态流转：只转发到仓储，flush 不提交，随调用方事务生效 ----

    async def get_call(self, tool_call_id: int) -> ToolCallRecord | None:
        """按 ID 读取工具调用记录，不存在时返回 None，不校验会话归属。"""
        return await self.tool_repo.get(tool_call_id)

    async def complete_call(self, record: ToolCallRecord, output: Any) -> ToolCallRecord:
        """把调用记录标记为成功并写入输出，用于不经真实执行的合成结果。"""
        return await self.tool_repo.succeed(record, output)

    async def mark_call_awaiting(self, record: ToolCallRecord) -> ToolCallRecord:
        """把调用记录置为等待人工交互。"""
        return await self.tool_repo.mark_awaiting_interaction(record)

    async def reject_awaiting_call(self, record: ToolCallRecord, message: str) -> ToolCallRecord:
        """把等待人工交互的调用记录标记为拒绝并写入原因。"""
        return await self.tool_repo.reject_awaiting(record, message)

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
        round_tool_names: frozenset[str] | None = None,
        subagents: SubAgentLauncher | None = None,
        scoped: ScopedGrant | None = None,
    ) -> ToolOutput:
        """执行工具并记录调用状态。

        这里是三类 actor(orchestrator / subagent / teammate)工具执行的唯一交汇点,
        因此把 PreToolUse / PostToolUse 两个 hook 挂在此处,一处覆盖全部产出者。

        record 非空时复用既有记录(如审批通过后复用之前的 awaiting 记录),
        回到 running 并以最新 input_args 覆盖(可能被审批时的 updated_input 修改过);
        否则新建一条 running 记录。subagents 是运行时注入的子代理启动器,scoped 是本执行的作用域工具,
        两者原样放进工具上下文。
        能力拒绝与 Hook 拦截的文本以 ToolOutput.ok 返回,与原先回给模型的结果一致。
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
        policy = self.policy_source.policy_for(tool_name)
        # 未声明策略的自定义工具保留旧的直接 ToolService 兼容路径；一旦
        # 运行时显式携带 profile，则未知工具也必须先登记策略才能调用。
        capability_check_required = profile is not None or policy.category != "unknown"
        denied: str | None = None
        if capability_check_required:
            # 与审核规则共用同一份能力检查（档位 + Skill 白名单），这里是执行层的纵深防御，
            # 不能只依赖工具自身的错误文本或模型可见 schema。跳过时策略必为未知工具策略，
            # 它不要求绑定 skill，所以只跳过档位检查不会漏掉 Skill 白名单检查。
            denied = check_capability(
                tool_name,
                policy,
                resolved_profile,
                input_args,
                allowed_skill_names,
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
            return ToolOutput.ok(denied)

        # PreToolUse:可拦截(block)或改写入参(updated_input)。permission 已在
        # 更高层(orchestrator _execute_tool_calls)裁决过,此处 hook 只能更严,不能提权。
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
            return ToolOutput.ok(blocked)

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
                round_tool_names=round_tool_names,
                allowed_skill_names=allowed_skill_names,
                shell_access=shell_access,
                shell_tmp_root=shell_tmp_root,
                subagents=subagents,
                scoped=scoped,
            )
            output = await tool.execute(input_args, ctx)
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
        await self.tool_repo.succeed(record, output.text)
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
                tool_output=output.text,
            )
        )
        return output
