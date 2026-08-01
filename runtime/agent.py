from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from anthropic.types import TextBlockParam, ToolParam
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import (
    ORCHESTRATOR_ACTOR,
    Actor,
    ErrorInfo,
    InteractionInfo,
    RuntimeEvent,
    StreamEvent,
    ToolInfo,
    teammate_actor,
)
from hooks import HookContext, HookEvent, get_hook_registry
from interaction.models import InteractionRequestRecord, RuntimeSuspensionRecord
from interaction.schemas import AskUserQuestionRequestPayload, InteractionRequestCreate
from interaction.service import InteractionService
from llm.client import LLMClient
from llm.types import (
    AnthropicStreamTranslator,
    ToolResultMessage,
    ToolUse,
    extract_tool_uses,
)
from memory.jobs.service import MemoryJobRunner, MemoryJobService
from memory.recall.service import MemoryRecallService
from permission.service import DANGEROUS_TOOLS, PermissionService
from plan.prompts import ENTER_PLAN_MODE_RESULT, render_plan_mode_instructions
from plan.service import PlanService
from prompt import PromptContext, compose_lead_blocks
from runtime.compact import CompactService
from session.models import SessionMessage, SessionRecord, SessionTurnRecord
from session.service import SessionService
from skill.service import SkillService
from task.service import TaskService
from tools.models import ToolCallRecord
from tools.registry import ToolRegistry, build_tool_registry
from tools.service import ToolService
from tools.subagents.definition import AgentType
from tools.subagents.registry import get_subagent_spec

logger = logging.getLogger(__name__)

INTERACTION_TOOL_KINDS = {
    "AskUserQuestion": "user_question",
    "EnterPlanMode": "enter_plan_mode",
    "ExitPlanMode": "exit_plan_mode",
}
PLAN_MODE_ALLOWED_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Agent",
        "Skill",
        "SkillResource",
        "AskUserQuestion",
        "WritePlan",
        "ExitPlanMode",
    }
)


@dataclass(frozen=True)
class TurnLoopResult:
    """一次 Turn 主循环的持久化结果。"""

    suspended: bool
    completed_message_id: int | None = None
    interaction: SuspendedInteraction | None = None


@dataclass(frozen=True)
class SuspendedInteraction:
    """提交挂起事务后需要发布给客户端的交互事件快照。"""

    session_id: int
    suspension_id: UUID
    request_id: UUID
    kind: str
    request_payload: dict[str, Any]
    schema_version: int


class AgentRuntime:
    def __init__(
        self, db: AsyncSession, user_id: int | None = None, workspace_id: int | None = None
    ):
        """初始化 Agent 主运行时依赖。"""
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.session_service = SessionService(db)
        self.compact_service = CompactService(self.session_service.repo)
        self.permission_service = PermissionService(db)
        self.interaction_service = InteractionService(db)
        self.plan_service = PlanService(db)
        self.memory_recall_service = MemoryRecallService(db)
        self.llm = LLMClient()
        self.tool_registry = build_tool_registry()
        self.tool_service = ToolService(db, self.tool_registry)
        self.bus = StreamBus()

    async def run(self, session_id: int | None, user_content: str) -> AsyncIterator[dict[str, Any]]:
        """执行一次会话消息处理并产出流式事件。

        采用生产者-消费者模式:后台任务跑 ReAct 主流程并把所有事件(含 lead 增量、
        工具事件、子代理增量)emit 到会话级事件总线;本协程从总线抽干并 yield 给 SSE。
        """
        producer = asyncio.create_task(self._produce(session_id, user_content))
        try:
            async for event in self.bus.stream():
                yield event
        finally:
            # 确保后台任务结束(正常 close 后 producer 已完成;异常/取消时显式收尾)。
            if not producer.done():
                producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass

    async def _produce(self, session_id: int | None, user_content: str) -> None:
        """后台生产者:跑完整流程,所有事件 emit 到 bus,最终 close。"""
        session: SessionRecord | None = None
        turn: SessionTurnRecord | None = None
        suspension_committed = False
        try:
            if self.user_id is None or self.workspace_id is None:
                raise AgentException.message("缺少可信用户或工作区上下文")
            session: SessionRecord = await self.session_service.prepare_for_message(
                session_id, user_content, self.user_id, self.workspace_id
            )
            session_id = session.id
            turn, _ = await self.session_service.start_turn(
                session,
                self.user_id,
                self.workspace_id,
                user_content,
            )
            # 流式执行跨越请求依赖的 yield 生命周期，原始输入与 Turn 必须先可靠提交。
            await self.db.commit()

            # Hook 只生成本次模型请求的临时上下文，绝不改写已保存的原始用户消息。
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

            # 开始 LLM Loop
            loop_result = await self._run_llm_loop(
                session_id,
                session,
                turn,
                additional_context,
            )
            if loop_result.suspended:
                # 暂停点、请求、Session/Turn 状态先提交，再向客户端发布交互事件。
                await self.db.commit()
                suspension_committed = True
                if loop_result.interaction is not None:
                    await self._emit_interaction(loop_result.interaction)
                return
            memory_job_id = await self._complete_turn(
                session,
                turn,
                loop_result.completed_message_id,
            )
            await self._run_inline_memory_job(memory_job_id)
            # 完成状态已提交，独立数据库会话中的队友才能读取到本轮最终回复。
            await self._wake_team_members(session_id, turn)
        except asyncio.CancelledError:
            if suspension_committed:
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
                "处理会话消息失败",
                extra={"session_id": session_id, "turn_id": str(turn.id if turn else "")},
            )
            if suspension_committed:
                await self.db.rollback()
            else:
                await self._mark_abnormal_end(session_id, turn.id if turn else None, "failed")
        finally:
            await self.bus.close()

    async def resume(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
        *,
        response_prepared: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """持久化人工响应并从通用暂停点恢复执行。"""
        producer = asyncio.create_task(
            self._produce_resume(
                session_id,
                request_id,
                kind,
                response_payload,
                response_prepared=response_prepared,
            )
        )
        try:
            async for event in self.bus.stream():
                yield event
        finally:
            if not producer.done():
                producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass

    async def prepare_interaction_response(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
    ) -> None:
        """校验并提交用户响应，使格式错误和冲突能在 SSE 建立前返回 HTTP 错误。"""
        if self.user_id is None or self.workspace_id is None:
            raise AgentException.message("缺少可信用户或工作区上下文")

        session = await self.session_service.lock_required(session_id)
        current = await self.interaction_service.get_request_required(
            request_id,
            session_id=session_id,
        )
        is_replay = current.status == "resolved"
        if not is_replay and session.status != "awaiting_interaction":
            raise AgentException.message("会话当前没有待处理的人工交互")

        normalized = await self._normalize_interaction_response(
            session_id,
            request_id,
            kind,
            response_payload,
        )
        request, suspension = await self.interaction_service.resolve_request(
            request_id,
            normalized,
            self.user_id,
            session_id=session_id,
            expected_kind=kind,
        )
        turn = await self.session_service.get_turn_required(suspension.turn_id)
        if turn.session_id != session_id:
            raise AgentException.message("人工交互对应的轮次不属于指定会话")
        if turn.user_id != self.user_id or turn.workspace_id != self.workspace_id:
            raise AgentException.message("无权限恢复该交互轮次", status_code=403)
        if not is_replay and turn.status != "awaiting_interaction":
            raise AgentException.message("人工交互对应的轮次不可恢复")
        if request.response_payload != normalized:
            raise AgentException.message("人工交互响应持久化结果不一致")

        # 流建立或客户端连接随后失败时，响应仍可从 resuming 暂停点重放。
        await self.db.commit()

    async def cancel_interaction(
        self,
        session_id: int,
        suspension_id: UUID,
    ) -> RuntimeSuspensionRecord:
        """取消当前暂停点，并原子收尾关联的 Turn 与工具调用。"""
        if self.user_id is None or self.workspace_id is None:
            raise AgentException.message("缺少可信用户或工作区上下文")

        session = await self.session_service.ensure_interaction_resumable(session_id)
        suspension = await self.interaction_service.get_suspension_required(
            suspension_id,
            session_id=session_id,
        )
        turn = await self.session_service.get_turn_required(suspension.turn_id)
        if turn.session_id != session_id:
            raise AgentException.message("运行暂停点与交互轮次不匹配")
        if turn.user_id != self.user_id or turn.workspace_id != self.workspace_id:
            raise AgentException.message("无权限取消该交互轮次", status_code=403)

        suspension, requests = await self.interaction_service.cancel_suspension(
            suspension_id,
            self.user_id,
            session_id=session_id,
        )
        await self._finish_cancelled_interaction(
            session,
            turn,
            requests,
            "用户取消了运行暂停点。",
        )
        return suspension

    async def _produce_resume(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
        *,
        response_prepared: bool = False,
    ) -> None:
        """后台生产者：先可靠保存响应，再应用 continuation 并续跑主循环。"""
        session: SessionRecord | None = None
        turn: SessionTurnRecord | None = None
        response_committed = response_prepared
        continuation_applied = False
        idempotent_replay = False
        terminal_committed = False
        try:
            if not response_prepared:
                await self.prepare_interaction_response(
                    session_id,
                    request_id,
                    kind,
                    response_payload,
                )
                response_committed = True

            # 所有恢复者都按 Session -> Suspension 的顺序重新加锁。竞争者会在首个
            # 恢复事务提交后读取最新状态，不会重复应用工具或 Plan Mode 副作用。
            session = await self.session_service.lock_required(session_id)
            request = await self.interaction_service.get_request_required(
                request_id,
                session_id=session_id,
            )
            suspension = await self.interaction_service.lock_suspension_required(
                request.suspension_id,
                session_id=session_id,
            )
            turn = await self.session_service.get_turn_required(suspension.turn_id)
            if request.suspension_id != suspension.id or turn.session_id != session_id:
                raise AgentException.message("人工交互与运行暂停点不匹配")
            if turn.user_id != self.user_id or turn.workspace_id != self.workspace_id:
                raise AgentException.message("无权限恢复该交互轮次", status_code=403)

            # 上一次恢复已经推进到同一 suspension 的下一项交互时，相同响应只需
            # 重放当前待处理请求，不能再次执行已经完成的工具副作用。
            if suspension.status == "pending":
                if session.status != "awaiting_interaction":
                    raise AgentException.message("人工交互暂停点与会话状态不一致")
                idempotent_replay = True
                await self.db.commit()
                interaction = await self._get_pending_interaction(session_id)
                await self._emit_interaction(interaction)
                return

            if suspension.status in {"resolved", "cancelled", "failed"}:
                idempotent_replay = True
                await self.db.commit()
                await self.bus.emit(
                    RuntimeEvent.session_ready(session.id, session.title, session.status)
                )
                await self.bus.emit(
                    StreamEvent.turn_end(
                        ORCHESTRATOR_ACTOR,
                        session_id,
                        f"interaction_{suspension.status}",
                        None,
                    )
                )
                return
            if suspension.status != "resuming":
                raise AgentException.message(
                    "运行暂停点当前不可恢复",
                    {"status": suspension.status},
                )
            if (
                session.status != "awaiting_interaction"
                or turn.status != "awaiting_interaction"
            ):
                raise AgentException.message("人工交互对应的会话或轮次不可恢复")

            persisted_response = request.response_payload
            if persisted_response is None:
                raise AgentException.message("人工交互缺少已持久化的响应")

            if request.kind == "user_question" and persisted_response["decision"] == "cancel":
                suspension, requests = await self.interaction_service.cancel_suspension(
                    suspension.id,
                    self.user_id,
                    session_id=session_id,
                )
                await self._finish_cancelled_interaction(
                    session,
                    turn,
                    requests,
                    "用户取消了问题交互。",
                )
                await self.db.commit()
                continuation_applied = True
                terminal_committed = True
                await self.bus.emit(
                    RuntimeEvent.session_ready(session.id, session.title, session.status)
                )
                await self.bus.emit(
                    StreamEvent.turn_end(
                        ORCHESTRATOR_ACTOR,
                        session_id,
                        "user_cancelled",
                        None,
                    )
                )
                return

            interaction = await self._apply_interaction_and_continue(
                session_id,
                session,
                turn,
                request,
                suspension,
            )
            if interaction is not None:
                await self.db.commit()
                await self._emit_interaction(interaction)
                return

            await self.interaction_service.mark_suspension_resolved(suspension.id)
            await self.session_service.repo.update_status(session, "running")
            await self.session_service.mark_turn_status(turn, "running")
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
            )
            if loop_result.suspended:
                await self.db.commit()
                if loop_result.interaction is not None:
                    await self._emit_interaction(loop_result.interaction)
                return
            memory_job_id = await self._complete_turn(
                session,
                turn,
                loop_result.completed_message_id,
            )
            await self._run_inline_memory_job(memory_job_id)
            await self._wake_team_members(session_id, turn)
        except asyncio.CancelledError:
            if terminal_committed or (response_committed and not continuation_applied):
                await self.db.rollback()
            else:
                await self._mark_abnormal_end(
                    session_id,
                    turn.id if turn else None,
                    "interrupted",
                )
            raise
        except Exception as exc:
            logger.exception(
                "恢复人工交互轮次失败",
                extra={"session_id": session_id, "turn_id": str(turn.id if turn else "")},
            )
            if not response_committed:
                # 请求格式、请求归属或并发状态错误都不能破坏仍可响应的原暂停点。
                await self.db.rollback()
                message = exc.message if isinstance(exc, AgentException) else "人工交互响应处理失败。"
                await self.bus.emit(
                    StreamEvent.error_event(
                        ORCHESTRATOR_ACTOR,
                        session_id,
                        ErrorInfo(
                            code="INTERACTION_RESPONSE_INVALID",
                            message=message,
                            retriable=True,
                            fatal=True,
                        ),
                    )
                )
            elif terminal_committed or idempotent_replay or not continuation_applied:
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
        finally:
            await self.bus.close()

    async def _run_llm_loop(
        self,
        session_id: int,
        session: SessionRecord,
        turn: SessionTurnRecord,
        additional_context: str | None,
    ) -> TurnLoopResult:
        """运行 ReAct 循环并处理模型工具调用,事件以协议形式 emit 到 bus。

        返回结果包含是否挂起；成功结束时还包含最终助手消息 ID。
        """
        actor = ORCHESTRATOR_ACTOR
        # 翻译器跨本回合多次 LLM 调用共用:block index 持续递增、tool_use 入参累积。
        translator = AnthropicStreamTranslator(actor, session_id)

        # 回合开始(开场事件,带完整 actor)。
        await self.bus.emit(StreamEvent.turn_start(actor, session_id))

        # skill catalog 每次 run 取一次缓存复用(仅主代理注入,§15.2):进 for 循环前取一次,
        # 循环内各轮共用,避免每轮 loop 都打 DB。
        skills_catalog = await SkillService(self.db).get_catalog()
        memory_context = await self.memory_recall_service.get_or_create_context(turn)
        # 子代理使用独立数据库会话，冻结结果必须先提交才能稳定继承。
        if memory_context is not None:
            await self.db.commit()
        rendered_memories = memory_context.rendered_memories if memory_context else None
        # blocks 形式携带 cache 断点：tools + 稳定段跨 turn 复用，
        # 完整 system 供本 turn 内多轮工具迭代复用。
        system_prompt: list[TextBlockParam] = compose_lead_blocks(
            PromptContext.create(
                skills_catalog=skills_catalog,
                session_prompt=session.system_prompt,
                memory_catalog=memory_context.rendered_catalog if memory_context else None,
            )
        )
        active_plan = await self.plan_service.get_active(session_id)
        active_registry = self._registry_for_mode(active_plan is not None)
        if active_plan is not None:
            system_prompt.append(
                {
                    "type": "text",
                    "text": render_plan_mode_instructions(active_plan.content),
                }
            )
        tools: list[ToolParam] = active_registry.to_anthropic_tools()

        stop_reason: str | None = None
        for _ in range(settings.max_tool_iterations):
            history, current, through_message_id = await self.session_service.load_context_for_turn(
                turn,
                additional_context,
                rendered_memories=rendered_memories,
            )
            # 只压缩当前 Turn 之前的历史，原始请求和本轮工具轨迹始终完整保留。
            history = await self.compact_service.maybe_compact(
                session_id,
                history,
                through_message_id=through_message_id,
            )
            context = history + current
            final_content: list[dict[str, Any]] | None = None
            async for chunk in self.llm.stream(context, system_prompt, tools=tools):
                if chunk.get("type") == "message_final":
                    final_content = chunk.get("content")
                # 原始 chunk 交给翻译器产出协议事件
                for event in translator.translate(chunk):
                    await self.bus.emit(event)

            stop_reason = translator.stop_reason
            tool_uses = extract_tool_uses(final_content)

            assistant_message = await self.session_service.add_message(
                session_id,
                "assistant",
                final_content if final_content else "",
                turn_id=turn.id,
            )

            if not tool_uses:
                # Stop:回合即将结束时触发。hook 可返回 continuation 强制续跑
                # (作为一条 user 消息注入),否则正常结束本回合。
                continuation = await self._apply_stop_hooks(
                    session_id, turn.id, actor, context
                )
                if continuation is not None:
                    await self.session_service.add_message(
                        session_id,
                        "user",
                        continuation,
                        turn_id=turn.id,
                    )
                    await self.db.flush()
                    continue
                break

            interaction = await self._execute_tool_uses(
                session_id,
                session,
                turn,
                actor,
                assistant_message.id,
                tool_uses,
                done_ids=[],
                additional_context=additional_context,
            )
            await self.db.flush()
            if interaction is not None:
                return TurnLoopResult(suspended=True, interaction=interaction)
        else:
            # 达到轮数上限:禁用工具最后调一次 LLM,产出最终答复(优雅降级)。
            assistant_message = await self._finalize_without_tools(
                session_id,
                turn,
                translator,
                additional_context,
                rendered_memories,
                system_prompt,
            )
            stop_reason = translator.stop_reason or "max_iterations"

        # 回合结束
        await self.bus.emit(
            StreamEvent.turn_end(actor, session_id, stop_reason, translator.last_usage)
        )
        return TurnLoopResult(
            suspended=False,
            completed_message_id=assistant_message.id,
        )

    async def _complete_turn(
        self,
        session: SessionRecord,
        turn: SessionTurnRecord,
        completed_message_id: int | None,
    ) -> UUID | None:
        """原子完成 Turn、恢复 Session，并按开关幂等创建提取 Job。"""
        await self.session_service.mark_turn_status(
            turn,
            "completed",
            completed_message_id=completed_message_id,
        )
        await self.session_service.repo.update_status(session, "idle")
        job_id: UUID | None = None
        if settings.memory_extraction_enabled:
            # Memory 是旁路子系统，入队失败不得回滚 Turn 完成与 Session 恢复。
            # 失败语句会让事务进入 aborted，故用 SAVEPOINT 隔离后回滚，
            # 保证下面的 commit 仍能提交主链路状态；本轮记忆则直接放弃。
            nested = await self.db.begin_nested()
            try:
                job = await MemoryJobService(self.db).enqueue_extract(session, turn)
            except asyncio.CancelledError:
                raise
            except Exception:
                await nested.rollback()
                logger.exception(
                    "创建Memory提取任务失败，本轮不提取记忆",
                    extra={"session_id": str(session.id), "turn_id": str(turn.id)},
                )
            else:
                await nested.commit()
                job_id = job.id
        # 流式运行需要在生产者生命周期内明确提交最终状态，不能依赖请求收尾。
        await self.db.commit()
        return job_id

    async def _run_inline_memory_job(self, job_id: UUID | None) -> None:
        """终轮提交后就地执行提取；失败时由持久 Job 和 Worker 接管。"""
        if job_id is None or not settings.memory_extraction_inline:
            return
        try:
            if self.workspace_id is None or self.user_id is None:
                raise RuntimeError("就地执行Memory任务缺少可信作用域")
            await MemoryJobRunner(
                claim_scope=(self.workspace_id, self.user_id),
            ).run_once(job_id=job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "就地执行Memory提取任务失败，等待独立Worker接管",
                extra={"job_id": str(job_id)},
            )

    async def _mark_abnormal_end(
        self,
        session_id: int | None,
        turn_id: UUID | None,
        turn_status: str,
    ) -> None:
        """回滚当前失败事务，再尽力持久化 Turn 和 Session 的异常终态。"""
        await self.db.rollback()
        if session_id is None or turn_id is None:
            return
        try:
            session = await self.session_service.repo.get(session_id)
            turn = await self.session_service.repo.get_turn(turn_id)
            if session is None or turn is None or turn.status == "completed":
                return
            await self.session_service.mark_turn_status(turn, turn_status)
            await self.session_service.repo.update_status(session, "failed")
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            logger.exception(
                "持久化交互轮次异常终态失败",
                extra={"session_id": session_id, "turn_id": str(turn_id)},
            )

    async def _execute_tool_uses(
        self,
        session_id: int,
        session: SessionRecord,
        turn: SessionTurnRecord,
        actor: Actor,
        assistant_message_id: int,
        tool_uses: list[ToolUse],
        done_ids: list[str],
        additional_context: str | None,
        existing_suspension: RuntimeSuspensionRecord | None = None,
    ) -> SuspendedInteraction | None:
        """按原始顺序执行工具；需要人工输入时返回可发布的交互事件快照。"""
        for idx, tool_use in enumerate(tool_uses):
            if tool_use.id in done_ids:
                continue

            active_plan = await self.plan_service.get_active(session_id)
            if active_plan is not None and tool_use.name not in PLAN_MODE_ALLOWED_TOOLS:
                await self._record_tool_response(
                    session_id,
                    turn.id,
                    actor,
                    tool_use,
                    "Plan Mode仅允许只读探索、AskUserQuestion、WritePlan和ExitPlanMode。",
                    is_error=True,
                )
                done_ids.append(tool_use.id)
                continue

            try:
                normalized_input = self.tool_registry.validate_input(
                    tool_use.name,
                    tool_use.input,
                )
            except AgentException as exc:
                details = (
                    f"：{json.dumps(exc.details, ensure_ascii=False, default=str)}"
                    if exc.details
                    else ""
                )
                await self._record_tool_response(
                    session_id,
                    turn.id,
                    actor,
                    tool_use,
                    f"{exc.message}{details}",
                    is_error=True,
                )
                done_ids.append(tool_use.id)
                continue
            tool_use = replace(tool_use, input=normalized_input)
            tool_uses[idx] = tool_use

            if (
                active_plan is not None
                and tool_use.name == "Agent"
                and tool_use.input.get("agent_type") not in {"Explore", "Plan"}
            ):
                await self._record_tool_response(
                    session_id,
                    turn.id,
                    actor,
                    tool_use,
                    "Plan Mode只能派发Explore或Plan只读子代理。",
                    is_error=True,
                )
                done_ids.append(tool_use.id)
                continue

            if tool_use.name in INTERACTION_TOOL_KINDS:
                if tool_use.name == "EnterPlanMode" and active_plan is not None:
                    await self._record_tool_response(
                        session_id,
                        turn.id,
                        actor,
                        tool_use,
                        "当前会话已经处于Plan Mode。",
                        is_error=True,
                    )
                    done_ids.append(tool_use.id)
                    continue
                if tool_use.name == "ExitPlanMode":
                    if active_plan is None:
                        await self._record_tool_response(
                            session_id,
                            turn.id,
                            actor,
                            tool_use,
                            "当前会话未处于Plan Mode，不能调用ExitPlanMode。",
                            is_error=True,
                        )
                        done_ids.append(tool_use.id)
                        continue
                    if not active_plan.content.strip():
                        await self._record_tool_response(
                            session_id,
                            turn.id,
                            actor,
                            tool_use,
                            "计划正文为空。请先使用WritePlan保存计划，再请求退出。",
                            is_error=True,
                        )
                        done_ids.append(tool_use.id)
                        continue

                remaining = [item for item in tool_uses[idx:] if item.id not in done_ids]
                return await self._suspend_for_interaction(
                    session_id,
                    session,
                    turn,
                    actor,
                    assistant_message_id,
                    remaining,
                    done_ids,
                    additional_context,
                    INTERACTION_TOOL_KINDS[tool_use.name],
                    existing_suspension,
                )

            behavior = await self.permission_service.evaluate(
                session_id, tool_use.name, tool_use.input
            )
            if behavior == "ask":
                remaining = [item for item in tool_uses[idx:] if item.id not in done_ids]
                return await self._suspend_for_interaction(
                    session_id,
                    session,
                    turn,
                    actor,
                    assistant_message_id,
                    remaining,
                    done_ids,
                    additional_context,
                    "tool_approval",
                    existing_suspension,
                )
            if behavior == "deny":
                await self._record_tool_response(
                    session_id,
                    turn.id,
                    actor,
                    tool_use,
                    "权限策略拒绝执行该工具调用。",
                    is_error=True,
                )
                done_ids.append(tool_use.id)
                continue
            await self._execute_one(
                session_id,
                turn.id,
                actor,
                assistant_message_id,
                tool_use,
            )
            done_ids.append(tool_use.id)
        return None

    async def _execute_one(
        self,
        session_id: int,
        turn_id: UUID,
        actor: Actor,
        assistant_message_id: int,
        tool_use,
        record: ToolCallRecord | None = None,
    ) -> str:
        """执行单个工具:调用 tool_service、emit tool_result、落 tool 消息。

        record 非空时复用该 tool_calls 记录(审批通过后复用之前的 awaiting 记录),
        避免同一调用留下两条记录。
        """
        output = await self.tool_service.run(
            session_id,
            tool_use.name,
            tool_use.input,
            message_id=assistant_message_id,
            bus=self.bus,
            record=record,
            actor=actor,
            turn_id=turn_id,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
        )
        await self.bus.emit(
            StreamEvent.tool_result(
                actor,
                session_id,
                ToolInfo(
                    id=tool_use.id,
                    name=tool_use.name,
                    output=output,
                    is_error=False,
                ),
            )
        )
        await self.session_service.add_message(
            session_id,
            "tool",
            ToolResultMessage(
                tool_use_id=tool_use.id,
                tool_name=tool_use.name,
                input_args=tool_use.input,
                output=output,
                is_error=False,
            ).to_content_dict(),
            turn_id=turn_id,
        )
        return output

    async def _record_tool_response(
        self,
        session_id: int,
        turn_id: UUID,
        actor: Actor,
        tool_use: ToolUse,
        output: str,
        *,
        is_error: bool,
        record: ToolCallRecord | None = None,
    ) -> None:
        """持久化不经过真实工具执行的合成 tool_result。"""
        if record is not None:
            if is_error:
                await self.tool_service.tool_repo.reject_awaiting(record, output)
            else:
                await self.tool_service.tool_repo.succeed(record, output)
        await self.bus.emit(
            StreamEvent.tool_result(
                actor,
                session_id,
                ToolInfo(
                    id=tool_use.id,
                    name=tool_use.name,
                    output=output,
                    is_error=is_error,
                ),
            )
        )
        await self.session_service.add_message(
            session_id,
            "tool",
            ToolResultMessage(
                tool_use_id=tool_use.id,
                tool_name=tool_use.name,
                input_args=tool_use.input,
                output=output,
                is_error=is_error,
            ).to_content_dict(),
            turn_id=turn_id,
        )

    async def _suspend_for_interaction(
        self,
        session_id: int,
        session: SessionRecord,
        turn: SessionTurnRecord,
        actor: Actor,
        assistant_message_id: int,
        remaining: list[ToolUse],
        done_ids: list[str],
        additional_context: str | None,
        kind: str,
        existing_suspension: RuntimeSuspensionRecord | None,
    ) -> SuspendedInteraction:
        """冻结 continuation 并创建一项业务交互请求。"""
        target = remaining[0]
        record = await self.tool_service.tool_repo.start(
            session_id, target.name, target.input, message_id=assistant_message_id
        )
        await self.tool_service.tool_repo.mark_awaiting_interaction(record)
        continuation = {
            "assistant_message_id": assistant_message_id,
            "actor": actor.to_dict_compact(),
            "pending": [
                {
                    "tool_use_id": tu.id,
                    "tool_name": tu.name,
                    "input": tu.input,
                    "tool_call_id": record.id if tu.id == target.id else None,
                }
                for tu in remaining
            ],
            "done_tool_use_ids": list(done_ids),
            "additional_context": additional_context,
        }
        request_payload = await self._build_interaction_payload(session_id, kind, target)
        request_create = InteractionRequestCreate(
            kind=kind,
            request_payload=request_payload,
            tool_call_id=record.id,
            tool_use_id=target.id,
        )

        if existing_suspension is None:
            suspension, request = await self.interaction_service.create_suspension_with_request(
                session_id,
                turn.id,
                continuation,
                {"mode": "sequential"},
                request_create,
            )
        else:
            suspension, request = (
                await self.interaction_service.continue_suspension_with_request(
                    existing_suspension.id,
                    continuation,
                    request_create,
                )
            )
        await self.session_service.repo.update_status(session, "awaiting_interaction")
        await self.session_service.mark_turn_status(turn, "awaiting_interaction")
        return SuspendedInteraction(
            session_id=session_id,
            suspension_id=suspension.id,
            request_id=request.id,
            kind=request.kind,
            request_payload=dict(request.request_payload),
            schema_version=request.schema_version,
        )

    def _ask_reason(self, tool_name: str) -> str:
        """给前端展示的审批原因文案。"""
        if tool_name in DANGEROUS_TOOLS:
            return "危险工具默认需审批"
        return "该工具已被配置为需审批"

    async def _build_interaction_payload(
        self,
        session_id: int,
        kind: str,
        target: ToolUse,
    ) -> dict[str, Any]:
        if kind == "tool_approval":
            return {
                "tool": {
                    "id": target.id,
                    "name": target.name,
                    "input": target.input,
                },
                "reason": self._ask_reason(target.name),
                "allowed_decisions": ["allow_once", "always_allow", "deny"],
                "can_update_input": True,
            }
        if kind == "user_question":
            return AskUserQuestionRequestPayload.model_validate(target.input).model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
        if kind == "enter_plan_mode":
            return {"message": "是否进入只读Plan Mode？"}
        if kind == "exit_plan_mode":
            plan = await self.plan_service.get_active_required(session_id)
            return {
                "plan_id": str(plan.id),
                "plan": plan.content,
                "plan_version": plan.version,
                "allowedPrompts": target.input.get("allowedPrompts", []),
            }
        raise AgentException.message("不支持的人工交互类型")

    async def _apply_interaction_and_continue(
        self,
        session_id: int,
        session: SessionRecord,
        turn: SessionTurnRecord,
        request: InteractionRequestRecord,
        suspension: RuntimeSuspensionRecord,
    ) -> SuspendedInteraction | None:
        """应用已持久化的业务响应，并继续同一 assistant 工具序列。"""
        pending = suspension.continuation_payload
        pending_items = pending.get("pending", [])
        if not pending_items:
            raise AgentException.message("运行暂停点缺少待处理工具")
        assistant_message_id = pending.get("assistant_message_id")
        if not isinstance(assistant_message_id, int):
            raise AgentException.message("运行暂停点缺少关联的助手消息")
        done_ids = list(pending.get("done_tool_use_ids", []))
        tool_uses = [
            ToolUse(id=it["tool_use_id"], name=it["tool_name"], input=it.get("input") or {})
            for it in pending_items
        ]
        target_item = pending_items[0]
        target = tool_uses[0]
        # 复用挂起时建的 awaiting 记录(避免同一调用留两条)。
        record: ToolCallRecord | None = None
        tool_call_id = target_item.get("tool_call_id")
        if tool_call_id is not None:
            record = await self.db.get(ToolCallRecord, tool_call_id)
        if record is None or request.tool_call_id != record.id or request.tool_use_id != target.id:
            raise AgentException.message("人工交互与工具调用记录不匹配")
        response = request.response_payload
        if response is None:
            raise AgentException.message("人工交互缺少已持久化的响应")

        actor = ORCHESTRATOR_ACTOR
        if request.kind == "tool_approval":
            await self._apply_tool_approval(
                session_id,
                turn,
                actor,
                assistant_message_id,
                target,
                tool_uses,
                record,
                response,
            )
        elif request.kind == "user_question":
            await self._apply_user_question(turn, actor, target, record, request, response)
        elif request.kind == "enter_plan_mode":
            await self._apply_enter_plan_mode(turn, actor, target, record, response)
        elif request.kind == "exit_plan_mode":
            await self._apply_exit_plan_mode(turn, actor, target, record, request, response)
        else:
            raise AgentException.message("不支持的人工交互类型")
        done_ids.append(target.id)

        return await self._execute_tool_uses(
            session_id,
            session,
            turn,
            actor,
            assistant_message_id,
            tool_uses,
            done_ids,
            additional_context=pending.get("additional_context"),
            existing_suspension=suspension,
        )

    async def _apply_tool_approval(
        self,
        session_id: int,
        turn: SessionTurnRecord,
        actor: Actor,
        assistant_message_id: int,
        target: ToolUse,
        tool_uses: list[ToolUse],
        record: ToolCallRecord,
        response: dict[str, Any],
    ) -> None:
        decision = response["decision"]
        if decision == "deny":
            message = response.get("message") or "用户拒绝执行该工具调用。"
            await self._record_tool_response(
                session_id,
                turn.id,
                actor,
                target,
                message,
                is_error=True,
                record=record,
            )
            return

        updated_input = response.get("updated_input")
        if updated_input is not None:
            normalized = self.tool_registry.validate_input(target.name, updated_input)
            target = replace(target, input=normalized)
            tool_uses[0] = target
        if decision == "always_allow":
            await self.permission_service.add_always_allow(
                session_id,
                target.name,
                response.get("always_scope", "session"),
            )
        await self._execute_one(
            session_id,
            turn.id,
            actor,
            assistant_message_id,
            target,
            record=record,
        )

    async def _apply_user_question(
        self,
        turn: SessionTurnRecord,
        actor: Actor,
        target: ToolUse,
        record: ToolCallRecord,
        request: InteractionRequestRecord,
        response: dict[str, Any],
    ) -> None:
        decision = response["decision"]
        answers = response.get("answers") or {}
        if decision == "submit":
            output = self._format_question_answers(
                request.request_payload,
                answers,
                response.get("annotations") or {},
            )
            is_error = False
        elif decision == "cancel":
            raise AgentException.message("取消问题交互必须由运行时中止路径处理")
        elif decision == "discuss":
            output = self._format_question_discussion(
                request.request_payload,
                answers,
                response.get("message"),
            )
            is_error = True
        else:
            active_plan = await self.plan_service.get_active(turn.session_id)
            if active_plan is None:
                output = "只有Plan Mode中的规划访谈可以使用finish_plan_interview。"
            else:
                output = self._format_finish_plan_interview(
                    request.request_payload,
                    answers,
                    response.get("message"),
                )
            is_error = True
        await self._record_tool_response(
            turn.session_id,
            turn.id,
            actor,
            target,
            output,
            is_error=is_error,
            record=record,
        )

    async def _apply_enter_plan_mode(
        self,
        turn: SessionTurnRecord,
        actor: Actor,
        target: ToolUse,
        record: ToolCallRecord,
        response: dict[str, Any],
    ) -> None:
        if response["decision"] == "approve":
            await self.plan_service.enter(turn.session_id, turn.id)
            output = ENTER_PLAN_MODE_RESULT
            is_error = False
        else:
            feedback = response.get("feedback")
            output = "用户拒绝进入Plan Mode。"
            if feedback:
                output = f"{output}\n\n用户反馈：{feedback}"
            is_error = True
        await self._record_tool_response(
            turn.session_id,
            turn.id,
            actor,
            target,
            output,
            is_error=is_error,
            record=record,
        )

    async def _apply_exit_plan_mode(
        self,
        turn: SessionTurnRecord,
        actor: Actor,
        target: ToolUse,
        record: ToolCallRecord,
        request: InteractionRequestRecord,
        response: dict[str, Any],
    ) -> None:
        if self.user_id is None:
            raise AgentException.message("退出Plan Mode缺少可信用户")
        edited_plan = response.get("edited_plan")
        feedback = response.get("feedback")
        if response["decision"] == "approve":
            allowed_prompts = request.request_payload.get("allowedPrompts") or []
            plan = await self.plan_service.approve_exit(
                turn.session_id,
                turn.id,
                self.user_id,
                allowed_prompts,
                feedback,
                edited_plan,
            )
            output = f"用户已批准以下计划并退出Plan Mode：\n\n{plan.content}"
            if feedback:
                output = f"{output}\n\n用户反馈：{feedback}"
            if allowed_prompts:
                output = (
                    f"{output}\n\nallowedPrompts仅作为计划声明保存，"
                    "不会绕过现有工具权限规则。"
                )
            is_error = False
        else:
            plan = await self.plan_service.reject_exit(
                turn.session_id,
                feedback,
                edited_plan,
            )
            output = "用户暂未批准计划，Plan Mode保持启用。"
            if feedback:
                output = f"{output}\n\n用户反馈：{feedback}"
            output = f"{output}\n\n当前计划：\n{plan.content}"
            is_error = True
        await self._record_tool_response(
            turn.session_id,
            turn.id,
            actor,
            target,
            output,
            is_error=is_error,
            record=record,
        )

    @staticmethod
    def _format_question_answers(
        request_payload: dict[str, Any],
        answers: dict[str, str],
        annotations: dict[str, dict[str, str]],
    ) -> str:
        parts: list[str] = []
        for question in request_payload.get("questions", []):
            question_text = question["question"]
            answer = answers[question_text]
            answer_parts = [f'"{question_text}"="{answer}"']
            annotation = annotations.get(question_text) or {}
            if annotation.get("preview"):
                answer_parts.append(f"selected preview:\n{annotation['preview']}")
            if annotation.get("notes"):
                answer_parts.append(f"user notes: {annotation['notes']}")
            parts.append(" ".join(answer_parts))
        joined = ", ".join(parts)
        return (
            "User has answered your questions: "
            f"{joined}. You can now continue with the user's answers in mind."
        )

    @staticmethod
    def _format_question_discussion(
        request_payload: dict[str, Any],
        answers: dict[str, str],
        message: str | None,
    ) -> str:
        lines = ["用户希望先讨论这些问题。请先询问用户想澄清什么，再按需重写问题。"]
        for question in request_payload.get("questions", []):
            text = question["question"]
            lines.append(f'- "{text}": {answers.get(text, "（未回答）")}')
        if message:
            lines.append(f"用户补充：{message}")
        return "\n".join(lines)

    @staticmethod
    def _format_finish_plan_interview(
        request_payload: dict[str, Any],
        answers: dict[str, str],
        message: str | None,
    ) -> str:
        lines = ["用户表示规划访谈信息已经足够。停止继续澄清并完成计划。"]
        for question in request_payload.get("questions", []):
            text = question["question"]
            lines.append(f'- "{text}": {answers.get(text, "（未回答）")}')
        if message:
            lines.append(f"用户补充：{message}")
        return "\n".join(lines)

    def _registry_for_mode(self, plan_mode: bool) -> ToolRegistry:
        if plan_mode:
            return self.tool_registry.only(*PLAN_MODE_ALLOWED_TOOLS)
        return self.tool_registry.without("ExitPlanMode", "WritePlan")

    async def _get_pending_interaction(self, session_id: int) -> SuspendedInteraction:
        """读取已提交的唯一待处理请求，用于幂等重放 SSE 通知。"""
        pending = await self.interaction_service.get_pending(session_id)
        if pending is None:
            raise AgentException.message("会话没有可重放的人工交互")
        suspension, requests = pending
        if suspension.status != "pending" or len(requests) != 1:
            raise AgentException.message(
                "运行暂停点的待处理请求数量无效",
                {"status": suspension.status, "request_count": len(requests)},
            )
        request = requests[0]
        return SuspendedInteraction(
            session_id=session_id,
            suspension_id=suspension.id,
            request_id=request.id,
            kind=request.kind,
            request_payload=dict(request.request_payload),
            schema_version=request.schema_version,
        )

    async def _finish_cancelled_interaction(
        self,
        session: SessionRecord,
        turn: SessionTurnRecord,
        requests: list[InteractionRequestRecord],
        message: str,
    ) -> None:
        """将取消投影到工具调用、Turn 与 Session，避免留下半挂起状态。"""
        for request in requests:
            if request.tool_call_id is not None:
                record = await self.tool_service.get_interaction_call_required(
                    request.tool_call_id,
                    session.id,
                )
                if record.status == "awaiting_interaction":
                    if request.tool_use_id is None:
                        raise AgentException.message("人工交互请求缺少工具调用ID")
                    await self._record_tool_response(
                        session.id,
                        turn.id,
                        ORCHESTRATOR_ACTOR,
                        ToolUse(
                            id=request.tool_use_id,
                            name=record.tool_name,
                            input=dict(record.input_args),
                        ),
                        message,
                        is_error=True,
                        record=record,
                    )
                elif record.status not in {"succeeded", "failed", "rejected"}:
                    raise AgentException.message(
                        "人工交互关联的工具调用状态无效",
                        {"tool_call_id": record.id, "status": record.status},
                    )
        await self.session_service.mark_turn_status(turn, "interrupted")
        await self.session_service.repo.update_status(session, "idle")

    async def _normalize_interaction_response(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """在响应落库前完成依赖工具注册表的校验。"""
        if kind != "tool_approval" or response_payload.get("updated_input") is None:
            return response_payload
        request = await self.interaction_service.get_request_required(
            request_id,
            session_id=session_id,
        )
        tool = request.request_payload.get("tool") or {}
        tool_name = tool.get("name")
        if not isinstance(tool_name, str):
            raise AgentException.message("工具审批请求缺少有效工具名")
        normalized = dict(response_payload)
        normalized["updated_input"] = self.tool_registry.validate_input(
            tool_name,
            response_payload["updated_input"],
        )
        return normalized

    async def _emit_interaction(self, interaction: SuspendedInteraction) -> None:
        await self.bus.emit(
            StreamEvent.interaction_request(
                ORCHESTRATOR_ACTOR,
                interaction.session_id,
                InteractionInfo(
                    request_id=str(interaction.request_id),
                    suspension_id=str(interaction.suspension_id),
                    kind=interaction.kind,
                    schema_version=interaction.schema_version,
                    request_payload=interaction.request_payload,
                ),
            )
        )

    async def _finalize_without_tools(
        self,
        session_id: int,
        turn: SessionTurnRecord,
        translator: AnthropicStreamTranslator,
        additional_context: str | None,
        rendered_memories: str | None,
        system_prompt: str | list[TextBlockParam],
    ) -> SessionMessage:
        """禁用工具再调一次 LLM,产出最终答复并落库。沿用同一翻译器维持事件连续。"""
        history, current, through_message_id = await self.session_service.load_context_for_turn(
            turn,
            additional_context,
            rendered_memories=rendered_memories,
        )
        history = await self.compact_service.maybe_compact(
            session_id,
            history,
            through_message_id=through_message_id,
        )
        context = history + current
        final_content: list[dict[str, Any]] | None = None

        async for chunk in self.llm.stream(context, system_prompt, tools=[]):
            if chunk.get("type") == "message_final":
                final_content = chunk.get("content")
            for event in translator.translate(chunk):
                await self.bus.emit(event)

        return await self.session_service.add_message(
            session_id,
            "assistant",
            final_content if final_content else "",
            turn_id=turn.id,
        )

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
        return next((o.continuation for o in outcomes if o.continuation), None)

    async def _wake_team_members(
        self,
        session_id: int,
        turn: SessionTurnRecord,
    ) -> None:
        """B1: 请求内单步唤醒。

        lead 主循环结束后,把本会话所有 status=='working' 的队友各唤醒一次,
        每人调一次 run_teammate 推进一步(单轮,不 while 连锁,B2 后台自动唤醒后续再加)。
        每个队友用独立 AsyncSessionLocal,共享 self.bus 让增量流式到前端 SSE。
        任一队友异常用 try/except 包住,emit error 事件后继续,不中断主流程。
        """
        from database.engine import AsyncSessionLocal
        from runtime.subagent import SubAgentRunner
        from team.service import TeamService

        members = await TeamService(self.db).list_members(session_id)
        working = [m for m in members if m.status == "working"]
        for member in working:
            try:
                spec = get_subagent_spec(AgentType(member.agent_type or "general_purpose"))
                # 独立 session 避免与主会话共用连接;共享 bus 让队友增量冒泡到 SSE。
                async with AsyncSessionLocal() as sub_db:
                    await SubAgentRunner(sub_db, bus=self.bus).run_teammate(
                        session_id,
                        member.name,
                        spec,
                        parent_turn_id=turn.id,
                        user_id=turn.user_id,
                        workspace_id=turn.workspace_id,
                    )
            except Exception as exc:  # noqa: BLE001 - 单个队友失败必须与主流程隔离
                # 单个队友失败隔离:发 error 事件后继续唤醒其余成员。
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


def format_sse(event: dict[str, Any]) -> str:
    """把事件字典序列化为 SSE 数据帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
