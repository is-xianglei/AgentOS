"""主 Agent 运行时：请求阶段准入、后台执行体与回合终态。

回合配置（模型、工具边界、Agent 绑定、委派目录）见 turn_config，工具执行见 tool_execution，
人工交互的挂起、恢复与取消见 interaction_flow，回合循环见 lead_loop；运行时按组合调用它们。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException, LeaseLostError
from core.event_bus import NullStreamBus, RunStreamBus, StreamBus
from core.events import (
    ORCHESTRATOR_ACTOR,
    Actor,
    ErrorInfo,
    RuntimeEvent,
    StreamEvent,
    teammate_actor,
)
from database.engine import AsyncSessionLocal
from harness.adapters.memory.builtin_provider import BuiltinMemoryProvider
from harness.adapters.models.factory import create_model_client
from harness.adapters.reviewers.policy_reviewer import PolicyReviewer
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.app.agent_service import ExecutionHandle
from harness.app.compaction import ContextCompaction
from harness.app.memory_port import MemoryProvider
from harness.app.ports import (
    ModelClient,
    ModelConnectionError,
    ModelStatusError,
    ResolvedModel,
    ToolCallReviewer,
)
from harness.app.subagent_catalog import get_subagent_spec
from harness.domain.budget import ToolCallBudget
from harness.domain.memory import MemoryWriteTicket, TurnRef
from harness.domain.roles.subagent import AgentType
from hooks import HookContext, HookEvent, get_hook_registry
from interaction.models import RuntimeSuspensionRecord
from interaction.service import InteractionService
from permission.service import PermissionService
from plan.service import PlanService
from runtime.execution import launch_turn_execution, lease_seconds, new_lease_owner
from runtime.interaction_flow import (
    RESUME_CANCEL_QUESTION,
    RESUME_REPLAY_FINISHED,
    RESUME_REPLAY_PENDING,
    InteractionFlow,
    SuspendedInteraction,
)
from runtime.lead_loop import LeadLoopDeps, TurnLoopResult, run_lead_turn
from runtime.subagent import SubAgentRunner
from runtime.tool_execution import LeadToolExecutor
from runtime.turn_config import TurnConfiguration, configure_turn
from session.models import SessionRecord, SessionTurnRecord
from session.service import SessionService
from task.service import TaskService
from team.service import TeamService
from tools.service import ToolService

logger = logging.getLogger(__name__)


def _turn_failure_info(exc: Exception) -> ErrorInfo:
    """主循环失败时推给客户端的错误信息。

    只有 AgentException 的消息是面向用户的业务文案;其余异常的 str() 可能含连接串、
    上游原始响应等内部细节,一律换成固定文案,详情只进服务端日志。
    """
    if isinstance(exc, AgentException):
        return ErrorInfo(code="TURN_FAILED", message=exc.message, fatal=True)
    transient = isinstance(exc, ModelConnectionError) or (
        isinstance(exc, ModelStatusError) and (exc.status_code == 429 or exc.status_code >= 500)
    )
    if transient:
        return ErrorInfo(
            code="TURN_FAILED",
            message="模型服务暂时不可用，请稍后重试",
            retriable=True,
            fatal=True,
        )
    return ErrorInfo(code="TURN_FAILED", message="本轮处理失败，请稍后重试", fatal=True)


class AgentRuntime:
    """主 Agent 运行时：准入、后台执行体与回合终态，按组合调用回合配置、工具执行与人工交互流程。"""

    def __init__(
        self,
        db: AsyncSession,
        user_id: int | None = None,
        workspace_id: int | None = None,
        llm: ModelClient | None = None,
        reviewer: ToolCallReviewer | None = None,
        memory: MemoryProvider | None = None,
    ):
        """初始化 Agent 主运行时依赖。

        llm 是注入的模型客户端，注入后不再按会话解析模型，后台重建运行时时随之传递；
        reviewer 未注入时使用主 Agent 的规则链审核员；memory 未注入时使用绑定本会话的内置记忆。
        """
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.llm = llm
        self.session_service = SessionService(db)
        # 压缩快照经会话域 Service 写入；未解析出压缩模型时按 .env 的 compact 场景现建客户端。
        self.compact_service = ContextCompaction(
            self.session_service,
            default_model=lambda: ResolvedModel.from_client(
                create_model_client(scenario="compact")
            ),
        )
        self.permission_service = PermissionService(db)
        if reviewer is None:
            reviewer = PolicyReviewer.for_main_agent(self.permission_service)
        self.reviewer = reviewer
        self.interaction_service = InteractionService(db)
        self.plan_service = PlanService(db)
        # 只做调用记录的状态流转，与工具边界无关；工具执行经回合配置里的网关。
        self.tool_service = ToolService(db, builtin_tool_set())
        # 组装根（阶段 7）之前在这里回落内置实现；注入的 Provider 随后台执行一并传递。
        self._memory_injected = memory is not None
        if memory is None:
            memory = BuiltinMemoryProvider(db)
        self.memory = memory
        # 按会话配置之前的回合配置：默认 Agent、标准模式；注入客户端时模型取客户端自带的默认参数。
        model = None
        if llm is not None:
            model = ResolvedModel.from_client(llm)
        self.configuration = TurnConfiguration.for_lead(db, model=model)
        # 执行之外(准入、交互取消)没有订阅方,事件直接丢弃;执行内由 bind_execution 替换。
        self.bus: StreamBus = NullStreamBus()
        # 当前执行的租约持有者;提交终态前据此隔离已被取消或回收的执行。
        self.lease_owner: str | None = None

    def bind_execution(self, bus: RunStreamBus, lease_owner: str) -> None:
        """把运行时绑定到一次后台执行的事件流与租约。"""
        self.bus = bus
        self.lease_owner = lease_owner

    def _injected_memory(self) -> MemoryProvider | None:
        """返回构造时注入的记忆 Provider，供后台执行重建运行时时沿用。

        未注入时返回 None：内置实现绑定请求作用域的数据库会话，由后台运行时按自己的会话重建。
        getattr 兜底：测试用 __new__ 构造的实例没有 _memory_injected 属性。
        """
        if getattr(self, "_memory_injected", False):
            return self.memory
        return None

    async def _configure_agent(self, session: SessionRecord) -> None:
        """按会话绑定刷新回合配置；解析出压缩模型与阈值时同步给上下文压缩。"""
        configuration = await configure_turn(
            self.db,
            session,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            injected=self.llm,
        )
        if configuration.compact_model is not None:
            self.compact_service.model = configuration.compact_model
        if configuration.compact_threshold is not None:
            self.compact_service.threshold = configuration.compact_threshold
        self.configuration = configuration

    def _interactions(self) -> InteractionFlow:
        """按当前回合配置、事件总线与各域 Service 组装工具执行者与人工交互流程。"""
        executor = LeadToolExecutor(
            configuration=self.configuration,
            reviewer=self.reviewer,
            plan_service=self.plan_service,
            session_service=self.session_service,
            tool_service=self.tool_service,
            bus=self.bus,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
        )
        return InteractionFlow(
            db=self.db,
            configuration=self.configuration,
            executor=executor,
            session_service=self.session_service,
            interaction_service=self.interaction_service,
            plan_service=self.plan_service,
            permission_service=self.permission_service,
            tool_service=self.tool_service,
            bus=self.bus,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
        )

    async def start(
        self,
        session_id: int | None,
        user_content: str,
        *,
        agent_id: int | None = None,
        model_options: dict[str, Any] | None = None,
    ) -> ExecutionHandle:
        """请求阶段准入:锁会话 → 校验可执行 → 按需建会话 → 写用户消息 → 建 Turn → 认领租约。

        准入失败直接以 HTTP 错误返回;成功后在推流前提交,再启动后台执行。
        """
        if self.user_id is None or self.workspace_id is None:
            raise AgentException.message("缺少可信用户或工作区上下文")
        # 没有模型选项时不传该参数，沿用会话当前设置。
        prepare_kwargs: dict[str, Any] = {}
        if model_options:
            prepare_kwargs["model_options"] = model_options
        session = await self.session_service.prepare_for_message(
            session_id,
            user_content,
            self.user_id,
            self.workspace_id,
            agent_id,
            **prepare_kwargs,
        )
        # 准入前解析实际模型，缺配置通过普通 JSON 错误返回；注入客户端时不解析。
        if self.llm is None:
            await self._configure_agent(session)
        turn, _ = await self.session_service.start_turn(
            session,
            self.user_id,
            self.workspace_id,
            user_content,
        )
        target_session_id, turn_id = session.id, turn.id
        return await self._launch(
            target_session_id,
            turn_id,
            lambda runtime: runtime._produce(target_session_id, turn_id, user_content),
        )

    async def _launch(
        self,
        session_id: int,
        turn_id: UUID,
        run: Callable[[AgentRuntime], Awaitable[None]],
    ) -> ExecutionHandle:
        """对轮次认领租约并提交，再在独立数据库会话里重建运行时、启动后台执行。"""
        owner = new_lease_owner()
        start_offset = await self.session_service.claim_turn_lease(
            turn_id, owner, lease_seconds()
        )
        # 显式提交:请求作用域的 db 只用于准入;轮次与租约须在后台任务读取前可见,
        # 且 get_db 要等整个 SSE 发完才收尾,不能让连接在推流期间一直挂着事务。
        await self.db.commit()
        # 只把构造时注入的客户端与记忆 Provider 传给后台任务；按会话解析的由后台运行时重新解析。
        user_id, workspace_id, llm = self.user_id, self.workspace_id, self.llm
        memory = self._injected_memory()

        async def body(bus: RunStreamBus) -> None:
            # 后台任务用独立 AsyncSessionLocal,生命周期与任务一致。
            async with AsyncSessionLocal() as db:
                runtime = AgentRuntime(
                    db, user_id=user_id, workspace_id=workspace_id, llm=llm, memory=memory
                )
                runtime.bind_execution(bus, owner)
                await run(runtime)

        return await launch_turn_execution(
            turn_id=turn_id,
            session_id=session_id,
            owner=owner,
            start_offset=start_offset,
            body=body,
        )

    async def _fence_lease(self, session_id: int, turn_id: UUID) -> None:
        """提交终态前按「会话 → 轮次」加锁并确认租约仍属于本次执行。

        已被用户取消或被回收时抛 LeaseLostError,避免覆盖清除方写下的终态。
        lease_owner 为空表示运行时未绑定执行(单元测试直接驱动),不做隔离。
        """
        if self.lease_owner is not None:
            await self.session_service.lock_owned_turn(session_id, turn_id, self.lease_owner)

    async def _produce(
        self,
        session_id: int,
        turn_id: UUID,
        user_content: str,
    ) -> None:
        """后台执行体:准入已提交,跑完整流程并把事件 emit 到 bus;收尾由执行包装层负责。"""
        turn: SessionTurnRecord | None = None
        suspension_committed = False
        try:
            session = await self.session_service.get_required(session_id)
            turn = await self.session_service.get_turn_required(turn_id)
            await self._configure_agent(session)

            # Hook 只生成本次模型请求的临时上下文，绝不改写已保存的原始用户消息。
            # 极速模式不触发 Hook。
            if self.configuration.turbo:
                additional_context = None
            else:
                additional_context = await self._apply_user_prompt_hooks(
                    session_id,
                    turn.id,
                    user_content,
                )

            # 会话级控制事件:前端据此拿 session 元信息建立 UI。
            await self.bus.emit(
                RuntimeEvent.session_ready(session.id, session.title, session.status)
            )

            # 首帧任务基线:多轮会话中上一轮建的任务此刻整体推给前端,建立看板基线;
            # 断线重连后前端也据此重建,无需额外轮询 GET /tasks。
            await TaskService(self.db, bus=self.bus).emit_snapshot(session.id, "baseline")

            loop_result = await self._run_llm_loop(
                session_id,
                session,
                turn,
                additional_context,
            )
            if loop_result.suspended:
                # 暂停点、请求、Session/Turn 状态先提交，再向客户端发布交互事件。
                await self._fence_lease(session_id, turn.id)
                await self.db.commit()
                suspension_committed = True
                if loop_result.interaction is not None:
                    await self._emit_interaction(loop_result.interaction)
                return
            memory_ticket = await self._complete_turn(
                session,
                turn,
                loop_result.completed_message_id,
            )
            if memory_ticket is not None:
                await self.memory.after_commit(memory_ticket)
            # 完成状态已提交，独立数据库会话中的队友才能读取到本轮最终回复。
            await self._wake_team_members(session_id, turn)
        except LeaseLostError:
            # 已被用户取消或被回收:终态由清除租约的一方写入,这里只放弃未提交的修改。
            await self.db.rollback()
            logger.info(
                "执行租约已失效，放弃本次执行的未提交修改",
                extra={"session_id": session_id, "turn_id": str(turn_id)},
            )
        except asyncio.CancelledError:
            logger.warning(
                "会话处理被取消",
                extra={"session_id": session_id, "turn_id": str(turn_id)},
            )
            if suspension_committed or self.lease_owner is not None:
                # 绑定执行时只回滚:执行包装层的收尾在同一事务里补写半截回答并写终态,
                # 被隔离时则什么都不写;这里先写 failed 会抢在半截回答之前提交。
                await self.db.rollback()
            else:
                await self._mark_abnormal_end(session_id, turn_id, "interrupted")
            raise
        except Exception as exc:
            logger.exception(
                "处理会话消息失败",
                extra={"session_id": session_id, "turn_id": str(turn_id)},
            )
            if suspension_committed:
                await self.db.rollback()
            elif await self._mark_abnormal_end(session_id, turn_id, "failed"):
                # 补发 error:否则 SSE 里只有 run_end(failed),用户看不到失败原因。
                # 先写终态再 emit,与恢复路径一致;此时仍在执行体内、总线未关闭,
                # run_end 到收尾时才分配 offset,必然排在其后。emit 内部已吞掉写入失败。
                await self.bus.emit(
                    StreamEvent.error_event(
                        ORCHESTRATOR_ACTOR,
                        session_id,
                        _turn_failure_info(exc),
                    )
                )

    async def start_resume(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
    ) -> ExecutionHandle:
        """保存人工响应、对原轮次认领租约,再启动后台恢复执行。

        响应先单独提交:即使随后认领冲突或 SSE 未建立,相同响应仍可重放恢复。
        """
        turn_id = await self.prepare_interaction_response(
            session_id,
            request_id,
            kind,
            response_payload,
        )
        return await self._launch(
            session_id,
            turn_id,
            lambda runtime: runtime._produce_resume(session_id, request_id),
        )

    async def prepare_interaction_response(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
    ) -> UUID:
        """校验并提交用户响应，使格式错误和冲突能在 SSE 建立前返回 HTTP 错误。

        返回暂停点所属轮次 ID,恢复执行沿用同一个轮次。
        """
        if self.user_id is None or self.workspace_id is None:
            raise AgentException.message("缺少可信用户或工作区上下文")
        session = await self.session_service.lock_required(session_id)
        await self._configure_agent(session)
        return await self._interactions().save_response(
            session_id,
            session,
            request_id,
            kind,
            response_payload,
        )

    async def cancel_interaction(
        self,
        session_id: int,
        suspension_id: UUID,
    ) -> RuntimeSuspensionRecord:
        """取消当前暂停点，原子收尾关联的 Turn 与工具调用，提交后补写 run_end。"""
        if self.user_id is None or self.workspace_id is None:
            raise AgentException.message("缺少可信用户或工作区上下文")
        return await self._interactions().cancel(session_id, suspension_id)

    async def _produce_resume(self, session_id: int, request_id: UUID) -> None:
        """后台恢复执行体:响应已由请求阶段提交,应用 continuation 并续跑主循环。"""
        turn: SessionTurnRecord | None = None
        continuation_applied = False
        idempotent_replay = False
        terminal_committed = False
        try:
            # 所有恢复者都按 Session -> Suspension 的顺序重新加锁。竞争者会在首个
            # 恢复事务提交后读取最新状态，不会重复应用工具或 Plan Mode 副作用。
            session = await self.session_service.lock_required(session_id)
            await self._configure_agent(session)
            interactions = self._interactions()
            point = await interactions.load_resume_point(session_id, request_id)
            turn, suspension = point.turn, point.suspension
            step = interactions.resume_step(session_id, session, point)

            if step == RESUME_REPLAY_PENDING:
                idempotent_replay = True
                await self.db.commit()
                interaction = await interactions.pending_interaction(session_id)
                await self._emit_interaction(interaction)
                return
            if step == RESUME_CANCEL_QUESTION:
                await interactions.cancel_question(session_id, session, point)
                await self._fence_lease(session_id, turn.id)
                await self.db.commit()
                continuation_applied = True
                terminal_committed = True
                await self._emit_resume_end(session, session_id, "user_cancelled")
                return
            if step == RESUME_REPLAY_FINISHED:
                # 暂停点已终结：相同响应只重放结束事件。
                idempotent_replay = True
                await self.db.commit()
                await self._emit_resume_end(
                    session,
                    session_id,
                    f"interaction_{suspension.status}",
                )
                return

            logger.info(
                "人工交互已恢复执行",
                extra={
                    "session_id": session_id,
                    "turn_id": str(turn.id),
                    "kind": point.request.kind,
                    "suspension_id": str(suspension.id),
                },
            )
            pending_budget = InteractionFlow.restored_budget(suspension)
            interaction = await interactions.apply_and_continue(
                session_id,
                session,
                turn,
                point.request,
                suspension,
                budget=pending_budget,
            )
            if interaction is not None:
                await self._fence_lease(session_id, turn.id)
                await self.db.commit()
                await self._emit_interaction(interaction)
                return

            await interactions.mark_resumed(session, turn, suspension)
            await self._fence_lease(session_id, turn.id)
            await self.db.commit()
            continuation_applied = True
            await self.bus.emit(
                RuntimeEvent.session_ready(session.id, session.title, session.status)
            )

            loop_result = await self._run_llm_loop(
                session_id,
                session,
                turn,
                suspension.continuation_payload.get("additional_context"),
                budget=pending_budget,
            )
            if loop_result.suspended:
                await self._fence_lease(session_id, turn.id)
                await self.db.commit()
                if loop_result.interaction is not None:
                    await self._emit_interaction(loop_result.interaction)
                return
            memory_ticket = await self._complete_turn(
                session,
                turn,
                loop_result.completed_message_id,
            )
            if memory_ticket is not None:
                await self.memory.after_commit(memory_ticket)
            await self._wake_team_members(session_id, turn)
        except LeaseLostError:
            await self.db.rollback()
            logger.info(
                "执行租约已失效，放弃本次恢复执行的未提交修改",
                extra={"session_id": session_id, "turn_id": str(turn.id if turn else "")},
            )
        except asyncio.CancelledError:
            if terminal_committed or not continuation_applied or self.lease_owner is not None:
                # 绑定执行的中断终态与半截回答由执行包装层收尾,原因同 _produce。
                await self.db.rollback()
            else:
                await self._mark_abnormal_end(
                    session_id,
                    turn.id if turn else None,
                    "interrupted",
                )
            raise
        except Exception:
            logger.exception(
                "恢复人工交互轮次失败",
                extra={"session_id": session_id, "turn_id": str(turn.id if turn else "")},
            )
            if terminal_committed or idempotent_replay or not continuation_applied:
                # 保持 session=awaiting_interaction / suspension=resuming，允许相同响应重放。
                await self.db.rollback()
                await self.bus.emit(
                    StreamEvent.error_event(
                        ORCHESTRATOR_ACTOR,
                        session_id,
                        ErrorInfo(
                            code="INTERACTION_RESUME_FAILED",
                            message="人工交互响应已保存，但恢复执行失败，可重试该响应。",
                            retriable=True,
                            fatal=True,
                        ),
                    )
                )
            else:
                await self._mark_abnormal_end(session_id, turn.id if turn else None, "failed")

    async def _emit_resume_end(
        self,
        session: SessionRecord,
        session_id: int,
        stop_reason: str,
    ) -> None:
        """恢复执行不再续跑时补发会话快照与 turn_end，前端据此离开等待交互状态。"""
        await self.bus.emit(RuntimeEvent.session_ready(session.id, session.title, session.status))
        await self.bus.emit(StreamEvent.turn_end(ORCHESTRATOR_ACTOR, session_id, stop_reason, None))

    async def _emit_interaction(self, interaction: SuspendedInteraction) -> None:
        """挂起事务提交之后发布交互请求事件。"""
        await self._interactions().emit(interaction)

    async def _run_llm_loop(
        self,
        session_id: int,
        session: SessionRecord,
        turn: SessionTurnRecord,
        additional_context: str | None,
        budget: ToolCallBudget | None = None,
    ) -> TurnLoopResult:
        """按当前回合配置组装循环协作者，运行一个回合的 ReAct 循环。"""
        deps = LeadLoopDeps(
            db=self.db,
            bus=self.bus,
            configuration=self.configuration,
            interactions=self._interactions(),
            memory=self.memory,
            plan_service=self.plan_service,
            session_service=self.session_service,
            compaction=self.compact_service,
            stop_hooks=self._apply_stop_hooks,
            workspace_id=self.workspace_id,
        )
        return await run_lead_turn(deps, session_id, session, turn, additional_context, budget)

    async def _complete_turn(
        self,
        session: SessionRecord,
        turn: SessionTurnRecord,
        completed_message_id: int | None,
    ) -> MemoryWriteTicket | None:
        """原子完成 Turn、恢复 Session，并在同一事务里登记回合的记忆写入。

        返回记忆写入回执，调用方在本方法提交之后交给 memory.after_commit。
        """
        # 先按「会话 → 轮次」加锁隔离,与取消接口的加锁顺序一致。
        await self._fence_lease(session.id, turn.id)
        await self.session_service.mark_turn_status(
            turn,
            "completed",
            completed_message_id=completed_message_id,
        )
        await self.session_service.set_status(session, "idle")
        memory_ticket: MemoryWriteTicket | None = None
        # 极速模式不写入记忆；写入失败由 Provider 自行隔离，不影响下面提交的回合终态。
        if not self.configuration.turbo:
            memory_ticket = await self.memory.on_turn_completed(
                TurnRef(
                    workspace_id=turn.workspace_id,
                    user_id=turn.user_id,
                    session_id=session.id,
                    turn_id=turn.id,
                )
            )
        # 流式运行需要在生产者生命周期内明确提交最终状态，不能依赖请求收尾。
        await self.db.commit()
        logger.debug(
            "回合完成状态已提交",
            extra={"session_id": str(session.id), "turn_id": str(turn.id)},
        )
        return memory_ticket

    async def _mark_abnormal_end(
        self,
        session_id: int | None,
        turn_id: UUID | None,
        turn_status: str,
    ) -> bool:
        """回滚当前失败事务，再尽力持久化 Turn 和 Session 的异常终态。

        绑定执行时先确认租约仍属于本次执行:用户取消已写下 interrupted+idle,
        回收已写下终态,都不能被这里覆盖成 failed。
        返回终态是否归本次执行:租约已失效、轮次缺失或已完成时为 False,
        调用方据此不再报告失败;写库本身失败时轮次仍是失败的,返回 True。
        """
        await self.db.rollback()
        if session_id is None or turn_id is None:
            return False
        try:
            if self.lease_owner is not None:
                session, turn = await self.session_service.lock_owned_turn(
                    session_id, turn_id, self.lease_owner
                )
            else:
                session = await self.session_service.get(session_id)
                turn = await self.session_service.get_turn(turn_id)
            if session is None or turn is None or turn.status == "completed":
                await self.db.rollback()
                return False
            await self.session_service.mark_turn_status(turn, turn_status)
            await self.session_service.set_status(session, "failed")
            await self.db.commit()
        except LeaseLostError:
            await self.db.rollback()
            logger.info(
                "执行租约已失效，异常终态由清除租约的一方负责",
                extra={"session_id": session_id, "turn_id": str(turn_id)},
            )
            return False
        except Exception:
            await self.db.rollback()
            logger.exception(
                "持久化交互轮次异常终态失败",
                extra={"session_id": session_id, "turn_id": str(turn_id)},
            )
        return True

    async def _apply_user_prompt_hooks(
        self,
        session_id: int,
        turn_id: UUID,
        user_content: str,
    ) -> str | None:
        """触发 UserPromptSubmit Hook，仅返回待注入请求副本的临时上下文。"""
        outcomes = await get_hook_registry().trigger(
            HookContext(
                event=HookEvent.USER_PROMPT_SUBMIT,
                session_id=session_id,
                actor=ORCHESTRATOR_ACTOR,
                turn_id=turn_id,
                user_content=user_content,
            )
        )
        extras = [o.additional_context for o in outcomes if o.additional_context]
        if extras:
            return "\n\n".join(extras)
        return None

    async def _apply_stop_hooks(
        self,
        session_id: int,
        turn_id: UUID,
        actor: Actor,
        messages: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """触发 Stop hook,返回首个非空 continuation(强制续跑),无则 None。

        messages 为本回合送进 LLM 的完整上下文:Stop hook 需要据此判断这一轮
        实际做了什么(如改了哪些文件、是否已走过验收),不能只凭会话 ID 猜。
        """
        outcomes = await get_hook_registry().trigger(
            HookContext(
                event=HookEvent.STOP,
                session_id=session_id,
                actor=actor,
                turn_id=turn_id,
                messages=messages,
            )
        )
        for outcome in outcomes:
            if outcome.continuation:
                return outcome.continuation
        return None

    async def _wake_team_members(
        self,
        session_id: int,
        turn: SessionTurnRecord,
    ) -> None:
        """请求内单步唤醒队友。

        lead 主循环结束后,把本会话所有 status=='working' 的队友各唤醒一次,
        每人调一次 run_teammate 推进一步(单轮,不循环连锁唤醒;后台自动唤醒尚未实现)。
        每个队友用独立 AsyncSessionLocal,共享 self.bus 让增量流式到前端 SSE。
        任一队友异常用 try/except 包住,emit error 事件后继续,不中断主流程。
        """
        if self.configuration.turbo:
            return
        members = await TeamService(self.db).list_members(session_id)
        working = [m for m in members if m.status == "working"]
        for member in working:
            try:
                spec = get_subagent_spec(AgentType(member.agent_type or "general-purpose"))
                # 独立 session 避免与主会话共用连接;共享 bus 让队友增量冒泡到 SSE。
                async with AsyncSessionLocal() as sub_db:
                    await SubAgentRunner(sub_db, bus=self.bus).run_teammate(
                        session_id,
                        member.name,
                        spec,
                        parent_turn_id=turn.id,
                        user_id=turn.user_id,
                        workspace_id=turn.workspace_id,
                        parent_allowed_tool_names=self.configuration.tools.names,
                        allowed_skill_names=self.configuration.allowed_skill_names,
                    )
            except Exception as exc:
                # 单个队友失败隔离:发 error 事件后继续唤醒其余成员。
                # 客户端断连时 SSE error 事件无人接收，服务端必须留下失败痕迹。
                logger.exception(
                    "唤醒队友失败，已隔离该队友并继续主流程",
                    extra={
                        "session_id": session_id,
                        "turn_id": str(turn.id),
                        "member_name": member.name,
                    },
                )
                await self.bus.emit(
                    StreamEvent.error_event(
                        teammate_actor(member.name, run_id=f"wake-{member.id}"),
                        session_id,
                        ErrorInfo(
                            code="TEAMMATE_WAKE_FAILED",
                            message=str(exc),
                            retriable=True,
                            fatal=False,
                        ),
                    )
                )
