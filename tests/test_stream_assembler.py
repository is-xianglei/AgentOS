"""TurnEventAssembler 单元测试(Phase 1)。

用手写 LLMStreamEvent 序列驱动,不碰网络。覆盖 design.md 第十一节边界情况表
的全部装配器职责项;表中「tool_use 入参 JSON 解析失败」一项属 Anthropic
适配器职责,由 test_stream_baseline.py 覆盖。
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from core.errors import AgentException
from core.events import ORCHESTRATOR_ACTOR, BlockType, ToolInfo, Usage, subagent_actor
from llm.types import LLMEventKind, LLMStreamEvent, LLMTurnResult
from runtime.stream_assembler import TurnEventAssembler

pytestmark = pytest.mark.anyio

_SID = 42
_ACTOR = {"role": "orchestrator", "name": "orchestrator"}


def _block_start(ref: int, block_type: BlockType = BlockType.TEXT) -> LLMStreamEvent:
    return LLMStreamEvent(kind=LLMEventKind.BLOCK_START, block_ref=ref, block_type=block_type)


def _text(ref: int, text: str) -> LLMStreamEvent:
    return LLMStreamEvent(kind=LLMEventKind.TEXT_DELTA, block_ref=ref, text=text)


def _thinking(ref: int, text: str) -> LLMStreamEvent:
    return LLMStreamEvent(kind=LLMEventKind.THINKING_DELTA, block_ref=ref, text=text)


def _tool_use(ref: int, tool: ToolInfo) -> LLMStreamEvent:
    return LLMStreamEvent(kind=LLMEventKind.TOOL_USE, block_ref=ref, tool=tool)


def _block_stop(ref: int) -> LLMStreamEvent:
    return LLMStreamEvent(kind=LLMEventKind.BLOCK_STOP, block_ref=ref)


def _turn_result(**kwargs: Any) -> LLMStreamEvent:
    return LLMStreamEvent(kind=LLMEventKind.TURN_RESULT, result=LLMTurnResult(**kwargs))


_TOOL = ToolInfo(id="toolu_01", name="file_read", input={"path": "a.txt"})


async def _feed(events: list[LLMStreamEvent]) -> AsyncIterator[LLMStreamEvent]:
    """把手写事件列表包装成异步流。"""
    for event in events:
        yield event


async def _collect(
    assembler: TurnEventAssembler, events: list[LLMStreamEvent]
) -> list[dict[str, Any]]:
    """经装配器消费一段流,收集协议事件并序列化。"""
    return [ev.to_dict() async for ev in assembler.consume(_feed(events))]


def _ev(event_type: str, **payload: Any) -> dict[str, Any]:
    """构造期望协议事件 dict(actor 一律精简形态,序列不含 turn_start)。"""
    return {"type": event_type, "session_id": _SID, "actor": _ACTOR, **payload}


def _new_assembler() -> TurnEventAssembler:
    return TurnEventAssembler(ORCHESTRATOR_ACTOR, _SID, turn_id=None)


async def test_text_block_roundtrip() -> None:
    """规则 1/2/4/5:start→delta→stop 正常装配,TURN_RESULT 不产出协议事件。"""
    assembler = _new_assembler()
    events = await _collect(
        assembler,
        [
            _block_start(0),
            _text(0, "你好"),
            _text(0, "世界"),
            _block_stop(0),
            _turn_result(text="你好世界", stop_reason="end_turn"),
        ],
    )
    assert events == [
        _ev("block_start", block={"index": 0, "block_type": "text"}),
        _ev("text_delta", block={"index": 0}, text="你好"),
        _ev("text_delta", block={"index": 0}, text="世界"),
        _ev("block_stop", block={"index": 0}),
    ]
    assert assembler.last_result.text == "你好世界"
    assert assembler.last_result.stop_reason == "end_turn"


async def test_tool_use_block_gets_paired_block_stop() -> None:
    """规则 3(R4.2 方案 A):TOOL_USE 事件后由装配器补发 block_stop,块成对。"""
    assembler = _new_assembler()
    events = await _collect(
        assembler,
        [
            _block_start(0, BlockType.TOOL_USE),
            _tool_use(0, _TOOL),
            _turn_result(stop_reason="tool_use"),
        ],
    )
    assert events == [
        _ev("block_start", block={"index": 0, "block_type": "tool_use"}),
        _ev(
            "tool_use",
            block={"index": 0},
            tool={"id": "toolu_01", "name": "file_read", "input": {"path": "a.txt"}},
        ),
        _ev("block_stop", block={"index": 0}),
    ]


async def test_index_increases_across_consume_calls() -> None:
    """R4.1:turn 内多次 consume(多轮 tool use)时 index 全局递增不重复。"""
    assembler = _new_assembler()
    first = await _collect(
        assembler,
        [
            _block_start(0),
            _text(0, "调用工具"),
            _block_stop(0),
            _block_start(1, BlockType.TOOL_USE),
            _tool_use(1, _TOOL),
            _turn_result(stop_reason="tool_use"),
        ],
    )
    # 第二次调用 provider 的 block_ref 又从 0 开始,但全局 index 接着连号。
    second = await _collect(
        assembler,
        [
            _block_start(0),
            _text(0, "最终答复"),
            _block_stop(0),
            _turn_result(text="最终答复", stop_reason="end_turn"),
        ],
    )
    first_indexes = [e["block"]["index"] for e in first if e["type"] == "block_start"]
    second_indexes = [e["block"]["index"] for e in second if e["type"] == "block_start"]
    assert first_indexes == [0, 1]
    assert second_indexes == [2]
    assert len(set(first_indexes + second_indexes)) == 3
    # 第二次的 last_result 覆写第一次的。
    assert assembler.last_result.text == "最终答复"


async def test_missing_turn_result_raises() -> None:
    """规则 7:流结束仍未见 TURN_RESULT,抛中文 AgentException。"""
    assembler = _new_assembler()
    with pytest.raises(AgentException, match="未产出终局结果"):
        await _collect(assembler, [_block_start(0), _text(0, "残流"), _block_stop(0)])


async def test_empty_stream_raises() -> None:
    """规则 7 边界:空流同样视为缺 TURN_RESULT。"""
    assembler = _new_assembler()
    with pytest.raises(AgentException, match="未产出终局结果"):
        await _collect(assembler, [])


async def test_last_result_before_consume_raises() -> None:
    """未 consume 就读 last_result 抛错,不返回空结果(避免静默截断 ReAct)。"""
    assembler = _new_assembler()
    with pytest.raises(AgentException, match="尚未消费任何 LLM 流"):
        _ = assembler.last_result


async def test_unmapped_delta_raises() -> None:
    """规则 8 前半:delta / tool_use 的 block_ref 无映射时抛错(丢弃等于丢内容)。"""
    for orphan in (_text(9, "孤儿"), _thinking(9, "孤儿"), _tool_use(9, _TOOL)):
        assembler = _new_assembler()
        with pytest.raises(AgentException, match="无对应的已开启内容块"):
            await _collect(assembler, [orphan, _turn_result()])


async def test_unmapped_block_stop_is_dropped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """规则 8 后半:BLOCK_STOP 无映射时 warning 后丢弃,不炸 turn、不产出孤儿 stop。"""
    assembler = _new_assembler()
    with caplog.at_level(logging.WARNING, logger="runtime.stream_assembler"):
        events = await _collect(
            assembler,
            [
                _block_start(0),
                _text(0, "正文"),
                _block_stop(7),
                _block_stop(0),
                _turn_result(text="正文"),
            ],
        )
    assert events == [
        _ev("block_start", block={"index": 0, "block_type": "text"}),
        _ev("text_delta", block={"index": 0}, text="正文"),
        _ev("block_stop", block={"index": 0}),
    ]
    assert any("无映射的 BLOCK_STOP" in r.message for r in caplog.records)


async def test_dangling_open_blocks_closed_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """规则 6:流正常结束但仍有未关闭块,按开启顺序补发 block_stop 并记 warning。"""
    assembler = _new_assembler()
    with caplog.at_level(logging.WARNING, logger="runtime.stream_assembler"):
        events = await _collect(
            assembler,
            [
                _block_start(0),
                _text(0, "没等到 stop"),
                _block_start(1, BlockType.THINKING),
                _turn_result(text="没等到 stop"),
            ],
        )
    assert [e["type"] for e in events] == [
        "block_start",
        "text_delta",
        "block_start",
        "block_stop",
        "block_stop",
    ]
    # 按开启顺序补发:先 0 后 1。
    assert [e["block"]["index"] for e in events[-2:]] == [0, 1]
    assert any("未关闭内容块" in r.message for r in caplog.records)


async def test_stream_exception_propagates_without_padding() -> None:
    """流中途抛异常:装配器不吞、不补 block_stop,原样向上抛。"""

    class _Boom(RuntimeError):
        pass

    async def _broken() -> AsyncIterator[LLMStreamEvent]:
        yield _block_start(0)
        yield _text(0, "半截")
        raise _Boom("连接断了")

    assembler = _new_assembler()
    got: list[dict[str, Any]] = []
    with pytest.raises(_Boom):
        async for ev in assembler.consume(_broken()):
            got.append(ev.to_dict())
    # 只收到中断前的事件,无补发的 block_stop。
    assert [e["type"] for e in got] == ["block_start", "text_delta"]


async def test_turn_result_without_payload_raises() -> None:
    """TURN_RESULT 事件缺 result 载荷属 provider 实现错误,立即抛错。"""
    assembler = _new_assembler()
    bad = LLMStreamEvent(kind=LLMEventKind.TURN_RESULT, result=None)
    with pytest.raises(AgentException, match="缺少终局结果"):
        await _collect(assembler, [bad])


async def test_concurrent_actors_have_independent_indexes() -> None:
    """多产出者并发:各持独立装配器,index 唯一性域是 (actor, turn),各自从 0 起。"""
    main = TurnEventAssembler(ORCHESTRATOR_ACTOR, _SID, turn_id=None)
    sub = TurnEventAssembler(subagent_actor("explore", run_id="run-1"), _SID, turn_id=None)
    seq = [
        _block_start(0),
        _text(0, "各自独立"),
        _block_stop(0),
        _turn_result(text="各自独立"),
    ]
    main_events = await _collect(main, seq)
    sub_events = await _collect(sub, seq)
    assert main_events[0]["block"]["index"] == 0
    assert sub_events[0]["block"]["index"] == 0
    assert sub_events[0]["actor"] == {"role": "subagent", "name": "explore", "run_id": "run-1"}


async def test_usage_carried_in_turn_result() -> None:
    """usage / stop_reason 经 LLMTurnResult 透出,供 runtime 构造 turn_end。"""
    assembler = _new_assembler()
    usage = Usage(input_tokens=120, output_tokens=7)
    await _collect(
        assembler,
        [
            _block_start(0),
            _text(0, "完"),
            _block_stop(0),
            _turn_result(text="完", stop_reason="end_turn", usage=usage),
        ],
    )
    assert assembler.last_result.usage == usage
