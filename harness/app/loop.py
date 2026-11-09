"""ReAct 循环骨架：主 Agent、子代理与队友共用的唯一一份「调模型 → 执行工具」循环。

骨架只管控制流：轮首钩子 → 构造上下文 → 调模型 → 写 assistant 消息 → 没有工具调用时由策略决定
结束还是续跑 → 审核并执行工具 → 刷新本轮写入 → 用完轮次后由策略收尾。消息从哪里来、写到哪里、
工具如何审核执行、挂起与上限如何处理，都由各运行时的循环策略（LoopPolicy）实现。骨架不碰数据库、
事件总线和 feature 域，只依赖领域类型与模型端口。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from harness.app.ports import ModelSettings, ModelTurnResult
from harness.domain.context import Context
from harness.domain.messages import ToolCall


@dataclass(frozen=True)
class StepResult:
    """一次模型调用的终局结果和中立助手消息块。"""

    llm_result: ModelTurnResult
    assistant_blocks: list[dict[str, Any]]

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.llm_result.tool_calls

    @property
    def text(self) -> str:
        return self.llm_result.text


class ModelStep(Protocol):
    """调一次模型：流式事件由实现按自己的 actor 发出，返回终局结果。runtime 的 AgentStep 满足它。"""

    async def run(self, context: Context[None], settings: ModelSettings) -> StepResult: ...


@dataclass(frozen=True)
class LoopRound:
    """当前轮次：index 从 0 开始；is_last 表示这是轮次上限内的最后一轮。"""

    index: int
    is_last: bool


@dataclass(frozen=True)
class ModelRequest:
    """一轮发给模型的请求：角色组装好的上下文与本轮调用参数。"""

    context: Context[None]
    settings: ModelSettings


@dataclass(frozen=True)
class LoopExit[R]:
    """钩子要求立即结束循环，result 即循环的返回值。"""

    result: R


class LoopPolicy[R](ABC):
    """循环策略：一次运行一个实例，承载这次运行的消息来源、落库方式与收尾规则。

    钩子按骨架的调用顺序列出。返回 LoopExit 的钩子表示立即结束循环，返回 None 表示继续。
    """

    async def before_round(self, loop_round: LoopRound) -> None:
        """轮首钩子，在构造上下文之前调用；默认什么都不做。"""

    @abstractmethod
    async def load_context(self, loop_round: LoopRound) -> ModelRequest:
        """构造本轮发给模型的上下文与调用参数。"""

    @abstractmethod
    async def record_assistant(self, loop_round: LoopRound, reply: StepResult) -> None:
        """写入本轮的 assistant 消息（落库或追加到内存消息列表）。"""

    @abstractmethod
    async def on_no_tool_calls(
        self,
        loop_round: LoopRound,
        reply: StepResult,
    ) -> LoopExit[R] | None:
        """模型本轮没有发起工具调用：返回 LoopExit 结束循环，返回 None 则续跑，续跑同样占一轮。"""

    @abstractmethod
    async def execute_tools(self, loop_round: LoopRound, reply: StepResult) -> LoopExit[R] | None:
        """审核并执行本轮的工具调用；返回 LoopExit 表示中止循环（如挂起等待人工交互）。"""

    @abstractmethod
    async def flush(self) -> None:
        """工具阶段之后刷新本轮写入，不提交；工具阶段要求中止时也先刷新再返回。"""

    @abstractmethod
    async def on_exhausted(self) -> R:
        """用完全部轮次仍未结束时的收尾，返回循环结果。"""


async def run_react_loop[R](policy: LoopPolicy[R], step: ModelStep, *, max_rounds: int) -> R:
    """唯一的 ReAct 循环骨架：最多跑 max_rounds 轮，各运行时的差异全部经策略钩子注入。"""
    for index in range(max_rounds):
        loop_round = LoopRound(index=index, is_last=index == max_rounds - 1)
        await policy.before_round(loop_round)
        request = await policy.load_context(loop_round)
        reply = await step.run(request.context, request.settings)
        await policy.record_assistant(loop_round, reply)
        if reply.tool_calls:
            outcome = await policy.execute_tools(loop_round, reply)
            await policy.flush()
        else:
            outcome = await policy.on_no_tool_calls(loop_round, reply)
        if outcome is not None:
            return outcome.result
    return await policy.on_exhausted()
