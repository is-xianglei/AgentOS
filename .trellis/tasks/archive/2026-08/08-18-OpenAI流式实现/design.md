# 技术设计：OpenAI 流式实现 + 消息体中立化

> 依据 `.trellis/spec/backend/llm-provider-guidelines.md`（§3.6 流式协议分层、§4 错误矩阵、
> §7 Wrong vs Correct）。全链路图见 `research/provider-architecture.html`。

## 1. 设计边界

| 层 | 归属 | 本次改动 |
|---|---|---|
| 出站编码（中立 → wire） | 各 Client 私有方法，**刻意不上 ABC** | 新增消息编码，两家各一份 |
| 入站适配（wire → 中立事件） | 各 Client 私有类 | 新增 `_OpenAIChunkAdapter` |
| 协议装配（中立事件 → 前端块协议） | `runtime/TurnEventAssembler` | **不改** |
| 落库形状 | `session_messages.content` | 改为全量自有协议 |

**明令禁止**（spec §3.6）：不建 `StreamTranslator` ABC，不建 `create_translator()` 工厂，
不把编码方法提上 `BaseLLMClient`。新增 provider 只写一个客户端类。
`runtime/` / `tools/` / `prompt/` 对 `anthropic` 保持零引用。

## 2. 已核实的 SDK 事实

本节结论全部来自本机 `.venv/lib/python3.14/site-packages/openai` 源码，版本 **3.0.0**
（`_version.py:4`），非训练记忆。带 ⚠ 的三条是反直觉项，直接决定了下面的路径选择。

### 2.1 ⚠ `.stream()` 强制要求 strict 工具

`AsyncCompletions.stream()`（`resources/chat/completions/completions.py:3158`）在方法体
第一行调用 `_validate_input_tools(tools)`（同文件 `:3226`）。该函数
（`lib/_parsing/_completions.py:65-83`）对每个工具断言：

```python
strict = tool["function"].get("strict")
if strict is not True:
    raise ValueError(f"`{tool['function']['name']}` is not strict. ...")
```

而项目现有 `OpenAIClient._encode_tools()`（`llm/openai_client.py:51`）产出的 function 工具
**不带 `strict` 字段**。也就是说：一旦带工具调用 `.stream()`，在发出任何 HTTP 请求之前
就会抛 `ValueError`。

同一校验只出现在 `parse()`（`:179` / `:1790`）与 `stream()`（`:3226`）三处，
**`create()` 不做此校验**。

### 2.2 ⚠ `get_final_completion()` 会在截断时抛异常

无论走 stream manager 还是 `ChatCompletionStreamState`，`get_final_completion()` 最终都落到
`parse_chat_completion()`（`lib/_parsing/_completions.py:98-103`），其中无条件检查：

```python
if choice.finish_reason == "length":
    raise LengthFinishReasonError(completion=chat_completion)
if choice.finish_reason == "content_filter":
    raise ContentFilterFinishReasonError()
```

即被 `max_completion_tokens` 截断时**拿不到已生成的内容**——异常取代了返回值。
这与 Anthropic 侧 `get_final_message()` 的行为不同：后者正常返回，
`stop_reason="max_tokens"` 由调用方判读。

### 2.3 ⚠ usage 需显式开启，且只在末 chunk 出现

`stream_options` 由调用方原样透传给 `create`（`completions.py:3262`），SDK **不会**自动注入
`include_usage`（全文 grep 无匹配）。累积逻辑
（`lib/streaming/chat/_completions.py:488`）无条件执行
`completion_snapshot.usage = chunk.usage`，故快照 usage 恒等于最后一个 chunk 的 usage。
不传 `stream_options={"include_usage": True}` 时该值为 `None`。

### 2.4 事件与字段位置

- `create(stream=True)` 返回 `AsyncStream[ChatCompletionChunk]`（`completions.py:2905`），
  产出**裸 chunk**。
- `ChoiceDelta`（`types/chat/chat_completion_chunk.py:74`）字段仅
  `content` / `function_call` / `refusal` / `role` / `tool_calls`
  ——**无 reasoning 字段**。
- tool call 的 `id` 只在该次调用的**首个 chunk** 携带
  （`ChoiceDeltaToolCall.id`，`chat_completion_chunk.py:65`，`Optional[str]`）；
  后续 chunk 只带 `index` 与 `function.arguments` 增量。
- `finish_reason` 在 `Choice.finish_reason`（`chat_completion_chunk.py:110`），
  取值 `stop` / `length` / `tool_calls` / `content_filter` / `function_call`。
- usage 字段名：`prompt_tokens` / `completion_tokens`
  （`types/completion_usage.py:50,53`），
  缓存读取在 `prompt_tokens_details.cached_tokens`（同文件 `:43`）。

## 3. 路径选择：裸 chunk 而非 stream helper

| 路径 | 结论 |
|---|---|
| A. `.stream()` helper | ❌ 需给所有工具加 `strict: True`。strict 模式对 JSON Schema 有额外约束（全字段 required、`additionalProperties: false`），会倒逼修改全部内置工具的 schema——改造半径远超本任务，且 Anthropic 侧 `_encode_tools` 同样不带 strict，加上去两家就不对称了。 |
| B. `create(stream=True)` 裸 chunk + 私有适配器 | ✅ **选定**。 |
| C. `create(stream=True)` + `ChatCompletionStreamState` | ❌ 不传 `input_tools` 时 `parsed_arguments` 恒为 `None`，等于只借了个累加器；而 `get_final_completion()` 仍会因 §2.2 抛异常。收益为负。 |

选 B 的四条理由：

1. **结构与 Anthropic 侧完全对称**——两家都是「裸 chunk → 模块私有适配器 → 中立事件」，
   `_AnthropicChunkAdapter` 是现成的形状参照，读代码的人不需要记两套心智模型。
2. **不受 strict 约束**，工具 schema 一行不用改。
3. **JSON 解析权在我们手里**，才做得到 R1.6 要求的中文 `AgentException`
   （SDK 自己解析时抛的是裸 `json.JSONDecodeError`，且无工具名上下文）。
4. **绕开 §2.2 的截断陷阱**——`finish_reason="length"` 由适配器翻成
   `stop_reason="max_tokens"` 正常返回，已生成的文本不丢。

## 4. `_OpenAIChunkAdapter` 设计

模块私有类，生命周期为单次 `stream()` 调用，形状对标
`_AnthropicChunkAdapter`（`llm/anthropic_client.py:76`）。

### 4.1 block_ref 的分配

Anthropic 的 chunk 自带 `index`，适配器直接沿用。OpenAI **没有块索引概念**：
文本是 `delta.content` 的连续增量，工具调用有自己的 `tool_calls[].index`
（从 0 起，与文本无关）。因此适配器需自建 block_ref 分配：

- 文本块：首次收到非空 `delta.content` 时占用 `ref=0`，后续文本增量复用。
- 工具块：`ref = 1 + tool_call.index`，避开文本占用的 0。

`block_ref` 只需**在单次调用内唯一**（`TurnEventAssembler` 负责映射成 turn 级
全局递增 index），故此分配是安全的。文本块恒定占 0 即便本轮无文本——
不产出 `BLOCK_START` 就不会有空块，装配器也不会为未开的块分配 index。

### 4.2 事件产出规则

| OpenAI chunk 特征 | 产出的中立事件 |
|---|---|
| 首个非空 `delta.content` | `BLOCK_START(ref=0, TEXT)` + `TEXT_DELTA` |
| 后续非空 `delta.content` | `TEXT_DELTA(ref=0)` |
| `delta.tool_calls[i]` 首次出现（带 `id`） | `BLOCK_START(ref=1+i, TOOL_USE)`，记录 id/name |
| `delta.tool_calls[i]` 后续（仅 arguments） | 无事件，累积 `arguments` 字符串 |
| `finish_reason` 到达 | 收束：文本块发 `BLOCK_STOP`，各工具块发 `TOOL_USE` |
| 末 chunk（`choices` 为空，仅带 usage） | 无事件，记录 usage |
| 流结束 | `TURN_RESULT` |

**工具块不发 `BLOCK_STOP`**——与 Anthropic 侧一致：`TurnEventAssembler` 在处理
`TOOL_USE` 时自行补发 `block_stop`（`runtime/stream_assembler.py`），
provider 侧不参与协议决策。

### 4.3 收束时机

Anthropic 有显式的 `content_block_stop`，OpenAI 没有。适配器的收束点是
**`finish_reason` 非空的那个 chunk**：此时把累积的工具入参一次性解析并产出
`TOOL_USE` 事件，文本块补 `BLOCK_STOP`。

若流意外中断（无 `finish_reason` 也无末 chunk），适配器在 `stream()` 的
生成器正常退出前不会产出 `TURN_RESULT`，`TurnEventAssembler` 会抛
「LLM 流未产出终局结果」——沿用现有失败语义，不新增分支。

### 4.4 终局信息的来源

**不调用 `get_final_completion()`**（理由见 §2.2）。`TURN_RESULT` 的四个字段
全部由适配器自累积：

- `text`：累加的 `delta.content`。
- `tool_uses`：累积的 tool_calls 解析结果。
- `usage`：末 chunk 的 `chunk.usage`，映射
  `prompt_tokens → input_tokens`、`completion_tokens → output_tokens`、
  `prompt_tokens_details.cached_tokens → cache_read_input_tokens`；
  `cache_creation_input_tokens` 恒 0（OpenAI 无显式缓存写入概念，
  `core/events.py` 的 `Usage` 该字段本就缺省 0）。
- `stop_reason`：`finish_reason` 原样带出，不改写成 Anthropic 的词汇表
  （`stop_reason` 是 `str | None`，无枚举约束；跨 provider 强行归一
  会丢失 `content_filter` 这类各家独有的语义）。

### 4.5 THINKING_DELTA 恒不产出

`ChoiceDelta` 无 reasoning 字段（§2.4）。Chat Completions 协议不回传思考内容，
这是**协议事实而非实现缺失**，须在适配器类 docstring 中写明原因，
避免后续维护者误判为遗漏而去"补齐"。

### 4.6 错误处理

| 情形 | 行为 |
|---|---|
| 工具入参 JSON 非法 | 抛中文 `AgentException`，details 带 `tool_name` / `tool_call_id`（对标 `anthropic_client.py:158`） |
| tool_call 缺 `id` 或 `name` | 抛中文 `AgentException`——无静默兜底，缺 id 会让后续 tool_result 无法配对 |
| `finish_reason="content_filter"` | 正常产出 `TURN_RESULT`，`stop_reason` 原样带出，由上层判读 |

## 5. `stream()` 方法骨架

```python
async def stream(self, messages, system_prompt=None, tools=None, model=None):
    client = AsyncOpenAI(
        base_url=self.base_url,
        api_key=self.api_key,
        max_retries=self.max_retries,
        # None 会被 SDK 理解为「禁用超时」，转 NOT_GIVEN 保留默认超时计算。
        timeout=self.timeout if self.timeout is not None else NOT_GIVEN,
    )
    adapter = _OpenAIChunkAdapter()
    raw_stream = await client.chat.completions.create(
        model=model or self.model,
        # messages 入参是中立形状，在此编成 OpenAI wire（见 §6.4）。
        messages=[system 消息] + self._encode_messages(messages),
        tools=self._encode_tools(tools),
        max_completion_tokens=settings.llm_default_max_tokens,
        stream=True,
        # 不传则 usage 恒为 None（见 §2.3）。
        stream_options={"include_usage": True},
    )
    async for chunk in raw_stream:
        for event in adapter.adapt(chunk):
            yield event
    for event in adapter.finalize():
        yield event
```

`_encode_messages()` 是本次新增的 Client 私有方法，与 `_encode_system()` /
`_encode_tools()` 并列——三者都是「中立 → 本家 wire」的出站编码，
同属 spec §3.6 中「刻意不上 ABC」的那一类。

与 Anthropic 侧的两处结构差异，均有确切原因：

1. **system 走 messages 而非独立参数**——OpenAI 的 system 是 messages 数组里
   `role="system"` 的首条，非顶层参数。`_encode_system()` 已产出纯文本，
   在此包成消息。空串时不插入该条消息。
2. **终局事件由 `finalize()` 产出而非 `get_final_completion()`**——见 §2.2 与 §4.4。

`tools` 为空列表时不传该参数（部分兼容网关对空 `tools: []` 报 400）。

## 6. 消息体中立化

### 6.1 现状：同一张表两套语义

`session_messages.content` 当前混着存：

| role | 现状存的东西 | 问题 |
|---|---|---|
| `assistant` | Anthropic wire 原文（`{"type":"tool_use","id":...,"input":{}}`） | provider 形状泄漏进库 |
| `tool` | 自有协议 `ToolResultMessage`（带 Anthropic 没有的 `tool_name`/`input_args`） | 读取时又被翻译回 Anthropic 的 `tool_result` 块 |

后果：切到 OpenAI 后，第一轮纯文本能出字，第二轮带工具历史回灌时
OpenAI 收到 Anthropic 形状的 messages 直接 400。这是本任务必须
连着 `stream()` 一起做（而非拆成两个任务）的根本原因。

### 6.2 中立块结构

assistant 消息的 content 为块数组，两类块：

```python
{"kind": "text", "text": "..."}
{"kind": "tool_use", "id": "...", "name": "...", "input": {...}}
```

用 `kind` 而非 `type` 作判别键，是为了让**残留的 Anthropic 形状数据在读取时
必然失败而非被误读**——两家的 `type` 取值恰好同名（都叫 `tool_use`），
若沿用 `type`，旧数据会被静默当成新格式处理，
入参字段 `input` 恰好也同名，错误会一路潜伏到运行时。换 `kind` 让它当场暴露。

`tool` 消息沿用现有 `ToolResultMessage`（`llm/types.py`），**形状不变**——
它已经是自有协议，本次只是不再把它翻译成 Anthropic 的 `tool_result` 块。

### 6.3 `provider_content` 的删除

`LLMTurnResult.provider_content`（`llm/types.py:84`）连同其三行字段注释一并删除。
该注释明写「消息体中立化属另一改造半径，本期不做；不得再新增第二个同类透传字段」
——本任务即是它预告的那次改造，注释随字段一起失效。

生产端：`anthropic_client.py:186` 的 `provider_content=content` 移除。
消费端与其余引用点由 §7 的实施顺序逐一处理。

### 6.4 存量数据与快照形状

**存量历史数据：不处理。** 用户已确认交付时清空数据库，
故不做数据迁移、不做兼容读、**也不写旧格式检测分支**——
为一个不会发生的场景写校验分支属多余代码，
且那个分支永远不会被测试覆盖到。

**但快照是另一个问题，清库解决不了。** 已核实的链路：

```
load_context_for_turn()          → 产出「已翻译成某家 wire 形状」的上下文
  └→ maybe_compact()             → agent.py:618 / :1606
       └→ _auto_compact()        → add_snapshot(compacted)   # compact.py:47-55
            └→ session_snapshots.messages
                 └→ 下轮 deepcopy(snapshot.messages) 直接并入   # service.py:432,448
```

快照存的是**出站翻译的产物**，不是中立形状。这带来两个后果：

1. **切 provider 后旧快照形状不匹配**。同一会话先用 Anthropic 跑出快照，
   改配置切到 OpenAI，下一轮 `deepcopy` 会把 Anthropic wire 形状的消息
   直接并进发给 OpenAI 的上下文——正是本任务要消灭的那类 400。
   这不是历史遗留，是运行时**持续产生**的。
2. `_micro_compact` 遍历的也是翻译后的形状（`compact.py:78,93,103`
   按 `type=="tool_result"` / `"tool_use"` / `tool_use_id` 判别）。

**选定：出站翻译收进 `stream()` 内部，调用方一路传中立消息。**

`_messages_to_context()` 不再产出 wire 形状，改产中立消息；
compact 在中立形状上工作、落中立快照；翻译发生在各 Client 的
`stream()` 方法里（编码 messages 那一步，与已有的 `_encode_system` /
`_encode_tools` 并列）。

这样定位而非「在 agent.py 调用前翻译」，是因为 `llm.stream()` 有 **4 个调用点**
（`agent.py:630`、`agent.py:1614`、`subagent.py:226`、`compact.py:122`），
其中 `subagent.py` 自建 messages、根本不走 `_messages_to_context`。
若把翻译放在调用方，这 4 处每处都要记得翻一次，漏一处就是一个 400，
而且新增第 5 个调用点时无人提醒。收进 Client 内部则调用方无从出错——
**`BaseLLMClient.stream()` 的入参契约就是中立消息**，这也与
「出站编码是 Client 私有方法」（spec §3.6）的既定分层一致。

收益：

- 快照与 `session_messages` 形状一致，都是自有协议；
- 切 provider 后旧快照仍可用，无需清库；
- `_micro_compact` 改读中立字段名后语义稳定，不随 provider 变；
- 4 个调用点无一需要关心 provider 形状。

代价：`stream()` 的 messages 入参语义从「provider 形状」变为「中立形状」，
是一处契约变更，须在 `BaseLLMClient.stream()` 的 docstring 中写明。

## 7. 改动点全清单

调研已穷尽排查（覆盖 `provider_content` 全部 8 处出现、`"tool_result"` /
`"tool_use"` / `tool_use_id` / `is_error` 四组字面量的全仓库分布）。
**实际改动点为 9 处，比初估的 5 处多出 4 处**，多出的部分单列在 7.2。

### 7.1 核心链路（初估内）

| # | 位置 | 现状 | 改动 |
|---|---|---|---|
| 1 | `llm/openai_client.py:116` | `stream()` 抛 `NotImplementedError` | 实现 + 新增 `_OpenAIChunkAdapter` |
| 2 | `llm/types.py:84` | `provider_content` 字段 + 3 行注释 | 删除 |
| 3 | `llm/anthropic_client.py:186` | `provider_content=content` 唯一生产点 | 移除，改产中立块 |
| 4 | `session/service.py:503-522` | tool 消息编码成 Anthropic `tool_result` 块 | 改为出站翻译，按 provider 分派 |
| 5 | `runtime/agent.py:642` / `:1623` | 两处读 `provider_content` 落库 | 改存中立块 |
| 6 | `runtime/subagent.py:308-318` | 自建 Anthropic 形状 tool_result | 改产中立形状 |
| 7 | `runtime/compact.py:78,93,103` | 按 `type=="tool_result"` / `"tool_use"` / `tool_use_id` 遍历 | 改读中立字段名 |

### 7.2 调研新发现（初估外，必须一并处理）

| # | 位置 | 风险 |
|---|---|---|
| 8 | `runtime/subagent.py:362` / `:582` | 子代理与 teammate 循环的**内存态** messages 直接塞 `provider_content`。`:582` 那条更严重——它经 `subagent.py:480` `json.dumps` **落库到 `team_members.history`**，是第二个持久化 provider 形状的地方，`_load_history()`（`:503`）读回后直接续用。 |
| 9 | `tools/subagents/trigger.py:54-62` | `_iter_tool_uses()` 按 `block.get("type") == "tool_use"` 解析 Stop hook 上下文。中立化后**匹配不到任何块**，`verification_trigger` hook 静默失效——不报错，只是再也不触发。 |
| 10 | `memory/jobs/extraction.py:620-628` | assistant 分支按 `block.get("type") == "text"` 提取文本。中立化后 `text_parts` 恒空，**记忆抽取静默拿到空内容**，同样不报错。 |
| 11 | `session/service.py:432,448` + `runtime/compact.py:47-55` | `session_snapshots.messages` 存的是**已编码的 provider 形状上下文**（压缩产物），`deepcopy` 后直接并入上下文。**清库解决不了**——这是运行时持续产生的写路径，且导致切 provider 后旧快照形状不匹配。按 §6.4 把出站翻译收进 `stream()`。 |

第 9、10 项是本次调研最有价值的发现：二者都是**静默失效**——不抛异常、不留日志，
只是功能悄悄不工作了。若按初估的 5 处改动实施，这两个缺陷会带着通过全部测试
进入主干。它们也是 §6.2 选择 `kind` 而非 `type` 作判别键的直接理由之一：
写路径改了、读路径没改时，同名键会让错误潜伏，异名键至少让数据对不上。

### 7.3 接口影响面

`session/api.py:94-108` 的 `GET /{session_id}/messages` 把 `content` 原样
（`Any`）透给前端。中立化后前端收到的 assistant 消息块形状会变
（`type` → `kind`）。当前仓库无前端工程，此处仅作记录，
不在本任务范围内做适配。

## 8. 测试策略

现有 27 个用例（`test_stream_assembler.py` 13 + `test_stream_baseline.py` 8 +
`test_encoders.py` 6）中，**只有 1 个**直接受影响：
`test_stream_baseline.py:168` 断言 `result.provider_content == [...]`，
随字段删除一并移除。其余 26 个不碰该字段。

**关键事实：本次改造的核心路径当前零测试覆盖**——`_messages_to_context`、
`_micro_compact`、`agent.py` / `subagent.py` 的落库与消息构造均无存量测试。
故新增测试不是"补充"，而是这些路径的**首次覆盖**。

新增测试文件与落点：

| 文件 | 覆盖 |
|---|---|
| `tests/fixtures/openai_chunks.py` | 手写 OpenAI 裸 chunk 序列，对标 `anthropic_chunks.py`：纯文本 / 文本+工具 / 多工具并发 / 坏 JSON / 末 chunk 带 usage |
| `tests/test_llm_openai_client.py` | 适配器单测：事件序列、block_ref 分配、usage 映射、坏 JSON 抛中文异常 |
| `tests/test_message_neutral.py` | 中立块 ↔ 两家 wire 的往返等价（AC5）、完整会话历史双向编码（AC6） |

`test_encoders.py` 是消息出站编码新测试的自然落点（该文件已有 system/tools
编码测试，形状一致）；现有 6 个用例本身不需改。

测试模式照搬 `test_stream_baseline.py` 的 `_ev()` / `_run_call()` / `_new_assembler()`
三件套：适配器每次新建、装配器由外部传入，不建真实 SDK 连接。

## 9. 兼容性与回滚

- **无 DDL 变更**：`session_messages.content` 与 `session_snapshots.messages`
  都是 JSONB，结构变更不涉及 Alembic 迁移。
- **回滚形态**：本任务在单一分支 `feature/unify-llm-client` 上完成，
  回滚即 `git revert` 该批提交；无数据迁移，故无需反向迁移脚本。
- **存量数据**：用户确认交付时清空数据库，故不做迁移、不做兼容读、
  不写旧格式检测分支（prd Constraints 第 5 条）。
- **快照与 provider 切换**：按 §6.4，出站翻译收进 `stream()`，
  库中（`session_messages` / `session_snapshots` / `team_members.history`）
  一律只存中立形状，切 provider 无需清库。
- **spec 同步**：`llm-provider-guidelines.md` §3.4 的能力边界描述与 §6.2
  的「断言抛 `NotImplementedError`」测试要求在本任务后失效，
  须在 Phase 3.3 一并更新。
