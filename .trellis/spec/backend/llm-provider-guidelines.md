# LLM Provider Guidelines

> AgentOS 多 Provider LLM 调用规范：工厂入口、场景模型解析、双客户端契约与消息成本归因

---

## 1. Scope / Trigger

**适用范围**：

- 任何需要调用 LLM 的代码（流式对话、结构化输出），包括新增 Service / Runtime / Job
- 新增或修改 `llm/` 包：`factory.py`、`base.py`、`types.py`、`anthropic_client.py`、`openai_client.py`
- 新增 LLM 使用场景（scenario）或调整 `{provider}_{scenario}_model` 场景模型配置
- 修改 `session_messages` 的 `provider` / `model` 成本归因列及其写入逻辑
- 新增、删除、重命名 `AGENTOS_LLM_*` / `AGENTOS_ANTHROPIC_*` / `AGENTOS_OPENAI_*` 配置项

**强制触发条件**（满足任一即须遵守本规范）：

- 代码中出现 `create_llm_client` / `resolve_scenario_model` / `BaseLLMClient`
- 涉及 Alembic 迁移 `0021_message_llm_attribution` 引入的归因两列
- Service / Runtime 构造器需要持有 LLM 客户端

**关键文件**：

- `llm/factory.py` — 工厂函数与模型解析（唯一权威校验关卡）
- `llm/base.py` — `BaseLLMClient` 抽象接口
- `llm/types.py` — 中立类型：`LLMStreamEvent` / `LLMTurnResult` / `PromptBlock` / `ToolSpec`
- `llm/anthropic_client.py` / `llm/openai_client.py` — 双 provider 实现
- `runtime/stream_assembler.py` — `TurnEventAssembler` 协议装配器（见 3.6）
- `alembic/versions/0021_add_message_llm_attribution.py` — 归因列迁移
- `runtime/agent.py` — `_llm_attribution()` 归因唯一来源
- `main.py` — lifespan 启动自检（`main.py:27`）

---

## 2. Signatures

### 2.1 工厂入口（`llm/factory.py`）

```python
def create_llm_client(scenario: str | None = None) -> BaseLLMClient:  # factory.py:88
    """创建 LLM 客户端。scenario=None 表示直接使用 provider 主模型。"""

def resolve_scenario_model(scenario: str | None = None) -> str:  # factory.py:79
    """只要模型名、不建客户端时用（如 Memory Job 入队冻结模型）。校验规则与工厂一致。"""
```

内部实现约定：

```python
_SUPPORTED_PROVIDERS = ("anthropic", "openai")

# 七个已注册场景，与 Settings 的 {provider}_{scenario}_model 字段一一对应
_KNOWN_SCENARIOS = frozenset({
    "agent", "subagent", "subagent_fast", "compact",
    "memory_selector", "memory_extractor", "memory_dream",
})

def _blank_to_none(value: str | None) -> str | None:
    """None / 空串 / 纯空白统一归一成 None（.env 留空的键会读成空串）。"""

def _resolve_model(provider: str, scenario: str | None) -> str:
    """两级回落：{provider}_{scenario}_model 有值则用，否则 {provider}_model；两级都空抛错。"""
```

**模型解析没有硬编码兜底**：两级都为空直接抛中文 `AgentException`（见第 4 段矩阵第 4 条）。

### 2.2 抽象接口（`llm/base.py`）

```python
class BaseLLMClient(ABC):
    @property
    @abstractmethod
    def provider(self) -> str:
        """provider 标识（anthropic/openai）。实现：anthropic_client.py:258、openai_client.py:281。"""

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | list[PromptBlock] | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """流式调用，产出中立 LLMStreamEvent；provider 原始格式在实现内部消化，不外露。

        入参已是中立类型，编码为本家 wire 形状由实现内部私有完成
        （_encode_system / _encode_tools），调用方不参与。
        契约：每次调用必须以恰好一个 TURN_RESULT 事件收尾且 result 非空。
        """

    @abstractmethod
    async def complete_structured[T: BaseModel](
        self,
        messages: list[dict[str, Any]],
        output_format: type[T],
        *,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int = 256,
        timeout: float | None = None,
    ) -> T:
        """结构化输出，返回 Pydantic 模型实例。"""
```

中立类型 `LLMStreamEvent` / `LLMTurnResult` / `PromptBlock` / `ToolSpec` 定义于
`llm/types.py`；流式协议的分层约定见 3.6。

**`stream()` 基类声明为 `def` 而非 `async def` 是刻意写法**：实现类是带 `yield` 的
`async def`（异步生成器函数），调用即得可直接 `async for` 的迭代器、无需 `await`。
基类若写 `async def`，签名会被推断为「返回 AsyncIterator 的协程」。不要「修复」这一点。

### 2.3 客户端构造器（双实现签名一致）

```python
class AnthropicClient(BaseLLMClient):  # anthropic_client.py:242
    def __init__(
        self,
        model: str,  # 必填：工厂已解析好的模型，客户端不再自行读模型配置
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float | None = None,
    ): ...

class OpenAIClient(BaseLLMClient):  # openai_client.py:265，构造器签名与上完全一致
    ...
```

### 2.4 依赖注入约定（Service / Runtime）

构造器统一模式：

```python
def __init__(self, ..., llm: BaseLLMClient | None = None):
    self.llm = llm or create_llm_client(scenario="<本场景>")
```

现有 9 处注入点（新增服务照此模式并在下表登记）：

| 注入点 | scenario |
|---|---|
| `runtime/agent.py:101` `AgentRuntime.__init__` | `agent` |
| `runtime/subagent.py:59` `SubAgentRunner.__init__` | `subagent` |
| `runtime/compact.py:20` `CompactService.__init__` | `compact` |
| `memory/recall/service.py:53` `MemoryRecallService.__init__` | `memory_selector` |
| `memory/jobs/service.py:182`（构造器接受注入并透传；入队冻结模型见 `:90` / `:167` 的 `resolve_scenario_model()`） | `memory_extractor` / `memory_dream` |
| `memory/jobs/extraction.py:214` / `:317` | `memory_extractor` |
| `memory/jobs/dream.py:222` / `:284` | `memory_dream` |

### 2.5 DB 签名（成本归因列）

迁移文件 `alembic/versions/0021_add_message_llm_attribution.py`：

```python
revision = "0021_message_llm_attribution"   # 注意：revision id 与文件名不同
down_revision = "0020_agent_mvp_comments"   # 同样是 revision id，不是文件名
```

`session_messages` 新增两列（均可空、带中文 comment，ORM 见 `session/models.py:157` / `:162`）：

| 列 | 类型 | comment（原文） |
|---|---|---|
| `provider` | `VARCHAR(32) NULL` | `产出该消息的LLM提供商标识（anthropic/openai），非模型产出的消息为空` |
| `model` | `VARCHAR(120) NULL` | `本次调用实际生效的模型ID，非模型产出的消息为空` |

写入链路：

```python
# runtime/agent.py:1579 — 归因唯一来源
def _llm_attribution(self, model_override: str | None) -> tuple[str | None, str | None]:
    provider = getattr(self.llm, "provider", None)               # 客户端自报
    model = model_override or getattr(self.llm, "model", None)   # 逐调用覆盖优先
    return provider, model

# session/service.py:401 与 session/repository.py:217 — 参数透传，默认 None
async def add_message(
    self, session_id: int, role: str, content: Any,
    turn_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> SessionMessage: ...
```

---

## 3. Contracts

### 3.1 环境变量（`AGENTOS_` 前缀，声明于 `core/config.py` 的 `Settings`）

**全局开关**：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `AGENTOS_LLM_PROVIDER` | 无（**必填**） | `anthropic` / `openai`；配置固定、重启生效，不支持运行时切换 |

**Anthropic 组**（OpenAI 组键名对称，把 `ANTHROPIC` 换成 `OPENAI`）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `AGENTOS_ANTHROPIC_API_KEY` | None | `provider=anthropic` 时必填，缺失抛错（矩阵 5a） |
| `AGENTOS_ANTHROPIC_BASE_URL` | None | **不带 `/v1`**（见第 7 段第二对） |
| `AGENTOS_ANTHROPIC_MODEL` | None | 主模型，所有场景的回落终点 |
| `AGENTOS_ANTHROPIC_MAX_RETRIES` | 2 | SDK 内置重试（429/5xx/网络超时，指数退避），不自造重试框架 |
| `AGENTOS_ANTHROPIC_TIMEOUT_SECONDS` | None | 留空 = 交给 SDK 自动计算（见 3.3） |
| `AGENTOS_ANTHROPIC_<SCENARIO>_MODEL` | None | 七个场景覆盖，全部可选，留空回落主模型 |

**OpenAI 组的差异项**：`AGENTOS_OPENAI_BASE_URL` 默认 `https://api.openai.com/v1`，**必须带 `/v1`**。

**max_tokens（provider 无关）**：

| 环境变量 | 默认值 | 使用方 |
|---|---|---|
| `AGENTOS_LLM_DEFAULT_MAX_TOKENS` | 131072 | 流式调用（`anthropic_client.py:362`、`openai_client.py:435`） |
| `AGENTOS_LLM_MEMORY_SELECTOR_MAX_TOKENS` | 256 | Memory 召回选择器 |
| `AGENTOS_LLM_MEMORY_EXTRACTOR_MAX_TOKENS` | 4096 | Memory 提取 |
| `AGENTOS_LLM_MEMORY_DREAM_MAX_TOKENS` | 8192 | Memory Dream |

**三处同步硬约束**：`Settings` 设 `extra="forbid"`，`.env` 出现未声明的键立即报错；
新增配置必须同时更新 `core/config.py` / `.env` / `.env.example` 三处。

### 3.2 模型解析优先级

```
方法参数 model > {provider}_{scenario}_model 场景覆盖 > {provider}_model 主模型 > 抛中文 AgentException
```

- 空串 / 纯空白配置视同未配置（`_blank_to_none` 归一）
- 场景覆盖未配置时回落主模型是**既定语义**（覆盖本就可选），不算静默兜底
- 客户端构造时收到的已是解析好的 `model`，客户端内部不再读模型配置

### 3.3 timeout 语义（易错）

- 配置留空（`None`）= 交给 SDK 自动计算超时
- 但把 `None` **显式传给 SDK** 会被理解成「禁用超时」，语义完全相反
- 统一转 `NOT_GIVEN` 规避：`openai_client.py:372/423`、`anthropic_client.py:356/436/463`；新代码沿用该写法

### 3.4 能力边界（当前实现）

- 两家客户端均已实现 `stream()` 与 `complete_structured()`，`BaseLLMClient` 的三个抽象成员
  （`provider` / `stream` / `complete_structured`）双实现齐备；依赖流式的场景
  （`agent` / `subagent` / `compact`）两家 provider 均可用
- OpenAI 侧的 `stream()` 走 `create(stream=True)` 裸 chunk 路径，**不用 SDK 的 `.stream()` helper**，
  理由见 3.6 末尾的决策记录
- `main.py:27` 在 lifespan 中调用一次 `create_llm_client()` 做启动自检，
  让配置错误在启动时暴露而非拖到首次对话；工厂本身仍是权威关卡，
  worker、脚本等不走 lifespan 的入口同样跑不掉校验

### 3.5 归因列数据契约

| 消息来源 | `provider` 列 | `model` 列 |
|---|---|---|
| LLM 产出的 assistant 消息（写入点 `runtime/agent.py:646` / `:1627`） | 客户端自报的 provider | 实际生效的模型 ID |
| user 消息 | NULL | NULL |
| tool 消息 | NULL | NULL |
| 存量历史消息 | NULL（迁移不回填） | NULL |

- **仅 LLM 产出的 assistant 消息填写**；user/tool 消息不经过模型，填了等于伪造归因
- `model` 存**实际生效的模型 ID**（`_resolve_model()` 解析后的最终值），不是配置里的场景档位名
- 归因唯一来源是 `_llm_attribution()`；注入的测试替身若未实现 `provider` 属性则留空 `None`

### 3.6 流式协议分层（入站适配私有、装配归 runtime）

| 职责 | 归属 | 位置 |
|---|---|---|
| 入站流式适配（provider 原始 chunk → 中立 `LLMStreamEvent`） | 各家 Client **私有实现** | `anthropic_client.py:129` `_AnthropicChunkAdapter`、`openai_client.py:98` `_OpenAIChunkAdapter`（均为模块私有） |
| 自有协议装配（打 actor/session_id 戳、分配 turn 内全局 block index、block_start/block_stop 配对） | runtime 侧装配器，**不是 provider 契约** | `runtime/stream_assembler.py:13` `TurnEventAssembler` |
| 出站编码（中立 `PromptBlock` / `ToolSpec` / 消息块 → 本家 wire 形状） | 各家 Client **私有方法**，不上 ABC | `_encode_system` / `_encode_tools` / `_encode_messages`（`anthropic_client.py:262/285/297`、`openai_client.py:285/298/313`） |

- **入站**：`stream()` 只产中立 `LLMStreamEvent`（`llm/types.py`），provider 原始格式
  在实现内部消化、不外露；必须以恰好一个 `TURN_RESULT` 事件收尾且 `result` 非空，
  提前耗尽视为 provider 实现错误，由消费方抛中文 `AgentException`。
- **装配**：中立事件不含 `actor` / `session_id` / 全局 index——那是 runtime 概念，
  不下沉进 `llm/`。块级协议规则（index 分配、start/stop 配对，含 tool_use 块补发
  `block_stop`）由 `TurnEventAssembler` 用代码统一保证，provider 不参与分配，
  两家语义想不一致都做不到；该类的类级 docstring 是这套块级协议的唯一定义处，
  不要把这些规则写成各 provider 需遵守的 docstring 契约。
- **出站**：编码是 `stream()` 的内部步骤，没有外部调用方，**刻意不提成抽象方法**——
  `BaseLLMClient` 抽象成员维持 3 个（`provider` / `stream` / `complete_structured`），
  不要「补全」`encode_*` 到 ABC 上。中立描述的产出方：`tools/base.py:36` `to_spec()`、
  `tools/registry.py:42` `to_specs()`、`prompt/compose.py:41` `compose_lead_blocks()`。
  消息体同理由 `_encode_messages()` 在 `stream()` 内部翻译，契约见 3.7。
- **不引入第二个 provider 抽象**：不建 `StreamTranslator` ABC、不建 `create_translator()`
  工厂；新增 provider 只写一个客户端。`runtime/` / `tools/` / `prompt/` 对 `anthropic`
  保持零引用。

**决策记录：OpenAI 侧不用 SDK 的 `.stream()` helper**（openai 3.0.0，结论取自本机
`.venv` 源码而非训练记忆）。三条硬约束使 helper 路径不可用，改用
`create(stream=True)` 裸 chunk + 私有适配器：

1. **strict 强制校验**：`AsyncCompletions.stream()` 方法体首行即调
   `_validate_input_tools()`，逐个断言 `tool["function"]["strict"] is True`，否则在发出
   任何 HTTP 请求前就抛 `ValueError`。strict 模式对 JSON Schema 有额外约束（全字段
   required、`additionalProperties: false`），启用它要倒逼改写全部内置工具 schema，
   且 Anthropic 侧 `_encode_tools` 同样不带 strict，加上去两家立刻不对称。
   注意 `create()` **不做**此校验，仅 `parse()` / `stream()` 做。
2. **截断即丢内容**：`get_final_completion()` 落到 `parse_chat_completion()`，
   对 `finish_reason == "length"` 无条件抛 `LengthFinishReasonError`——被
   `max_completion_tokens` 截断时拿不到已生成内容。这与 Anthropic 的
   `get_final_message()` 行为相反（后者正常返回，由调用方判读 `stop_reason`）。
   走裸 chunk 则由适配器把 `length` 翻成 `stop_reason="max_tokens"` 正常返回，文本不丢。
3. **JSON 解析权**：工具入参由适配器自己解析，坏 JSON 才抛得出带工具名上下文的中文
   `AgentException`；SDK 自行解析时抛的是裸 `json.JSONDecodeError`，不符第 4 段错误矩阵。

附带两条易错项：`stream_options={"include_usage": True}` 必须显式传，SDK 不会自动注入，
不传则 usage 恒为 `None`；tool call 的 `id` 只在该次调用的**首个** chunk 携带，
后续 chunk 只有 `index` 与 `arguments` 增量，适配器须自行留存。

### 3.7 消息体中立化（`stream()` 的 messages 入参契约）

**`BaseLLMClient.stream()` 的 messages 入参是中立形状，不是任何一家的 wire 形状。**
库里（`session_messages.content`、`session_snapshots.messages`）存的也一律是中立形状，
provider 形状只存在于各家 `stream()` 内部 `_encode_messages()` 之后。

assistant 消息的 content 是块数组，两类块（定义于 `llm/types.py:70/81`，
经 `build_assistant_blocks()`（`:105`）构造、`parse_assistant_blocks()`（`:121`）解析）：

```python
{"kind": "text", "text": "..."}
{"kind": "tool_use", "id": "...", "name": "...", "input": {...}}
```

`tool` 消息沿用 `ToolResultMessage`（`llm/types.py:27`），形状不变——它本就是自有协议。

- **判别键用 `kind` 而非 `type`，这是刻意的**：两家 wire 的 `type` 取值恰好同名
  （都叫 `tool_use`），入参字段也都叫 `input`，若沿用 `type`，残留的 provider 形状数据
  会被静默当成中立格式读下去，错误一路潜伏到运行时。换 `kind` 让它在
  `parse_assistant_blocks()` 当场抛中文 `AgentException`。新增块类型时沿用 `kind`。
- **翻译收在 Client 内部，不在调用方**：`llm.stream()` 有 4 个调用点
  （`agent.py:631`、`agent.py:1616`、`subagent.py:226`、`compact.py:108`），
  其中 `subagent.py` 自建 messages、不走 `_messages_to_context()`。翻译放调用方则每处
  都要记得翻一次，漏一处就是一个 400，新增第 5 个调用点时也无人提醒。
- **两家的工具结果结构性不同**，这正是出站翻译必须存在的理由，不能共用一套形状糊过去：
  Anthropic 把 tool_result 作为块塞进 `role="user"` 消息并合并连续块；
  OpenAI 每个工具结果是一条独立的 `role="tool"` 消息，带 `tool_call_id`。
- **快照同为中立形状**：`compact.py` 在中立形状上工作、落中立快照，
  `_micro_compact()`（`compact.py:62`）直接读 `role="tool"` 的 `tool_name`，
  不再从 assistant 的 tool_use 块反查，语义不随 provider 变。
  切 provider 后旧快照仍可用，无需清库。
- **无兼容读**：不写旧格式检测分支、不做存量迁移——那个分支永远不会被测试覆盖到。

---

## 4. Validation & Error Matrix

所有校验集中在 `llm/factory.py`（`create_llm_client()` 与 `resolve_scenario_model()` 共用），
一律抛 `AgentException.message`（简体中文），**无静默兜底**。错误消息原文如下，改代码时须同步本表：

| # | 条件 | 校验位置 | 错误消息（原文） |
|---|---|---|---|
| 1 | `llm_provider` 未配置（None / 空串 / 纯空白） | `_require_provider` | `未配置 LLM provider，请在 .env 中设置 AGENTOS_LLM_PROVIDER（可选值：anthropic / openai）` |
| 2 | `llm_provider` 取值不在支持列表 | `_require_provider` | `不支持的 LLM provider：{provider}，可选值：anthropic / openai` |
| 3 | scenario 非 None 且不在七个已注册场景 | `_resolve_model` | `未知的 LLM 场景：{scenario}，可选值：agent、compact、memory_dream、memory_extractor、memory_selector、subagent、subagent_fast` |
| 4 | 场景覆盖与主模型两级都为空 | `_resolve_model` | `未配置 {provider} 的模型，请设置 AGENTOS_{PROVIDER}_MODEL，或为该场景设置 AGENTOS_{PROVIDER}_<SCENARIO>_MODEL` |
| 5a | `provider=anthropic` 且 api_key 为空 | `create_llm_client` | `LLM provider 为 anthropic，但未配置 AGENTOS_ANTHROPIC_API_KEY` |
| 5b | `provider=openai` 且 api_key 为空 | `create_llm_client` | `LLM provider 为 openai，但未配置 AGENTOS_OPENAI_API_KEY` |

说明：

- 第 3 条的可选值列表是 `'、'.join(sorted(_KNOWN_SCENARIOS))`，按字母序排列（与上表一致）
- 第 4 条的 `{PROVIDER}` 为大写（源码 `provider.upper()`），如 `AGENTOS_ANTHROPIC_MODEL`
- 场景名拼错属**编码错误**，直接抛错（第 3 条），而非静默回落主模型
- `resolve_scenario_model()` 不建客户端，只触发第 1-4 条，不校验 api_key

---

## 5. Good/Base/Bad Cases

### Base：Service 标准注入 + 工厂回落

```python
from llm import BaseLLMClient, create_llm_client

class MemoryRecallService:
    def __init__(self, db: AsyncSession, llm: BaseLLMClient | None = None):
        # 未注入时回落工厂：工厂内部完成 provider/model/api_key 校验并抛中文错
        self.llm = llm or create_llm_client(scenario="memory_selector")
```

### Good：只要模型名、不建客户端

```python
from llm import resolve_scenario_model

# Memory Job 入队时冻结模型，避免出队执行时配置已变（memory/jobs/service.py:90）
model = resolve_scenario_model("memory_extractor")
```

### Good：测试注入替身

```python
class FakeLLM(BaseLLMClient):
    @property
    def provider(self) -> str:
        return "anthropic"
    # ... stream / complete_structured 返回预置数据 ...

service = MemoryRecallService(db, llm=FakeLLM())  # 不触发工厂，无需真实配置
```

### Bad：绕过工厂直接实例化具体客户端

```python
# ❌ 绕过 provider 校验与场景模型解析；全局切换 provider 时该处失效
self.llm = AnthropicClient(model="claude-example-model")
```

### Bad：静默兜底（明令禁止）

```python
# ❌ 配置缺失时静默用了硬编码 provider/模型，「跑起来了但用的不是你以为的模型」
self.llm = llm or AnthropicClient(model=settings.anthropic_model or "claude-example-model")

# ✅ 回落目标只能是工厂（内部校验并抛中文错，见第 4 段矩阵）
self.llm = llm or create_llm_client(scenario="compact")
```

### Bad：给非模型消息填归因

```python
# ❌ user/tool 消息不经过模型，填 provider/model 等于伪造归因
await self.session_service.add_message(session_id, "user", content, provider="anthropic")

# ✅ user/tool 消息走默认 None；仅 assistant 落库时传 _llm_attribution() 的结果
provider, model = self._llm_attribution(model_override)
await self.session_service.add_message(
    session_id, "assistant", final_content, turn_id=turn.id,
    provider=provider, model=model,
)
```

---

## 6. Tests Required

> **状态声明（2026-08 更新）**：现有 `tests/test_stream_assembler.py`（装配器 13 例）、
> `tests/test_stream_baseline.py`（Anthropic 适配器 + 装配器组合基线 8 例）、
> `tests/test_encoders.py`（两家 `_encode_system` / `_encode_tools` 编码形状 6 例）、
> `tests/test_llm_openai_client.py`（OpenAI 适配器与 `stream()` 17 例）、
> `tests/test_message_neutral.py`（中立块往返与双向编码 14 例）、
> `tests/test_neutral_context.py`（中立上下文装配与 compact 6 例），共 64 passed——
> 即 3.6 / 3.7 两节的流式分层与消息中立化均已有覆盖。**但以下 4 个文件仍尚未编写**，
> 属另外的欠账：工厂校验、客户端构造参数（timeout 归一、max_tokens 来源）、
> 注入点回落与归因链路均无测试。后续补齐时必须覆盖以下用例与断言点；
> 在测试真正写出并运行前，不得声称已验证通过。

### 6.1 `tests/test_llm_factory.py`（待编写）

- 场景覆盖优先：patch `anthropic_memory_selector_model="m-a"`、`anthropic_model="m-b"`，
  断言 `resolve_scenario_model("memory_selector") == "m-a"`
- 回落主模型：场景覆盖 patch 为 `None`，断言返回 `anthropic_model` 的值
- 空白归一：场景覆盖 patch 为 `"  "`，断言回落主模型（`_blank_to_none` 生效）
- 五类错误各一条：按第 4 段矩阵逐条 patch settings，断言 `pytest.raises(AgentException)`
  且 `match` 对应中文关键词（`"未配置 LLM provider"`、`"不支持的 LLM provider"`、
  `"未知的 LLM 场景"`、`"未配置 anthropic 的模型"`、`"AGENTOS_OPENAI_API_KEY"`）
- provider 分派：`llm_provider=anthropic` 时断言 `client.provider == "anthropic"`
  且 `client.model` 为解析结果；openai 同理。**断言用 `client.provider`，不用 isinstance**

### 6.2 `tests/test_llm_openai_client.py`（已编写，17 例）

流式部分已覆盖（新增 provider 或改动适配器时须维持这些断言点）：

- 事件序列：纯文本 / 文本+工具 / 多工具，断言产出的 `LLMStreamEvent` 序列与收尾的
  单个 `TURN_RESULT`；与 Anthropic 侧同输入的协议等价性对照
- block_ref 分配：文本占 0、工具为 `1 + tool_call.index`；tool_use 块不发 `BLOCK_STOP`
  （由 `TurnEventAssembler` 补发，见 3.6）
- 终局信息：usage 与 stop_reason 映射；缺 usage 时为 `None`；
  `finish_reason="length"` 保留已生成文本并翻成 `max_tokens`；`content_filter` 透传
- 错误分支（均须为中文 `AgentException`，见第 4 段矩阵）：工具入参坏 JSON、
  tool call 缺 id 或 name、流未给 `finish_reason` 即耗尽
- 分层边界：适配器只产中立事件，不含 `actor` / `session_id` / 全局 index；
  恒不产出 `THINKING_DELTA`（`ChoiceDelta` 无 reasoning 字段）；噪声 chunk 不产事件

以下非流式用例仍未覆盖，属欠账：

- 参数映射：Mock `AsyncOpenAI`，断言 `parse()` 收到 `messages[0] == {"role": "system", ...}`
  与 `max_completion_tokens`（而非 `max_tokens`）
- 拒答处理：`message.parsed = None` 时断言抛 `ValueError` 且消息含 `finish_reason`
- timeout 归一：构造 `timeout=None`，断言 SDK 构造参数收到 `NOT_GIVEN` 而非 `None`

### 6.3 `tests/test_llm_anthropic_client.py`（待编写）

- timeout 归一：构造 `timeout=None`，断言 `AsyncAnthropic` 构造参数收到 `NOT_GIVEN`
- 异步生成器契约：断言 `inspect.isasyncgen(client.stream(...))` 为 True（调用无需 await）
- 流式 max_tokens：Mock 后断言取自 `settings.llm_default_max_tokens`，代码中无硬编码

### 6.4 `tests/test_service_llm_injection.py`（待编写）

对第 2.4 节的 9 处注入点，各覆盖两条：

- 注入替身：`Service(llm=fake)` 时断言工厂未被调用（`mock_factory.assert_not_called()`）
- 默认回落：不注入时断言工厂以正确 scenario 被调用一次，
  如 `mock_factory.assert_called_once_with(scenario="memory_selector")`

### 6.5 `tests/test_message_llm_attribution.py`（待编写，需真实 PostgreSQL）

- assistant 落库：`add_message(..., provider=..., model=...)` 写入后查库，断言两列值与传入一致
- user/tool 落库：不传归因参数，断言两列均为 `NULL`
- `_llm_attribution` 单元：`model_override` 非空时优先返回覆盖值；
  注入的替身无 `provider` 属性时返回 `(None, model)`，不抛错
- 迁移往返：`alembic upgrade` 到 `0021_message_llm_attribution` 后两列存在、可空、带中文 comment；
  `alembic downgrade -1` 后两列消失

---

## 7. Wrong vs Correct

### 第一对：判定当前使用哪家 provider

```python
# ❌ Wrong：isinstance 判断具体客户端类，注入测试替身（Mock/Fake）时恒为 False
if isinstance(self.llm, AnthropicClient):
    ...

# ❌ Wrong：直接读全局配置，注入替身与全局配置不一致时判断失真
if settings.llm_provider == "anthropic":
    ...

# ✅ Correct：读客户端实例自报的 provider 属性（BaseLLMClient 的抽象属性）
if self.llm.provider == "anthropic":
    ...
```

**原因**：`llm` 是可注入依赖，运行时实例未必来自工厂。测试注入替身时，
isinstance 与 `settings.llm_provider` 都描述不了「当前实例是谁」，只有实例自报的
`provider` 属性始终如实。归因写入点 `_llm_attribution()` 即按此实现。

### 第二对：base_url 拼接（两家 SDK 规则相反）

```bash
# ❌ Wrong：Anthropic 带 /v1 —— SDK 内部路径已是 /v1/messages，
#    多写会拼成 /v1/v1/messages，请求 404
AGENTOS_ANTHROPIC_BASE_URL=https://gateway.example.com/v1

# ✅ Correct：Anthropic 的 base_url 不带 /v1
AGENTOS_ANTHROPIC_BASE_URL=https://gateway.example.com
```

```bash
# ❌ Wrong：OpenAI 不带 /v1 —— SDK 内部路径是 /chat/completions，
#    少写会让网关返回 HTML，SDK 报 'str' object has no attribute 'choices'
#    这类看似无关的错误
AGENTOS_OPENAI_BASE_URL=https://gateway.example.com

# ✅ Correct：OpenAI 的 base_url 必须带 /v1
AGENTOS_OPENAI_BASE_URL=https://gateway.example.com/v1
```

### 第三对：timeout 传参

```python
# ❌ Wrong：把 None 直接传 SDK，会被理解成「禁用超时」而非「默认超时」
client = AsyncOpenAI(..., timeout=self.timeout)

# ✅ Correct：None 转 NOT_GIVEN，保留 SDK 自动计算的默认超时
client = AsyncOpenAI(
    ...,
    timeout=self.timeout if self.timeout is not None else NOT_GIVEN,
)
```

现有实现参照：`openai_client.py:372/423`、`anthropic_client.py:356/436/463`。

---

## 参考文件

- 工厂与全部校验：`llm/factory.py`
- 接口定义：`llm/base.py`
- 中立类型：`llm/types.py`
- 中立消息构造与解析：`llm/types.py`（`build_assistant_blocks()` / `parse_assistant_blocks()`）、
  产出方 `session/service.py`（`_messages_to_context()`）、消费方 `runtime/compact.py`、
  `tools/subagents/trigger.py`、`memory/jobs/extraction.py`
- 双实现：`llm/anthropic_client.py`、`llm/openai_client.py`
- 协议装配器：`runtime/stream_assembler.py`（`TurnEventAssembler`）
- 配置声明：`core/config.py`（`Settings`，`extra="forbid"`）
- 归因迁移：`alembic/versions/0021_add_message_llm_attribution.py`
- 归因写入：`runtime/agent.py`（`_llm_attribution()`）、`session/service.py`、`session/repository.py`
- 启动自检：`main.py`（lifespan）
