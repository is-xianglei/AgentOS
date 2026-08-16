# Implementation Plan: 统一大模型调用入口

## 实施顺序

按依赖关系和风险程度排序，从基础到上层、从低风险到高风险。

---

## Phase 1: 基础设施层（0.5天）

### 1.1 定义 BaseLLMClient 接口

**文件**: `llm/base.py` (新建)

**内容**:
```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel


class BaseLLMClient(ABC):
    """LLM 客户端统一接口。"""
    
    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        system_prompt: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用，返回该 provider 的原始事件流，不在此处做归一化。

        注意用 def 而非 async def：实现类是异步生成器，
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
        """结构化输出，返回 Pydantic 模型实例。
        
        校验失败时抛出 ValueError 或 ValidationError。
        """
```

**验证**: `mypy llm/base.py` 通过

---

### 1.2 重构 AnthropicClient

**操作**:
1. 重命名文件: `llm/client.py` → `llm/anthropic_client.py`
2. 重命名类: `LLMClient` → `AnthropicClient`
3. 继承 `BaseLLMClient`
4. 构造器**显式接收 `model` 参数**（关键变更）
5. 构造器增加 `max_retries` 和 `timeout` 参数

**变更**:
```python
# llm/anthropic_client.py
from llm.base import BaseLLMClient

class AnthropicClient(BaseLLMClient):
    def __init__(
        self,
        model: str,  # 必填参数，由工厂或调用方提供
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float | None = None,
    ):
        self.model = model  # 构造时确定，不再回落 settings
        self.api_key = api_key or settings.anthropic_api_key
        self.base_url = base_url or settings.anthropic_base_url
        self.max_retries = max_retries
        self.timeout = timeout
    
    async def stream(self, ...) -> AsyncIterator[LLMRawChunk]:
        client = AsyncAnthropic(
            base_url=self.base_url,
            api_key=self.api_key,
            max_retries=self.max_retries,
            timeout=self.timeout,
        )
        # 其余逻辑不变
```

**验证**: 
- `pytest tests/test_llm_client.py` 通过（如果存在）
- 类型检查通过：`mypy llm/anthropic_client.py`

---

### 1.3 更新 llm/__init__.py

**内容**:
```python
from llm.base import BaseLLMClient
from llm.anthropic_client import AnthropicClient

__all__ = ["BaseLLMClient", "AnthropicClient"]
```

**验证**: `python -c "from llm import AnthropicClient; print(AnthropicClient)"`

---

## Phase 2: OpenAI 实现（1.5天）

### 2.1 实现 OpenAIClient.complete_structured

**文件**: `llm/openai_client.py` (新建)

**优先实现非流式**（Memory 场景只需这个）:
```python
from openai import AsyncOpenAI
from pydantic import BaseModel

from core.config import settings
from llm.base import BaseLLMClient


class OpenAIClient(BaseLLMClient):
    def __init__(
        self,
        model: str,  # 必填参数，由工厂提供
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float | None = None,
    ):
        self.model = model
        self.api_key = api_key or settings.openai_api_key
        self.base_url = base_url or settings.openai_base_url
        self.max_retries = max_retries
        self.timeout = timeout
    
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
        """使用 OpenAI Structured Outputs API。"""
        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.max_retries,
            timeout=timeout or self.timeout,
        )
        
        # 构造消息（OpenAI 要求 system 是首条消息）
        openai_messages = []
        if system_prompt:
            openai_messages.append({"role": "system", "content": system_prompt})
        openai_messages.extend(messages)
        
        # 调用 parse() 方法（Structured Outputs，新版 SDK 已从 beta 提升）
        response = await client.chat.completions.parse(
            model=model or self.model,
            messages=openai_messages,
            response_format=output_format,
            max_completion_tokens=max_tokens,  # 注意参数名差异
            temperature=0,
        )
        
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError(f"结构化输出缺失，finish_reason={response.choices[0].finish_reason}")
        return parsed
    
    async def stream(self, ...) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError("OpenAI 流式翻译在 Phase 2.2 实现")
```

**验证**:
```bash
pytest tests/test_llm_openai_client.py::test_complete_structured -v
```

---

### 2.2 实现 OpenAIClient.stream（**本任务不做**，留待后续任务）

按 prd.md Out of Scope 第 7 条，OpenAI 流式与 OpenAI 侧翻译器不在本任务范围内，
本步骤仅记录实现要点备查。`OpenAIClient.stream()` 在本任务结束时保持
`NotImplementedError` 桩即可（`llm/openai_client.py:73`）。

**职责边界**：`OpenAIClient.stream()` 只吐 OpenAI 原生事件，**不翻译成 AgentOS 协议**。
归一化由 OpenAI 侧翻译器完成（对标 `AnthropicStreamTranslator`），见 prd D8 与 design「格式转换边界」。

**推荐用 SDK 的 .stream() helper**（已用 Context7 核实 openai-python）:
```python
async with client.chat.completions.stream(
    model=model or self.model,
    messages=openai_messages,
) as stream:
    async for event in stream:
        # event.type: "content.delta" / "tool_calls.function.arguments.delta" / ...
        yield event  # 原样吐出，翻译交给 OpenAI 翻译器
```
- SDK 自动聚合，提供类型化 event，避免手工解析 raw chunk 的边界坑
- 翻译器侧负责：`content.delta` → 文本增量事件；
  `tool_calls.function.arguments.delta/done` → 累积后产出 tool_use；
  最终 snapshot 的 `usage` 聚合到 `message_final`（注意 OpenAI 是
  `prompt_tokens`/`completion_tokens`，需换算成协议统一字段名）

---

### 2.3 删除废弃方法 _complete_structured

**文件**: `llm/anthropic_client.py`（或其他 client）

**原因**: 
- `_complete_structured()` 是 server-side output_config 的实现
- 第三方中转会静默丢弃该参数，不合理
- 已被 tool-use 的 `complete_structured()` 替代

**操作**:
1. 搜索 `_complete_structured` 方法定义并删除
2. 搜索所有调用方并迁移到 `complete_structured()`
3. 删除相关测试用例（注意保留 tool-use 版本的 `test_complete_structured`）

**验证**:
```bash
# 确认无残留引用
rg "_complete_structured" --type py
```

---

## Phase 3: 配置与工厂（1.0天）

### 3.1 更新 core/config.py

**核心变更**：全局 provider 开关 + 按 provider 分组的模型配置

> 配置的单一事实源是 `design.md`「新增配置项」节，本节只是抄录，冲突时以 design.md 为准。

**新增字段**:
```python
class Settings(BaseSettings):
    # ... 现有字段 ...

    # ========== Provider 全局选择 ==========
    # 必填，无默认值：未配置时工厂与启动自检抛中文 AgentException
    llm_provider: str | None = None  # "anthropic" | "openai"

    # ========== Anthropic 配置组 ==========
    # 现有字段保持：anthropic_api_key, anthropic_base_url
    anthropic_model: str | None = None  # 主模型，不设硬编码默认值
    # 场景覆盖（可选）
    anthropic_agent_model: str | None = None
    anthropic_subagent_model: str | None = None
    anthropic_subagent_fast_model: str | None = None
    anthropic_compact_model: str | None = None
    anthropic_memory_extractor_model: str | None = None
    anthropic_memory_dream_model: str | None = None
    anthropic_memory_selector_model: str | None = None
    # SDK 重试配置
    anthropic_max_retries: int = 2
    anthropic_timeout_seconds: float | None = None
    
    # ========== OpenAI 配置组 ==========
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str | None = None  # 主模型，不设硬编码默认值
    # 场景覆盖（可选）
    openai_agent_model: str | None = None
    openai_subagent_model: str | None = None
    openai_subagent_fast_model: str | None = None
    openai_compact_model: str | None = None
    openai_memory_extractor_model: str | None = None
    openai_memory_dream_model: str | None = None
    openai_memory_selector_model: str | None = None
    # SDK 重试配置
    openai_max_retries: int = 2
    openai_timeout_seconds: float | None = None
    
    # ========== 场景通用配置 ==========
    # 按场景 max_tokens（保留现有，见 PRD R1.2）
    llm_default_max_tokens: int = 131072
    llm_compact_max_tokens: int = 8192
    llm_memory_extractor_max_tokens: int = 4096
    llm_memory_dream_max_tokens: int = 8192
    llm_memory_selector_max_tokens: int = 256
```

**需要删除的现有字段**:
- `model` → `anthropic_model`（重命名）
- `subagent_default_model` → `anthropic_subagent_model`
- `subagent_fast_model` → `anthropic_subagent_fast_model`
- `memory_selector_model` / `memory_extractor_model` / `memory_dream_model`
  → 加 `anthropic_` 前缀
- 不保留 `@property` 别名或旧字段映射

**同步更新**: `.env` 和 `.env.example`（`.env` 中必须显式写 `AGENTOS_LLM_PROVIDER=anthropic`，
否则启动自检直接报错；`.env.example` 只放键名与安全占位符）

**验证**:
```python
from core.config import settings

# .env 已显式配置 AGENTOS_LLM_PROVIDER 与 AGENTOS_ANTHROPIC_MODEL
assert settings.llm_provider == "anthropic"
assert settings.anthropic_model  # 非空即可，不断言具体模型名
```

---

### 3.2 实现工厂函数

**文件**: `llm/factory.py` (新建)

**核心逻辑**：根据 scenario 解析 model，根据 provider 创建对应客户端

```python
from core.config import settings
from llm.anthropic_client import AnthropicClient
from llm.base import BaseLLMClient
from llm.openai_client import OpenAIClient


def _resolve_model(provider: str, scenario: str | None) -> str:
    """根据 provider 和 scenario 解析最终使用的模型。

    优先级：场景覆盖 > provider 主模型；都没配则抛中文 AgentException。

    Args:
        provider: "anthropic" | "openai"
        scenario: "agent" | "subagent" | "subagent_fast" | "compact" |
                  "memory_extractor" | "memory_dream" | "memory_selector"，
                  None 表示直接使用 provider 主模型

    Returns:
        模型名称字符串
    """
    if scenario:
        scenario_model = getattr(settings, f"{provider}_{scenario}_model", None)
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


def create_llm_client(scenario: str | None = None) -> BaseLLMClient:
    """创建 LLM 客户端，根据全局 provider 和场景选择模型。

    provider 未配置、取值不支持、api_key 缺失、模型解析不出时一律抛中文
    AgentException，不做任何静默兜底（不存在「默认 Anthropic」这种回落）。

    Args:
        scenario: 场景标识，None 表示使用 provider 主模型

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
```

**导入提醒**：需 `from core.errors import AgentException`。全程使用 `AgentException.message()`，
不要用 `ValueError`——业务错误统一入口是 `AgentException`（见 CLAUDE.md 与 prd D9）。

**更新 llm/__init__.py**:
```python
from llm.base import BaseLLMClient
from llm.anthropic_client import AnthropicClient
from llm.factory import create_llm_client
from llm.openai_client import OpenAIClient

__all__ = ["BaseLLMClient", "AnthropicClient", "OpenAIClient", "create_llm_client"]
```

**验证**:
```python
import pytest
import unittest.mock as mock

from core.config import settings
from core.errors import AgentException
from llm import create_llm_client

# .env 中显式配置 AGENTOS_LLM_PROVIDER=anthropic 时
client = create_llm_client("agent")
assert client.__class__.__name__ == "AnthropicClient"

# 模拟 OpenAI
with mock.patch.object(settings, "llm_provider", "openai"):
    with mock.patch.object(settings, "openai_api_key", "sk-test"):
        client = create_llm_client("memory_selector")
        assert client.__class__.__name__ == "OpenAIClient"

# 未配置 provider：必须抛中文错误，不得回落到 Anthropic
with mock.patch.object(settings, "llm_provider", None):
    with pytest.raises(AgentException, match="未配置 LLM provider"):
        create_llm_client("agent")

# provider 取值不支持
with mock.patch.object(settings, "llm_provider", "gemini"):
    with pytest.raises(AgentException, match="不支持的 LLM provider"):
        create_llm_client("agent")

# provider 已配置但缺 api_key
with mock.patch.object(settings, "llm_provider", "openai"):
    with mock.patch.object(settings, "openai_api_key", None):
        with pytest.raises(AgentException, match="AGENTOS_OPENAI_API_KEY"):
            create_llm_client("agent")
```

---

## Phase 4: Service 层迁移（1.5天）

### 4.1 明确所有注入点与 scenario 映射

**需要改动的 7 处调用方**：

| 文件路径 | 类/函数 | scenario 值 | 说明 |
|---------|--------|------------|------|
| `runtime/agent.py` | `AgentRuntime.__init__` | `"agent"` | 主 Agent 执行 |
| `runtime/subagent.py` | `SubAgentRunner.__init__` | `"subagent"` | SubAgent 执行 |
| `runtime/compact.py` | `CompactService.__init__` | `"compact"` | 上下文压缩 |
| `memory/jobs/extraction.py` | `MemoryExtractionService.extract_with_llm` | `"memory_extractor"` | Memory 提取 |
| `memory/jobs/dream.py` | `MemoryDreamService.complete_with_llm` | `"memory_dream"` | Memory 归纳 |
| `memory/recall/service.py` | `MemoryRecallService.__init__` | `"memory_selector"` | Memory 召回选择 |
| （预留） | 未来扩展 | 自定义 scenario | 新场景直接在 Settings 加字段 |

---

### 4.2 Memory Recall Service

**文件**: `memory/recall/service.py`

**变更**:
```python
# 原代码
class MemoryRecallService:
    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()

# 新代码
from llm import BaseLLMClient, create_llm_client

class MemoryRecallService:
    def __init__(self, llm: BaseLLMClient | None = None):
        self.llm = llm or create_llm_client(scenario="memory_selector")
```

**验证**: `pytest tests/test_memory_recall.py -v`

---

### 4.3 Memory Extraction Service

**文件**: `memory/jobs/extraction.py`

**变更**:
```python
# 原 classmethod 签名
@classmethod
async def extract_with_llm(
    cls,
    ...,
    llm: LLMClient,  # 外部传入
):

# 新签名
from llm import BaseLLMClient, create_llm_client

@classmethod
async def extract_with_llm(
    cls,
    ...,
    llm: BaseLLMClient | None = None,  # 改为可选
):
    client = llm or create_llm_client(scenario="memory_extractor")
```

**调用方更新**: `memory/jobs/service.py` 的 `MemoryJobRunner` 构造器注入

---

### 4.4 Memory Dream Service

**文件**: `memory/jobs/dream.py`

**变更**: 同 4.3，scenario 改为 `"memory_dream"`

---

### 4.5 Compact Service

**文件**: `runtime/compact.py`

**变更**:
```python
# 原代码
class CompactService:
    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()

# 新代码
from llm import BaseLLMClient, create_llm_client

class CompactService:
    def __init__(self, llm: BaseLLMClient | None = None):
        self.llm = llm or create_llm_client(scenario="compact")
```

**验证**: `pytest tests/test_runtime_compact.py -v`

---

### 4.6 AgentRuntime（迁移到工厂）

**文件**: `runtime/agent.py`

**变更**:
```python
# 原代码
from llm import LLMClient

class AgentRuntime:
    def __init__(
        self,
        session_id: int,
        ...,
    ):
        self.session_id = session_id
        ...
        self.llm = LLMClient()

# 新代码
from llm import BaseLLMClient, create_llm_client

class AgentRuntime:
    def __init__(
        self,
        session_id: int,
        ...,
        llm: BaseLLMClient | None = None,  # 新增参数，放最后
    ):
        self.session_id = session_id
        ...
        # 使用工厂，但 scenario 指定 "agent"
        self.llm = llm or create_llm_client(scenario="agent")
```

**重要**: 默认走 `create_llm_client("agent")`，在全局 provider=anthropic 时自动使用 AnthropicClient

**验证**: 
- `pytest tests/test_runtime_agent.py -v`
- 确认 Prompt Caching 日志正常（"prompt cache 使用情况"）

---

### 4.7 SubAgentRunner（迁移到工厂）

**文件**: `runtime/subagent.py`

**变更**:
```python
# 原代码
from llm import LLMClient

class SubAgentRunner:
    def __init__(self, ...):
        self.llm = LLMClient()

# 新代码
from llm import BaseLLMClient, create_llm_client

class SubAgentRunner:
    def __init__(
        self,
        ...,
        llm: BaseLLMClient | None = None,  # 新增参数
    ):
        self.llm = llm or create_llm_client(scenario="subagent")
```

**验证**: `pytest tests/test_runtime_subagent.py -v`

---

## Phase 5: 测试（1.0天）

### 5.1 单元测试

**新增文件**:
- `tests/test_llm_base_client.py`: 接口契约（用 Mock 验证）
- `tests/test_llm_anthropic_client.py`: AnthropicClient Mock 测试
- `tests/test_llm_openai_client.py`: OpenAIClient Mock 测试（重点：`complete_structured`）
- `tests/test_llm_factory.py`: 工厂函数、_resolve_model、provider 切换

**测试要点**:

#### 5.1.1 测试 _resolve_model 场景覆盖逻辑
```python
# tests/test_llm_factory.py
from unittest.mock import patch
from llm.factory import _resolve_model
from core.config import settings

def test_resolve_model_scenario_override():
    """验证场景覆盖优先于主模型。"""
    with patch.object(settings, "anthropic_model", "claude-3-5-sonnet-20241022"):
        with patch.object(settings, "anthropic_memory_selector_model", "claude-3-haiku-20240307"):
            model = _resolve_model("anthropic", "memory_selector")
            assert model == "claude-3-haiku-20240307"

def test_resolve_model_fallback_to_main():
    """验证未配置场景覆盖时回落主模型。"""
    with patch.object(settings, "openai_model", "gpt-4o-2024-08-06"):
        with patch.object(settings, "openai_agent_model", None):
            model = _resolve_model("openai", "agent")
            assert model == "gpt-4o-2024-08-06"
```

#### 5.1.2 测试工厂 provider 切换
```python
def test_create_llm_client_anthropic():
    """验证 provider=anthropic 创建 AnthropicClient。"""
    with patch.object(settings, "llm_provider", "anthropic"):
        client = create_llm_client("agent")
        assert client.__class__.__name__ == "AnthropicClient"

def test_create_llm_client_openai():
    """验证 provider=openai 创建 OpenAIClient。"""
    with patch.object(settings, "llm_provider", "openai"):
        with patch.object(settings, "openai_api_key", "sk-test"):
            client = create_llm_client("memory_selector")
            assert client.__class__.__name__ == "OpenAIClient"

def test_create_llm_client_openai_missing_key():
    """验证 OpenAI 缺失 API Key 时抛中文 AgentException。"""
    with patch.object(settings, "llm_provider", "openai"):
        with patch.object(settings, "openai_api_key", None):
            with pytest.raises(AgentException, match="AGENTOS_OPENAI_API_KEY"):
                create_llm_client("agent")

def test_create_llm_client_provider_missing():
    """验证 provider 未配置时抛错，且不回落到 Anthropic。"""
    with patch.object(settings, "llm_provider", None):
        with pytest.raises(AgentException, match="未配置 LLM provider"):
            create_llm_client("agent")

def test_create_llm_client_provider_unsupported():
    """验证 provider 取值不支持时抛错。"""
    with patch.object(settings, "llm_provider", "gemini"):
        with pytest.raises(AgentException, match="不支持的 LLM provider"):
            create_llm_client("agent")

def test_resolve_model_missing():
    """验证场景与主模型都未配置时抛错，不返回 None。"""
    with patch.object(settings, "llm_provider", "anthropic"):
        with patch.object(settings, "anthropic_model", None):
            with patch.object(settings, "anthropic_agent_model", None):
                with pytest.raises(AgentException, match="未配置 anthropic 的模型"):
                    create_llm_client("agent")
```

**导入提醒**：测试需 `from core.errors import AgentException`；断言一律用 `AgentException`，
不要用 `ValueError`。

#### 5.1.3 测试 OpenAI 参数映射
```python
# tests/test_llm_openai_client.py
@pytest.mark.asyncio
async def test_complete_structured_with_mock():
    """验证 OpenAI complete_structured 正确映射参数。"""
    client = OpenAIClient(model="gpt-4o", api_key="sk-test")
    
    # Mock AsyncOpenAI
    with patch("llm.openai_client.AsyncOpenAI") as mock_openai:
        mock_response = MagicMock()
        mock_response.choices[0].message.parsed = SampleModel(field="value")
        mock_openai.return_value.chat.completions.parse = AsyncMock(
            return_value=mock_response
        )
        
        result = await client.complete_structured(
            messages=[{"role": "user", "content": "test"}],
            output_format=SampleModel,
            system_prompt="You are helpful",
            max_tokens=512,
        )
        
        # 验证调用参数
        call_kwargs = mock_openai.return_value.chat.completions.parse.call_args[1]
        assert call_kwargs["messages"][0] == {"role": "system", "content": "You are helpful"}
        assert call_kwargs["max_completion_tokens"] == 512  # 参数名转换
        assert result.field == "value"
```

**运行**: `pytest tests/test_llm_*.py -v`

---

### 5.2 Service 层 Mock 注入测试

**新增文件**: `tests/test_service_llm_injection.py`

**验证各 Service 正确使用工厂的 scenario**:
```python
from unittest.mock import patch, MagicMock
from memory.recall.service import MemoryRecallService
from runtime.compact import CompactService

def test_memory_recall_uses_correct_scenario():
    """验证 MemoryRecallService 使用 scenario='memory_selector'。"""
    with patch("memory.recall.service.create_llm_client") as mock_factory:
        mock_factory.return_value = MagicMock()
        service = MemoryRecallService()
        mock_factory.assert_called_once_with(scenario="memory_selector")

def test_compact_uses_correct_scenario():
    """验证 CompactService 使用 scenario='compact'。"""
    with patch("runtime.compact.create_llm_client") as mock_factory:
        mock_factory.return_value = MagicMock()
        service = CompactService()
        mock_factory.assert_called_once_with(scenario="compact")
```

---

### 5.3 集成测试（可选，需真实 API Key）

**新增文件**:
- `tests/test_memory_openai_integration.py`

**内容**:
```python
@pytest.mark.skipif(
    not os.getenv("AGENTOS_OPENAI_API_KEY"),
    reason="需要 AGENTOS_OPENAI_API_KEY"
)
@pytest.mark.asyncio
async def test_memory_extraction_with_openai():
    """端到端验证 Memory Extraction 使用 OpenAI。"""
    # 临时覆盖配置
    with patch.object(settings, "llm_provider", "openai"):
        # 调用真实的 MemoryExtractionService.extract_with_llm
        ...
```

**运行**: `AGENTOS_OPENAI_API_KEY=sk-... pytest tests/test_memory_openai_integration.py -v`

---

### 5.4 回归测试

**运行现有测试套件**:
```bash
pytest tests/ -v --ignore=tests/test_memory_openai_integration.py
```

**关键验证**:
- 主 Agent 流式对话（`tests/test_runtime_agent.py`）
- SubAgent 执行（`tests/test_runtime_subagent.py`）
- Memory 提取/Dream/召回（`tests/test_memory_*.py`）
- Compact 压缩（`tests/test_runtime_compact.py`）

**通过标准**: 所有现有测试 100% 通过

---

## Phase 6: 文档与清理（0.5天）

### 6.1 更新 CLAUDE.md

**新增章节**: "多 Provider 配置"

```markdown
## 多 Provider 配置

AgentOS 支持 Anthropic 和 OpenAI 两种 LLM Provider，通过全局开关统一切换：

### 全局 Provider 开关

```bash
# 全局 provider 选择（所有场景生效）
AGENTOS_LLM_PROVIDER=anthropic  # 或 openai
```

- **全局切换**：一个开关控制所有场景（Agent、SubAgent、Compact、Memory 系列）
- **按场景档位**：同一 provider 下，不同场景可配置不同档位模型

### 环境变量配置

```bash
# ========== Anthropic 配置组 ==========
AGENTOS_ANTHROPIC_API_KEY=sk-ant-...
AGENTOS_ANTHROPIC_MODEL=claude-3-5-sonnet-20241022  # 主模型

# 场景覆盖（可选，不配置则使用主模型）
AGENTOS_ANTHROPIC_AGENT_MODEL=claude-3-5-sonnet-20241022
AGENTOS_ANTHROPIC_SUBAGENT_MODEL=claude-3-5-sonnet-20241022
AGENTOS_ANTHROPIC_COMPACT_MODEL=claude-3-5-sonnet-20241022
AGENTOS_ANTHROPIC_MEMORY_EXTRACTOR_MODEL=claude-3-5-sonnet-20241022
AGENTOS_ANTHROPIC_MEMORY_DREAM_MODEL=claude-3-5-sonnet-20241022
AGENTOS_ANTHROPIC_MEMORY_SELECTOR_MODEL=claude-3-haiku-20240307  # 示例：用 Haiku 降本

# ========== OpenAI 配置组 ==========
AGENTOS_OPENAI_API_KEY=sk-...
AGENTOS_OPENAI_MODEL=gpt-4o-2024-08-06  # 主模型

# 场景覆盖（可选）
AGENTOS_OPENAI_AGENT_MODEL=gpt-4o-2024-08-06
AGENTOS_OPENAI_MEMORY_SELECTOR_MODEL=gpt-4o-mini  # 示例：用 Mini 降本
```

### 配置优先级

**模型解析优先级**：场景覆盖 > provider 主模型

示例：
```bash
AGENTOS_LLM_PROVIDER=anthropic
AGENTOS_ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
AGENTOS_ANTHROPIC_MEMORY_SELECTOR_MODEL=claude-3-haiku-20240307
```
- Agent、SubAgent、Compact 使用 `claude-3-5-sonnet-20241022`
- Memory Selector 使用 `claude-3-haiku-20240307`

### 代码中自定义客户端

```python
from llm import OpenAIClient, AnthropicClient, create_llm_client

# 方式1: 工厂函数（推荐，自动读取全局 provider 和场景配置）
client = create_llm_client(scenario="memory_selector")

# 方式2: 直接创建（测试或特殊场景）
client = OpenAIClient(model="gpt-4o-mini", api_key="...")

# 方式3: 依赖注入（Service 测试）
service = MemoryRecallService(llm=custom_client)
```

### 场景列表

| scenario 值 | 用途 | 对应 Service |
|------------|------|-------------|
| `agent` | 主 Agent 执行 | `AgentRuntime` |
| `subagent` | SubAgent 执行 | `SubAgentRunner` |
| `compact` | 上下文压缩 | `CompactService` |
| `memory_extractor` | Memory 提取 | `MemoryExtractionService` |
| `memory_dream` | Memory 归纳 | `MemoryDreamService` |
| `memory_selector` | Memory 召回 | `MemoryRecallService` |
```

---

### 6.2 清理临时代码

**检查清单**:
- [ ] 删除 `llm/client.py`（已重命名为 `anthropic_client.py`）
- [ ] 确认 `.env.example` 包含所有新配置项（provider 开关 + 两组模型配置）
- [ ] 确认 `.env` 不包含真实密钥（提交前检查）
- [ ] 删除 Debug 日志或临时 print
- [ ] 删除旧的 `subagent_*_model` 环境变量引用（如果存在）

---

### 6.3 Code Review Checklist

提交前自查：

**架构**:
- [ ] `AgentRuntime` 和 `SubAgentRunner` 使用 `create_llm_client(scenario="agent/subagent")`
- [ ] 其他 Service 通过 `create_llm_client(scenario=...)` 工厂创建
- [ ] `BaseLLMClient` 接口参数名沿用现有命名（`system_prompt`/`output_format`）
- [ ] AnthropicClient 和 OpenAIClient 构造器必填 `model` 参数

**配置**:
- [ ] `core/config.py`、`.env`、`.env.example` 三处同步
- [ ] `llm_provider` 无默认值；未配置 / 取值不支持 / api_key 缺失 / 模型解析不出
      均抛具体中文 `AgentException`，代码中不存在 `x or AnthropicClient()` 式兜底
- [ ] `main.py` lifespan 中有一次 `create_llm_client()` 启动自检
- [ ] 两组 provider 配置完整（主模型 + 7 个场景覆盖 + 重试/超时）
- [ ] `openai_api_key` 在工厂函数中校验非空

**迁移完整性**:
- [ ] 7 处调用方已全部迁移到 `create_llm_client(scenario=...)`
- [ ] 所有 `from llm import LLMClient` 已改为 `from llm import BaseLLMClient, create_llm_client`
- [ ] 删除 `llm/client.py`（已重命名为 `anthropic_client.py`）
- [ ] 删除 `_complete_structured()` 方法及其测试
- [ ] 回归测试 100% 通过

**测试**:
- [ ] Mock 测试覆盖 `_resolve_model` 场景覆盖和回落逻辑
- [ ] Mock 测试覆盖 OpenAI 参数映射（`max_completion_tokens`、system 消息转换）
- [ ] Service 层 Mock 测试验证 scenario 参数正确传递
- [ ] 集成测试可选执行（需真实 API Key）
- [ ] 错误场景测试（provider 不支持、API Key 缺失）

**文档**:
- [ ] `CLAUDE.md` 更新多 Provider 配置说明（全局开关 + 场景档位）
- [ ] `design.md` 和 `implement.md` 与代码一致
- [ ] 提交信息清晰（feat: 统一大模型调用入口，支持全局 provider 切换）

---

## 关键命令速查

### 验证配置加载
```bash
python -c "from core.config import settings; print(settings.openai_model)"
```

### 运行单元测试
```bash
pytest tests/test_llm_*.py -v
```

### 运行回归测试
```bash
pytest tests/ -v --ignore=tests/test_memory_openai_integration.py
```

### 运行完整测试（含集成）
```bash
AGENTOS_OPENAI_API_KEY=sk-... pytest tests/ -v
```

### 类型检查
```bash
mypy llm/ --strict
```

### 代码格式化
```bash
ruff format llm/
```

---

## 风险回滚方案

如果重构导致线上问题，按以下顺序回滚：

1. **立即回滚**：恢复 `llm/client.py`（从 git 历史）
2. **恢复调用方**：7 处注入点恢复为 `LLMClient()` 直接实例化
3. **配置回退**：`.env` 中 `llm_provider` 改回 `anthropic`
4. **数据库无影响**（消息格式未变，无需回滚数据）

---

## 预估时间分配

| Phase | 任务 | 预估时间 | 关键变更 |
|-------|------|---------|---------|
| 1 | 基础设施层（接口 + AnthropicClient 重构） | 0.5天 | `llm/base.py`、构造器必填 `model`、删除别名 |
| 2 | OpenAIClient 实现 + 删除废弃方法 | 1.5天 | `llm/openai_client.py`、删除 `_complete_structured` |
| 3 | 配置与工厂 | 1.0天 | 全局 provider 开关、_resolve_model、7 个 scenario 映射 |
| 4 | Service 层迁移（7 处调用方） | 1.0天 | 所有注入点改用 `create_llm_client(scenario=...)`、删除 import 别名 |
| 5 | 测试（单元 + Service Mock + 回归） | 1.0天 | _resolve_model 测试、scenario 注入测试 |
| 6 | 文档与清理 | 0.5天 | CLAUDE.md 全局 provider 说明、确认无别名残留 |
| **总计** | | **5.5天** | |

**缓冲**: 预留 0.5天 处理意外问题（配置迁移、测试调整），**总工期 6.0天以内完成**。

**相比旧版调整**：
- Phase 2 新增删除 `_complete_structured` 废弃方法（无额外时间，与 OpenAI 实现并行）
- Phase 4 因不保留兼容性，直接改 import 更快（-0.5天）
- Phase 6 无需维护别名和 deprecated 逻辑（省时）
- 总工期从 6.5天降至 6.0天

---

## 完成标志

- [x] 所有验收标准（AC1-AC5）通过
- [x] 回归测试 100% 通过
- [x] 全局 provider 可切换到 OpenAI，所有场景正常工作
- [x] Memory Selector 可通过场景覆盖配置切换到 Haiku/Mini 并正常工作
- [x] 主 Agent 的 Prompt Caching 日志正常（全局 provider=anthropic 时）
- [x] _resolve_model 场景覆盖和回落逻辑测试通过
- [x] 7 处 Service 注入点的 scenario 参数验证通过
- [x] Code Review 清单全部勾选
- [x] 文档更新完成（CLAUDE.md 包含全局开关和场景档位说明）
