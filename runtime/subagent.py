import asyncio
import json
import logging
from copy import deepcopy
from typing import Any
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
    subagent_actor,
    teammate_actor,
)
from llm.client import LLMClient
from llm.types import (
    AnthropicStreamTranslator,
    ToolInfo,
    extract_tool_uses,
    text_from_content,
)
from memory.recall.repository import MemoryRecallRepository
from permission.service import PermissionService
from prompt import compose_subagent_prompt, compose_teammate_prompt
from team.subagent_run_repository import SubAgentRunRepository
from team.service import TeamService
from team.isolation import (
    new_team_task_instance_id,
    reset_team_task_instance_id,
    set_team_task_instance_id,
)
from tools.service import ToolService
from tools.registry import ToolRegistry, build_tool_registry
from tools.subagents.definition import SubAgentSpec

logger = logging.getLogger(__name__)

# teammate ReAct 轮次上限。比一次性子代理默认值略放宽,因为 teammate 还要
# 处理收件箱与认领任务;但仍设硬上限以防失控空转。
TEAMMATE_MAX_ROUNDS = 12

# 子代理并发闸门。进程级信号量,按配置的并发上限做背压:
# 超额的 Agent 调用在此排队而非直接失败,避免一次派发过多子代理打满上游速率限制。
_concurrency_gate: asyncio.Semaphore | None = None


def _get_concurrency_gate() -> asyncio.Semaphore:
    """惰性创建并发信号量(需在事件循环内构造,故不放模块加载期)。"""
    global _concurrency_gate
    if _concurrency_gate is None:
        _concurrency_gate = asyncio.Semaphore(max(1, settings.subagent_max_concurrency))
    return _concurrency_gate


class SubAgentRunner:
    def __init__(self, db: AsyncSession, bus: StreamBus | None = None):
        """初始化子代理运行器依赖。"""
        self.db = db
        self.bus = bus
        self.llm = LLMClient()
        self.run_repo = SubAgentRunRepository(db)
        self.permission_service = PermissionService(db)

    async def run(
        self,
        session_id: int,
        prompt: str,
        spec: SubAgentSpec,
        *,
        parent_turn_id: UUID | None = None,
        user_id: int | None = None,
        workspace_id: int | None = None,
    ) -> str:
        """无状态执行一次子代理:按 spec 配置跑 ReAct 并返回报告。

        不依赖团队成员或收件箱,prompt 由调用方(Agent 工具)直接给出。
        member_name 复用为 agent_type 值,兼容运行记录表与前端事件契约。
        """
        member_name = spec.agent_type.value
        tool_registry = spec.resolve_tools(build_tool_registry())
        tool_service = ToolService(self.db, tool_registry)
        # omit_inherited_memory 的 spec(如 Explore)不继承父 Turn 记忆:
        # 它只做代码检索,注入用户偏好等长期记忆既无用又占满上下文预算。
        rendered_memories = (
            None
            if spec.omit_inherited_memory
            else await self._load_frozen_memories(
                session_id,
                parent_turn_id,
                user_id,
                workspace_id,
            )
        )
        system_prompt = compose_subagent_prompt(spec.system_prompt, rendered_memories)
        if spec.critical_reminder:
            # 关键约束放在系统提示末尾:近端位置比开头更不容易被长上下文冲淡。
            system_prompt = f"{system_prompt}\n\n{spec.critical_reminder}"

        # 为本次 run 设置独立隔离 id(供运行期内派生消息的隔离与调试归属)。
        token = set_team_task_instance_id(new_team_task_instance_id())
        try:
            run = await self.run_repo.create(session_id, member_name, prompt)
            # 一次性子代理的 actor:role=subagent,name=agent_type,唯一键 run_id,task=prompt。
            actor = subagent_actor(member_name, run_id=f"run-{run.id}", task=prompt)
            translator = AnthropicStreamTranslator(actor, session_id)
            await self._emit_event(StreamEvent.turn_start(actor, session_id))
            try:
                # 并发闸门只圈住 ReAct 循环本身(LLM + 工具),不含建记录与事件收尾,
                # 避免持锁做无谓的 DB 往返。
                async with _get_concurrency_gate():
                    report = await self._run_react_loop(
                        session_id,
                        actor,
                        translator,
                        prompt,
                        system_prompt,
                        rendered_memories,
                        tool_registry,
                        tool_service,
                        spec,
                        parent_turn_id,
                        user_id,
                        workspace_id,
                    )
                await self.run_repo.succeed(run, report)
                await self.db.commit()
                await self._emit_event(
                    StreamEvent.turn_end(
                        actor, session_id, translator.stop_reason, translator.last_usage
                    )
                )
                return report
            except Exception as exc:
                await self.run_repo.fail(run, str(exc))
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
        context = await MemoryRecallRepository(self.db).get_context(
            workspace_id,
            user_id,
            parent_turn_id,
            session_id=session_id,
        )
        return context.rendered_memories if context is not None else None

    @staticmethod
    def _with_memory_context(
        messages: list[dict[str, Any]],
        rendered_memories: str | None,
    ) -> list[dict[str, Any]]:
        """仅在 LLM 请求副本中前置正文，避免污染 teammate 持久历史。"""
        request_messages = deepcopy(messages)
        if not rendered_memories:
            return request_messages
        for message in request_messages:
            content = message.get("content")
            if (
                message.get("role") == "user"
                and isinstance(content, str)
                and not content.startswith("<identity>")
            ):
                message["content"] = f"{rendered_memories}\n\n{content}"
                break
        return request_messages

    async def _emit_event(self, event: StreamEvent) -> None:
        """向事件总线发送协议事件(若总线存在)。"""
        if self.bus is None:
            return
        await self.bus.emit(event)

    async def _stream_and_translate(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tool_registry: ToolRegistry,
        translator: AnthropicStreamTranslator,
        rendered_memories: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """跑一次 LLM 流:原始 chunk 经翻译器产出协议事件并 emit,返回 final_content。"""
        final_content: list[dict[str, Any]] | None = None
        request_messages = self._with_memory_context(messages, rendered_memories)
        async for chunk in self.llm.stream(
            request_messages,
            system_prompt,
            tools=tool_registry.to_anthropic_tools(),
            model=model,
        ):
            if chunk.get("type") == "message_final":
                final_content = chunk.get("content")
            for event in translator.translate(chunk):
                await self._emit_event(event)
        return final_content

    async def _emit_tool_result(
        self,
        actor: Actor,
        session_id: int,
        tool_use,
        output: str,
        *,
        is_error: bool = False,
    ) -> None:
        """发协议 tool_result 事件(actor 为该产出者自身)。"""
        await self._emit_event(
            StreamEvent.tool_result(
                actor,
                session_id,
                ToolInfo(id=tool_use.id, name=tool_use.name, output=output, is_error=is_error),
            )
        )

    async def _execute_tool_uses(
        self,
        session_id: int,
        actor: Actor,
        tool_uses: list[Any],
        messages: list[dict[str, Any]],
        tool_service: ToolService,
        spec: SubAgentSpec,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
    ) -> None:
        """裁决并执行一轮 tool_use,把结果追加回 messages。

        子代理没有人工交互挂起通道(无法像主代理那样发布请求后等待用户回包),
        因此 ask 在此坍缩为拒绝,并把原因作为 tool_result 回给模型,让它自行改道;
        不能沿用"未裁决直接执行"的旧行为,否则子代理成为绕过审批的提权路径。
        """
        agent_type = spec.agent_type.value
        for tool_use in tool_uses:
            behavior = await self.permission_service.evaluate(
                session_id,
                tool_use.name,
                tool_use.input,
                agent_type=agent_type,
            )
            if behavior == "allow":
                output = await tool_service.run(
                    session_id,
                    tool_use.name,
                    tool_use.input,
                    actor=actor,
                    turn_id=parent_turn_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    shell_access=spec.shell_access,
                    shell_tmp_root=spec.shell_tmp_root(),
                )
                is_error = False
            else:
                output = self._denied_message(tool_use.name, behavior)
                is_error = True
                logger.warning(
                    "子代理工具调用被权限策略拒绝,会话 ID=%s 类型=%s 工具=%s 判定=%s",
                    session_id,
                    agent_type,
                    tool_use.name,
                    behavior,
                    extra={"session_id": session_id, "agent_type": agent_type},
                )
            await self._emit_tool_result(actor, session_id, tool_use, output, is_error=is_error)
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": output,
                        }
                    ],
                }
            )

    @staticmethod
    def _denied_message(tool_name: str, behavior: str) -> str:
        """拒绝原因文本:区分显式 deny 与因无审批通道而坍缩的 ask。"""
        if behavior == "ask":
            return (
                f"error: 工具 {tool_name} 需要用户审批,而子代理无审批通道,"
                "本次调用已被拒绝。请改用只读方式完成,或把该操作留给主代理。"
            )
        return f"error: 工具 {tool_name} 已被权限策略禁止,本次调用未执行。"

    async def _run_react_loop(
        self,
        session_id: int,
        actor: Actor,
        translator: AnthropicStreamTranslator,
        prompt: str,
        system_prompt: str,
        rendered_memories: str | None,
        tool_registry: ToolRegistry,
        tool_service: ToolService,
        spec: SubAgentSpec,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
    ) -> str:
        """执行子代理 ReAct 循环并返回报告。系统提示、工具集与轮次上限均取自 spec。"""
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        model = spec.resolve_model()
        last_text = ""
        for _ in range(max(1, spec.max_rounds)):
            final_content = await self._stream_and_translate(
                messages,
                system_prompt,
                tool_registry,
                translator,
                rendered_memories,
                model=model,
            )
            tool_uses = extract_tool_uses(final_content)
            messages.append({"role": "assistant", "content": final_content or ""})
            last_text = text_from_content(final_content) or last_text

            if not tool_uses:
                return text_from_content(final_content)

            await self._execute_tool_uses(
                session_id,
                actor,
                tool_uses,
                messages,
                tool_service,
                spec,
                parent_turn_id,
                user_id,
                workspace_id,
            )
            await self.db.flush()

        # 轮次耗尽不抛异常:调用方(Agent 工具)需要拿到已完成的部分结论。
        # 抛异常会让整轮子代理白跑,而截断报告 + 明确标注仍可供主代理判断与补做。
        logger.warning(
            "子代理轮次耗尽,返回截断报告,会话 ID=%s 类型=%s 上限=%s",
            session_id,
            spec.agent_type.value,
            spec.max_rounds,
            extra={"session_id": session_id, "agent_type": spec.agent_type.value},
        )
        notice = f"[注意] 子代理已达 {spec.max_rounds} 轮工具调用上限,以下为截断的中间结论:"
        return f"{notice}\n\n{last_text}" if last_text else notice

    # ============ teammate 模式(B1: 无状态短运行 + 请求内唤醒) ============

    async def run_teammate(
        self,
        session_id: int,
        member_name: str,
        spec: SubAgentSpec,
        *,
        parent_turn_id: UUID | None = None,
        user_id: int | None = None,
        workspace_id: int | None = None,
    ) -> str:
        """恢复并单次推进一个 teammate:读历史→注入身份→排空收件箱→认领→ReAct→写回。

        与 run() 的一次性子代理不同,teammate 状态全落库(member.history),
        本方法是一个「恢复 + 单次推进」运行单元:跑完即结束,不起常驻线程。
        任何实例都能据 DB 接着唤醒,多实例正确性由 DB 保证。
        """
        team_service = TeamService(self.db)
        member = await team_service.repo.get_member(session_id, member_name)
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
        # 拆分后通信类工具(SendMessage / ReadInbox / ListMessages / TeamList)不在黑名单内,
        # spec.resolve_tools 只剔除造人 / 派子代理 / shell(TeamCreate/TeamSpawn/Agent/Bash),
        # 因此 teammate 天然能通信但不能越权,无需再补回受限工具实例。
        tool_registry = spec.resolve_tools(build_tool_registry())
        tool_service = ToolService(self.db, tool_registry)

        # 为本次推进设置独立隔离 id(收件箱消费按该 id 隔离;任务认领不隔离)。
        instance_id = new_team_task_instance_id()
        token = set_team_task_instance_id(instance_id)
        # teammate 的 actor:role=teammate,唯一键 name,run_id=本次推进隔离 id,
        # task=初始任务,team 携带 team_id(若已归属团队)。
        team_ref = TeamRef(id=member.team_id) if member.team_id is not None else None
        actor = teammate_actor(name, run_id=instance_id, task=member.prompt, team=team_ref)
        translator = AnthropicStreamTranslator(actor, session_id)
        try:
            # 1) 从 member.history 反序列化历史;为空则用初始 prompt 起一条 user 消息。
            messages = self._load_history(member)

            # 2) 身份重注入:确保首条是 identity,避免重复注入。
            self._ensure_identity(messages, session_id, name, role)

            # 3) 排空收件箱:每条未读消息作为 user 消息追加(read_inbox 内部已隔离)。
            inbox = await team_service.read_inbox(session_id, name)
            for msg in inbox:
                messages.append(
                    {
                        "role": "user",
                        "content": f"[消息来自 {msg.sender}] {msg.content}",
                    }
                )

            # teammate 人格头:在 spec.system_prompt 之上拼接 name/role/team 身份。
            system_prompt = compose_teammate_prompt(
                spec.system_prompt, session_id, name, role
            )
            system_prompt = compose_subagent_prompt(system_prompt, rendered_memories)

            await self._emit_event(StreamEvent.turn_start(actor, session_id))
            # 4) ReAct 循环(轮内允许认领本会话 task)。
            report = await self._run_teammate_loop(
                session_id,
                actor,
                translator,
                messages,
                system_prompt,
                rendered_memories,
                tool_registry,
                tool_service,
                spec,
                parent_turn_id,
                user_id,
                workspace_id,
            )

            # 5) 写回历史 + 据剩余待办决定 idle / working。
            await team_service.repo.update_member_history(
                member, json.dumps(messages, ensure_ascii=False)
            )
            await self._settle_member_status(session_id, name, member, team_service)
            await self.db.commit()
            await self._emit_event(
                StreamEvent.turn_end(
                    actor, session_id, translator.stop_reason, translator.last_usage
                )
            )
            return report
        except Exception as exc:
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
        JSON 原生 dict(写回时由 model_dump(mode="json") 保证),可直接 round-trip。
        """
        raw = (member.history or "").strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        prompt = member.prompt or ""
        return [{"role": "user", "content": prompt}]

    def _identity_block(self, session_id: int, name: str, role: str) -> str:
        """构造身份块文本(对应 s11 的 make_identity_block)。"""
        return (
            f"<identity>You are '{name}', role: {role}, "
            f"team session {session_id}. Continue your work.</identity>"
        )

    def _ensure_identity(
        self, messages: list[dict[str, Any]], session_id: int, name: str, role: str
    ) -> None:
        """在 messages 最前面注入 identity;若首条已是 identity 则跳过避免重复。"""
        identity = self._identity_block(session_id, name, role)
        first = messages[0] if messages else None
        first_text = first.get("content") if isinstance(first, dict) else None
        if (
            isinstance(first_text, str)
            and first_text.startswith("<identity>")
            and "Continue your work." in first_text
        ):
            return
        messages.insert(0, {"role": "user", "content": identity})

    async def _settle_member_status(
        self, session_id: int, name: str, member, team_service: TeamService
    ) -> None:
        """据剩余未读消息 / 可认领任务,把成员置 working 或 idle。"""
        pending_inbox = await team_service.peek_inbox(session_id, name)
        claimable = await team_service.list_claimable_tasks(session_id)
        status = "working" if (pending_inbox or claimable) else "idle"
        await team_service.repo.update_member_runtime(member, status=status)

    async def _run_teammate_loop(
        self,
        session_id: int,
        actor: Actor,
        translator: AnthropicStreamTranslator,
        messages: list[dict[str, Any]],
        system_prompt: str,
        rendered_memories: str | None,
        tool_registry: ToolRegistry,
        tool_service: ToolService,
        spec: SubAgentSpec,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
    ) -> str:
        """teammate 版 ReAct:每轮开始尝试认领本会话 task,再走模型+工具循环。"""
        name = actor.name
        model = spec.resolve_model()
        for _ in range(TEAMMATE_MAX_ROUNDS):
            # 轮首尝试认领可处理任务(只按 session_id,不加实例隔离)。
            await self._try_claim_tasks(session_id, name, messages)

            final_content = await self._stream_and_translate(
                messages,
                system_prompt,
                tool_registry,
                translator,
                rendered_memories,
                model=model,
            )
            tool_uses = extract_tool_uses(final_content)
            # final_content 已是 model_dump(mode="json") 产物(JSON 原生),可直接落库。
            messages.append({"role": "assistant", "content": final_content or ""})

            if not tool_uses:
                return text_from_content(final_content)

            await self._execute_tool_uses(
                session_id,
                actor,
                tool_uses,
                messages,
                tool_service,
                spec,
                parent_turn_id,
                user_id,
                workspace_id,
            )
            await self.db.flush()

        # teammate 历史全落库,截断后可再次唤醒接着推进,故这里也不抛异常。
        logger.warning(
            "teammate 轮次耗尽,本次推进结束,会话 ID=%s 成员=%s",
            session_id,
            name,
            extra={"session_id": session_id},
        )
        return f"[注意] 已达 {TEAMMATE_MAX_ROUNDS} 轮工具调用上限,本次推进到此结束,可再次唤醒继续。"

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
