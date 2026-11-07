"""模型端口与解析器：Context、Effort、ModelSettings、档位映射表与 DatabaseModelResolver。

请求参数逐字节不变由 test_model_request_golden.py 保证；这里覆盖新增类型自身的契约，
以及解析器在数据库与 .env 两条路径上给出的客户端、调用参数和端点元信息。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

import database.registry  # noqa: F401  注册全部 ORM 映射，单独构造 ModelProviderRecord 需要
from core.config import settings as app_settings
from core.errors import AgentException
from harness.adapters.models import resolver as resolver_module
from harness.adapters.models.anthropic_client import AnthropicClient
from harness.adapters.models.builder import build_resolved_model, reasoning_levels
from harness.adapters.models.openai_client import OpenAIClient
from harness.adapters.models.reasoning import ReasoningLevels, ThinkingLevel, resolve_level
from harness.adapters.models.resolver import DatabaseModelResolver
from harness.app.ports import Effort, ModelSettings, ResolvedModel
from harness.domain.context import Context, PromptBlock, messages_from_rows
from harness.domain.messages import AssistantMessage, ToolResult, UserMessage
from model_service.models import ModelProviderRecord
from model_service.service import ModelService

pytestmark = pytest.mark.anyio


class _Answer(BaseModel):
    answer: int


def _record(provider: str = "deepseek", **extra: object) -> ModelProviderRecord:
    return ModelProviderRecord(
        provider_id="provider-1",
        workspace_id=2,
        display_name="工作区模型",
        provider_type="chat",
        provider=provider,
        base_url="https://db.test/v1",
        api_key="db-key",
        model_name="db-model",
        extra_config=dict(extra),
        is_active=True,
    )


# ---------- 领域层 Context ----------


def test_messages_from_rows_restores_each_role() -> None:
    rows = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": [{"kind": "text", "text": "在"}]},
        {
            "role": "tool",
            "content": ToolResult(
                tool_call_id="t1", tool_name="Read", arguments={}, content="A"
            ).to_content(),
        },
    ]
    messages = messages_from_rows(rows)
    assert [type(message) for message in messages] == [UserMessage, AssistantMessage, ToolResult]


def test_messages_from_rows_rejects_unknown_role() -> None:
    with pytest.raises(AgentException, match="未知的消息角色"):
        messages_from_rows([{"role": "system", "content": "x"}])


def test_context_guards_structured_inputs() -> None:
    blocks = (PromptBlock(text="稳定段", cache_breakpoint=True),)
    streaming: Context[None] = Context(instruction=blocks, messages=())
    with pytest.raises(AgentException, match="缺少目标类型"):
        streaming.require_output_type()
    with pytest.raises(AgentException, match="只支持纯文本"):
        streaming.text_instruction()
    structured = Context(instruction="选出答案", messages=(), output_type=_Answer)
    assert structured.require_output_type() is _Answer
    assert structured.text_instruction() == "选出答案"


# ---------- 应用层端口 ----------


def test_effort_values_match_session_thinking_effort() -> None:
    from session.schemas import SessionRenameRequest

    allowed = SessionRenameRequest.model_fields["thinking_effort"].annotation.__args__[0].__args__
    assert tuple(effort.value for effort in Effort) == allowed


def test_effort_parse_rejects_unknown_value() -> None:
    assert Effort.parse(None) is None
    assert Effort.parse("max") is Effort.MAX
    with pytest.raises(AgentException, match="未知的思考强度档位"):
        Effort.parse("off")


def test_model_settings_requires_model_id() -> None:
    assert ModelSettings(model_id="m").require_model_id() == "m"
    with pytest.raises(AgentException, match="缺少模型名"):
        ModelSettings(model_id="").require_model_id()


def test_resolved_model_from_client_uses_client_default_model() -> None:
    client = SimpleNamespace(model="fake-model")
    assert ResolvedModel.from_client(client).settings == ModelSettings(model_id="fake-model")
    assert ResolvedModel.from_client(client, "other").settings.model_id == "other"
    # 替身没有模型名时模型名为空串，成本归因据此留空。
    assert ResolvedModel.from_client(object()).settings.model_id == ""


# ---------- 档位映射表 ----------


def test_reasoning_levels_require_every_effort() -> None:
    with pytest.raises(AgentException, match="思考档位映射表不完整"):
        ReasoningLevels({Effort.FAST: ThinkingLevel(enabled=False)})


def test_reasoning_levels_normalize_unsupported_effort_to_model_default() -> None:
    levels = reasoning_levels(
        {
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": [{"key": "low", "value": "minimal"}],
            "default_reasoning_effort": "low",
        }
    )
    assert levels.level(Effort.FAST) == ThinkingLevel(enabled=False)
    assert levels.level(None) == ThinkingLevel(enabled=False)
    # 端点只声明了 low，其余档位按既有规则回落模型默认档位。
    assert levels.level(Effort.MAX) == ThinkingLevel(enabled=True, value="minimal")


def test_resolve_level_for_env_endpoint() -> None:
    assert resolve_level(None, None) is None
    assert resolve_level(None, Effort.FAST) == ThinkingLevel(enabled=False)
    assert resolve_level(None, Effort.HIGH) == ThinkingLevel(enabled=True, value="high")


# ---------- 构造器与解析器 ----------


def test_build_resolved_model_carries_endpoint_metadata() -> None:
    resolved = build_resolved_model(_record(context_length=65536), Effort.HIGH)
    assert isinstance(resolved.client, OpenAIClient)
    assert resolved.settings == ModelSettings(model_id="db-model", effort=Effort.HIGH)
    assert (resolved.provider_record_id, resolved.context_length) == ("provider-1", 65536)
    # bool 是 int 子类，不能当作上下文窗口。
    assert build_resolved_model(_record(context_length=True), None).context_length is None
    anthropic = build_resolved_model(_record("anthropic"), None)
    assert isinstance(anthropic.client, AnthropicClient)


def test_model_service_build_model_delegates_to_builder() -> None:
    resolved = ModelService.build_model(_record(), Effort.FAST)
    assert resolved.settings.effort is Effort.FAST
    assert resolved.client._thinking_kwargs(Effort.FAST) == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }


async def test_resolver_database_path_uses_workspace_endpoint(monkeypatch) -> None:
    select = AsyncMock(return_value=_record(context_length=4096))
    monkeypatch.setattr(ModelService, "select_provider", select)
    env_factory = AsyncMock()
    monkeypatch.setattr(resolver_module, "create_model_client", env_factory)

    resolved = await DatabaseModelResolver(object()).resolve(
        2, 3, "subagent", provider_id="provider-1", effort=Effort.MAX
    )

    assert select.await_args.args == (2, 3, "provider-1", "subagent")
    assert resolved.provider_record_id == "provider-1"
    assert resolved.context_length == 4096
    assert resolved.settings == ModelSettings(model_id="db-model", effort=Effort.MAX)
    env_factory.assert_not_called()


async def test_resolver_env_path_keeps_client_unmodified(monkeypatch) -> None:
    monkeypatch.setattr(ModelService, "select_provider", AsyncMock(return_value=None))
    monkeypatch.setattr(app_settings, "llm_provider", "openai")
    monkeypatch.setattr(app_settings, "openai_api_key", "env-key")
    monkeypatch.setattr(app_settings, "openai_model", "gpt-env")
    monkeypatch.setattr(app_settings, "openai_compact_model", "gpt-compact")

    resolved = await DatabaseModelResolver(object()).resolve(2, 3, "compact", effort=Effort.MAX)

    assert resolved.provider_record_id is None and resolved.context_length is None
    assert resolved.settings == ModelSettings(model_id="gpt-compact", effort=Effort.MAX)
    # 思考强度只放进 ModelSettings，客户端实例仍是 .env 端点配置，不带任何档位状态。
    assert resolved.client.reasoning_levels is None
    assert not hasattr(resolved.client, "thinking_enabled")
    assert not hasattr(resolved.client, "max_tokens")


async def test_resolver_without_workspace_skips_database(monkeypatch) -> None:
    select = AsyncMock()
    monkeypatch.setattr(ModelService, "select_provider", select)
    monkeypatch.setattr(
        resolver_module, "create_model_client", lambda **_: SimpleNamespace(model="m")
    )
    resolved = await DatabaseModelResolver(object()).resolve(None, None, "agent")
    assert resolved.settings == ModelSettings(model_id="m")
    select.assert_not_awaited()


async def test_resolver_for_session_rejects_unknown_effort(monkeypatch) -> None:
    monkeypatch.setattr(ModelService, "select_provider", AsyncMock(return_value=_record()))
    session = SimpleNamespace(
        workspace_id=2, user_id=3, model_provider_id=None, extra={}, thinking_effort="off"
    )
    with pytest.raises(AgentException, match="未知的思考强度档位"):
        await DatabaseModelResolver(object()).for_session(session, "agent")
