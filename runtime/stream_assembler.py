"""把 provider 中立流事件装配成对外协议 StreamEvent 的装配器。"""

import logging
from collections.abc import AsyncIterator

from core.errors import AgentException
from core.events import Actor, StreamEvent
from llm.types import LLMEventKind, LLMStreamEvent, LLMTurnResult

logger = logging.getLogger(__name__)


class TurnEventAssembler:
    """把 provider 中立事件装配成对外协议 StreamEvent。

    一个 turn 一个实例,跨该 turn 内多次 stream() 调用存活——这是它存在的
    唯一理由:block index 需在 turn 内全局递增。

    block index 语义(AgentOS 块级协议的唯一定义处):
    - index 由本装配器统一分配,turn 内从 0 单调递增、全局唯一,
      可作为消费方的分组 key;不透传任何 provider 的原始 index。
    - provider 事件里的 block_ref 仅是单次 stream() 调用内的局部序号,
      由本装配器映射为全局 index,消费方永远看不到 block_ref。

    块边界语义:
    - block_start / block_stop 严格成对,消费方规则统一为「见 block_stop 即收尾」。
    - tool_use 块也成对:TOOL_USE 事件产出后由本装配器补发 block_stop,
      前端无需为 tool_use 写特例。

    turn_start / turn_end / tool_result / interaction_request / error 仍由
    runtime 自己 emit,装配器只负责「LLM 一次调用产出的内容块」这一段。
    """

    def __init__(self, actor: Actor, session_id: int) -> None:
        self._actor = actor
        self._session_id = session_id
        # turn 级游标:跨 consume 调用累加,保证 index 全 turn 连号。
        self._next_index = 0
        # 以下三项均为单次 consume 的调用内状态,进入 consume 时重置。
        self._ref_to_index: dict[int, int] = {}
        self._open: set[int] = set()
        self._last_result: LLMTurnResult | None = None

    @property
    def last_result(self) -> LLMTurnResult:
        """最近一次 consume 收到的终局结果;尚未消费任何流时抛错。

        返回空结果会被 runtime 读成「无 tool_uses」导致 ReAct 静默截断,故必须抛。
        """
        if self._last_result is None:
            raise AgentException.message("尚未消费任何 LLM 流,无终局结果可读")
        return self._last_result

    async def consume(self, stream: AsyncIterator[LLMStreamEvent]) -> AsyncIterator[StreamEvent]:
        """消费一次 stream() 调用的完整中立事件流,产出协议事件。

        包住整个流而非逐事件 stamp:装配器需要「一次调用」的边界才能把
        block_ref 映射到全局 index,一次 consume 天然就是这个边界。
        流中途抛异常时不吞、不补 block_stop(中断态补收尾会让前端误判块完整),
        原样向上抛,转 error 事件是 runtime 外层的既有职责。
        """
        self._ref_to_index = {}
        self._open = set()
        self._last_result = None

        async for event in stream:
            if event.kind is LLMEventKind.BLOCK_START:
                index = self._next_index
                self._next_index += 1
                if event.block_ref is None or event.block_type is None:
                    raise AgentException.message(
                        "LLM 流的 BLOCK_START 事件缺少 block_ref 或 block_type",
                        {"provider_block_ref": event.block_ref},
                    )
                self._ref_to_index[event.block_ref] = index
                self._open.add(index)
                yield StreamEvent.block_start(
                    self._actor, self._session_id, index, event.block_type
                )
            elif event.kind is LLMEventKind.TEXT_DELTA:
                yield StreamEvent.text_delta(
                    self._actor, self._session_id, self._require_index(event), event.text
                )
            elif event.kind is LLMEventKind.THINKING_DELTA:
                yield StreamEvent.thinking_delta(
                    self._actor, self._session_id, self._require_index(event), event.text
                )
            elif event.kind is LLMEventKind.TOOL_USE:
                index = self._require_index(event)
                if event.tool is None:
                    raise AgentException.message(
                        "LLM 流的 TOOL_USE 事件缺少工具信息",
                        {"block_ref": event.block_ref},
                    )
                yield StreamEvent.tool_use(self._actor, self._session_id, index, event.tool)
                # R4.2 方案 A:tool_use 块同样成对,紧随其后补发 block_stop。
                self._open.discard(index)
                yield StreamEvent.block_stop(self._actor, self._session_id, index)
            elif event.kind is LLMEventKind.BLOCK_STOP:
                index = (
                    self._ref_to_index.get(event.block_ref)
                    if (event.block_ref is not None)
                    else None
                )
                if index is None:
                    # 关一个从没开过的块不损失信息,不值得炸掉整个 turn;
                    # 但照发会让前端收到 index 对不上的孤儿 stop,故丢弃并留日志。
                    logger.warning(
                        "LLM 流出现无映射的 BLOCK_STOP,已丢弃,block_ref=%s",
                        event.block_ref,
                    )
                    continue
                self._open.discard(index)
                yield StreamEvent.block_stop(self._actor, self._session_id, index)
            elif event.kind is LLMEventKind.TURN_RESULT:
                if event.result is None:
                    raise AgentException.message("LLM 流的 TURN_RESULT 事件缺少终局结果")
                self._last_result = event.result
            else:
                # 无静默兜底:未知事件种类说明 provider 实现有错,必须立刻暴露。
                raise AgentException.message(
                    "未知的 LLM 流事件种类",
                    {"kind": str(event.kind)},
                )

        if self._last_result is None:
            raise AgentException.message("LLM 流未产出终局结果")

        # 内容已完整送达,此时抛错等于丢结果;但静默补齐会掩盖 provider 缺陷,留日志。
        if self._open:
            logger.warning(
                "LLM 流结束仍有未关闭内容块,已补发 block_stop,provider 遗留块数=%d",
                len(self._open),
            )
            for index in sorted(self._open):
                yield StreamEvent.block_stop(self._actor, self._session_id, index)
            self._open.clear()

    def _require_index(self, event: LLMStreamEvent) -> int:
        """查 block_ref 的全局 index 映射;无映射时抛错(丢弃等于静默丢内容)。"""
        index = self._ref_to_index.get(event.block_ref) if event.block_ref is not None else None
        if index is None:
            raise AgentException.message(
                "LLM 流事件的 block_ref 无对应的已开启内容块",
                {"kind": event.kind.value, "block_ref": event.block_ref},
            )
        return index
