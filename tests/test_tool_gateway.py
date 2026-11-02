"""ToolGateway 的 profile、身份和工具暴露边界测试。"""

import pytest
from pathlib import Path

from core.events import ORCHESTRATOR_ACTOR
from tools.gateway import ToolGateway
from tools.registry import build_tool_registry
from tools.shell_policy import ShellAccess

pytestmark = pytest.mark.anyio


def test_lead_gateway_exposes_control_plane_tools() -> None:
    gateway = ToolGateway.create(None, "lead", principal_name="lead")

    assert {"Agent", "TeamCreate", "TeamSpawn", "AskUserQuestion"} <= (
        gateway.registry.names
    )
    assert {"Read", "TaskList", "Write"} <= gateway.registry.names


def test_user_agent_gateway_exposes_leaf_tools_but_hides_control_plane() -> None:
    gateway = ToolGateway.create(None, "user_agent", principal_name="agent:7")

    assert {"Read", "TaskGet", "TaskList", "Write", "Edit"} <= gateway.registry.names
    assert not (
        {"Agent", "TeamCreate", "TeamSpawn", "AskUserQuestion", "EnterPlanMode"}
        & gateway.registry.names
    )
    assert "Bash" not in gateway.registry.names


async def test_gateway_run_uses_its_fixed_profile_and_principal(monkeypatch) -> None:
    gateway = ToolGateway.create(
        None,
        "user_agent",
        principal_name="agent:7",
        requested_names=["Read"],
    )
    captured: dict[str, object] = {}

    async def fake_run(*args, **kwargs) -> str:
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(gateway.service, "run", fake_run)

    result = await gateway.run(
        1,
        "Read",
        {"path": "README.md"},
        actor=ORCHESTRATOR_ACTOR,
    )

    assert result == "ok"
    assert captured["profile"] == "user_agent"
    assert captured["principal_name"] == "agent:7"


async def test_gateway_run_cannot_expand_frozen_resource_boundaries(monkeypatch, tmp_path) -> None:
    gateway = ToolGateway.create(
        None,
        "user_agent",
        principal_name="agent:7",
        requested_names=["Read"],
        agent_id=7,
        allowed_skill_names=frozenset({"bound-skill"}),
        shell_access=ShellAccess.READ_ONLY,
        shell_tmp_root=tmp_path / "safe",
    )
    captured: dict[str, object] = {}

    async def fake_run(*args, **kwargs) -> str:
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(gateway.service, "run", fake_run)

    result = await gateway.run(
        1,
        "Read",
        {"path": "README.md"},
        agent_id=999,
        allowed_skill_names=frozenset({"forged-skill"}),
        shell_access=ShellAccess.FULL,
        shell_tmp_root=Path("/tmp/forged"),
    )

    assert result == "ok"
    assert captured["agent_id"] == 7
    assert captured["allowed_skill_names"] == frozenset({"bound-skill"})
    assert captured["shell_access"] is ShellAccess.READ_ONLY
    assert captured["shell_tmp_root"] == tmp_path / "safe"


async def test_gateway_start_call_cannot_forge_agent_id(monkeypatch) -> None:
    gateway = ToolGateway.create(
        None,
        "user_agent",
        principal_name="agent:7",
        requested_names=["Read"],
        agent_id=7,
    )
    captured: dict[str, object] = {}

    async def fake_start_call(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(gateway.service, "start_call", fake_start_call)

    await gateway.start_call(
        1,
        "Read",
        {"path": "README.md"},
        agent_id=999,
    )

    assert captured["agent_id"] == 7


@pytest.mark.parametrize(
    "tool_name", ["TeamCreate", "AskUserQuestion", "SkillRun", "missing-tool"]
)
def test_user_agent_gateway_strict_creation_rejects_denied_or_unknown_tools(
    tool_name: str,
) -> None:
    with pytest.raises(ValueError, match="profile=user_agent"):
        ToolGateway.create(
            None,
            "user_agent",
            principal_name="agent:7",
            requested_names=["Read", tool_name],
        )


def test_lead_gateway_accepts_explicit_control_plane_allowlist() -> None:
    gateway = ToolGateway.create(
        None,
        "lead",
        principal_name="lead",
        requested_names=["Read", "TeamCreate", "AskUserQuestion"],
    )

    assert gateway.registry.names == frozenset(
        {"Read", "TeamCreate", "AskUserQuestion"}
    )


def test_user_agent_from_registry_rejects_control_plane_tools() -> None:
    registry = build_tool_registry().only("Read", "TeamCreate")

    with pytest.raises(ValueError, match="profile=user_agent"):
        ToolGateway.from_registry(
            None,
            registry,
            "user_agent",
            principal_name="agent:7",
        )


def test_skill_runtime_tool_is_runtime_injected_not_user_bound() -> None:
    registry = build_tool_registry().only("SkillRun")

    gateway = ToolGateway.from_registry(
        None,
        registry,
        "user_agent",
        principal_name="agent:7",
        allowed_skill_names=frozenset({"approved-skill"}),
    )

    assert gateway.registry.names == frozenset({"SkillRun"})
