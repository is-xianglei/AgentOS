"""回合配置：默认 Agent、绑定自建 Agent、极速模式与注入客户端四种情况的配置结果。

数据库与模型解析器全部打桩；网关与工具集走真实实现。
"""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from agent.service import AgentRuntimeConfig, DelegateCatalogEntry
from harness.adapters.models import resolver as resolver_module
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.app.ports import ModelSettings, ResolvedModel
from harness.domain.review import PLAN_MODE_ALLOWED_TOOLS
from runtime import turn_config as turn_config_module
from runtime.turn_config import TurnConfiguration, configure_turn

pytestmark = pytest.mark.anyio

_DB: Any = object()
_AGENT = AgentRuntimeConfig(
    agent_id=7,
    system_prompt="你是审稿人",
    model_name="reviewer-model",
    tool_runtime_names=("Read", "Grep", "Write"),
    skill_ids=(11, 12),
    skill_names=("review", "style"),
    name="审稿人",
    description=None,
)


class _Client:
    provider = "anthropic"
    model = "claude-main"


class _FakeAgentDefinitionService:
    """记录目录与运行配置查询；只返回固定的一项目录与 _AGENT。"""

    calls: ClassVar[list[tuple[str, int]]] = []

    def __init__(self, db: object) -> None:
        pass

    async def list_delegate_catalog(self, workspace_id: int) -> tuple[DelegateCatalogEntry, ...]:
        self.calls.append(("catalog", workspace_id))
        return (DelegateCatalogEntry(agent_id=7, name="审稿人", description=None, tool_names=()),)

    async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
        self.calls.append(("runtime_config", agent_id))
        return _AGENT


class _FakeResolver:
    """模型解析器替身：主模型声明上下文长度 8000，来自工作区模型配置。"""

    calls: ClassVar[list[tuple[str, Any]]] = []
    main = ResolvedModel(
        client=_Client(),  # type: ignore[arg-type]
        settings=ModelSettings(model_id="db-main"),
        provider_record_id="provider-a",
        context_length=8000,
    )
    compact = ResolvedModel(
        client=_Client(),  # type: ignore[arg-type]
        settings=ModelSettings(model_id="db-compact"),
    )

    def __init__(self, db: object) -> None:
        pass

    async def for_session(self, session: Any, scenario: str) -> ResolvedModel:
        self.calls.append(("for_session", scenario))
        return self.main

    async def resolve(self, workspace_id: Any, user_id: Any, scenario: str) -> ResolvedModel:
        self.calls.append(("resolve", (workspace_id, user_id, scenario)))
        return self.compact


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAgentDefinitionService.calls = []
    _FakeResolver.calls = []
    monkeypatch.setattr(turn_config_module, "AgentDefinitionService", _FakeAgentDefinitionService)
    monkeypatch.setattr(resolver_module, "DatabaseModelResolver", _FakeResolver)


def _session(**overrides: Any) -> Any:
    values = {"agent_id": None, "mode_slug": "standard", "model_name": "session-model"}
    return SimpleNamespace(**{**values, **overrides})


async def test_default_agent_resolves_models_and_loads_delegate_catalog() -> None:
    configuration = await configure_turn(_DB, _session(), user_id=3, workspace_id=2)

    assert (configuration.mode_slug, configuration.turbo) == ("standard", False)
    assert (configuration.agent_id, configuration.profile, configuration.principal_name) == (
        None,
        "lead",
        "lead",
    )
    assert configuration.agent_prompt is None and configuration.skill_ids is None
    assert configuration.allowed_skill_names is None
    assert configuration.delegate_catalog == "- agent_id=7 名称:审稿人 描述:(无描述) 工具:(无)"
    assert configuration.tools.names == builtin_tool_set().names
    assert configuration.gateway.context.profile == "lead"
    assert configuration.gateway.context.principal_name == "lead"
    assert configuration.model is _FakeResolver.main and configuration.llm is _FakeResolver.main.client
    assert configuration.compact_model is _FakeResolver.compact
    assert configuration.compact_threshold == 6000
    assert configuration.database_model is True
    assert _FakeResolver.calls == [("for_session", "agent"), ("resolve", (2, 3, "compact"))]
    assert _FakeAgentDefinitionService.calls == [("catalog", 2)]
    # 工作区端点自带模型名，不再用会话上的模型名覆盖。
    session = _session()
    assert configuration.model_override(session) is None
    assert configuration.round_settings(session) == ModelSettings(model_id="db-main")
    assert configuration.llm_attribution(ModelSettings(model_id="db-main")) == (
        "anthropic",
        "db-main",
    )


async def test_bound_agent_uses_agent_tools_skills_and_identity() -> None:
    configuration = await configure_turn(_DB, _session(agent_id=7), user_id=3, workspace_id=2)

    assert (configuration.agent_id, configuration.profile, configuration.principal_name) == (
        7,
        "user_agent",
        "agent:7",
    )
    assert configuration.agent_prompt == "你是审稿人"
    assert configuration.agent_model_name == "reviewer-model"
    assert configuration.skill_ids == (11, 12)
    assert configuration.allowed_skill_names == frozenset({"review", "style"})
    # 绑定了自建 Agent 的会话不能再委派，目录也不查。
    assert configuration.delegate_catalog is None
    assert _FakeAgentDefinitionService.calls == [("runtime_config", 7)]
    assert [tool.name for tool in configuration.tools.tools] == ["Read", "Write", "Grep"]
    assert configuration.gateway.context.agent_id == 7
    assert configuration.gateway.context.allowed_skill_names == frozenset({"review", "style"})
    # Plan Mode 只留只读白名单，平时去掉两个 Plan 工具。
    assert configuration.tools_for_mode(True).names == {"Read", "Grep"}
    assert configuration.tools_for_mode(False).names == {"Read", "Write", "Grep"}


async def test_bound_agent_requires_trusted_workspace() -> None:
    with pytest.raises(Exception, match="缺少可信工作区上下文"):
        await configure_turn(_DB, _session(agent_id=7), user_id=3, workspace_id=None)


async def test_turbo_limits_tools_and_skills() -> None:
    configuration = await configure_turn(_DB, _session(mode_slug="turbo"), user_id=3, workspace_id=2)

    assert configuration.turbo is True
    # 极速放行名单目前为空：没有工具、不能用 Skill。
    assert configuration.tools.names == frozenset()
    assert configuration.allowed_skill_names == frozenset()
    assert configuration.tools_for_mode(True).names == frozenset()
    assert configuration.gateway.registry.names == frozenset()
    # 目录照常按默认 Agent 加载，由回合循环决定极速模式不注入。
    assert configuration.delegate_catalog is not None


async def test_injected_client_skips_model_resolution_but_still_binds_agent() -> None:
    client = _Client()

    configuration = await configure_turn(
        _DB,
        _session(agent_id=7),
        user_id=3,
        workspace_id=2,
        injected=client,  # type: ignore[arg-type]
    )

    assert _FakeResolver.calls == []
    assert configuration.llm is client
    assert configuration.model == ResolvedModel.from_client(client)  # type: ignore[arg-type]
    assert configuration.compact_model is None and configuration.compact_threshold is None
    assert configuration.database_model is False
    assert configuration.profile == "user_agent" and configuration.agent_id == 7
    # 不是工作区端点，按 Agent 上的模型名覆盖。
    session = _session(agent_id=7)
    assert configuration.model_override(session) == "reviewer-model"
    assert configuration.round_settings(session) == ModelSettings(model_id="reviewer-model")


async def test_injected_client_on_default_agent_overrides_with_session_model() -> None:
    configuration = await configure_turn(
        _DB, _session(), user_id=3, workspace_id=2, injected=_Client()  # type: ignore[arg-type]
    )

    assert configuration.round_settings(_session()) == ModelSettings(model_id="session-model")
    assert configuration.round_settings(_session(model_name=None)) == ModelSettings(
        model_id="claude-main"
    )


def test_configuration_is_immutable_and_lead_tools_drop_plan_tools() -> None:
    configuration = TurnConfiguration.for_lead(_DB)

    with pytest.raises(FrozenInstanceError):
        configuration.mode_slug = "turbo"  # type: ignore[misc]
    assert "ExitPlanMode" not in configuration.tools_for_mode(False).names
    assert "WritePlan" not in configuration.tools_for_mode(False).names
    assert configuration.tools_for_mode(True).names == configuration.tools.only(
        *PLAN_MODE_ALLOWED_TOOLS
    ).names
    with pytest.raises(Exception, match="回合配置缺少模型"):
        configuration.require_model()
