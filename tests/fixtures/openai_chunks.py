"""OpenAI 裸流式 chunk 的手写基线样本(阶段 4 新增)。

形状对齐 llm/openai_client.py `stream()` 内部实际消费的结构:
`chunk.model_dump(mode="json", warnings=False)` 的产物。手写而非真实调用
录制,避免联网与 API Key 进仓库。适配器只消费 usage 与 choices[0] 的
delta / finish_reason,其余顶层字段(id / object / created / model 等)
与真实 chunk 一致地存在与否不影响行为,样本中省略以保持可读。

与 Anthropic 侧的协议差异(fixtures 据此构造):
- 无块索引概念:文本是 delta.content 连续增量,工具调用有独立的
  tool_calls[].index(从 0 起);
- 无显式 block_stop:收束点是 finish_reason 非空的那个 chunk;
- usage 只在末 chunk 出现(choices 为空),且需显式开启 include_usage;
- tool_call 的 id / name 只在该调用的首个增量携带,后续只带 index 与
  function.arguments 增量。

各序列刻意掺入噪声 chunk(role-only 首 chunk、空 delta、内容 chunk 上的
usage: null),固化「噪声不产出协议事件」这一行为。
"""

from typing import Any

# ---- 序列 a:单轮纯文本(文本分 3 次 delta 下发,末 chunk 带 usage) ----

CHUNKS_SINGLE_TEXT: list[dict[str, Any]] = [
    # 噪声:role-only 首 chunk,content 为 None,不产出协议事件。
    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}], "usage": None},
    {"choices": [{"index": 0, "delta": {"content": "你好"}}], "usage": None},
    {"choices": [{"index": 0, "delta": {"content": "，世"}}], "usage": None},
    {"choices": [{"index": 0, "delta": {"content": "界"}}], "usage": None},
    # 收束 chunk:delta 为空对象,finish_reason 到达。
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": None},
    # 末 chunk:choices 为空,仅带 usage(include_usage 显式开启后才有)。
    {
        "choices": [],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 7,
            "total_tokens": 127,
            "prompt_tokens_details": {"cached_tokens": 90},
        },
    },
]

# ---- 序列 b:文本 + 单工具调用(入参 JSON 分 3 次增量下发) ----

CHUNKS_TEXT_AND_TOOL_USE: list[dict[str, Any]] = [
    # 噪声:role-only 首 chunk。
    {"choices": [{"index": 0, "delta": {"role": "assistant"}}], "usage": None},
    {"choices": [{"index": 0, "delta": {"content": "我来读取文件"}}], "usage": None},
    # 工具调用首个增量:携带 id / name,arguments 从此处开始累积。
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_01",
                            "type": "function",
                            "function": {"name": "file_read", "arguments": '{"pa'},
                        }
                    ]
                },
            }
        ],
        "usage": None,
    },
    # 后续增量:只带 index 与 arguments,不带 id / name。
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": 'th": "a'}}]
                },
            }
        ],
        "usage": None,
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '.txt"}'}}]},
            }
        ],
        "usage": None,
    },
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": None},
    {
        "choices": [],
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 45,
            "total_tokens": 245,
            "prompt_tokens_details": {"cached_tokens": 180},
        },
    },
]

# ---- 序列 c:多工具并发(两个 tool_calls index 交错下发,无文本) ----

CHUNKS_MULTI_TOOL_USE: list[dict[str, Any]] = [
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "file_read", "arguments": ""},
                        }
                    ],
                },
            }
        ],
        "usage": None,
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '{"path": "a.txt"}'}}
                    ]
                },
            }
        ],
        "usage": None,
    },
    # 第二个工具调用开始:index=1,与 index=0 的增量交错。
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "file_write", "arguments": '{"path"'},
                        }
                    ]
                },
            }
        ],
        "usage": None,
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {"index": 1, "function": {"arguments": ': "b.txt", "content": "x"}'}}
                    ]
                },
            }
        ],
        "usage": None,
    },
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": None},
    {
        "choices": [],
        "usage": {
            "prompt_tokens": 300,
            "completion_tokens": 60,
            "total_tokens": 360,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    },
]

# ---- 序列 d:工具入参 JSON 非法(截断),断言抛中文 AgentException ----

CHUNKS_TOOL_USE_BAD_JSON: list[dict[str, Any]] = [
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_bad",
                            "type": "function",
                            # 拼出来是坏 JSON:{"path": (后半截丢失)。
                            "function": {"name": "file_read", "arguments": '{"path": '},
                        }
                    ],
                },
            }
        ],
        "usage": None,
    },
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": None},
]

# ---- 序列 e:被 max_completion_tokens 截断(finish_reason=length,文本保留) ----

CHUNKS_TEXT_TRUNCATED: list[dict[str, Any]] = [
    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "结论是"}}], "usage": None},
    {
        "choices": [{"index": 0, "delta": {"content": "…"}, "finish_reason": "length"}],
        "usage": None,
    },
    {
        "choices": [],
        "usage": {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58},
    },
]

# ---- 序列 f:无 usage 末 chunk(网关未回 usage,TURN_RESULT.usage 为 None) ----

CHUNKS_TEXT_NO_USAGE: list[dict[str, Any]] = [
    {"choices": [{"index": 0, "delta": {"content": "好的"}}], "usage": None},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": None},
]
