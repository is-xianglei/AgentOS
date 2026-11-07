"""领域消息类型的序列化契约测试。

落库 JSON 与发给 LLM 的中立形状由这些类型产出和读入:字段名是领域语言,JSON 键名是
落库契约,两者有意不同。样本 JSON 是照着改名前 to_dict() / to_content_dict() 的输出
手写的字面量,反序列化再序列化必须逐字节一致,任何键名或键序漂移都会在此暴露。
"""

import json
from typing import Any

import pytest

from core.errors import AgentException
from harness.domain.messages import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    UserMessage,
    message_from_row,
)

# ---- 改名前的落库样本(手写字面量,键名与键序即契约) ----

_LEGACY_ASSISTANT_JSON = (
    '[{"kind": "thinking", "text": "先分析", "signature": "sig_1", "redacted_data": null}, '
    '{"kind": "thinking", "text": "", "signature": null, "redacted_data": "enc_2"}, '
    '{"kind": "text", "text": "我来读文件"}, '
    '{"kind": "tool_use", "id": "toolu_01", "name": "Read", "input": {"path": "a.txt"}}, '
    '{"kind": "tool_use", "id": "toolu_02", "name": "Bash", "input": {}}]'
)
_LEGACY_TOOL_JSON = (
    '{"tool_use_id": "toolu_01", "tool_name": "Read", "input_args": {"path": "a.txt"}, '
    '"output": "内容A", "is_error": true}'
)


def _dumps(content: Any) -> str:
    """与落库 JSONB 写入时的序列化方式无关,只用于逐字节比对键名与键序。"""
    return json.dumps(content, ensure_ascii=False)


def test_legacy_assistant_json_round_trips_byte_for_byte() -> None:
    raw = json.loads(_LEGACY_ASSISTANT_JSON)

    message = AssistantMessage.from_content(raw)

    assert _dumps(message.to_content()) == _LEGACY_ASSISTANT_JSON


def test_legacy_tool_json_round_trips_byte_for_byte() -> None:
    raw = json.loads(_LEGACY_TOOL_JSON)

    result = ToolResult.from_content(raw)

    assert _dumps(result.to_content()) == _LEGACY_TOOL_JSON
    # 字段名是领域语言,与 JSON 键名有意不同。
    assert result.tool_call_id == "toolu_01"
    assert result.arguments == {"path": "a.txt"}
    assert result.content == "内容A"


def test_legacy_json_maps_to_domain_fields() -> None:
    message = AssistantMessage.from_content(json.loads(_LEGACY_ASSISTANT_JSON))

    assert message.thinking == (
        ThinkingBlock(text="先分析", signature="sig_1"),
        ThinkingBlock(redacted_data="enc_2"),
    )
    assert message.text == "我来读文件"
    assert message.tool_calls == (
        ToolCall(id="toolu_01", tool_name="Read", arguments={"path": "a.txt"}),
        ToolCall(id="toolu_02", tool_name="Bash", arguments={}),
    )


def test_block_to_dict_matches_legacy_shapes() -> None:
    """单块序列化与改名前的 to_dict() 输出逐键一致。"""
    assert ThinkingBlock(text="t", signature="s").to_dict() == {
        "kind": "thinking",
        "text": "t",
        "signature": "s",
        "redacted_data": None,
    }
    assert TextBlock(text="x").to_dict() == {"kind": "text", "text": "x"}
    assert ToolCall(id="c1", tool_name="Read", arguments={"a": 1}).to_dict() == {
        "kind": "tool_use",
        "id": "c1",
        "name": "Read",
        "input": {"a": 1},
    }


# ---- 每种消息 to_content / from_content 往返 ----


@pytest.mark.parametrize(
    "message",
    [
        UserMessage(text="读一下 a.txt"),
        UserMessage(text=""),
        AssistantMessage(blocks=()),
        AssistantMessage(
            blocks=(
                ThinkingBlock(text="想", signature="sig"),
                TextBlock(text="答"),
                ToolCall(id="c1", tool_name="Read", arguments={"path": "a"}),
            )
        ),
        ToolResult(tool_call_id="c1", tool_name="Read", arguments={"path": "a"}, content="ok"),
        ToolResult(tool_call_id="c2", tool_name="", arguments={}, content="失败", is_error=True),
    ],
)
def test_message_round_trip(message: UserMessage | AssistantMessage | ToolResult) -> None:
    assert type(message).from_content(message.to_content()) == message


@pytest.mark.parametrize(
    ("role", "content"),
    [
        ("user", "你好"),
        ("assistant", json.loads(_LEGACY_ASSISTANT_JSON)),
        ("tool", json.loads(_LEGACY_TOOL_JSON)),
    ],
)
def test_message_from_row_round_trips(role: str, content: Any) -> None:
    assert message_from_row(role, content).to_content() == content


def test_message_from_row_dispatches_by_role() -> None:
    assert isinstance(message_from_row("user", "x"), UserMessage)
    assert isinstance(message_from_row("assistant", []), AssistantMessage)
    assert isinstance(message_from_row("tool", json.loads(_LEGACY_TOOL_JSON)), ToolResult)


def test_message_from_row_rejects_unknown_role() -> None:
    with pytest.raises(AgentException) as excinfo:
        message_from_row("system", "x")

    assert excinfo.value.message == "未知的消息角色"
    assert excinfo.value.details == {"role": "system"}


def test_tool_result_defaults_for_optional_fields() -> None:
    """tool_name / input_args / is_error 缺省时沿用改名前的兜底取值。"""
    result = ToolResult.from_content({"tool_use_id": "c1", "output": "ok", "input_args": None})

    assert result == ToolResult(tool_call_id="c1", tool_name="", arguments={}, content="ok")


def test_tool_call_missing_input_defaults_to_empty_arguments() -> None:
    message = AssistantMessage.from_content([{"kind": "tool_use", "id": "c1", "name": "Read"}])

    assert message.tool_calls == (ToolCall(id="c1", tool_name="Read", arguments={}),)


# ---- 构造语义:from_turn 与 str 形态归一 ----


def test_from_turn_puts_thinking_first_and_keeps_order() -> None:
    message = AssistantMessage.from_turn(
        "结论",
        [ToolCall(id="c1", tool_name="Read", arguments={})],
        [
            ThinkingBlock(text="思考一", signature="sig_1"),
            ThinkingBlock(redacted_data="enc_2"),
            ThinkingBlock(text="思考三", signature="sig_3"),
        ],
    )

    assert [block.to_dict()["kind"] for block in message.blocks] == [
        "thinking",
        "thinking",
        "thinking",
        "text",
        "tool_use",
    ]
    assert [(b.signature, b.redacted_data) for b in message.thinking] == [
        ("sig_1", None),
        (None, "enc_2"),
        ("sig_3", None),
    ]


def test_from_turn_skips_empty_thinking_but_keeps_redacted() -> None:
    """既无文本也无加密载荷的思考段跳过;redacted 的无可读文本也必须保留。"""
    message = AssistantMessage.from_turn(
        "答案",
        [],
        [ThinkingBlock(), ThinkingBlock(redacted_data="enc"), ThinkingBlock(signature="s")],
    )

    assert message.blocks == (ThinkingBlock(redacted_data="enc"), TextBlock(text="答案"))


def test_from_turn_without_text_produces_no_text_block() -> None:
    message = AssistantMessage.from_turn("", [ToolCall(id="c1", tool_name="Bash")])

    assert message.to_content() == [{"kind": "tool_use", "id": "c1", "name": "Bash", "input": {}}]
    assert message.text == ""


def test_thinking_order_survives_persistence_round_trip() -> None:
    original = AssistantMessage.from_turn(
        "结论",
        [],
        [
            ThinkingBlock(text="思考一", signature="sig_1"),
            ThinkingBlock(redacted_data="enc_2"),
            ThinkingBlock(text="思考三", signature="sig_3"),
        ],
    )

    restored = AssistantMessage.from_content(original.to_content())

    assert restored == original
    assert restored.to_content() == original.to_content()


def test_str_content_normalizes_to_single_text_block() -> None:
    assert AssistantMessage.from_content("纯文本").blocks == (TextBlock(text="纯文本"),)
    assert AssistantMessage.from_content("").blocks == ()


def test_text_property_joins_all_text_blocks() -> None:
    message = AssistantMessage(blocks=(TextBlock(text="上"), TextBlock(text="下")))

    assert message.text == "上下"


# ---- 失败路径:中文消息与 details 与改名前一致 ----


@pytest.mark.parametrize(
    ("content", "message", "details"),
    [
        (
            [{"text": "缺 kind"}],
            "assistant 消息块的 kind 未知或缺失",
            {"kind": None, "keys": ["text"]},
        ),
        (
            [{"type": "tool_use", "id": "t1", "name": "echo", "input": {}}],
            "assistant 消息块的 kind 未知或缺失",
            {"kind": None, "keys": ["id", "input", "name", "type"]},
        ),
        (
            [{"kind": "image", "url": "x"}],
            "assistant 消息块的 kind 未知或缺失",
            {"kind": "image", "keys": ["kind", "url"]},
        ),
        (["裸字符串块"], "assistant 消息块必须是字典", {"block_type": "str"}),
        (
            {"kind": "text"},
            "assistant 消息 content 必须是中立块数组或字符串",
            {"content_type": "dict"},
        ),
        (None, "assistant 消息 content 必须是中立块数组或字符串", {"content_type": "NoneType"}),
    ],
)
def test_assistant_invalid_content_raises(
    content: Any, message: str, details: dict[str, Any]
) -> None:
    with pytest.raises(AgentException) as excinfo:
        AssistantMessage.from_content(content)

    assert excinfo.value.message == message
    assert excinfo.value.details == details


def test_assistant_tool_call_missing_id_raises_key_error() -> None:
    """tool_use 块缺 id / name 沿用改名前行为:直接抛 KeyError,不包装。"""
    with pytest.raises(KeyError):
        AssistantMessage.from_content([{"kind": "tool_use", "name": "Read"}])


@pytest.mark.parametrize(
    ("content", "content_type"),
    [([{"type": "tool_result", "tool_use_id": "t1"}], "list"), (None, "NoneType")],
)
def test_user_non_str_content_raises(content: Any, content_type: str) -> None:
    with pytest.raises(AgentException) as excinfo:
        UserMessage.from_content(content)

    assert excinfo.value.message == "user 消息 content 必须是字符串"
    assert excinfo.value.details == {"content_type": content_type}


def test_tool_non_dict_content_raises() -> None:
    with pytest.raises(AgentException) as excinfo:
        ToolResult.from_content("纯文本")

    assert excinfo.value.message == "tool 消息 content 必须是中立 ToolResultMessage 字典"
    assert excinfo.value.details == {"content_type": "str"}


@pytest.mark.parametrize(
    ("content", "missing"),
    [({"output": "ok"}, "tool_use_id"), ({"tool_use_id": "t1"}, "output")],
)
def test_tool_missing_required_field_raises(content: dict[str, Any], missing: str) -> None:
    with pytest.raises(AgentException) as excinfo:
        ToolResult.from_content(content)

    assert excinfo.value.message == "tool 消息缺少必需字段"
    assert excinfo.value.details == {"field": missing}
    assert isinstance(excinfo.value.__cause__, KeyError)
