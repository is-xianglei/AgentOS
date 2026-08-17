"""固化「Anthropic 适配器 + 装配器」组合行为的基线测试(Phase 1 迁移版)。

验证意图与 Phase 0 一致:同样的 Anthropic chunk 输入 → 什么样的协议事件输出。
驱动对象由旧 AnthropicStreamTranslator 换成 _AnthropicChunkAdapter +
TurnEventAssembler 组合。相对 Phase 0 基线,仅以下三点行为是主动变更:
1. block index 由「每次 API 调用从 0 重来」改为 turn 内全局递增(R4.1);
2. tool_use 块补发 block_stop,所有块 start/stop 成对(R4.2 方案 A);
3. tool_use 入参 JSON 非法由「兜底 {}」改为抛中文 AgentException(消除静默兜底)。
"""

from typing import Any

import pytest

from core.errors import AgentException
from core.events import ORCHESTRATOR_ACTOR, Usage
from llm.anthropic_client import _AnthropicChunkAdapter
from llm.types import LLMStreamEvent
from runtime.stream_assembler import TurnEventAssembler
from tests.fixtures.anthropic_chunks import (
    CHUNKS_SINGLE_TEXT,
    CHUNKS_TEXT_AND_TOOL_USE,
    CHUNKS_THINKING_TEXT_INTERLEAVED,
    CHUNKS_TOOL_USE_BAD_JSON,
)

pytestmark = pytest.mark.anyio

_SID = 42
# 基线序列不含 turn_start 事件,actor 一律按精简形态序列化。
_ACTOR = {"role": "orchestrator", "name": "orchestrator"}


def _ev(event_type: str, **payload: Any) -> dict[str, Any]:
    """构造期望事件 dict:公共三字段(type/session_id/actor)+ 各事件专有载荷。"""
    return {"type": event_type, "session_id": _SID, "actor": _ACTOR, **payload}


async def _run_call(
    assembler: TurnEventAssembler, chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """模拟一次 stream() 调用:chunk 经适配器出中立事件,再经装配器出协议事件。

    与生产链路一致:适配器每次调用新建(生命周期为单次调用),
    装配器由调用方传入(生命周期为整个 turn)。
    """
    adapter = _AnthropicChunkAdapter()

    async def _neutral_stream():
        for chunk in chunks:
            for event in adapter.adapt(chunk):
                yield event

    return [ev.to_dict() async for ev in assembler.consume(_neutral_stream())]


def _new_assembler() -> TurnEventAssembler:
    return TurnEventAssembler(ORCHESTRATOR_ACTOR, _SID)


# 序列 a 期望:噪声(message_start / message_delta)与 TURN_RESULT 均不产出协议事件。
_EXPECTED_SINGLE_TEXT: list[dict[str, Any]] = [
    _ev("block_start", block={"index": 0, "block_type": "text"}),
    _ev("text_delta", block={"index": 0}, text="你好"),
    _ev("text_delta", block={"index": 0}, text="，世"),
    _ev("text_delta", block={"index": 0}, text="界"),
    _ev("block_stop", block={"index": 0}),
]

# 序列 b 期望:tool_use 事件之后由装配器补发 block_stop(【变更 2】R4.2 方案 A)。
# Phase 0 现状是 tool_use 块不发 block_stop、start/stop 不成对;改后所有块统一
# 「见 block_stop 即收尾」,前端无需为 tool_use 写特例。
_EXPECTED_TEXT_AND_TOOL_USE: list[dict[str, Any]] = [
    _ev("block_start", block={"index": 0, "block_type": "text"}),
    _ev("text_delta", block={"index": 0}, text="我来读取文件"),
    _ev("block_stop", block={"index": 0}),
    _ev("block_start", block={"index": 1, "block_type": "tool_use"}),
    # 3 次 input_json_delta 只累积不产出;stop 时入参一次性拼齐。
    _ev(
        "tool_use",
        block={"index": 1},
        tool={"id": "toolu_01", "name": "file_read", "input": {"path": "a.txt"}},
    ),
    _ev("block_stop", block={"index": 1}),
]

# 序列 c 期望:thinking / text 交错,signature_delta 为噪声不产出。
_EXPECTED_THINKING_TEXT_INTERLEAVED: list[dict[str, Any]] = [
    _ev("block_start", block={"index": 0, "block_type": "thinking"}),
    _ev("thinking_delta", block={"index": 0}, text="先分析需求"),
    _ev("block_stop", block={"index": 0}),
    _ev("block_start", block={"index": 1, "block_type": "text"}),
    _ev("text_delta", block={"index": 1}, text="初步结论"),
    _ev("block_stop", block={"index": 1}),
    _ev("block_start", block={"index": 2, "block_type": "thinking"}),
    _ev("thinking_delta", block={"index": 2}, text="再核对边界"),
    _ev("block_stop", block={"index": 2}),
]


async def test_single_text_sequence() -> None:
    """序列 a:单 text 块完整事件序列;message_start/message_delta/TURN_RESULT 不产出。"""
    events = await _run_call(_new_assembler(), CHUNKS_SINGLE_TEXT)
    assert events == _EXPECTED_SINGLE_TEXT


async def test_text_and_tool_use_sequence() -> None:
    """序列 b:入参多次 delta 累积后一次性产出;tool_use 块补发 block_stop(变更 2)。"""
    events = await _run_call(_new_assembler(), CHUNKS_TEXT_AND_TOOL_USE)
    assert events == _EXPECTED_TEXT_AND_TOOL_USE
    # 显式断言【变更 2】:tool_use 块(index=1)现在有配对的 block_stop。
    assert any(e["type"] == "block_stop" and e["block"]["index"] == 1 for e in events)


async def test_thinking_text_interleaved_sequence() -> None:
    """序列 c:thinking/text 交错三块;signature_delta 噪声不产出事件。"""
    events = await _run_call(_new_assembler(), CHUNKS_THINKING_TEXT_INTERLEAVED)
    assert events == _EXPECTED_THINKING_TEXT_INTERLEAVED


async def test_block_index_increases_across_calls() -> None:
    """【变更 1】同一装配器跨两次调用消费流,块 index 在 turn 内全局递增不重复。

    Phase 0 现状是翻译器透传 Anthropic 原始 index、每次调用从 0 重来,
    一个 turn 内 tool use 多轮时消费方会重复收到 index=0,拿 index 当 key 会串块。
    改后 index 由装配器统一分配:第一次调用产出 0/1,第二次调用接着从 2 起。
    """
    assembler = _new_assembler()
    first = await _run_call(assembler, CHUNKS_TEXT_AND_TOOL_USE)
    second = await _run_call(assembler, CHUNKS_SINGLE_TEXT)
    assert first == _EXPECTED_TEXT_AND_TOOL_USE
    # 第二次调用的 text 块 index 接续为 2,不再从 0 重来。
    assert second[0]["type"] == "block_start"
    assert second[0]["block"]["index"] == 2
    # 两次调用的全部 block_start index 全局唯一。
    all_indexes = [e["block"]["index"] for e in first + second if e["type"] == "block_start"]
    assert all_indexes == [0, 1, 2]
    assert len(all_indexes) == len(set(all_indexes))


async def test_tool_use_bad_json_raises() -> None:
    """【变更 3】tool_use 入参 JSON 非法时抛中文 AgentException,附工具名与 id。

    Phase 0 现状是静默兜底成 {},工具随后带空参执行,失败原因在链路上彻底消失,
    违反「禁止静默兜底」约定,不原样搬入新代码。
    """
    with pytest.raises(AgentException, match="JSON 解析失败") as exc_info:
        await _run_call(_new_assembler(), CHUNKS_TOOL_USE_BAD_JSON)
    assert exc_info.value.details == {"tool_name": "file_read", "tool_use_id": "toolu_bad"}


async def test_turn_result_exposes_usage_and_stop_reason() -> None:
    """message_final 转为 TURN_RESULT:不产出协议事件,终局经 last_result 读取。"""
    assembler = _new_assembler()
    events = await _run_call(assembler, CHUNKS_SINGLE_TEXT)
    # TURN_RESULT 自身零协议事件:全部事件均来自内容块。
    assert all(e["type"] in {"block_start", "text_delta", "block_stop"} for e in events)
    result = assembler.last_result
    assert result.stop_reason == "end_turn"
    assert result.usage == Usage(
        input_tokens=120,
        output_tokens=7,
        cache_creation_input_tokens=30,
        cache_read_input_tokens=90,
    )
    # 终局解析收归客户端:text 与 provider_content 由 TURN_RESULT 带出。
    assert result.text == "你好，世界"
    assert result.provider_content == [{"type": "text", "text": "你好，世界"}]
    assert result.tool_uses == []


async def test_turn_result_carries_tool_uses() -> None:
    """runtime 不再自行解析 message_final:tool_uses 由 TURN_RESULT 带出。"""
    assembler = _new_assembler()
    await _run_call(assembler, CHUNKS_TEXT_AND_TOOL_USE)
    result = assembler.last_result
    assert len(result.tool_uses) == 1
    tool_use = result.tool_uses[0]
    assert tool_use.id == "toolu_01"
    assert tool_use.name == "file_read"
    assert tool_use.input == {"path": "a.txt"}
    assert result.stop_reason == "tool_use"


def test_adapter_emits_neutral_events_without_runtime_concepts() -> None:
    """适配器产出的中立事件不含 actor / session_id / 全局 index(分层收口)。"""
    adapter = _AnthropicChunkAdapter()
    events: list[LLMStreamEvent] = []
    for chunk in CHUNKS_TEXT_AND_TOOL_USE:
        events.extend(adapter.adapt(chunk))
    assert events, "适配器应产出中立事件"
    for event in events:
        assert not hasattr(event, "actor")
        assert not hasattr(event, "session_id")
