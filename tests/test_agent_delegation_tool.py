"""Agent 工具委派自建 Agent 的入参与分支测试。

全程 Fake/monkeypatch,不连数据库与 LLM;工作区隔离、启用状态与软删除的真实断言
见 test_agent_delegation_pg.py。
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from agent.service import AgentRuntimeConfig
from core.errors import AgentException
from harness.adapters.tools.builtin.agent import AgentTool, AgentToolInput
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.domain.roles.subagent import AgentType
from harness.domain.tool import ToolContext

pytestmark = pytest.mark.anyio

_WORKSPACE_ID = 3
_ROUND_TOOL_NAMES = frozenset({"Read", "Grep", "Agent"})


def _config(**overrides: Any) -> AgentRuntimeConfig:
    values: dict[str, Any] = {
        "agent_id": 7,
        "system_prompt": "你是审稿人",
        "model_name": "reviewer-model",
        "tool_runtime_names": ("Read", "Write"),
        "skill_ids": (1,),
        "skill_names": ("review-skill",),
        "name": "审稿人",
        "description": "负责审稿",
    }
    values.update(overrides)
    return AgentRuntimeConfig(**values)


def _ctx(**overrides: Any) -> ToolContext:
    values: dict[str, Any] = {
        "session_id": 1,
        "db": object(),
        "workspace_id": _WORKSPACE_ID,
        "user_id": 2,
        "profile": "lead",
        "allowed_tool_names": builtin_tool_set().names,
        "round_tool_names": _ROUND_TOOL_NAMES,
    }
    values.update(overrides)
    return ToolContext(**values)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: AgentRuntimeConfig | None = None,
    error: AgentException | None = None,
) -> tuple[list[tuple[int, int]], list[dict[str, Any]]]:
    """替换 agent 域 Service 与 _dispatch,返回 (配置查询记录, 派发记录)。"""
    config_requests: list[tuple[int, int]] = []
    dispatches: list[dict[str, Any]] = []

    class _FakeAgentDefinitionService:
        def __init__(self, db: object) -> None:
            pass

        async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
            config_requests.append((workspace_id, agent_id))
            if error is not None:
                raise error
            return config or _config()

    async def _fake_dispatch(
        self: AgentTool,
        ctx: ToolContext,
        prompt: str,
        spec: Any,
        parent_allowed_tool_names: frozenset[str] | None,
        allowed_skill_names: frozenset[str] | None,
    ) -> str:
        dispatches.append(
            {
                "prompt": prompt,
                "spec": spec,
                "parent_allowed_tool_names": parent_allowed_tool_names,
                "allowed_skill_names": allowed_skill_names,
                "workspace_id": ctx.workspace_id,
            }
        )
        return "委派报告"

    # _delegate 在调用时局部导入 AgentDefinitionService,替换模块属性即可生效。
    monkeypatch.setattr("agent.service.AgentDefinitionService", _FakeAgentDefinitionService)
    monkeypatch.setattr(AgentTool, "_dispatch", _fake_dispatch)
    return config_requests, dispatches


# ---------- 入参:agent_type 与 agent_id 二选一 ----------


def test_input_rejects_explicit_agent_type_and_agent_id() -> None:
    with pytest.raises(ValidationError, match="只能二选一"):
        AgentToolInput(agent_type=AgentType.EXPLORE, agent_id=7, prompt="审一下")


def test_runtime_validation_reports_conflict_as_tool_error() -> None:
    """主运行时先经注册表校验入参:冲突转成「工具参数校验失败」,作为工具结果回给模型。"""
    registry = builtin_tool_set()

    with pytest.raises(AgentException) as exc_info:
        registry.validate_input(
            "Agent", {"agent_type": "general-purpose", "agent_id": 7, "prompt": "审一下"}
        )

    assert exc_info.value.message == "工具参数校验失败"
    assert "只能二选一" in exc_info.value.details["errors"][0]["msg"]


def test_only_agent_id_survives_runtime_normalization() -> None:
    """规范化后不能凭空带上 agent_type 默认值,否则执行时会被误判为两者都传。"""
    normalized = builtin_tool_set().validate_input("Agent", {"agent_id": 7, "prompt": "审一下"})

    assert normalized == {"agent_id": 7, "prompt": "审一下"}
    assert AgentToolInput.model_validate(normalized).agent_type is None


# ---------- 委派分支 ----------


async def test_agent_id_delegates_with_agent_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_requests, dispatches = _install_fakes(monkeypatch)

    output = (await AgentTool().execute({"agent_id": 7, "prompt": "审一下"}, _ctx())).text

    assert config_requests == [(_WORKSPACE_ID, 7)]
    (dispatch,) = dispatches
    spec = dispatch["spec"]
    assert spec.agent_id == 7
    assert spec.display_name == "delegate-审稿人"
    assert spec.tool_profile == "delegate"
    assert spec.system_prompt == "你是审稿人"
    assert spec.allowed_tools == ("Read", "Write")
    assert spec.model == "reviewer-model"
    assert dispatch["prompt"] == "审一下"
    # 父名单取运行时注入的本轮生效名单,而不是父网关的完整工具边界。
    assert dispatch["parent_allowed_tool_names"] == _ROUND_TOOL_NAMES
    assert dispatch["allowed_skill_names"] == frozenset({"review-skill"})
    assert json.loads(output) == {
        "agent": "delegate-审稿人",
        "report": "委派报告",
        "error": None,
        "verdict": None,
        "verdict_note": None,
    }


async def test_parent_tool_names_fall_back_to_gateway_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, dispatches = _install_fakes(monkeypatch)
    ctx = _ctx(round_tool_names=None)

    await AgentTool().run(AgentToolInput(agent_id=7, prompt="审一下"), ctx)

    assert dispatches[0]["parent_allowed_tool_names"] == ctx.allowed_tool_names


async def test_delegate_display_name_is_truncated_to_column_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行标识落 subagent_runs.member_name(String(120)),Agent 名可达 128 字符。"""
    _, dispatches = _install_fakes(monkeypatch, config=_config(name="甲" * 128))

    await AgentTool().run(AgentToolInput(agent_id=7, prompt="审一下"), _ctx())

    display_name = dispatches[0]["spec"].display_name
    assert display_name.startswith("delegate-")
    assert len(display_name) == 120


@pytest.mark.parametrize(
    ("message", "status_code"),
    [("Agent不存在", 404), ("Agent已停用", 409), ("当前Agent类型不允许绑定这些工具", 403)],
)
async def test_unavailable_agent_returns_chinese_error_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    status_code: int,
) -> None:
    error = AgentException.message(message, status_code=status_code)
    _, dispatches = _install_fakes(monkeypatch, error=error)

    output = await AgentTool().run(AgentToolInput(agent_id=7, prompt="审一下"), _ctx())

    result = json.loads(output)
    assert result["report"] is None
    assert result["error"] == f"无法委派给 agent_id=7 的 Agent:{message}"
    assert dispatches == []


@pytest.mark.parametrize("profile", ["user_agent", "orchestrator", "subagent", None])
async def test_only_lead_profile_can_delegate(
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
) -> None:
    config_requests, dispatches = _install_fakes(monkeypatch)

    output = await AgentTool().run(
        AgentToolInput(agent_id=7, prompt="审一下"), _ctx(profile=profile)
    )

    assert "只有默认 Agent 可以委派自建 Agent" in json.loads(output)["error"]
    assert config_requests == []
    assert dispatches == []


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"prompt": "找代码"}, AgentType.GENERAL_PURPOSE),
        ({"agent_type": "Explore", "prompt": "找代码"}, AgentType.EXPLORE),
    ],
)
async def test_without_agent_id_keeps_builtin_subagent_path(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    expected_type: AgentType,
) -> None:
    config_requests, dispatches = _install_fakes(monkeypatch)
    ctx = _ctx()

    output = (await AgentTool().execute(payload, ctx)).text

    spec = dispatches[0]["spec"]
    assert spec.agent_type is expected_type
    assert spec.agent_id is None
    assert spec.tool_profile is None
    # 内置子代理沿用父网关的工具边界,行为与改造前一致。
    assert dispatches[0]["parent_allowed_tool_names"] == ctx.allowed_tool_names
    assert json.loads(output)["agent"] == expected_type.value
    assert config_requests == []
