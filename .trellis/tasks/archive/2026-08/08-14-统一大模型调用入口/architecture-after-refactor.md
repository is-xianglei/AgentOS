# 重构后架构图：统一 LLM 调用入口

> ## ⚠️ 已废弃（早期草稿，勿据此实现）
>
> 本文是方案早期草稿，多处结论已被 `prd.md` 和 `design.md` 推翻。**以 `prd.md` 的决策记录
> 和 `design.md` 为准**，本文仅作演进过程留档。已知失效内容：
>
> | 本文写法 | 现行结论 | 依据 |
> |---|---|---|
> | `self.llm = llm or AnthropicClient()  # 可注入，默认 Anthropic`（:183） | 无静默兜底，回落只能是 `create_llm_client(scenario)`，配置缺失抛中文错误 | prd D2 / D9 |
> | 场景级 provider：`llm_memory_provider`、`llm_compact_provider`（:109-110、:218、:250、:485-486） | 只有一个全局 `llm_provider` 开关，场景只覆盖模型不覆盖 provider | prd D1 / D4 |
> | 「数据库只存 Anthropic 格式」（:326、:344、:361、:366、:478） | 库里存 AgentOS 自有协议，各 provider 经翻译器双向转换 | prd D8 / design「消息格式边界」 |
> | 「显式默认 / 默认仍是 anthropic」（:258、:380、:427-428、:469） | `llm_provider` 无默认值 | prd D9 |
>
> 注：文中标注 **重构前** 的代码块（如 3.1/3.2/3.3 各节）是对改动前现状的描述，
> 不是失效结论，不在上表之列。

## 一、总体架构（分层视图）

```
┌────────────────────────────────────────────────────────────────────────┐
│                         API 层（FastAPI 路由）                          │
│   /agent/interact  │  /agent/run  │  /memory/extract  │  /compact     │
└──────────┬────────────────┬──────────────┬──────────────┬──────────────┘
           │                │              │              │
           ▼                ▼              ▼              ▼
┌────────────────────────────────────────────────────────────────────────┐
│                            Service 层                                   │
├──────────────────┬──────────────────┬──────────────────┬───────────────┤
│  AgentRuntime    │  SubAgentRunner  │  MemoryService   │ CompactService│
│  (主 Agent)      │  (子 Agent)      │  (提取/召回)     │ (上下文压缩)  │
├──────────────────┼──────────────────┼──────────────────┼───────────────┤
│ llm: BaseLLM     │ llm: BaseLLM     │ llm: BaseLLM     │ llm: BaseLLM  │
│   = Anthropic    │   = Anthropic    │   = factory()    │   = factory() │
│   (显式硬编码)   │   (显式硬编码)   │   (可配置)       │   (可配置)    │
└──────────┬───────┴──────────┬───────┴──────────┬───────┴───────┬───────┘
           │                  │                  │               │
           │ 依赖注入         │ 依赖注入         │ 依赖注入      │ 依赖注入
           │ (可测试)         │ (可测试)         │ (场景切换)    │ (场景切换)
           │                  │                  │               │
           ▼                  ▼                  ▼               ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      BaseLLMClient（抽象接口）                         │
│  • stream(messages, system_prompt, tools, model) → AsyncIterator[dict]│
│  • complete_structured(messages, output_format, ...) → T: BaseModel   │
└────────────────────────┬───────────────────────────┬───────────────────┘
                         │                           │
          ┌──────────────┴─────────┐    ┌───────────┴────────────┐
          ▼                        ▼    ▼                        ▼
┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│  AnthropicClient     │  │   OpenAIClient       │  │  create_llm_client() │
│  (继承 BaseLLMClient)│  │  (继承 BaseLLMClient)│  │      (工厂函数)      │
├──────────────────────┤  ├──────────────────────┤  ├──────────────────────┤
│ • AsyncAnthropic     │  │ • AsyncOpenAI        │  │ provider: str        │
│ • stream() 直透      │  │ • stream() 事件翻译  │  │   → BaseLLMClient    │
│ • tool use 强制结构化│  │ • parse() 结构化     │  │                      │
│ • Prompt Caching     │  │ • 参数映射 (system,  │  │ 读取 settings:       │
│ • thinking blocks    │  │   max_completion_)   │  │ • llm_*_provider     │
└──────────┬───────────┘  └──────────┬───────────┘  │ • *_api_key          │
           │                         │              │ • *_model            │
           │                         │              └──────────────────────┘
           ▼                         ▼
┌──────────────────────┐  ┌──────────────────────┐
│   Anthropic SDK      │  │     OpenAI SDK       │
│  (AsyncAnthropic)    │  │   (AsyncOpenAI)      │
└──────────────────────┘  └──────────────────────┘
```

---

## 二、配置流（从环境变量到 SDK）

```
┌───────────────────────────────────────────────────────────────────────┐
│                    环境变量（.env）                                    │
├───────────────────────────────────────────────────────────────────────┤
│ AGENTOS_ANTHROPIC_API_KEY=sk-ant-...                                 │
│ AGENTOS_ANTHROPIC_MODEL=claude-3-5-sonnet-20241022                   │
│ AGENTOS_ANTHROPIC_MAX_RETRIES=2                                      │
│ AGENTOS_ANTHROPIC_TIMEOUT_SECONDS=600                                │
│                                                                       │
│ AGENTOS_OPENAI_API_KEY=sk-...                                        │
│ AGENTOS_OPENAI_MODEL=gpt-4o-2024-08-06                               │
│ AGENTOS_OPENAI_MAX_RETRIES=2                                         │
│ AGENTOS_OPENAI_TIMEOUT_SECONDS=300                                   │
│                                                                       │
│ AGENTOS_LLM_MEMORY_PROVIDER=openai     # Memory 用 OpenAI            │
│ AGENTOS_LLM_COMPACT_PROVIDER=anthropic # Compact 用 Anthropic        │
│                                                                       │
│ AGENTOS_LLM_DEFAULT_MAX_TOKENS=131072                                │
│ AGENTOS_LLM_MEMORY_EXTRACTOR_MAX_TOKENS=4096                         │
│ AGENTOS_MEMORY_EXTRACTOR_MODEL=claude-3-haiku-20240307  # 场景专用  │
└───────────┬───────────────────────────────────────────────────────────┘
            │ Pydantic Settings 加载
            ▼
┌───────────────────────────────────────────────────────────────────────┐
│                  core/config.py (Settings)                            │
├───────────────────────────────────────────────────────────────────────┤
│ anthropic_api_key: str                                                │
│ anthropic_model: str                                                  │
│ anthropic_max_retries: int = 2                                        │
│ anthropic_timeout_seconds: float | None = None                        │
│                                                                       │
│ openai_api_key: str | None = None                                    │
│ openai_model: str = "gpt-4o-2024-08-06"                              │
│ openai_max_retries: int = 2                                           │
│ openai_timeout_seconds: float | None = None                           │
│                                                                       │
│ llm_memory_provider: str = "anthropic"   # 场景级 provider 选择      │
│ llm_compact_provider: str = "anthropic"                               │
│                                                                       │
│ memory_extractor_model: str | None = None  # 场景专用 model           │
│ llm_memory_extractor_max_tokens: int = 4096                           │
└───────────┬───────────────────────────────────────────────────────────┘
            │
            │ create_llm_client(provider) 读取配置
            ▼
┌───────────────────────────────────────────────────────────────────────┐
│              llm/factory.py (create_llm_client)                       │
├───────────────────────────────────────────────────────────────────────┤
│ if provider == "anthropic":                                           │
│     return AnthropicClient(                                           │
│         api_key=settings.anthropic_api_key,                           │
│         model=settings.anthropic_model,                               │
│         max_retries=settings.anthropic_max_retries,                   │
│         timeout=settings.anthropic_timeout_seconds,                   │
│     )                                                                 │
│ elif provider == "openai":                                            │
│     return OpenAIClient(                                              │
│         api_key=settings.openai_api_key,                              │
│         model=settings.openai_model,                                  │
│         max_retries=settings.openai_max_retries,                      │
│         timeout=settings.openai_timeout_seconds,                      │
│     )                                                                 │
└───────────┬───────────────────────────────────────────────────────────┘
            │ 实例化具体 Client
            ▼
┌───────────────────────────────────────────────────────────────────────┐
│           AnthropicClient / OpenAIClient 构造器                       │
├───────────────────────────────────────────────────────────────────────┤
│ self.client = AsyncAnthropic(                                         │
│     api_key=api_key,                                                  │
│     base_url=base_url,                                                │
│     max_retries=max_retries,      # SDK 内置重试机制                  │
│     timeout=timeout,               # SDK 自动超时计算                 │
│ )                                                                     │
└───────────────────────────────────────────────────────────────────────┘
```

**配置回落链（以 Memory Extraction 为例）**：

```
Memory Extraction 需要确定 model
    ↓
1. 场景专用配置：settings.memory_extractor_model
    ↓ (如果为 None)
2. provider 默认：settings.anthropic_model 或 settings.openai_model
    ↓ (取决于 settings.llm_memory_provider)
3. 硬编码默认：AnthropicClient.model 或 OpenAIClient.model

总计 2 层回落（场景 → 默认）
```

---

## 三、调用路径对比（重构前后）

### 3.1 主 Agent 流式对话（AgentRuntime）

**重构前**：
```
AgentRuntime.__init__()
    self.llm = LLMClient()  # 硬编码，无法注入
    
AgentRuntime._run_llm_loop()
    async for chunk in self.llm.stream(...):
        # 翻译 Anthropic 事件
```

**重构后**：
```
AgentRuntime.__init__(llm: BaseLLMClient | None = None)
    self.llm = llm or AnthropicClient()  # 可注入，默认 Anthropic
    
AgentRuntime._run_llm_loop()
    async for chunk in self.llm.stream(...):
        # 翻译 Anthropic 事件（行为完全不变）
        
# 测试时注入 Mock
runtime = AgentRuntime(session_id=1, llm=MockLLMClient())
```

**变化**：
- ✅ 增加依赖注入能力（可测试）
- ✅ 默认行为完全不变（仍是 AnthropicClient）
- ✅ 不走工厂函数（避免配置误触发 OpenAI）

---

### 3.2 Memory Extraction（按场景切换）

**重构前**：
```
MemoryExtractionService.extract_with_llm(
    llm: LLMClient,  # 外部传入，调用方自己 new
)
    result = await llm.complete_structured(...)
    
# 调用方
runner = MemoryJobRunner(llm=LLMClient())  # 每次 new，无配置选择
```

**重构后**：
```
MemoryExtractionService.extract_with_llm(
    llm: BaseLLMClient | None = None,  # 改为可选
)
    client = llm or create_llm_client(settings.llm_memory_provider)
    result = await client.complete_structured(...)
    
# 调用方
runner = MemoryJobRunner(llm=None)  # 默认按配置创建（可选 OpenAI）

# 或显式注入
runner = MemoryJobRunner(llm=OpenAIClient())
```

**变化**：
- ✅ 支持通过 `llm_memory_provider` 配置切换 Anthropic/OpenAI
- ✅ 外部仍可注入自定义客户端（向后兼容）
- ✅ 默认值从"外部传入"改为"内部按配置创建"

---

### 3.3 Compact 上下文压缩

**重构前**：
```
CompactService.__init__(llm: LLMClient | None = None)
    self.llm = llm or LLMClient()  # 默认 Anthropic
    
CompactService._auto_compact()
    async for chunk in self.llm.stream(...):
        # 压缩逻辑
```

**重构后**：
```
CompactService.__init__(llm: BaseLLMClient | None = None)
    self.llm = llm or create_llm_client(settings.llm_compact_provider)
    
CompactService._auto_compact()
    async for chunk in self.llm.stream(...):
        # 压缩逻辑（完全不变）
```

**变化**：
- ✅ 可通过 `llm_compact_provider` 配置切换（默认仍是 anthropic）
- ✅ 压缩逻辑完全不动

---

## 四、双 Provider 的适配差异

```
┌─────────────────────────────────────────────────────────────────────┐
│                   统一接口（BaseLLMClient）                          │
│  stream(messages, system_prompt, tools, model) → AsyncIterator[dict]│
│  complete_structured(messages, output_format, ...) → T: BaseModel   │
└──────────────┬──────────────────────────────────┬───────────────────┘
               │                                  │
               ▼                                  ▼
    ┌──────────────────────┐          ┌──────────────────────┐
    │  AnthropicClient     │          │   OpenAIClient       │
    ├──────────────────────┤          ├──────────────────────┤
    │ 流式事件             │          │ 流式事件             │
    ├──────────────────────┤          ├──────────────────────┤
    │ content_block_start  │   →→→   │ 检测首个非空 delta   │
    │ content_block_delta  │   →→→   │ choices[].delta.     │
    │   (text/thinking)    │          │   content            │
    │ content_block_delta  │   →→→   │ choices[].delta.     │
    │   (tool input_json)  │          │   tool_calls[].      │
    │                      │          │   function.arguments │
    │ content_block_stop   │   →→→   │ finish_reason=       │
    │                      │          │   "tool_calls"       │
    │ message_final        │   →→→   │ finish_reason="stop" │
    │   (usage)            │          │   + usage            │
    └──────────────────────┘          └──────────────────────┘
    
    ┌──────────────────────┐          ┌──────────────────────┐
    │ 结构化输出           │          │ 结构化输出           │
    ├──────────────────────┤          ├──────────────────────┤
    │ tool use 强制：      │          │ Structured Outputs： │
    │ • 包装成 tool        │          │ • response_format    │
    │ • tool_choice 指定   │          │ • parse() 方法       │
    │ • 校验失败回灌重试   │          │ • SDK 自动校验       │
    └──────────────────────┘          └──────────────────────┘
    
    ┌──────────────────────┐          ┌──────────────────────┐
    │ 参数映射             │          │ 参数映射             │
    ├──────────────────────┤          ├──────────────────────┤
    │ system_prompt        │   →→→   │ messages[0]          │
    │   (独立参数)         │          │   role='system'      │
    │ max_tokens           │   →→→   │ max_completion_tokens│
    │ tools (ToolParam)    │   →→→   │ tools (function fmt) │
    └──────────────────────┘          └──────────────────────┘
    
    ┌──────────────────────┐          ┌──────────────────────┐
    │ 原生能力保留         │          │ 原生能力保留         │
    ├──────────────────────┤          ├──────────────────────┤
    │ ✅ Prompt Caching    │          │ ✅ Structured Outputs│
    │   (显式断点)         │          │   (服务端约束解码)   │
    │ ✅ thinking blocks   │          │ ❌ thinking (无)     │
    │   (extended thinking)│          │ ✅ reasoning_content │
    │                      │          │   (o1/o3 推理模型)   │
    └──────────────────────┘          └──────────────────────┘
```

---

## 五、消息格式边界

```
┌─────────────────────────────────────────────────────────────────────┐
│                PostgreSQL (session_messages 表)                      │
│                content JSONB (Anthropic 格式)                        │
├─────────────────────────────────────────────────────────────────────┤
│ {"role": "user", "content": "..."}                                  │
│ {"role": "assistant", "content": [{"type": "text", "text": "..."}]}│
│ {"role": "user", "content": [{"type": "tool_result", ...}]}        │
└──────────────┬──────────────────────────────────────────────────────┘
               │ SessionService 读取（不转换）
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Service 层                                     │
│  AgentRuntime / SubAgentRunner / MemoryService / CompactService     │
└───────┬──────────────────────────────────────┬──────────────────────┘
        │                                      │
        │ Anthropic 场景                       │ OpenAI 场景
        │ (原样传递)                           │ (内部临时转换)
        ▼                                      ▼
┌──────────────────────┐          ┌──────────────────────────┐
│  AnthropicClient     │          │   OpenAIClient           │
│  接收 Anthropic 格式 │          │  转为 OpenAI 格式：      │
│  直接传给 SDK        │          │  • system → messages[0]  │
│                      │          │  • tool_result → content │
│                      │          │  (仅在调用时转换，不落库) │
└──────────────────────┘          └──────────────────────────┘
        │                                      │
        │                                      │
        ▼                                      ▼
   Anthropic SDK                          OpenAI SDK
   (原生格式)                             (转换后格式)
   
        │                                      │
        │ 响应                                 │ 响应
        ▼                                      ▼
┌──────────────────────┐          ┌──────────────────────────┐
│  原样返回             │          │  结果用完即弃            │
│  AgentRuntime 落库   │          │  MemoryService 不落库    │
│  (Anthropic 格式)    │          │  (仅用于 Memory 提取)    │
└──────────────────────┘          └──────────────────────────┘
```

**关键点**：
- 数据库只存 Anthropic 格式（主 Agent 历史消息）
- OpenAI 仅用于 Memory/Compact 等非历史场景
- OpenAI 的消息格式转换在 `OpenAIClient` 内部，对上层透明

---

## 六、依赖注入与测试

```
┌─────────────────────────────────────────────────────────────────────┐
│                       生产环境（默认行为）                           │
├─────────────────────────────────────────────────────────────────────┤
│ AgentRuntime(session_id=1)  # llm 参数省略                          │
│     ↓                                                               │
│ self.llm = AnthropicClient()  # 显式默认                            │
│                                                                     │
│ MemoryRecallService()  # llm 参数省略                               │
│     ↓                                                               │
│ self.llm = create_llm_client(settings.llm_memory_provider)         │
│     ↓                                                               │
│ AnthropicClient() 或 OpenAIClient()  # 按配置决定                   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                         测试环境（注入 Mock）                        │
├─────────────────────────────────────────────────────────────────────┤
│ # 单元测试                                                          │
│ mock_llm = Mock(spec=BaseLLMClient)                                 │
│ mock_llm.stream = AsyncMock(return_value=...)                       │
│ mock_llm.complete_structured = AsyncMock(return_value=...)          │
│                                                                     │
│ runtime = AgentRuntime(session_id=1, llm=mock_llm)                 │
│ # 验证调用参数、次数、返回值                                        │
│ assert mock_llm.stream.call_count == 1                              │
│ assert mock_llm.stream.call_args[1]["model"] == "claude-3-5-sonnet"│
│                                                                     │
│ # 集成测试（可选，需真实 API Key）                                  │
│ real_client = OpenAIClient(api_key="sk-test-...")                   │
│ service = MemoryRecallService(llm=real_client)                      │
│ result = await service.recall(...)  # 真实调用 OpenAI              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 七、文件结构（重构后）

```
AgentOS/
├── llm/
│   ├── __init__.py              # 导出 BaseLLMClient, create_llm_client, LLMClient(别名)
│   ├── base_client.py           # BaseLLMClient 抽象接口（新增）
│   ├── anthropic_client.py      # AnthropicClient 实现（重构自 client.py）
│   ├── openai_client.py         # OpenAIClient 实现（新增）
│   ├── factory.py               # create_llm_client() 工厂函数（新增）
│   └── types.py                 # LLMRawChunk, ToolUse, AnthropicStreamTranslator（保持）
│
├── core/
│   └── config.py                # Settings 新增 openai_*/llm_*_provider 配置
│
├── runtime/
│   ├── agent.py                 # AgentRuntime 加 llm 参数（默认 AnthropicClient）
│   ├── subagent.py              # SubAgentRunner 加 llm 参数（默认 AnthropicClient）
│   └── compact.py               # CompactService 改用 create_llm_client()
│
├── memory/
│   ├── jobs/
│   │   ├── extraction.py        # MemoryExtractionService 改用 create_llm_client()
│   │   └── dream.py             # MemoryDreamService 改用 create_llm_client()
│   └── recall/
│       └── service.py           # MemoryRecallService 改用 create_llm_client()
│
├── tests/
│   ├── test_llm_base_client.py      # BaseLLMClient 接口契约测试（新增）
│   ├── test_llm_anthropic_client.py # AnthropicClient Mock 测试（新增）
│   ├── test_llm_openai_client.py    # OpenAIClient Mock 测试（新增）
│   ├── test_llm_factory.py          # 工厂函数测试（新增）
│   ├── test_memory_openai_integration.py  # OpenAI 集成测试（新增，可选）
│   └── test_compact_openai_integration.py # Compact OpenAI 集成测试（新增，可选）
│
├── .env                         # 新增 AGENTOS_OPENAI_* 和 AGENTOS_LLM_*_PROVIDER
└── .env.example                 # 同步更新配置示例
```

**变更统计**：
- 新增文件：6 个（3 个 llm/ + 3 个测试骨架，可选测试按需）
- 修改文件：9 个（config + 6 个 Service + 2 个 env）
- 删除文件：1 个（llm/client.py 重命名为 anthropic_client.py）

---

## 八、关键设计约束可视化

```
┌─────────────────────────────────────────────────────────────────────┐
│                         设计约束总览                                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  约束 1: 主 Agent 完全不改默认行为                                  │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ runtime/agent.py                                            │   │
│  │ runtime/subagent.py                                         │   │
│  │   ↓                                                         │   │
│  │ 只加 llm 参数，显式默认 AnthropicClient()                   │   │
│  │ 不走工厂函数，不受 llm_*_provider 配置影响                  │   │
│  │ Prompt Caching + thinking blocks 保留                       │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  约束 2: 消息历史格式不变                                           │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ PostgreSQL session_messages.content                         │   │
│  │   ↓                                                         │   │
│  │ 始终是 Anthropic 格式（tool_use/tool_result）               │   │
│  │ OpenAI 场景的消息格式转换在 OpenAIClient 内部，不落库       │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  约束 3: 不做运行时切换                                             │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ 同一场景的 provider 在配置中固定：                          │   │
│  │ • Memory 系列：llm_memory_provider (anthropic/openai)       │   │
│  │ • Compact：llm_compact_provider (anthropic/openai)          │   │
│  │   ↓                                                         │   │
│  │ 不支持同一 Agent 在运行时动态切换 provider                  │   │
│  │ 不做通用消息格式转换层（Anthropic ↔ OpenAI 双向）          │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  约束 4: 向后兼容                                                   │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ from llm import LLMClient  # 仍可用（别名指向 Anthropic）  │   │
│  │ 所有现有调用方无需改代码                                    │   │
│  │ 接口参数名沿用（system_prompt/output_format）               │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 九、总结：重构后的核心改进

| 维度 | 重构前 | 重构后 |
|-----|--------|--------|
| **Provider 支持** | 仅 Anthropic | Anthropic + OpenAI（按场景固定） |
| **配置管理** | 硬编码 max_tokens/timeout | 统一到 settings，支持场景覆盖 |
| **依赖注入** | 部分支持（Memory 系列），主 Agent 不支持 | 全部支持（可测试性提升） |
| **可扩展性** | 新增 provider 需改多处 if-else | 实现 BaseLLMClient + 注册到工厂 |
| **错误处理** | 策略割裂（Memory 有重试，主 Agent 无） | 统一暴露 SDK 内置重试配置 |
| **向后兼容** | N/A | 100%（接口不变，默认行为不变） |
| **工程量** | N/A | +730 行（3-4 天） |

**架构升级的本质**：
- 从"单一 LLMClient 类"升级到"接口 + 双实现 + 工厂"
- 从"硬编码配置"升级到"分层配置 + 场景覆盖"
- 从"部分可测试"升级到"全部可测试"
- 保持主 Agent 核心流程完全不动（最小风险）
