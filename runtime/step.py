"""Agent 单步执行原语。

AgentStep 只负责一次模型调用：把中立消息和工具描述交给 LLM，消费
TurnEventAssembler 产出的对外事件，并把终局结果转换成可持久化的中立
assistant blocks。ReAct 是否继续、工具如何执行、数据库如何提交由上层负责。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from llm import BaseLLMClient
from llm.types import LLMTurnResult, PromptBlock, ToolUse, build_assistant_blocks
from runtime.stream_assembler import TurnEventAssembler
from tools.registry import ToolRegistry


@dataclass(frozen=True)
class StepRequest:
    """一次 LLM 调用的完整输入。"""

    messages: list[dict[str, Any]]
    system_prompt: str | list[PromptBlock] | None
    tool_registry: ToolRegistry
    model: str | None = None


@dataclass(frozen=True)
class StepResult:
    """一次 LLM 调用的终局结果和中立助手消息块。"""

    llm_result: LLMTurnResult
    assistant_blocks: list[dict[str, Any]]

    @property
    def tool_uses(self) -> list[ToolUse]:
        return self.llm_result.tool_uses

    @property
    def text(self) -> str:
        return self.llm_result.text


class AgentStep:
    """跨主 Agent、SubAgent、teammate、Workflow 节点复用的单步执行器。"""

    def __init__(
        self,
        llm: BaseLLMClient,
        assembler: TurnEventAssembler,
        emit: Callable[[Any], Awaitable[None]],
    ) -> None:
        self._llm = llm
        self._assembler = assembler
        self._emit = emit

    async def run(self, request: StepRequest) -> StepResult:
        """执行一次 LLM 流并返回终局结果。

        ``emit`` 由调用方提供，因此 AgentStep 不依赖具体的 StreamBus；主 Agent、
        子代理和 Workflow 可以共享同一实现，同时保留各自的事件归属和生命周期。
        """
        async for event in self._assembler.consume(
            self._llm.stream(
                request.messages,
                request.system_prompt,
                tools=request.tool_registry.to_specs(),
                model=request.model,
            )
        ):
            await self._emit(event)
        result = self._assembler.last_result
        return StepResult(
            llm_result=result,
            assistant_blocks=build_assistant_blocks(
                result.text,
                result.tool_uses,
                result.thinking,
            ),
        )
