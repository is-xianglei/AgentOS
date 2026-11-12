"""子代理运行器：一次性子代理(Agent 工具)与 teammate 单次推进的 ReAct 循环。"""

import asyncio
import json
import logging
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import (
    Actor,
    ErrorInfo,
    StreamEvent,
    TeamRef,
    ToolInfo,
    subagent_actor,
    teammate_actor,
)
from harness.app.loop import (
    LoopExit,
    LoopPolicy,
    LoopRound,
    ModelRequest,
    StepResult,
    run_react_loop,
)
from harness.app.memory_port import MemoryProvider
from harness.app.ports import (
    Effort,
    ModelClient,
    ModelResolver,
    ModelSettings,
    ResolvedModel,
    ToolCallReviewer,
)
from harness.app.session_port import SessionOptionsReader
from harness.app.tool_set import ToolSet
from harness.domain.budget import ToolCallBudget
from harness.domain.context import messages_from_rows
from harness.domain.memory import TurnRef
from harness.domain.messages import AssistantMessage, ToolCall, ToolResult
from harness.domain.review import SOURCE_BUDGET, ReviewRequest
from harness.domain.roles.subagent import SubAgent, SubAgentSpec
from harness.domain.roles.teammate import Teammate, is_identity_block
from harness.domain.tool import ToolProfile
from runtime.step import AgentStep
from runtime.stream_assembler import TurnEventAssembler
from team.isolation import (
    new_team_task_instance_id,
    reset_team_task_instance_id,
    set_team_task_instance_id,
)
from team.service import TeamService
from tools.gateway import ToolGateway

# 兼容旧测试/扩展点(测试会替换本模块的 ToolService)；实际执行统一经 ToolGateway。
from tools.service import ToolService  # noqa: F401

logger = logging.getLogger(__name__)

# teammate ReAct 轮次上限。比一次性子代理默认值略放宽,因为 teammate 还要
# 处理收件箱与认领任务;但仍设硬上限以防失控空转。
TEAMMATE_MAX_ROUNDS = 12

# 被拒摘要里每次调用入参预览的最大字符数:够默认 Agent 认出是哪一步,又不把大段写入内容塞回上下文。
DENIED_INPUT_PREVIEW_MAX_CHARS = 200

# 子代理并发闸门。进程级信号量,按配置的并发上限做背压:
# 超额的 Agent 调用在此排队而非直接失败,避免一次派发过多子代理打满上游速率限制。
_concurrency_gate: asyncio.Semaphore | None = None


def _get_concurrency_gate() -> asyncio.Semaphore:
    """惰性创建并发信号量(需在事件循环内构造,故不放模块加载期)。"""
    global _concurrency_gate
    if _concurrency_gate is None:
        _concurrency_gate = asyncio.Semaphore(max(1, settings.subagent_max_concurrency))
    return _concurrency_gate


# 打开一个独立数据库会话的工厂（如 async_sessionmaker），由组装根注入。
type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class SubAgentRunnerFactory(Protocol):
    """按数据库会话与事件总线构造子代理运行器，由组装根提供；运行时据此唤醒队友、运行 Workflow 成员。"""

    def __call__(self, db: AsyncSession, *, bus: StreamBus | None) -> SubAgentRunner: ...


@dataclass(frozen=True)
class DeniedToolCall:
    """被委派 Agent 一次被权限裁决拒绝的工具调用,用于在结果末尾生成被拒摘要。"""

    tool_name: str
    input_args: dict[str, Any]
    # 动态权限层的判定:ask(需审批,无审批通道而坍缩为拒绝)或 deny。
    behavior: str
    reason: str


def _render_denied_summary(denied_calls: list[DeniedToolCall]) -> str:
    """把实际拒绝记录渲染成追加在报告末尾的摘要,不依赖子模型自述。"""
    header = (
        "[运行时附注] 以下工具调用被拒绝,未执行(依据运行时的实际拒绝记录生成)。"
        "如确需这些操作,请在主会话中自行重新调用,按正常流程审批:"
    )
    lines = [header]
    for call in denied_calls:
        preview = json.dumps(call.input_args, ensure_ascii=False, default=str)
        if len(preview) > DENIED_INPUT_PREVIEW_MAX_CHARS:
            preview = preview[:DENIED_INPUT_PREVIEW_MAX_CHARS] + "…(已截断)"
        if call.behavior == "ask":
            reason = "需要用户审批,被委派的 Agent 没有审批通道"
        else:
            reason = call.reason
        lines.append(f"- {call.tool_name} {preview}:{reason}")
    return "\n".join(lines)


@dataclass(frozen=True)
class SubAgentLoopRun:
    """一次子代理或队友循环共用的运行参数：身份、工具、调用参数与工具预算。"""

    session_id: int
    actor: Actor
    spec: SubAgentSpec
    tool_registry: ToolSet
    tool_gateway: ToolGateway
    model_settings: ModelSettings
    budget: ToolCallBudget
    parent_turn_id: UUID | None
    user_id: int | None
    workspace_id: int | None
    allowed_skill_names: frozenset[str] | None


class _InMemoryLoopPolicy[R](LoopPolicy[R]):
    """子代理与队友共用的循环策略基类：消息只在内存列表里，工具结果追加回同一列表。

    记忆正文由角色拼进请求副本，messages 本身不改动（队友历史会原样落库）。
    """

    def __init__(
        self,
        runner: SubAgentRunner,
        run: SubAgentLoopRun,
        messages: list[dict[str, Any]],
        role: SubAgent | Teammate,
        *,
        denied_calls: list[DeniedToolCall] | None = None,
    ) -> None:
        self._runner = runner
        self._run = run
        self.messages = messages
        self._role = role
        self._denied_calls = denied_calls

    async def load_context(self, loop_round: LoopRound) -> ModelRequest:
        context = self._role.context(
            messages_from_rows(self.messages),
            self._run.tool_registry.to_specs(),
        )
        return ModelRequest(context=context, settings=self._run.model_settings)

    async def record_assistant(self, loop_round: LoopRound, reply: StepResult) -> None:
        # 中立块均为 JSON 原生结构,可直接随 messages 落 team_members.history 并原样 round-trip。
        result = reply.llm_result
        self.messages.append(
            {
                "role": "assistant",
                "content": AssistantMessage.from_turn(
                    result.text, result.tool_calls, result.thinking
                ).to_content(),
            }
        )

    async def execute_tools(self, loop_round: LoopRound, reply: StepResult) -> LoopExit[R] | None:
        run = self._run
        await self._runner._execute_tool_calls(
            run.session_id,
            run.actor,
            reply.tool_calls,
            self.messages,
            run.tool_gateway,
            run.spec,
            run.parent_turn_id,
            run.user_id,
            run.workspace_id,
            allowed_skill_names=run.allowed_skill_names,
            budget=run.budget,
            denied_calls=self._denied_calls,
        )
        return None

    async def flush(self) -> None:
        await self._runner.db.flush()


class SubAgentLoopPolicy(_InMemoryLoopPolicy[tuple[str, int]]):
    """一次性子代理的循环策略：返回 (报告, 实际执行轮数)。

    轮次耗尽不抛异常：调用方（Agent 工具）需要拿到已完成的部分结论，返回带注意事项的截断报告。
    """

    def __init__(
        self,
        runner: SubAgentRunner,
        run: SubAgentLoopRun,
        messages: list[dict[str, Any]],
        role: SubAgent,
        *,
        denied_calls: list[DeniedToolCall] | None = None,
    ) -> None:
        super().__init__(runner, run, messages, role, denied_calls=denied_calls)
        # 最近一次非空文本，轮次耗尽时作为截断报告的正文。
        self._last_text = ""

    async def record_assistant(self, loop_round: LoopRound, reply: StepResult) -> None:
        await super().record_assistant(loop_round, reply)
        if reply.text:
            self._last_text = reply.text

    async def on_no_tool_calls(
        self,
        loop_round: LoopRound,
        reply: StepResult,
    ) -> LoopExit[tuple[str, int]] | None:
        return LoopExit((reply.text, loop_round.index + 1))

    async def on_exhausted(self) -> tuple[str, int]:
        # 抛异常会让整轮子代理白跑,截断报告 + 明确标注仍可供主代理判断与补做。
        spec = self._run.spec
        session_id = self._run.session_id
        logger.warning(
            "子代理轮次耗尽,返回截断报告,会话 ID=%s 类型=%s 上限=%s",
            session_id,
            spec.runtime_name,
            spec.max_rounds,
            extra={"session_id": session_id, "agent_type": spec.runtime_name},
        )
        notice = f"[注意] 子代理已达 {spec.max_rounds} 轮工具调用上限,以下为截断的中间结论:"
        rounds = max(1, spec.max_rounds)
        if self._last_text:
            return f"{notice}\n\n{self._last_text}", rounds
        return notice, rounds


class TeammateLoopPolicy(_InMemoryLoopPolicy[str]):
    """队友的循环策略：每轮开始先认领本会话任务，返回本次推进的报告。

    队友历史全落库，截断后可再次唤醒接着推进，所以轮次耗尽同样不抛异常。
    """

    async def before_round(self, loop_round: LoopRound) -> None:
        # 轮首尝试认领可处理任务(只按 session_id,不加实例隔离)。
        run = self._run
        await self._runner._try_claim_tasks(run.session_id, run.actor.name, self.messages)

    async def on_no_tool_calls(
        self,
        loop_round: LoopRound,
        reply: StepResult,
    ) -> LoopExit[str] | None:
        return LoopExit(reply.text)

    async def on_exhausted(self) -> str:
        session_id = self._run.session_id
        logger.warning(
            "teammate 轮次耗尽,本次推进结束,会话 ID=%s 成员=%s",
            session_id,
            self._run.actor.name,
            extra={"session_id": session_id},
        )
        return f"[注意] 已达 {TEAMMATE_MAX_ROUNDS} 轮工具调用上限,本次推进到此结束,可再次唤醒继续。"


class SubAgentRunner:
    """在父轮次内运行子代理；事件经父 bus 发出，actor 标识子代理自身。"""

    def __init__(
        self,
        db: AsyncSession,
        bus: StreamBus | None = None,
        llm: ModelClient | None = None,
        *,
        reviewer: ToolCallReviewer,
        memory: MemoryProvider,
        sessions: SessionOptionsReader,
        models: ModelResolver,
        tools: ToolSet,
        subagent_model: Callable[[SubAgentSpec], str | None],
    ):
        """初始化子代理运行器依赖，具体实现全部由组装根（harness/bootstrap.py）注入。

        llm 是注入的模型客户端，注入后不再按会话解析模型；sessions 只用来读取所属会话冻结的模型选项；
        tools 是完整工具集，既是裁剪的底集，也是网关与审核按名查策略的策略全集；
        subagent_model 按 spec 给出 .env 端点上要覆盖的模型名。
        """
        self.db = db
        self.bus = bus
        # 客户端持 subagent 默认档模型;Explore 等速度档由 subagent_model
        # 写进每次调用的 ModelSettings.model_id,见 _configure_model。
        self.llm = llm
        self._llm_injected = llm is not None
        # 子代理运行记录属 team 域，经其 Service 写入。
        self.team_service = TeamService(db)
        self.reviewer = reviewer
        self.memory = memory
        self.sessions = sessions
        self.models = models
        self.tools = tools
        # 按名查策略的策略全集：本阶段就是注入的完整工具集，裁剪只作用于可见集，不作用于它。
        self.policies = tools
        self._subagent_model = subagent_model

    def _resolve_tools(
        self,
        spec: SubAgentSpec,
        parent_allowed_tool_names: frozenset[str] | None,
        profile: ToolProfile = "subagent",
    ) -> ToolSet:
        """先继承父 Agent 工具边界，再叠加子代理自身规则。"""
        registry = self.tools
        if parent_allowed_tool_names is not None:
            registry = registry.only(*parent_allowed_tool_names)
        registry = spec.resolve_tools(registry)
        names = self.policies.callable_names(registry.names, profile)
        return registry.only(*names)

    async def _configure_model(
        self,
        session_id: int,
        spec: SubAgentSpec,
        workspace_id: int | None,
        user_id: int | None,
    ) -> ModelSettings:
        """按会话模型选项解析客户端，返回子代理每轮调用的参数。

        .env 端点上 spec 声明的模型名（固定模型或速度档）覆盖场景模型；
        数据库端点有自己的模型名，不再用 spec 的 .env 模型名覆盖。
        """
        if self._llm_injected:
            return self._override_model(ResolvedModel.from_client(self.llm).settings, spec)
        provider_id = None
        effort = None
        if workspace_id is not None:
            session = await self.sessions.runtime_model_options(session_id)
            if session.workspace_id != workspace_id or session.user_id != user_id:
                raise AgentException.message("子智能体会话归属无效")
            if getattr(session, "mode_slug", "standard") == "turbo":
                raise AgentException.message("极速模式不能启动子智能体")
            if (session.extra or {}).get("model_provider_override"):
                provider_id = session.model_provider_id
            effort = Effort.parse(getattr(session, "thinking_effort", Effort.FAST))
        resolved = await self.models.resolve(
            workspace_id,
            user_id,
            spec.model_scenario or "subagent",
            provider_id=provider_id,
            effort=effort,
        )
        self.llm = resolved.client
        if resolved.provider_record_id is not None:
            return resolved.settings
        return self._override_model(resolved.settings, spec)

    def _override_model(self, settings: ModelSettings, spec: SubAgentSpec) -> ModelSettings:
        """spec 声明了模型名（固定模型或速度档）时覆盖场景模型，否则沿用。"""
        model = self._subagent_model(spec)
        if model:
            return replace(settings, model_id=model)
        return settings

    async def run(
        self,
        session_id: int,
        prompt: str,
        spec: SubAgentSpec,
        *,
        parent_turn_id: UUID | None = None,
        user_id: int | None = None,
        workspace_id: int | None = None,
        parent_allowed_tool_names: frozenset[str] | None = None,
        allowed_skill_names: frozenset[str] | None = None,
    ) -> str:
        """无状态执行一次子代理:按 spec 配置跑 ReAct 并返回报告。

        不依赖团队成员或收件箱,prompt 由调用方(Agent 工具)直接给出。
        member_name 复用为 spec 的运行标识(内置即 agent_type 值),兼容运行记录表与前端事件契约。
        """
        member_name = spec.runtime_name
        started_at = time.perf_counter()
        logger.info(
            "子代理启动",
            extra={
                "session_id": session_id,
                "turn_id": str(parent_turn_id) if parent_turn_id else None,
                "agent_type": member_name,
            },
        )
        # 工具裁决档位:spec 显式声明的优先(workflow_node / delegate);带 agent_id 但未声明
        # 档位的旧调用按工作流节点处理,保持原行为;内置子代理用 subagent 档位。
        if spec.tool_profile is not None:
            profile: ToolProfile = spec.tool_profile
        elif spec.agent_id is not None:
            profile = "workflow_node"
        else:
            profile = "subagent"
        # 保持旧的二参数调用形态，便于上层替换/测试 _resolve_tools；数据库 Agent
        # 在默认子代理边界基础上再按自身档位做一次能力解析。
        tool_registry = self._resolve_tools(spec, parent_allowed_tool_names)
        if profile != "subagent" and hasattr(tool_registry, "names"):
            profile_names = self.policies.callable_names(
                tool_registry.names,
                profile,
            )
            tool_registry = tool_registry.only(*profile_names)
        # 被委派的自建 Agent 没有审批通道:被拒的工具调用由运行时记下,结束时汇总给默认 Agent,
        # 由它在主会话里按正常审批重做。其余档位不收集,输出保持原样。
        denied_calls: list[DeniedToolCall] | None = None
        if profile == "delegate":
            denied_calls = []
        # omit_inherited_memory 的 spec(如 Explore)不继承父 Turn 记忆:
        # 它只做代码检索,注入用户偏好等长期记忆既无用又占满上下文预算。
        if spec.omit_inherited_memory:
            rendered_memories = None
        else:
            rendered_memories = await self._load_frozen_memories(
                session_id,
                parent_turn_id,
                user_id,
                workspace_id,
            )
        role = SubAgent(spec=spec, memory_prefix=rendered_memories)

        # 为本次 run 设置独立隔离 id(供运行期内派生消息的隔离与调试归属)。
        token = set_team_task_instance_id(new_team_task_instance_id())
        try:
            run = await self.team_service.create_subagent_run(
                session_id, member_name, prompt, agent_id=spec.agent_id
            )
            # 一次性子代理的 actor:role=subagent,name=agent_type,唯一键 run_id,task=prompt;
            # workflow 成员额外带 agent_id,供前端把事件归到工作流图节点。
            actor = subagent_actor(
                member_name, run_id=f"run-{run.id}", task=prompt, agent_id=spec.agent_id
            )
            tool_gateway = ToolGateway.from_registry(
                self.db,
                tool_registry,
                profile,
                principal_name=actor.name,
                policies=self.policies,
                agent_id=spec.agent_id,
                allowed_skill_names=allowed_skill_names,
                shell_access=spec.shell_access,
                shell_tmp_root=spec.shell_tmp_root(Path(tempfile.gettempdir())),
            )
            assembler = TurnEventAssembler(actor, session_id, turn_id=parent_turn_id)
            await self._emit_event(StreamEvent.turn_start(actor, session_id))
            try:
                # 并发闸门只圈住 ReAct 循环本身(LLM + 工具),不含建记录与事件收尾,
                # 避免持锁做无谓的 DB 往返。
                async with _get_concurrency_gate():
                    report, rounds = await self._run_react_loop(
                        session_id,
                        actor,
                        assembler,
                        prompt,
                        role,
                        tool_registry,
                        tool_gateway,
                        spec,
                        parent_turn_id,
                        user_id,
                        workspace_id,
                        allowed_skill_names=allowed_skill_names,
                        denied_calls=denied_calls,
                    )
                if denied_calls:
                    report = f"{report}\n\n{_render_denied_summary(denied_calls)}"
                await self.team_service.succeed_subagent_run(run, report)
                await self.db.commit()
                result = assembler.last_result
                await self._emit_event(
                    StreamEvent.turn_end(actor, session_id, result.stop_reason, result.usage)
                )
                logger.info(
                    "子代理运行完成",
                    extra={
                        "session_id": session_id,
                        "turn_id": str(parent_turn_id) if parent_turn_id else None,
                        "agent_type": member_name,
                        "rounds": rounds,
                        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    },
                )
                return report
            except Exception as exc:
                logger.exception(
                    "子代理运行失败",
                    extra={
                        "session_id": session_id,
                        "turn_id": str(parent_turn_id) if parent_turn_id else None,
                        "agent_type": member_name,
                        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    },
                )
                await self.team_service.fail_subagent_run(run, str(exc))
                await self.db.commit()
                await self._emit_event(
                    StreamEvent.error_event(
                        actor,
                        session_id,
                        ErrorInfo(code="SUBAGENT_RUN_FAILED", message=str(exc), fatal=True),
                    )
                )
                raise
        finally:
            reset_team_task_instance_id(token)

    async def _load_frozen_memories(
        self,
        session_id: int,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
    ) -> str | None:
        """只读继承父 Turn 已冻结正文，缺失可信作用域时不注入。"""
        if parent_turn_id is None or user_id is None or workspace_id is None:
            return None
        recall = await self.memory.inherited(
            TurnRef(
                workspace_id=workspace_id,
                user_id=user_id,
                session_id=session_id,
                turn_id=parent_turn_id,
            )
        )
        if recall is None:
            return None
        # 子代理只继承正文，父 Turn 的记忆目录不进子代理的系统提示。
        return recall.message_prefix

    async def _emit_event(self, event: StreamEvent) -> None:
        """向事件总线发送协议事件(若总线存在)。"""
        if self.bus is None:
            return
        await self.bus.emit(event)

    def _model_step(self, assembler: TurnEventAssembler) -> AgentStep:
        """每轮调模型的单步执行器：中立事件经装配器补齐 actor/index 后发到父 bus。"""
        return AgentStep(self.llm, assembler, self._emit_event)

    async def _emit_tool_result(
        self,
        actor: Actor,
        session_id: int,
        tool_call: ToolCall,
        output: str,
        *,
        is_error: bool = False,
    ) -> None:
        """发协议 tool_result 事件(actor 为该产出者自身)。"""
        await self._emit_event(
            StreamEvent.tool_result(
                actor,
                session_id,
                ToolInfo(
                    id=tool_call.id, name=tool_call.tool_name, output=output, is_error=is_error
                ),
            )
        )

    async def _execute_tool_calls(
        self,
        session_id: int,
        actor: Actor,
        tool_calls: list[Any],
        messages: list[dict[str, Any]],
        tool_gateway: ToolGateway,
        spec: SubAgentSpec,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
        *,
        allowed_skill_names: frozenset[str] | None = None,
        budget: ToolCallBudget | None = None,
        denied_calls: list[DeniedToolCall] | None = None,
    ) -> None:
        """裁决并执行一轮 tool_use,把结果追加回 messages。

        子代理没有人工交互挂起通道(无法像主代理那样发布请求后等待用户回包),
        因此 ask 在此坍缩为拒绝,并把原因作为 tool_result 回给模型,让它自行改道;
        不能沿用"未裁决直接执行"的旧行为,否则子代理成为绕过审批的提权路径。
        denied_calls 非空时,每次被权限裁决拒绝的调用都会追加进去,供调用方生成被拒摘要。
        """
        agent_type = spec.runtime_name
        if budget is None:
            budget = ToolCallBudget(
                total_limit=max(1, spec.max_rounds * 4),
                per_tool_limit=max(1, spec.max_rounds * 2),
            )
        for tool_call in tool_calls:
            decision = await self.reviewer.review(
                tool_call,
                ReviewRequest(
                    session_id=session_id,
                    profile=tool_gateway.context.profile,
                    agent_type=agent_type,
                    budget=budget,
                    workspace_id=workspace_id,
                    allowed_skill_names=tool_gateway.context.allowed_skill_names,
                    # 与构造本次网关时传入的是同一份策略全集。
                    policies=self.policies,
                ),
            )
            # 审核员改写过入参时（默认规则链不改写），执行与落库都用审核后的入参。
            if decision.arguments is not tool_call.arguments:
                tool_call = replace(tool_call, arguments=decision.arguments)
            if decision.behavior == "allow":
                result = await tool_gateway.run(
                    session_id,
                    tool_call.tool_name,
                    tool_call.arguments,
                    actor=actor,
                    turn_id=parent_turn_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    allowed_skill_names=allowed_skill_names,
                )
                output = result.text
                is_error = result.is_error
            elif decision.source == SOURCE_BUDGET:
                # 预算耗尽不执行工具，原因作为错误结果回给模型；不算权限裁决，不记被拒摘要。
                output = decision.message
                is_error = True
            else:
                # 子代理没有审批通道，审核员已把挂起合并为拒绝。
                if decision.needs_approval:
                    behavior = "ask"
                else:
                    behavior = "deny"
                output = decision.message
                is_error = True
                logger.warning(
                    "子代理工具调用被权限策略拒绝,会话 ID=%s 类型=%s 工具=%s 判定=%s",
                    session_id,
                    agent_type,
                    tool_call.tool_name,
                    behavior,
                    extra={"session_id": session_id, "agent_type": agent_type},
                )
                if denied_calls is not None:
                    denied_calls.append(
                        DeniedToolCall(
                            tool_name=tool_call.tool_name,
                            input_args=tool_call.arguments,
                            behavior=behavior,
                            reason=decision.reason,
                        )
                    )
            await self._emit_tool_result(actor, session_id, tool_call, output, is_error=is_error)
            # 中立协议下工具结果是独立的 tool 角色消息,编成各家 wire(Anthropic 的
            # tool_result 块 / OpenAI 的 role="tool" 消息)由客户端出站翻译负责。
            messages.append(
                {
                    "role": "tool",
                    "content": ToolResult(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.tool_name,
                        arguments=tool_call.arguments,
                        content=output,
                        is_error=is_error,
                    ).to_content(),
                }
            )

    async def _run_react_loop(
        self,
        session_id: int,
        actor: Actor,
        assembler: TurnEventAssembler,
        prompt: str,
        role: SubAgent,
        tool_registry: ToolSet,
        tool_gateway: ToolGateway,
        spec: SubAgentSpec,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
        *,
        allowed_skill_names: frozenset[str] | None = None,
        denied_calls: list[DeniedToolCall] | None = None,
    ) -> tuple[str, int]:
        """执行子代理 ReAct 循环,返回报告与实际执行轮数。系统提示由角色组装,轮次上限取自 spec。

        denied_calls 原样透传给 _execute_tool_calls,用于收集被拒的工具调用。
        """
        model_settings = await self._configure_model(session_id, spec, workspace_id, user_id)
        run = SubAgentLoopRun(
            session_id=session_id,
            actor=actor,
            spec=spec,
            tool_registry=tool_registry,
            tool_gateway=tool_gateway,
            model_settings=model_settings,
            budget=ToolCallBudget(
                total_limit=max(1, spec.max_rounds * 4),
                per_tool_limit=max(1, spec.max_rounds * 2),
            ),
            parent_turn_id=parent_turn_id,
            user_id=user_id,
            workspace_id=workspace_id,
            allowed_skill_names=allowed_skill_names,
        )
        policy = SubAgentLoopPolicy(
            self,
            run,
            [{"role": "user", "content": prompt}],
            role,
            denied_calls=denied_calls,
        )
        step = self._model_step(assembler)
        return await run_react_loop(policy, step, max_rounds=max(1, spec.max_rounds))

    # ============ teammate 模式:无状态短运行,在请求内被唤醒 ============

    async def run_teammate(
        self,
        session_id: int,
        member_name: str,
        spec: SubAgentSpec,
        *,
        parent_turn_id: UUID | None = None,
        user_id: int | None = None,
        workspace_id: int | None = None,
        parent_allowed_tool_names: frozenset[str] | None = None,
        allowed_skill_names: frozenset[str] | None = None,
    ) -> str:
        """恢复并单次推进一个 teammate:读历史→注入身份→排空收件箱→认领→ReAct→写回。

        与 run() 的一次性子代理不同,teammate 状态全落库(member.history),
        本方法是一个「恢复 + 单次推进」运行单元:跑完即结束,不起常驻线程。
        任何实例都能据 DB 接着唤醒,多实例正确性由 DB 保证。
        """
        logger.info(
            "唤醒 teammate,开始单次推进",
            extra={
                "session_id": session_id,
                "turn_id": str(parent_turn_id) if parent_turn_id else None,
                "member_name": member_name,
            },
        )
        team_service = TeamService(self.db)
        member = await team_service.get_member(session_id, member_name)
        if member is None:
            raise AgentException.message("要唤醒的团队成员不存在")
        name = member.name
        role = member.role
        rendered_memories = await self._load_frozen_memories(
            session_id,
            parent_turn_id,
            user_id,
            workspace_id,
        )
        # teammate 人格头:在 spec.system_prompt 之上拼接 name/role/team 身份,不改 spec。
        teammate = Teammate(
            spec=spec,
            session_id=session_id,
            name=name,
            role=role,
            memory_prefix=rendered_memories,
        )
        # 先继承父 Agent 的硬白名单，再叠加 teammate 规格；通信工具同样不得越过父边界。
        tool_registry = self._resolve_tools(spec, parent_allowed_tool_names, "teammate")
        tool_gateway = ToolGateway.from_registry(
            self.db,
            tool_registry,
            "teammate",
            principal_name=name,
            policies=self.policies,
            agent_id=spec.agent_id,
            allowed_skill_names=allowed_skill_names,
            shell_access=spec.shell_access,
            shell_tmp_root=spec.shell_tmp_root(Path(tempfile.gettempdir())),
        )

        # 为本次推进设置独立隔离 id(收件箱消费按该 id 隔离;任务认领不隔离)。
        instance_id = new_team_task_instance_id()
        token = set_team_task_instance_id(instance_id)
        # teammate 的 actor:role=teammate,唯一键 name,run_id=本次推进隔离 id,
        # task=初始任务,team 携带 team_id(若已归属团队)。
        team_ref: TeamRef | None = None
        if member.team_id is not None:
            team_ref = TeamRef(id=member.team_id)
        actor = teammate_actor(name, run_id=instance_id, task=member.prompt, team=team_ref)
        assembler = TurnEventAssembler(actor, session_id, turn_id=parent_turn_id)
        try:
            # 1) 从 member.history 反序列化历史;为空则用初始 prompt 起一条 user 消息。
            messages = self._load_history(member)

            # 2) 身份重注入:确保首条是 identity,避免重复注入。
            self._ensure_identity(messages, teammate)

            # 3) 排空收件箱:每条未读消息作为 user 消息追加(read_inbox 内部已隔离)。
            inbox = await team_service.read_inbox(session_id, name)
            for msg in inbox:
                messages.append(
                    {
                        "role": "user",
                        "content": f"[消息来自 {msg.sender}] {msg.content}",
                    }
                )

            await self._emit_event(StreamEvent.turn_start(actor, session_id))
            # 4) ReAct 循环(轮内允许认领本会话 task)。
            report = await self._run_teammate_loop(
                session_id,
                actor,
                assembler,
                messages,
                teammate,
                tool_registry,
                tool_gateway,
                spec,
                parent_turn_id,
                user_id,
                workspace_id,
                allowed_skill_names=allowed_skill_names,
            )

            # 5) 写回历史 + 据剩余待办决定 idle / working。
            await team_service.save_member_history(
                session_id,
                name,
                json.dumps(messages, ensure_ascii=False),
            )
            await self._settle_member_status(session_id, name, member, team_service)
            await self.db.commit()
            result = assembler.last_result
            await self._emit_event(
                StreamEvent.turn_end(actor, session_id, result.stop_reason, result.usage)
            )
            return report
        except Exception as exc:
            # 失败先落日志再回滚:客户端断连时 SSE 事件送不出去,服务端日志是唯一现场。
            logger.exception(
                "teammate 推进失败",
                extra={
                    "session_id": session_id,
                    "turn_id": str(parent_turn_id) if parent_turn_id else None,
                    "member_name": member_name,
                },
            )
            await self.db.rollback()
            await self._emit_event(
                StreamEvent.error_event(
                    actor,
                    session_id,
                    ErrorInfo(code="TEAMMATE_RUN_FAILED", message=str(exc), fatal=True),
                )
            )
            raise
        finally:
            reset_team_task_instance_id(token)

    def _load_history(self, member) -> list[dict[str, Any]]:
        """从 member.history(JSON 字符串)反序列化历史 messages。

        history 为空时用 member.prompt 初始化首条 user 消息。历史里的每个元素都是
        中立消息(assistant 块由 to_dict() 产出,tool 结果由 to_content() 产出),
        均为 JSON 原生结构,可直接 round-trip 后继续发给任一 provider。
        """
        raw = (member.history or "").strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        prompt = member.prompt or ""
        return [{"role": "user", "content": prompt}]

    def _ensure_identity(self, messages: list[dict[str, Any]], teammate: Teammate) -> None:
        """在 messages 最前面注入 identity;若首条已是 identity 则跳过避免重复。"""
        first_text = None
        if messages and isinstance(messages[0], dict):
            first_text = messages[0].get("content")
        if is_identity_block(first_text):
            return
        messages.insert(0, {"role": "user", "content": teammate.identity_block()})

    async def _settle_member_status(
        self, session_id: int, name: str, member, team_service: TeamService
    ) -> None:
        """据剩余未读消息 / 可认领任务,把成员置 working 或 idle。"""
        pending_inbox = await team_service.peek_inbox(session_id, name)
        claimable = await team_service.list_claimable_tasks(session_id)
        if pending_inbox or claimable:
            status = "working"
        else:
            status = "idle"
        await team_service.update_member_runtime(session_id, name, status=status)

    async def _run_teammate_loop(
        self,
        session_id: int,
        actor: Actor,
        assembler: TurnEventAssembler,
        messages: list[dict[str, Any]],
        teammate: Teammate,
        tool_registry: ToolSet,
        tool_gateway: ToolGateway,
        spec: SubAgentSpec,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
        *,
        allowed_skill_names: frozenset[str] | None = None,
    ) -> str:
        """teammate 版 ReAct:每轮开始尝试认领本会话 task,再走模型+工具循环;messages 原地追加。"""
        model_settings = await self._configure_model(session_id, spec, workspace_id, user_id)
        run = SubAgentLoopRun(
            session_id=session_id,
            actor=actor,
            spec=spec,
            tool_registry=tool_registry,
            tool_gateway=tool_gateway,
            model_settings=model_settings,
            budget=ToolCallBudget(
                total_limit=max(1, TEAMMATE_MAX_ROUNDS * 4),
                per_tool_limit=max(1, TEAMMATE_MAX_ROUNDS * 2),
            ),
            parent_turn_id=parent_turn_id,
            user_id=user_id,
            workspace_id=workspace_id,
            allowed_skill_names=allowed_skill_names,
        )
        policy = TeammateLoopPolicy(self, run, messages, teammate)
        step = self._model_step(assembler)
        return await run_react_loop(policy, step, max_rounds=TEAMMATE_MAX_ROUNDS)

    async def _try_claim_tasks(
        self, session_id: int, name: str, messages: list[dict[str, Any]]
    ) -> None:
        """认领本会话可处理任务,认领成功的作为 user 消息注入 messages。

        传入自身 bus:认领会把任务推进到 in_progress,需同步刷新前端任务列表。
        """
        team_service = TeamService(self.db, bus=self.bus)
        claimable = await team_service.list_claimable_tasks(session_id)
        for task in claimable:
            ok, _reason = await team_service.claim_task(session_id, task.id, name)
            if ok:
                messages.append(
                    {
                        "role": "user",
                        "content": f"[认领任务 #{task.id}] {task.subject}\n{task.description or ''}",
                    }
                )
