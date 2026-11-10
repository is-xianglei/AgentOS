"""模型请求黄金矩阵：固化发往两家 SDK 的完整请求参数。

逐个组合截获传给 AsyncAnthropic / AsyncOpenAI 构造器，以及 messages.stream、
messages.create、chat.completions.create、chat.completions.parse 的全部 kwargs，
与 tests/fixtures/model_request_golden.py 的字面量逐项比对（含嵌套字典键序）。
字面量由阶段 2b 重构前的代码实际产出生成；此后只允许改动本文件「驱动」一节
（如何解析客户端、如何组装调用参数），字面量一个都不许改。重构前就抛异常的组合，
固化的是异常类型、文案与详情。
"""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock
from uuid import UUID

import anthropic
import openai
import pytest
from anthropic.types import TextBlock as SdkTextBlock
from anthropic.types import ThinkingBlock as SdkThinkingBlock
from anthropic.types import ToolUseBlock as SdkToolUseBlock
from pydantic import BaseModel

from agent.service import AgentRuntimeConfig
from core.config import settings as app_settings
from core.events import ORCHESTRATOR_ACTOR
from harness.adapters.models import anthropic_client as anthropic_module
from harness.adapters.models import openai_client as openai_module
from harness.adapters.models.resolver import DatabaseModelResolver
from harness.adapters.tools.builtin.echo import EchoTool
from harness.app.ports import Effort, ResolvedModel
from harness.app.subagent_catalog import get_subagent_spec
from harness.app.tool_set import ToolSet
from harness.domain.context import Context, PromptBlock, messages_from_rows
from harness.domain.messages import AssistantMessage, ThinkingBlock, ToolCall, ToolResult
from harness.domain.roles.subagent import AgentType, SubAgent
from memory.jobs.dream import MemoryDreamService
from memory.jobs.extraction import MemoryExtractionService
from memory.recall.service import MemoryRecallService
from memory.repository import MemoryCatalogEntry
from model_service.models import ModelProviderRecord
from model_service.service import ModelService
from runtime.agent import AgentRuntime
from runtime.step import AgentStep
from runtime.stream_assembler import TurnEventAssembler
from runtime.subagent import SubAgentRunner
from runtime.workflow import LlmRoutingOrchestrator, ParallelOrchestrator
from session.service import SessionService
from tests.fixtures.model_request_golden import EXPECTED

pytestmark = pytest.mark.anyio

# 固定全部相关配置，避免本地 .env 影响请求参数。
_FIXED_SETTINGS: dict[str, Any] = {
    "llm_provider": "anthropic",
    "llm_default_max_tokens": 16384,
    "llm_memory_selector_max_tokens": 256,
    "llm_memory_extractor_max_tokens": 4096,
    "llm_memory_dream_max_tokens": 8192,
    "memory_selector_timeout_seconds": 5.0,
    "memory_extractor_timeout_seconds": 20.0,
    "memory_dream_timeout_seconds": 60.0,
    "memory_recall_max_items": 5,
    "anthropic_api_key": "env-anthropic-key",
    "anthropic_base_url": "https://anthropic.env.test",
    "anthropic_max_retries": 2,
    "anthropic_timeout_seconds": None,
    "anthropic_thinking_enabled": False,
    "anthropic_thinking_budget_tokens": 4096,
    "openai_api_key": "env-openai-key",
    "openai_base_url": "https://openai.env.test/v1",
    "openai_max_retries": 3,
    "openai_timeout_seconds": 45.0,
    "openai_thinking_enabled": False,
    "openai_reasoning_effort": None,
}
# 场景模型：主模型加部分场景覆盖，覆盖命中与回落主模型两种解析都会出现在请求里。
_SCENARIO_SUFFIXES: dict[str, str | None] = {
    "model": "env",
    "agent_model": None,
    "subagent_model": "sub",
    "subagent_fast_model": "fast",
    "compact_model": "compact",
    "memory_selector_model": None,
    "memory_extractor_model": None,
    "memory_dream_model": "dream",
    "workflow_model": "workflow",
}
for _provider, _prefix in (("anthropic", "claude"), ("openai", "gpt")):
    for _key, _suffix in _SCENARIO_SUFFIXES.items():
        _FIXED_SETTINGS[f"{_provider}_{_key}"] = f"{_prefix}-{_suffix}" if _suffix else None
_FIXED_SETTINGS.update(
    {
        "memory_extractor_max_messages": 10,
        "memory_extractor_max_chars": 20_000,
        "memory_extractor_prompt_version": "v1",
        "memory_dream_max_input_chars": 120_000,
        "memory_dream_prompt_version": "v1",
    }
)

_TURN_ID = UUID("00000000-0000-4000-8000-000000000001")
_JOB_ID = UUID("00000000-0000-4000-8000-000000000002")


@dataclass(frozen=True)
class _Endpoint:
    """工作区数据库里的一条对话模型配置。"""

    provider: str
    extra: dict[str, Any]
    api_key: str = "db-key"


def _levels(**values: str | int) -> list[dict[str, str | int]]:
    return [{"key": key, "value": value} for key, value in values.items()]


_DB_ENDPOINTS: dict[str, _Endpoint] = {
    "anthropic_plain": _Endpoint("anthropic", {}),
    "anthropic_budget": _Endpoint("anthropic", {"thinking_budget_tokens": 2048}),
    "anthropic_legacy_levels": _Endpoint("anthropic", {"supports_reasoning_effort": True}),
    "anthropic_int_levels": _Endpoint(
        "anthropic",
        {
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": _levels(medium=2048, max=8192),
            "default_reasoning_effort": "medium",
            "thinking_budget_tokens": 3072,
        },
    ),
    "openai_plain": _Endpoint("openai", {}),
    "openai_fast_none": _Endpoint("openai", {"fast_reasoning_effort": "none"}),
    "openai_str_levels": _Endpoint(
        "openai",
        {
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": _levels(low="minimal", high="high"),
            "default_reasoning_effort": "low",
            "fast_reasoning_effort": "none",
        },
    ),
    "deepseek_plain": _Endpoint("deepseek", {}),
    "deepseek_legacy_levels": _Endpoint("deepseek", {"supports_reasoning_effort": True}),
    "deepseek_int_levels": _Endpoint(
        "deepseek",
        {
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": _levels(medium=4096),
            "default_reasoning_effort": "medium",
        },
    ),
    "template_plain": _Endpoint("openai_compatible", {}, api_key=""),
    "template_str_levels": _Endpoint(
        "openai_compatible",
        {
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": _levels(max="high"),
            "default_reasoning_effort": "max",
        },
    ),
    "template_int_levels": _Endpoint(
        "zhipu",
        {
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": _levels(medium=2048, max=8192),
            "default_reasoning_effort": "medium",
        },
    ),
    "override_openai_protocol": _Endpoint(
        "openai_compatible",
        {
            "thinking_protocol": "openai",
            "fast_reasoning_effort": "none",
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": _levels(high="high"),
            "default_reasoning_effort": "high",
        },
    ),
}
# 调用点矩阵的工作区端点：声明了上下文长度，压缩阈值随之变化。
_WORLD_ENDPOINTS: dict[str, _Endpoint | None] = {
    "env_anthropic": None,
    "env_openai": None,
    "db_anthropic": _Endpoint(
        "anthropic", {**_DB_ENDPOINTS["anthropic_int_levels"].extra, "context_length": 48}
    ),
    "db_deepseek": _Endpoint("deepseek", {"supports_reasoning_effort": True, "context_length": 48}),
}
_WORLD_SETTINGS: dict[str, dict[str, Any]] = {
    "env_anthropic": {"llm_provider": "anthropic", "anthropic_thinking_enabled": True},
    "env_openai": {
        "llm_provider": "openai",
        "openai_thinking_enabled": True,
        "openai_reasoning_effort": "low",
    },
    "db_anthropic": {"llm_provider": "anthropic"},
    "db_deepseek": {"llm_provider": "anthropic"},
}


# ---------------------------------------------------------------------------
# SDK 替身：只记录、不联网
# ---------------------------------------------------------------------------

_NOT_GIVEN_MARK = "<NOT_GIVEN>"


def _normalize(value: Any) -> Any:
    """把请求参数转成可写成字面量的结构；遇到无法固化的类型直接报错，不静默丢弃。"""
    if isinstance(value, (anthropic.NotGiven, openai.NotGiven)):
        return _NOT_GIVEN_MARK
    if isinstance(value, type):
        return f"<class {value.__name__}>"
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"黄金矩阵无法固化的取值类型: {type(value).__name__}")


def _ordered(value: Any) -> Any:
    """连同字典键序一起比较：键序决定 SDK 序列化出的请求体字节。"""
    if isinstance(value, dict):
        return ("dict", tuple((key, _ordered(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_ordered(item) for item in value))
    return value


class _AnthropicStream:
    """messages.stream() 上下文内的流：不下发增量，终局消息直接给出。"""

    def __init__(self, response: Any) -> None:
        self._response = response

    def __aiter__(self) -> _AnthropicStream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def get_final_message(self) -> Any:
        return self._response


class _OpenAIStream:
    """chat.completions.create(stream=True) 的返回：一段文本、收束、末尾用量。"""

    _CHUNKS = (
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def __aiter__(self):  # 异步生成器即迭代器
        for chunk in self._CHUNKS:
            yield chunk


@dataclass
class _Sdk:
    """两家 SDK 的替身：按序记录构造参数与请求 kwargs，结构化调用按队列给出答案。"""

    answers: list[dict[str, Any]]
    log: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0

    def _next_answer(self) -> dict[str, Any]:
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.log.append({"sdk": name, "kwargs": _normalize(kwargs)})

    def anthropic_client(self, **kwargs: Any) -> Any:
        self._record("AsyncAnthropic", kwargs)
        messages = SimpleNamespace(stream=self._anthropic_stream, create=self._anthropic_create)
        return SimpleNamespace(messages=messages)

    def _anthropic_response(self, kwargs: dict[str, Any], *, thinking: bool) -> Any:
        self.calls += 1
        tool_names = [tool.get("name") for tool in kwargs.get("tools") or []]
        if "emit_structured_output" not in tool_names:
            text = SdkTextBlock(type="text", text="ok")
            return SimpleNamespace(content=[text], stop_reason="end_turn", usage=None)
        content: list[Any] = []
        if thinking:
            content.append(
                SdkThinkingBlock(type="thinking", thinking="核对字段", signature="sig-1")
            )
        content.append(
            SdkToolUseBlock(
                type="tool_use",
                id=f"call-{self.calls}",
                name="emit_structured_output",
                input=self._next_answer(),
            )
        )
        return SimpleNamespace(content=content, stop_reason="tool_use", usage=None)

    @asynccontextmanager
    async def _anthropic_stream(self, **kwargs: Any):  # 上下文管理器工厂
        self._record("anthropic.messages.stream", kwargs)
        yield _AnthropicStream(self._anthropic_response(kwargs, thinking=True))

    async def _anthropic_create(self, **kwargs: Any) -> Any:
        self._record("anthropic.messages.create", kwargs)
        return self._anthropic_response(kwargs, thinking=False)

    def openai_client(self, **kwargs: Any) -> Any:
        self._record("AsyncOpenAI", kwargs)
        completions = SimpleNamespace(create=self._openai_create, parse=self._openai_parse)
        return SimpleNamespace(chat=SimpleNamespace(completions=completions))

    async def _openai_create(self, **kwargs: Any) -> Any:
        self._record("openai.chat.completions.create", kwargs)
        return _OpenAIStream()

    async def _openai_parse(self, **kwargs: Any) -> Any:
        self._record("openai.chat.completions.parse", kwargs)
        parsed = kwargs["response_format"].model_validate(self._next_answer())
        message = SimpleNamespace(parsed=parsed)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def _outcome(sdk: _Sdk, exc: BaseException | None = None) -> dict[str, Any]:
    """一个组合的完整观测：SDK 调用序列，以及（若有）抛出的异常。"""
    result: dict[str, Any] = {"calls": sdk.log}
    if exc is not None:
        error: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
        details = getattr(exc, "details", None)
        if details:
            error["details"] = _normalize(details)
        result["error"] = error
    return result


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> _Sdk:
    """固定配置并替换两家 SDK 客户端类；默认结构化答案为 {"answer": 42}。"""
    for key, value in _FIXED_SETTINGS.items():
        monkeypatch.setattr(app_settings, key, value)
    fake = _Sdk(answers=[{"answer": 42}])
    monkeypatch.setattr(anthropic_module, "AsyncAnthropic", fake.anthropic_client)
    monkeypatch.setattr(openai_module, "AsyncOpenAI", fake.openai_client)
    return fake


def _record(endpoint: _Endpoint, *, model_name: str = "db-model") -> ModelProviderRecord:
    base_url = "https://anthropic.db.test" if endpoint.provider == "anthropic" else (
        "https://db.test/v1"
    )
    return ModelProviderRecord(
        provider_id=f"provider-{endpoint.provider}",
        workspace_id=2,
        display_name="工作区模型",
        provider_type="chat",
        provider=endpoint.provider,
        base_url=base_url,
        api_key=endpoint.api_key,
        model_name=model_name,
        extra_config=dict(endpoint.extra),
        is_active=True,
    )


class _Answer(BaseModel):
    answer: int


# 统一的中立输入：覆盖 user / assistant（思考 + 文本 + 工具调用）/ tool 三种角色。
_MESSAGES: list[dict[str, Any]] = [
    {"role": "user", "content": "读一下 a.txt"},
    {
        "role": "assistant",
        "content": AssistantMessage.from_turn(
            "我来读",
            [ToolCall(id="toolu_1", tool_name="Read", arguments={"path": "a.txt"})],
            [ThinkingBlock(text="先想", signature="sig-0")],
        ).to_content(),
    },
    {
        "role": "tool",
        "content": ToolResult(
            tool_call_id="toolu_1", tool_name="Read", arguments={"path": "a.txt"}, content="A"
        ).to_content(),
    },
]
_SYSTEM_BLOCKS = [PromptBlock(text="稳定段", cache_breakpoint=True), PromptBlock(text="易失段")]
_USER = {"role": "user", "content": "你好"}
_ECHO_SPEC = EchoTool().to_spec()


# ---------------------------------------------------------------------------
# 驱动：如何解析客户端、如何发起调用。重构后只允许改动本节。
# ---------------------------------------------------------------------------


async def _resolve(
    scenario: str,
    *,
    workspace_id: int | None,
    effort: str | None,
) -> ResolvedModel:
    """经模型解析器拿到可调用的句柄（工作区数据库优先，回落 .env）。"""
    return await DatabaseModelResolver(object()).resolve(
        workspace_id, 3, scenario, effort=Effort.parse(effort)
    )


async def _stream(
    handle: ResolvedModel,
    *,
    instruction: str | list[PromptBlock] | None,
    messages: list[dict[str, Any]],
    tools: list[Any],
) -> None:
    if isinstance(instruction, list):
        instruction = tuple(instruction)
    context: Context[None] = Context(
        instruction=instruction, messages=messages_from_rows(messages), tools=tuple(tools)
    )
    async for _ in handle.client.stream(context, handle.settings):
        pass


async def _structured(
    handle: ResolvedModel,
    *,
    instruction: str | None,
    messages: list[dict[str, Any]],
    max_tokens: int,
    timeout: float | None,
) -> BaseModel:
    context = Context(
        instruction=instruction, messages=messages_from_rows(messages), output_type=_Answer
    )
    settings = replace(handle.settings, max_tokens=max_tokens, timeout=timeout)
    return await handle.client.complete_structured(context, settings)


# ---------------------------------------------------------------------------
# 组合：客户端矩阵（来源 × effort × 调用类型）与生产调用点矩阵
# ---------------------------------------------------------------------------

type _CaseRun = Callable[[_Sdk, pytest.MonkeyPatch], Awaitable[Any]]
_CASES: dict[str, _CaseRun] = {}


def _register(case_id: str, run: _CaseRun) -> None:
    assert case_id not in _CASES, f"组合 ID 重复: {case_id}"
    _CASES[case_id] = run


def _apply(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]) -> None:
    for key, value in overrides.items():
        monkeypatch.setattr(app_settings, key, value)


def _use_endpoint(monkeypatch: pytest.MonkeyPatch, endpoint: _Endpoint | None) -> None:
    """让工作区模型选择返回给定端点；None 表示工作区没有可用配置，回落 .env。"""
    record = None if endpoint is None else _record(endpoint)
    monkeypatch.setattr(ModelService, "select_provider", AsyncMock(return_value=record))


_ENV_SOURCES: dict[str, dict[str, Any]] = {
    "env_anthropic_off": {"llm_provider": "anthropic"},
    "env_anthropic_on": {"llm_provider": "anthropic", "anthropic_thinking_enabled": True},
    "env_anthropic_overflow": {
        "llm_provider": "anthropic",
        "anthropic_thinking_enabled": True,
        "anthropic_thinking_budget_tokens": 20000,
    },
    "env_openai_off": {"llm_provider": "openai"},
    "env_openai_off_effort": {"llm_provider": "openai", "openai_reasoning_effort": "high"},
    "env_openai_on": {"llm_provider": "openai", "openai_thinking_enabled": True},
    "env_openai_on_effort": {
        "llm_provider": "openai",
        "openai_thinking_enabled": True,
        "openai_reasoning_effort": "high",
    },
}
_DB_ENDPOINTS["anthropic_overflow"] = _Endpoint(
    "anthropic",
    {
        "supports_reasoning_effort": True,
        "reasoning_effort_levels": _levels(max=20000),
        "default_reasoning_effort": "max",
    },
)
_EFFORTS: tuple[str | None, ...] = (None, "fast", "medium", "max")


async def _client_stream(handle: Any) -> None:
    await _stream(handle, instruction=_SYSTEM_BLOCKS, messages=[_USER], tools=[_ECHO_SPEC])


async def _encoding_stream(handle: Any) -> None:
    """完整中立会话（含思考、工具调用与工具结果）的出站编码，每种协议各跑一次。"""
    await _stream(handle, instruction=_SYSTEM_BLOCKS, messages=_MESSAGES, tools=[_ECHO_SPEC])


async def _client_structured(handle: Any) -> None:
    await _structured(handle, instruction="选出答案", messages=[_USER], max_tokens=256, timeout=None)


_CALL_KINDS = {"stream": _client_stream, "structured": _client_structured}


def _client_case(
    source: str,
    endpoint: _Endpoint | None,
    overrides: dict[str, Any],
    effort: str | None,
    call: Callable[[Any], Awaitable[None]],
) -> _CaseRun:
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
        _apply(monkeypatch, overrides)
        _use_endpoint(monkeypatch, endpoint)
        workspace_id = None if endpoint is None else 2
        handle = await _resolve("agent", workspace_id=workspace_id, effort=effort)
        await call(handle)

    return run


for _source, _overrides in _ENV_SOURCES.items():
    for _effort in _EFFORTS:
        for _kind, _call in _CALL_KINDS.items():
            _register(
                f"client/{_source}/{_effort or 'none'}/{_kind}",
                _client_case(_source, None, _overrides, _effort, _call),
            )
for _source, _endpoint in _DB_ENDPOINTS.items():
    for _effort in _EFFORTS:
        for _kind, _call in _CALL_KINDS.items():
            _register(
                f"client/db_{_source}/{_effort or 'none'}/{_kind}",
                _client_case(_source, _endpoint, {}, _effort, _call),
            )


async def _structured_retry(sdk: _Sdk, effort: str | None, workspace_id: int | None) -> None:
    """首轮答案不合法、回灌错误后第二轮合法：固化 Anthropic 两轮请求的完整参数。"""
    sdk.answers[:] = [{"answer": "bad"}, {"answer": 42}]
    handle = await _resolve("agent", workspace_id=workspace_id, effort=effort)
    await _client_structured(handle)


def _retry_case(endpoint: _Endpoint | None, overrides: dict[str, Any], effort: str | None):
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
        _apply(monkeypatch, overrides)
        _use_endpoint(monkeypatch, endpoint)
        await _structured_retry(sdk, effort, None if endpoint is None else 2)

    return run


_register(
    "retry/env_anthropic_off/none", _retry_case(None, _ENV_SOURCES["env_anthropic_off"], None)
)
_register(
    "retry/env_anthropic_on/medium", _retry_case(None, _ENV_SOURCES["env_anthropic_on"], "medium")
)
_register(
    "retry/db_anthropic_int_levels/max",
    _retry_case(_DB_ENDPOINTS["anthropic_int_levels"], {}, "max"),
)
_register(
    "retry/db_anthropic_legacy_levels/medium",
    _retry_case(_DB_ENDPOINTS["anthropic_legacy_levels"], {}, "medium"),
)


async def _probe(record: ModelProviderRecord) -> None:
    """模型连通性测试：失败不抛出，只看发出去的请求。"""
    await ModelService(None).ping(record)


def _probe_case(endpoint: _Endpoint):
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
        await _probe(_record(endpoint))

    return run


for _source in ("anthropic_plain", "anthropic_int_levels", "openai_plain", "openai_fast_none",
                "deepseek_plain", "template_plain", "override_openai_protocol"):
    _register(f"probe/{_source}", _probe_case(_DB_ENDPOINTS[_source]))


def _max_tokens_case(endpoint: _Endpoint | None, overrides: dict[str, Any], effort: str | None):
    """结构化调用的 max_tokens / timeout 由调用方显式给出的路径（memory 三处同款）。"""

    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
        _apply(monkeypatch, overrides)
        _use_endpoint(monkeypatch, endpoint)
        handle = await _resolve("agent", workspace_id=None if endpoint is None else 2, effort=effort)
        await _structured(handle, instruction=None, messages=[_USER], max_tokens=32, timeout=7.5)

    return run


for _case_id, _endpoint, _overrides, _effort in (
    ("env_anthropic_on/none", None, _ENV_SOURCES["env_anthropic_on"], None),
    ("db_anthropic_int_levels/max", _DB_ENDPOINTS["anthropic_int_levels"], {}, "max"),
    ("db_anthropic_plain/medium", _DB_ENDPOINTS["anthropic_plain"], {}, "medium"),
    ("env_openai_on_effort/none", None, _ENV_SOURCES["env_openai_on_effort"], None),
    ("db_openai_str_levels/max", _DB_ENDPOINTS["openai_str_levels"], {}, "max"),
    ("db_template_int_levels/max", _DB_ENDPOINTS["template_int_levels"], {}, "max"),
):
    _register(f"max_tokens/{_case_id}", _max_tokens_case(_endpoint, _overrides, _effort))


def _encoding_case(endpoint: _Endpoint | None, overrides: dict[str, Any], effort: str | None):
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
        _apply(monkeypatch, overrides)
        _use_endpoint(monkeypatch, endpoint)
        handle = await _resolve("agent", workspace_id=None if endpoint is None else 2, effort=effort)
        await _encoding_stream(handle)

    return run


for _case_id, _endpoint, _overrides, _effort in (
    ("env_anthropic_off", None, _ENV_SOURCES["env_anthropic_off"], None),
    ("env_openai_on", None, _ENV_SOURCES["env_openai_on"], None),
    ("db_deepseek_plain", _DB_ENDPOINTS["deepseek_plain"], {}, "medium"),
    ("db_template_plain", _DB_ENDPOINTS["template_plain"], {}, "fast"),
):
    _register(f"encoding/{_case_id}", _encoding_case(_endpoint, _overrides, _effort))


# ---- 生产调用点：从各运行时的真实入口解析客户端并发起调用 ----


def _session(effort: str, *, model_name: str | None = None, workflow_id: int | None = None):
    return SimpleNamespace(
        id=1, workspace_id=2, user_id=3, agent_id=None, mode_slug="standard",
        thinking_effort=effort, model_provider_id=None, extra={}, model_name=model_name,
        system_prompt=None, workflow_id=workflow_id,
    )


def _agent_config(agent_id: int, model_name: str | None) -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        agent_id=agent_id, system_prompt="你是审稿人", model_name=model_name,
        tool_runtime_names=("echo",), skill_ids=(), skill_names=(), name=f"成员{agent_id}",
        description="负责审稿",
    )


class _FakeAgentDefinitionService:
    def __init__(self, db: object) -> None:
        pass

    async def list_delegate_catalog(self, workspace_id: int) -> tuple[()]:
        return ()

    async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
        return _agent_config(agent_id, "agent-model")


def _world(monkeypatch: pytest.MonkeyPatch, world: str) -> None:
    _apply(monkeypatch, _WORLD_SETTINGS[world])
    _use_endpoint(monkeypatch, _WORLD_ENDPOINTS[world])
    monkeypatch.setattr("runtime.turn_config.AgentDefinitionService", _FakeAgentDefinitionService)
    monkeypatch.setattr("runtime.lead_loop.SkillService.get_catalog", AsyncMock(return_value=""))
    # 环境段含工作目录与日期，固定下来，避免系统提示随运行目录或日期变化。
    monkeypatch.setattr("runtime.lead_loop.date", _FixedDate)
    monkeypatch.setattr("runtime.lead_loop.Path", SimpleNamespace(cwd=lambda: "/srv"))


class _FixedDate(date):
    @classmethod
    def today(cls) -> _FixedDate:
        return cls(2026, 1, 1)


async def _configured_agent(session: Any) -> AgentRuntime:
    """经真实 __init__ 与 _configure_agent 解析主 Agent 与压缩客户端，再换掉数据库依赖。"""
    runtime = AgentRuntime(object(), user_id=3, workspace_id=2)
    await runtime._configure_agent(session)
    runtime.configuration = replace(runtime.configuration, tools=ToolSet([EchoTool()]))
    runtime.bus = SimpleNamespace(emit=AsyncMock())
    runtime.sessions = SimpleNamespace(
        load_context_for_turn=AsyncMock(return_value=([], [_USER], 1)),
        add_message=AsyncMock(return_value=SimpleNamespace(id=99)),
    )
    runtime.memory = SimpleNamespace(recall=AsyncMock(return_value=None))
    runtime.plan_service = SimpleNamespace(get_active=AsyncMock(return_value=None))
    runtime._apply_stop_hooks = AsyncMock(return_value=None)
    return runtime


def _agent_case(world: str, effort: str, model_name: str | None, agent_id: int | None):
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        _world(monkeypatch, world)
        session = _session(effort, model_name=model_name)
        session.agent_id = agent_id
        runtime = await _configured_agent(session)
        turn = SimpleNamespace(id=_TURN_ID, session_id=1, user_id=3, workspace_id=2)
        await runtime._run_llm_loop(1, session, turn, None)
        attribution = runtime.sessions.add_message.await_args.kwargs
        return {"provider": attribution["provider"], "model": attribution["model"]}

    return run


def _compact_case(world: str):
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        _world(monkeypatch, world)
        runtime = await _configured_agent(_session("max"))
        compacted = await runtime.compact_service.summarize([_USER])
        return {"threshold": runtime.compact_service.threshold, "summary": compacted}

    return run


def _subagent_case(world: str, effort: str, agent_type: AgentType, spec_model: str | None):
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
        _world(monkeypatch, world)
        monkeypatch.setattr(
            SessionService, "runtime_model_options", AsyncMock(return_value=_session(effort))
        )
        spec = get_subagent_spec(agent_type)
        if spec_model is not None:
            spec = replace(spec, model=spec_model)
        runner = SubAgentRunner(object())
        settings = await runner._configure_model(1, spec, 2, 3)
        assembler = TurnEventAssembler(ORCHESTRATOR_ACTOR, 1, turn_id=_TURN_ID)
        role = SubAgent(spec=replace(spec, system_prompt="子代理系统提示", critical_reminder=""))
        # 子代理循环每轮的模型调用：角色组装 Context，经 AgentStep 发起流式调用。
        context = role.context(messages_from_rows([_USER]), ToolSet([EchoTool()]).to_specs())
        await AgentStep(runner.llm, assembler, runner._emit_event).run(context, settings)

    return run


def _workflow_deps() -> dict[str, Any]:
    return {
        "db": object(), "bus": SimpleNamespace(emit=AsyncMock()), "user_id": 3,
        "workspace_id": 2, "session_id": 1, "turn_id": _TURN_ID, "workflow_id": 8,
        "members": (),
    }


async def _workflow_model(world: str, effort: str) -> dict[str, Any]:
    """WorkflowRuntime._build_orchestrator 与准入阶段共用的会话级解析。"""
    session = _session(effort, workflow_id=8)
    resolved = await DatabaseModelResolver(object()).for_session(session, "workflow")
    return {"llm": resolved.client, "model_settings": resolved.settings}


def _workflow_case(world: str, effort: str, mode: str):
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any] | None:
        _world(monkeypatch, world)
        model = await _workflow_model(world, effort)
        if mode == "merge":
            result = await ParallelOrchestrator(**model, **_workflow_deps())._merge(
                "用户提问", [("甲", "结论一"), ("乙", "结论二")]
            )
            return {"provider": result.provider, "model": result.model}
        sdk.answers[:] = [{"agent_id": 1}]
        router = LlmRoutingOrchestrator(**model, **_workflow_deps())
        await router._route("用户提问", [_agent_config(1, None), _agent_config(2, None)])
        return None

    return run


def _selector_case(world: str):
    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        _world(monkeypatch, world)
        sdk.answers[:] = [{"selected_memory_ids": [1]}]
        entry = MemoryCatalogEntry(
            id=1, version=1, memory_key="k", memory_type="user", name="偏好", description="简洁"
        )
        selection = await MemoryRecallService(object())._select_memory_ids(
            2, 3, (entry,), ["你好"]
        )
        return {"memory_ids": list(selection.memory_ids), "reason": selection.degraded_reason}

    return run


def _job(job_type: str, model: str | None, provider_id: str | None) -> SimpleNamespace:
    payload = {} if provider_id is None else {"model_provider_id": provider_id}
    return SimpleNamespace(
        id=_JOB_ID, job_type=job_type, space_id=5, session_id=1, turn_id=_TURN_ID,
        model=model, prompt_version="v1", payload=payload,
    )


def _extraction_service(job: SimpleNamespace) -> MemoryExtractionService:
    """prepare() 只读仓储，换成返回固定来源的替身。"""
    service = MemoryExtractionService(object())
    session = SimpleNamespace(id=1, user_id=3, workspace_id=2)
    turn = SimpleNamespace(
        id=_TURN_ID, session_id=1, user_id=3, workspace_id=2, status="completed",
        started_message_id=10, completed_message_id=11,
    )
    messages = [
        SimpleNamespace(id=10, session_id=1, turn_id=_TURN_ID, role="user", content="记住我爱喝茶"),
        SimpleNamespace(
            id=11, session_id=1, turn_id=_TURN_ID, role="assistant",
            content=[{"kind": "text", "text": "好的"}],
        ),
    ]
    service.job_repo = SimpleNamespace(get_owned_lease=AsyncMock(return_value=job))
    service.session_repo = SimpleNamespace(
        get=AsyncMock(return_value=session),
        get_turn=AsyncMock(return_value=turn),
        list_messages_for_extraction=AsyncMock(return_value=messages),
    )
    snapshot = SimpleNamespace(space_id=5, catalog_version=3, entries=())
    service.memory_repo = SimpleNamespace(get_catalog_snapshot=AsyncMock(return_value=snapshot))
    return service


def _dream_service(job: SimpleNamespace) -> MemoryDreamService:
    service = MemoryDreamService(object())
    space = SimpleNamespace(id=5, workspace_id=2, user_id=3, catalog_version=4)
    entry = SimpleNamespace(
        item_id=1, revision_id=7, version=1, memory_key="tea", memory_type="user",
        name="喝茶", description="偏好", body="爱喝茶", source_kind="extract",
        last_used_at=None, use_count=0,
    )
    service.job_repo = SimpleNamespace(
        require_owned_lease=AsyncMock(return_value=job), update_dream_snapshot=AsyncMock()
    )
    service.memory_repo = SimpleNamespace(
        get_space_for_worker=AsyncMock(return_value=space),
        list_active_dream_entries=AsyncMock(return_value=[entry]),
    )
    return service


_EXTRACTION_ANSWER = {
    "memories": [
        {
            "memory_key": "tea", "type": "user", "name": "喝茶", "description": "偏好",
            "body": "爱喝茶", "source_message_ids": [10],
        }
    ]
}


def _memory_job_case(world: str, job_type: str, model: str | None, provider_id: str | None):
    """入队冻结的模型名与 provider 决定 Job 执行时的端点与请求模型。"""

    async def run(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        _world(monkeypatch, world)
        job = _job(job_type, model, provider_id)
        if job_type == "extract":
            sdk.answers[:] = [_EXTRACTION_ANSWER]
            prepared = await _extraction_service(job).prepare(_JOB_ID, "worker")
            await MemoryExtractionService.extract_with_llm(prepared)
        else:
            sdk.answers[:] = [{"operations": []}]
            prepared = await _dream_service(job).prepare(_JOB_ID, "worker")
            await MemoryDreamService.complete_with_llm(prepared)
        return {"prepared_model": prepared.model}

    return run


for _world_id in _WORLD_ENDPOINTS:
    for _effort in ("fast", "max"):
        _register(f"site/agent/{_world_id}/{_effort}", _agent_case(_world_id, _effort, None, None))
    _register(f"site/agent_session_model/{_world_id}", _agent_case(_world_id, "medium", "sess-m", None))
    _register(f"site/agent_bound/{_world_id}", _agent_case(_world_id, "max", "sess-m", 7))
    _register(f"site/compact/{_world_id}", _compact_case(_world_id))
    for _agent_type, _spec_model in (
        (AgentType.GENERAL_PURPOSE, None),
        (AgentType.EXPLORE, None),
        (AgentType.GENERAL_PURPOSE, "spec-model"),
    ):
        _name = _agent_type.value if _spec_model is None else f"{_agent_type.value}_fixed"
        _register(
            f"site/subagent/{_name}/{_world_id}",
            _subagent_case(_world_id, "max", _agent_type, _spec_model),
        )
    _register(f"site/subagent_fast_effort/{_world_id}", _subagent_case(
        _world_id, "fast", AgentType.GENERAL_PURPOSE, None
    ))
    _register(f"site/workflow_merge/{_world_id}", _workflow_case(_world_id, "max", "merge"))
    _register(f"site/workflow_route/{_world_id}", _workflow_case(_world_id, "fast", "route"))
    _register(f"site/memory_selector/{_world_id}", _selector_case(_world_id))
    for _job_type in ("extract", "dream"):
        for _frozen, _model, _provider_id in (
            ("legacy_env", "frozen-env-model", None),
            ("frozen_provider", "db-model", "provider-x"),
            ("unfrozen", None, None),
        ):
            _register(
                f"site/memory_{_job_type}/{_frozen}/{_world_id}",
                _memory_job_case(_world_id, _job_type, _model, _provider_id),
            )


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


async def _observe(case_id: str, sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    try:
        extra = await _CASES[case_id](sdk, monkeypatch)
    except Exception as exc:  # noqa: BLE001  异常本身就是要固化的观测
        return _outcome(sdk, exc)
    outcome = _outcome(sdk)
    if extra is not None:
        outcome["result"] = _normalize(extra)
    return outcome


@pytest.mark.parametrize("case_id", sorted(_CASES))
async def test_model_request_matches_golden(
    case_id: str, sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个组合发往 SDK 的参数（含键序）与重构前代码产出的字面量完全一致。"""
    actual = await _observe(case_id, sdk, monkeypatch)
    assert case_id in EXPECTED, f"组合 {case_id} 缺少黄金字面量"
    assert actual == EXPECTED[case_id]
    assert _ordered(actual) == _ordered(EXPECTED[case_id]), "取值相同但字典键序不同"


def test_golden_literals_cover_exactly_the_matrix() -> None:
    """字面量与组合一一对应：不允许悄悄删组合，也不允许留下无人校验的字面量。"""
    assert sorted(EXPECTED) == sorted(_CASES)
