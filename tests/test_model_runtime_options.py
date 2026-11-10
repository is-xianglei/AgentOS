"""模型选项贯穿会话、子代理、记忆任务和出站参数。"""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from core.errors import AgentException
from harness.adapters.memory.builtin_provider import BuiltinMemoryProvider
from harness.adapters.models import anthropic_client as anthropic_module
from harness.adapters.models import openai_client as openai_module
from harness.adapters.models import resolver as resolver_module
from harness.adapters.models.anthropic_client import AnthropicClient
from harness.adapters.models.openai_client import OpenAIClient
from harness.adapters.models.resolver import DatabaseModelResolver
from harness.app.ports import (
    Effort,
    ModelEventKind,
    ModelSettings,
    ModelStreamEvent,
    ModelTurnResult,
    ResolvedModel,
)
from harness.domain.prompts.lead import TURBO_PROMPT
from model_service.models import ModelProviderRecord
from model_service.service import ModelService
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner
from runtime.turn_config import TurnConfiguration
from session.schemas import SessionRenameRequest, SessionSendMessageRequest
from session.service import SessionService
from tests.fixtures.model_calls import structured, thinking_off, thinking_on

pytestmark = pytest.mark.anyio


def _provider(provider="openai", extra=None):
    return SimpleNamespace(
        provider_id="provider-a", provider=provider, extra_config=extra or {},
        model_name="model-a", api_key="test", base_url="https://example.test/v1",
    )


def _record(provider="openai", extra=None):
    """build_model 需要真实 ORM 实例（读取 provider_id 等字段），其余用例沿用 SimpleNamespace。"""
    return ModelProviderRecord(**vars(_provider(provider, extra)))


def _session(**kwargs):
    return SimpleNamespace(
        id=1, workspace_id=2, user_id=3, workflow_id=None, extra={},
        model_provider_id=None, mode_slug="standard", thinking_effort="fast",
        **kwargs,
    )


def test_patch_distinguishes_default_reset_and_missing_options():
    assert SessionSendMessageRequest(content="继续").model_fields_set == {"content"}
    assert SessionRenameRequest(model_provider_id=None).model_dump(exclude_unset=True) == {
        "model_provider_id": None,
    }
    for invalid in ({}, {"mode_slug": None}, {"thinking_effort": None}):
        with pytest.raises(ValidationError):
            SessionRenameRequest(**invalid)


async def test_turbo_rejects_native_default_before_mutating_session(monkeypatch):
    select = AsyncMock(return_value=_provider())
    monkeypatch.setattr(ModelService, "select_provider", select)
    db = SimpleNamespace(flush=AsyncMock())
    session = _session()
    with pytest.raises(AgentException, match="关闭思考"):
        await SessionService(db).apply_model_options(
            session, 3, 2, {"mode_slug": "turbo", "model_provider_id": "provider-a"},
        )
    assert session.mode_slug == "standard" and session.model_provider_id is None
    assert session.extra == {}
    db.flush.assert_not_awaited()


async def test_turbo_locks_fast_and_keeps_explicit_provider(monkeypatch):
    monkeypatch.setattr(ModelService, "select_provider", AsyncMock(
        return_value=_provider(extra={"fast_reasoning_effort": "none"}),
    ))
    session = _session()
    await SessionService(SimpleNamespace(flush=AsyncMock())).apply_model_options(
        session, 3, 2, {"mode_slug": "turbo", "thinking_effort": "high",
                        "model_provider_id": "provider-a"},
    )
    assert session.thinking_effort == "fast" and session.mode_slug == "turbo"
    assert session.extra["model_provider_override"] is True


async def test_omitted_options_preserve_model_and_explicit_null_resets(monkeypatch):
    selected = AsyncMock(return_value=_provider())
    monkeypatch.setattr(ModelService, "select_provider", selected)
    session = _session()
    session.model_provider_id = "old"
    session.extra = {"model_provider_override": True}
    service = SessionService(SimpleNamespace(flush=AsyncMock()))
    await service.apply_model_options(session, 3, 2, {})
    assert selected.await_args.args[2] == "old"
    await service.apply_model_options(session, 3, 2, {"model_provider_id": None})
    assert selected.await_args.args[2] is None
    assert session.extra["model_provider_override"] is False


async def test_workflow_uses_role_and_rejects_turbo(monkeypatch):
    selected = AsyncMock(return_value=_provider())
    monkeypatch.setattr(ModelService, "select_provider", selected)
    session = _session()
    session.workflow_id = 8
    service = SessionService(SimpleNamespace(flush=AsyncMock()))
    await service.apply_model_options(session, 3, 2, {})
    assert selected.await_args.args[3] == "workflow"
    with pytest.raises(AgentException, match="标准模式"):
        await service.apply_model_options(session, 3, 2, {"mode_slug": "turbo"})


def _resolved(client=None, provider_record_id=None):
    return ResolvedModel(
        client=client or object(),
        settings=ModelSettings(model_id="model-a"),
        provider_record_id=provider_record_id,
    )


@pytest.mark.parametrize("scenario", ["agent", "subagent", "workflow"])
async def test_explicit_model_and_effort_propagate_to_all_scenarios(monkeypatch, scenario):
    resolve = AsyncMock(return_value=_resolved(provider_record_id="provider-a"))
    monkeypatch.setattr(DatabaseModelResolver, "resolve", resolve)
    session = _session()
    session.model_provider_id = "provider-a"
    session.thinking_effort = "high"
    session.extra = {"model_provider_override": True}
    await DatabaseModelResolver(object()).for_session(session, scenario)
    assert resolve.await_args.kwargs == {"provider_id": "provider-a", "effort": "high"}


async def test_nonexplicit_workflow_uses_its_role(monkeypatch):
    resolve = AsyncMock(return_value=_resolved(provider_record_id="provider-a"))
    monkeypatch.setattr(DatabaseModelResolver, "resolve", resolve)
    session = _session()
    session.model_provider_id = "provider-a"
    await DatabaseModelResolver(object()).for_session(session, "workflow")
    assert resolve.await_args.kwargs["provider_id"] is None


@pytest.mark.parametrize("scenario", ["memory_extractor", "memory_dream"])
async def test_legacy_memory_job_keeps_environment_endpoint(monkeypatch, scenario):
    legacy_client = SimpleNamespace(model="legacy-env-model")
    monkeypatch.setattr(resolver_module, "create_model_client", lambda **_: legacy_client)
    resolve = AsyncMock()
    monkeypatch.setattr(DatabaseModelResolver, "resolve", resolve)
    resolved = await DatabaseModelResolver(object()).for_frozen_job(
        2, 3, scenario, model="legacy-model", provider_id=None,
    )
    assert resolved.client is legacy_client
    resolve.assert_not_awaited()


async def test_new_memory_job_honors_frozen_provider(monkeypatch):
    frozen = object()
    resolve = AsyncMock(return_value=_resolved(frozen, provider_record_id="provider-a"))
    monkeypatch.setattr(DatabaseModelResolver, "resolve", resolve)
    resolved = await DatabaseModelResolver(object()).for_frozen_job(
        2, 3, "memory_extractor", model="frozen", provider_id="provider-a",
    )
    assert resolved.client is frozen
    assert resolve.await_args.kwargs["provider_id"] == "provider-a"


async def test_environment_fast_does_not_emit_unverified_private_protocol(monkeypatch):
    client = OpenAIClient(model="model-a", api_key="test")
    monkeypatch.setattr(resolver_module, "create_model_client", lambda **_: client)
    resolved = await DatabaseModelResolver(object()).resolve(
        None, None, "agent", effort=Effort.FAST,
    )
    assert resolved.provider_record_id is None and resolved.client is client
    assert client._thinking_kwargs(resolved.settings.effort) == {}


async def test_turbo_never_wakes_existing_team():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.configuration = TurnConfiguration.for_lead(object(), mode_slug="turbo")
    await runtime._wake_team_members(1, object())


async def test_subagent_rejects_turbo_before_resolving_client(monkeypatch):
    session = _session()
    session.mode_slug = "turbo"
    monkeypatch.setattr(SessionService, "runtime_model_options", AsyncMock(return_value=session))
    runner = SubAgentRunner.__new__(SubAgentRunner)
    runner._llm_injected = False
    runner.db = object()
    runner.sessions = SessionService(runner.db)
    with pytest.raises(AgentException, match="极速模式"):
        await runner._configure_model(1, SimpleNamespace(), 2, 3)


@pytest.mark.parametrize("protocol,enabled,effort,fast,expected", [
    ("openai", False, None, None, {}),
    ("openai", False, None, "none", {"reasoning_effort": "none"}),
    ("openai", True, "high", None, {"reasoning_effort": "high"}),
    ("deepseek", False, None, None, {"extra_body": {"thinking": {"type": "disabled"}}}),
    ("chat_template", True, 4096, None, {"extra_body": {"chat_template_kwargs": {
        "thinking": True, "reasoning_effort": 4096,
    }}}),
])
def test_openai_wire_mapping_preserves_protocol_and_value_type(
    protocol, enabled, effort, fast, expected,
):
    levels = thinking_on(effort) if enabled else thinking_off()
    client = OpenAIClient(
        model="model-a", api_key="test", thinking_protocol=protocol,
        fast_reasoning_effort=fast, reasoning_levels=levels,
    )
    assert client._thinking_kwargs(Effort.HIGH) == expected


def test_build_model_honors_native_fast_mapping():
    resolved = ModelService.build_model(
        _record(extra={"fast_reasoning_effort": "none"}), Effort.FAST
    )
    assert resolved.client._thinking_kwargs(resolved.settings.effort) == {
        "reasoning_effort": "none"
    }


@pytest.mark.parametrize("effort,expected", [
    ("high", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
    (4096, {"thinking": {"type": "enabled", "budget_tokens": 4096}}),
])
def test_anthropic_wire_mapping_preserves_adaptive_or_budget(effort, expected):
    client = AnthropicClient(model="model-a", api_key="test", reasoning_levels=thinking_on(effort))
    assert client._thinking_kwargs(Effort.HIGH, 8192) == expected
    with pytest.raises(AgentException):
        AnthropicClient(
            model="model-a", api_key="test", reasoning_levels=thinking_on(8192),
        )._thinking_kwargs(Effort.HIGH, 8192)


class _Output(BaseModel):
    answer: int


async def test_openai_structured_request_keeps_selected_effort(monkeypatch):
    parse = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(parsed=_Output(answer=42)),
    )]))
    monkeypatch.setattr(openai_module, "AsyncOpenAI", lambda **_: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)),
    ))
    client = OpenAIClient(
        model="model-a", api_key="test", thinking_protocol="openai",
        reasoning_levels=thinking_on("high"),
    )
    assert (await structured(client, [], _Output)).answer == 42
    assert parse.await_args.kwargs["reasoning_effort"] == "high"
    assert "temperature" not in parse.await_args.kwargs


@pytest.mark.parametrize("budget", [4096, 31744, 32512])
def test_anthropic_structured_budget_keeps_requested_output_room(monkeypatch, budget):
    monkeypatch.setattr(anthropic_module.app_settings, "llm_default_max_tokens", 32768)
    client = AnthropicClient(model="model-a", api_key="test", reasoning_levels=thinking_on(budget))
    maximum = client._structured_max_tokens(Effort.HIGH, 1024)
    assert maximum == min(budget + 1024, 32768)
    assert maximum > budget
    assert client._thinking_kwargs(Effort.HIGH, maximum)["thinking"]["budget_tokens"] == budget


async def test_anthropic_structured_retry_keeps_effort_and_signed_thinking(monkeypatch):
    from anthropic.types import ThinkingBlock, ToolUseBlock

    signed = ThinkingBlock(type="thinking", thinking="核对字段", signature="signed")
    invalid = ToolUseBlock(
        type="tool_use", id="call-1", name="emit_structured_output", input={"answer": "bad"},
    )
    valid = invalid.model_copy(update={"id": "call-2", "input": {"answer": 42}})
    create = AsyncMock(side_effect=[
        SimpleNamespace(content=[signed, invalid], stop_reason="tool_use"),
        SimpleNamespace(content=[signed, valid], stop_reason="tool_use"),
    ])

    @asynccontextmanager
    async def stream(**kwargs):
        response = await create(**kwargs)
        yield SimpleNamespace(get_final_message=AsyncMock(return_value=response))

    monkeypatch.setattr(anthropic_module, "AsyncAnthropic", lambda **_: SimpleNamespace(
        messages=SimpleNamespace(stream=stream),
    ))
    client = AnthropicClient(model="model-a", api_key="test", reasoning_levels=thinking_on("high"))
    assert (await structured(client, [], _Output)).answer == 42
    for call in create.await_args_list:
        assert call.kwargs["tool_choice"] == {"type": "auto"}
        assert call.kwargs["thinking"] == {"type": "adaptive"}
        assert call.kwargs["output_config"] == {"effort": "high"}
        assert "temperature" not in call.kwargs
    retry_messages = create.await_args_list[1].kwargs["messages"]
    assert retry_messages[0]["content"][0] == signed.model_dump(mode="json")
    assert retry_messages[0]["content"][1]["id"] == "call-1"
    assert retry_messages[1]["content"][0]["tool_use_id"] == "call-1"


async def test_turbo_loop_bypasses_skills_memory_plan_compact_and_stop(monkeypatch):
    requests = []

    class _LLM:
        provider = "openai"
        model = "model-a"

        async def stream(self, context, settings):
            requests.append({"system_prompt": context.instruction, "tools": list(context.tools)})
            yield ModelStreamEvent(
                ModelEventKind.TURN_RESULT,
                result=ModelTurnResult(text="答案", stop_reason="end_turn"),
            )

    db = SimpleNamespace(commit=AsyncMock())
    runtime = AgentRuntime(db, workspace_id=2, llm=_LLM())
    runtime.configuration = TurnConfiguration.for_lead(
        db,
        mode_slug="turbo",
        model=runtime.configuration.model,
        database_model=True,
    )
    runtime.bus = SimpleNamespace(emit=AsyncMock())
    runtime.sessions = SimpleNamespace(
        load_context_for_turn=AsyncMock(return_value=([], [], 1)),
        add_message=AsyncMock(return_value=SimpleNamespace(id=99)),
    )
    runtime.memory = SimpleNamespace(recall=AsyncMock())
    runtime.plan_service = SimpleNamespace(get_active=AsyncMock())
    runtime.compact_service = SimpleNamespace(maybe_compact=AsyncMock())
    runtime._apply_stop_hooks = AsyncMock()
    skill_catalog = AsyncMock()
    monkeypatch.setattr("runtime.lead_loop.SkillService.get_catalog", skill_catalog)
    session = SimpleNamespace(system_prompt="原系统提示")
    result = await runtime._run_llm_loop(1, session, SimpleNamespace(id=uuid4()), None)
    assert not result.suspended and result.completed_message_id == 99
    assert len(requests) == 1
    assert requests[0]["tools"] == []
    assert requests[0]["system_prompt"][0].text == TURBO_PROMPT
    for call in (
        skill_catalog, runtime.memory.recall,
        runtime.plan_service.get_active, runtime.compact_service.maybe_compact,
        runtime._apply_stop_hooks,
    ):
        call.assert_not_awaited()


async def test_turbo_completes_without_enqueuing_memory(monkeypatch):
    monkeypatch.setattr("core.config.settings.memory_extraction_enabled", True)
    enqueue = AsyncMock()
    monkeypatch.setattr("memory.jobs.service.MemoryJobService.enqueue_extract", enqueue)
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.configuration = TurnConfiguration.for_lead(object(), mode_slug="turbo")
    runtime._fence_lease = AsyncMock()
    runtime.db = SimpleNamespace(commit=AsyncMock(), begin_nested=AsyncMock())
    runtime.memory = BuiltinMemoryProvider(runtime.db)
    runtime.sessions = SimpleNamespace(
        mark_turn_status=AsyncMock(), set_status=AsyncMock(),
    )
    assert await runtime._complete_turn(SimpleNamespace(id=1), SimpleNamespace(id=uuid4()), 99) is None
    enqueue.assert_not_awaited()
    runtime.db.begin_nested.assert_not_awaited()
    runtime.db.commit.assert_awaited_once()


async def test_anthropic_large_structured_budget_uses_real_sdk_stream_and_retry(monkeypatch):
    from anthropic import AsyncAnthropic

    monkeypatch.setattr(anthropic_module.app_settings, "llm_default_max_tokens", 50000)
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        answer = "bad" if len(requests) == 1 else 42
        events = [
            {"type": "message_start", "message": {
                "id": "msg-1", "type": "message", "role": "assistant",
                "model": "test-model", "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0},
            }},
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "thinking", "thinking": "", "signature": "",
            }},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "thinking_delta", "thinking": "核对字段",
            }},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "signature_delta", "signature": "opaque-signature",
            }},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "id": "call-1", "name": "emit_structured_output",
                "input": {},
            }},
            {"type": "content_block_delta", "index": 1, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps({"answer": answer}),
            }},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {
                "stop_reason": "tool_use", "stop_sequence": None,
            }, "usage": {"output_tokens": 12}},
            {"type": "message_stop"},
        ]
        data = "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        monkeypatch.setattr(
            anthropic_module, "AsyncAnthropic",
            lambda **kwargs: AsyncAnthropic(**kwargs, http_client=http_client),
        )
        client = AnthropicClient(
            model="test-model", api_key="test", max_retries=0,
            reasoning_levels=thinking_on(30000),
        )
        assert (await structured(client, [], _Output)).answer == 42
    assert len(requests) == 2
    for body in requests:
        assert body["stream"] is True
        assert body["max_tokens"] == 30256
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 30000}
        assert body["tool_choice"] == {"type": "auto"}
        assert "temperature" not in body
    retry = requests[1]["messages"]
    assert retry[0]["content"][0]["signature"] == "opaque-signature"
    assert retry[0]["content"][0]["thinking"] == "核对字段"
    assert retry[1]["content"][0]["tool_use_id"] == "call-1"
