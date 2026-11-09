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
