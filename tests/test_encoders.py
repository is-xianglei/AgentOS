"""两家客户端出站编码单测(Phase 2)。

_encode_system / _encode_tools 是各客户端的私有实现,不上 ABC;此处直接
调私有方法断言编码结果形状,验证中立 PromptBlock / ToolSpec 真够两家用:
Anthropic 侧 cache_breakpoint 落成 cache_control,OpenAI 侧忽略断点拼纯文本。
编码是纯静态方法,不建 SDK 连接、不读配置,无需构造客户端实例。
"""

from llm.anthropic_client import AnthropicClient
from llm.openai_client import OpenAIClient
from llm.types import PromptBlock, ToolSpec

_TOOL_SPECS = [
    ToolSpec(
        name="file_read",
        description="读取文件",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    ),
    ToolSpec(name="echo", description="回显", input_schema={}),
]

_BLOCKS = [
    PromptBlock(text="稳定段", cache_breakpoint=True),
    PromptBlock(text="易失段"),
]


# --- Anthropic ---


def test_anthropic_encode_system_cache_breakpoint() -> None:
    """断点块带 cache_control,非断点块不带该键。"""
    encoded = AnthropicClient._encode_system(_BLOCKS)
    assert encoded == [
        {"type": "text", "text": "稳定段", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "易失段"},
    ]


def test_anthropic_encode_system_str_passthrough() -> None:
    """str 形态(子代理整段纯文本)原样透传,None 归一为空串。"""
    assert AnthropicClient._encode_system("整段提示") == "整段提示"
    assert AnthropicClient._encode_system(None) == ""


def test_anthropic_encode_tools_flat_shape() -> None:
    """ToolSpec 编成 Anthropic 平铺的 {name, description, input_schema}。"""
    encoded = AnthropicClient._encode_tools(_TOOL_SPECS)
    assert encoded == [
        {
            "name": "file_read",
            "description": "读取文件",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {"name": "echo", "description": "回显", "input_schema": {}},
    ]
    assert AnthropicClient._encode_tools(None) == []


# --- OpenAI ---


def test_openai_encode_system_ignores_breakpoints() -> None:
    """OpenAI 无 cache 断点概念:标志被忽略,多块按空行拼接成纯文本。"""
    assert OpenAIClient._encode_system(_BLOCKS) == "稳定段\n\n易失段"


def test_openai_encode_system_str_passthrough() -> None:
    assert OpenAIClient._encode_system("整段提示") == "整段提示"
    assert OpenAIClient._encode_system(None) == ""


def test_openai_encode_tools_nested_function_shape() -> None:
    """ToolSpec 编成 OpenAI 嵌套的 {"type": "function", "function": {...}}。"""
    encoded = OpenAIClient._encode_tools(_TOOL_SPECS)
    assert encoded == [
        {
            "type": "function",
            "function": {
                "name": "file_read",
                "description": "读取文件",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {"name": "echo", "description": "回显", "parameters": {}},
        },
    ]
    assert OpenAIClient._encode_tools(None) == []
