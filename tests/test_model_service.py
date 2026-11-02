"""模型服务的配置、探测与密钥保留契约。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

import database.registry  # noqa: F401  测试直接构造 ORM 前注册完整映射
from core.errors import AgentException
from model_service.models import ModelProviderRecord
from model_service.policy import fast_capabilities, normalize_effort, validate_reasoning_config
from model_service.schemas import ModelProbeRequest, ModelProviderInput, ModelProviderUpdate
from model_service.service import ModelService, public_provider

pytestmark = pytest.mark.anyio


def provider(**overrides):
    values = dict(
        provider_id="model-a", workspace_id=1, display_name="测试模型",
        provider_type="chat", provider="openai_compatible", base_url="https://test.local/v1",
        api_key="private-test-key", model_name="test", extra_config={}, is_active=True,
        is_deleted=False,
    )
    return ModelProviderRecord(**(values | overrides))


def service(record=None):
    instance = ModelService(SimpleNamespace())
    instance.repo = SimpleNamespace(
        get_provider=AsyncMock(return_value=record), list_roles=AsyncMock(return_value=[]),
        save=AsyncMock(), delete=AsyncMock(), list_providers=AsyncMock(return_value=[]),
    )
    instance.ping = AsyncMock(return_value=SimpleNamespace(success=True, error=None))
    return instance


def test_schema_rejects_whitespace_and_partial_null():
    with pytest.raises(ValidationError):
        ModelProviderInput(display_name=" ", provider="openai", model_name="x")
    with pytest.raises(ValidationError):
        ModelProviderUpdate(is_active=None)
    assert ModelProviderUpdate(api_key=None).model_fields_set == {"api_key"}


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_partial_update_preserves_key_and_does_not_retest_display_name(key):
    record = provider()
    instance = service(record)
    result = await instance.save_provider(
        1, ModelProviderUpdate(display_name="新名称", api_key=key), "model-a",
    )
    assert record.api_key == "private-test-key"
    assert result.display_name == "新名称" and result.api_key == "********"
    instance.ping.assert_not_awaited()
    instance.repo.save.assert_awaited_once_with(record)


async def test_disabling_unassigned_provider_skips_upstream_test():
    record = provider()
    instance = service(record)
    await instance.save_provider(1, ModelProviderUpdate(is_active=False), "model-a")
    assert record.is_active is False
    instance.ping.assert_not_awaited()


async def test_reactivating_provider_retests_unchanged_connection():
    record = provider(is_active=False)
    instance = service(record)
    await instance.save_provider(1, ModelProviderUpdate(is_active=True), "model-a")
    assert record.is_active is True
    instance.ping.assert_awaited_once()
    assert instance.ping.call_args.args[0] is not record
    instance.repo.save.assert_awaited_once_with(record)


async def test_failed_reactivation_preserves_disabled_provider():
    record = provider(is_active=False)
    instance = service(record)
    instance.ping.return_value = SimpleNamespace(success=False, error="安全错误")
    with pytest.raises(AgentException):
        await instance.save_provider(1, ModelProviderUpdate(is_active=True), "model-a")
    assert record.is_active is False
    instance.repo.save.assert_not_awaited()


async def test_failed_connection_change_does_not_mutate_persistent_record():
    record = provider()
    instance = service(record)
    instance.ping.return_value = SimpleNamespace(success=False, error="安全错误")
    with pytest.raises(AgentException):
        await instance.save_provider(1, ModelProviderUpdate(model_name="other"), "model-a")
    assert record.model_name == "test"
    instance.repo.save.assert_not_awaited()


async def test_referenced_provider_cannot_be_disabled_or_deleted():
    instance = service(provider())
    instance.repo.list_roles.return_value = [SimpleNamespace(role_key="agent", provider_id="model-a")]
    for operation in (
        instance.save_provider(1, ModelProviderUpdate(is_active=False), "model-a"),
        instance.delete_provider(1, "model-a"),
    ):
        with pytest.raises(AgentException) as error:
            await operation
        assert error.value.status_code == 409
    instance.ping.assert_not_awaited()
    instance.repo.delete.assert_not_awaited()


def test_public_provider_masks_secret():
    result = public_provider(provider()).model_dump()
    assert result["api_key"] == "********"
    assert "private-test-key" not in str(result)


@pytest.mark.parametrize("extra", [
    {"supports_reasoning_effort": "true"},
    {"supports_reasoning_effort": True, "reasoning_effort_levels": None},
    {"supports_reasoning_effort": True, "reasoning_effort_levels": []},
    {"supports_reasoning_effort": True, "reasoning_effort_levels": [{"key": "high", "value": True}], "default_reasoning_effort": "high"},
    {"supports_reasoning_effort": True, "reasoning_effort_levels": [{"key": "high", "value": 2048}, {"key": "high", "value": 4096}], "default_reasoning_effort": "high"},
    {"supports_reasoning_effort": True, "reasoning_effort_levels": [{"key": "high", "value": "high"}], "default_reasoning_effort": "max"},
    {"supports_reasoning_effort": False, "reasoning_effort_levels": []},
])
def test_reject_invalid_reasoning_mappings(extra):
    with pytest.raises(AgentException):
        validate_reasoning_config(extra, "chat")


def test_numeric_reasoning_mapping_and_current_model_fallback():
    extra = {"supports_reasoning_effort": True, "reasoning_effort_levels": [
        {"key": "low", "value": 1024}, {"key": "high", "value": 8192},
    ], "default_reasoning_effort": "low"}
    validate_reasoning_config(extra, "chat")
    assert normalize_effort(extra, "max") == "low"
    assert normalize_effort(extra, "high", "turbo") == "fast"
    assert extra["reasoning_effort_levels"][0]["value"] == 1024


def mock_http(monkeypatch, data, status=200, requests=None):
    original = httpx.AsyncClient

    def handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(status, json=data)

    monkeypatch.setattr("model_service.service.httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@test.local", "https://test.local?key=hidden"])
async def test_probe_rejects_invalid_url_before_sending_stored_key(monkeypatch, url):
    requests = []
    mock_http(monkeypatch, {}, requests=requests)
    instance = service(provider())
    with pytest.raises(AgentException):
        await instance.probe_metadata(1, ModelProbeRequest(
            provider="openai", provider_id="model-a", base_url=url, model_name="test",
        ))
    assert requests == []


@pytest.mark.parametrize("data", [None, "invalid", {"data": {}}, {"models": 1}])
async def test_probe_malformed_metadata_fails_explicitly(monkeypatch, data):
    mock_http(monkeypatch, data)
    with pytest.raises(AgentException):
        await service().probe_metadata(1, ModelProbeRequest(
            provider="openai_compatible", base_url="https://test.local/v1", model_name="test",
        ))


async def test_unknown_probe_does_not_claim_capabilities(monkeypatch):
    mock_http(monkeypatch, {"data": [{"id": "test"}]})
    instance = service()
    payload = ModelProbeRequest(provider="openai_compatible", base_url="https://test.local/v1", api_key="test-key", model_name="test")
    assert (await instance.detect_context(1, payload))["confidence"] == "none"
    result = await instance.detect_reasoning(1, payload)
    assert result["levels"] == [] and result["complete"] is False
    instance.repo.save.assert_not_awaited()


@pytest.mark.parametrize(("kind", "data"), [
    ("embedding", {}), ("embedding", {"data": [{"embedding": [True]}]}),
    ("reranker", {"results": []}), ("audio", {"models": []}),
])
async def test_non_chat_ping_requires_real_protocol_result(monkeypatch, kind, data):
    mock_http(monkeypatch, data)
    result = await ModelService(SimpleNamespace()).ping(provider(provider_type=kind))
    assert result.success is False
    assert "private-test-key" not in str(result)


@pytest.mark.parametrize(("kind", "data"), [
    ("embedding", {"data": [{"embedding": [0.1, -0.2]}]}),
    ("reranker", {"results": [{"index": 0, "relevance_score": 0.9}]}),
    ("audio", {"text": ""}),
])
async def test_non_chat_ping_uses_actual_endpoint(monkeypatch, kind, data):
    requests = []
    mock_http(monkeypatch, data, requests=requests)
    result = await ModelService(SimpleNamespace()).ping(provider(provider_type=kind))
    assert result.success is True
    expected = {"embedding": "/v1/embeddings", "reranker": "/v1/rerank", "audio": "/v1/audio/transcriptions"}
    assert requests[0].url.path == expected[kind]


async def test_chat_ping_rejects_incomplete_stream_and_closes_it(monkeypatch):
    closed = []

    async def stream(messages):
        try:
            yield SimpleNamespace(kind=SimpleNamespace(value="text_delta"))
        finally:
            closed.append(True)

    monkeypatch.setattr(ModelService, "build_client", staticmethod(lambda *args: SimpleNamespace(stream=stream)))
    result = await ModelService(SimpleNamespace()).ping(provider())
    assert result.success is False and closed == [True]


def test_fast_capability_requires_explicit_openai_none_mapping():
    assert fast_capabilities("openai", {})["fast_behavior"] == "model_default"
    assert not fast_capabilities("openai", {})["turbo_supported"]
    assert fast_capabilities("openai", {"fast_reasoning_effort": "none"})["turbo_supported"]
    assert fast_capabilities("anthropic", {})["turbo_supported"]
    assert fast_capabilities("deepseek", {})["turbo_supported"]
    assert not fast_capabilities("openai_compatible", {"thinking_protocol": "openai"})["turbo_supported"]
    with pytest.raises(AgentException):
        validate_reasoning_config({"fast_reasoning_effort": "low"}, "chat")


@pytest.mark.parametrize("budget", [None, "2048", "invalid", True, False, 2048.0, 1023, 8192])
async def test_anthropic_explicit_budget_rejects_invalid_type_or_range(monkeypatch, budget):
    from core.config import settings

    monkeypatch.setattr(settings, "llm_default_max_tokens", 8192)
    record = provider(provider="anthropic")
    instance = service(record)
    payload = ModelProviderUpdate(extra_config={"thinking_budget_tokens": budget})
    with pytest.raises(AgentException, match="整数") as error:
        await instance.save_provider(1, payload, record.provider_id)
    assert error.value.status_code == 400
    assert record.extra_config == {}
    instance.ping.assert_not_awaited()
    instance.repo.save.assert_not_awaited()


@pytest.mark.parametrize("budget", [1024, 8191])
async def test_anthropic_explicit_budget_accepts_integer_boundaries(monkeypatch, budget):
    from core.config import settings

    monkeypatch.setattr(settings, "llm_default_max_tokens", 8192)
    record = provider(provider="anthropic")
    instance = service(record)
    payload = ModelProviderUpdate(extra_config={"thinking_budget_tokens": budget})
    result = await instance.save_provider(1, payload, record.provider_id)
    assert result.extra_config["thinking_budget_tokens"] == budget
    assert type(result.extra_config["thinking_budget_tokens"]) is int
    instance.ping.assert_awaited_once()
    instance.repo.save.assert_awaited_once_with(record)


@pytest.mark.parametrize("budget", [1023, 8192])
def test_anthropic_numeric_reasoning_levels_keep_budget_range_validation(monkeypatch, budget):
    from core.config import settings

    monkeypatch.setattr(settings, "llm_default_max_tokens", 8192)
    payload = ModelProviderInput(
        display_name="测试模型", provider="anthropic", model_name="test",
        base_url="https://test.local", api_key="test-key",
        extra_config={
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": [{"key": "high", "value": budget}],
            "default_reasoning_effort": "high",
        },
    )
    with pytest.raises(AgentException, match="思考预算"):
        service()._validate_provider(payload, "test-key")


async def test_capabilities_separate_agent_and_workflow_role_policies():
    agent = provider(
        provider_id="agent-provider", provider="openai",
        extra_config={
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": [{"key": "low", "value": "low"}],
            "default_reasoning_effort": "low",
        },
    )
    workflow = provider(
        provider_id="workflow-provider", provider="anthropic",
        extra_config={
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": [{"key": "high", "value": 4096}],
            "default_reasoning_effort": "high",
        },
    )
    instance = service()
    instance.repo.list_providers.return_value = [agent, workflow]
    instance.repo.list_roles.return_value = [
        SimpleNamespace(role_key="agent", provider_id=agent.provider_id),
        SimpleNamespace(role_key="workflow", provider_id=workflow.provider_id),
    ]
    records = {row.provider_id: row for row in (agent, workflow)}
    instance.repo.get_provider.side_effect = lambda workspace_id, provider_id: records.get(provider_id)
    instance.preferences = AsyncMock(return_value={"default_model_provider_id": None})

    result = await instance.capabilities(1, 3)

    assert result["main_provider_id"] == agent.provider_id
    assert result["workflow_provider_id"] == workflow.provider_id
    assert result["default_reasoning_effort"] == "low"
    assert result["reasoning_effort_levels"] == [{"key": "low", "value": "low"}]
    assert result["turbo_supported"] is False
    policies = {row["provider_id"]: row for row in result["models"]}
    assert policies[agent.provider_id]["reasoning_effort_levels"] == [
        {"key": "low", "value": "low"},
    ]
    assert policies[workflow.provider_id]["reasoning_effort_levels"] == [
        {"key": "high", "value": 4096},
    ]
    assert policies[workflow.provider_id]["default_reasoning_effort"] == "high"
    assert policies[workflow.provider_id]["turbo_supported"] is True
    instance.preferences.assert_awaited_once_with(1, 3)
