"""思考块进中立协议与落库的行为测试。

覆盖两家 provider 的完整往返:provider 流 → 中立终局段 → 落库中立块 →
出站 wire。全程手写 chunk 与中立消息,不建真实 SDK 连接。

核心不变式:思考块保序且签名不串位。Anthropic 要求带签名的思考块原样回传,
不得修改、重排或过滤,同一回合的多段思考签名互不相同——合并或重排即校验失败。
"""

from typing import Any

import pytest
from anthropic import NOT_GIVEN

from core.errors import AgentException
from core.events import BlockType
from llm import anthropic_client as anthropic_module
from llm import openai_client as openai_module
from llm.anthropic_client import AnthropicClient, _AnthropicChunkAdapter, _thinking_param
from llm.openai_client import OpenAIClient, _OpenAIChunkAdapter
from llm.types import (
    LLMEventKind,
    LLMTurnResult,
    ThinkingSegment,
    build_assistant_blocks,
    parse_assistant_blocks,
)
from tests.fixtures.anthropic_chunks import (
    CHUNKS_REDACTED_THINKING,
    CHUNKS_THINKING_TEXT_INTERLEAVED,
)

_ANTHROPIC = AnthropicClient(model="claude-x", api_key="k", base_url="https://example.test")
_OPENAI = OpenAIClient(model="deepseek-x", api_key="k", base_url="https://example.test/v1")


def _final_result(adapter: Any, chunks: list[dict[str, Any]]) -> LLMTurnResult:
    """喂完 chunk 序列,取出终局 TURN_RESULT 的 result。"""
    events = [event for chunk in chunks for event in adapter.adapt(chunk)]
    if hasattr(adapter, "finalize"):
        events.extend(adapter.finalize())
    results = [e.result for e in events if e.kind is LLMEventKind.TURN_RESULT]
    assert len(results) == 1, "每次调用必须以恰好一个 TURN_RESULT 收尾"
    return results[0]


# ---- Anthropic 入站:终局思考段(AC2、AC6) ----


def test_anthropic_terminal_thinking_ordered_with_signatures() -> None:
    """AC2:两段思考按下发序带出,签名各归各位不串号。"""
    result = _final_result(_AnthropicChunkAdapter(), CHUNKS_THINKING_TEXT_INTERLEAVED)

    assert result.thinking == [
        ThinkingSegment(text="先分析需求", signature="sig_abc"),
        ThinkingSegment(text="再核对边界", signature="sig_def"),
    ]
    # 思考不混进正文:text 只含 text 块内容。
    assert result.text == "初步结论"


def test_anthropic_redacted_thinking_extracted() -> None:
    """AC6:redacted_thinking 进终局段,加密载荷落在 redacted_data。"""
    result = _final_result(_AnthropicChunkAdapter(), CHUNKS_REDACTED_THINKING)

    assert result.thinking == [ThinkingSegment(redacted_data="encrypted_payload_xyz")]
    assert result.text == "换个说法回答"


def test_anthropic_redacted_block_announced_as_thinking() -> None:
    """F10:redacted_thinking 的 BLOCK_START 必须报 THINKING 而非兜底成 TEXT。"""
    adapter = _AnthropicChunkAdapter()
    events = adapter.adapt(CHUNKS_REDACTED_THINKING[0])

    assert [(e.kind, e.block_type) for e in events] == [
        (LLMEventKind.BLOCK_START, BlockType.THINKING)
    ]


def test_anthropic_unknown_block_type_raises() -> None:
    """未知块类型抛中文异常,不静默兜底成文本块。"""
    adapter = _AnthropicChunkAdapter()
    chunk = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "server_tool_use"},
    }

    with pytest.raises(AgentException) as excinfo:
        adapter.adapt(chunk)

    assert "未知的 Anthropic 内容块类型" in str(excinfo.value)


# ---- 落库形状:构造与解析(AC2 保序) ----


def test_build_blocks_puts_thinking_first_in_order() -> None:
    """AC2:思考块排在最前且保持原序,文本块随后。"""
    blocks = build_assistant_blocks(
        "初步结论",
        [],
        [
            ThinkingSegment(text="先分析需求", signature="sig_abc"),
            ThinkingSegment(text="再核对边界", signature="sig_def"),
        ],
    )

    assert [b["kind"] for b in blocks] == ["thinking", "thinking", "text"]
    assert [b.get("signature") for b in blocks[:2]] == ["sig_abc", "sig_def"]


def test_build_blocks_skips_empty_thinking() -> None:
    """既无文本也无加密载荷的思考段不产出空块。"""
    blocks = build_assistant_blocks("答案", [], [ThinkingSegment()])

    assert [b["kind"] for b in blocks] == ["text"]


def test_thinking_blocks_round_trip_through_persistence() -> None:
    """AC2:落库形状 → parse → 再落库,签名与顺序逐字段不变。"""
    original = build_assistant_blocks(
        "结论",
        [],
        [
            ThinkingSegment(text="思考一", signature="sig_1"),
            ThinkingSegment(redacted_data="enc_2"),
            ThinkingSegment(text="思考三", signature="sig_3"),
        ],
    )

    parsed = parse_assistant_blocks(original)
    assert [b.to_dict() for b in parsed] == original


def test_legacy_thinking_wire_shape_raises() -> None:
    """残留的 Anthropic wire 形状(带 type 而非 kind)当场抛错,不静默误读。"""
    with pytest.raises(AgentException) as excinfo:
        parse_assistant_blocks([{"type": "thinking", "thinking": "x", "signature": "s"}])

    assert "kind 未知或缺失" in str(excinfo.value)


# ---- 出站编码(AC6、AC10) ----


def _encode_assistant(client: Any, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """把单条 assistant 中立消息编成该 provider 的 wire 消息。"""
    wire = client._encode_messages([{"role": "assistant", "content": blocks}])
    return wire[0]


def test_anthropic_encodes_thinking_blocks_in_order() -> None:
    """AC6:思考块与加密块原样编成 wire,顺序不重排。"""
    blocks = build_assistant_blocks(
        "结论",
        [],
        [
            ThinkingSegment(text="思考一", signature="sig_1"),
            ThinkingSegment(redacted_data="enc_2"),
        ],
    )

    assert _encode_assistant(_ANTHROPIC, blocks)["content"] == [
        {"type": "thinking", "thinking": "思考一", "signature": "sig_1"},
        {"type": "redacted_thinking", "data": "enc_2"},
        {"type": "text", "text": "结论"},
    ]


def test_anthropic_skips_unsigned_thinking_block() -> None:
    """AC10:无签名思考块(切 provider 后的遗留数据)出站被跳过,不回传。

    签名由 Anthropic 签发且不可伪造,回传缺签名的思考块会被判为篡改。
    """
    blocks = build_assistant_blocks("结论", [], [ThinkingSegment(text="来自 OpenAI 的思考")])

    assert _encode_assistant(_ANTHROPIC, blocks)["content"] == [
        {"type": "text", "text": "结论"}
    ]


def test_openai_encodes_thinking_as_reasoning_content() -> None:
    """思考文本拼进 reasoning_content;签名与加密载荷不下发。"""
    blocks = build_assistant_blocks(
        "结论",
        [],
        [
            ThinkingSegment(text="思考一", signature="sig_1"),
            ThinkingSegment(redacted_data="enc_2"),
            ThinkingSegment(text="思考三"),
        ],
    )

    encoded = _encode_assistant(_OPENAI, blocks)
    assert encoded["reasoning_content"] == "思考一思考三"
    assert encoded["content"] == "结论"
    assert "signature" not in encoded and "redacted_data" not in encoded


def test_openai_omits_reasoning_key_without_thinking() -> None:
    """无思考块时不出现 reasoning_content 键,标准端点行为零变化。"""
    encoded = _encode_assistant(_OPENAI, build_assistant_blocks("结论", [], None))

    assert "reasoning_content" not in encoded


# ---- OpenAI 入站:reasoning_content 通道 ----


def _openai_chunks(*deltas: dict[str, Any]) -> list[dict[str, Any]]:
    """把 delta 列表包成裸 chunk 序列,末尾补收束与用量 chunk。"""
    chunks: list[dict[str, Any]] = [
        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas
    ]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    return chunks


def test_openai_reasoning_content_emits_thinking_events() -> None:
    """reasoning_content 增量产出 THINKING_DELTA,思考块用独立 block_ref。"""
    adapter = _OpenAIChunkAdapter()
    chunks = _openai_chunks(
        {"reasoning_content": "先想一步"},
        {"reasoning_content": "再想一步"},
        {"content": "答案"},
    )
    events = [event for chunk in chunks for event in adapter.adapt(chunk)]

    thinking = [e for e in events if e.kind is LLMEventKind.THINKING_DELTA]
    assert [(e.block_ref, e.text) for e in thinking] == [(0, "先想一步"), (0, "再想一步")]
    starts = [e for e in events if e.kind is LLMEventKind.BLOCK_START]
    assert [(e.block_ref, e.block_type) for e in starts] == [
        (0, BlockType.THINKING),
        (1, BlockType.TEXT),
    ]


def test_openai_thinking_block_closes_when_text_starts() -> None:
    """思考块在首个正文增量处闭合,前端可立刻收起思考区域。"""
    adapter = _OpenAIChunkAdapter()
    adapter.adapt(_openai_chunks({"reasoning_content": "想"})[0])
    events = adapter.adapt({"choices": [{"index": 0, "delta": {"content": "答"}}]})

    first = events[0]
    assert (first.kind, first.block_ref) == (LLMEventKind.BLOCK_STOP, 0)


def test_openai_thinking_only_response_closes_at_finish() -> None:
    """只有思考没有正文时,思考块在收束点兜底闭合。"""
    adapter = _OpenAIChunkAdapter()
    result = _final_result(adapter, _openai_chunks({"reasoning_content": "只想不说"}))

    assert result.thinking == [ThinkingSegment(text="只想不说")]
    assert result.text == ""


def test_openai_terminal_thinking_merged_without_signature() -> None:
    """OpenAI 侧无签名概念,整段思考归并为单个无签名 ThinkingSegment。"""
    result = _final_result(
        _OpenAIChunkAdapter(),
        _openai_chunks({"reasoning_content": "先想"}, {"reasoning_content": "再想"}),
    )

    assert result.thinking == [ThinkingSegment(text="先想再想")]


# ---- 开关与预算校验(AC8、AC9) ----


def test_anthropic_thinking_param_absent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC8:开关关闭时产出 NOT_GIVEN,SDK 侧等同于不传该参数。

    不能返回 None:显式 None 会被 SDK 当作有效取值下发。
    """
    monkeypatch.setattr(anthropic_module.settings, "anthropic_thinking_enabled", False)

    assert _thinking_param() is NOT_GIVEN


def test_anthropic_thinking_param_carries_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关打开时下发 enabled 与预算。"""
    monkeypatch.setattr(anthropic_module.settings, "anthropic_thinking_enabled", True)
    monkeypatch.setattr(anthropic_module.settings, "anthropic_thinking_budget_tokens", 4096)

    assert _thinking_param() == {"type": "enabled", "budget_tokens": 4096}


@pytest.mark.parametrize("budget", [1023, 0, -1, 131072, 999_999])
def test_anthropic_budget_out_of_range_raises(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    """AC9:预算越界抛中文异常,不静默夹取。

    下限 1024 是 Anthropic 的硬约束;上限须严格小于 max_tokens,
    否则思考会吃满预算导致没有 token 留给正文。
    """
    monkeypatch.setattr(anthropic_module.settings, "anthropic_thinking_enabled", True)
    monkeypatch.setattr(anthropic_module.settings, "anthropic_thinking_budget_tokens", budget)

    with pytest.raises(AgentException) as excinfo:
        _thinking_param()

    assert "思考预算配置越界" in str(excinfo.value)


def test_thinking_defaults_are_off() -> None:
    """AC8:两家的代码默认值均为关,既有部署行为零变化。

    断言字段默认值而非实例值:实例会读 .env,受本地配置影响。
    """
    from core.config import Settings

    assert Settings.model_fields["anthropic_thinking_enabled"].default is False
    assert Settings.model_fields["openai_thinking_enabled"].default is False
    assert Settings.model_fields["openai_reasoning_effort"].default is None


class _StopStream(Exception):
    """替身在拿到请求参数后立即中断,不需要真跑完整条流。"""


async def _capture_request_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """替换 AsyncOpenAI,截获 chat.completions.create 的请求参数。"""
    captured: dict[str, Any] = {}

    class _StubCompletions:
        async def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            raise _StopStream

    class _StubClient:
        def __init__(self, **kwargs: Any) -> None:
            self.chat = type("_Chat", (), {"completions": _StubCompletions()})()

    monkeypatch.setattr(openai_module, "AsyncOpenAI", _StubClient)
    with pytest.raises(_StopStream):
        async for _ in _OPENAI.stream([{"role": "user", "content": "问题"}]):
            pass
    return captured


@pytest.mark.anyio
async def test_openai_thinking_absent_from_request_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8:开关关闭时 extra_body / reasoning_effort 两个键都不出现。"""
    monkeypatch.setattr(openai_module.settings, "openai_thinking_enabled", False)

    captured = await _capture_request_kwargs(monkeypatch)

    assert "extra_body" not in captured
    assert "reasoning_effort" not in captured


@pytest.mark.anyio
async def test_openai_thinking_injected_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关打开时 extra_body 带 thinking,配了档位则一并下发。"""
    monkeypatch.setattr(openai_module.settings, "openai_thinking_enabled", True)
    monkeypatch.setattr(openai_module.settings, "openai_reasoning_effort", "high")

    captured = await _capture_request_kwargs(monkeypatch)

    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["reasoning_effort"] == "high"
