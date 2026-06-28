import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.event_bus import StreamBus
from app.core.events import RuntimeEvent
from app.core.team_task_isolation import (
    new_team_task_instance_id,
    reset_team_task_instance_id,
    set_team_task_instance_id,
)
from app.llm.client import LLMClient
from app.llm.types import chunk_text, chunk_type, extract_tool_uses, text_from_content
from app.repositories.subagent_run_repo import SubAgentRunRepository
from app.services.team_service import TeamService
from app.services.tool_service import ToolService
from app.tools.registry import ToolRegistry, build_tool_registry
from app.tools.subagents.definition import SubAgentSpec

# teammate ReAct 轮次上限。比一次性子代理(6 轮)略放宽,因为 teammate 还要
# 处理收件箱与认领任务;但仍设硬上限以防失控空转。
TEAMMATE_MAX_ROUNDS = 12


class SubAgentRunner:
    def __init__(self, db: AsyncSession, bus: StreamBus | None = None):
        """初始化子代理运行器依赖。"""
        self.db = db
        self.bus = bus
        self.llm = LLMClient()
        self.run_repo = SubAgentRunRepository(db)

    async def run(self, session_id: int, prompt: str, spec: SubAgentSpec) -> str:
        """无状态执行一次子代理:按 spec 配置跑 ReAct 并返回报告。

        不依赖团队成员或收件箱,prompt 由调用方(Agent 工具)直接给出。
        member_name 复用为 agent_type 值,兼容运行记录表与前端事件契约。
        """
        member_name = spec.agent_type.value
        tool_registry = spec.resolve_tools(build_tool_registry())
        tool_service = ToolService(self.db, tool_registry)

        # 为本次 run 设置独立隔离 id(供运行期内派生消息的隔离与调试归属)。
        token = set_team_task_instance_id(new_team_task_instance_id())
        try:
            run = await self.run_repo.create(session_id, member_name, prompt)
            await self._emit(
                "subagent_run_created",
                {"run_id": run.id, "member": member_name, "prompt": prompt},
            )
            try:
                await self._emit(
                    "subagent_run_started", {"run_id": run.id, "member": member_name}
                )
                report = await self._run_react_loop(
                    session_id, member_name, prompt, run.id, spec, tool_registry, tool_service
                )
                await self.run_repo.succeed(run, report)
                await self.db.commit()
                await self._emit(
                    "subagent_run_done",
                    {"run_id": run.id, "member": member_name, "report": report},
                )
                return report
            except Exception as exc:
                await self.run_repo.fail(run, str(exc))
                await self.db.commit()
                await self._emit(
                    "subagent_run_error",
                    {"run_id": run.id, "member": member_name, "error": str(exc)},
                )
                raise
        finally:
            reset_team_task_instance_id(token)

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """向事件总线发送子代理事件(若总线存在)。"""
        if self.bus is None:
            return
        await self.bus.emit(RuntimeEvent(type=event_type, data=data))

    async def _run_react_loop(
        self,
        session_id: int,
        member_name: str,
        prompt: str,
        run_id: int,
        spec: SubAgentSpec,
        tool_registry: ToolRegistry,
        tool_service: ToolService,
    ) -> str:
        """执行子代理 ReAct 循环并返回报告。系统提示与工具集均取自 spec。"""
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        for _ in range(6):
            final_content: list[dict[str, Any]] | None = None

            async for chunk in self.llm.stream(
                messages,
                spec.system_prompt,
                tools=tool_registry.to_anthropic_tools(),
            ):
                if chunk_type(chunk) == "message_final":
                    final_content = chunk.get("content")
                    continue
                text = chunk_text(chunk)
                if text:
                    await self._emit(
                        "subagent_run_delta",
                        {"run_id": run_id, "member": member_name, "text": text},
                    )

            tool_uses = extract_tool_uses(final_content)

            messages.append({"role": "assistant", "content": final_content or ""})

            if not tool_uses:
                return text_from_content(final_content)

            for tool_use in tool_uses:
                output = await tool_service.run(
                    session_id,
                    tool_use.name,
                    tool_use.input,
                )
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
            await self.db.flush()

        raise RuntimeError("子代理工具调用轮次过多，已停止执行")

    # ============ teammate 模式(B1: 无状态短运行 + 请求内唤醒) ============

    async def run_teammate(self, session_id: int, member, spec: SubAgentSpec) -> str:
        """恢复并单次推进一个 teammate:读历史→注入身份→排空收件箱→认领→ReAct→写回。

        与 run() 的一次性子代理不同,teammate 状态全落库(member.history),
        本方法是一个「恢复 + 单次推进」运行单元:跑完即结束,不起常驻线程。
        任何实例都能据 DB 接着唤醒,多实例正确性由 DB 保证。
        """
        team_service = TeamService(self.db)
        name = member.name
        role = member.role
        # 拆分后通信类工具(SendMessage / ReadInbox / ListMessages / TeamList)不在黑名单内,
        # spec.resolve_tools 只剔除造人 / 派子代理 / shell(TeamCreate/TeamSpawn/Agent/Bash),
        # 因此 teammate 天然能通信但不能越权,无需再补回受限工具实例。
        tool_registry = spec.resolve_tools(build_tool_registry())
        tool_service = ToolService(self.db, tool_registry)

        # 为本次推进设置独立隔离 id(收件箱消费按该 id 隔离;任务认领不隔离)。
        token = set_team_task_instance_id(new_team_task_instance_id())
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
            system_prompt = self._teammate_system_prompt(spec, session_id, name, role)

            await self._emit(
                "subagent_run_started",
                {"member": name, "role": role, "session_id": session_id},
            )
            # 4) ReAct 循环(轮内允许认领本会话 task)。
            report = await self._run_teammate_loop(
                session_id, name, role, messages, spec, system_prompt, tool_registry, tool_service
            )

            # 5) 写回历史 + 据剩余待办决定 idle / working。
            await team_service.repo.update_member_history(
                member, json.dumps(messages, ensure_ascii=False)
            )
            await self._settle_member_status(session_id, name, member, team_service)
            await self.db.commit()
            await self._emit(
                "subagent_run_done",
                {"member": name, "role": role, "report": report},
            )
            return report
        except Exception as exc:
            await self.db.rollback()
            await self._emit(
                "subagent_run_error",
                {"member": name, "role": role, "error": str(exc)},
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

    def _teammate_system_prompt(
        self, spec: SubAgentSpec, session_id: int, name: str, role: str
    ) -> str:
        """在 spec.system_prompt 基础上拼接 teammate 身份头,不改 registry 里的 spec。"""
        header = (
            f"你是团队成员 '{name}',角色: {role},隶属会话 {session_id} 的团队。\n"
            f"你可以通过 Team 工具与其他成员协作,并认领本会话的待办任务。\n"
        )
        return header + (spec.system_prompt or "")

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
        name: str,
        role: str,
        messages: list[dict[str, Any]],
        spec: SubAgentSpec,
        system_prompt: str,
        tool_registry: ToolRegistry,
        tool_service: ToolService,
    ) -> str:
        """teammate 版 ReAct:每轮开始尝试认领本会话 task,再走模型+工具循环。"""
        for _ in range(TEAMMATE_MAX_ROUNDS):
            # 轮首尝试认领可处理任务(只按 session_id,不加实例隔离)。
            await self._try_claim_tasks(session_id, name, messages)

            final_content: list[dict[str, Any]] | None = None
            async for chunk in self.llm.stream(
                messages,
                system_prompt,
                tools=tool_registry.to_anthropic_tools(),
            ):
                if chunk_type(chunk) == "message_final":
                    final_content = chunk.get("content")
                    continue
                text = chunk_text(chunk)
                if text:
                    await self._emit(
                        "subagent_run_delta",
                        {"member": name, "role": role, "text": text},
                    )

            tool_uses = extract_tool_uses(final_content)
            # final_content 已是 model_dump(mode="json") 产物(JSON 原生),可直接落库。
            messages.append({"role": "assistant", "content": final_content or ""})

            if not tool_uses:
                return text_from_content(final_content)

            for tool_use in tool_uses:
                output = await tool_service.run(session_id, tool_use.name, tool_use.input)
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
            await self.db.flush()

        raise RuntimeError("teammate 工具调用轮次过多，已停止执行")

    async def _try_claim_tasks(
        self, session_id: int, name: str, messages: list[dict[str, Any]]
    ) -> None:
        """认领本会话可处理任务,认领成功的作为 user 消息注入 messages。"""
        team_service = TeamService(self.db)
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
