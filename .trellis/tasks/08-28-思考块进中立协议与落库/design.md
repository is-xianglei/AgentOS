# 技术设计：思考块进中立协议与落库

## 一、边界与分层

改动集中在 `llm/` 层，向上只扩展数据载体，不改变调用协议：

```
provider wire        中立协议              运行时                持久化
─────────────        ────────              ──────                ──────
reasoning_content ┐                    ┌ THINKING_DELTA ──→ SSE（已通，不改）
                  ├→ ThinkingSegment ──┤
thinking+signature┘   （有序列表）      └ LLMTurnResult.thinking
                                              │
                                              ↓ build_assistant_blocks()
                                        ThinkingBlock（kind="thinking"）
                                              │
                                              ↓ session_messages.content（JSON，无需迁移）
                                              │
                                              ↓ _encode_messages()（各家私有）
                                        reasoning_content / thinking 块
```

关键边界（沿用 CLAUDE.md 多 Provider 规范第 14 条）：
`_encode_messages` 仍是各家客户端私有方法，**不提升到 `BaseLLMClient` 抽象成员**，
抽象成员维持 `provider` / `stream` / `complete_structured` 三个不变。

## 二、核心数据结构

### 2.1 `ThinkingSegment`（流式载体，`llm/types.py`）

```python
@dataclass(frozen=True)
class ThinkingSegment:
    """一段思考。signature / redacted_data 是 provider 下发的不透明取值，
    与 ToolUseBlock.id 同一性质：必须原样回传，但不构成 wire 形状泄漏。"""
    text: str = ""
    signature: str | None = None
    redacted_data: str | None = None
```

`LLMTurnResult` 增加 `thinking: list[ThinkingSegment] = field(default_factory=list)`。
默认空列表使既有构造点（含全部测试替身）无需改动。

### 2.2 `ThinkingBlock`（持久化形状，`llm/types.py`）

```python
_KIND_THINKING = "thinking"

@dataclass(frozen=True)
class ThinkingBlock:
    text: str = ""
    signature: str | None = None
    redacted_data: str | None = None
```

`AssistantBlock` 联合扩为 `TextBlock | ToolUseBlock | ThinkingBlock`。

### 2.3 为什么是「有序列表」而不是单块

`build_assistant_blocks` 现有 docstring 声明「块间顺序不承载语义……
故不保留 provider 下发时的交错顺序」。该前提在思考块上**不成立**：

- Anthropic 明确要求块不得修改、重排或过滤（PRD F7）；
- 现有 fixture 已固化一次响应含两个签名不同的思考块（PRD F8）。

塌缩成单块会让 `sig_abc` / `sig_def` 无处安放，回传即 400。
故思考段保序，且整体排在 text / tool_use 之前（与两家实际下发顺序一致）。

文本块仍保持「合并成单串」的既有行为，本次不改。

## 三、数据流分家：终局权威

| provider | 增量来源 | 终局来源 | 签名 |
|---|---|---|---|
| Anthropic | `thinking_delta` | `get_final_message()` 的 content | 终局才有 |
| OpenAI | `delta.reasoning_content` | 适配器 `finalize()` 自累积 | 恒 `None` |

两家都以 **`LLMTurnResult.thinking` 为唯一权威**，装配器不自累积
（`runtime/stream_assembler.py:140` 对 `text` 已是此约定）。

`THINKING_DELTA` 事件只服务 SSE 实时展示，不参与落库，
因此 Anthropic「签名只在终局出现」不会造成落库缺签名。

## 四、出站编码

### 4.1 Anthropic

```
ThinkingBlock(redacted_data=D)          → {"type":"redacted_thinking","data":D}
ThinkingBlock(text=T, signature=S)      → {"type":"thinking","thinking":T,"signature":S}
ThinkingBlock(text=T, signature=None)   → 跳过（外来数据，签名不可伪造）
```

顺序：`parse_assistant_blocks()` 的返回序即落库序，编码时**原样遍历不重排**。

### 4.2 OpenAI

思考段的 `text` 按序拼接成 assistant 消息的 `reasoning_content` 字段，
与 `content` / `tool_calls` 并列；`signature` / `redacted_data` 不下发。
`redacted_data` 无可读文本，跳过。

该字段不在 OpenAI SDK 的 TypedDict 内，沿用既有
`cast("list[ChatCompletionMessageParam]", encoded)` 收窄即可，无需新增类型。

## 五、请求参数与开关

### 5.1 配置项（三处同步）

| 键 | 类型 | 代码默认 | 说明 |
|---|---|---|---|
| `AGENTOS_OPENAI_THINKING_ENABLED` | bool | `False` | DeepSeek 私有扩展，默认关 |
| `AGENTOS_OPENAI_REASONING_EFFORT` | str \| None | `None` | 开启时随请求下发 |
| `AGENTOS_ANTHROPIC_THINKING_ENABLED` | bool | `False` | 改变成本与延迟，默认关 |
| `AGENTOS_ANTHROPIC_THINKING_BUDGET_TOKENS` | int | `4096` | 仅开启时生效 |

**代码默认必须为关**：OpenAI 侧 `extra_body={"thinking":...}` 发给
`api.openai.com` 等标准端点会被拒；默认关保证既有部署行为零变化。
本地 `.env` 可按需置 `true`。

### 5.2 注入点

```python
# llm/openai_client.py stream()，紧随 request_kwargs 构造
if settings.openai_thinking_enabled:
    request_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    if settings.openai_reasoning_effort:
        request_kwargs["reasoning_effort"] = settings.openai_reasoning_effort

# llm/anthropic_client.py stream()，messages.stream(...) 参数
# 关闭时整个 thinking 参数不出现（NOT_GIVEN 而非 None，沿用既有超时写法）
```

### 5.3 `budget_tokens` 校验

开启 Anthropic 思考时校验 `1024 <= budget < llm_default_max_tokens`
（`core/config.py:65`，当前 131072），越界抛中文 `AgentException`。
不静默夹取——沿用项目「无静默兜底」原则。

## 六、兼容性与降级

| 场景 | 行为 |
|---|---|
| 开关全关 | 请求参数与改造前逐键一致；不产思考段；落库块不含 thinking |
| 库中旧消息无思考块 | `parse_assistant_blocks` 原样解析，行为不变 |
| DeepSeek 写的块 → Anthropic 读 | 无签名，编码时跳过（不伪造、不抛异常） |
| Anthropic 写的块 → DeepSeek 读 | 只取 `text` 拼 `reasoning_content`，弃签名 |
| 压缩后 | `_auto_compact` 折叠为单条 user 摘要，无悬空工具轮次（PRD F4），F1 约束天然满足 |

**无需 Alembic 迁移**：`session_messages.content` 是无 schema JSON，
新增块 kind 不触碰 ORM（PRD F3）。

## 七、被推翻的既有约定

| 位置 | 现状 | 变更 |
|---|---|---|
| `llm/openai_client.py:110-113` | 注释断言「协议本身不回传思考内容，不要补齐」 | 改写：对 DeepSeek 不成立 |
| `tests/test_llm_openai_client.py:198` | `test_no_thinking_delta_emitted` | 反转为「开关开启时产出」 |
| `llm/anthropic_client.py:195` | `signature_delta` 归为噪声 | 增量仍不产事件，但终局须提取签名 |
| `llm/types.py` `build_assistant_blocks` docstring | 「块间顺序不承载语义」 | 限定为「文本与工具块之间」，思考块保序 |
| `tests/test_stream_baseline.py:115` | 断言 signature 不出现 | 改为断言终局思考段带签名 |
| `_BLOCK_TYPE_MAP`（`llm/anthropic_client.py:35`） | 无 `redacted_thinking`，兜底成 TEXT | 登记为 `BlockType.THINKING` |

`redacted_thinking` 复用 `BlockType.THINKING` 而非新增枚举值：
`BlockType` 属对外 SSE 协议（`core/events.py:43-49`），
新增取值会让前端面对未知块类型，收益不抵成本。加密块无可读增量，
下游表现为「一个不产文本的思考块」，语义自洽。

## 八、风险

| 风险 | 缓解 |
|---|---|
| 开启后 ReAct 第二轮 400 | AC11 端到端验证是硬门槛，必须真实跑通 |
| 思考文本推高压缩 prompt 体积 | 已记入 Out of Scope 观察项，不在本任务处理 |
| `reasoning_content` 非标准字段被网关拒 | 开关默认关；开启仅限已验证的 DeepSeek 端点 |
| 多处测试基线被推翻，掩盖真实回归 | 逐条改写并保留原断言意图，不整体删除用例 |
