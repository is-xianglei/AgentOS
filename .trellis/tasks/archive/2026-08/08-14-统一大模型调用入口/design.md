# Design: 统一大模型调用入口

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                        Service 层                            │
│  AgentRuntime │ SubAgentRunner │ MemoryService │ Compact    │
└────────┬──────────────┬─────────────┬──────────────┬────────┘
         │              │             │              │
         │ (注入/工厂)  │ (注入/工厂) │ (注入/工厂)  │ (注入/工厂)
         │              │             │              │
         ▼              ▼             ▼              ▼
┌────────────────────────────────────────────────────────────┐
│         MessageTranslator 层（按 llm_provider 分派）         │
│  出站：AgentOS 协议 → provider wire 格式                    │
│  入站：provider 原始事件 → AgentOS 协议事件                  │
│  （数据库只认 AgentOS 协议，详见「消息格式边界」）             │
└────────┬───────────────────────────────────────┬───────────┘
         │                                       │
         ▼                                       ▼
┌────────────────────────────────────────────────────────────┐
│                   BaseLLMClient (接口)                      │
│  • stream() → AsyncIterator[dict]（原始事件，不归一化）      │
│  • complete_structured() → T                               │
└────────┬───────────────────────────────────────┬───────────┘
         │                                       │
         ▼                                       ▼
┌─────────────────────┐              ┌─────────────────────┐
│  AnthropicClient    │              │   OpenAIClient      │
│  (重构现有LLMClient)│              │   (新增实现)        │
├─────────────────────┤              ├─────────────────────┤
│ • AsyncAnthropic    │              │ • AsyncOpenAI       │
│ • stream() 吐原始事件│              │ • stream() 吐原始事件│
│ • tool use 强制     │              │ • response_format   │
│ • Prompt Caching    │              │ • 只调 SDK，不翻译   │
└─────────────────────┘              └─────────────────────┘
         │                                       │
         ▼                                       ▼
┌────────────────────────────────────────────────────────────┐
│              Anthropic SDK      │      OpenAI SDK          │
└────────────────────────────────────────────────────────────┘
```

**设计原则**：
1. **全局统一切换**：一个 `llm_provider` 开关控制整个 AgentOS 的 LLM 提供商
2. **场景档位分离**：同一 provider 下，不同场景使用不同档位模型（主 Agent、子 Agent、Memory 等）
3. **配置分组清晰**：按 provider 分组配置，每组包含主模型和场景覆盖字段
4. **保留原生能力**：Anthropic 保留 Caching/thinking，OpenAI 保留 Structured Outputs
5. **无静默兜底**：`llm_provider` 不设默认值；provider 未配置、取值不支持、api_key 缺失、
   模型解析不出，一律抛具体中文 `AgentException`，禁止硬编码 `AnthropicClient()` 之类的回落

---

## 核心设计决策

### D1: BaseLLMClient 接口签名

**决策**：定义统一接口，参数名以清晰合理为准。

```python
class BaseLLMClient(ABC):
    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        system_prompt: str | list[TextBlockParam] | None = None,
        tools: list[ToolParam] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMRawChunk]:
        """
        流式调用，返回该 provider 的原始事件流，不在此处做归一化。

        统一成 AgentOS 协议是翻译器层的职责（见「消息格式边界」），客户端保持薄。
        故此处用 def 而非 async def：实现类是异步生成器，
        写成 async def 会让返回值变成协程而非可直接 async for 的迭代器。

        事件格式：
        - Anthropic: content_block_start/delta/stop, message_final
        - OpenAI: choices[].delta, finish_reason, usage
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
        """结构化输出，返回 Pydantic 实例。"""
```

**理由**：
- 接口统一两家 SDK 的核心能力（流式调用、结构化输出）
- 7 处调用方直接迁移到工厂函数或新接口
- 格式差异由翻译器层吸收，客户端只做 SDK 调用与参数透传（下文详述）

### D2: AnthropicClient 重构策略

> **状态**：本节第 1、2、3、5 项已在代码中完成（`llm/anthropic_client.py` 共 204 行，
> `AnthropicClient(BaseLLMClient)` 在 `:19`，`llm/client.py` 已不存在，
> `llm/__init__.py` 直接导出 `AnthropicClient` 且无 `LLMClient` 别名）。
> 保留本节是为了记录决策依据，实施时只需核对剩余项。

**现状**：`llm/anthropic_client.py` 的 `AnthropicClient` 类包含：
- `stream()`（`:34`）
- `_log_cache_usage()`（`:86`，静态方法）
- `complete_structured()`（`:105`）

**改动**：
1. ~~重命名 `llm/client.py` → `llm/anthropic_client.py`~~（已完成）
2. ~~类名 `LLMClient` → `AnthropicClient`~~（已完成）
3. ~~继承 `BaseLLMClient`~~（已完成，`llm/base.py:10` 定义接口）
4. ~~`stream()` 签名与基类不一致，需统一为 `def`~~
   （**误判，已撤销**：基类 `def` + 实现 `async def` + `yield` 是刻意写法，不是缺陷。
   带 `yield` 的 `async def` 是**异步生成器函数**，调用即得 `AsyncGenerator`、**无需 await**，
   与基类 `def stream(...) -> AsyncIterator` 的调用语义完全一致。
   `llm/base.py:23-24` 的 docstring 已写明「故此处不声明 async，避免签名被推断为
   『返回 AsyncIterator 的协程』」；`llm/openai_client.py:83` 更放了不可达的 `yield {}`
   并注释「使本方法成为 async generator，与基类签名保持一致」。
   已运行时验证：`inspect.isasyncgen()` 为 True、`iscoroutine()` 为 False，
   不加 await 可直接 `async for`。项目也未配置 mypy/pyright，仅 ruff 不做类型推断。
   **保持现状，不要改。**）
5. ~~更新 `llm/__init__.py` 导出~~（已完成）

**影响**：
- 所有调用方改为 `from llm import create_llm_client` 或通过依赖注入获取客户端
- 需要直接使用 `AnthropicClient` 的地方显式导入

### D3: OpenAI 适配

> 本节涉及的格式翻译由翻译器层承担，**不写进 `OpenAIClient`**（见「消息格式边界」）。
> 客户端只负责调 SDK 与吐原始事件。

#### 3.1 流式事件翻译

两家的原始事件各自翻译成**同一套 AgentOS 协议事件**，而不是把一家翻译成另一家。
现有 `AnthropicStreamTranslator`（`llm/types.py:118`）产出的事件即当前的事实标准：

| AgentOS 协议事件 | Anthropic 原始事件 | OpenAI 原始事件 |
|---|---|---|
| 内容块开始 | `content_block_start` | `choices[].delta` 首次非空（无显式 start，需推断） |
| 文本增量 | `content_block_delta` (text_delta) | `choices[].delta.content` |
| 工具参数增量 | `content_block_delta` (input_json_delta) | `choices[].delta.tool_calls[].function.arguments` |
| 内容块结束 | `content_block_stop` | `finish_reason='tool_calls'` |
| 消息结束 + usage | `message_final` | `finish_reason='stop'` + `usage` |

**实现要点**：
- OpenAI 的 tool_calls `arguments` 是流式 JSON 字符串，需累积后 `json.loads()`
- usage 字段名不同：Anthropic 为 `input_tokens`/`output_tokens`，
  OpenAI 为 `prompt_tokens`/`completion_tokens`，协议层统一后再落库
- OpenAI 无 `thinking` 类型（reasoning 模型的 `reasoning_content` 不在流式 delta 中），
  该协议事件对 OpenAI 恒不产出
- `AnthropicStreamTranslator` 的 block index **直接透传 Anthropic 原始 index,每次 API
  调用从 0 重新开始**(`llm/types.py:_on_block_start`),并非跨调用递增。
  一个 turn 内发生 tool use 多轮时,消费方会重复收到 index=0。
  **OpenAI 侧翻译器对齐这一「每次调用内计数」语义即可**,无需自造跨调用累加器。
  index 只用于关联同一次调用内的 block_start/delta/stop,不是全局唯一标识。

  > 曾有文档声称「持续递增」(`llm/types.py:121` docstring 与 `runtime/agent.py:564`
  > 注释),与实现不符,已于 2026-08-15 一并改为如实描述。
  > **决定改文档而非改代码**,原因:index 已进入对外 SSE 协议
  > (`core/events.py:113-123` `BlockInfo.to_dict()`),真正的消费方是前端,而本仓库
  > 没有前端工程,无法验证改语义的影响;本仓库内则无任何消费方依赖它
  > (后端只做原样转发,不落库——消息按 `tool_use_id` 关联,且 tests/ 已在 `e872cdc`
  > 整体删除、历史上从无 index 断言)。在看不见的消费方上改对外协议,风险不对称。
  >
  > 若日后确需改为跨调用递增:翻译器 `_open: dict[int, _OpenBlock]` 在**三处**用
  > index 做关联键(`llm/types.py:162` 写、`:186` 读、`:197` pop),只改
  > `_on_block_start` 会打断 tool_use 入参累积(工具参数变空)。须三处一致,或采用
  > 「内部沿用原始 index、仅对外发 offset 后的值」。前提条件已具备:三处循环
  > (`runtime/agent.py:565`、`runtime/subagent.py:124` 与 `:436`)都复用同一翻译器实例。
- 补充协议事实:tool_use 块在 `_on_block_stop` 走 tool_use 事件路径时**不发
  block_stop**(`llm/types.py:200-207`,注释表明是有意为之),即 block_start/block_stop
  本就不成对,消费方不能假设「每个 index 必有 stop」。

#### 3.2 结构化输出

**Anthropic 方式**（保持）：
```python
# tool use 强制 + 回灌重试
tool = {"name": "_emit", "input_schema": Model.model_json_schema(), "strict": True}
response = await client.messages.create(..., tools=[tool], tool_choice={"type": "tool", "name": "_emit"})
```

**OpenAI 方式**（新增）：
```python
# Structured Outputs API（新版 SDK：parse 已从 beta 提升到正式命名空间）
response = await client.chat.completions.parse(
    model=model,
    messages=messages,
    response_format=Model,  # Pydantic 模型直接传入，SDK 自动转 JSON Schema
    max_completion_tokens=max_tokens,
)
parsed = response.choices[0].message.parsed  # SDK 自动校验
```

**关键差异**（已用 Context7 核实 openai-python 官方文档）：
- **API 位置**：新版用 `client.chat.completions.parse()`；旧版本才是 `client.beta.chat.completions.parse()`。实施时按实际安装的 SDK 版本核对（见 implement.md Phase 2）
- OpenAI 原生支持 `response_format`，无需包装成 tool
- 可能返回 `message.refusal`（模型拒答）而非 `parsed`，需处理为与 Anthropic 一致的 ValueError
- 校验失败时 SDK 抛异常（无内置回灌重试），行为需对齐 Anthropic 的"重试一次"
- `max_tokens` 参数名是 `max_completion_tokens`（需映射；旧 `max_tokens` 已废弃但仍兼容）

#### 3.3 配置参数映射

| 通用参数 | Anthropic | OpenAI | OpenAIClient 映射 |
|---------|-----------|--------|------------------|
| `system_prompt` | `system` (str/list) | `messages[0]` role='system' | 转为首条消息 |
| `max_tokens` | `max_tokens` | `max_completion_tokens` | 重命名参数 |
| `timeout` | `timeout` | `timeout` | 直接透传 |
| `tools` | `tools` (ToolParam) | `tools` (function format) | 转换 schema 结构 |

**system_prompt 转换**：
```python
# Anthropic: system 是独立参数
client.messages.create(system="...", messages=[...])

# OpenAI: system 是首条消息
client.chat.completions.create(messages=[
    {"role": "system", "content": "..."},
    ...
])
```

### D4: 工厂函数与依赖注入

#### 4.1 场景枚举

系统定义以下场景，每个场景可独立配置模型档位：

| 场景标识 | 说明 | 典型用例 | 推荐档位 |
|---------|------|---------|---------|
| `agent` | 主 Agent 运行时 | 用户任务执行、工具调用 | Anthropic: opus/sonnet, OpenAI: gpt-4o |
| `subagent` | 子 Agent 默认档位 | 通用子任务 | Anthropic: sonnet, OpenAI: gpt-4o-mini |
| `subagent_fast` | 子 Agent 快速档位 | 简单查询、快速响应 | Anthropic: haiku, OpenAI: gpt-4o-mini |
| `memory_selector` | 记忆召回选择器 | 高频调用、成本敏感 | Anthropic: haiku, OpenAI: gpt-4o-mini |
| `memory_extractor` | 记忆提取 | 结构化信息抽取 | Anthropic: sonnet, OpenAI: gpt-4o-mini |
| `memory_dream` | 记忆梦境推理 | 深度分析、语义关联 | Anthropic: sonnet, OpenAI: gpt-4o |
| `compact` | 上下文压缩 | 长文本摘要（依赖 Caching） | Anthropic: sonnet |

#### 4.2 工厂函数设计

**核心函数**（`llm/factory.py`）：
```python
def create_llm_client(scenario: str | None = None) -> BaseLLMClient:
    """
    创建 LLM 客户端，根据全局 provider 和场景选择模型。

    provider 未配置、取值不支持、api_key 缺失、模型解析不出时一律抛中文
    AgentException，不做任何静默兜底（不存在「默认 Anthropic」这种回落）。

    Args:
        scenario: 场景标识（agent/subagent/memory_selector 等），None 表示使用 provider 主模型

    Returns:
        BaseLLMClient 实例
    """
    provider = settings.llm_provider
    if not provider:
        raise AgentException.message(
            "未配置 LLM provider，请在 .env 中设置 AGENTOS_LLM_PROVIDER"
            "（可选值：anthropic / openai）"
        )

    model = _resolve_model(provider, scenario)

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise AgentException.message(
                "LLM provider 为 anthropic，但未配置 AGENTOS_ANTHROPIC_API_KEY"
            )
        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            model=model,
            max_retries=settings.anthropic_max_retries,
            timeout=settings.anthropic_timeout_seconds,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise AgentException.message(
                "LLM provider 为 openai，但未配置 AGENTOS_OPENAI_API_KEY"
            )
        return OpenAIClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=model,
            max_retries=settings.openai_max_retries,
            timeout=settings.openai_timeout_seconds,
        )

    raise AgentException.message(
        f"不支持的 LLM provider：{provider}，可选值：anthropic / openai"
    )


def _resolve_model(provider: str, scenario: str | None) -> str:
    """
    解析场景对应的模型，按优先级：场景覆盖 > provider 主模型。
    
    Args:
        provider: LLM 提供商（anthropic/openai）
        scenario: 场景标识，None 表示无场景覆盖
    
    Returns:
        模型标识符
    """
    # 构造场景配置字段名：{provider}_{scenario}_model
    if scenario:
        scenario_field = f"{provider}_{scenario}_model"
        scenario_model = getattr(settings, scenario_field, None)
        if scenario_model:
            return scenario_model
    
    # 回退到 provider 主模型
    default_field = f"{provider}_model"
    model = getattr(settings, default_field, None)
    if not model:
        raise AgentException.message(
            f"未配置 {provider} 的模型，请设置 AGENTOS_{provider.upper()}_MODEL，"
            f"或为该场景设置 AGENTOS_{provider.upper()}_<SCENARIO>_MODEL"
        )
    return model
```

**启动自检**：`main.py` 的 lifespan 中调用一次 `create_llm_client()`，让配置错误在应用启动时立刻暴露，
而不是拖到首次对话或首个 Memory 作业。工厂本身仍是权威关卡，worker、脚本等不走 lifespan 的入口
同样跑不掉校验。

#### 4.3 Service 层注入模式

**统一注入模式**：所有 Service 通过 `create_llm_client(scenario)` 创建客户端。

```python
# Agent Runtime (主 Agent)
class AgentRuntime:
    def __init__(self, ..., llm: BaseLLMClient | None = None):
        self.llm = llm or create_llm_client(scenario="agent")

# SubAgent Runner
class SubAgentRunner:
    def __init__(self, ..., llm: BaseLLMClient | None = None):
        # 根据子 Agent 类型选择 subagent 或 subagent_fast
        scenario = "subagent_fast" if fast_mode else "subagent"
        self.llm = llm or create_llm_client(scenario=scenario)

# Memory Extraction Service (classmethod)
class MemoryExtractionService:
    @classmethod
    async def extract_with_llm(cls, ..., llm: BaseLLMClient | None = None):
        client = llm or create_llm_client(scenario="memory_extractor")
        ...

# Memory Recall Service
class MemoryRecallService:
    def __init__(self, ..., llm: BaseLLMClient | None = None):
        self.llm = llm or create_llm_client(scenario="memory_selector")

# Memory Dream Job
async def run_dream_job(..., llm: BaseLLMClient | None = None):
    client = llm or create_llm_client(scenario="memory_dream")
    ...

# Compact Service
class CompactService:
    def __init__(self, llm: BaseLLMClient | None = None):
        self.llm = llm or create_llm_client(scenario="compact")
```

**注入优势**：
- 测试时可注入 Mock 客户端
- 保持默认行为（`None` 时走工厂函数）
- 所有场景统一通过 `settings.llm_provider` 控制

---

## 数据流与边界

### 消息格式边界

**设计约束**：数据库存储的是 **AgentOS 自有协议格式**，不是任何一家 provider 的 wire 格式。
provider 的原始格式在入库前统一经翻译器转成 AgentOS 协议；出库送模型前，再由同一个翻译器
转成目标 provider 的 wire 格式。无论以后接入多少家 provider，库里永远只有一种格式。

> **现状澄清**：本节早期版本写的是「数据库保持 Anthropic 格式」，与代码不符。
> `session_messages.role` 存在 Anthropic 协议中不存在的 `"tool"` 角色；
> `ToolResultMessage.to_content_dict()`（`llm/types.py:42`）落库的
> `{tool_use_id, tool_name, input_args, output, is_error}` 同样是 AgentOS 自有结构
> —— Anthropic 的 `tool_result` block 并不携带 `tool_name` 和 `input_args`。
> 即 AgentOS 协议早已事实存在，`SessionService._messages_to_context()`（`session/service.py:468`）
> 就是既有的出站翻译层。本次改造是把这一层显式化并按 provider 分派，不是新建抽象。

```
              ┌────────────────────────────────────────┐
              │      PostgreSQL session_messages       │
              │      content = AgentOS 协议格式         │
              │            （provider 无关）            │
              └──────┬──────────────────────────▲──────┘
              出站读取 │                          │ 入站落库
                     ▼                          │
              ┌──────────────────────────────────┴──────┐
              │          MessageTranslator 层            │
              │      按 llm_provider 分派，双向翻译       │
              ├────────────────────┬────────────────────┤
              │ AnthropicTranslator│  OpenAITranslator  │
              └─────────┬──────────┴─────────┬──────────┘
                        ▼                    ▼
              ┌──────────────────┐ ┌──────────────────┐
              │  AnthropicClient │ │   OpenAIClient   │
              │   （只调 SDK）    │ │   （只调 SDK）    │
              └──────────────────┘ └──────────────────┘
```

**关键点**：
- 数据库只认 AgentOS 协议，与 provider 解耦；provider 换代或 wire 格式变更不触发数据迁移。
- 每个 provider 提供一对双向翻译：入站「原始事件/响应 → AgentOS 协议 → 落库」，
  出站「AgentOS 协议 → provider wire 格式 → 送 SDK」。
- 翻译器独立成层，**不塞进 `AnthropicClient` / `OpenAIClient` 内部**；客户端保持薄，
  只负责调 SDK 与吐原始事件。
- 出站翻译的落点是 `SessionService._messages_to_context()`（`session/service.py:468`，由 `load_context()` 调用），按当前 provider 分派。
  若改在客户端内部转换，会变成「AgentOS 协议 → Anthropic → OpenAI」的双跳，中间一跳纯属信息损耗。
- 新增 provider 的成本是「一个翻译器」，而不是「每个读取方加一个分支」：
  `memory/jobs/extraction.py`、`memory/recall/repository.py`、`session/api.py`
  以及任何外部消费方，读到的永远是同一种格式。
- 因为库里格式统一，同一会话中途切换 provider 不再有格式障碍
  （是否开放为产品能力另议，但不再被存储格式卡死）。

**AgentOS 协议对 OpenAI 反而更友好**：OpenAI 要求 assistant 消息携带
`tool_calls: [{id, function: {name, arguments}}]`，工具响应是独立的
`{role: "tool", tool_call_id, content}`。AgentOS 规范行里的
`tool_use_id` / `tool_name` / `input_args` / `output` 正好一一对应；
若库里存的是 Anthropic wire 格式，转 OpenAI 时还得回头到前一个 `tool_use` block 里捞 `tool_name`。

### 格式转换边界

**翻译器负责的转换**（每个 provider 一个实现）：

出站（AgentOS 协议 → provider wire 格式）：
1. **system_prompt**：AgentOS system 文本 → Anthropic 顶层 `system` 参数 / OpenAI `messages[0]` role='system'
2. **messages**：AgentOS 规范消息 → 目标 wire 格式，含 tool 调用与结果的结构重组
   （Anthropic：`tool_use` + `tool_result` block 合并进相邻消息；
   OpenAI：assistant `tool_calls` + 独立 `{role:"tool"}` 消息）
3. **tools**：AgentOS 工具声明 → Anthropic `ToolParam` / OpenAI `function` 格式

入站（provider 原始输出 → AgentOS 协议）：
4. **流式事件**：provider 原始事件 → AgentOS 协议事件。
   现有 `AnthropicStreamTranslator`（`llm/types.py:118`）即 Anthropic 的入站实现，
   OpenAI 侧需补一个对等实现，两者产出同一套事件。
5. **落库消息**：provider 响应 → AgentOS 规范消息结构（如 `ToolResultMessage`）

**客户端（`AnthropicClient` / `OpenAIClient`）不负责格式翻译**：
只调 SDK、透传参数、吐原始事件；`stream()` 的返回即各家原始事件流。
这一点与 `implement.md` 步骤 1.1 的模板一致。

**明确不做的转换**：
- ❌ 数据库不存任何 provider 的 wire 格式
- ❌ 翻译逻辑不下沉进 LLM 客户端（否则每加一个 provider 都要改所有客户端）
- ❌ 读取方（Memory、API、外部消费方）不做 provider 分支判断

**已拍板项**（2026-08-15 用户决定）：

- ✅ **给 `session_messages` 增加 `provider` / `model` 元数据列**,用于成本归因与问题复现。
  这属于元数据而非格式,与「库里只存一种格式」不冲突。设计要点:

  - **两列均可空**(`String | None`)。`session_messages` 现有六个写入点,
    只有 `runtime/agent.py:642` 与 `:1611` 两处 assistant 消息是 LLM 产出;
    user 消息(`session/service.py:368`、`agent.py:656`)和 tool 消息
    (`agent.py:982`、`:1025`)不经过模型,**这两列必须留空**——给它们填
    provider 等于伪造归因。存量历史消息同理为 NULL,迁移不做回填。
  - `provider` 存 provider 标识(如 `anthropic`/`openai`),`model` 存**实际生效的
    模型 ID**,不是配置里的档位名。场景档位(agent/subagent/compact 等)会经
    `_resolve_model()` 解析,只有解析后的最终值才对复现有意义。
  - 写入路径:`SessionService.add_message()`(`session/service.py:396`)与
    `SessionRepository.add_message()`(`repository.py:210`)各加一对可选参数,
    默认 `None`,由 `runtime/agent.py` 两处 assistant 写入点显式传入。
    默认值保证其余四个调用点无需改动。
  - 需要显式 Alembic upgrade/downgrade 迁移(当前最新为 `0020`),
    两列 `nullable=True` 且带中文 comment。成本归因若需按 provider 聚合,
    可按需给 `model` 建索引;初期数据量小,不预先建。

- ✅ **暂不做 provider 特有内容的逃生通道**(Anthropic 的 thinking block、
  `cache_control` 断点,OpenAI 的 refusal、logprobs 等)。这些内容在落库时按现有
  翻译规则丢弃,不额外保留 `_raw`。

  > ⚠️ 代价需知悉:这意味着**落库消息无法无损还原成 provider 原始响应**。
  > 已知影响面——Anthropic 的 thinking 内容不入库(当前 `_on_block_delta` 对
  > `signature_delta` 等本就按噪声丢弃),跨 provider 迁移会话时特有语义丢失。
  > 若日后需要,加逃生通道属于**向后兼容的增量改动**(content 内新增 `_raw` 键
  > 或独立列),不影响本次已存的数据;但**已丢弃的历史内容无法追溯补回**。

**迁移编号与共享库**(2026-08-16 用户决定):

- ✅ 本次迁移定为 `0021_add_message_llm_attribution.py`,
  `revision = "0021_message_llm_attribution"`,`down_revision = "0020_agent_mvp_comments"`。

  > ⚠️ `0020` 的文件名是 `0020_align_agent_mvp_comments.py`,但内部 revision id
  > 是 `0020_agent_mvp_comments`(无 `align`)。`down_revision` 必须写 id 不是文件名。

- ⚠️ **与 `multi` 分支存在迁移编号冲突,合并时必须处理**。`multi`(提交 `6916d67`)
  的 `0021_message_branch` 同样声明 `down_revision = "0020_agent_mvp_comments"`,
  两者从 0020 分叉,同时进 main 会产生**双 head**,`alembic upgrade head` 直接失败。
  已定**本分支先合入 main**,故改基责任在 `multi`:其 `0021→0022→0023` 三个迁移
  需重挂到 `0021_message_llm_attribution` 之下并顺延编号。本分支不携带那三个文件。

- ✅ **两个分支分库**,避免共用一个库时 `alembic_version` 只能描述一条链。
  本分支使用 `agentos_llm`(由 `CREATE DATABASE agentos_llm TEMPLATE agentos` 克隆,
  含全部既有数据),`.env` 的 `AGENTOS_DATABASE_URL` 已指向该库;
  原 `agentos` 归 `multi` 使用,已还原为 `0023_orchestration_plans`。
  `.env.example` 保持占位符不变(分支专用库属本地环境事实,不进模板)。

  > 注:`agentos_llm` 由克隆而来,物理上仍带有 `multi` 的 `session_messages.branch`
  > 列与 `orchestration_plans` 等表。本分支 ORM 未映射这些对象,SQLAlchemy 忽略
  > 多余列,不影响运行;待 `multi` 合入后它们会由其迁移正式接管。

---

## 配置管理

### 配置架构：方案 A（按 Provider 分组）

**核心思想**：
- 全局统一 `llm_provider` 开关控制使用哪家 SDK
- 每个 provider 一组配置：连接信息 + 主模型 + 场景覆盖字段
- 场景覆盖为可选，未设置时回退到 provider 主模型

### 新增配置项（`core/config.py`）

```python
class Settings(BaseSettings):
    # ============ 全局 LLM Provider 选择 ============
    # 必填，无默认值：未配置时工厂与启动自检抛中文 AgentException（见设计原则五）
    llm_provider: str | None = None  # "anthropic" | "openai"

    # ============ Anthropic 配置组 ============
    anthropic_api_key: str | None = None  # 已有
    anthropic_base_url: str | None = None  # 已有
    # 主模型不设硬编码默认值，与现有 core/config.py:26 一致；
    # 解析不出时由 _resolve_model 抛中文错，避免「跑起来了但用的不是你以为的模型」
    anthropic_model: str | None = None  # 已有
    anthropic_max_retries: int = 2
    anthropic_timeout_seconds: float | None = None
    
    # Anthropic 场景覆盖（可选，为空则用 anthropic_model）
    anthropic_agent_model: str | None = None  # 主 Agent 专用模型
    anthropic_subagent_model: str | None = None  # 子 Agent 默认档位
    anthropic_subagent_fast_model: str | None = None  # 子 Agent 快速档位
    anthropic_memory_selector_model: str | None = None  # 记忆选择器
    anthropic_memory_extractor_model: str | None = None  # 记忆提取
    anthropic_memory_dream_model: str | None = None  # 记忆梦境
    anthropic_compact_model: str | None = None  # 上下文压缩
    
    # ============ OpenAI 配置组 ============
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str | None = None  # 主模型，同样不设硬编码默认值
    openai_max_retries: int = 2
    openai_timeout_seconds: float | None = None
    
    # OpenAI 场景覆盖（可选，为空则用 openai_model）
    openai_agent_model: str | None = None
    openai_subagent_model: str | None = None
    openai_subagent_fast_model: str | None = None
    openai_memory_selector_model: str | None = None
    openai_memory_extractor_model: str | None = None
    openai_memory_dream_model: str | None = None
    openai_compact_model: str | None = None
```

> **本节是配置的单一事实源**：`prd.md` 与 `implement.md` 的配置代码块若与此处不一致，以此处为准。
> 特别注意两点：`llm_provider` 无默认值；主模型字段一律 `str | None = None`，
> 不写 `"claude-xxx"` / `"gpt-xxx"` 之类的硬编码默认值。

### 现有配置迁移

**已存在、无需改动的字段**（`core/config.py`）：
`anthropic_base_url:24`、`anthropic_api_key:25`、`anthropic_model:26`、
`openai_api_key:31`、`openai_base_url:32`——OpenAI 的两项**已经存在**，
上面的配置块中它们属于「保持原样」，不是新增。

**现有字段 → 新字段映射**（直接重命名，不保留旧字段）：

| 现有字段 | 新字段 | 说明 |
|---------|-------|------|
| `subagent_default_model:39` | `anthropic_subagent_model` | 重命名 + 移到 Anthropic 组 |
| `subagent_fast_model:40` | `anthropic_subagent_fast_model` | 重命名 + 移到 Anthropic 组 |
| `memory_selector_model:74` | `anthropic_memory_selector_model` | 重命名 + 移到 Anthropic 组 |
| `memory_extractor_model:83` | `anthropic_memory_extractor_model` | 重命名 + 移到 Anthropic 组 |
| `memory_dream_model:94` | `anthropic_memory_dream_model` | 重命名 + 移到 Anthropic 组 |

**迁移方式**：
- `core/config.py` 中直接删除旧字段，添加新字段
- `.env` 和 `.env.example` 同步更新环境变量名
- 不保留 `@property` 别名或旧字段映射

### 配置解析逻辑

**2 层回落链**：场景覆盖 → provider 主模型

实现在 `_resolve_model(provider, scenario)` 中（见 D4.2）：

```python
# 示例 1：Memory Selector 场景
# 配置：llm_provider=anthropic, anthropic_memory_selector_model=haiku
model = _resolve_model("anthropic", "memory_selector")  # → "haiku"

# 示例 2：Memory Selector 场景，无覆盖
# 配置：llm_provider=anthropic, anthropic_memory_selector_model=None
model = _resolve_model("anthropic", "memory_selector")  # → anthropic_model (回退)

# 示例 3：切换 provider
# 配置：llm_provider=openai, openai_memory_selector_model=gpt-4o-mini
model = _resolve_model("openai", "memory_selector")  # → "gpt-4o-mini"
```

### 配置示例

**典型场景 1**：全部使用 Anthropic，主 Agent 用 Opus，其他用 Sonnet
```bash
AGENTOS_LLM_PROVIDER=anthropic
AGENTOS_ANTHROPIC_MODEL=claude-sonnet-4-20250514  # 默认档位
AGENTOS_ANTHROPIC_AGENT_MODEL=claude-opus-4-20250514  # 主 Agent 专用
# 其他场景未设置，回退到 claude-sonnet-4-20250514
```

**典型场景 2**：全部使用 OpenAI，分档位配置
```bash
AGENTOS_LLM_PROVIDER=openai
AGENTOS_OPENAI_API_KEY=sk-xxx
AGENTOS_OPENAI_MODEL=gpt-4o-2024-08-06  # 默认档位
AGENTOS_OPENAI_SUBAGENT_FAST_MODEL=gpt-4o-mini  # 快速子任务
AGENTOS_OPENAI_MEMORY_SELECTOR_MODEL=gpt-4o-mini  # 高频调用
```

**典型场景 3**：最简配置（所有场景同一模型）
```bash
AGENTOS_LLM_PROVIDER=anthropic
AGENTOS_ANTHROPIC_MODEL=claude-sonnet-4-20250514
# 无场景覆盖，所有场景都用 Sonnet
```

---

## 错误处理策略

**MVP 范围**（已确定）：
- ✅ 暴露两家 SDK 的内置重试配置（`max_retries`/`timeout`）
- ✅ 保留各调用方现有的错误处理逻辑
- ❌ 不做自定义异常分类器
- ❌ 不做统一重试装饰器
- ❌ 不做错误增强包装

**实现**：
```python
class AnthropicClient(BaseLLMClient):
    def __init__(self, ...):
        self.client = AsyncAnthropic(
            api_key=...,
            base_url=...,
            max_retries=settings.anthropic_max_retries,
            timeout=settings.anthropic_timeout_seconds,
        )

class OpenAIClient(BaseLLMClient):
    def __init__(self, ...):
        self.client = AsyncOpenAI(
            api_key=...,
            base_url=...,
            max_retries=settings.openai_max_retries,
            timeout=settings.openai_timeout_seconds,
        )
```

**后续迭代**（Out of Scope）：
- 统一异常分类（transient/terminal）
- 错误增强（附加排查提示）
- 重试策略可配置化

---

## 测试策略

### 单元测试

**新增文件**：
- `tests/test_llm_base_client.py`：接口契约测试（用 Mock 验证）
- `tests/test_llm_anthropic_client.py`：Anthropic 适配逻辑（Mock SDK）
- `tests/test_llm_openai_client.py`：OpenAI 结构化输出（Mock SDK）
- `tests/test_llm_factory.py`：工厂函数、配置回落、四类配置缺失报错

**测试重点**：
- `stream()` 原样透传 provider 原始事件，不在客户端内做归一化
- 翻译器将原始事件翻译为 AgentOS 协议事件；消息经「出站 → 入站」往返后与原规范消息等价
- `OpenAIClient.complete_structured()` 正确映射参数并调用 `parse()`
- 配置回落链按优先级生效
- provider 未配置 / 取值不支持 / api_key 缺失 / 模型解析不出，四种情况均抛中文 `AgentException`
- 依赖注入可覆盖工厂创建的客户端

### 集成测试

**新增文件**：
- `tests/test_memory_openai_integration.py`：Memory Extraction 用 OpenAI 完整流程
- `tests/test_compact_openai_integration.py`：Compact 用 OpenAI 验证

**测试条件**：
- 需要真实 OpenAI API Key（通过 `AGENTOS_OPENAI_API_KEY` 环境变量）
- 可选执行（CI 跳过，本地验证）

---

## 配置迁移与采用路径

### 配置迁移指南

**步骤 1**：重命名环境变量（在 `.env` 中）

> 字段名必须严格满足 `_resolve_model()` 的拼接规则 `f"{provider}_{scenario}_model"`，
> 即 `AGENTOS_<PROVIDER>_<SCENARIO>_MODEL`。scenario 取值见 D4 场景列表。

```bash
# 旧配置 → 新配置
AGENTOS_ANTHROPIC_MODEL          → 不变（已是 provider 主模型，无需迁移）
AGENTOS_SUBAGENT_DEFAULT_MODEL   → AGENTOS_ANTHROPIC_SUBAGENT_MODEL
AGENTOS_SUBAGENT_FAST_MODEL      → AGENTOS_ANTHROPIC_SUBAGENT_FAST_MODEL
AGENTOS_MEMORY_SELECTOR_MODEL    → AGENTOS_ANTHROPIC_MEMORY_SELECTOR_MODEL
AGENTOS_MEMORY_EXTRACTOR_MODEL   → AGENTOS_ANTHROPIC_MEMORY_EXTRACTOR_MODEL
AGENTOS_MEMORY_DREAM_MODEL       → AGENTOS_ANTHROPIC_MEMORY_DREAM_MODEL
```

> ⚠️ 两处易错点：
> 1. `SUBAGENT_DEFAULT` 重命名后**去掉 `_DEFAULT`**（scenario 名是 `subagent`，
>    不是 `subagent_default`），否则 `_resolve_model("anthropic", "subagent")`
>    拼出的字段名对不上，场景覆盖会静默失效并回落到主模型。
> 2. `SUBAGENT_FAST` 必须一并迁移（Explore 用的速度优先档，
>    现为 `core/config.py:40` 的 `subagent_fast_model`），漏掉同样静默失效。
>
> 「静默失效」在此指配置层面回落到主模型，与 D9「不做静默兜底」不冲突：
> 场景覆盖本就是可选项，未配置即回落主模型是既定语义；D9 约束的是
> provider / api_key / 主模型缺失这类**必填项**，那些一律抛错。
> 但重命名写错会让人**以为配了**却没生效，故必须在迁移时逐项核对。

**步骤 2**：新增全局 provider 开关（必填，无默认值）
```bash
AGENTOS_LLM_PROVIDER=anthropic  # 必填，不配置则启动自检直接报错
```

**步骤 3**：（可选）配置 OpenAI 支持
```bash
AGENTOS_LLM_PROVIDER=openai  # 切换到 OpenAI
AGENTOS_OPENAI_API_KEY=sk-xxx
AGENTOS_OPENAI_MODEL=gpt-4o-2024-08-06
# 场景覆盖（可选）
AGENTOS_OPENAI_MEMORY_SELECTOR_MODEL=gpt-4o-mini
AGENTOS_OPENAI_SUBAGENT_FAST_MODEL=gpt-4o-mini
```

### 渐进式采用路径

**阶段 1**：重构完成（保持 Anthropic）
- 所有服务改用 `create_llm_client(scenario="xxx")`
- 所有调用方迁移到新接口（删除 `from llm import LLMClient`）
- `llm_provider` 显式配置为 `"anthropic"`
- 验证功能无回归、Prompt Caching 正常

**阶段 2**：试点全局切换 OpenAI
- 全局切换 OpenAI（评估所有场景影响）：
  ```bash
  AGENTOS_LLM_PROVIDER=openai
  ```
- 监控错误率、响应时间、成本
- 注意：当前设计不支持场景级 provider 混合使用（需等待未来支持）

**阶段 3**：全局切换 OpenAI（可选）
- 评估通过后，全量使用 OpenAI
- 按场景配置不同档位模型：
  - 主 Agent：`gpt-4o`
  - 子 Agent/Memory：`gpt-4o-mini`
- Compact 场景需评估无 Prompt Caching 的成本影响

**阶段 4**：长期优化（Out of Scope）
- 支持场景级 provider 切换（如主 Agent 用 Anthropic，Memory 用 OpenAI）
- 引入 LiteLLM 支持更多 provider（Azure、Vertex、Bedrock）

> 注：`session_messages` 的 `provider`/`model` 元数据列**已于 2026-08-15 拍板要做**，
> 不再属于 Out of Scope，详见「格式转换边界」的已拍板项。

---

## 关键文件依赖图

```
llm/
├── __init__.py          # 导出 BaseLLMClient, AnthropicClient, OpenAIClient, create_llm_client
├── base.py              # BaseLLMClient 接口定义
├── anthropic_client.py  # AnthropicClient 实现（重构现有 LLMClient）
├── openai_client.py     # OpenAIClient 实现（新增）
├── factory.py           # create_llm_client() + _resolve_model()
└── types.py             # 保持不变（LLMRawChunk, ToolUse, AnthropicStreamTranslator）

core/
└── config.py            # 新增 llm_provider, anthropic_*, openai_* 配置组

runtime/
├── agent.py             # AgentRuntime.__init__ 改用 create_llm_client(scenario="agent")
├── subagent.py          # SubAgentRunner.__init__ 改用 create_llm_client(scenario="subagent"/"subagent_fast")
└── compact.py           # CompactService.__init__ 改用 create_llm_client(scenario="compact")

memory/
├── jobs/extraction.py   # extract_with_llm 改用 create_llm_client(scenario="memory_extractor")
├── jobs/dream.py        # complete_with_llm 改用 create_llm_client(scenario="memory_dream")
└── recall/service.py    # MemoryRecallService.__init__ 改用 create_llm_client(scenario="memory_selector")
```

**依赖关系**：
```
┌─────────────────────────────────────────┐
│           core/config.py                │
│  (llm_provider, anthropic_*, openai_*)  │
└──────────────┬──────────────────────────┘
               │ 读取配置
               ▼
         llm/factory.py
    (create_llm_client + _resolve_model)
               │
      ┌────────┴────────┐
      ▼                 ▼
llm/anthropic_client  llm/openai_client
      │                 │
      └────────┬────────┘
               │ 实现接口
               ▼
        llm/base.py
        (BaseLLMClient)
               ▲
               │ 注入
      ┌────────┴────────┐
      ▼                 ▼
runtime/*          memory/*
(Agent/SubAgent)   (Extraction/Dream/Recall)
```

---

## 风险缓解

### 风险1: OpenAI 流式事件边界情况

**风险**：OpenAI 流式 delta 可能有未覆盖的边界情况（如 function_call 为空、连续 tool_calls）

**缓解**：
- 参考 ADK 的 `lite_llm.py` 翻译逻辑（已验证多 provider）
- 补充单元测试覆盖：空 delta、分块 tool arguments、finish_reason 变化
- 初期仅用于 Memory 非流式场景（`complete_structured` 不依赖流式翻译）

### 风险2: 配置复杂度增加

**风险**：新增多个配置项（每个 provider 7+ 字段），用户配置错误概率增加

**缓解**：
- 所有场景覆盖字段默认 `None`，未配置时自动回退到 provider 主模型
- `.env.example` 添加详细注释和配置示例（典型场景 1-3）
- 启动时校验：如果 `llm_provider="openai"` 但 `openai_api_key` 为空，提前报错
- 配置命名规范：`{provider}_{scenario}_model`，便于理解归属

### 风险3: 全局切换影响主 Agent

**风险**：用户修改 `llm_provider=openai` 后，主 Agent 也会切换到 OpenAI，可能丢失 Prompt Caching 和 extended thinking 能力

**缓解**：
- 文档明确说明全局切换的影响范围（所有场景统一切换）
- 提供场景模型覆盖能力：若希望主 Agent 保持 Anthropic，其他场景用 OpenAI，可设置：
  ```bash
  AGENTOS_LLM_PROVIDER=anthropic  # 全局显式指定 Anthropic
  # 主 Agent 无需覆盖，使用 anthropic_model
  ```
  或反向配置（不推荐，因 Compact 依赖 Caching）：
  ```bash
  AGENTOS_LLM_PROVIDER=openai  # 全局 OpenAI
  AGENTOS_OPENAI_AGENT_MODEL=...  # 主 Agent 用 OpenAI
  AGENTOS_OPENAI_COMPACT_MODEL=...  # Compact 用 OpenAI（失去 Caching 优势）
  ```
- 集成测试验证主 Agent 在 `llm_provider=anthropic` 时的 Prompt Caching 日志正常

### 风险4: 消息格式转换错误

**风险**：AgentOS 协议 → provider wire 格式转换时，复杂 content 结构（多模态、tool 调用与结果）可能转换错误

**缓解**：
- 翻译逻辑集中在翻译器层，每 provider 一个实现，便于逐个覆盖测试（见 D3、「格式转换边界」）
- 单元测试覆盖：纯文本、tool 调用、tool 结果、多轮工具循环、多模态 content
- 优先保证「AgentOS 协议 → Anthropic」这条既有链路等价于现状（`_messages_to_context()` 行为不回归）
- OpenAI 链路初期限定 Memory 等纯文本场景，主 Agent 接入前补齐完整转换测试
- 定一条硬约束：任意消息经「出站翻译 → 入站翻译」往返后应与原规范消息等价，
  以此写往返测试，避免翻译静默丢字段

---

## 性能考量

**客户端实例化成本**：
- 当前：每个 Service 实例创建一个 `LLMClient`（每次 `new AsyncAnthropic()`）
- 重构后：不变（`AnthropicClient` / `OpenAIClient` 构造器同样创建 SDK 客户端）
- 未来优化：可考虑全局单例或连接池（Out of Scope）

**流式翻译开销**：
- Anthropic：无额外开销（直接透传 SDK 事件）
- OpenAI：每个 delta 需检查 `tool_calls` 数组并累积 JSON（开销 <1ms/event）

**结构化输出**：
- Anthropic：tool use 强制 + 客户端 Pydantic 校验
- OpenAI：服务端约束解码 + SDK 自动校验（理论上更快，但差异不明显）

---

## 未来扩展点

**已预留的扩展能力**：
1. ✅ `BaseLLMClient` 接口可添加新方法（如 `count_tokens()`、`embed()`）
2. ✅ 工厂函数可扩展支持更多 provider（如 `"azure"`、`"vertex"`）
3. ✅ 依赖注入允许自定义客户端实现（用于测试或企业定制）
4. ✅ 场景枚举可扩展（添加新场景只需在 Settings 中新增 `{provider}_{scenario}_model` 字段）

**明确不做的**（需要架构升级）：
- ❌ 场景级 provider 切换（同一实例中，主 Agent 用 Anthropic，Memory 用 OpenAI）
  - 原因：当前设计是全局统一切换，场景级切换需要配置架构重构
  - 绕过方案：通过场景模型覆盖在同一 provider 下分档位
- ❌ 运行时动态切换 provider（同一会话内切换 Anthropic/OpenAI）
  - 原因：MVP 简化，provider 配置固定、重启生效。
    注意**不再是格式原因**——库里存 AgentOS 协议，格式上已无障碍（见「消息格式边界」）
- ❌ provider 间格式直转（Anthropic ↔ OpenAI 互转）
  - 原因：架构上就不该存在。一切经 AgentOS 协议中转，
    直转等于「AgentOS 协议 → A → B」的双跳，中间一跳纯属信息损耗
- ❌ 多 provider 能力协商（如检测模型是否支持 vision/tools）
  - 原因：需要维护能力矩阵，增加复杂度

**后续迭代可能方向**：
1. **场景级 provider 选择**（如需混合使用）：
   ```python
   # 配置示例
   AGENTOS_AGENT_PROVIDER=anthropic
   AGENTOS_MEMORY_PROVIDER=openai
   # 工厂函数改为 create_llm_client(scenario, provider_override)
   ```
2. **LiteLLM 集成**（如需接入更多 provider）：
   - 统一接口适配 20+ provider
   - 自动格式转换和能力降级
   - 成本：引入额外依赖，流式事件翻译可能有坑
3. **消息元数据增强**（不改变存储格式）：
   - `session_messages` 新增 `provider`/`model` 列，用于成本归因与问题复现
   - 存储格式仍只有 AgentOS 协议一种，不按 provider 分格式存
4. **Token 计数统一接口**：
   - `BaseLLMClient.count_tokens()` 抽象方法
   - Anthropic 用 `anthropic.count_tokens()`，OpenAI 用 `tiktoken`

---

## 总结

### 核心设计思想

**从"按场景切换 provider"改为"全局统一 provider + 场景模型档位"**：
- **全局切换**：一个 `llm_provider` 开关控制整个 AgentOS 使用哪家 SDK
- **场景分档**：同一 provider 下，不同场景可用不同档位模型（主 Agent 用 Opus，Memory 用 Haiku）
- **配置分组**：按 provider 组织配置（Anthropic 组、OpenAI 组），每组包含连接信息、主模型、场景覆盖
- **接口抽象**：`BaseLLMClient` 统一接口，双实现（AnthropicClient、OpenAIClient）
- **依赖注入**：所有 Service 通过 `create_llm_client(scenario)` 获取客户端，测试时可注入 Mock
- **保留原生能力**：Anthropic 保留 Prompt Caching/thinking，OpenAI 保留 Structured Outputs

### 关键优势

1. **配置简单**：一个开关控制全局，无需逐场景配置 provider
2. **成本优化**：同一 provider 下按场景分档（主任务用大模型，辅助任务用小模型）
3. **无静默兜底**：`llm_provider` 不设默认值，未配置或配置有误时抛具体中文错误，不回落到 Anthropic
4. **易于测试**：依赖注入 + 接口抽象，方便 Mock 和单元测试
5. **渐进迁移**：先重构（保持 Anthropic），验证通过后再按需切换 OpenAI

### 工程量估算

- **新增代码**：~800 行
  - `llm/base.py`：~50 行
  - `llm/openai_client.py`：~150 行（SDK 调用 + 结构化输出，不含翻译）
  - `llm/translator.py`（或按 provider 分文件）：~250 行
    （出站 messages/system/tools + 入站事件；Anthropic 侧复用现有 `AnthropicStreamTranslator`）
  - `llm/factory.py`：~80 行
  - `core/config.py`：~30 行（配置字段）
  - `main.py`：~5 行（lifespan 启动自检）
  - Service 层改动：~200 行（7 处注入点）
  - 测试代码：~90 行
- **重构代码**：~100 行
  - `llm/client.py` → `llm/anthropic_client.py` 重命名 + 继承接口
  - `llm/__init__.py` 导出更新
- **总计**：~900 行，预计 3-4 天

### 验收标准

见 `prd.md` AC1-AC5（以 prd.md 为准，本文不另列清单）。
