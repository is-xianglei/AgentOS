"""工具能力策略的阶段 0 回归测试。"""

from types import SimpleNamespace

import pytest

from core.errors import AgentException
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.domain.tool import ToolPolicy, ToolProfile
from tools.service import ToolCatalogService

pytestmark = pytest.mark.anyio


# 原 tools/policy.py 的按名查询函数已并入 ToolSet；这里保留同名辅助函数，断言不变。
def get_tool_policy(tool_name: str) -> ToolPolicy:
    return builtin_tool_set().policy_for(tool_name)


def can_bind_tool(tool_name: str, profile: ToolProfile) -> bool:
    return get_tool_policy(tool_name).can_bind(profile)


def can_call_tool(tool_name: str, profile: ToolProfile) -> bool:
    return get_tool_policy(tool_name).can_call(profile)


def test_user_agent_cannot_bind_control_plane_tools() -> None:
    forbidden = (
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        "WritePlan",
        "Agent",
        "TeamCreate",
        "TeamSpawn",
    )

    assert all(not can_bind_tool(name, "user_agent") for name in forbidden)


def test_user_agent_can_bind_leaf_and_task_tools() -> None:
    allowed = ("echo", "Read", "Glob", "Grep", "Weather", "TaskGet", "TaskList")

    assert all(can_bind_tool(name, "user_agent") for name in allowed)


def test_every_registered_tool_has_a_matching_policy() -> None:
    tool_set = builtin_tool_set()

    assert all(tool_set.policy_for(name).category != "unknown" for name in tool_set.names)
    assert all(tool.policy.name == tool.name for tool in tool_set.tools)


def test_user_agent_cannot_bind_privileged_shell_or_skill_runtime_tools() -> None:
    assert not can_bind_tool("Bash", "user_agent")
    assert not can_bind_tool("SkillRun", "user_agent")
    # SkillRun 由已绑定 Skill 在运行时自动补齐，不能靠直接 tool_id 绕过资产边界。
    assert can_call_tool("SkillRun", "user_agent")


def test_user_agent_cannot_call_control_plane_tools_even_if_invoked_directly() -> None:
    forbidden = ("Agent", "TeamCreate", "TeamSpawn", "AskUserQuestion")

    assert all(not can_call_tool(name, "user_agent") for name in forbidden)


def test_lead_can_bind_and_call_control_plane_tools() -> None:
    control_plane = ("Agent", "TeamCreate", "TeamSpawn", "AskUserQuestion")

    assert all(can_bind_tool(name, "lead") for name in control_plane)
    assert all(can_call_tool(name, "lead") for name in control_plane)


def test_subagent_cannot_bind_or_call_control_plane_tools() -> None:
    control_plane = ("Agent", "TeamCreate", "TeamSpawn", "AskUserQuestion")

    assert all(not can_bind_tool(name, "subagent") for name in control_plane)
    assert all(not can_call_tool(name, "subagent") for name in control_plane)


@pytest.mark.parametrize("profile", [
    "lead",
    "user_agent",
    "workflow_node",
    "subagent",
    "teammate",
    "orchestrator",
])
def test_resolver_preserves_order_deduplicates_and_filters_unknown(profile) -> None:
    resolver = builtin_tool_set()

    assert resolver.callable_names(
        ["Read", "AskUserQuestion", "Read", "missing-tool", "TaskList"],
        profile,
    ) == (
        ("Read", "AskUserQuestion", "TaskList")
        if profile in {"lead", "orchestrator"}
        else ("Read", "TaskList")
    )


def test_resolver_strict_mode_reports_denied_names() -> None:
    resolver = builtin_tool_set()

    with pytest.raises(ValueError, match="profile=user_agent"):
        resolver.callable_names(
            ["Read", "TeamCreate", "Unknown"], "user_agent", strict=True
        )

    assert resolver.denied_names(["Read", "TeamCreate", "Unknown"], "user_agent") == (
        "TeamCreate",
        "Unknown",
    )


@pytest.mark.parametrize("tool_name", ["Bash", "Write", "Edit"])
def test_high_risk_tool_matrix(tool_name: str) -> None:
    assert get_tool_policy(tool_name).requires_approval is True
    if tool_name == "Bash":
        assert not can_bind_tool(tool_name, "user_agent")
    else:
        assert can_bind_tool(tool_name, "user_agent")
    assert can_call_tool(tool_name, "user_agent") is (tool_name != "Bash")


@pytest.mark.parametrize("tool_name", ["Skill", "SkillResource", "SkillRun"])
def test_skill_tools_need_bound_skill_metadata(tool_name: str) -> None:
    policy = get_tool_policy(tool_name)

    assert policy.requires_bound_skill is True
    assert not can_bind_tool(tool_name, "user_agent")
    # user_agent 的调用期放行只表示“可由已绑定 Skill 自动注入”，不能代替绑定校验。
    assert can_call_tool(tool_name, "user_agent")


class _FakeCatalogRepository:
    def __init__(self, records: list[SimpleNamespace]):
        self.records = records

    async def list_by_ids(self, tool_ids: list[int]) -> list[SimpleNamespace]:
        return [record for record in self.records if record.id in tool_ids]


def _catalog_service(*records: SimpleNamespace) -> ToolCatalogService:
    service = ToolCatalogService.__new__(ToolCatalogService)
    service.repo = _FakeCatalogRepository(list(records))
    return service


async def test_catalog_rejects_control_plane_binding_for_user_agent() -> None:
    service = _catalog_service(
        SimpleNamespace(id=7, is_enabled=True, runtime_name="TeamCreate")
    )

    with pytest.raises(AgentException, match="不允许绑定") as exc_info:
        await service.require_bindable_tool_ids([7], profile="user_agent")

    assert exc_info.value.status_code == 403
    assert exc_info.value.details["tool_names"] == ["TeamCreate"]


async def test_catalog_accepts_read_tool_binding_for_user_agent() -> None:
    service = _catalog_service(SimpleNamespace(id=8, is_enabled=True, runtime_name="Read"))

    assert await service.require_bindable_tool_ids([8], profile="user_agent") == [8]
