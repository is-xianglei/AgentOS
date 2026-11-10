"""应用层入口 AgentService：把发消息、恢复人工交互、取消暂停点与取消轮次分发给对应的运行时。

运行时在 runtime 包里，依赖 session、tools 等 feature 域，应用层不导入它，只认这里声明的端口：
AgentRunner、WorkflowRunner 描述运行时要提供的能力，runtime 的 AgentRuntime、WorkflowRuntime 天然满足。
具体类由装配方以工厂形式注入，目前是 api/deps.py 的 get_agent_service，阶段 7 改由组装根装配。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ExecutionHandle:
    """返回给流式接口:从 start_cursor 之后、sequence 大于 start_offset 处开始跟随。"""

    turn_id: UUID
    session_id: int
    start_offset: int
    start_cursor: str


class CancelledSuspension(Protocol):
    """被取消的运行暂停点；interaction 域的 RuntimeSuspensionRecord 天然满足。"""

    @property
    def id(self) -> UUID: ...

    @property
    def session_id(self) -> int: ...

    @property
    def turn_id(self) -> UUID: ...

    @property
    def status(self) -> str: ...


class CancelledTurn(Protocol):
    """被取消轮次的终态；会话存储端口的 TurnTermination 天然满足。"""

    @property
    def turn_id(self) -> UUID: ...

    @property
    def status(self) -> str: ...

    @property
    def session_status(self) -> str: ...


class AgentRunner(Protocol):
    """主 Agent 运行时：准入并启动后台执行、保存人工响应后恢复执行、取消运行暂停点。"""

    async def start(
        self,
        session_id: int | None,
        user_content: str,
        *,
        agent_id: int | None = None,
        model_options: dict[str, Any] | None = None,
    ) -> ExecutionHandle: ...

    async def start_resume(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
    ) -> ExecutionHandle: ...

    async def cancel_interaction(
        self,
        session_id: int,
        suspension_id: UUID,
    ) -> CancelledSuspension: ...


class WorkflowRunner(Protocol):
    """Workflow 运行时：准入并启动一次完整编排。"""

    async def start(
        self,
        session_id: int | None,
        user_content: str,
        *,
        workflow_id: int,
        model_options: dict[str, Any] | None = None,
    ) -> ExecutionHandle: ...


# 运行时绑定请求作用域的数据库会话，每次调用现建一个实例，所以注入的是工厂而不是实例。
type AgentRunnerFactory = Callable[[], AgentRunner]
type WorkflowRunnerFactory = Callable[[], WorkflowRunner]
type TurnCanceller = Callable[[int, UUID], Awaitable[CancelledTurn]]


class AgentService:
    """对外的 Agent 入口：只做分发与运行时编排。

    鉴权、会话写权限与「已有会话不能切换 Agent / Workflow」等会话级校验仍在 HTTP 层完成，
    这里收到的参数都已可信。
    """

    def __init__(
        self,
        *,
        agent_runner: AgentRunnerFactory,
        workflow_runner: WorkflowRunnerFactory,
        cancel_turn: TurnCanceller,
    ) -> None:
        self._agent_runner = agent_runner
        self._workflow_runner = workflow_runner
        self._cancel_turn = cancel_turn

    async def chat(
        self,
        session_id: int | None,
        content: str,
        *,
        agent_id: int | None = None,
        workflow_id: int | None = None,
        model_options: dict[str, Any] | None = None,
    ) -> ExecutionHandle:
        """发一条用户消息：指定了 Workflow 走 Workflow 运行时，否则走主 Agent 运行时。"""
        if agent_id is not None and workflow_id is not None:
            raise ValueError("同一次请求不能同时指定Agent和Workflow")
        if workflow_id is not None:
            # 没有模型选项时不传该参数，让运行时使用自己的默认值。
            workflow_kwargs: dict[str, Any] = {"workflow_id": workflow_id}
            if model_options:
                workflow_kwargs["model_options"] = model_options
            return await self._workflow_runner().start(session_id, content, **workflow_kwargs)

        agent_kwargs: dict[str, Any] = {"agent_id": agent_id}
        if model_options:
            agent_kwargs["model_options"] = model_options
        return await self._agent_runner().start(session_id, content, **agent_kwargs)

    async def resume(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
    ) -> ExecutionHandle:
        """保存人工交互响应并恢复原轮次；只有主 Agent 会挂起等待人工交互。"""
        return await self._agent_runner().start_resume(
            session_id,
            request_id,
            kind,
            response_payload,
        )

    async def cancel_interaction(
        self,
        session_id: int,
        suspension_id: UUID,
    ) -> CancelledSuspension:
        """取消运行暂停点，关联的轮次随之中断。"""
        return await self._agent_runner().cancel_interaction(session_id, suspension_id)

    async def cancel_turn(self, session_id: int, turn_id: UUID) -> CancelledTurn:
        """取消执行中的轮次。"""
        return await self._cancel_turn(session_id, turn_id)
