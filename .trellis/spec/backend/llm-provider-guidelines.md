# LLM Provider Guidelines

> AgentOS 多 Provider LLM 调用规范：工厂入口、场景模型解析、双客户端契约与消息成本归因

---

## 1. Scope / Trigger

**适用范围**：

- 任何需要调用 LLM 的代码（流式对话、结构化输出），包括新增 Service / Runtime / Job
- 新增或修改 `llm/` 包：`factory.py`、`base.py`、`anthropic_client.py`、`openai_client.py`
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
- `llm/anthropic_client.py` / `llm/openai_client.py` — 双 provider 实现
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
        """provider 标识（anthropic/openai）。实现：anthropic_client.py:35、openai_client.py:33。"""

    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        system_prompt: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用，返回该 provider 的原始事件流（归一化由翻译器层负责）。"""

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

**`stream()` 基类声明为 `def` 而非 `async def` 是刻意写法**：实现类是带 `yield` 的
`async def`（异步生成器函数），调用即得可直接 `async for` 的迭代器、无需 `await`。
基类若写 `async def`，签名会被推断为「返回 AsyncIterator 的协程」。不要「修复」这一点。

### 2.3 客户端构造器（双实现签名一致）

```python
class AnthropicClient(BaseLLMClient):  # anthropic_client.py:19
    def __init__(
        self,
        model: str,  # 必填：工厂已解析好的模型，客户端不再自行读模型配置
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float | None = None,
    ): ...

class OpenAIClient(BaseLLMClient):  # openai_client.py:17，构造器签名与上完全一致
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
| `runtime/agent.py:105` `AgentRuntime.__init__` | `agent` |
| `runtime/subagent.py:66` `SubAgentRunner.__init__` | `subagent` |
| `runtime/compact.py:24` `CompactService.__init__` | `compact` |
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
# runtime/agent.py:1584 — 归因唯一来源
def _llm_attribution(self, model_override: str | None) -> tuple[str | None, str | None]:
    provider = getattr(self.llm, "provider", None)               # 客户端自报
    model = model_override or getattr(self.llm, "model", None)   # 逐调用覆盖优先
    return provider, model

# session/service.py:396 与 session/repository.py:210 — 参数透传，默认 None
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
| `AGENTOS_LLM_DEFAULT_MAX_TOKENS` | 131072 | 流式调用（`anthropic_client.py:62`） |
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
- 统一转 `NOT_GIVEN` 规避：`openai_client.py:53`、`anthropic_client.py:58/133/160`；新代码沿用该写法

### 3.4 能力边界（当前实现）

- `OpenAIClient` 仅实现 `complete_structured()`；`stream()` 抛 `NotImplementedError`（`openai_client.py:87`）
- 依赖流式的场景（`agent` / `subagent` / `compact`）当前只能使用 Anthropic
- `main.py:27` 在 lifespan 中调用一次 `create_llm_client()` 做启动自检，
  让配置错误在启动时暴露而非拖到首次对话；工厂本身仍是权威关卡，
  worker、脚本等不走 lifespan 的入口同样跑不掉校验

### 3.5 归因列数据契约

| 消息来源 | `provider` 列 | `model` 列 |
|---|---|---|
| LLM 产出的 assistant 消息（写入点 `runtime/agent.py:647` / `:1629`） | 客户端自报的 provider | 实际生效的模型 ID |
| user 消息 | NULL | NULL |
| tool 消息 | NULL | NULL |
| 存量历史消息 | NULL（迁移不回填） | NULL |

- **仅 LLM 产出的 assistant 消息填写**；user/tool 消息不经过模型，填了等于伪造归因
- `model` 存**实际生效的模型 ID**（`_resolve_model()` 解析后的最终值），不是配置里的场景档位名
- 归因唯一来源是 `_llm_attribution()`；注入的测试替身若未实现 `provider` 属性则留空 `None`

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

> **状态声明：以下测试全部尚未编写。** `tests/` 目录已在 commit `e872cdc` 整体删除，
> 本次改动未补任何测试。本节是待办契约：后续为 `llm/` 或归因链路补测试时，
> 必须覆盖以下用例与断言点；在测试真正写出并运行前，不得声称已验证通过。

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

### 6.2 `tests/test_llm_openai_client.py`（待编写）

- 参数映射：Mock `AsyncOpenAI`，断言 `parse()` 收到 `messages[0] == {"role": "system", ...}`
  与 `max_completion_tokens`（而非 `max_tokens`）
- 拒答处理：`message.parsed = None` 时断言抛 `ValueError` 且消息含 `finish_reason`
- timeout 归一：构造 `timeout=None`，断言 SDK 构造参数收到 `NOT_GIVEN` 而非 `None`
- 能力边界：消费 `client.stream(...)` 时断言抛 `NotImplementedError`

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

现有实现参照：`openai_client.py:53`、`anthropic_client.py:58/133/160`。

---

## 参考文件

- 工厂与全部校验：`llm/factory.py`
- 接口定义：`llm/base.py`
- 双实现：`llm/anthropic_client.py`、`llm/openai_client.py`
- 配置声明：`core/config.py`（`Settings`，`extra="forbid"`）
- 归因迁移：`alembic/versions/0021_add_message_llm_attribution.py`
- 归因写入：`runtime/agent.py`（`_llm_attribution()`）、`session/service.py`、`session/repository.py`
- 启动自检：`main.py`（lifespan）
