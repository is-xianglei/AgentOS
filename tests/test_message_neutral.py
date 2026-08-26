"""中立消息协议与两家出站翻译的等价性测试(阶段 1)。

覆盖 AC5(中立 → 各家 wire → 回中立的往返等价)与 AC6(含文本、工具调用、
工具结果、后续追问的完整会话可分别编成两家 wire 且各自合法)。

_encode_messages 是各客户端私有实现、刻意不上 ABC(见 spec §3.6),此处直接调
私有方法。反向解码器只存在于测试:生产链路无「wire → 中立」需求,把它写在测试里
正是往返测试的意义——解码器只能从 wire 里实际存在的字段重建,任何被静默丢弃的
字段都会让往返断言失败。

编码是纯静态方法,不建 SDK 连接、不读配置,无需构造客户端实例。
"""

import json
from typing import Any

import pytest

from core.errors import AgentException
from llm.anthropic_client import AnthropicClient
from llm.openai_client import _TOOL_ERROR_PREFIX, OpenAIClient
from llm.types import ToolResultMessage, ToolUse, build_assistant_blocks

# 完整会话:文本 + 两个并发工具调用 + 两个工具结果(一个失败)+ 纯文本 + 追问。
# 刻意用两个连续工具结果:Anthropic 侧必须合并进同一条 user 消息,
# OpenAI 侧必须拆成两条独立 role="tool" 消息——两家的结构性差异全在这一段。
_SESSION: list[dict[str, Any]] = [
    {"role": "user", "content": "读一下 a.txt 和 b.txt"},
    {
        "role": "assistant",
        "content": build_assistant_blocks(
            "我来读这两个文件",
            [
                ToolUse(id="toolu_01", name="file_read", input={"path": "a.txt"}),
                ToolUse(id="toolu_02", name="file_read", input={"path": "b.txt"}),
            ],
        ),
    },
    {
        "role": "tool",
        "content": ToolResultMessage(
            tool_use_id="toolu_01",
            tool_name="file_read",
            input_args={"path": "a.txt"},
            output="内容A",
        ).to_content_dict(),
    },
    {
        "role": "tool",
        "content": ToolResultMessage(
            tool_use_id="toolu_02",
            tool_name="file_read",
            input_args={"path": "b.txt"},
            output="文件不存在",
            is_error=True,
        ).to_content_dict(),
    },
    {"role": "assistant", "content": build_assistant_blocks("a.txt 有内容，b.txt 缺失", [])},
    {"role": "user", "content": "那就只用 a.txt"},
]


def _strip_untransmitted(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把中立消息归一为「两家 wire 都承载得下」的子集,供往返比对。

    tool_name / input_args 是 AgentOS 自有的排障字段,两家协议都没有对应位置,
    刻意不发给模型。此处显式抹平这两个字段,使「有意不传输」与「静默丢字段」
    在断言上区分开——其余任何字段的丢失都会让往返失败。
    """
    normalized: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] != "tool":
            normalized.append(message)
            continue
        tool_result = ToolResultMessage.from_content_dict(message["content"])
        normalized.append(
            {
                "role": "tool",
                "content": ToolResultMessage(
                    tool_use_id=tool_result.tool_use_id,
                    tool_name="",
                    input_args={},
                    output=tool_result.output,
                    is_error=tool_result.is_error,
                ).to_content_dict(),
            }
        )
    return normalized


def _decode_anthropic(wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic wire → 中立消息(仅测试用的反向解码)。"""
    decoded: list[dict[str, Any]] = []
    for message in wire:
        content = message["content"]
        if message["role"] == "assistant":
            blocks: list[dict[str, Any]] = []
            for block in content if isinstance(content, list) else []:
                if block["type"] == "text":
                    blocks.append({"kind": "text", "text": block["text"]})
                else:
                    blocks.append(
                        {
                            "kind": "tool_use",
                            "id": block["id"],
                            "name": block["name"],
                            "input": block["input"],
                        }
                    )
            decoded.append({"role": "assistant", "content": blocks})
        elif isinstance(content, str):
            decoded.append({"role": "user", "content": content})
        else:
            # tool_result 块被塞进 user 消息并合并,解码时逐块拆回 tool 消息。
            decoded.extend(
                {
                    "role": "tool",
                    "content": ToolResultMessage(
                        tool_use_id=block["tool_use_id"],
                        tool_name="",
                        input_args={},
                        output=block["content"],
                        is_error=bool(block.get("is_error", False)),
                    ).to_content_dict(),
                }
                for block in content
            )
    return decoded


def _decode_openai(wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI wire → 中立消息(仅测试用的反向解码)。"""
    decoded: list[dict[str, Any]] = []
    for message in wire:
        role = message["role"]
        if role == "user":
            decoded.append({"role": "user", "content": message["content"]})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message["content"]:
                blocks.append({"kind": "text", "text": message["content"]})
            for call in message.get("tool_calls") or []:
                blocks.append(
                    {
                        "kind": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": json.loads(call["function"]["arguments"]),
                    }
                )
            decoded.append({"role": "assistant", "content": blocks})
        else:
            # is_error 在 OpenAI 侧无字段位置,编成内容前缀,解码时剥回布尔。
            output = message["content"]
            is_error = output.startswith(_TOOL_ERROR_PREFIX)
            decoded.append(
                {
                    "role": "tool",
                    "content": ToolResultMessage(
                        tool_use_id=message["tool_call_id"],
                        tool_name="",
                        input_args={},
                        output=output.removeprefix(_TOOL_ERROR_PREFIX),
                        is_error=is_error,
                    ).to_content_dict(),
                }
            )
    return decoded


# --- AC5:往返等价 ---


def test_anthropic_round_trip_preserves_message() -> None:
    """中立 → Anthropic wire → 回中立,与原消息等价。"""
    wire = AnthropicClient._encode_messages(_SESSION)
    assert _decode_anthropic(wire) == _strip_untransmitted(_SESSION)


def test_openai_round_trip_preserves_message() -> None:
    """中立 → OpenAI wire → 回中立,与原消息等价。"""
    wire = OpenAIClient._encode_messages(_SESSION)
    assert _decode_openai(wire) == _strip_untransmitted(_SESSION)


def test_both_round_trips_converge() -> None:
    """两条往返路径落到同一个中立结果:中立协议对两家等距,无偏向某一家。"""
    assert _decode_anthropic(AnthropicClient._encode_messages(_SESSION)) == _decode_openai(
        OpenAIClient._encode_messages(_SESSION)
    )


# --- AC6:完整会话双向编码,各自合法 ---


def test_anthropic_wire_shape_of_full_session() -> None:
    """Anthropic 形状:tool_use 是 content 里的块,连续 tool_result 合并进一条 user。"""
    wire = AnthropicClient._encode_messages(_SESSION)
    assert [message["role"] for message in wire] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert wire[1]["content"] == [
        {"type": "text", "text": "我来读这两个文件"},
        {"type": "tool_use", "id": "toolu_01", "name": "file_read", "input": {"path": "a.txt"}},
        {"type": "tool_use", "id": "toolu_02", "name": "file_read", "input": {"path": "b.txt"}},
    ]
    # 两个工具结果合并进同一条 user 消息:否则出现连续 user 消息,违反角色交替。
    assert wire[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "内容A"},
        {
            "type": "tool_result",
            "tool_use_id": "toolu_02",
            "content": "文件不存在",
            "is_error": True,
        },
    ]
    # is_error 仅在为真时下发,成功结果不带该键。
    assert "is_error" not in wire[2]["content"][0]


def test_openai_wire_shape_of_full_session() -> None:
    """OpenAI 形状:tool_use 进 tool_calls 数组,每个结果一条独立 role="tool" 消息。"""
    wire = OpenAIClient._encode_messages(_SESSION)
    assert [message["role"] for message in wire] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "user",
    ]
    assert wire[1]["content"] == "我来读这两个文件"
    assert [call["id"] for call in wire[1]["tool_calls"]] == ["toolu_01", "toolu_02"]
    # 入参在 OpenAI 侧是 JSON 字符串,不是原生对象。
    assert wire[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'
    assert wire[2] == {"role": "tool", "tool_call_id": "toolu_01", "content": "内容A"}
    assert wire[3]["content"].startswith(_TOOL_ERROR_PREFIX)
    # 纯文本 assistant 不带 tool_calls 键。
    assert "tool_calls" not in wire[4]


def test_role_counts_differ_between_providers() -> None:
    """两家消息条数不同(4 vs 6),固化「共用一套形状糊不过去」这一事实。"""
    assert len(AnthropicClient._encode_messages(_SESSION)) == 5
    assert len(OpenAIClient._encode_messages(_SESSION)) == 6


# --- 边界与无静默兜底 ---


def test_assistant_without_text_keeps_content_null_on_openai() -> None:
    """仅有工具调用而无文本时,OpenAI 的 content 置 None(该键不能省略)。"""
    messages = [
        {
            "role": "assistant",
            "content": build_assistant_blocks("", [ToolUse(id="t1", name="echo", input={})]),
        }
    ]
    assert OpenAIClient._encode_messages(messages)[0]["content"] is None


def test_empty_assistant_blocks_encode_to_empty_string_on_anthropic() -> None:
    """空块数组落成空串:Anthropic 拒绝空 content 数组。"""
    encoded = AnthropicClient._encode_messages([{"role": "assistant", "content": []}])
    assert encoded[0]["content"] == ""


@pytest.mark.parametrize(
    "encode", [AnthropicClient._encode_messages, OpenAIClient._encode_messages]
)
def test_legacy_provider_shape_raises(encode: Any) -> None:
    """残留的 provider wire 形状(带 type 而非 kind)当场抛错,不被静默误读。

    这正是中立块判别键取 kind 而非 type 的理由:两家的 type 取值恰好同名,
    沿用 type 会让旧数据被当成新格式处理,错误一路潜伏到运行时。
    """
    legacy = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "echo", "input": {}}],
        }
    ]
    with pytest.raises(AgentException, match="kind 未知或缺失"):
        encode(legacy)


@pytest.mark.parametrize(
    "encode", [AnthropicClient._encode_messages, OpenAIClient._encode_messages]
)
def test_unknown_role_raises(encode: Any) -> None:
    """未知角色抛中文异常,不静默跳过。"""
    with pytest.raises(AgentException, match="未知的消息角色"):
        encode([{"role": "system", "content": "x"}])


@pytest.mark.parametrize(
    "encode", [AnthropicClient._encode_messages, OpenAIClient._encode_messages]
)
def test_user_list_content_raises(encode: Any) -> None:
    """user 消息只承载纯文本:list 形态一律视为残留 wire 形状并抛错。"""
    with pytest.raises(AgentException, match="user 消息 content 必须是字符串"):
        encode([{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}])
