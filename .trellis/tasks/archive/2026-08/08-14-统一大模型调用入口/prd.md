# PRD: 统一大模型调用入口

## 目标

解决当前 LLM 调用配置分散、实例化方式不一致、错误处理策略割裂的问题，在保持现有 `LLMClient` 封装的基础上，实现：

1. **配置统一**：所有 LLM 相关配置（model、timeout、max_tokens）遵循相同的回落链和命名规范
2. **实例化统一**：所有服务都支持依赖注入，方便测试和自定义配置
3. **错误处理**：暴露两家 SDK 内置重试配置（max_retries/timeout），不自造重试框架
4. **接口合理**：内部接口以清晰合理为准，可自由重构，调用方一次性迁移到位

**用户价值**：降低维护成本，减少配置错误，提升系统可测试性和可靠性。

---

## 背景

### 当前架构

> **仓库当前处于半迁移状态**：`llm/client.py` 已被删除，`llm/` 下现为
> `base.py`（`BaseLLMClient`）、`anthropic_client.py`（`AnthropicClient`）、
> `openai_client.py`（`OpenAIClient`）、`types.py`。但 7 处调用方仍在
> `from llm.client import LLMClient`（`runtime/agent.py:32`、`runtime/subagent.py:21`、
> `runtime/compact.py:6`、`memory/jobs/extraction.py:15`、`memory/jobs/dream.py:17`、
> `memory/jobs/service.py:14`、`memory/recall/service.py:13`），**应用当前无法导入**。
> 下文「当前架构」描述的是重构前的设计状态，用于说明动机；`llm/factory.py` 尚未创建。

AgentOS 原有统一的 LLM 封装层 `llm/client.py` (LLMClient)，所有调用都经过它：

- **流式调用**：`stream()` 用于主 Agent、SubAgent、上下文压缩
- **结构化输出**：`complete_structured()` 用于 Memory 提取/Dream/召回选择器

项目仅使用 Anthropic SDK，无多 provider 复杂度。

### 新增需求：多 Provider 支持

现增加 **OpenAI SDK** 支持，采用**全局统一切换** 的架构：

- **配置方式**：一个 `llm_provider` 开关控制整个 AgentOS 使用哪家 provider
- **模型档位分级**：在同一 provider 下，不同场景可以使用不同档位的模型（如 Anthropic 的 sonnet/haiku，OpenAI 的 gpt-4o/gpt-4o-mini）
- **统一入口**：所有服务（Agent/SubAgent/Memory/Compact）都通过工厂函数创建 LLM Client

**关键约束**：
- 消息历史落库格式为 **AgentOS 自有协议**（provider 无关）。任何 provider 的原始格式，
  入库前经翻译器转成 AgentOS 协议，出库送模型前再转成目标 provider 的 wire 格式；
  新增 provider 只加一个翻译器，不改数据库、不改读取方
- 翻译器独立成层，不下沉进 LLM 客户端；客户端只调 SDK 并吐原始事件
- 不做运行时切换（provider 在配置中固定，重启后生效）
- `llm_provider` 无默认值：未配置、取值不支持、api_key 缺失、模型解析不出，
  一律抛具体中文 `AgentException`，不做静默回落

### 已知问题（sub-agent 调查报告）

#### 1. 配置回落链不一致

| 场景 | model 配置回落链（旧） | 层数 |
|-----|-----------------|------|
| Memory Selector | `memory_selector_model` → `anthropic_model` | 2 层 |
| Memory Extraction | `memory_extractor_model` → `anthropic_model` | 2 层（实际代码是 3 层但多余） |
| Memory Dream | `memory_dream_model` → `memory_extractor_model` → `anthropic_model` | 3 层（不必要的继承） |
| SubAgent | `spec.model` → `subagent_{type}_model` → `anthropic_model` | 3 层（档位分级合理） |
| Compact | 留空 → `anthropic_model` | 1 层 |

**问题**：
1. Dream 继承 Extractor 的 model 配置没有业务语义，纯属历史遗留
2. 配置命名不统一，缺少 provider 前缀，多 provider 场景下会冲突

**改进方案**：
- 配置字段直接重命名为 `anthropic_*_model`（如 `anthropic_memory_selector_model`）
- 新增 OpenAI 对应配置 `openai_*_model`
- 删除 Dream 对 Extractor model 的继承，统一为 2 层回落

#### 2. 硬编码配置分散

- `max_tokens`：流式硬编码 `131072`（现 `llm/anthropic_client.py:55`），结构化分别为 `256/4096/8192`
- `timeout`：流式用 SDK 默认；结构化三处已改为读取 settings（`recall/service.py:171,177`、`extraction.py:331`、`dream.py:297`），本项无需迁移
- `temperature`：流式未设置（SDK 默认），结构化固定 `0`

**问题**：无法按场景灵活调整，修改需要改代码。

#### 3. 客户端实例化不统一

- **硬编码**：`AgentRuntime`（`runtime/agent.py:113`）和 `SubAgentRunner`（`runtime/subagent.py:66`）在 `__init__` 中固定 `self.llm = LLMClient()`
- **支持注入**：`CompactService`、`MemoryRecallService`、`MemoryJobRunner` 接受可选 `llm` 参数

**问题**：前两者无法在测试中注入 mock，也无法自定义配置。

#### 4. 错误处理策略割裂

- **主 Agent/SubAgent**：异常向上抛出，无分类，无自动重试
- **Memory Extraction/Dream**：有 `_classify_extractor_error()`，429/5xx 可重试，依赖 Job 队列
- **Memory Selector**：有降级策略（词法匹配），无自动重试

**问题**：相同的 SDK 异常（429、5xx、网络超时）在不同场景有不同处理，维护成本高。

---

## 需求

### R1: 统一配置管理

**R1.1 配置字段重命名与回落链标准化**

- **配置字段重命名**：所有 model 配置直接重命名为 `{provider}_{场景}_model` 格式
  - 旧：`memory_selector_model`、`memory_extractor_model`、`memory_dream_model`
  - 新：`anthropic_memory_selector_model`、`anthropic_memory_extractor_model`、`anthropic_memory_dream_model`
  - 新增：`openai_memory_selector_model`、`openai_memory_extractor_model`、`openai_memory_dream_model`
  - **不保留旧配置名兼容映射**，直接重命名

- **回落链标准化**：所有场景按 **2 层回落**：`{provider}_{场景}_model` → `{provider}_model`
  - 示例：`anthropic_memory_selector_model` → `anthropic_model`
  - 删除 Dream 对 Extractor model 的继承，统一回落到 `anthropic_model`

- **SubAgent 档位分级**：档位语义保留（默认档 / 快速档两个档位），但配置项统一纳入
  `{provider}_{场景}_model` 命名与 2 层回落：`subagent_default_model` →
  `{provider}_subagent_model`，`subagent_fast_model` → `{provider}_subagent_fast_model`。
  即「分档」这件事不变，变的是配置项归属到 provider 组、走同一套回落规则。

**R1.2 抽取硬编码配置到 settings**

在 `core/config.py` 新增：

```python
# 全局 Provider 选择（必填，无默认值；未配置时抛中文错误，见 D9）
llm_provider: str | None = None  # anthropic | openai

# Anthropic 配置（按 provider 分组）
anthropic_model: str | None = None  # 主模型，不设硬编码默认值（见 D9）
anthropic_subagent_fast_model: str | None = None  # 可选覆盖，未设置则继承 anthropic_model
anthropic_memory_selector_model: str | None = None
anthropic_memory_extractor_model: str | None = None
anthropic_memory_dream_model: str | None = None
anthropic_compact_model: str | None = None
anthropic_agent_model: str | None = None
anthropic_subagent_model: str | None = None

# OpenAI 配置（按 provider 分组）
openai_model: str | None = None  # 主模型，不设硬编码默认值（见 D9）
openai_subagent_fast_model: str | None = None  # 可选覆盖，如 gpt-4o-mini
openai_memory_selector_model: str | None = None
openai_memory_extractor_model: str | None = None
openai_memory_dream_model: str | None = None
openai_compact_model: str | None = None
openai_agent_model: str | None = None
openai_subagent_model: str | None = None

# LLM 调用通用配置
llm_default_max_tokens: int = 131072
llm_default_timeout_seconds: float | None = None  # None = SDK 自动计算
llm_default_temperature: float = 1.0

# 按场景覆盖 max_tokens（可选）
llm_compact_max_tokens: int = 131072
llm_memory_extractor_max_tokens: int = 4096
llm_memory_dream_max_tokens: int = 8192
llm_memory_selector_max_tokens: int = 256
```

**R1.3 配置文件同步**

同步更新 `.env` 和 `.env.example`，确保三处（代码/实际/示例）一致。

### R2: 统一实例化方式

**R2.1 所有服务支持依赖注入**

修改所有服务的 `__init__()`（包括 `AgentRuntime`、`SubAgentRunner`、`CompactService`、Memory 系列）：

```python
def __init__(self, ..., llm: BaseLLMClient | None = None):
    self.llm = llm or create_llm_client(scenario="xxx")
```

**R2.2 调用方统一迁移**

7 处调用方全部改为通过工厂函数或依赖注入获取 Client，不保留旧 import 方式。所有 `from llm.client import LLMClient` 和 `LLMClient()` 直接实例化的代码全部替换为 `create_llm_client(scenario="xxx")`。

### R3: 暴露 SDK 内置重试配置

Anthropic SDK（`AsyncAnthropic`）和 OpenAI SDK（`AsyncOpenAI`）都内置了重试机制：
- **默认重试次数**：2 次
- **重试条件**：429 (Rate Limit)、5xx (Server Error)、网络超时
- **退避策略**：指数退避

MVP 只需将这两个配置参数暴露为 settings 配置项：

```python
# Anthropic SDK 重试配置
anthropic_max_retries: int = 2
anthropic_timeout_seconds: float = 600.0

# OpenAI SDK 重试配置
openai_max_retries: int = 2
openai_timeout_seconds: float = 600.0
```

各调用方现有的错误处理逻辑保持不变，不做统一的异常分类器或重试装饰器。

后续迭代可考虑统一异常分类和错误增强（Out of Scope）。

### R4: Provider 抽象层

**R4.1 定义 BaseLLMClient 接口**

在 `llm/base.py` 新增抽象基类：

```python
class BaseLLMClient(ABC):
    @abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用，返回该 provider 的原始事件流；归一化由翻译器层负责。"""
        
    @abstractmethod
    async def complete_structured(
        self,
        messages: list[dict[str, Any]],
        response_model: type[T],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
        **kwargs,
    ) -> T:
        """结构化输出，返回 Pydantic 模型实例。"""
```

**R4.2 实现双 Client**

- `llm/anthropic_client.py`：重命名现有 `LLMClient`，实现 `BaseLLMClient`
- `llm/openai_client.py`：新增 OpenAI 实现
  - 流式：吐 OpenAI 原始 `ChatCompletionChunk`，归一化交给翻译器层
  - 结构化：使用 Structured Outputs（`response_format` + `parse()`）
  - 配置映射：`max_tokens` → `max_completion_tokens`
- 两个 Client 都只做 SDK 调用与参数透传，不承担消息格式翻译

**R4.2b 实现双向翻译器**

每 provider 一个翻译器，负责「AgentOS 协议 ↔ provider wire 格式」双向转换：
出站转 messages/system/tools，入站转流式事件与落库消息。
Anthropic 入站部分已由现有 `AnthropicStreamTranslator`（`llm/types.py:118`）承担，
出站部分已由 `SessionService._messages_to_context()`（`session/service.py:468`）事实承担，
本次将其显式化并按 `llm_provider` 分派。

**R4.3 配置驱动工厂**

在 `llm/factory.py` 新增：

```python
def create_llm_client(scenario: str | None = None) -> BaseLLMClient:
    """根据全局 provider 和场景创建对应的 LLM Client。

    权威实现见 design.md D4.2，本处仅示意结构。要点：
    - provider 未配置 / 取值不支持 / api_key 缺失 / 模型解析不出，一律抛中文 AgentException
    - 模型在**工厂层**解析完成后作为必填参数传给客户端，客户端不再自行读配置

    Args:
        scenario: 场景标识，用于解析场景专用模型配置
                 可选值：agent, subagent, subagent_fast, compact,
                        memory_selector, memory_extractor, memory_dream
                 None 表示直接使用 provider 主模型

    Returns:
        BaseLLMClient 实例（AnthropicClient 或 OpenAIClient）
    """
    provider = settings.llm_provider
    if not provider:
        raise AgentException.message(
            "未配置 LLM provider，请在 .env 中设置 AGENTOS_LLM_PROVIDER"
            "（可选值：anthropic / openai）"
        )

    model = _resolve_model(provider, scenario)  # 解析不出则抛中文错

    if provider == "anthropic":
        # api_key 缺失时抛中文错，详见 design.md D4.2
        return AnthropicClient(model=model, ...)
    if provider == "openai":
        return OpenAIClient(model=model, ...)

    raise AgentException.message(
        f"不支持的 LLM provider：{provider}，可选值：anthropic / openai"
    )
```

**R4.4 场景配置解析**

模型解析由工厂层的 `_resolve_model(provider, scenario)` 统一完成，**客户端不参与**：
按 `{provider}_{scenario}_model` → `{provider}_model` 的顺序取值，两级都为空则抛中文
`AgentException`（不做硬编码模型兜底）。权威实现见 design.md D4.2。

### R5: 接口稳定性

**R5.1 BaseLLMClient 公开接口统一**

`stream()` 和 `complete_structured()` 的参数签名在两个实现中保持一致，内部读取新增的 settings 配置。

**R5.2 配置优先级**

```
方法参数 > 场景专用配置 > 默认配置
```

示例：`stream(model="claude-3-5-sonnet")` > `settings.llm_compact_max_tokens` > `settings.llm_default_max_tokens`

---

## 范围

### In Scope

1. ✅ 修改 `core/config.py`：新增 `llm_provider` 全局开关、按 provider 分组的模型配置（主模型 + 场景覆盖）
2. ✅ 同步 `.env` 和 `.env.example`
3. ✅ 新增 `llm/base.py`：定义 `BaseLLMClient` 抽象接口
4. ✅ 重构 `llm/client.py` → `llm/anthropic_client.py`：实现 `BaseLLMClient`，读取 Anthropic 配置
5. ✅ 新增 `llm/openai_client.py`：实现 OpenAI SDK 适配
6. ✅ 新增 `llm/factory.py`：实现 `create_llm_client(scenario)` 工厂函数
7. ✅ 修改所有服务（`runtime/agent.py`、`runtime/subagent.py`、Memory 系列、Compact）：统一通过依赖注入 + 工厂函数创建 Client
8. ✅ 更新相关测试：验证配置回落、依赖注入、双 provider 实现、全局切换

### Out of Scope

1. ❌ 不做自定义异常分类器和重试装饰器（仅暴露 SDK 内置重试配置）
2. ❌ 不引入全局单例或连接池（性能优化留待后续）
3. ❌ 不改变 SubAgent 的档位语义（默认档 / 快速档仍分两档；仅配置项改名并纳入 provider 组）
4. ❌ 不改变 `AnthropicStreamTranslator` 的既有事件翻译逻辑（它就是 Anthropic 侧的入站翻译器）
5. ❌ 不做运行时 provider 切换（provider 在配置中固定，重启后生效）
6. ❌ 不迁移存量消息数据（库中已有数据本就是 AgentOS 协议，见 D8，无需迁移）
7. ❌ 本任务不实现 OpenAI 侧翻译器与 OpenAI 流式（随 OpenAI 流式支持一并落地）；
   本任务只确立翻译器层的边界与职责，并把 Anthropic 侧既有实现归位

---

## 验收标准

### AC1: 配置统一

- [ ] 所有 LLM 调用的 `max_tokens` 和 `timeout` 都来自 `settings.llm_*`，代码中无硬编码
- [ ] 所有 model 配置字段已直接重命名为 `{provider}_{场景}_model` 格式（如 `anthropic_memory_selector_model`），不保留旧配置名兼容映射
- [ ] `anthropic_memory_dream_model` 直接回落到 `anthropic_model`，不再继承 `anthropic_memory_extractor_model`
- [ ] `.env`、`.env.example`、`core/config.py` 中的配置三处同步，且使用新的命名格式

### AC2: 依赖注入

- [ ] 所有服务（`AgentRuntime`、`SubAgentRunner`、`CompactService`、Memory 系列）的 `__init__` 接受可选 `llm: BaseLLMClient | None` 参数
- [ ] Service 的上层调用者无需改动（构造参数向后兼容）；7 处 LLM 实例化点全部迁移到工厂函数
- [ ] 测试中可注入 mock BaseLLMClient 验证行为

### AC3: 接口一致性

- [ ] 方法参数可覆盖 settings 配置（优先级：参数 > settings）
- [ ] 现有功能测试全部通过

### AC4: 文档与测试

- [ ] 更新 `CLAUDE.md` 中关于 LLM 配置的说明
- [ ] 补充单元测试：配置回落链、参数覆盖、依赖注入
- [ ] 补充单元测试：provider 未配置 / 取值不支持 / api_key 缺失 / 模型解析不出，
      四种情况均抛中文 `AgentException`
- [ ] 补充集成测试：验证各场景（Agent/SubAgent/Memory/Compact）的实际调用
- [ ] 验证 Anthropic 链路的流式与结构化输出；OpenAI 仅验证结构化输出
      （OpenAI 流式见 Out of Scope 第 7 条）

### AC5: Provider 抽象

- [ ] `BaseLLMClient` 接口定义清晰，包含 `stream()` 和 `complete_structured()` 两个核心方法
- [ ] `AnthropicClient` 和 `OpenAIClient` 都实现 `BaseLLMClient` 接口，且都不含格式翻译逻辑
- [ ] `create_llm_client(scenario)` 工厂函数根据 `settings.llm_provider` 返回对应 Client
- [ ] 所有服务（Agent/SubAgent/Memory/Compact）统一通过工厂函数创建 Client
- [ ] OpenAI 结构化输出使用 Structured Outputs API
- [ ] 修改 `.env` 中的 `AGENTOS_LLM_PROVIDER` 可全局切换 provider

---

## 技术说明

### Provider 抽象设计

**核心思路**：定义统一接口，封装 SDK 差异，保持调用方透明。

**接口设计**：

```python
class BaseLLMClient(ABC):
    """所有 LLM Provider 的抽象基类。"""
    
    @abstractmethod
    def stream(...) -> AsyncIterator[dict[str, Any]]:
        """流式调用，返回该 provider 的原始事件流；归一化由翻译器层负责。"""
    
    @abstractmethod
    async def complete_structured(...) -> T:
        """结构化输出，返回 Pydantic 实例。"""
```

**实现职责**：

- **AnthropicClient**：封装 Anthropic SDK，保持现有逻辑，根据 `scenario` 参数解析对应的 Anthropic 模型配置
- **OpenAIClient**：封装 OpenAI SDK，只调 SDK 与透传参数；不做格式翻译（由 OpenAI 侧翻译器承担，见 D8）。构造时接收工厂解析好的 `model`
- **工厂函数**：根据 `settings.llm_provider` 决定实例化哪个 Client，传递 `scenario` 参数

**调用路径**：

```
所有服务（Agent/SubAgent/Memory/Compact）
  → create_llm_client(scenario="xxx")
  → 读取 settings.llm_provider
  → AnthropicClient(scenario) 或 OpenAIClient(scenario)
  → BaseLLMClient
```

### OpenAI 适配要点

**1. 流式事件翻译**

OpenAI 返回 `ChatCompletionChunk`，需翻译为统一格式：

| OpenAI 事件 | 统一事件 | 说明 |
|------------|---------|------|
| `delta.content` | `{"type": "content_block_delta", "delta": {"type": "text_delta", "text": ...}}` | 文本增量 |
| `delta.tool_calls` | `{"type": "content_block_start", "content_block": {"type": "tool_use", ...}}` | 工具调用 |
| `finish_reason="stop"` | `{"type": "message_stop"}` | 消息结束 |

**2. 结构化输出**

使用 OpenAI Structured Outputs API（`response_format` + `parse()`）：

```python
completion = await client.beta.chat.completions.parse(
    model=model,
    messages=messages,
    response_format=response_model,  # Pydantic 模型
)
return completion.choices[0].message.parsed  # 直接返回类型化对象
```

**3. 配置参数映射**

| 统一参数 | Anthropic | OpenAI |
|---------|-----------|--------|
| `max_tokens` | `max_tokens` | `max_completion_tokens` |
| `system` | `system` (单独参数) | `messages[0]` (role="system") |
| `temperature` | `temperature` | `temperature` |

### 配置回落实现

模型解析在**工厂层**的 `_resolve_model(provider, scenario)` 完成（权威实现见 design.md D4.2），
**不在客户端内部**：客户端构造时收到的已是解析好的 `model`。

优先级：方法参数 > `{provider}_{scenario}_model` 场景覆盖 > `{provider}_model` 主模型 >
**抛中文 `AgentException`**（不存在硬编码模型兜底，见 D9）。

```python
# 工厂内部，provider 与 scenario 均由调用方给出
model = _resolve_model(settings.llm_provider, "memory_selector")
# → anthropic_memory_selector_model，未设则 anthropic_model，都没有则抛错
```

### 依赖注入实现

修改服务构造器，示例（AgentRuntime）：

```python
class AgentRuntime:
    def __init__(
        self,
        session_id: int,
        ...,
        llm: BaseLLMClient | None = None,  # 新增参数
    ):
        self.session_id = session_id
        ...
        # 回落目标只能是工厂函数（内部校验并抛中文错），
        # 不得写成 llm or AnthropicClient()——那是 D9 明令禁止的静默兜底
        self.llm = llm or create_llm_client(scenario="agent")
```

测试中注入 mock：

```python
mock_llm = Mock(spec=BaseLLMClient)
runtime = AgentRuntime(session_id=1, llm=mock_llm)
```

### 配置迁移清单

本任务只迁移 `max_tokens`。三处 `timeout` 已在现有代码中读取 `settings`
（`extraction.py:331`、`dream.py:297`、`recall/service.py:171,177`），无需再动。

| 硬编码位置 | 迁移到 |
|----------|--------|
| `llm/anthropic_client.py:55` `max_tokens=131072` | `settings.llm_default_max_tokens` |
| `memory/jobs/extraction.py:330` `max_tokens=4096` | `settings.llm_memory_extractor_max_tokens` |
| `memory/jobs/dream.py:296` `max_tokens=8192` | `settings.llm_memory_dream_max_tokens` |
| `complete_structured` 签名默认值 `max_tokens: int = 256`（`llm/base.py:39`、`llm/anthropic_client.py:112`）；Memory 选择器未显式传值 | `settings.llm_memory_selector_max_tokens`，由 `recall/service.py` 显式传入 |

---

## 风险与缓解

### 风险1: 配置字段重命名和回落变更可能影响现有行为

**影响范围**：
1. 所有 model 配置字段从 `{场景}_model` 重命名为 `{provider}_{场景}_model`
2. `anthropic_memory_dream_model` 的回落链从 3 层改为 2 层

**缓解**：
- 配置字段直接重命名，不保留旧名兼容映射（需要用户手动更新 `.env`）
- 当前 `.env` 中 `memory_extractor_model` 和 `memory_dream_model` 都未设置时，重命名后行为不变（都回落到 `anthropic_model`）
- 如果 `.env` 中只设置了 `memory_extractor_model`，迁移后需重命名为 `anthropic_memory_extractor_model`，Dream 将不再继承它

**建议**：迁移前检查 `.env` 配置，提供配置重命名清单，确保用户明确迁移路径。

### 风险2: 依赖注入参数可能与现有参数冲突

**影响范围**：`AgentRuntime` 和 `SubAgentRunner` 的构造器参数列表变长

**缓解**：
- 新参数放在最后，设置默认值 `None`
- 7 处调用方统一显式迁移，使用依赖注入或工厂函数

### 风险3: OpenAI 事件翻译可能遗漏边界情况

**影响范围**：OpenAI 流式事件与 AgentOS 协议事件差异较大，入站翻译可能不完整

**缓解**：
- 优先支持核心场景（文本生成、工具调用、结构化输出）
- Memory 系列主要使用结构化输出，流式翻译复杂度较低
- 补充集成测试验证 OpenAI Client 在实际场景中的行为
- OpenAI 流式与 OpenAI 侧翻译器不在本任务范围（见 Out of Scope 第 7 条），
  本任务只落地 Anthropic 链路，风险随后续任务再评估

### 风险4: 测试覆盖不足可能遗漏回归

**缓解**：
- 优先运行现有集成测试，确保功能不退化
- 补充单元测试覆盖配置回落、参数覆盖、依赖注入、双 provider 实现
- 在测试环境验证 Memory 提取/Dream/Selector 的实际调用
- 使用 OpenAI 官方 mock/fixture 验证适配层逻辑

### 风险5: 多 provider 配置复杂度增加

**影响范围**：新增 OpenAI API key、base URL、model 配置项

**缓解**：
- OpenAI 配置仅在全局 `llm_provider="openai"` 时必填（不存在按场景拆分的 provider）
- `llm_provider` 必填无默认值：升级的现有用户需在 `.env` 显式写入
  `AGENTOS_LLM_PROVIDER=anthropic`，否则启动自检直接报错（见 D9）
- `.env.example` 提供清晰的配置示例和注释

---

## 关键决策

### D1: 配置方案选择

✅ **已确定**：采用方案 A（按 provider 分组配置模型），优化为 "provider 默认主模型 + 场景可选覆盖"

**理由**：
- 一个 `llm_provider` 全局开关控制整个 AgentOS
- 每个 provider 有一个主模型（如 `anthropic_model`），所有场景默认使用
- 场景可选覆盖（如 `anthropic_subagent_fast_model`），未设置则继承主模型
- 配置清晰，避免跨 provider 混用的复杂性

### D2: 依赖注入方式

✅ **已确定**：构造器可选参数 `llm: BaseLLMClient | None = None`，未注入时回落到
`create_llm_client(scenario)`。注意回落目标只能是工厂函数（工厂内部会校验并抛中文错误），
**不得写成 `llm or AnthropicClient()` 这类硬编码 provider 的静默兜底**。

### D3: 配置优先级

✅ **已确定**：方法参数 > 场景配置 > 默认配置

### D4: Provider 抽象架构

✅ **已确定**：定义 `BaseLLMClient` 接口，实现 `AnthropicClient` 和 `OpenAIClient`，通过工厂函数 `create_llm_client(scenario)` 按全局 provider 创建

### D5: 所有服务是否统一使用工厂函数

✅ **已确定**：**统一使用**。所有服务（`AgentRuntime`、`SubAgentRunner`、Memory 系列、`CompactService`）都通过 `create_llm_client(scenario)` 创建 Client，不再有硬编码 `AnthropicClient()` 的例外

**理由**：
- 实现全局统一切换：一个配置项控制整个系统的 provider
- 保持架构一致性：所有 LLM 调用都走统一入口
- 简化维护：不需要为不同服务维护不同的实例化逻辑
- 测试友好：所有服务都支持依赖注入

### D6: 错误处理边界

✅ **已确定**：仅暴露 SDK 内置重试配置（`max_retries`/`timeout`），不做自定义异常分类器和重试装饰器

### D7: 删除废弃方法

✅ **已确定**：删除 `_complete_structured()` 方法

**理由**：该方法为官方 API 的 `output` 配置提供兼容，但 server-side output_config 在第三方 API 中转（如 one-api）时会被静默丢弃，导致结构化输出失败。保留该方法会误导用户，应直接删除，统一使用 tool-use 强制的 `complete_structured()` 方法（通过 `emit_structured_output` 工具实现结构化输出）。

### D8: 数据库消息存储格式

✅ **已确定**：数据库存 **AgentOS 自有协议格式**，不存任何 provider 的 wire 格式。
所有 provider 统一经翻译器转成 AgentOS 协议后落库；出库送模型时再翻译成目标 provider 格式。
翻译器独立成层（每 provider 一个），不下沉进 LLM 客户端。

**理由**：
- AgentOS 协议事实上已经存在：`session_messages.role` 有 Anthropic 没有的 `"tool"` 角色，
  `ToolResultMessage` 落库结构带 Anthropic `tool_result` block 没有的 `tool_name`/`input_args`。
  本决策是承认并显式化现状，不是新造抽象
- 存 provider 原始格式的代价是所有读取方（Memory 提取、召回、API 序列化、外部消费方）都要写 provider 分支，
  且历史数据永久绑定当年那家 provider 的格式；新增 provider 的成本从 O(1) 变成 O(读取方数量)
- AgentOS 协议对 OpenAI 反而更贴合：`tool_use_id`/`tool_name`/`input_args`/`output`
  与 OpenAI 的 `tool_calls` + `{role:"tool", tool_call_id}` 一一对应；
  若存 Anthropic wire 格式，转 OpenAI 时还要回溯前一个 `tool_use` block 才能拿到 `tool_name`
- 翻译放客户端内部会形成「AgentOS 协议 → Anthropic → OpenAI」双跳，中间一跳是纯损耗

### D9: provider 配置缺失时的行为

✅ **已确定**：**无静默兜底**。`llm_provider` 不设默认值；provider 未配置、取值不支持、
对应 api_key 缺失、模型解析不出，一律抛具体中文 `AgentException`。
校验落在 `create_llm_client()`（权威关卡），另在 `main.py` 的 lifespan 中调用一次做启动自检，
让配置错误在启动时暴露而非拖到首次对话。

**理由**：静默回落到 Anthropic 会让「配置写错」表现为「跑起来了但用的不是你以为的模型」，
账单和行为都对不上，且极难排查。

---

## 相关文件

### 需修改
- `core/config.py` - 新增 `llm_*` 配置项（provider、OpenAI 配置、场景专用配置）
- `.env` / `.env.example` - 同步配置
- `llm/client.py` → `llm/anthropic_client.py` - 重命名并实现 `BaseLLMClient`
- `runtime/agent.py` - 添加依赖注入参数（未注入时经 `create_llm_client(scenario="agent")` 创建）
- `runtime/subagent.py` - 添加依赖注入参数（未注入时经 `create_llm_client(scenario="subagent")` 创建）
- `runtime/compact.py` - 通过工厂函数创建可配置的 Client
- `memory/jobs/extraction.py` - 通过工厂函数创建可配置的 Client
- `memory/jobs/dream.py` - 通过工厂函数创建可配置的 Client，修改 model 回落链
- `memory/recall/service.py` - 通过工厂函数创建可配置的 Client
- `memory/jobs/service.py` - 停止持有并向下传递单个共享 `self.llm`：
  extraction 与 dream 需要不同模型且超时不同（20s / 60s），必须各自按 scenario 建客户端
- `main.py` - lifespan 中调用一次 `create_llm_client()` 做启动自检（见 D9）

### 需新增
- `llm/base.py` - 定义 `BaseLLMClient` 抽象接口
- `llm/openai_client.py` - 实现 OpenAI SDK 适配
- `llm/factory.py` - 实现 `create_llm_client()` 工厂函数

### 需新增测试
- `tests/test_llm_base_client.py` - 测试 `BaseLLMClient` 接口契约
- `tests/test_llm_anthropic_client.py` - 测试 AnthropicClient 实现（配置回落、参数覆盖）
- `tests/test_llm_openai_client.py` - 测试 OpenAIClient 实现（事件翻译、结构化输出）
- `tests/test_llm_factory.py` - 测试工厂函数（provider 选择、配置传递）
- `tests/test_runtime_agent.py` - 测试依赖注入
- `tests/test_memory_integration.py` - 测试 Memory 系列配置验证（双 provider）

---

## 影响评估

### 文件数量
- **原估计**：9 个文件
- **新估计**：12 个核心文件 + 6 个测试文件 = **18 个文件**

### 改动量
- **原估计**：~20 处修改
- **新估计**：~50 处修改
  - 新增 3 个 llm/ 核心文件（~800 行）
  - 重构现有 `LLMClient` 为 `AnthropicClient`（~200 行）
  - 修改配置和调用方（~300 行）
  - 新增测试（~600 行）

### 工程量
- **原估计**：1-2 天
- **新估计**：**3-4 天**
  - Day 1: Provider 抽象层（base.py + anthropic_client.py 重构）
  - Day 2: OpenAI 适配（openai_client.py + 流式翻译 + 结构化输出）
  - Day 3: 配置整合 + 调用方迁移（config.py + 9 个调用文件）
  - Day 4: 测试补充 + 集成验证 + 文档更新
