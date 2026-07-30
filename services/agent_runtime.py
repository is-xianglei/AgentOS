import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

from anthropic.types import ToolParam
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import (
    ORCHESTRATOR_ACTOR,
    Actor,
    ErrorInfo,
    PermissionInfo,
    RuntimeEvent,
    StreamEvent,
    ToolInfo,
    teammate_actor,
)
from hooks import HookContext, HookEvent, get_hook_registry
from llm.client import LLMClient
from llm.prompts import compose_system_prompt
from llm.types import (
    AnthropicStreamTranslator,
    ToolResultMessage,
    ToolUse,
    extract_tool_uses,
)
from models.tool import ToolCallRecord
from permission.service import DANGEROUS_TOOLS, PermissionService
from services.compact_service import CompactService
from services.memory_job_service import MemoryJobRunner, MemoryJobService
from services.memory_recall_service import MemoryRecallService
from skill.service import SkillService
from services.task_service import TaskService
from services.tool_service import ToolService
from session.models import SessionMessage, SessionRecord, SessionTurnRecord
from session.service import SessionService
from tools.registry import build_tool_registry
from tools.subagents.definition import AgentType
from tools.subagents.registry import get_subagent_spec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnLoopResult:
    """一次 Turn 主循环的持久化结果。"""

    suspended: bool
    completed_message_id: int | None = None


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
                # 已因等待用户审批挂起:提交会话 awaiting_approval + 待批指针 + 已执行的
                # tool 结果,不唤醒队友、不标记 idle,正常关闭本次 SSE(前端 onDone)。
                await self.db.commit()
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
            await self._mark_abnormal_end(session_id, turn.id if turn else None, "interrupted")
            raise
        except Exception:
            logger.exception(
                "处理会话消息失败",
                extra={"session_id": session_id, "turn_id": str(turn.id if turn else "")},
            )
            await self._mark_abnormal_end(session_id, turn.id if turn else None, "failed")
        finally:
            await self.bus.close()

    async def resume(
        self,
        session_id: int,
        request_id: str,
        decision: str,
        updated_input: dict[str, Any] | None = None,
        always_scope: str = "session",
    ) -> AsyncIterator[dict[str, Any]]:
        """从审批挂起点恢复执行,产出流式事件(与 run 同构)。"""
        producer = asyncio.create_task(
            self._produce_resume(session_id, request_id, decision, updated_input, always_scope)
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

    async def _produce_resume(
        self,
        session_id: int,
        request_id: str,
        decision: str,
        updated_input: dict[str, Any] | None,
        always_scope: str,
    ) -> None:
        """后台生产者:裁决当前待批项,补齐同 turn 待批,再续跑主循环。"""
        session: SessionRecord | None = None
        turn: SessionTurnRecord | None = None
        try:
            session = await self.session_service.ensure_resumable(session_id)
            pending = self.session_service.get_pending_approval(session)
            if not pending or pending.get("request_id") != request_id:
                raise AgentException.message("审批请求已失效或不匹配")

            try:
                turn_id = UUID(str(pending.get("turn_id")))
            except (TypeError, ValueError) as exc:
                raise AgentException.message("审批请求缺少有效的交互轮次") from exc
            turn = await self.session_service.get_turn_required(turn_id)
            if turn.session_id != session_id or turn.status != "awaiting_approval":
                raise AgentException.message("审批请求对应的交互轮次不可恢复")
            if turn.user_id != self.user_id or turn.workspace_id != self.workspace_id:
                raise AgentException.message("无权限恢复该交互轮次")

            await self.session_service.repo.update_status(session, "running")
            await self.session_service.mark_turn_status(turn, "running")
            await self.db.commit()

            await self.bus.emit(
                RuntimeEvent.session_ready(session.id, session.title, session.status)
            )

            actor = ORCHESTRATOR_ACTOR
            suspended = await self._apply_decision_and_continue(
                session_id,
                session,
                turn,
                actor,
                pending,
                decision,
                updated_input,
                always_scope,
            )
            await self.db.flush()
            if suspended:
                # 同 turn 还有下一个待批项,已重新挂起:提交挂起状态后结束本次流。
                await self.db.commit()
                return

            # 该 turn 待批全部裁决完:清指针,继续主循环跑完剩余回合。
            await self.session_service.clear_pending_approval(session)
            await self.db.commit()
            # 续跑:此时上下文已含该 turn 全部 tool_result,_run_llm_loop 从下一次 LLM
            # 调用开始(新 SSE 流,重新 emit turn_start 供前端开面板)。
            loop_result = await self._run_llm_loop(
                session_id,
                session,
                turn,
                pending.get("additional_context"),
            )
            if loop_result.suspended:
                await self.db.commit()
                return
            memory_job_id = await self._complete_turn(
                session,
                turn,
                loop_result.completed_message_id,
            )
            await self._run_inline_memory_job(memory_job_id)
            await self._wake_team_members(session_id, turn)
        except asyncio.CancelledError:
            await self._mark_abnormal_end(session_id, turn.id if turn else None, "interrupted")
            raise
        except Exception:
            logger.exception(
                "恢复审批交互轮次失败",
                extra={"session_id": session_id, "turn_id": str(turn.id if turn else "")},
            )
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
        system_prompt = compose_system_prompt(
            session.system_prompt,
            skills_catalog,
            memory_context.rendered_catalog if memory_context else None,
        )
        tools: list[ToolParam] = self.tool_registry.to_anthropic_tools()

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
                continuation = await self._apply_stop_hooks(session_id, turn.id, actor)
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

            suspended = await self._execute_tool_uses(
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
            if suspended:
                # 命中 ask:待批指针已落库、permission_request 已发,提前返回挂起信号。
                return TurnLoopResult(suspended=True)
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
            await self.session_service.clear_pending_approval(session)
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
        tool_uses: list,
        done_ids: list[str],
        additional_context: str | None,
    ) -> bool:
        """按序处理一个 assistant turn 的 tool_uses,逐个过权限判定。

        - allow → 执行工具,emit tool_result,落 tool 消息,计入 done_ids;
        - deny  → 不执行,落一条"用户拒绝"tool_result 消息(让模型自行反应);
        - ask   → 不执行,写待批指针 + tool_calls 置 awaiting_approval +
                  emit permission_request + 会话置 awaiting_approval,返回 True(挂起)。

        done_ids 传入时已含本 turn 之前已完成的 tool_use_id(恢复场景),
        用于挂起时精确记录"哪些已完成、哪些还在等"。
        """
        for idx, tool_use in enumerate(tool_uses):
            if tool_use.id in done_ids:
                continue
            behavior = await self.permission_service.evaluate(
                session_id, tool_use.name, tool_use.input
            )
            if behavior == "ask":
                # 剩余未裁决的 tool_use(含当前这个)作为待批列表。
                remaining = [tu for tu in tool_uses[idx:] if tu.id not in done_ids]
                await self._suspend_for_approval(
                    session_id,
                    session,
                    turn,
                    actor,
                    assistant_message_id,
                    remaining,
                    done_ids,
                    additional_context,
                )
                return True
            if behavior == "deny":
                await self._record_denied(session_id, turn.id, actor, tool_use)
                done_ids.append(tool_use.id)
                continue
            # allow:正常执行
            await self._execute_one(
                session_id,
                turn.id,
                actor,
                assistant_message_id,
                tool_use,
            )
            done_ids.append(tool_use.id)
        return False

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

    async def _record_denied(
        self,
        session_id: int,
        turn_id: UUID,
        actor: Actor,
        tool_use,
    ) -> None:
        """把被用户拒绝的工具调用作为 tool_result 喂回模型(标记 is_error)。"""
        message = "用户拒绝执行该工具调用。"
        await self.bus.emit(
            StreamEvent.tool_result(
                actor,
                session_id,
                ToolInfo(
                    id=tool_use.id,
                    name=tool_use.name,
                    output=message,
                    is_error=True,
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
                output=message,
                is_error=True,
            ).to_content_dict(),
            turn_id=turn_id,
        )

    async def _suspend_for_approval(
        self,
        session_id: int,
        session: SessionRecord,
        turn: SessionTurnRecord,
        actor: Actor,
        assistant_message_id: int,
        remaining: list,
        done_ids: list[str],
        additional_context: str | None,
    ) -> None:
        """在 remaining[0] 处挂起:落待批指针、置 tool_calls awaiting、emit permission_request。"""
        target = remaining[0]
        request_id = f"appr-{uuid4().hex[:12]}"
        # 为待批工具建一条 awaiting_approval 记录(入参已存,便于恢复时复用)。
        record = await self.tool_service.tool_repo.start(
            session_id, target.name, target.input, message_id=assistant_message_id
        )
        await self.tool_service.tool_repo.mark_awaiting(record)
        pending = {
            "request_id": request_id,
            "turn_id": str(turn.id),
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
        await self.session_service.set_pending_approval(session, pending)
        await self.session_service.mark_turn_status(turn, "awaiting_approval")
        await self.bus.emit(
            StreamEvent.permission_request(
                actor,
                session_id,
                PermissionInfo(
                    request_id=request_id,
                    tool_id=target.id,
                    tool_name=target.name,
                    tool_input=target.input,
                    behavior="ask",
                    reason=self._ask_reason(target.name),
                ),
            )
        )

    def _ask_reason(self, tool_name: str) -> str:
        """给前端展示的审批原因文案。"""
        if tool_name in DANGEROUS_TOOLS:
            return "危险工具默认需审批"
        return "该工具已被配置为需审批"

    async def _apply_decision_and_continue(
        self,
        session_id: int,
        session: SessionRecord,
        turn: SessionTurnRecord,
        actor: Actor,
        pending: dict[str, Any],
        decision: str,
        updated_input: dict[str, Any] | None,
        always_scope: str,
    ) -> bool:
        """裁决当前待批项(pending[0]),再补齐该 turn 剩余待批项。

        返回 True 表示同 turn 还有下一个 ask、已重新挂起;False 表示该 turn 全部裁决完。
        """
        pending_items = pending.get("pending", [])
        if not pending_items:
            return False
        assistant_message_id = pending.get("assistant_message_id")
        if not isinstance(assistant_message_id, int):
            raise AgentException.message("审批请求缺少关联的助手消息")
        done_ids = list(pending.get("done_tool_use_ids", []))
        # 从待批指针重建 tool_uses(保存了原始顺序);done 的会被 _execute_tool_uses 跳过。
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

        if decision == "deny":
            if record is not None:
                await self.tool_service.tool_repo.resolve_awaiting(record, "deny")
            await self._record_denied(session_id, turn.id, actor, target)
            done_ids.append(target.id)
        else:
            # allow_once / always_allow
            if decision == "always_allow":
                await self.permission_service.add_always_allow(
                    session_id, target.name, always_scope
                )
            # 批准时可修改入参
            if updated_input is not None:
                target = replace(target, input=updated_input)
                tool_uses[0] = target
            await self._execute_one(
                session_id,
                turn.id,
                actor,
                assistant_message_id,
                target,
                record=record,
            )
            done_ids.append(target.id)

        # 继续处理该 turn 剩余待批项(可能在下一个 ask 处再次挂起)。
        return await self._execute_tool_uses(
            session_id,
            session,
            turn,
            actor,
            assistant_message_id,
            tool_uses,
            done_ids,
            additional_context=pending.get("additional_context"),
        )

    async def _finalize_without_tools(
        self,
        session_id: int,
        turn: SessionTurnRecord,
        translator: AnthropicStreamTranslator,
        additional_context: str | None,
        rendered_memories: str | None,
        system_prompt: str,
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
    ) -> str | None:
        """触发 Stop hook,返回首个非空 continuation(强制续跑),无则 None。"""
        outcomes = await get_hook_registry().trigger(
            HookContext(
                event=HookEvent.STOP,
                session_id=session_id,
                actor=actor,
                turn_id=turn_id,
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
        from db.engine import AsyncSessionLocal
        from services.subagent_runner import SubAgentRunner
        from services.team_service import TeamService

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
