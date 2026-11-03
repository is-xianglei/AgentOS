"""LLM SDK 异常翻译为中立异常,以及 Memory Job 对中立异常的失败分类。

回归点:OpenAI SDK 异常与 Anthropic 的没有继承关系,翻译前 dream/extraction
只认 anthropic 异常,openai 的超时、连接失败、429、5xx 被标成不可重试。
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock
from uuid import uuid4

import anthropic
import httpx
import httpx2
import openai
import pytest
from anthropic._exceptions import (
    DeadlineExceededError,
    OverloadedError,
    RequestTooLargeError,
    ServiceUnavailableError,
)
from pydantic import BaseModel

from core.errors import AgentException
from llm import anthropic_client as anthropic_module
from llm import openai_client as openai_module
from llm.anthropic_client import AnthropicClient
from llm.errors import LLMCallError, LLMConnectionError, LLMStatusError
from llm.openai_client import OpenAIClient
from llm.types import LLMEventKind, LLMStreamEvent
from memory.jobs.dream import MemoryDreamFailure, MemoryDreamService, PreparedDream
from memory.jobs.extraction import (
    MemoryExtractionFailure,
    MemoryExtractionService,
    PreparedMemoryExtraction,
)
from memory.recall.service import MemoryRecallService
from memory.repository import MemoryCatalogEntry
from model_service.service import ModelService

pytestmark = pytest.mark.anyio

# openai 3.x 基于 httpx2,anthropic 基于 httpx,SDK 异常须用各自的 Request/Response 构造。
_ANTHROPIC_REQUEST = httpx.Request("POST", "https://anthropic.test/v1/messages")
_OPENAI_REQUEST = httpx2.Request("POST", "https://openai.test/v1/chat/completions")


def _anthropic_response(status: int) -> httpx.Response:
    return httpx.Response(status, request=_ANTHROPIC_REQUEST)


def _openai_response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=_OPENAI_REQUEST)


# 每类故障在两家 SDK 下的真实异常(与各家 _make_status_error 按状态码选的子类一致)。
_ANTHROPIC_ERRORS = {
    "timeout": lambda: anthropic.APITimeoutError(request=_ANTHROPIC_REQUEST),
    "connection": lambda: anthropic.APIConnectionError(request=_ANTHROPIC_REQUEST),
    "rate_limit": lambda: anthropic.RateLimitError(
        "Error code: 429", response=_anthropic_response(429), body=None
    ),
    "server_503": lambda: anthropic.InternalServerError(
        "Error code: 503", response=_anthropic_response(503), body=None
    ),
    "overloaded_529": lambda: OverloadedError(
        "Error code: 529", response=_anthropic_response(529), body=None
    ),
    "bad_request_400": lambda: anthropic.BadRequestError(
        "Error code: 400", response=_anthropic_response(400), body=None
    ),
    "response_invalid": lambda: anthropic.APIResponseValidationError(
        response=_anthropic_response(200), body=None
    ),
    "api_error": lambda: anthropic.APIError("upstream error", _ANTHROPIC_REQUEST, body=None),
}
_OPENAI_ERRORS = {
    "timeout": lambda: openai.APITimeoutError(request=_OPENAI_REQUEST),
    "connection": lambda: openai.APIConnectionError(request=_OPENAI_REQUEST),
    "rate_limit": lambda: openai.RateLimitError(
        "Error code: 429", response=_openai_response(429), body=None
    ),
    "server_503": lambda: openai.InternalServerError(
        "Error code: 503", response=_openai_response(503), body=None
    ),
    "overloaded_529": lambda: openai.InternalServerError(
        "Error code: 529", response=_openai_response(529), body=None
    ),
    "bad_request_400": lambda: openai.BadRequestError(
        "Error code: 400", response=_openai_response(400), body=None
    ),
    "response_invalid": lambda: openai.APIResponseValidationError(
        response=_openai_response(200), body=None
    ),
    "api_error": lambda: openai.APIError("upstream error", _OPENAI_REQUEST, body=None),
}
_SDK_ERRORS = {"anthropic": _ANTHROPIC_ERRORS, "openai": _OPENAI_ERRORS}

# 修复前 anthropic 的分类即基准:openai 必须得到完全相同的错误码与 retryable。
# 429 先落「不可用」分支,错误码是 *_unavailable 而非 *_http_429。
_CLASSIFICATION = [
    pytest.param("timeout", "dream_unavailable", "extractor_unavailable", True, id="timeout"),
    pytest.param(
        "connection", "dream_unavailable", "extractor_unavailable", True, id="connection"
    ),
    pytest.param(
        "rate_limit", "dream_unavailable", "extractor_unavailable", True, id="rate_limit"
    ),
    pytest.param("server_503", "dream_http_503", "extractor_http_503", True, id="503"),
    pytest.param("overloaded_529", "dream_http_529", "extractor_http_529", True, id="529"),
    pytest.param("bad_request_400", "dream_http_400", "extractor_http_400", False, id="400"),
    pytest.param("response_invalid", "dream_error", "extractor_error", False, id="invalid"),
    pytest.param("api_error", "dream_error", "extractor_error", False, id="api_error"),
]
_PROVIDERS = ["anthropic", "openai"]


def _structured_client_raising(monkeypatch, provider: str, exc: Exception):
    """构造真实客户端,把其结构化输出的 SDK 调用换成抛出 exc 的替身(思考关闭)。"""
    if provider == "anthropic":
        create = AsyncMock(side_effect=exc)
        monkeypatch.setattr(
            anthropic_module,
            "AsyncAnthropic",
            lambda **_: SimpleNamespace(messages=SimpleNamespace(create=create)),
        )
        return AnthropicClient(model="model-a", api_key="test", thinking_enabled=False)
    parse = AsyncMock(side_effect=exc)
    monkeypatch.setattr(
        openai_module,
        "AsyncOpenAI",
        lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))),
    )
    return OpenAIClient(
        model="model-a", api_key="test", thinking_enabled=False, thinking_protocol="openai"
    )


def _prepared_dream(client) -> PreparedDream:
    return PreparedDream(
        job_id=uuid4(),
        space_id=1,
        user_id=1,
        workspace_id=1,
        model="model-a",
        prompt_version="v1",
        base_catalog_version=0,
        entries=(),
        client=client,
    )


def _prepared_extraction(client) -> PreparedMemoryExtraction:
    return PreparedMemoryExtraction(
        job_id=uuid4(),
        space_id=1,
        session_id=1,
        turn_id=uuid4(),
        user_id=1,
        workspace_id=1,
        model="model-a",
        prompt_version="v1",
        mode="active",
        catalog_version=0,
        catalog_entries=(),
        messages=(),
        client=client,
    )


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize(("kind", "dream_code", "extractor_code", "retryable"), _CLASSIFICATION)
async def test_dream_classifies_sdk_errors_same_for_both_providers(
    monkeypatch, provider, kind, dream_code, extractor_code, retryable
):
    sdk_exc = _SDK_ERRORS[provider][kind]()
    client = _structured_client_raising(monkeypatch, provider, sdk_exc)

    with pytest.raises(MemoryDreamFailure) as caught:
        await MemoryDreamService.complete_with_llm(_prepared_dream(client))

    assert (caught.value.code, caught.value.retryable) == (dream_code, retryable)
    # Job 记录的 last_error_message 取自 str(failure),须仍是原始 SDK 异常类名。
    assert str(caught.value) == type(sdk_exc).__name__


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize(("kind", "dream_code", "extractor_code", "retryable"), _CLASSIFICATION)
async def test_extraction_classifies_sdk_errors_same_for_both_providers(
    monkeypatch, provider, kind, dream_code, extractor_code, retryable
):
    sdk_exc = _SDK_ERRORS[provider][kind]()
    client = _structured_client_raising(monkeypatch, provider, sdk_exc)

    with pytest.raises(MemoryExtractionFailure) as caught:
        await MemoryExtractionService.extract_with_llm(_prepared_extraction(client))

    assert (caught.value.code, caught.value.retryable) == (extractor_code, retryable)
    assert str(caught.value) == type(sdk_exc).__name__


# 每类 SDK 异常应转成的中立异常;状态码为 None 表示该中立类型不携带状态码。
_NEUTRAL = {
    "timeout": (LLMConnectionError, None),
    "connection": (LLMConnectionError, None),
    "rate_limit": (LLMStatusError, 429),
    "server_503": (LLMStatusError, 503),
    "overloaded_529": (LLMStatusError, 529),
    "bad_request_400": (LLMStatusError, 400),
    # 非状态错误,但 SDK 带了响应状态码,原样带上供诊断展示。
    "response_invalid": (LLMCallError, 200),
    "api_error": (LLMCallError, None),
}
# 各 SDK 调用边界:结构化输出(Anthropic 思考开启时走 messages.stream)、建流即失败、流中途失败。
_CALL_PATHS = [
    ("anthropic", "structured"),
    ("anthropic", "structured_thinking"),
    ("anthropic", "stream_open"),
    ("anthropic", "stream_mid"),
    ("openai", "structured"),
    ("openai", "stream_open"),
    ("openai", "stream_mid"),
]


class _Answer(BaseModel):
    answer: int


class _FailingStream:
    """SDK 流替身:open_fails 时进入上下文即抛,否则先产出 first_chunk 再抛。"""

    def __init__(self, exc: BaseException, *, open_fails: bool, first_chunk: Any) -> None:
        self._exc = exc
        self._open_fails = open_fails
        self._first_chunk = first_chunk

    async def __aenter__(self) -> Self:
        if self._open_fails:
            raise self._exc
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def __aiter__(self):
        yield self._first_chunk
        raise self._exc


async def _invoke(monkeypatch, provider: str, path: str, exc: BaseException) -> list[Any]:
    """让指定 provider 的指定 SDK 调用边界抛出 exc,并经真实客户端发起调用。"""
    if provider == "anthropic":
        return await _invoke_anthropic(monkeypatch, path, exc)
    return await _invoke_openai(monkeypatch, path, exc)


async def _invoke_anthropic(monkeypatch, path: str, exc: BaseException) -> list[Any]:
    if path in {"structured", "structured_thinking"}:
        create = AsyncMock(side_effect=exc)

        @asynccontextmanager
        async def stream(**_: Any):
            # 思考开启时 complete_structured 走 messages.stream + get_final_message。
            yield SimpleNamespace(get_final_message=AsyncMock(side_effect=exc))

        messages = SimpleNamespace(create=create, stream=stream)
        monkeypatch.setattr(
            anthropic_module, "AsyncAnthropic", lambda **_: SimpleNamespace(messages=messages)
        )
        thinking = {"thinking_enabled": True, "reasoning_effort": "high"}
        if path == "structured":
            thinking = {"thinking_enabled": False}
        client = AnthropicClient(model="model-a", api_key="test", **thinking)
        return [await client.complete_structured([], _Answer)]

    client = _streaming_client(monkeypatch, "anthropic", exc, open_fails=path == "stream_open")
    return [event async for event in client.stream([{"role": "user", "content": "hi"}])]


async def _invoke_openai(monkeypatch, path: str, exc: BaseException) -> list[Any]:
    if path != "structured":
        client = _streaming_client(monkeypatch, "openai", exc, open_fails=path == "stream_open")
        return [event async for event in client.stream([{"role": "user", "content": "hi"}])]
    completions = SimpleNamespace(parse=AsyncMock(side_effect=exc))
    monkeypatch.setattr(
        openai_module,
        "AsyncOpenAI",
        lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    client = OpenAIClient(
        model="model-a", api_key="test", thinking_enabled=False, thinking_protocol="openai"
    )
    return [await client.complete_structured([], _Answer)]


def _streaming_client(monkeypatch, provider: str, exc: BaseException, *, open_fails: bool):
    """装配一个流式 SDK 替身并返回真实客户端;未在建流时失败则先下发一个合法 chunk。"""
    if provider == "anthropic":
        first_chunk = SimpleNamespace(
            model_dump=lambda **_: {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        )
        fake_stream = _FailingStream(exc, open_fails=open_fails, first_chunk=first_chunk)
        messages = SimpleNamespace(stream=lambda **_: fake_stream)
        monkeypatch.setattr(
            anthropic_module, "AsyncAnthropic", lambda **_: SimpleNamespace(messages=messages)
        )
        return AnthropicClient(model="model-a", api_key="test", thinking_enabled=False)

    first_chunk = {"choices": [{"delta": {"content": "半截"}, "finish_reason": None}]}
    fake_stream = _FailingStream(exc, open_fails=open_fails, first_chunk=first_chunk)
    completions = SimpleNamespace(create=AsyncMock(return_value=fake_stream))
    monkeypatch.setattr(
        openai_module,
        "AsyncOpenAI",
        lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    return OpenAIClient(
        model="model-a", api_key="test", thinking_enabled=False, thinking_protocol="openai"
    )


@pytest.mark.parametrize("kind", list(_NEUTRAL))
@pytest.mark.parametrize(("provider", "path"), _CALL_PATHS)
async def test_sdk_errors_translate_to_neutral_errors(monkeypatch, provider, path, kind):
    sdk_exc = _SDK_ERRORS[provider][kind]()
    neutral_type, status_code = _NEUTRAL[kind]

    with pytest.raises(LLMCallError) as caught:
        await _invoke(monkeypatch, provider, path, sdk_exc)

    error = caught.value
    assert type(error) is neutral_type
    assert error.__cause__ is sdk_exc
    assert error.sdk_error_type == type(sdk_exc).__name__
    # 消息原样沿用 SDK 文本,SSE 错误事件与日志中的 str(exc) 不因转换而变化。
    assert str(error) == str(sdk_exc)
    assert getattr(error, "status_code", None) == status_code
    assert not isinstance(error, AgentException)


@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_stream_mid_failure_keeps_events_before_error(monkeypatch, provider):
    """流中途失败:失败前已产出的事件照常交给消费方,随后才抛中立异常。"""
    sdk_exc = _SDK_ERRORS[provider]["timeout"]()
    client = _streaming_client(monkeypatch, provider, sdk_exc, open_fails=False)
    received: list[LLMStreamEvent] = []

    with pytest.raises(LLMConnectionError) as caught:
        async for event in client.stream([{"role": "user", "content": "hi"}]):
            received.append(event)

    assert caught.value.__cause__ is sdk_exc
    assert received and received[0].kind is LLMEventKind.BLOCK_START


def _transport_timeout(provider: str) -> Exception:
    """SDK 未包装的底层 HTTP 读超时(各家 SDK 使用各自的 httpx 发行版)。"""
    if provider == "anthropic":
        return httpx.ReadTimeout("read timeout", request=_ANTHROPIC_REQUEST)
    return httpx2.ReadTimeout("read timeout", request=_OPENAI_REQUEST)


_PASSTHROUGH = {
    "builtin_timeout": lambda _: TimeoutError(),
    "value_error": lambda _: ValueError("bad output"),
    "cancelled": lambda _: asyncio.CancelledError(),
    "httpx_read_timeout": _transport_timeout,
}


@pytest.mark.parametrize("kind", list(_PASSTHROUGH))
@pytest.mark.parametrize(("provider", "path"), _CALL_PATHS)
async def test_non_sdk_errors_are_not_translated(monkeypatch, provider, path, kind):
    exc = _PASSTHROUGH[kind](provider)

    with pytest.raises(type(exc)) as caught:
        await _invoke(monkeypatch, provider, path, exc)

    assert caught.value is exc
    assert not isinstance(caught.value, LLMCallError)


class _RaisingLLM:
    """只认中立协议的 LLM 替身:complete_structured 直接抛出给定异常。"""

    provider = "openai"
    model = "model-a"

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def complete_structured(self, *_: Any, **__: Any) -> Any:
        raise self._exc


def _status(code: int, sdk_type: str = "APIStatusError") -> LLMStatusError:
    return LLMStatusError(f"Error code: {code}", sdk_error_type=sdk_type, status_code=code)


# (中立异常工厂, dream 失败码, extractor 失败码, retryable, Job 记录的消息)。
_NEUTRAL_CLASSIFICATION = [
    pytest.param(
        lambda: LLMConnectionError("timed out", sdk_error_type="APITimeoutError"),
        "dream_unavailable", "extractor_unavailable", True, "APITimeoutError",
        id="connection",
    ),
    pytest.param(
        lambda: _status(429, "RateLimitError"),
        "dream_unavailable", "extractor_unavailable", True, "RateLimitError",
        id="status_429",
    ),
    pytest.param(
        lambda: _status(500, "InternalServerError"),
        "dream_http_500", "extractor_http_500", True, "InternalServerError",
        id="status_500",
    ),
    pytest.param(
        lambda: _status(404, "NotFoundError"),
        "dream_http_404", "extractor_http_404", False, "NotFoundError",
        id="status_404",
    ),
    pytest.param(
        lambda: _status(200),
        "dream_http_200", "extractor_http_200", False, "APIStatusError",
        id="stream_error_200",
    ),
    pytest.param(
        lambda: LLMCallError("length", sdk_error_type="LengthFinishReasonError"),
        "dream_error", "extractor_error", False, "LengthFinishReasonError",
        id="call_error",
    ),
    pytest.param(
        TimeoutError, "dream_unavailable", "extractor_unavailable", True, "TimeoutError",
        id="builtin_timeout",
    ),
    pytest.param(
        lambda: ValueError("结构化输出缺失"),
        "invalid_dream_output", "invalid_extractor_output", False, "ValueError",
        id="value_error",
    ),
    pytest.param(
        lambda: RuntimeError("boom"),
        "dream_error", "extractor_error", False, "RuntimeError",
        id="other",
    ),
]


@pytest.mark.parametrize(
    ("make_exc", "dream_code", "extractor_code", "retryable", "message"),
    _NEUTRAL_CLASSIFICATION,
)
async def test_memory_jobs_classify_neutral_errors(
    make_exc, dream_code, extractor_code, retryable, message
):
    llm = _RaisingLLM(make_exc())
    with pytest.raises(MemoryDreamFailure) as dream_failure:
        await MemoryDreamService.complete_with_llm(_prepared_dream(None), llm)
    with pytest.raises(MemoryExtractionFailure) as extraction_failure:
        await MemoryExtractionService.extract_with_llm(_prepared_extraction(None), llm)

    dream, extraction = dream_failure.value, extraction_failure.value
    assert (dream.code, dream.retryable, str(dream)) == (dream_code, retryable, message)
    assert (extraction.code, extraction.retryable, str(extraction)) == (
        extractor_code,
        retryable,
        message,
    )


def _a_status(cls: type, status: int):
    return lambda: cls(f"Error code: {status}", response=_anthropic_response(status), body=None)


def _o_status(cls: type, status: int):
    return lambda: cls(f"Error code: {status}", response=_openai_response(status), body=None)


# 两家 SDK 可能从 stream()/complete_structured() 抛出的全部异常类(按 _make_status_error 取状态码)。
_EVERY_SDK_ERROR = [
    *(
        pytest.param("anthropic", factory, id=f"anthropic-{name}")
        for name, factory in {
            "APITimeoutError": _ANTHROPIC_ERRORS["timeout"],
            "APIConnectionError": _ANTHROPIC_ERRORS["connection"],
            "BadRequestError": _a_status(anthropic.BadRequestError, 400),
            "AuthenticationError": _a_status(anthropic.AuthenticationError, 401),
            "PermissionDeniedError": _a_status(anthropic.PermissionDeniedError, 403),
            "NotFoundError": _a_status(anthropic.NotFoundError, 404),
            "ConflictError": _a_status(anthropic.ConflictError, 409),
            "RequestTooLargeError": _a_status(RequestTooLargeError, 413),
            "UnprocessableEntityError": _a_status(anthropic.UnprocessableEntityError, 422),
            "RateLimitError": _a_status(anthropic.RateLimitError, 429),
            "InternalServerError": _a_status(anthropic.InternalServerError, 500),
            "ServiceUnavailableError": _a_status(ServiceUnavailableError, 503),
            "DeadlineExceededError": _a_status(DeadlineExceededError, 504),
            "OverloadedError": _a_status(OverloadedError, 529),
            "APIStatusError-402": _a_status(anthropic.APIStatusError, 402),
            "APIStatusError-stream-200": _a_status(anthropic.APIStatusError, 200),
            "APIResponseValidationError": _ANTHROPIC_ERRORS["response_invalid"],
            "APIError": _ANTHROPIC_ERRORS["api_error"],
            "WorkloadIdentityError": lambda: anthropic.WorkloadIdentityError(
                "token exchange failed", status_code=401
            ),
            "AnthropicError": lambda: anthropic.AnthropicError("sdk error"),
        }.items()
    ),
    *(
        pytest.param("openai", factory, id=f"openai-{name}")
        for name, factory in {
            "APITimeoutError": _OPENAI_ERRORS["timeout"],
            "APIConnectionError": _OPENAI_ERRORS["connection"],
            "BadRequestError": _o_status(openai.BadRequestError, 400),
            "AuthenticationError": _o_status(openai.AuthenticationError, 401),
            "OAuthError": lambda: openai.OAuthError(response=_openai_response(401), body=None),
            "PermissionDeniedError": _o_status(openai.PermissionDeniedError, 403),
            "NotFoundError": _o_status(openai.NotFoundError, 404),
            "ConflictError": _o_status(openai.ConflictError, 409),
            "UnprocessableEntityError": _o_status(openai.UnprocessableEntityError, 422),
            "RateLimitError": _o_status(openai.RateLimitError, 429),
            "InternalServerError": _o_status(openai.InternalServerError, 500),
            "InternalServerError-529": _o_status(openai.InternalServerError, 529),
            "APIStatusError-402": _o_status(openai.APIStatusError, 402),
            "APIResponseValidationError": _OPENAI_ERRORS["response_invalid"],
            "APIError-stream": _OPENAI_ERRORS["api_error"],
            "LengthFinishReasonError": lambda: openai.LengthFinishReasonError(
                completion=SimpleNamespace(usage=None)
            ),
            "ContentFilterFinishReasonError": openai.ContentFilterFinishReasonError,
            "OpenAIError": lambda: openai.OpenAIError("sdk error"),
        }.items()
    ),
]
_SDK_MODULES = {"anthropic": anthropic, "openai": openai}


def _legacy_ping_output(exc: Exception) -> tuple[str, str, int | None]:
    """修复前(HEAD 581ed3f)model_service ping 对原始 SDK 异常的输出,逐行照搬旧实现。"""
    status = getattr(exc, "status_code", None)
    error = f"上游 HTTP {status}" if status else f"无法连接模型服务（{type(exc).__name__}）"
    return error, type(exc).__name__, status


def _legacy_degraded_reason(exc: Exception) -> str:
    """修复前 memory/recall/service.py 对原始 SDK 异常记录的降级原因。"""
    return f"selector_error:{type(exc).__name__}"[:200]


def _legacy_job_classification(provider: str, exc: Exception) -> tuple[str, bool]:
    """修复前 dream/extraction 的分类式(返回失败码后缀与 retryable)。

    anthropic 即 HEAD 的原样逻辑;openai 套用同名的 openai 异常类,即本次修复要达到的
    「与 anthropic 相同的分类」。
    """
    sdk = _SDK_MODULES[provider]
    if isinstance(exc, (sdk.APITimeoutError, sdk.APIConnectionError, sdk.RateLimitError)):
        return "unavailable", True
    if isinstance(exc, sdk.APIStatusError):
        return f"http_{exc.status_code}", exc.status_code == 429 or exc.status_code >= 500
    return "error", False


@pytest.mark.parametrize(("provider", "make_exc"), _EVERY_SDK_ERROR)
async def test_model_ping_output_unchanged_for_every_sdk_error(
    monkeypatch, caplog, provider, make_exc
):
    sdk_exc = make_exc()
    client = _streaming_client(monkeypatch, provider, sdk_exc, open_fails=False)
    monkeypatch.setattr(ModelService, "build_client", staticmethod(lambda *_: client))
    expected_error, expected_type, expected_status = _legacy_ping_output(sdk_exc)

    with caplog.at_level("WARNING", logger="model_service.service"):
        result = await ModelService(None).ping(SimpleNamespace(provider_type="chat"))

    assert result.success is False
    assert result.error == expected_error
    (record,) = [r for r in caplog.records if r.getMessage() == "模型服务测试失败"]
    assert (record.exception_type, record.upstream_status) == (expected_type, expected_status)


@pytest.mark.parametrize(("provider", "make_exc"), _EVERY_SDK_ERROR)
async def test_recall_degraded_reason_unchanged_for_every_sdk_error(
    monkeypatch, provider, make_exc
):
    sdk_exc = make_exc()
    client = _structured_client_raising(monkeypatch, provider, sdk_exc)
    monkeypatch.setattr(MemoryRecallService, "_fallback_select", AsyncMock(return_value=[]))
    entry = MemoryCatalogEntry(
        id=1, version=1, memory_key="k", memory_type="user", name="n", description="d"
    )

    selection = await MemoryRecallService(None, client)._select_memory_ids(
        1, 1, (entry,), ["你好"]
    )

    assert selection.degraded_reason == _legacy_degraded_reason(sdk_exc)


@pytest.mark.parametrize(("provider", "make_exc"), _EVERY_SDK_ERROR)
async def test_memory_job_classification_matches_legacy_for_every_sdk_error(
    monkeypatch, provider, make_exc
):
    sdk_exc = make_exc()
    suffix, retryable = _legacy_job_classification(provider, sdk_exc)
    client = _structured_client_raising(monkeypatch, provider, sdk_exc)

    with pytest.raises(MemoryDreamFailure) as dream_failure:
        await MemoryDreamService.complete_with_llm(_prepared_dream(client))
    with pytest.raises(MemoryExtractionFailure) as extraction_failure:
        await MemoryExtractionService.extract_with_llm(_prepared_extraction(client))

    dream, extraction = dream_failure.value, extraction_failure.value
    message = type(sdk_exc).__name__
    assert (dream.code, dream.retryable, str(dream)) == (f"dream_{suffix}", retryable, message)
    assert (extraction.code, extraction.retryable, str(extraction)) == (
        f"extractor_{suffix}",
        retryable,
        message,
    )

