# Provider 流式协议翻译抽象

## 目标

把「各家 provider 的原始流式数据结构」彻底关进各自的客户端实现里，由每家 provider
自己适配成 AgentOS 自有协议。`runtime/` 层不再认识任何 provider 的形状——新增
provider 时 `runtime/agent.py`、`runtime/subagent.py`、`runtime/compact.py` 一行不改。

## 背景

`runtime/agent.py:570` 硬编码 `translator = AnthropicStreamTranslator(actor, session_id)`。
当前不报错的唯一原因是 `llm/openai_client.py:87` 的 `stream()` 仍是
`NotImplementedError`——流式路径事实上只有 Anthropic 能走通（CLAUDE.md
「多 Provider LLM 配置」第 10 条已记录该现状）。这是欠账，不是设计。

### 泄漏面：四类，不止翻译器一处

即便补一个 `OpenAIStreamTranslator` 再在 runtime 里 `if provider == ...`，
仍有四类 Anthropic 形状横穿分层：

| 类别 | 位置 | 说明 |
| --- | --- | --- |
| 入站翻译器硬编码 | `runtime/agent.py:570,1598`；`runtime/subagent.py:131,443` | 本任务的直接触发点 |
| 直接判 Anthropic 事件名 | `runtime/agent.py:638,1624`；`runtime/subagent.py:236`；`runtime/compact.py:124` | `chunk.get("type") == "message_final"`，runtime 认识原始 chunk |
| 解析 Anthropic content block | `runtime/agent.py:645`；`runtime/subagent.py:367,369,372,587` | `extract_tool_uses` / `text_from_content` 按 Anthropic 块数组解析 |
| **出站**参数也是 Anthropic 形状 | `runtime/agent.py:590,610,1601`；`tools/base.py:36`；`prompt/compose.py:48` | `to_anthropic_tools()` 直接构造 SDK `ToolParam`；system prompt 用 SDK `TextBlockParam` |

### 结论一：翻译是 provider 私有实现，不需要跨 provider 抽象

`BaseLLMClient` 已经是 provider 抽象层，不必在它旁边再立一个
「翻译器接口」让各家去对齐。各家原始流的结构差异极大——Anthropic 有 content block
与块级 index；OpenAI 只有 `choices[].delta`，无块边界，`tool_calls[].index`
是数组下标、语义完全不同，text 增量更是无 index。把这些差异抽象成一个公共接口，
等于强行给两种不同的东西找共同父类；正确做法是各自客户端在 `stream()` 内部消化掉，
**直接产出 AgentOS 中立事件**。

### 结论二：现有翻译器没有任何真正的跨调用状态

`runtime/agent.py:568` 注释称「翻译器跨本回合多次 LLM 调用共用，以延续 tool_use
入参累积」。核查 `llm/types.py` 后该前提不成立：

| 状态 | 实际生命周期 |
| --- | --- |
| `_open: dict[int, _OpenBlock]` | `_on_block_start` 写入、`_on_block_stop` pop，单次 API 调用内开闭完毕 |
| `json_buffer`（tool_use 入参） | `input_json_delta` 累积 → `content_block_stop` 一次性 flush，同样单次调用内闭合 |
| `_last_usage` / `_stop_reason` | 每次 `message_final` 覆写，循环后立即读走（`agent.py:644`） |

`_finalize_without_tools`（`agent.py:1604`）「沿用同一翻译器维持事件连续」同样未兑现：
index 透传 Anthropic 原始值、每次调用从 0 重来，共用同一对象并不产生连续性。
即注释描述了代码并未实现的意图。

真正需要 turn 级存活的只有「turn 内全局递增的 block index」——而这属于 AgentOS
自有协议的**装配职责**，与 provider 无关，应放在 runtime 侧的中立装配器，
不应塞进 provider 抽象。

## 目标结构

```
客户端 stream()  →  yield 中立 LLMStreamEvent
                    ↑ 各家自己适配，翻译私有、不抽象、不外露
                      （无 actor / 无 session_id / 无全局 index）

runtime 侧 assembler  →  盖 actor + session_id、分配 turn 内全局 index
                    ↓    并按协议规则补齐块边界
                    StreamEvent（core/events.py 现有协议，结构不变）
                    ↑ 只有一份，provider 无关，不随 provider 数量增长
```

runtime 调用点最终形态：

```python
assembler = TurnEventAssembler(actor, session_id)   # 中立，非 provider 产出
...
async for ev in assembler.consume(
    self.llm.stream(context, system_prompt, tools=tools, model=model_override)
):
    await self.bus.emit(ev)
result = assembler.last_result
```

## 需求

### R1 客户端直接产出中立事件（翻译私有化）

- 新增中立事件类型 `LLMStreamEvent`，置于 `llm/types.py`（provider 侧的出口类型；
  `core/events.py` 保持只承载对外协议，二者不混）。
- `BaseLLMClient.stream()` 的返回类型由 `AsyncIterator[dict[str, Any]]`
  改为 `AsyncIterator[LLMStreamEvent]`。
- `LLMStreamEvent` 不含 `actor` / `session_id` / 全局 index——这些是 runtime 概念，
  不得下沉进 `llm/`。块归属用**调用内局部块标识**表达（如 `local_block_id: int`），
  由 assembler 负责映射为全局 index。
- 事件种类需覆盖现有协议的内容语义：块开始（带块类型）、文本增量、思考增量、
  工具调用（入参已解析完整）、块结束。
- `AnthropicStreamTranslator`（`llm/types.py:118`）**降级为 `AnthropicClient` 的
  私有实现细节**：搬进 Anthropic 客户端内部、改名去掉对外语义、不再被 `runtime/`
  导入。其逐 chunk 翻译逻辑本身不改语义。
- **不新增 `StreamTranslator` ABC，不新增 `create_translator()` 工厂方法。**
  provider 抽象只有 `BaseLLMClient` 一个，新增 provider 只写一个客户端。

### R2 turn 终局结果中立化

- 新增 `LLMTurnResult`（frozen dataclass，置于 `llm/types.py`），字段：
  `text: str`、`tool_uses: list[ToolUse]`、`usage: Usage | None`、
  `stop_reason: str | None`，以及遗留承载字段
  `provider_content: list[dict[str, Any]]`。
  最后一个字段用于 assistant 消息落库——本期不做消息体中立化
  （DB 已有 JSONB 历史数据与快照，属另一改造半径，详见 design.md 第十节），
  因此仍需一个受控出口把 provider 形状的 content 带给 runtime，
  不得再新增第二个同类透传字段。
- 由客户端在流末尾以一个终结事件产出（承载于 `LLMStreamEvent`），
  assembler 收集后经 `last_result` 属性暴露给 runtime
  （取 `consume()` 包住整个流、而非逐事件 `stamp()` 的理由见 design.md 第六节）。
- runtime 不再判 `message_final`、不再调 `extract_tool_uses` / `text_from_content`；
  `llm/types.py` 中这两个 Anthropic 专用解析函数随之收归 Anthropic 客户端私有。
- `runtime/compact.py:124-127` 同步改造。

### R3 出站：tools 与 system prompt 编码归客户端

- 定义中立的 `ToolSpec` 与 `PromptBlock`（类型化 dataclass，遵循「内部协议用
  dataclass」约定），承载 name / description / input_schema 与 cache 断点语义。
- 各 provider 在 `stream()` 内部用**私有方法** `_encode_tools` / `_encode_system`
  把中立描述编成本家 SDK 形状。
  **注**：初版曾要求在 `BaseLLMClient` 上加两个公开抽象方法，已在 design.md
  第十三节推翻——编码是 `stream()` 的内部步骤，没有任何外部调用方，
  提成抽象方法等于把实现细节写进契约，正是本任务要清除的那类过度抽象。
  `BaseLLMClient` 抽象成员数维持 3 个不变。
- `tools/base.py:36` `to_param()` 与 `tools/registry.py:42` `to_anthropic_tools()`
  改为产出中立 `ToolSpec`（更名 `to_spec()` / `to_specs()`）；
  Anthropic 形状转换下沉到 `AnthropicClient`。
- `prompt/compose.py:48` `compose_lead_blocks()` 返回中立 `PromptBlock` 列表，
  不再 import `anthropic.types.TextBlockParam`。

### R4 块级协议装配规则（由 assembler 唯一实现）

**调研前提**（2026-08-16 全仓核查）：外发协议里的 `block.index` **当前零消费**——
链路只有 `llm/types.py` 生产 → `core/events.py:352` `to_dict()` →
`core/event_bus.py` → `session/api.py:70` SSE 吐出，服务端无任何代码读取
`event.block.index`（`tools/service.py:209` 的 `o.block` 是 hook 阻断语义，
同名不同物），且仓库无前端工程。故这不是「修 bug」，而是**在尚无消费方约束时
把契约定死**——前端一旦接入，再改块级语义即为破坏性变更。

**R4.1 index 由 assembler 统一分配，turn 内全局递增。**

现状透传 Anthropic 原始 index、每次 API 调用从 0 重来（`llm/types.py:125-129`
docstring 已标注），一个 turn 内多轮 tool use 时第二次调用的 text 块又是 `index=0`，
消费方拿 index 当 key 会串块。改由 assembler 用 `(第几次调用, local_block_id)`
映射到全局递增 index。

关键收益：index 分配是 assembler 的**代码**，不是各 provider 需遵守的**契约**。
provider 根本不参与分配，因此两家语义**想不一致都做不到**——这比写在 ABC
docstring 里的约定强得多，也是不要 `StreamTranslator` ABC 的原因之一。

**R4.2 start/stop 配对规则，同样由 assembler 兜底。**

现状 `block_start` / `block_stop` 不成对：tool_use 块走 `tool_use` 事件路径时
不发 `block_stop`（`llm/types.py:206-213`），消费方若按「见 block_stop 才收尾」
实现会让 tool_use 块永远悬空。需二选一并由 assembler 统一保证：

- 方案 A（推荐）：所有块一律成对，tool_use 块在 `tool_use` 事件后补发 `block_stop`，
  消费方规则统一为「见 block_stop 即收尾」；
- 方案 B：维持不成对，明确「`tool_use` 事件本身即该块的终止信号」。

**R4.3 规则写入 assembler 的类级 docstring**，作为 AgentOS 块级协议的唯一定义处。

## 非目标

- ❌ 不实现 OpenAI 流式（`OpenAIClient.stream()` 保持 `NotImplementedError`，
  仅按新签名调整类型注解）。本任务只做结构收口；OpenAI 流式落地是独立任务。
- ❌ 不改 `core/events.py` 的 `StreamEvent` **数据结构**（字段、工厂方法、
  `to_dict()` 形状均不变）。与 R4 不冲突：R4 改的是产出这些事件的**语义**
  （index 如何分配、tool_use 块是否补发 `block_stop`），不动结构本身。
- ❌ 不改 provider 选择与模型解析逻辑（`llm/factory.py` 保持现状）。
- ❌ 不改 `complete_structured()` 路径（非流式，各客户端已各自实现）。
- ❌ 不引入 `StreamTranslator` 之类的第二个 provider 抽象（见 R1 结论一）。

## 约束

1. 遵循 CLAUDE.md「无静默兜底」：provider 未配置、事件类型未知一律抛中文
   `AgentException`，不得回落 Anthropic 处理分支。
2. 遵循记忆「自有协议 + 翻译器」（2026-08-16 修订版）：一律转成 AgentOS 协议，
   不存各家原始格式；翻译按方向区分——**落库/出站独立成层，入站流式适配归各家
   Client 私有**。本任务属入站方向，故翻译下沉进 Client 不违反该约定。
3. `Service` / `Runtime` 的 `__init__` 继续接受可选 `llm: BaseLLMClient | None`，
   测试替身注入方式不变；替身只需产出 `LLMStreamEvent`，无需再实现翻译器。
4. 判断 provider 一律读 `client.provider` 属性，不得 `isinstance` 或读
   `settings.llm_provider`。
5. **本仓当前无任何测试**：`pyproject.toml:26-27` 配了 `testpaths = ["tests"]`，
   但 `tests/` 目录不存在，全仓 `test_*.py` 与 `conftest.py` 数量为 0。
   故不存在可依赖的回归基线——事件序列正确性只能靠本任务新建的测试保证，
   必须在改造前先建立基线（见执行分期 Phase 0）。
6. 改动分期提交，每期独立可运行、可测试。

## 验收标准

- [ ] `runtime/` 下 `grep -rn "Anthropic\|anthropic"` 无业务代码命中（仅允许注释说明）。
- [ ] `runtime/agent.py`、`runtime/subagent.py`、`runtime/compact.py` 不再出现
      `message_final`、`extract_tool_uses`、`text_from_content`、`to_anthropic_tools`、
      `TextBlockParam`，也不再出现任何 translator 变量。
- [ ] `prompt/compose.py`、`tools/base.py`、`tools/registry.py` 不再 import
      `anthropic.types`。
- [ ] 全仓不存在 `StreamTranslator` ABC 或 `create_translator()`；
      `AnthropicStreamTranslator` 已成为 `AnthropicClient` 的私有实现，
      `runtime/` 无任何导入。
- [ ] `BaseLLMClient.stream()` 签名返回 `AsyncIterator[LLMStreamEvent]`，
      入参为中立 `PromptBlock` / `ToolSpec`；抽象成员仍是 3 个
      （编码为客户端私有方法，见 R3 注）；`OpenAIClient` 的编码方法
      可独立单测（不依赖其流式实现）。
- [ ] assembler 类级 docstring 已写明 R4.1 index 分配与 R4.2 start/stop 配对规则。
- [ ] **Phase 0 基线已建立**：`tests/` 与 `conftest.py` 从零创建，
      覆盖改造前的 Anthropic 事件序列（单轮、多轮 tool use、thinking 交错三种），
      先跑通再动生产代码。
- [ ] 改造后基线仍通过；**唯一允许的差异是主动变更的三点**——R4 的块级语义
      （index 由「每次调用重置」变为「turn 内递增」、start/stop 配对），
      以及 tool_use 入参 JSON 非法时由「兜底 `{}`」改为抛 `AgentException`
      （`llm/types.py:208-211` 的既有静默兜底，违反项目约定，不原样搬入新代码）。
      三点均需显式断言新语义，不得以「测试挂了就改断言」糊过去。
- [ ] 新增单测：Anthropic 客户端逐 chunk 适配、tool_use 入参累积、
      `LLMTurnResult` 三项断言；assembler 在多轮 tool use 下 index
      全局递增不重复的断言；两家 `_encode_tools` / `_encode_system` 形状断言。
- [ ] 冒烟验证：`_log_cache_usage` 仍报出 cache 命中，证明缓存断点
      未在 `PromptBlock` 编码过程中丢失。
- [ ] 如实报告执行的测试命令与通过项、失败项。

## 执行分期

- **Phase 0（基线）**：建 `tests/` 与 `conftest.py`，用录制的 Anthropic chunk 序列
  驱动现有 `AnthropicStreamTranslator`，把当前事件序列固化为基线。
  此期**不改任何生产代码**。
- **Phase 1（入站）**：R1 + R2 + R4。改动集中在 `llm/base.py`、`llm/types.py`、
  `llm/anthropic_client.py`、`llm/openai_client.py`（仅签名）、
  新增 assembler、`runtime/{agent,subagent,compact}.py`。
  本期只改 `stream()` 的返回类型，入参保持现状形状。
- **Phase 2（出站）**：R3。改动集中在 `llm/base.py`、`llm/anthropic_client.py`、
  `tools/{base,registry}.py`、`prompt/compose.py`、`runtime/agent.py`。
  本期才把 `stream()` 入参换成 `PromptBlock` / `ToolSpec`。

签名分两期改是为了让每期结束都有可运行、可冒烟的状态；一次改完则中间态
无法运行，出问题只能整体回退。详见 `implement.md`。

Phase 1 / Phase 2 结束各跑一次相关测试与完整测试。

## 备注

- assembler 落位已定：`runtime/stream_assembler.py`（design 第二节——
  `llm/types.py:4` 已 import `core.events`，放 `core/` 必成循环导入）。
- 规划产物已齐：`design.md`（类型定义、签名、职责边界、边界情况）
  与 `implement.md`（分期执行清单、门禁、回滚点），可进入 `task.py start` 评审。
- 本版为第二版：初版曾设计 `StreamTranslator` ABC + `create_translator()` 工厂，
  经核查「翻译器跨调用状态」前提不成立后废弃，改为翻译私有化 + 中立 assembler。
  记录于此以免后续重新引入该设计。




