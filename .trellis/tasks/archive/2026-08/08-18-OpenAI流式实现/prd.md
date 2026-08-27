# OpenAI 流式实现

## Goal

实现 `OpenAIClient.stream()`，并把消息体从 Anthropic 专属 wire 形状中立化，
使 `agent` / `subagent` / `compact` 三个流式场景可通过 `AGENTOS_LLM_PROVIDER=openai`
真正切到 OpenAI provider，且同一段会话历史在两家 provider 之间可来回切换。

## Background

`llm/types.py:84` 的 `LLMTurnResult.provider_content` 是上一期「流式协议翻译抽象」
刻意留下的债，其字段注释已预告「消息体中立化属另一改造半径」。现状是：

- `role="assistant"` 消息把 provider 返回的 content 块数组**原样落库**，存的是
  Anthropic wire 原文（`{"type": "tool_use", "id": ..., "input": {...}}`）。
- `role="tool"` 消息落的是自有协议 `ToolResultMessage`（含 Anthropic 本身没有的
  `tool_name` / `input_args`），但读取时 `session/service.py` 又把它翻译回
  Anthropic 专属的 `{"type": "tool_result", "tool_use_id": ...}` 塞进 user 消息。

因此只实现 `stream()` 不足以让 OpenAI 跑通：第一轮纯文本能出字，第二轮带工具历史
回灌时 OpenAI 会收到 Anthropic 形状的 messages 而报 400。本任务合并交付两件事
（方案 B），不拆父子任务——二者共享同一套往返测试，拆开则前半程无法验收。

## Scope

### In Scope

1. `OpenAIClient.stream()` 实现：OpenAI 流式 chunk → 中立 `LLMStreamEvent`，
   以恰好一个 `TURN_RESULT` 收尾。
2. 消息体中立化：库中不再存任何 provider wire 字段，出站时由翻译层现编成目标
   provider 形状。
3. 删除 `LLMTurnResult.provider_content` 字段及其全部生产/消费点。
4. 两家 provider 的出站消息翻译，以及配套的往返等价测试。

### Out of Scope

- 不实现 OpenAI 的 prompt caching（OpenAI 无显式断点概念，`_encode_system` 现状
  忽略 `cache_breakpoint` 即为正确行为）。
- 不改动 `TurnEventAssembler` 的块级协议语义（index 分配、start/stop 配对规则不变）。
- 不做存量历史数据迁移（见 Constraints 第 5 条）。
- 不引入 `StreamTranslator` ABC 或 `create_translator()` 工厂
  （`llm-provider-guidelines.md` §3.6 明令禁止第二个 provider 抽象）。
- 不改 `complete_structured()` 的现有实现。

## Requirements

### R1 OpenAIClient.stream()

- R1.1 使用 SDK 自带的 `client.chat.completions.stream()` 上下文管理器，
  流末尾用 `get_final_completion()` 取终局，与 Anthropic 侧
  `get_final_message()` 的写法对称。
- R1.2 入站适配器为**模块私有**类（对标 `_AnthropicChunkAdapter`），
  生命周期为单次 `stream()` 调用，provider 原始格式不外露。
- R1.3 产出的中立事件序列必须满足 `BaseLLMClient.stream()` 契约：
  块 `BLOCK_START` 先于其 delta，`block_ref` 在单次调用内唯一，
  流以恰好一个 `TURN_RESULT` 收尾且 `result` 非空。
- R1.4 `max_tokens` 取自 `settings.llm_default_max_tokens`，不硬编码；
  OpenAI 侧参数名为 `max_completion_tokens`。
- R1.5 `timeout` 为 `None` 时转 `NOT_GIVEN`，不得直接传 `None`（会被 SDK
  理解为禁用超时）。
- R1.6 工具调用入参 JSON 解析失败时抛中文 `AgentException`，
  携带 `tool_name` / `tool_call_id`，不得兜底成 `{}`。
- R1.7 `THINKING_DELTA` 在 OpenAI 侧不产出——Chat Completions 的 `ChoiceDelta`
  无 reasoning 字段，这是协议事实而非实现缺失，须在代码注释中写明原因。
- R1.8 usage 必须能拿到；若需 `stream_options={"include_usage": True}` 才有值，
  则显式传入，不得让 `TURN_RESULT` 的 usage 恒为空。

### R2 消息体中立化

- R2.1 定义 assistant 消息的中立块结构，覆盖 text 与 tool_use 两类；
  沿用现有 `ToolResultMessage`（已是自有协议）承载 tool 消息，形状不变。
- R2.2 `LLMTurnResult.provider_content` 删除；`TURN_RESULT` 不再透传任何
  provider 形状数据。
- R2.3 落库的 `session_messages.content` 中不得出现 `"type": "tool_use"` /
  `"type": "tool_result"` / `tool_use_id` 等 provider wire 字段名。
- R2.4 出站翻译（中立消息 → 各家 wire 形状）实现为独立可测单元，
  两家各一个编码路径；入站适配保持各 Client 私有。
- R2.5 `runtime/compact.py` 的历史遍历改读中立字段名，不再按 Anthropic
  的 `type` 判别块类型。

### R3 一致性与兼容

- R3.1 `runtime/` / `tools/` / `prompt/` 对 `anthropic` 保持零引用
  （现状即如此，改造不得引入新引用）。
- R3.2 `BaseLLMClient` 抽象成员仍为 3 个（`provider` / `stream` /
  `complete_structured`），不把翻译/编码方法提上 ABC。
- R3.3 成本归因写入逻辑不变：仅 LLM 产出的 assistant 消息填
  `provider` / `model` 两列。
- R3.4 改造后 `llm-provider-guidelines.md` §3.4 的能力边界描述
  （「OpenAIClient 仅实现 complete_structured」）已过时，须在 Phase 3 同步更新规范。

## Constraints

1. 遵守 `llm-provider-guidelines.md` §3.6 的流式分层：入站适配私有于 Client，
   协议装配归 `TurnEventAssembler`，出站编码是 `stream()` 的内部步骤。
2. 无静默兜底：provider 未配置、场景未知、模型解析不出、入参 JSON 非法，
   一律抛具体中文 `AgentException`。
3. 不自造重试框架，重试与超时直接用 SDK 内置机制。
4. `AGENTOS_OPENAI_BASE_URL` 必须带 `/v1`；当前 `.env` 配的是第三方网关
   `https://4sapi.org/v1`，模型 `gpt-5.6-sol` 非 OpenAI 原生模型名——
   验收时必须把「SDK 契约支持」与「该网关实际支持」分开验证并分别报告。
5. 存量库中已有 Anthropic 形状的 assistant 历史消息。用户已确认交付时会清空数据库，
   故本任务不做数据迁移、不做兼容读、也不写旧格式检测分支——为不会发生的场景
   写校验属多余代码。注意此项只豁免「历史遗留数据」，不豁免运行时持续产生的
   快照形状问题（见 design.md §6.4）。
6. 不新增 Alembic 迁移——`session_messages.content` 是 JSONB，结构变更不涉及 DDL。
7. 编码遵循项目 4 空格缩进、100 字符行宽、Python 3.14 语法与中文注释规范。

## Acceptance Criteria

- [ ] AC1 `OpenAIClient.stream()` 不再抛 `NotImplementedError`；
      对纯文本、文本+工具调用两类 chunk 序列，产出的中立事件序列经
      `TurnEventAssembler` 装配后与 Anthropic 侧同语义输入的协议事件**逐字段等价**
      （block index、start/stop 配对、tool_use 载荷）。
- [ ] AC2 工具调用入参 JSON 非法时抛中文 `AgentException` 且携带工具名，
      有对应测试用例。
- [ ] AC3 `TURN_RESULT` 携带非空 usage（input/output token 数），
      `stop_reason` 与 `tool_uses` 正确填充，有对应测试用例。
- [ ] AC4 全仓库（排除 `.venv`）搜索 `provider_content` 无任何有效引用。
- [ ] AC5 出站翻译往返测试：中立消息 → Anthropic wire → 回中立，
      与 → OpenAI wire → 回中立，两条路径均与原消息等价，无字段静默丢失。
- [ ] AC6 一段含「文本 + 工具调用 + 工具结果 + 后续追问」的完整会话历史，
      可分别编成两家的 wire 形状且各自合法，有对应测试用例。
- [ ] AC7 落库内容断言：三个持久化点（`session_messages.content`、
      `session_snapshots.messages`、`team_members.history`）落库后
      均不含 `"type": "tool_use"` / `tool_use_id` 等 wire 字段名。
- [ ] AC11 切 provider 后旧快照仍可用：同一会话产生快照后切换 provider，
      下一轮请求不因快照形状不匹配而失败（design §6.4）。
- [ ] AC8 `uv run pytest` 全量通过；现有 27 个用例不因本次改造而失败
      （允许因中立化而修改断言，但不得删除用例或降低覆盖）。
- [ ] AC9 `llm-provider-guidelines.md` 的 §3.4 能力边界、§6.2 测试清单
      已同步更新为改造后的事实。
- [ ] AC10 真实网关连通性验证：以 `AGENTOS_LLM_PROVIDER=openai` 实跑一次
      纯文本流式与一次带工具的流式，如实报告结果；若网关不支持，
      须明确区分「SDK 契约已满足」与「该网关能力不足」并报告具体错误。

## Notes

- 架构图见 `research/provider-architecture.html`（方案 B 全链路 + 落库形态对比）。
- 本任务为复杂任务，需 `design.md` 与 `implement.md` 齐备后方可 `task.py start`。
