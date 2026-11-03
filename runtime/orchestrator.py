"""统一编排协议与运行上下文。

Workflow 当前仍按顺序、并行和路由分别实现控制流，但它们共享同一个
``Orchestrator`` 协议和 ``OrchestrationContext``。后续 Team、循环和恢复编排
可以复用这两个抽象，而不再自行定义一套运行身份与状态容器。
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from core.event_bus import StreamBus
from core.events import Actor


@dataclass(frozen=True)
class OrchestrationCheckpoint:
    """可持久化的编排状态快照。

    快照只保存编排控制流需要的身份、游标和业务状态，不保存数据库连接、事件总线
    或其他运行时对象。这样可以在请求结束后落到现有运行状态存储，恢复时再注入依赖。
    """

    run_id: UUID
    session_id: int
    turn_id: UUID
    cursor: str | None
    state: dict[str, Any]
    revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON/JSONB 的副本，避免调用方修改上下文内部状态。"""
        return {
            "run_id": str(self.run_id),
            "session_id": self.session_id,
            "turn_id": str(self.turn_id),
            "cursor": self.cursor,
            "state": deepcopy(self.state),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OrchestrationCheckpoint:
        """从 JSON/JSONB 形状恢复快照，保留未知 state 字段以支持前向兼容。"""
        return cls(
            run_id=UUID(str(data["run_id"])),
            session_id=int(data["session_id"]),
            turn_id=UUID(str(data["turn_id"])),
            cursor=data.get("cursor"),
            state=deepcopy(dict(data.get("state", {}))),
            revision=int(data.get("revision", 0)),
        )


@dataclass
class OrchestrationContext:
    """一次编排运行共享的身份、事件和可恢复状态。

    ``state`` 保存节点之间传递的业务数据，``cursor`` 表示最近一次完成的
    控制流位置。当前版本先以内存对象承载，持久化 checkpoint 由后续阶段接入。
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
        """原子更新控制流游标和节点状态，并推进快照版本。"""
        if cursor is not None:
            self.cursor = cursor
        if state_updates:
            self.state.update(deepcopy(dict(state_updates)))
        self.revision += 1

    def snapshot(self) -> OrchestrationCheckpoint:
        """生成不携带运行时依赖的快照。"""
        return OrchestrationCheckpoint(
            run_id=self.run_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            cursor=self.cursor,
            state=deepcopy(self.state),
            revision=self.revision,
        )

    def restore(self, checkpoint: OrchestrationCheckpoint) -> None:
        """恢复同一轮次的快照，拒绝跨运行/跨会话误用。"""
        if (
            checkpoint.run_id != self.run_id
            or checkpoint.session_id != self.session_id
            or checkpoint.turn_id != self.turn_id
        ):
            raise ValueError("编排快照与当前运行上下文不匹配")
        self.cursor = checkpoint.cursor
        self.state = deepcopy(checkpoint.state)
        self.revision = checkpoint.revision


class CheckpointStore(Protocol):
    """编排检查点的最小异步协议，具体实现可在内存或数据库中。"""

    async def save(self, checkpoint: OrchestrationCheckpoint) -> None: ...

    async def load(self, run_id: UUID) -> OrchestrationCheckpoint | None: ...

    async def delete(self, run_id: UUID) -> None: ...


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

