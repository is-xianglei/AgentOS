import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from anthropic.types import ToolParam
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import config
from app.core.event_bus import StreamBus
from app.core.events import LEAD_ACTOR, Actor, RuntimeEvent, agent_actor
from app.core.prompts import compose_system_prompt
from app.llm.client import LLMClient
from app.llm.types import ToolResultMessage, chunk_type, extract_tool_uses
from app.services.compact_service import CompactService
from app.services.session_service import SessionService
from app.services.tool_service import ToolService
from app.tools.registry import build_tool_registry
from app.tools.subagents.definition import AgentType
from app.tools.subagents.registry import get_subagent_spec


class AgentRuntime:
    def __init__(self, db: AsyncSession):
        """初始化 Agent 主运行时依赖。"""
        self.db = db
        self.session_service = SessionService(db)
        self.compact_service = CompactService(self.session_service.repo)
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
            except (asyncio.CancelledError, Exception):
                pass

    async def _produce(self, session_id: int | None, user_content: str) -> None:
        """后台生产者:跑完整流程,所有事件 emit 到 bus,最终 close。"""
        try:
            created = session_id is None
            session = await self.session_service.prepare_for_message(session_id, user_content)
            session_id = session.id
            await self.session_service.add_message(session_id, "user", user_content)
            await self.db.commit()

            event = RuntimeEvent(type='session_ready', data={
                "session_id": session.id,
                "title": session.title,
                "status": session.status,
                "created": created,
            })
            # 会话已就绪
            await self.bus.emit(event)

            # 消息开始推送
            await self.bus.emit(RuntimeEvent(type='message_start', data={"session_id": session_id}))

            try:
                # 开始 LLM Loop
                await self._run_llm_loop(session_id, session)
                # B1: lead 主循环结束后,请求内单步唤醒本会话所有 working 队友各推进一次。
                await self._wake_team_members(session_id)
                # 一次对话完成,标记会话为空闲中
                await self.session_service.mark_finished(session, "idle")

                await self.bus.emit(RuntimeEvent(type='message_stop', data={"session_id": session_id}))

            except Exception as exc:
                await self.session_service.mark_finished(session, "failed")
                await self.bus.emit(RuntimeEvent(type='session_error', data={"message": str(exc)}))
        finally:
            await self.bus.close()

    async def _run_llm_loop(self, session_id: int, session) -> None:
        """运行 ReAct 循环并处理模型工具调用,事件 emit 到 bus。"""
        for _ in range(config.MAX_TOOL_ITERATIONS):
            # 加载对话上下文
            context = await self.session_service.load_context(session_id)
            # 压缩上下文
            context = await self.compact_service.maybe_compact(session_id, context)

            final_content: list[dict[str, Any]] | None = None

            # 系统提示词 + 会话提示词
            system_prompt: str = compose_system_prompt(session.system_prompt)

            # 所有工具列表
            tools: list[ToolParam] = self.tool_registry.to_anthropic_tools()

            chunks = self.llm.stream(context, system_prompt, tools=tools)

            async for chunk in chunks:
                if chunk_type(chunk) == "message_final":
                    final_content = chunk.get("content")
                    continue
                await self.bus.emit(add_runtime_metadata(chunk, session_id=session_id, actor=LEAD_ACTOR))

            tool_uses = extract_tool_uses(final_content)

            assistant_message = await self.session_service.add_message(
                session_id,
                "assistant",
                final_content if final_content else "",
            )

            if not tool_uses:
                return

            for tool_use in tool_uses:
                # 工具名称
                tool_name = tool_use.name
                # 工具参数
                input_args = tool_use.input
                # 推送工具开始执行的状态
                await self.bus.emit(RuntimeEvent(type='tool_start', data={"tool_name": tool_name, "input_args": input_args}))
                # 执行工具,获取工具执行结果
                output = await self.tool_service.run(
                    session_id,
                    tool_name,
                    input_args,
                    message_id=assistant_message.id,
                    bus=self.bus,
                )
                # 将工具执行结果添加上对话上下文
                await self.session_service.add_message(
                    session_id,
                    "tool",
                    ToolResultMessage(
                        tool_use_id=tool_use.id,
                        tool_name=tool_name,
                        input_args=input_args,
                        output=output,
                    ).to_content_dict(),
                )
                # 推送工具执行结束的状态
                await self.bus.emit(RuntimeEvent(type='tool_done', data={"tool_name": tool_name, "output": output}))

            await self.db.flush()

        # 达到轮数上限:不再报错,强制禁用工具最后调用一次 LLM,
        # 让模型基于已有的工具结果给出最终答复(优雅降级)。
        await self._finalize_without_tools(session_id)

    async def _finalize_without_tools(self, session_id: int) -> None:
        """禁用工具再调一次 LLM,产出最终答复并落库。"""
        context = await self.session_service.load_context(session_id)
        context = await self.compact_service.maybe_compact(session_id, context)
        final_content: list[dict[str, Any]] | None = None

        async for chunk in self.llm.stream(
            context, compose_system_prompt(session_prompt=None), tools=[]
        ):
            if chunk_type(chunk) == "message_final":
                final_content = chunk.get("content")
                continue
            await self.bus.emit(
                add_runtime_metadata(
                    chunk,
                    session_id=session_id,
                    actor=LEAD_ACTOR,
                )
            )

        await self.session_service.add_message(
            session_id,
            "assistant",
            final_content if final_content else "",
        )

    async def _wake_team_members(self, session_id: int) -> None:
        """B1: 请求内单步唤醒。

        lead 主循环结束后,把本会话所有 status=='working' 的队友各唤醒一次,
        每人调一次 run_teammate 推进一步(单轮,不 while 连锁,B2 后台自动唤醒后续再加)。
        每个队友用独立 AsyncSessionLocal,共享 self.bus 让增量流式到前端 SSE。
        任一队友异常用 try/except 包住,emit error 事件后继续,不中断主流程。
        """
        from app.db.session import AsyncSessionLocal
        from app.services.subagent_runner import SubAgentRunner
        from app.services.team_service import TeamService

        members = await TeamService(self.db).list_members(session_id)
        working = [m for m in members if m.status == "working"]
        for member in working:
            try:
                spec = get_subagent_spec(
                    AgentType(member.agent_type or "general_purpose")
                )
                # 独立 session 避免与主会话共用连接;共享 bus 让队友增量冒泡到 SSE。
                async with AsyncSessionLocal() as sub_db:
                    await SubAgentRunner(sub_db, bus=self.bus).run_teammate(
                        session_id, member, spec
                    )
            except Exception as exc:
                # 单个队友失败隔离:发 error 事件后继续唤醒其余成员。
                await self.bus.emit(
                    RuntimeEvent(
                        type="subagent_run_error",
                        data={
                            "member": member.name,
                            "error": str(exc),
                            "actor": agent_actor(member.name).to_dict(),
                        },
                    )
                )


def format_sse(event: dict[str, Any]) -> str:
    """把事件字典序列化为 SSE 数据帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def add_runtime_metadata(
    chunk: dict[str, Any],
    *,
    session_id: int,
    actor: Actor,
) -> dict[str, Any]:
    """给透传的模型原始 chunk 附加运行时上下文(session_id + actor)。"""
    chunk["session_id"] = session_id
    chunk["actor"] = actor.to_dict()
    return chunk



