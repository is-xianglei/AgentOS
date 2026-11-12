"""主 Agent 的回合循环：组装本回合的角色与工具，按 LeadLoopPolicy 跑共用的 ReAct 骨架。

循环要用的协作者（回合配置、人工交互流程、会话、压缩、记忆、Stop hook）经 LeadLoopDeps 显式传入，
策略不再回调运行时的私有方法。
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import ORCHESTRATOR_ACTOR, Actor, RuntimeEvent, StreamEvent
from harness.app.compaction import ContextCompaction
from harness.app.loop import (
    LoopExit,
    LoopPolicy,
    LoopRound,
    ModelRequest,
    StepResult,
    run_react_loop,
)
from harness.app.memory_port import MemoryProvider
from harness.app.ports import ModelSettings
from harness.app.session_port import StoredMessage, StoredSession, StoredTurn, TurnContextStore
from harness.app.tool_set import ToolSet
from harness.domain.budget import ToolCallBudget
from harness.domain.context import messages_from_rows
from harness.domain.memory import TurnRef
from harness.domain.roles.assistant import Assistant
from model_service.policy import TURBO_MAX_ITERATIONS
from plan.service import PlanService
from runtime.interaction_flow import InteractionFlow, SuspendedInteraction
from runtime.step import AgentStep
from runtime.stream_assembler import TurnEventAssembler
from runtime.tool_execution import default_tool_budget
from runtime.turn_config import TurnConfiguration
from skill.service import SkillService

logger = logging.getLogger(__name__)

# Stop hook：(session_id, turn_id, actor, 本回合上下文) -> 要求续跑时注入的 user 消息，没有则 None。
type StopHooks = Callable[
    [int, UUID, Actor, list[dict[str, Any]] | None],
    Awaitable[str | None],
]


@dataclass(frozen=True)
class TurnLoopResult:
    """一次 Turn 主循环的持久化结果。"""

    suspended: bool
    completed_message_id: int | None = None
    interaction: SuspendedInteraction | None = None


@dataclass(frozen=True)
class LeadLoopDeps:
    """主 Agent 回合循环的协作者，一次运行内不变。"""

    db: AsyncSession
    bus: StreamBus
    configuration: TurnConfiguration
    interactions: InteractionFlow
    memory: MemoryProvider
    plan_service: PlanService
    sessions: TurnContextStore
    compaction: ContextCompaction
    stop_hooks: StopHooks
    workspace_id: int | None


class LeadLoopPolicy(LoopPolicy[TurnLoopResult]):
    """主 Agent 的循环策略：消息每轮从会话加载并按需压缩，assistant 消息落库到 session_messages。

    没有工具调用时先跑 Stop hook（可要求续跑一轮）；工具阶段经人工交互流程执行，可能挂起等待
    人工交互；用完轮次后不带工具再调一次收尾，极速模式改为抛中文异常。回合级的工具预算、角色与
    「LLM调用开始/完成」「回合结束」观测日志都在这里。
    """

    def __init__(
        self,
        deps: LeadLoopDeps,
        *,
        session_id: int,
        session: StoredSession,
        turn: StoredTurn,
        additional_context: str | None,
        rendered_memories: str | None,
        role: Assistant,
        tools: ToolSet,
        budget: ToolCallBudget,
        assembler: TurnEventAssembler,
        step: AgentStep,
    ) -> None:
        self._deps = deps
        self._configuration = deps.configuration
        self._session_id = session_id
        self._session = session
        self._turn = turn
        self._additional_context = additional_context
        self._rendered_memories = rendered_memories
        self._role = role
        self._tools = tools
        self._budget = budget
        self._assembler = assembler
        self._step = step
        self._turbo = deps.configuration.turbo
        # 观测用计时与轮次计数，仅供日志使用，不参与业务控制流。
        self._started = time.perf_counter()
        self._rounds = 0
        self._round_started = 0.0
        self._round_settings: ModelSettings | None = None
        self._context: list[dict[str, Any]] = []
        self._assistant_message: StoredMessage | None = None
        self._stop_reason: str | None = None

    async def load_context(self, loop_round: LoopRound) -> ModelRequest:
        # 极速模式不压缩；只压缩当前 Turn 之前的历史，原始请求和本轮工具轨迹始终完整保留。
        self._context = await self._load_messages(compact=not self._turbo)
        self._round_settings = self._configuration.round_settings(self._session)
        self._rounds += 1
        round_model = self._configuration.llm_attribution(self._round_settings)[1]
        self._round_started = time.perf_counter()
        logger.info(
            "LLM调用开始",
            extra={
                "session_id": self._session_id,
                "turn_id": str(self._turn.id),
                "round": self._rounds,
                "model": round_model,
                "msg_count": len(self._context),
            },
        )
        # 极速模式最后一轮不给工具，迫使模型直接作答。
        if self._turbo and loop_round.is_last:
            round_tools = self._tools.only()
        else:
            round_tools = self._tools
        context = self._role.context(messages_from_rows(self._context), round_tools.to_specs())
        return ModelRequest(context=context, settings=self._round_settings)

    async def record_assistant(self, loop_round: LoopRound, reply: StepResult) -> None:
        logger.info(
            "LLM调用完成",
            extra={
                "session_id": self._session_id,
                "turn_id": str(self._turn.id),
                "round": self._rounds,
                "duration_ms": round((time.perf_counter() - self._round_started) * 1000, 2),
            },
        )
        self._stop_reason = reply.llm_result.stop_reason
        # 落库中立块:库中不存任何 provider wire 形状,出站翻译由各 Client 现编。
        assert self._round_settings is not None
        self._assistant_message = await self._add_assistant(reply, self._round_settings)

    async def on_no_tool_calls(
        self,
        loop_round: LoopRound,
        reply: StepResult,
    ) -> LoopExit[TurnLoopResult] | None:
        # Stop:回合即将结束时触发。hook 可返回 continuation 强制续跑
        # (作为一条 user 消息注入),否则正常结束本回合。极速模式不触发。
        continuation = None
        if not self._turbo:
            continuation = await self._deps.stop_hooks(
                self._session_id,
                self._turn.id,
                ORCHESTRATOR_ACTOR,
                self._context,
            )
        if continuation is None:
            return LoopExit(await self._finish())
        await self._deps.sessions.add_message(
            self._session_id,
            "user",
            continuation,
            turn_id=self._turn.id,
        )
        await self._deps.db.flush()
        return None

    async def execute_tools(
        self,
        loop_round: LoopRound,
        reply: StepResult,
    ) -> LoopExit[TurnLoopResult] | None:
        assert self._assistant_message is not None
        interaction = await self._deps.interactions.run_tool_calls(
            self._session_id,
            self._session,
            self._turn,
            ORCHESTRATOR_ACTOR,
            self._assistant_message.id,
            reply.tool_calls,
            done_ids=[],
            additional_context=self._additional_context,
            budget=self._budget,
        )
        if interaction is None:
            return None
        return LoopExit(TurnLoopResult(suspended=True, interaction=interaction))

    async def flush(self) -> None:
        await self._deps.db.flush()

    async def on_exhausted(self) -> TurnLoopResult:
        # 达到轮数上限:禁用工具最后调一次 LLM,产出最终答复(优雅降级)。
        if self._turbo:
            raise AgentException.message("极速模式已达到模型调用上限，请切换标准模式继续")
        self._assistant_message = await self._finalize_without_tools()
        self._stop_reason = self._assembler.last_result.stop_reason or "max_iterations"
        return await self._finish()

    async def _load_messages(self, *, compact: bool) -> list[dict[str, Any]]:
        """加载本轮上下文：可压缩的历史在前，本轮消息在后；compact 为 True 时先压缩历史。"""
        deps = self._deps
        history, current, through_message_id = await deps.sessions.load_context_for_turn(
            self._turn,
            self._additional_context,
            rendered_memories=self._rendered_memories,
        )
        if compact:
            history = await deps.compaction.maybe_compact(
                self._session_id,
                history,
                through_message_id=through_message_id,
            )
        return history + current

    async def _add_assistant(
        self,
        reply: StepResult,
        round_settings: ModelSettings,
    ) -> StoredMessage:
        provider, effective_model = self._configuration.llm_attribution(round_settings)
        return await self._deps.sessions.add_message(
            self._session_id,
            "assistant",
            reply.assistant_blocks,
            turn_id=self._turn.id,
            provider=provider,
            model=effective_model,
        )

    async def _finalize_without_tools(self) -> StoredMessage:
        """禁用工具再调一次 LLM,产出最终答复并落库。

        沿用同一装配器,使收尾这次调用的块 index 接在前面的块之后,不从 0 重来。
        """
        round_settings = self._configuration.round_settings(self._session)
        context = await self._load_messages(compact=True)
        reply = await self._step.run(
            self._role.final_context(messages_from_rows(context)),
            round_settings,
        )
        return await self._add_assistant(reply, round_settings)

    async def _finish(self) -> TurnLoopResult:
        """回合正常结束：记「回合结束」日志并发出 turn_end。"""
        assert self._assistant_message is not None
        usage = self._assembler.last_result.usage
        logger.info(
            "回合结束",
            extra={
                "session_id": self._session_id,
                "turn_id": str(self._turn.id),
                "stop_reason": self._stop_reason,
                "rounds": self._rounds,
                "in_tokens": usage.input_tokens if usage else None,
                "out_tokens": usage.output_tokens if usage else None,
                "duration_ms": round((time.perf_counter() - self._started) * 1000, 2),
            },
        )
        await self._deps.bus.emit(
            StreamEvent.turn_end(
                ORCHESTRATOR_ACTOR,
                self._session_id,
                self._stop_reason,
                self._assembler.last_result.usage,
            )
        )
        return TurnLoopResult(
            suspended=False,
            completed_message_id=self._assistant_message.id,
        )


async def run_lead_turn(
    deps: LeadLoopDeps,
    session_id: int,
    session: StoredSession,
    turn: StoredTurn,
    additional_context: str | None,
    budget: ToolCallBudget | None = None,
) -> TurnLoopResult:
    """运行主 Agent 一个回合的 ReAct 循环,事件以协议形式 emit 到 bus。

    返回结果包含是否挂起；成功结束时还包含最终助手消息 ID。
    """
    actor = ORCHESTRATOR_ACTOR
    configuration = deps.configuration
    # 装配器跨本回合多次 LLM 调用共用:块 index 需在 turn 内全局递增连号,
    # 这是它唯一的跨调用状态(详见装配器 docstring)。
    assembler = TurnEventAssembler(
        actor,
        session_id,
        turn_id=turn.id,
        source_of=configuration.policies.source_of,
    )

    # 回合开始(开场事件,带完整 actor)。
    await deps.bus.emit(StreamEvent.turn_start(actor, session_id))
    # 本回合有作用域工具来源不可用(如 MCP server 连不上)时紧随其后通知前端;没有故障不发,
    # 现有事件序列不变。故障文本可直接展示,不带地址与凭证,授权地址由前端凭 server_id 自己请求。
    faults = configuration.scoped.faults
    if faults:
        await deps.bus.emit(
            RuntimeEvent.tool_sources(session_id, [fault.to_dict() for fault in faults])
        )

    if deps.workspace_id is None:
        raise AgentException.message("缺少可信工作区上下文")
    # 极速模式跳过 skill、记忆、Plan Mode、上下文压缩与 Stop hook，只用固定提示词。
    turbo = configuration.turbo

    # skill catalog 每次 run 取一次缓存复用(仅主代理注入,§15.2):进 for 循环前取一次,
    # 循环内各轮共用,避免每轮 loop 都打 DB。
    if turbo:
        skills_catalog = ""
    else:
        if configuration.skill_ids is not None:
            skill_ids = list(configuration.skill_ids)
        else:
            skill_ids = None
        skills_catalog = await SkillService(deps.db).get_catalog(deps.workspace_id, skill_ids)
    # 可委派 Agent 目录已在回合配置的 lead 分支加载;极速模式没有 Agent 工具,不注入。
    if turbo:
        delegate_catalog = None
    else:
        delegate_catalog = configuration.delegate_catalog

    if turbo:
        memory_recall = None
    else:
        memory_recall = await deps.memory.recall(
            TurnRef(
                workspace_id=turn.workspace_id,
                user_id=turn.user_id,
                session_id=turn.session_id,
                turn_id=turn.id,
            )
        )
    if memory_recall is not None:
        # 子代理使用独立数据库会话，冻结结果必须先提交才能稳定继承。
        await deps.db.commit()
        # 正文由会话加载拼到本轮起始的用户消息前面，目录由角色放进系统提示末段。
        rendered_memories = memory_recall.message_prefix
    else:
        rendered_memories = None

    # 会话绑定了 Agent 时用 Agent 的提示词，否则用会话自带的系统提示。
    if configuration.agent_id is not None:
        session_prompt = configuration.agent_prompt
    else:
        session_prompt = session.system_prompt
    if turbo:
        active_plan = None
    else:
        active_plan = await deps.plan_service.get_active(session_id)
    active_tools = configuration.tools_for_mode(active_plan is not None)
    # 系统指令按回合冻结：块形式携带缓存断点，tools + 稳定段跨回合复用，
    # 完整指令供本回合内多轮工具迭代复用。工作目录与日期在这里读取，角色不读进程状态。
    role = Assistant(
        cwd=str(Path.cwd()),
        today=date.today(),
        skills_catalog=skills_catalog,
        delegate_catalog=delegate_catalog,
        session_prompt=session_prompt,
        memory=memory_recall,
        plan_content=active_plan.content if active_plan is not None else None,
        turbo=turbo,
    )
    step = AgentStep(configuration.require_model().client, assembler, deps.bus.emit)
    # 一次 Turn 内跨所有 LLM 回合共享工具预算；恢复人工交互时由调用方传入
    # continuation 中持久化的同一实例，避免重建运行时后额度被重置。
    if budget is None:
        budget = default_tool_budget()

    policy = LeadLoopPolicy(
        deps,
        session_id=session_id,
        session=session,
        turn=turn,
        additional_context=additional_context,
        rendered_memories=rendered_memories,
        role=role,
        tools=active_tools,
        budget=budget,
        assembler=assembler,
        step=step,
    )
    if turbo:
        iterations = TURBO_MAX_ITERATIONS
    else:
        iterations = settings.max_tool_iterations
    return await run_react_loop(policy, step, max_rounds=iterations)
