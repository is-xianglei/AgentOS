"""OpenAI 流式适配器(_OpenAIChunkAdapter)的行为测试(阶段 4 新增)。

驱动方式对标 test_stream_baseline.py:手写裸 chunk 经适配器出中立事件,
再经 TurnEventAssembler 出协议事件,不建真实 SDK 连接。覆盖 AC2(坏 JSON
抛中文异常)与 AC3(usage / stop_reason / tool_uses 正确填充),另固化
block_ref 分配规则(文本 0 / 工具 1+i)与「工具块不发 BLOCK_STOP、由装配器
补发」的分层行为,以及与 Anthropic 侧的协议事件逐字段等价(AC1)。
"""

from typing import Any

import pytest

from core.errors import AgentException
from core.events import ORCHESTRATOR_ACTOR, BlockType, Usage
from llm.anthropic_client import _AnthropicChunkAdapter
from llm.openai_client import _OpenAIChunkAdapter
from llm.types import LLMEventKind, LLMStreamEvent
from runtime.stream_assembler import TurnEventAssembler
from tests.fixtures import anthropic_chunks
from tests.fixtures.openai_chunks import (
    CHUNKS_MULTI_TOOL_USE,
    CHUNKS_SINGLE_TEXT,
    CHUNKS_TEXT_AND_TOOL_USE,
    CHUNKS_TEXT_NO_USAGE,
    CHUNKS_TEXT_TRUNCATED,
    CHUNKS_TOOL_USE_BAD_JSON,
)

pytestmark = pytest.mark.anyio

_SID = 42
_ACTOR = {"role": "orchestrator", "name": "orchestrator"}


def _ev(event_type: str, **payload: Any) -> dict[str, Any]:
    """构造期望协议事件 dict:公共三字段 + 各事件专有载荷。"""
    return {"type": event_type, "session_id": _SID, "actor": _ACTOR, **payload}


def _adapt_all(chunks: list[dict[str, Any]]) -> list[LLMStreamEvent]:
    """整段 chunk 序列过适配器,返回中立事件列表(含 finalize 产物)。"""
    adapter = _OpenAIChunkAdapter()
    events: list[LLMStreamEvent] = []
    for chunk in chunks:
        events.extend(adapter.adapt(chunk))
    events.extend(adapter.finalize())
    return events


async def _run_call(
    assembler: TurnEventAssembler, chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """模拟一次 stream() 调用:chunk 经适配器出中立事件,再经装配器出协议事件。"""
    adapter = _OpenAIChunkAdapter()

    async def _neutral_stream():
        for chunk in chunks:
            for event in adapter.adapt(chunk):
                yield event
        for event in adapter.finalize():
            yield event

    return [ev.to_dict() async for ev in assembler.consume(_neutral_stream())]


def _new_assembler() -> TurnEventAssembler:
    return TurnEventAssembler(ORCHESTRATOR_ACTOR, _SID, turn_id=None)


# ---- 协议事件序列(经装配器) ----


async def test_single_text_sequence() -> None:
    """序列 a:纯文本完整协议事件;role-only 首 chunk 与末 usage chunk 为噪声。"""
    events = await _run_call(_new_assembler(), CHUNKS_SINGLE_TEXT)
    assert events == [
        _ev("block_start", block={"index": 0, "block_type": "text"}),
        _ev("text_delta", block={"index": 0}, text="你好"),
        _ev("text_delta", block={"index": 0}, text="，世"),
        _ev("text_delta", block={"index": 0}, text="界"),
        _ev("block_stop", block={"index": 0}),
    ]


async def test_text_and_tool_use_sequence() -> None:
    """序列 b:入参多次增量累积后收束时一次性产出;tool_use 块由装配器补发 block_stop。"""
    events = await _run_call(_new_assembler(), CHUNKS_TEXT_AND_TOOL_USE)
    assert events == [
        _ev("block_start", block={"index": 0, "block_type": "text"}),
        _ev("text_delta", block={"index": 0}, text="我来读取文件"),
        _ev("block_start", block={"index": 1, "block_type": "tool_use"}),
        _ev("block_stop", block={"index": 0}),
        _ev(
            "tool_use",
            block={"index": 1},
            tool={"id": "call_01", "name": "file_read", "input": {"path": "a.txt"}},
        ),
        _ev("block_stop", block={"index": 1}),
    ]


async def test_openai_anthropic_protocol_equivalence() -> None:
    """AC1:同语义输入(文本+工具调用)经两家适配器装配后协议事件逐字段等价。

    OpenAI 无显式 block_stop,文本块的 stop 在收束点才补发,与 Anthropic 的
    「text stop 先于 tool block start」顺序不同;等价断言按事件集合与
    块级配对语义(每块 start/stop 成对、tool_use 载荷一致)进行,
    并归一两家不同的工具调用 id 前缀。
    """
    openai_events = await _run_call(_new_assembler(), CHUNKS_TEXT_AND_TOOL_USE)
    anthropic_events = await _run_call_anthropic(
        _new_assembler(), anthropic_chunks.CHUNKS_TEXT_AND_TOOL_USE
    )

    def _normalize(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for event in events:
            copied = dict(event)
            if "tool" in copied:
                copied["tool"] = {**copied["tool"], "id": "<id>"}
            normalized.append(copied)
        return normalized

    left, right = _normalize(openai_events), _normalize(anthropic_events)
    # 两家产出的协议事件集合一致(载荷逐字段等价,仅顺序允许差异)。
    assert sorted(left, key=str) == sorted(right, key=str)
    # 每块 start / stop 严格成对。
    for events in (openai_events, anthropic_events):
        starts = [e["block"]["index"] for e in events if e["type"] == "block_start"]
        stops = [e["block"]["index"] for e in events if e["type"] == "block_stop"]
        assert sorted(starts) == sorted(stops) == [0, 1]


async def _run_call_anthropic(
    assembler: TurnEventAssembler, chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Anthropic 侧对照组:同 test_stream_baseline 的驱动方式。"""
    adapter = _AnthropicChunkAdapter()

    async def _neutral_stream():
        for chunk in chunks:
            for event in adapter.adapt(chunk):
                yield event

    return [ev.to_dict() async for ev in assembler.consume(_neutral_stream())]


# ---- block_ref 分配与分层行为(中立事件层) ----


def test_block_ref_allocation_text_zero_tools_offset() -> None:
    """block_ref 自建分配:文本块恒占 0,工具块占 1 + tool_call.index。"""
    events = _adapt_all(CHUNKS_TEXT_AND_TOOL_USE)
    starts = [e for e in events if e.kind is LLMEventKind.BLOCK_START]
    assert [(e.block_ref, e.block_type) for e in starts] == [
        (0, BlockType.TEXT),
        (1, BlockType.TOOL_USE),
    ]


def test_multi_tool_refs_and_payloads() -> None:
    """序列 c:多工具并发,ref 按 1+index 分配,入参交错累积互不串块。"""
    events = _adapt_all(CHUNKS_MULTI_TOOL_USE)
    starts = [e for e in events if e.kind is LLMEventKind.BLOCK_START]
    assert [(e.block_ref, e.block_type) for e in starts] == [
        (1, BlockType.TOOL_USE),
        (2, BlockType.TOOL_USE),
    ]
    tool_events = [e for e in events if e.kind is LLMEventKind.TOOL_USE]
    assert [(e.block_ref, e.tool.id, e.tool.name, e.tool.input) for e in tool_events] == [
        (1, "call_a", "file_read", {"path": "a.txt"}),
        (2, "call_b", "file_write", {"path": "b.txt", "content": "x"}),
    ]


def test_tool_blocks_emit_no_block_stop() -> None:
    """工具块不发 BLOCK_STOP(装配器统一补发);文本块在收束点补发。"""
    events = _adapt_all(CHUNKS_TEXT_AND_TOOL_USE)
    stops = [e for e in events if e.kind is LLMEventKind.BLOCK_STOP]
    assert [e.block_ref for e in stops] == [0]
    multi_tool_events = _adapt_all(CHUNKS_MULTI_TOOL_USE)
    assert not [e for e in multi_tool_events if e.kind is LLMEventKind.BLOCK_STOP]


def test_noise_chunks_emit_no_events() -> None:
    """噪声(role-only 首 chunk、空 delta、choices 为空)不产出任何事件。"""
    adapter = _OpenAIChunkAdapter()
    noise: list[dict[str, Any]] = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]},
        {"choices": [{"index": 0, "delta": {}}]},
        {"choices": []},
    ]
    for chunk in noise:
        assert adapter.adapt(chunk) == []


def test_no_thinking_delta_emitted() -> None:
    """THINKING_DELTA 恒不产出:ChoiceDelta 无 reasoning 字段,属协议事实。"""
    for chunks in (CHUNKS_SINGLE_TEXT, CHUNKS_TEXT_AND_TOOL_USE, CHUNKS_MULTI_TOOL_USE):
        events = _adapt_all(chunks)
        assert not [e for e in events if e.kind is LLMEventKind.THINKING_DELTA]


# ---- 终局:usage / stop_reason / tool_uses(AC3) ----


def test_turn_result_usage_and_stop_reason() -> None:
    """AC3:TURN_RESULT 携带非空 usage(含缓存读取映射)与 stop_reason。"""
    events = _adapt_all(CHUNKS_SINGLE_TEXT)
    results = [e for e in events if e.kind is LLMEventKind.TURN_RESULT]
    assert len(results) == 1
    result = results[0].result
    assert result is not None
    assert result.text == "你好，世界"
    assert result.stop_reason == "stop"
    assert result.usage == Usage(
        input_tokens=120,
        output_tokens=7,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=90,
    )
    assert result.tool_uses == []


def test_turn_result_carries_tool_uses() -> None:
    """AC3:tool_uses 由 TURN_RESULT 带出,stop_reason 原样为 tool_calls 不归一。"""
    events = _adapt_all(CHUNKS_TEXT_AND_TOOL_USE)
    result = events[-1].result
    assert result is not None
    assert result.stop_reason == "tool_calls"
    assert len(result.tool_uses) == 1
    tool_use = result.tool_uses[0]
    assert (tool_use.id, tool_use.name, tool_use.input) == (
        "call_01",
        "file_read",
        {"path": "a.txt"},
    )
    assert result.usage == Usage(
        input_tokens=200,
        output_tokens=45,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=180,
    )


def test_truncated_text_preserved_with_length_stop_reason() -> None:
    """finish_reason=length 时已生成文本保留,stop_reason 原样带出(不走 SDK 终局)。"""
    events = _adapt_all(CHUNKS_TEXT_TRUNCATED)
    result = events[-1].result
    assert result is not None
    assert result.text == "结论是…"
    assert result.stop_reason == "length"
    assert result.usage == Usage(input_tokens=50, output_tokens=8)


def test_missing_usage_yields_none() -> None:
    """网关未回 usage 时 TURN_RESULT.usage 为 None,不伪造零值。"""
    events = _adapt_all(CHUNKS_TEXT_NO_USAGE)
    result = events[-1].result
    assert result is not None
    assert result.usage is None
    assert result.stop_reason == "stop"


def test_stream_interrupted_without_finish_reason() -> None:
    """流意外中断(无 finish_reason):finalize 不产出 TURN_RESULT,由装配器抛错。"""
    adapter = _OpenAIChunkAdapter()
    adapter.adapt({"choices": [{"index": 0, "delta": {"content": "半截"}}]})
    assert adapter.finalize() == []


# ---- 错误矩阵(AC2) ----


async def test_tool_use_bad_json_raises() -> None:
    """AC2:工具入参 JSON 非法时抛中文 AgentException,details 带工具名与调用 id。"""
    with pytest.raises(AgentException, match="JSON 解析失败") as exc_info:
        await _run_call(_new_assembler(), CHUNKS_TOOL_USE_BAD_JSON)
    assert exc_info.value.details == {"tool_name": "file_read", "tool_call_id": "call_bad"}


def test_tool_call_missing_id_or_name_raises() -> None:
    """tool_call 首个增量缺 id 或 name 一律抛中文异常,无静默兜底。"""
    missing_id = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"name": "file_read", "arguments": "{}"}}
                    ]
                },
            }
        ]
    }
    with pytest.raises(AgentException, match="缺少 id 或 name"):
        _OpenAIChunkAdapter().adapt(missing_id)

    missing_name = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [{"index": 0, "id": "call_x", "function": {"arguments": "{}"}}]
                },
            }
        ]
    }
    with pytest.raises(AgentException, match="缺少 id 或 name"):
        _OpenAIChunkAdapter().adapt(missing_name)


def test_content_filter_finish_reason_passes_through() -> None:
    """finish_reason=content_filter 正常产出 TURN_RESULT,由上层判读。"""
    chunks: list[dict[str, Any]] = [
        {"choices": [{"index": 0, "delta": {"content": "部分内容"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]},
    ]
    events = _adapt_all(chunks)
    result = events[-1].result
    assert result is not None
    assert result.stop_reason == "content_filter"
    assert result.text == "部分内容"


# ---- 分层收口 ----


def test_adapter_emits_neutral_events_without_runtime_concepts() -> None:
    """适配器产出的中立事件不含 actor / session_id / 全局 index(分层收口)。"""
    events = _adapt_all(CHUNKS_TEXT_AND_TOOL_USE)
    assert events, "适配器应产出中立事件"
    for event in events:
        assert not hasattr(event, "actor")
        assert not hasattr(event, "session_id")


