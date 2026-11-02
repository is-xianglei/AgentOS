"""PermissionService 默认审批策略与规则优先级测试。"""

from types import SimpleNamespace

import pytest

from permission.service import PermissionService


def test_default_behavior_comes_from_tool_policy() -> None:
    service = PermissionService(None)

    assert service.default_behavior("Write") == "ask"
    assert service.default_behavior("Edit") == "ask"
    assert service.default_behavior("Bash") == "ask"
    assert service.default_behavior("SkillRun") == "ask"
    assert service.default_behavior("Read") == "allow"
    assert service.default_behavior("unknown-tool") == "allow"


def test_builtin_agent_default_can_override_risky_tool() -> None:
    service = PermissionService(None)

    assert service.default_behavior("Bash", "verification") == "allow"
    assert service.default_behavior("Bash", "general-purpose") == "ask"


@pytest.mark.anyio
async def test_evaluate_prefers_session_rule_then_global_then_default() -> None:
    service = PermissionService(None)
    calls: list[tuple[str, int | None, int | None]] = []

    class FakeRepository:
        async def find_match(
            self,
            scope: str,
            session_id: int | None,
            tool_name: str,
            agent_type: str | None = None,
            workspace_id: int | None = None,
        ):
            calls.append((scope, session_id, workspace_id))
            if scope == "session":
                return SimpleNamespace(behavior="allow")
            return SimpleNamespace(behavior="deny")

    service.repo = FakeRepository()

    assert await service.evaluate(42, "Write", workspace_id=7) == "allow"
    assert calls == [("session", 42, 7)]


@pytest.mark.anyio
async def test_evaluate_uses_workspace_scoped_global_rule() -> None:
    service = PermissionService(None)
    calls: list[tuple[str, int | None, int | None]] = []

    class FakeRepository:
        async def find_match(
            self,
            scope: str,
            session_id: int | None,
            tool_name: str,
            agent_type: str | None = None,
            workspace_id: int | None = None,
        ):
            calls.append((scope, session_id, workspace_id))
            if scope == "global":
                return SimpleNamespace(behavior="deny")
            return None

    service.repo = FakeRepository()

    assert await service.evaluate(42, "Read", workspace_id=7) == "deny"
    assert calls == [("session", 42, 7), ("global", None, 7)]
