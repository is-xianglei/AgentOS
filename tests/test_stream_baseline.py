"""固化 AnthropicStreamTranslator 当前行为的基线测试(Phase 0)。

用手写 Anthropic chunk 序列驱动现有翻译器,完整比对产出事件的 to_dict() 序列,
任何意外变化都会在此暴露。其中三点现状是 Phase 1 要主动改掉的语义,
在对应用例中显式断言,改造时差异才可见:
1. block index 每次 API 调用从 0 重来 → 将改为 turn 内全局递增;
2. tool_use 块不发 block_stop → 将改为补发,使 start/stop 成对;
3. tool_use 入参 JSON 非法时兜底成 {} → 将改为抛中文 AgentException。
"""

from typing import Any

from core.events import ORCHESTRATOR_ACTOR, Usage
from llm.types import AnthropicStreamTranslator
from tests.fixtures.anthropic_chunks import (
    CHUNKS_SINGLE_TEXT,
    CHUNKS_TEXT_AND_TOOL_USE,
    CHUNKS_THINKING_TEXT_INTERLEAVED,
    CHUNKS_TOOL_USE_BAD_JSON,
)

_SID = 42
# 基线序列不含 turn_start 事件,actor 一律按精简形态序列化。
_ACTOR = {"role": "orchestrator", "name": "orchestrator"}


def _ev(event_type: str, **payload: Any) -> dict[str, Any]:
    """构造期望事件 dict:公共三字段(type/session_id/actor)+ 各事件专有载荷。"""
    return {"type": event_type, "session_id": _SID, "actor": _ACTOR, **payload}


def _translate_all(
    translator: AnthropicStreamTranslator, chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """整段 chunk 依次喂给翻译器,收集全部产出事件并序列化。"""
    return [event.to_dict() for chunk in chunks for event in translator.translate(chunk)]


def _new_translator() -> AnthropicStreamTranslator:
    return AnthropicStreamTranslator(ORCHESTRATOR_ACTOR, _SID)


# 序列 a 期望:噪声(message_start / message_delta)与 message_final 均不产出事件。
_EXPECTED_SINGLE_TEXT: list[dict[str, Any]] = [
    _ev("block_start", block={"index": 0, "block_type": "text"}),
    _ev("text_delta", block={"index": 0}, text="你好"),
    _ev("text_delta", block={"index": 0}, text="，世"),
    _ev("text_delta", block={"index": 0}, text="界"),
    _ev("block_stop", block={"index": 0}),
]

# 序列 b 期望:tool_use 块的 content_block_stop 只产出 tool_use 事件。
# 【现状】tool_use 块不发 block_stop,即 start/stop 不成对;Phase 1 将改为补发
# block_stop,使所有块类型 start/stop 成对(R4.2 方案 A)。
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


def test_single_text_sequence() -> None:
    """序列 a:单 text 块完整事件序列;message_start/message_delta/message_final 不产出。"""
    events = _translate_all(_new_translator(), CHUNKS_SINGLE_TEXT)
    assert events == _EXPECTED_SINGLE_TEXT


def test_text_and_tool_use_sequence() -> None:
    """序列 b:入参多次 delta 累积后一次性产出;tool_use 块无 block_stop(现状)。"""
    events = _translate_all(_new_translator(), CHUNKS_TEXT_AND_TOOL_USE)
    assert events == _EXPECTED_TEXT_AND_TOOL_USE
    # 显式断言【现状 2】:全序列无任何针对 index=1 的 block_stop。
    # Phase 1 将改为 tool_use 事件后补发 block_stop。
    assert not any(e["type"] == "block_stop" and e["block"]["index"] == 1 for e in events)


def test_thinking_text_interleaved_sequence() -> None:
    """序列 c:thinking/text 交错三块;signature_delta 噪声不产出事件。"""
    events = _translate_all(_new_translator(), CHUNKS_THINKING_TEXT_INTERLEAVED)
    assert events == _EXPECTED_THINKING_TEXT_INTERLEAVED


def test_block_index_resets_per_call() -> None:
    """【现状 1】同一 translator 连续消费两次调用的流,块 index 每次从 0 重来。

    模拟一个 turn 内两次 LLM 调用(tool use 多轮)共用同一 translator 的
    生产用法(runtime/agent.py 跨调用共用)。Phase 1 将改为由装配器分配
    turn 内全局递增 index,第二次调用的块不再从 0 开始。
    """
    translator = _new_translator()
    first = _translate_all(translator, CHUNKS_TEXT_AND_TOOL_USE)
    second = _translate_all(translator, CHUNKS_SINGLE_TEXT)
    # 第一次调用产出 index 0/1 两个块;第二次调用的 text 块 index 又是 0。
    assert first == _EXPECTED_TEXT_AND_TOOL_USE
    assert second == _EXPECTED_SINGLE_TEXT
    assert second[0]["type"] == "block_start"
    assert second[0]["block"]["index"] == 0


def test_tool_use_bad_json_falls_back_to_empty_input() -> None:
    """【现状 3】tool_use 入参 JSON 非法时静默兜底成 {},不抛异常。

    Phase 1 将改为抛中文 AgentException(附工具名与 tool_use_id),
    不再让工具带空参执行。
    """
    events = _translate_all(_new_translator(), CHUNKS_TOOL_USE_BAD_JSON)
    assert events == [
        _ev("block_start", block={"index": 0, "block_type": "tool_use"}),
        _ev(
            "tool_use",
            block={"index": 0},
            tool={"id": "toolu_bad", "name": "file_read", "input": {}},
        ),
    ]


def test_message_final_exposes_usage_and_stop_reason() -> None:
    """message_final 不产出协议事件,但 usage / stop_reason 可从属性读到。"""
    translator = _new_translator()
    events = _translate_all(translator, CHUNKS_SINGLE_TEXT)
    # message_final 自身零事件:全部事件均来自内容块。
    assert all(e["type"] in {"block_start", "text_delta", "block_stop"} for e in events)
    assert translator.stop_reason == "end_turn"
    assert translator.last_usage == Usage(
        input_tokens=120,
        output_tokens=7,
        cache_creation_input_tokens=30,
        cache_read_input_tokens=90,
    )
