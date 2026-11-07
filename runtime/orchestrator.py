"""统一编排协议与运行上下文。

Workflow 当前仍按顺序、并行和路由分别实现控制流，但它们共享同一个
``Orchestrator`` 协议和 ``OrchestrationContext``。后续 Team、循环和恢复编排
可以复用这两个抽象，而不再自行定义一套运行身份与状态容器。
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from core.event_bus import StreamBus
from core.events import Actor


@dataclass
class OrchestrationContext:
    """一次编排运行共享的身份、事件和状态。

    ``state`` 保存节点之间传递的业务数据，``cursor`` 表示最近一次完成的
    控制流位置。当前仅以内存对象承载，不做持久化。
    """

    run_id: UUID
    session_id: int
    turn_id: UUID
    actor: Actor
    bus: StreamBus
    state: dict[str, Any] = field(default_factory=dict)
    cursor: str | None = None
    revision: int = 0

    def update(
        self,
        *,
        cursor: str | None = None,
        state_updates: Mapping[str, Any] | None = None,
    ) -> None:
        """原子更新控制流游标和节点状态，并推进状态版本。"""
        if cursor is not None:
            self.cursor = cursor
        if state_updates:
            self.state.update(deepcopy(dict(state_updates)))
        self.revision += 1


class Orchestrator(ABC):
    """所有控制流编排器遵循的最小异步协议。"""

    @abstractmethod
    async def run(self, user_content: str) -> Any:
        """执行一次编排并返回最终结果。"""


@dataclass(frozen=True)
class TopLevelOrchestrator:
    """用户消息进入运行时后的统一分发器。

    HTTP 层只负责鉴权、会话锁定和参数校验；具体选择普通 Agent 还是 Workflow
    由这里完成。运行时类通过构造参数注入，既便于测试，也避免本模块和具体运行时
    形成循环导入。
    """

    db: Any
    user_id: int
    workspace_id: int
    agent_runtime_cls: type | None = None
    workflow_runtime_cls: type | None = None
    llm: Any | None = None

    async def start(
        self,
        session_id: int | None,
        user_content: str,
        *,
        agent_id: int | None = None,
        workflow_id: int | None = None,
        model_options: dict[str, Any] | None = None,
    ) -> Any:
        """按请求的目标类型启动一次后台执行。"""
        if agent_id is not None and workflow_id is not None:
            raise ValueError("同一次请求不能同时指定Agent和Workflow")
        if workflow_id is not None:
            runtime_cls = self.workflow_runtime_cls
            if runtime_cls is None:
                from runtime.workflow import WorkflowRuntime

                runtime_cls = WorkflowRuntime
            kwargs = {"user_id": self.user_id, "workspace_id": self.workspace_id}
            if self.llm is not None:
                kwargs["llm"] = self.llm
            runtime = runtime_cls(self.db, **kwargs)
            # 没有模型选项时不传该参数，让运行时类使用自己的默认值。
            start_kwargs: dict[str, Any] = {"workflow_id": workflow_id}
            if model_options:
                start_kwargs["model_options"] = model_options
            return await runtime.start(session_id, user_content, **start_kwargs)

        runtime_cls = self.agent_runtime_cls
        if runtime_cls is None:
            from runtime.agent import AgentRuntime

            runtime_cls = AgentRuntime
        kwargs = {"user_id": self.user_id, "workspace_id": self.workspace_id}
        if self.llm is not None:
            kwargs["llm"] = self.llm
        runtime = runtime_cls(self.db, **kwargs)
        start_kwargs: dict[str, Any] = {"agent_id": agent_id}
        if model_options:
            start_kwargs["model_options"] = model_options
        return await runtime.start(session_id, user_content, **start_kwargs)

