# 设计：Provider 流式协议翻译抽象

对应 `prd.md`。本文定义类型、签名与职责边界；执行清单见 `implement.md`。

## 一、设计原则

1. **provider 抽象只有 `BaseLLMClient` 一个。** 不为流翻译另立接口。各家在
   `stream()` 内部把自己的 wire 格式消化成中立事件，怎么消化是私有实现。
2. **中立事件不含 runtime 概念。** `LLMStreamEvent` 里没有 `actor`、`session_id`、
   全局 `index`——这些是会话层的东西，下沉进 `llm/` 就是分层倒挂。
3. **自有协议的装配职责归 runtime 侧的中立装配器。** index 分配、块边界配对由
   `TurnEventAssembler` 用代码保证，而非各 provider 需遵守的 docstring 契约。
4. **不改 `core/events.py` 的数据结构。** 只改产出这些事件的语义。

## 二、落位与依赖方向

现有依赖是 `llm/types.py` → `core/events.py`（`llm/types.py:4` 已 import
`core.events`）。因此**装配器不能放 `core/`**——`core/` 若 import `llm.types`
就形成循环。装配器放 `runtime/`，依赖方向保持单向：

```
core/events.py          （对外协议，零依赖）
      ↑           ↑
llm/types.py            runtime/stream_assembler.py
（中立出口类型）    ↑            ↑
llm/anthropic_client.py    runtime/{agent,subagent}.py
（私有适配）
```

| 新增 / 改动 | 文件 | 说明 |
| --- | --- | --- |
| 新增 `LLMStreamEvent`、`LLMTurnResult` | `llm/types.py` | provider 侧中立出口类型 |
| 新增 `ToolSpec`、`PromptBlock` | `llm/types.py` | 出站方向的中立输入类型 |
| 新增 `TurnEventAssembler` | `runtime/stream_assembler.py`（新建） | 唯一一份，provider 无关 |
| 改签名 | `llm/base.py` | `stream()` 返回类型 + 两个编码方法 |
| 私有化适配 | `llm/anthropic_client.py` | 现 `AnthropicStreamTranslator` 迁入并降级 |
| 仅改签名 | `llm/openai_client.py` | 流式仍 `NotImplementedError` |
| 去 Anthropic 化 | `runtime/{agent,subagent,compact}.py` | 见第七节 |
| 产出中立描述 | `tools/{base,registry}.py`、`prompt/compose.py` | 见第五节 |

`llm/types.py` 中现有的 `chunk_type` / `chunk_text` / `extract_tool_uses` /
`text_from_content` 四个 Anthropic 专用解析函数随适配器一并迁入
`llm/anthropic_client.py` 并转为模块私有（`_`前缀），不再对外导出。

## 三、中立事件类型（`llm/types.py`）

```python
class LLMEventKind(str, Enum):
    """中立流事件种类。刻意与 core.events.EventType 分开：
    前者是 provider 出口，后者是对外协议，二者不应互相绑定演进。"""

    BLOCK_START = "block_start"      # 内容块开始
    TEXT_DELTA = "text_delta"        # 文本增量
    THINKING_DELTA = "thinking_delta"  # 思考增量
    TOOL_USE = "tool_use"            # 工具调用（入参已解析完整）
    BLOCK_STOP = "block_stop"        # 内容块结束
    TURN_RESULT = "turn_result"      # 本次调用终局（流的最后一个事件）


@dataclass(frozen=True)
class LLMStreamEvent:
    """provider 适配后的中立流事件。

    不含 actor / session_id / 全局 index —— 那是 runtime 的装配职责。
    block_ref 是**单次 stream() 调用内**的块序号，由 provider 自行分配、
    从 0 递增即可；跨调用的全局唯一性由 TurnEventAssembler 负责。
    """

    kind: LLMEventKind
    block_ref: int | None = None
    block_type: BlockType | None = None   # 仅 BLOCK_START
    text: str = ""                        # 仅 TEXT_DELTA / THINKING_DELTA
    tool: ToolInfo | None = None          # 仅 TOOL_USE
    result: "LLMTurnResult | None" = None  # 仅 TURN_RESULT


@dataclass(frozen=True)
class LLMTurnResult:
    """单次 LLM 调用的终局信息，取代原先 runtime 自行解析 message_final。"""

    text: str = ""
    tool_uses: list[ToolUse] = field(default_factory=list)
    usage: Usage | None = None
    stop_reason: str | None = None
    # 遗留承载：assistant 消息落库仍是 provider 形状的 content 块数组，
    # 由客户端原样带出给 runtime 落库。中立化见第十节，本期不做。
    provider_content: list[dict[str, Any]] = field(default_factory=list)
```

`BlockType`、`ToolInfo`、`Usage` 复用 `core/events.py` 现有定义，不另造一套——
它们本就是 provider 无关的值对象，重复定义只会带来两套转换。

**契约**：每次 `stream()` 必须以恰好一个 `TURN_RESULT` 事件收尾，且 `result`
非空。提前耗尽而无 `TURN_RESULT` 视为 provider 实现错误，由消费方抛
中文 `AgentException`，不静默兜底。

## 四、出站中立类型（`llm/types.py`）

```python
@dataclass(frozen=True)
class ToolSpec:
    """中立工具描述。各家客户端自行编码成本家 tools 参数形状。"""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptBlock:
    """中立 system prompt 块。

    cache_breakpoint=True 表示「此块末尾是一个缓存断点」，语义中立；
    Anthropic 编码为 cache_control={"type": "ephemeral"}，
    不支持缓存的 provider 直接忽略该标志并拼接纯文本。
    """

    text: str
    cache_breakpoint: bool = False
```

`prompt/compose.py:48` `compose_lead_blocks()` 现在直接产出带
`cache_control` 的 `TextBlockParam`（`compose.py:60-75`），改为产出
`list[PromptBlock]`，两个断点标志原样保留（稳定段、易失段各一个）。
其 docstring 里关于「渲染顺序 tools → system → messages」与 5m TTL 的说明
属 Anthropic 缓存行为，随编码逻辑一起搬到 `AnthropicClient._encode_system()`。

`tools/base.py:36` `to_param()` 改名 `to_spec()` 返回 `ToolSpec`；
`tools/registry.py:42` `to_anthropic_tools()` 改名 `to_specs()` 返回
`list[ToolSpec]`。二者都不再 import `anthropic.types`。

## 五、`BaseLLMClient` 新契约（`llm/base.py`）

> **对 PRD R3 的修正**：PRD 把 `encode_tools` / `encode_system` 列为**公开抽象方法**。
> 设计时发现这重犯了翻译器那个错误——一旦 `stream()` 直接收中立类型，编码就纯属
> 实现细节，公开它只会让调用方多一步「先编码再调用」的编排，也多一组各家必须对齐的
> 签名。**改为私有**：编码在各客户端内部完成，ABC 上不出现。R3 相应收窄为
> 「出站入参中立化」，而非「新增两个抽象方法」。

```python
class BaseLLMClient(ABC):
    @property
    @abstractmethod
    def provider(self) -> str: ...          # 不变

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | list[PromptBlock] | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """流式调用，产出中立事件。实现为 async generator，故不声明 async。

        入参已是中立类型，编码为本家 wire 形状由实现内部私有完成
        （`_encode_system` / `_encode_tools`），调用方不参与。
        必须以恰好一个 TURN_RESULT 事件收尾。
        """

    @abstractmethod
    async def complete_structured[T: BaseModel](...) -> T: ...   # 不变
```

参数名 `system_prompt` 与 `str` 入参形态都保留：子代理与 teammate 的系统提示
是 `compose_subagent_prompt()` 拼出的整段纯文本（`subagent.py:120-123`），
没有 cache 断点诉求，硬包成单元素 `PromptBlock` 列表只是噪音；
`str` 由 `_encode_system` 内部归一化。改名同理——纯改名不带来任何收益，
却要动全部调用点，属无关重构。

抽象面净变化：**方法数量不变**（仍是 3 个），只有 `stream()` 的入参与出参类型
由 Anthropic 形状换成中立类型。这是本设计相对初版方案最重要的收敛——
provider 抽象没有变大。

**签名分两期落地**：Phase 1 只改返回类型（`AsyncIterator[LLMStreamEvent]`），
入参保持现状的 `str | list[dict]`；Phase 2 才把入参换成
`PromptBlock` / `ToolSpec`。理由是每期结束都要有可运行、可冒烟的状态——
一次改完则中间态无法运行，出问题只能整体回退，二分不出是哪一侧引入的。
执行细节见 `implement.md`。

## 六、`TurnEventAssembler`（`runtime/stream_assembler.py`）

```python
class TurnEventAssembler:
    """把 provider 中立事件装配成对外协议 StreamEvent。

    一个 turn 一个实例，跨该 turn 内多次 stream() 调用存活——这是它存在的
    唯一理由：block index 需在 turn 内全局递增（见 prd R4.1）。

    block index 语义：turn 内从 0 单调递增，全局唯一，可作为消费方的分组 key。
    不透传任何 provider 的原始 index。
    块边界语义：block_start / block_stop 严格成对，tool_use 块也补发 block_stop。
    """

    def __init__(self, actor: Actor, session_id: int) -> None: ...

    async def consume(
        self, stream: AsyncIterator[LLMStreamEvent]
    ) -> AsyncIterator[StreamEvent]: ...

    @property
    def last_result(self) -> LLMTurnResult: ...
```

**为什么是 `consume(stream)` 而不是 `stamp(event)`**：装配器需要知道「一次
`stream()` 调用」的边界，才能把 provider 的调用内 `block_ref` 映射到 turn 内
全局 index。逐事件 `stamp()` 无法可靠识别边界；包住整个流则边界天然等于
一次 `consume()` 调用。runtime 侧也因此少写一层 for 循环。

内部状态：

| 字段 | 作用 | 生命周期 |
| --- | --- | --- |
| `_next_index: int` | 全局 index 分配游标 | **turn**（跨 consume 调用累加） |
| `_ref_to_index: dict[int, int]` | 本次调用 `block_ref` → 全局 index | 单次 consume，进入时清空 |
| `_open: set[int]` | 本次调用已开未闭的全局 index | 单次 consume，退出时兜底补 stop |
| `_last_result: LLMTurnResult \| None` | 最近一次 TURN_RESULT | 每次 consume 覆写 |

装配规则：

1. `BLOCK_START` → 分配 `_next_index++`，登记映射，产出
   `StreamEvent.block_start(actor, session_id, index, block_type)`。
2. `TEXT_DELTA` / `THINKING_DELTA` → 查映射，产出对应 delta 事件。
3. `TOOL_USE` → 产出 `StreamEvent.tool_use(...)`，**随后补发
   `StreamEvent.block_stop(...)`**（R4.2 方案 A），并从 `_open` 移除。
4. `BLOCK_STOP` → 产出 `block_stop`，从 `_open` 移除。
5. `TURN_RESULT` → 不产出协议事件，仅记入 `_last_result`。
6. 流结束时 `_open` 非空 → 为每个残留 index 补发 `block_stop` 并记 warning
   日志（provider 实现异常的兜底，不静默吞）。
7. 未见 `TURN_RESULT` 即流结束 → 抛中文 `AgentException`。
8. `block_ref` 查不到映射，分两种：`TEXT_DELTA` / `THINKING_DELTA` / `TOOL_USE`
   抛中文 `AgentException`（丢弃等于静默丢内容，消费方永远拼不齐）；
   `BLOCK_STOP` 记 warning 后丢弃（关一个从没开过的块，丢掉不损失任何信息，
   为此炸掉整个 turn 不划算）。现有实现（`llm/types.py:203-205`）对后者是
   照发不误，会让前端收到 index 对不上的孤儿 stop，本次一并收紧。

`turn_start` / `turn_end` / `tool_result` / `interaction_request` / `error`
仍由 runtime 自己 emit，装配器不碰——它只负责「LLM 一次调用产出的内容块」
这一段，职责边界与现有 `AnthropicStreamTranslator` 的 docstring 保持一致。

## 七、`AnthropicClient` 内部适配

现 `AnthropicStreamTranslator`（`llm/types.py:118-226`）迁入
`llm/anthropic_client.py`，改名 `_AnthropicChunkAdapter`（模块私有），
并作如下调整：

- **生命周期由 turn 缩短为单次调用**：在 `stream()` 内部 `new` 一个，
  流结束即弃。原先跨调用共用的写法在核查中已确认无实际依赖（见 prd 结论二）。
- **产出 `LLMStreamEvent` 而非 `StreamEvent`**：不再需要 `actor` / `session_id`
  构造参数，`__init__` 变为无参。
- **`index` 降级为 `block_ref`**：仍直接用 Anthropic 原始 index 作为调用内序号
  （调用内它本就唯一），全局化交给装配器。原 docstring 中「每次调用从 0 重来、
  不可当全局唯一标识」的告警随之删除——那个坑已由装配器消除。
- **`message_final` 转为 `TURN_RESULT` 事件**：原 `_on_message_final` 只记录
  `usage` / `stop_reason`；现额外用迁入的私有 `_extract_tool_uses` /
  `_text_from_content` 解析 `content`，组装完整 `LLMTurnResult` 后产出。
- **tool_use 块不再吞掉 stop**：适配器照常产出 `TOOL_USE` 事件即可，
  补发 `block_stop` 是装配器的职责（R4.2 A），provider 侧不参与协议决策。
- **顺带修掉一个静默兜底**：现实现里 tool_use 入参 JSON 解析失败会兜成
  `tool_input = {}`（`llm/types.py:208-211`），工具随后带空参执行，
  失败原因在链路上彻底消失。迁移时改为抛中文 `AgentException`
  （附工具名与 tool_use_id）。这是搬运过程中唯一允许的行为变更，
  因为把已知的静默兜底原样搬进新代码，等于新写一个违反项目约定的实现。

`stream()` 主体（`anthropic_client.py:38-90`）改动：`system` / `tools` 入参
先经私有 `_encode_system` / `_encode_tools` 转成 SDK 形状，SDK 事件不再
`model_dump()` 后原样 yield，而是喂给适配器产出中立事件。
`_log_cache_usage`（`:92-110`）保持不动。

## 八、`OpenAIClient` 现状

`stream()` 仅按新签名调整类型注解，方法体维持
`raise NotImplementedError("OpenAI 流式在后续 Phase 实现")`（`openai_client.py:87`）。
`_encode_system` / `_encode_tools` **本期实现并单测**——它们不依赖流式，
且能提前验证中立类型是否真的够两家用（若 OpenAI 编不出来，说明
`PromptBlock` / `ToolSpec` 设计有缺陷，此时暴露成本最低）。

OpenAI 侧留给后续任务的难点，在此记录以免遗忘：无 content block 概念，
需自行合成块边界；`tool_calls[].index` 是数组下标而非块序号，需另建映射；
text 增量无任何 index。这些都在客户端内部消化，不外溢。

## 九、runtime 三处改造

### 9.1 `runtime/agent.py::_run_llm_loop`

改前（`:567-570`、`:632-645`）：

```python
actor = ORCHESTRATOR_ACTOR
translator = AnthropicStreamTranslator(actor, session_id)   # 跨调用共用
...
system_prompt: list[TextBlockParam] = compose_lead_blocks(...)
tools: list[ToolParam] = active_registry.to_anthropic_tools()
...
    final_content: list[dict[str, Any]] | None = None
    async for chunk in self.llm.stream(context, system_prompt, tools=tools, model=model_override):
        if chunk.get("type") == "message_final":
            final_content = chunk.get("content")
        for event in translator.translate(chunk):
            await self.bus.emit(event)

    stop_reason = translator.stop_reason
    tool_uses = extract_tool_uses(final_content)
```

改后：

```python
actor = ORCHESTRATOR_ACTOR
assembler = TurnEventAssembler(actor, session_id)           # 跨调用共用，为的是 index 连号
...
system_prompt: list[PromptBlock] = compose_lead_blocks(...)
tools: list[ToolSpec] = active_registry.to_specs()
...
    async for event in assembler.consume(
        self.llm.stream(context, system_prompt, tools=tools, model=model_override)
    ):
        await self.bus.emit(event)

    result = assembler.last_result
    stop_reason = result.stop_reason
    tool_uses = result.tool_uses
    final_content = result.provider_content
```

注意「跨调用共用」这一点在改造前后都存在，但**理由变了**：改前的注释声称是为了
「延续 tool_use 入参累积」，核查证明不成立（见 prd 结论二）；改后共用的唯一理由是
装配器要给整个 turn 的块发连号 index，这是真实且可验证的需求。

同函数内另有两处直接读 translator 属性，一并改（2026-08-17 复查补记，
原第九节漏列，Phase 1 若漏改会在此处直接报 `AttributeError`）：

| 位置 | 改前 | 改后 |
| --- | --- | --- |
| `:702` | `translator.stop_reason or "max_iterations"` | `assembler.last_result.stop_reason or "max_iterations"` |
| `:706` | `StreamEvent.turn_end(actor, session_id, stop_reason, translator.last_usage)` | `... , assembler.last_result.usage)` |

`_finalize_without_tools`（`:1594-1637`）同步改造：参数 `translator:
AnthropicStreamTranslator` 改为 `assembler: TurnEventAssembler`，
`system_prompt: str | list[TextBlockParam]` 改为 `str | list[PromptBlock]`，
流消费改用同一形状，`final_content` 取 `assembler.last_result.provider_content`。
调用点 `:692` 传参随之改名。
docstring 里「沿用同一翻译器维持事件连续」这句改成实话——沿用同一装配器，
使收尾这次调用的块 index 接在前面的块之后，不从 0 重来。

`_llm_attribution`（`:1584-1592`）不动：它读的是客户端自报的 `provider` 属性，
本就与流式协议无关。文件头 `from anthropic.types import TextBlockParam, ToolParam`
（`:11`）删除——这是主 Agent 里最后一处 Anthropic 直接依赖。

**plan mode 追加块**（`:602-608`，2026-08-17 复查补记，原第九节漏列）：
`compose_lead_blocks()` 的返回值不是只读的，紧随其后还有一处就地追加——
active plan 存在时把 `render_plan_mode_instructions()` 作为**第三个 system 块**
append 进去。Phase 2 改返回类型后这里的字面量 dict 必须同步换成 `PromptBlock`：

| 位置 | 改前 | 改后 |
| --- | --- | --- |
| `:603` | `system_prompt.append({"type": "text", "text": render_plan_mode_instructions(...)})` | `system_prompt.append(PromptBlock(text=render_plan_mode_instructions(...)))` |

**该块不带 cache 断点**（改前就没有 `cache_control`），故 `cache_breakpoint`
保持默认 `False`，不要顺手加断点：plan 内容随计划编辑而变，是易失段，
给它加断点会把前面两段的缓存前缀击穿。这也顺带说明 `PromptBlock` 的默认值
选 `False` 是对的——追加块天然不该是断点。

### 9.2 `runtime/subagent.py`

两处 `AnthropicStreamTranslator(actor, session_id)`（`:131` 一次性子代理、
`:443` teammate）改为 `TurnEventAssembler(actor, session_id)`，各自的 actor
构造逻辑不变——装配器同样按 actor 打戳，这正是共享 bus 上区分产出者的依据。
两个 ReAct 循环方法的 `translator: AnthropicStreamTranslator` 参数
（`:341` 一次性子代理、`:559` teammate）同步改名换型。

`_stream_and_translate`（`:218-240`）是改动最集中的一处，改后：

```python
async def _stream_and_collect(
    self,
    messages: list[dict[str, Any]],
    system_prompt: str,
    tool_registry: ToolRegistry,
    assembler: TurnEventAssembler,
    rendered_memories: str | None = None,
    model: str | None = None,
) -> LLMTurnResult:
    """跑一次 LLM 流:中立事件经装配器补齐 actor/index 后 emit,返回终局结果。"""
    request_messages = self._with_memory_context(messages, rendered_memories)
    async for event in assembler.consume(
        self.llm.stream(
            request_messages,
            system_prompt,
            tools=tool_registry.to_specs(),
            model=model,
        )
    ):
        await self._emit_event(event)
    return assembler.last_result
```

返回类型由 `list[dict] | None` 变为 `LLMTurnResult`，方法名随之由
`_stream_and_translate` 改为 `_stream_and_collect`——调用方不再需要自己
`extract_tool_uses` / `text_from_content`（`:367,369,372,587,592` 五处一并简化）。
`_emit_event` 的 `bus is None` 判空（`:212-216`）保持不变：装配器不感知 bus，
产出事件后丢不丢由调用方决定。

### 9.3 `runtime/compact.py::_auto_compact`

压缩只要一段摘要文本，不往 bus 发任何事件，因此**不建装配器**——它是验证
「中立流本身可独立消费」的最小样本，也是 `TURN_RESULT` 契约的兜底检查点。

改前（`:121-127`）：

```python
final_content: list[dict[str, Any]] | None = None
async for chunk in self.llm.stream(messages=[MessageParam(role="user", content=compact_prompt)]):
    if chunk_type(chunk) == "message_final":
        final_content = chunk.get("content")
summary = text_from_content(final_content)
```

改后：

```python
result: LLMTurnResult | None = None
async for event in self.llm.stream(messages=[{"role": "user", "content": compact_prompt}]):
    if event.kind is LLMEventKind.TURN_RESULT:
        result = event.result
if result is None:
    raise AgentException.message("自动压缩失败：LLM 流未产出终局结果")
summary = result.text
```

文本拼接由客户端负责，`text_from_content` 不再需要；`MessageParam`（`:4`）与
`chunk_type` / `text_from_content`（`:7`）三个导入一并删除，
compact 从此不依赖任何 Anthropic 类型。

## 十、消息体仍是 provider 形状（本期非目标）

`stream()` 的 `messages` 参数保持 `list[dict[str, Any]]` 不变，assistant 消息
仍以 provider 形状的 content 块数组落库（`agent.py:648-655` 把 `final_content`
原样交给 `session_service.add_message`，`session/service.py:476`
`_messages_to_context` 再原样读回来喂给下一次调用）。

这确实是与「一份自有协议」相冲突的遗留面，但**本期不动**，理由是：

1. 它的改造半径完全不同——涉及 DB 里已有的 JSONB 历史数据、快照
   （`SessionSnapshot.messages`）、compact 产物、teammate 的 `member.history`
   序列化，是一次带数据迁移的改造，与「流式出站协议」不是同一件事。
2. 混在一起做会让本任务无法独立验收：出站事件的正确性可以逐事件断言，
   消息体中立化的正确性只能靠端到端历史回放验证。

代价是 `LLMTurnResult.provider_content` 这个字段——它是这条遗留通道的唯一
出口，字段注释和此节都明确标注，后续任务摘掉它时一眼可见影响面。
现阶段不允许再新增第二个透传 provider 形状的字段。

## 十一、边界情况与错误处理

| 情况 | 处理 | 原因 |
| --- | --- | --- |
| 流中途抛异常（SDK 报错 / 连接断） | 装配器不吞异常、不补 `block_stop`，原样向上抛 | 中断态下补一个 `block_stop` 会让前端误判该块正常收尾；转 `error` 事件是 runtime 既有外层职责（`agent.py:515`、`subagent.py:160/497`） |
| 流正常结束但仍有块未关闭 | 按开启顺序补发 `block_stop`，并 `logger.warning` 记录 provider 与遗留块数 | 内容已完整送达，此时抛错等于丢结果；但静默补齐会掩盖 provider 实现缺陷，故必须留日志 |
| 未收到 `TURN_RESULT`（含空流） | 抛中文 `AgentException`（“LLM 流未产出终局结果”） | 无终局就拿不到 `tool_uses`，静默当成“没有工具调用”会让 ReAct 循环提前收尾，是最难查的一类错 |
| 收到 delta / tool_use 但 `block_ref` 无映射 | 抛中文 `AgentException` | index 编造不出来；丢弃则前端拼不出完整文本 |
| 收到 `BLOCK_STOP` 但 `block_ref` 无映射 | warning 后丢弃 | 关一个从没开过的块不损失信息，不值得炸掉整个 turn |
| tool_use 入参 JSON 解析失败 | 抛中文 `AgentException`，附 tool 名与 id | 现实现兜底成 `{}`（`llm/types.py:210-211`），等于让工具带空参跑，违反「禁止静默兜底」 |
| `TOOL_USE` 之后 | 装配器补发 `block_stop`（R4.2 方案 A） | 让所有块类型的 start/stop 成对，前端无需为 tool_use 写特例 |
| 未 `consume` 就读 `last_result` | 抛中文 `AgentException`（“尚未消费任何 LLM 流”） | 返回空 `LLMTurnResult` 会被 runtime 读成“无 tool_uses”，同上一条一样导致静默截断 |
| 多产出者并发 | 各自持有独立装配器，index 各自从 0 起 | index 的唯一性域是 **(actor, turn)**，不是全局；共享 bus 上前端本就先按 actor 分流（最多 1 主 + 3 子，`settings.subagent_max_concurrency`） |

所有异常一律 `AgentException.message("中文消息", details)`，包装底层异常时用
`raise ... from exc`；装配器自身不 emit `error` 事件，也不碰 bus。

## 十二、测试基线（Phase 0，先于任何改造）

仓库当前**没有 `tests/` 目录、没有任何测试文件**（`pyproject.toml:26-27` 已配
`testpaths = ["tests"]`，但目录不存在）。本任务是纯行为等价重构，没有基线就
无法证明「改完和改前一样」，因此 Phase 0 必须先补齐：

1. `tests/test_stream_assembler.py`——覆盖上表全部 8 种情况，用手写的
   `LLMStreamEvent` 序列驱动，不碰网络。
2. `tests/test_anthropic_adapter.py`——喂录制好的 Anthropic 原始 chunk 序列
   （含 text / thinking / tool_use 三类块、多块交错、`message_final`），
   断言产出的中立事件序列。录制样本以 fixture 形式落在 `tests/fixtures/`，
   **不含任何 API Key**。
3. `tests/test_encoders.py`——两家 client 的 `_encode_system` / `_encode_tools`
   各自的编码结果，重点验证 `PromptBlock.cache_breakpoint` 在 Anthropic 侧
   落成 `cache_control: {"type": "ephemeral"}`，在 OpenAI 侧被正确忽略。

纪律：改造过程中测试挂了，先查实现，**不准改断言迁就实现**；确需调整断言的，
必须在 PR 说明里写清为什么原断言是错的。

## 十三、与 PRD 的差异（需回写 prd.md）

| PRD 原文 | 设计结论 | 理由 |
| --- | --- | --- |
| R3：`BaseLLMClient` 新增 `encode_system` / `encode_tools` 两个**抽象方法** | 改为各客户端的**私有方法** `_encode_system` / `_encode_tools`，抽象方法数仍是 3 | 编码是 `stream()` 的内部步骤，没有任何外部调用方；提成抽象方法等于把实现细节写进契约，是本任务一开始就想清除的那类过度抽象 |
| R4：`LLMTurnResult` 只含 `text` / `tool_uses` / `usage` / `stop_reason` | 追加 `provider_content` | assistant 消息落库仍是 provider 形状（第十节），不给出口就得让 runtime 自己再解一遍原始流 |

这两条已在本文档定稿，需同步回写 prd.md，避免实现时按旧版 R3 落地。

## 十四、已决事项

### 14.1 顺带清理 `continuation_payload` 的死字段 `actor`（决定：本任务做）

`agent.py:1071` 把 `actor.to_dict_compact()` 写进 JSONB，但**全仓无任何读取方**：
恢复路径只读 `additional_context`（`:491`）、`pending`（`:1170`）、
`assistant_message_id`（`:1173`）、`done_tool_use_ids`（`:1176`），
需要 actor 时在 `:1650` 直接重建 `ORCHESTRATOR_ACTOR`；
`interaction/` 域（service / repository / models / schemas）零 `actor` 引用。

**连带清理**：删掉这行写入后，`_suspend_for_interaction` 的 `actor: Actor` 参数
（`:1051`）在方法内再无使用者——它此前只服务于这一行。按「删除功能不留死代码」，
参数与两处调用点的实参（`:903`、`:921`）一并删除。

**不需要 Alembic 迁移**：这是 JSONB 里的一个 key，不是表列。历史行里残留的
`actor` key 无人读取，不影响跨版本恢复——旧暂停点在新代码下恢复时，
读的仍是那四个字段。

放进本任务而非另开任务的理由：它与本任务同属 actor / 事件面，判定其为死字段
所需的全仓核查已经在本任务做完；单独开任务要把同样的核查再做一遍。

### 14.2 `ToolUse` 留在 `llm/types.py`（决定：不迁移）

它描述的是「模型请求调用工具」这一 LLM 概念，不是对外事件协议的一部分——
`core/events.py` 里的 `ToolInfo` 才是。二者语义不同：`ToolUse` 是模型的
调用意图，`ToolInfo` 是协议里对外呈现的工具信息（还带 `output` / `is_error`）。
保持现状，改动最小，也不制造 `core/` 对 LLM 概念的依赖。

## 十五、明确不做

- 不实现 OpenAI 流式（`stream()` 维持 `NotImplementedError`）。
- 不改 `core/events.py` 的 `StreamEvent` 数据结构（字段、工厂方法、
  `to_dict()` 形状）；R4 改的是发射语义，不是结构。
- 不改 SSE 端点、前端契约之外的任何 API 层代码。
- 不做消息体中立化与历史数据迁移（第十节）。
- 不引入任何新的第三方依赖。










