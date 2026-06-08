# MessageParam 内容块回传指南

## 核心原则

**把 `response.content` 原封不动地存回 messages，不要过滤任何 block。**

```python
# 正确做法 — 保留全部 block
messages.append({"role": "assistant", "content": response.content})
```

API 会验证 `response.content` 里每个 block 的完整性。手动枚举或过滤 block 类型极易遗漏，直接回传是最安全的方式。

---

## 内容块速查表

| Block 类型 | 放在哪个 role | 说明 |
|---|---|---|
| `TextBlock` (`type: "text"`) | `assistant` | 从 `response.content` 原样保留 |
| `ThinkingBlock` (`type: "thinking"`) | `assistant` | **必须**原样保留，丢掉会 400 |
| `RedactedThinkingBlock` (`type: "redacted_thinking"`) | `assistant` | **必须**原样保留，内容被系统遮盖但不可删 |
| `ToolUseBlock` (`type: "tool_use"`) | `assistant` | **必须**原样保留，是后续 tool_result 的配对依据 |
| `ServerToolUseBlock` + 对应结果块 | `assistant` | 服务端工具（code execution、web search）的调用和结果，原样保留 |
| `ToolResultBlockParam` (`type: "tool_result"`) | `user` | 你执行工具后构造并发回，`tool_use_id` 需对应 ToolUseBlock 的 id |
| `TextBlockParam` (`type: "text"`) | `user` | 用户输入的文本 |
| `ImageBlockParam` / `DocumentBlockParam` | `user` | 用户上传的图片/文档 |

---

## ThinkingBlockParam — 必须回传，role: "assistant"

`ThinkingBlock` 必须原样保留在 assistant 消息里。API 会校验 thinking block 的完整性，**丢掉 thinking block 会直接返回 400 错误**。

`RedactedThinkingBlock`（被系统出于安全原因遮盖的 thinking）同样必须原样保留，虽然你无法读取其内容。

```python
# 启用 adaptive thinking
response = client.messages.create(
    model="claude-opus-4-8",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    messages=messages
)

# 可以读取 thinking 内容展示给用户（Opus 4.7/4.8 默认 omitted，需开启 display）
# thinking={"type": "adaptive", "display": "summarized"}

for block in response.content:
    if block.type == "thinking":
        print(f"[思考过程]: {block.thinking}")
    elif block.type == "text":
        print(f"[回复]: {block.text}")

# 原样回传 — 不要手动转换
messages.append({"role": "assistant", "content": response.content})
```

---

## ToolUseBlockParam — 必须回传，role: "assistant"

当 `stop_reason == "tool_use"` 时，response.content 包含 `ToolUseBlock`。你需要：

1. 把完整 `response.content`（含 thinking + tool_use blocks）存为 assistant 消息
2. 执行工具
3. 把工具结果作为 **role: "user"** 消息发回

```python
# assistant 的完整 content（含 thinking + tool_use blocks）
messages.append({"role": "assistant", "content": response.content})

# 执行工具，结果放在 user 消息里
tool_results = []
for block in response.content:
    if block.type == "tool_use":
        result = execute_tool(block.name, block.input)
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,   # 必须对应 tool_use block 的 id
            "content": result
        })

messages.append({"role": "user", "content": tool_results})
```

---

## TypedDict 类型 vs 裸字典

SDK 的 `MessageParam`、`ToolResultBlockParam` 等都是 `TypedDict` 子类，**运行时本质上仍然是 dict**，API 层面无差异。但使用类型化类有明确好处：

- IDE 自动补全字段名
- 静态类型检查（mypy/pyright）可以在编码阶段捕获拼写错误
- 代码意图更清晰

```python
from anthropic.types import (
    MessageParam,
    ToolResultBlockParam,
    TextBlockParam,
    ImageBlockParam,
)

# 裸字典方式（可用，但无类型检查）
messages.append({
    "role": "user",
    "content": [{
        "type": "tool_result",
        "tool_use_id": tool.id,
        "content": result
    }]
})

# TypedDict 类型方式（推荐，有类型检查）
messages.append(MessageParam(
    role="user",
    content=[
        ToolResultBlockParam(
            type="tool_result",
            tool_use_id=tool.id,
            content=result,
        )
    ]
))
```

**重要区分**：`response.content` 里的块（`ThinkingBlock`、`ToolUseBlock` 等）是 **Pydantic 模型对象**，不是 TypedDict。直接回传 `response.content` 给 SDK 完全可以——SDK 会自动处理 Pydantic 对象列表。

---

## 完整 Agentic Loop 示例

```python
import anthropic

client = anthropic.Anthropic()

tools = [
    {
        "name": "get_weather",
        "description": "获取指定城市的天气",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称"}
            },
            "required": ["city"]
        }
    }
]

messages = [{"role": "user", "content": user_input}]

while True:
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        tools=tools,
        messages=messages
    )

    if response.stop_reason == "end_turn":
        break

    if response.stop_reason == "pause_turn":
        # 服务端工具达到迭代上限，重新发送继续
        messages.append({"role": "assistant", "content": response.content})
        continue

    # 把完整 content 存回（含 thinking + tool_use blocks）
    messages.append({"role": "assistant", "content": response.content})

    # 处理 tool_use，结果作为 user 消息发回
    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = execute_tool(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result
            })

    if tool_results:
        messages.append({"role": "user", "content": tool_results})

# 获取最终文本回复
final_text = next(
    (b.text for b in response.content if b.type == "text"), ""
)
print(final_text)
```

---

## 序列化/存储场景

如果需要把对话历史写入数据库或 JSON 文件，使用 SDK 提供的 `.to_dict()` 方法，不要手写转换逻辑：

```python
# 把完整 content 转为可序列化的 dict 列表
content_dicts = [block.to_dict() for block in response.content]

# 或者转整个 response
response_dict = response.to_dict()

# 从 dict 恢复时，直接作为 content 传回
messages.append({"role": "assistant", "content": content_dicts})
```

---

## 常见错误

| 错误做法 | 后果 | 正确做法 |
|---|---|---|
| 只保留 `text` block，丢掉 `thinking` | API 返回 400 | 保留 `response.content` 全部内容 |
| 只保留 `thinking`，丢掉 `redacted_thinking` | API 返回 400 | 两者都必须保留 |
| tool_result 里的 `tool_use_id` 写错 | API 返回 400 | 从对应 `ToolUseBlock.id` 取值 |
| assistant 消息只保留文本字符串 | compaction block 丢失 | 必须保留 `response.content` 完整列表 |
| 手动枚举 block 类型转换 assistant 消息 | 遗漏新 block 类型（如 `redacted_thinking`）导致 400 | 直接回传 `response.content` |
