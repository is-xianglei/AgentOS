# 思考块进中立协议与落库

## Goal

让思考内容成为 AgentOS 的一等公民：进入中立消息协议、随会话落库，
并按各家协议要求在后续请求中原样回传。两家 provider 同时落地。

用户价值：当前 provider 为 DeepSeek，SSE 流中不存在任何思考过程（见 Background），
用户只能看到最终答案。补齐后思考过程可实时展示、可回放、可复盘。

**这不是可拆分的展示增强**：F1 表明开启思考模式与实现回传是同一件事，
只推 SSE 不落库会让所有带工具的会话在第二轮直接 400。

## Background：现状为什么没有思考过程

一条从 provider 到 SSE 的完整链路，只在最上游断了一节：

| 环节 | 位置 | 现状 |
|---|---|---|
| 中立事件种类 | `llm/types.py:193` | ✅ `LLMEventKind.THINKING_DELTA` 已定义 |
| 装配器转发 | `runtime/stream_assembler.py:106-107` | ✅ 已转成 `StreamEvent.thinking_delta` |
| 对外事件协议 | `core/events.py:35,46,274-278` | ✅ `THINKING` / `thinking_delta()` 齐备 |
| Anthropic 入站 | `llm/anthropic_client.py:181-186` | ✅ 解析 `thinking_delta` |
| **OpenAI 入站** | `llm/openai_client.py:144-145` | ❌ `adapt()` 只读 `content` 与 `tool_calls` |
| **请求开关** | `llm/openai_client.py:432-440` | ❌ 未传 `extra_body.thinking` |

即下游全通，缺的是 OpenAI 侧的入站解析与请求开关。

`llm/openai_client.py:110-113` 的注释断言「ChoiceDelta 没有 reasoning 字段，
协议本身不回传思考内容——这是协议事实而非实现缺失，不要『补齐』」。
该断言对 DeepSeek 不成立（见 R2），本任务须推翻它及其配套测试
`tests/test_llm_openai_client.py:198`。

## 已确认事实

### F1 DeepSeek 的回传约束是强制的（官方文档）

来源：`api-docs.deepseek.com/zh-cn/guides/thinking_mode`

- 思考模式经 `extra_body={"thinking": {"type": "enabled"}}` + `reasoning_effort` 开启；
- 响应字段为 `message.reasoning_content`；
- **若某轮发生了工具调用，该轮 `reasoning_content` 必须在后续所有请求中回传，
  否则 API 返回 400**；
- 未发生工具调用的轮次，`reasoning_content` 回传与否都会被忽略（传了无害）。

推论：AgentOS 是 ReAct 循环，同一 Turn 内几乎每轮 assistant 都带工具调用。
因此「只推 SSE 不落库」不成立——**开启思考模式与实现回传是同一件事，不可拆分交付**，
否则所有带工具的会话在第二轮即 400。

### F2 中立协议已有可直接复用的先例

`llm/types.py:62-160` 的中立块以 `kind` 判别（刻意不用 `type`）：
`TextBlock` / `ToolUseBlock`，经 `build_assistant_blocks()` 构造、
`parse_assistant_blocks()` 解析，未知 `kind` 抛中文 `AgentException`，无静默兜底。

`ToolUseBlock.id` 已经是「provider 下发的不透明字符串、必须原样回传」的字段
（Anthropic 为 `toolu_*`，OpenAI 为 `call_*`）。这说明中立协议禁止的是 wire **形状**
（键布局如 `type` / `input`），不是不透明**取值**。思考块沿用同一模式即可。

### F3 落库路径无需改动

- 写入：`session_service.add_message(session_id, "assistant", build_assistant_blocks(...))`，
  四处生产调用点 `runtime/agent.py:697`、`runtime/agent.py:1735`、
  `runtime/subagent.py:394`、`runtime/subagent.py:635`；
- 读取：`session/service.py:_messages_to_context()` 明确「三类角色均原样透出库中的中立形状」，
  不做任何 wire 编码。

因此 `session_messages.content` 是无 schema 的 JSON，新增一种块 kind
**不需要 ORM 变更，也不需要 Alembic 迁移**。

### F4 压缩路径不会破坏回传链

- `runtime/compact.py:_micro_compact()` 只改写 `role="tool"` 消息，不碰 assistant 块；
- `runtime/compact.py:_auto_compact()` 返回单条 `role="user"` 摘要消息
  （`runtime/compact.py:146-152`），整段历史被折叠，不留悬空的工具调用轮次。

因此压缩后不存在「有 tool_calls 却缺 reasoning_content」的消息，F1 约束天然满足。
但摘要 prompt 用 `json.dumps(messages)` 拼装（`runtime/compact.py:135`），
思考文本会连带进入压缩 prompt，需评估 token 膨胀。

### F5 缺失的承载字段

- `LLMTurnResult`（`llm/types.py:214-219`）只有 `text` / `tool_uses` / `usage` /
  `stop_reason`，**没有思考文本字段**；
- `runtime/stream_assembler.py` 转发 `THINKING_DELTA` 但**不累积**思考文本；
- `build_assistant_blocks(text, tool_uses)` 签名不收思考内容。

三处都要扩展，思考文本才能从流抵达落库。

### F6 目前没有任何思考相关配置

`core/config.py` 与 `.env.example` 中不存在 `thinking` / `reasoning` 键
（`.env.example:12` 的 `claude-opus-4-6-thinking` 只是模型名）。
两家客户端也都未传开关：Anthropic `messages.stream()`（`llm/anthropic_client.py:361-367`）
无 `thinking=` 参数，OpenAI `request_kwargs` 无 `extra_body`。

### F7 Anthropic 侧的约束显著更重（官方文档）

来源：`platform.claude.com/docs/en/api/errors`

- thinking / redacted_thinking 块在最新 assistant 消息中
  **不得修改、重排或过滤，必须原样返回**，即使 thinking 字段为空；
- 块需携带 `signature`；另有 `redacted_thinking` 加密块类型与 `display: omitted` 模式。

而 AgentOS 现状是：`signature_delta` 被显式归类为噪声不产出
（`llm/anthropic_client.py:195`、`tests/fixtures/anthropic_chunks.py:103-107`），
且 `build_assistant_blocks` 的 docstring 明确声明「块间顺序不承载语义…
故不保留 provider 下发时的交错顺序」——这一前提在 Anthropic 开启思考后不再成立。

结论：两家的成本高度不对称——OpenAI 侧思考是一段纯文本，Anthropic 侧则牵涉
签名、加密块与顺序约束。本任务同时纳入两家（用户决策），
因此 `ThinkingBlock` 从一开始就携带 `signature` 与 `redacted_data`。

### F8 Anthropic 会下发多个思考块，且各自带独立签名

现有 fixture 已固化 `thinking → text → thinking` 的交错三块序列
（`tests/fixtures/anthropic_chunks.py:94-137`），两个思考块签名不同
（`sig_abc` / `sig_def`），并由 `tests/test_stream_baseline.py:115` 锁定。

结合 F7 的「不得修改、重排或过滤」，**思考块不能塌缩成单块**：
中立协议须保留有序的多个思考段，每段各自携带签名。
这与文本块的处理方式不同——文本在装配器侧本就被合并成单串
（`llm/openai_client.py` docstring：「build_assistant_blocks 本就最多产一个文本块」），
思考块不能照抄这个简化。

### F9 Anthropic 终局消息自带完整签名，OpenAI 侧则需自累积

- Anthropic：`_on_message_final()`（`llm/anthropic_client.py:219-239`）从
  `stream.get_final_message()` 的 content 构造 `LLMTurnResult`，
  该 content 里的 thinking 块已带 `signature`
  （fixture `tests/fixtures/anthropic_chunks.py:134-136` 即为此形状）。
  故新增 `_extract_thinking_blocks(content)` 即可，与既有
  `_text_from_content` / `_extract_tool_uses` 同构。
- OpenAI：无终局消息，适配器自累积（`llm/openai_client.py` stream docstring），
  `reasoning_content` 累积成单段，签名恒为 `None`。

### F10 `redacted_thinking` 当前会被误判为文本块（潜在缺陷）

`_BLOCK_TYPE_MAP`（`llm/anthropic_client.py:35-39`）只登记
`text` / `thinking` / `tool_use`，`_on_block_start()` 对未知类型
兜底成 `BlockType.TEXT`（`llm/anthropic_client.py:161`）。

一旦开启 extended thinking，Anthropic 安全系统可能下发 `redacted_thinking`
加密块。当前实现会把它当作文本块向下游宣告，且终局提取不认识它、
直接丢弃——回传时块缺失，触发 F7 的 400「blocks must remain as they were」。
因此这不是打磨项，是开启思考后的**正确性前提**。

`core/events.py:43-49` 的 `BlockType` 仅有 `THINKING` / `TEXT` / `TOOL_USE`，
且该枚举是对外 SSE 协议的一部分。

## Requirements

### R1 中立协议新增思考块

- `llm/types.py` 增加 `_KIND_THINKING = "thinking"` 与 `ThinkingBlock`
  （`frozen=True` dataclass），字段为 `text: str`、`signature: str | None = None`、
  `redacted_data: str | None = None`；`to_dict()` 产出
  `{"kind": "thinking", "text": ..., "signature": ..., "redacted_data": ...}`；
- 并入 `AssistantBlock` 联合类型；`parse_assistant_blocks()` 识别该 kind，
  未知 kind 继续抛中文 `AgentException`；
- `build_assistant_blocks()` 签名扩展为接收**有序思考段列表**（F8），
  思考块排在文本块与工具块之前；空列表不产出块。

`signature` 与 `redacted_data` 是 provider 下发的不透明取值，
与既有 `ToolUseBlock.id` 同一性质（F2），不构成 wire 形状泄漏。

### R2 `LLMTurnResult` 与装配器贯通思考内容

- `LLMTurnResult`（`llm/types.py:214-219`）增加思考段列表字段，默认空列表，
  保持既有构造点兼容；
- `runtime/stream_assembler.py` **不自累积**思考文本：终局思考段以
  `LLMTurnResult` 为准。装配器对 `text` 本就如此
  （`runtime/stream_assembler.py:140` 直接 `self._last_result = event.result`），
  思考段沿用同一约定，避免「累积值与终局值不一致」的分叉；
  `THINKING_DELTA` 仍照常转发给 SSE，只负责实时展示；
- 四处 `build_assistant_blocks` 调用点（F3）传入思考段。

### R3 OpenAI 客户端补齐 reasoning 通道

- `_OpenAIChunkAdapter.adapt()` 读取 `delta.reasoning_content`，产出
  `THINKING_DELTA`；思考块占用独立 `block_ref`，不与 `_TEXT_REF = 0`
  及工具块的 `1 + index` 冲突；
- 适配器累积思考文本，`finalize()` 写入终局的思考段（签名为 `None`）；
- 出站 `_encode_messages()` 把思考段拼成 assistant 消息的 `reasoning_content`
  字段（与 `content` / `tool_calls` 并列）；
- 推翻 `llm/openai_client.py:110-113` 的过时注释与
  `tests/test_llm_openai_client.py:198` 的 `test_no_thinking_delta_emitted`。

### R4 Anthropic 客户端补齐思考块采集与回传

- `messages.stream()`（`llm/anthropic_client.py:361-367`）按开关传入
  `thinking={"type": "enabled", "budget_tokens": N}`；
- `signature_delta` 不再一律当噪声：终局从 `get_final_message()` 的 content
  提取带签名的思考块（F9），新增 `_extract_thinking_blocks()`；
- `_BLOCK_TYPE_MAP` 登记 `redacted_thinking`，消除 F10 的文本误判；
  加密块以 `redacted_data` 原样承载，不产出可读增量；
- `_encode_assistant_block()` 支持 `ThinkingBlock` → `{"type":"thinking",...}`
  或 `{"type":"redacted_thinking","data":...}`，**保持原始顺序不重排**；
- 推翻 `build_assistant_blocks` docstring 中「块间顺序不承载语义」的前提，
  并更新 `tests/test_stream_baseline.py:115` 对 `signature_delta` 的噪声断言。

### R5 思考模式开关配置

- 两家各一组开关，沿用 `AGENTOS_` 前缀与既有 `{provider}_*` 命名，
  按「配置同步规则」同步 `core/config.py`、`.env`、`.env.example`；
- **代码默认一律关闭**：OpenAI 侧 `extra_body={"thinking": ...}` 是 DeepSeek
  私有扩展，默认发给标准端点会被拒（F6）；Anthropic 侧开启会改变成本与延迟；
- 开关关闭时不得下发任何思考相关参数，行为与改造前逐键一致；
- Anthropic 的 `budget_tokens` 须校验下限 1024 且小于
  `llm_default_max_tokens`（`core/config.py:65`，当前 131072），
  越界抛中文 `AgentException`，不静默夹取。

### R6 跨 provider 残留数据安全

库中可能存在一家写入的思考块，而 provider 事后被切到另一家：

- 切到 Anthropic 时，缺少合法 `signature` 的思考块（DeepSeek 写入）
  **必须跳过**，不得伪造签名（F7：签名不可伪造），也不得抛异常
  导致历史会话不可用；
- 切到 OpenAI 时，带签名的思考块只取其 `text` 拼入 `reasoning_content`，
  `signature` / `redacted_data` 不发给模型；`redacted_data` 无可读文本，跳过。

## Acceptance Criteria

### 中立协议

- [ ] AC1 `parse_assistant_blocks([{"kind":"thinking","text":"x","signature":"s"}])`
      返回携带签名的 `ThinkingBlock`；未知 kind 仍抛中文 `AgentException`；
      带 `type` 的残留 provider wire 形状仍当场暴露。
- [ ] AC2 `build_assistant_blocks(思考段=[A,B], 文本, 工具)` 产出的块序列中，
      两个思考块**按原序**排在文本块与工具块之前，且各自签名不串位（F8）。

### OpenAI 侧

- [ ] AC3 喂入含 `reasoning_content` 的 DeepSeek chunk 序列，适配器产出
      `THINKING_DELTA`，且终局思考段文本等于各增量拼接、签名为 `None`。
- [ ] AC4 中立消息含思考块时，出站 assistant 消息同时带 `reasoning_content`
      与 `tool_calls`，满足 F1 的回传要求。

### Anthropic 侧

- [ ] AC5 现有 fixture 的 `thinking → text → thinking` 交错序列（F8）经改造后，
      终局产出**两个**思考段，签名分别为 `sig_abc` / `sig_def`，顺序不颠倒。
- [ ] AC6 `redacted_thinking` 块的 `block_start` 不再被宣告为 `TEXT`（F10），
      其加密 `data` 进入终局思考段，出站编码原样还原为
      `{"type":"redacted_thinking","data":...}`。
- [ ] AC7 一条含 thinking + text + tool_use 的中立消息，出站编码后块顺序与
      落库顺序完全一致（F7：不得重排）。

### 开关与兼容

- [ ] AC8 两家开关关闭时，请求参数与改造前**逐键一致**
      （OpenAI 无 `extra_body`/`reasoning_effort`，Anthropic 无 `thinking`），
      防止默认行为漂移。
- [ ] AC9 `budget_tokens` 小于 1024 或不小于 `llm_default_max_tokens` 时
      抛中文 `AgentException`，不静默夹取。
- [ ] AC10 无签名思考块（DeepSeek 写入）经 Anthropic 出站编码被跳过，
      其余 text/tool_use 块编码结果不变，不抛异常（R6）。

### 端到端与回归

- [ ] AC11 真实 DeepSeek 会话：发起一次**触发工具调用**的提问，SSE 出现
      thinking 事件，且第二轮请求不报 400（F1 的直接验证）。
- [ ] AC12 `uv run pytest` 全绿；`uv run ruff check .` 无新增问题；
      `Settings` 可加载且三处配置同步。

## Out of Scope

- 思考内容的前端渲染：`THINKING_DELTA` → SSE 链路已通（见 Background 表），
  本任务不改前端，也不新增对外事件类型。
- 压缩 prompt 的 token 优化：F4 指出思考文本会随 `json.dumps(messages)`
  进入压缩 prompt（`runtime/compact.py:135`），记为观察项，本任务不改
  `_auto_compact`。
- 思考内容的脱敏与审计：思考文本按普通消息内容处理，不额外加密或降级存储；
  日志仍只记长度不记正文（沿用既有日志规范）。
- Anthropic 的 `display: omitted` 模式（F7）：不在本任务开启，
  仅保证 `redacted_thinking` 块不被丢弃。
- `complete_structured()` 路径：结构化输出不涉及思考回传，不改。
