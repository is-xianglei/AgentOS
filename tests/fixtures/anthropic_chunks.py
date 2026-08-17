"""Anthropic 原始流式 chunk 的手写基线样本(Phase 0)。

形状严格对齐 llm/anthropic_client.py `stream()` 实际 yield 的结构:
SDK 事件的 `event.model_dump(mode="json", warnings=False)`,外加流末尾
一个合成的 `message_final`。手写而非真实调用录制,避免联网与 API Key 进仓库。

翻译器只消费 content_block_start / content_block_delta / content_block_stop /
message_final 四种 type,其余(ping / message_start / message_delta /
signature_delta 等)是噪声;各序列刻意掺入噪声 chunk,固化「噪声不产出
协议事件」这一行为。
"""

from typing import Any

# ---- 序列 a:单轮纯文本(一个 text 块,文本分 3 次 delta 下发) ----

CHUNKS_SINGLE_TEXT: list[dict[str, Any]] = [
    # 噪声:message_start 不产出协议事件。
    {"type": "message_start", "message": {"id": "msg_a", "role": "assistant", "content": []}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "，世"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "界"}},
    {"type": "content_block_stop", "index": 0},
    # 噪声:message_delta 不产出协议事件。
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 7},
    },
    {
        "type": "message_final",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 7,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 90,
        },
        "content": [{"type": "text", "text": "你好，世界"}],
    },
]

# ---- 序列 b:text 块 + tool_use 块(入参分多次 input_json_delta 下发) ----

CHUNKS_TEXT_AND_TOOL_USE: list[dict[str, Any]] = [
    # 噪声:ping 不产出协议事件。
    {"type": "ping"},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "我来读取文件"},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_01", "name": "file_read"},
    },
    # 入参 JSON 分 3 次下发,验证 json_buffer 累积逻辑。
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"pa'},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": 'th": "a'},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '.txt"}'},
    },
    {"type": "content_block_stop", "index": 1},
    {
        "type": "message_final",
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 200,
            "output_tokens": 45,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 180,
        },
        "content": [
            {"type": "text", "text": "我来读取文件"},
            {"type": "tool_use", "id": "toolu_01", "name": "file_read", "input": {"path": "a.txt"}},
        ],
    },
]

# ---- 序列 c:thinking 与 text 交错多块(thinking → text → thinking) ----

CHUNKS_THINKING_TEXT_INTERLEAVED: list[dict[str, Any]] = [
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "先分析需求"},
    },
    # 噪声:signature_delta 不产出协议事件。
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "signature_delta", "signature": "sig_abc"},
    },
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "text_delta", "text": "初步结论"},
    },
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "thinking"}},
    {
        "type": "content_block_delta",
        "index": 2,
        "delta": {"type": "thinking_delta", "thinking": "再核对边界"},
    },
    {"type": "content_block_stop", "index": 2},
    {
        "type": "message_final",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 300,
            "output_tokens": 60,
            "cache_creation_input_tokens": 12,
            "cache_read_input_tokens": 0,
        },
        "content": [
            {"type": "thinking", "thinking": "先分析需求", "signature": "sig_abc"},
            {"type": "text", "text": "初步结论"},
            {"type": "thinking", "thinking": "再核对边界", "signature": "sig_def"},
        ],
    },
]

# ---- 序列 d:tool_use 入参 JSON 非法(截断),供固化当前兜底成 {} 的行为 ----

CHUNKS_TOOL_USE_BAD_JSON: list[dict[str, Any]] = [
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_bad", "name": "file_read"},
    },
    # 拼出来是坏 JSON:{"path":（后半截丢失)。
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"path": '},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_final",
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 80,
            "output_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "content": [
            {"type": "tool_use", "id": "toolu_bad", "name": "file_read", "input": {}},
        ],
    },
]
