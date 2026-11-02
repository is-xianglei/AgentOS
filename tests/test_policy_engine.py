"""统一策略门面的静态与动态决策测试。"""

import pytest

from tools.policy_engine import PolicyEngine


def test_policy_engine_static_gate_checks_profile_and_skill() -> None:
    engine = PolicyEngine(None)

    denied = engine.admit_tool("AskUserQuestion", "user_agent")
    assert denied.allowed is False
    assert denied.behavior == "deny"
    assert "profile=user_agent" in (denied.reason or "")

    missing_skill = engine.admit_tool(
        "SkillRun",
        "user_agent",
        input_args={"skill_name": "private"},
        allowed_skill_names=frozenset({"public"}),
    )
    assert missing_skill.allowed is False
    assert "已绑定的 skill" in (missing_skill.reason or "")

    allowed_skill = engine.admit_tool(
        "SkillRun",
        "user_agent",
        input_args={"skill_name": "public"},
        allowed_skill_names=frozenset({"public"}),
    )
    assert allowed_skill.allowed is True


@pytest.mark.anyio
async def test_policy_engine_dynamic_decision_preserves_three_states(monkeypatch) -> None:
    engine = PolicyEngine(None)

    async def evaluate(_session_id, _tool_name, _input, **_kwargs):
        return "ask"

    monkeypatch.setattr(engine.permission_service, "evaluate", evaluate)
    decision = await engine.authorize(
        1,
        "Write",
        "user_agent",
        input_args={"path": "a.txt", "content": "x"},
        workspace_id=3,
    )

    assert decision.allowed is False
    assert decision.behavior == "ask"
    assert decision.requires_approval is True


@pytest.mark.anyio
async def test_policy_engine_static_denial_short_circuits_permission(monkeypatch) -> None:
    engine = PolicyEngine(None)
    called = False

    async def evaluate(*_args, **_kwargs):
        nonlocal called
        called = True
        return "allow"

    monkeypatch.setattr(engine.permission_service, "evaluate", evaluate)
    decision = await engine.admit_call(1, "TeamCreate", "user_agent")

    assert decision.allowed is False
    assert decision.behavior == "deny"
    assert called is False
