"""delegate 档位的工具策略矩阵,以及 TeamSpawn 在派遣时校验 agent_type。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.errors import AgentException
from harness.adapters.tools.builtin import team as team_tools
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.domain.roles.subagent import GLOBAL_SUBAGENT_DENIED_TOOLS, AgentType
from harness.domain.tool import ToolContext, requires_skill_allowlist, tool_policy

pytestmark = pytest.mark.anyio

_ALLOWED_TEXT = "general-purpose、Explore、Plan、verification"


# ---------- delegate 档位 ----------


@pytest.mark.parametrize("tool_name", sorted(builtin_tool_set().names))
def test_delegate_callable_matrix_matches_workflow_node(tool_name: str) -> None:
    """初始取值逐个工具对齐 workflow_node。"""
    policy = builtin_tool_set().policy_for(tool_name)

    assert policy.can_call("delegate") is policy.can_call("workflow_node")


def test_new_tool_is_not_callable_by_delegate_by_default() -> None:
    """新增工具沿用默认档位集合时,其他档位照常可调,被委派的 Agent 不可调。"""
    new_policy = tool_policy("NewTool", "leaf")

    assert new_policy.can_call("workflow_node") is True
    assert new_policy.can_call("delegate") is False
    assert builtin_tool_set().policy_for("UnregisteredTool").can_call("delegate") is False


@pytest.mark.parametrize(
    "tool_name",
    ["Agent", "TeamCreate", "TeamSpawn", "AskUserQuestion", "EnterPlanMode", "SendMessage"],
)
def test_delegate_cannot_call_control_plane_tools(tool_name: str) -> None:
    assert builtin_tool_set().policy_for(tool_name).can_call("delegate") is False


def test_delegated_agent_cannot_call_agent_tool() -> None:
    """深度为 1:全局子代理黑名单与 delegate 档位两道边界都拦住 Agent 工具。"""
    assert "Agent" in GLOBAL_SUBAGENT_DENIED_TOOLS
    assert builtin_tool_set().policy_for("Agent").can_call("delegate") is False


def test_delegate_must_carry_skill_allowlist() -> None:
    assert requires_skill_allowlist("delegate") is True


# ---------- TeamSpawn 派遣时校验 agent_type ----------


@pytest.mark.parametrize("agent_type", ["explore", "custom-agent", ""])
def test_team_spawn_rejects_unknown_agent_type_with_allowed_values(agent_type: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        team_tools.TeamSpawnInput(name="w", role="执行", prompt="干活", agent_type=agent_type)

    message = str(exc_info.value)
    assert "队友子代理类型无效" in message
    assert _ALLOWED_TEXT in message


def test_runtime_validation_reports_team_spawn_agent_type_error() -> None:
    """主运行时在执行前校验入参,非法值作为工具结果回给模型,而不是落库后在唤醒时失败。"""
    payload = {"name": "w", "role": "执行", "prompt": "干活", "agent_type": "explore"}

    with pytest.raises(AgentException) as exc_info:
        builtin_tool_set().validate_input("TeamSpawn", payload)

    assert exc_info.value.message == "工具参数校验失败"
    assert _ALLOWED_TEXT in exc_info.value.details["errors"][0]["msg"]


def _install_team_service(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    spawned: list[dict] = []

    class _FakeTeamService:
        def __init__(self, db: object) -> None:
            pass

        async def spawn_member(self, session_id: int, **kwargs: object) -> SimpleNamespace:
            spawned.append(kwargs)
            return SimpleNamespace()

    monkeypatch.setattr(team_tools, "TeamService", _FakeTeamService)
    monkeypatch.setattr(team_tools, "_member_data", lambda member: {})
    return spawned


async def test_team_spawn_with_invalid_agent_type_never_reaches_team_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = _install_team_service(monkeypatch)
    payload = {"name": "w", "role": "执行", "prompt": "干活", "agent_type": "Reviewer"}

    output = await team_tools.TeamSpawnTool().execute(
        payload, ToolContext(session_id=1, db=None, principal_name="lead")
    )

    assert output.startswith("error:")
    assert "队友子代理类型无效" in output
    assert spawned == []


@pytest.mark.parametrize("agent_type", [item.value for item in AgentType])
async def test_team_spawn_accepts_every_builtin_agent_type(
    monkeypatch: pytest.MonkeyPatch,
    agent_type: str,
) -> None:
    spawned = _install_team_service(monkeypatch)
    args = team_tools.TeamSpawnInput(name="w", role="执行", prompt="干活", agent_type=agent_type)

    await team_tools.TeamSpawnTool().run(
        args, ToolContext(session_id=1, db=None, principal_name="lead")
    )

    assert spawned[0]["agent_type"] == agent_type
