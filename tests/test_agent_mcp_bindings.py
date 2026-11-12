"""自建 Agent 绑定 MCP server（MCP 接入 M2）：领域规则与 Service 规则，不连数据库。

- 领域：原名换模型名、ScopedGrant 的收窄、子代理规格按白名单继承作用域工具；
- agent 域：空白名单与重复 server 报错、运行配置只留可用 server 并换成来源与模型名、
  市场快照不带 MCP 绑定；
- mcp_server 域：require_usable 对私有、停用、跨工作区、不存在的 server 报错，删除 server 时级联解除绑定；
- Agent 工具：子代理按规格、委派按绑定与父回合取交集，把 ScopedGrant 交给启动器。
真实 PostgreSQL 的索引、级联与接口见 test_agent_mcp_bindings_pg.py。
"""

from types import SimpleNamespace
from typing import Any

import pytest

import database.registry  # noqa: F401  构造 ORM 实例前需全部映射在册
from agent.models import AgentMcpServerRecord
from agent.repository import McpBindingRow
from agent.service import (
    AgentDefinitionService,
    AgentMcpBinding,
    AgentMcpBindingInput,
    AgentRuntimeConfig,
)
from core.errors import AgentException
from harness.adapters.tools.builtin.agent import AgentTool
from harness.app.subagent_catalog import get_subagent_spec
from harness.domain.mcp import bound_model_names, model_tool_names
from harness.domain.roles.subagent import AgentType
from harness.domain.tool import ScopedGrant, ToolBinding, ToolContext
from mcp_server.models import McpServerRecord
from mcp_server.service import McpServerService
from tests.fixtures.scoped_tools import FakeScopedTool

pytestmark = pytest.mark.anyio

_WORKSPACE_ID = 2
_USER_ID = 3

_GH_ISSUE = FakeScopedTool("mcp__github__create_issue", source="mcp:github")
_GH_PR = FakeScopedTool("mcp__github__list_prs", source="mcp:github")
_JIRA = FakeScopedTool("mcp__jira__search", source="mcp:jira")
_GRANT = ScopedGrant(
    tools=(_GH_ISSUE, _GH_PR, _JIRA),
    visible=frozenset({_GH_ISSUE.name, _GH_PR.name, _JIRA.name}),
)


# ---------- 领域：原名换模型名与 ScopedGrant ----------


def test_bound_model_names_converts_remote_names_like_the_server_side() -> None:
    names = bound_model_names("github", ["create_issue", "a.b", "create_issue"])

    # 与 server 侧的命名逐个一致：有损清洗加哈希后缀，重复的原名不会被当成撞名剔除。
    expected = model_tool_names("github", ["create_issue", "a.b"]).names
    assert names == frozenset(expected.values())
    assert "mcp__github__create_issue" in names
    assert bound_model_names("github", None) is None


def test_scoped_grant_narrowed_and_bound_keep_all_tools() -> None:
    narrowed = _GRANT.narrowed({_GH_PR.name, "Read"})
    bound = _GRANT.bound(ToolBinding.servers({"mcp:jira": None}))

    assert narrowed == ScopedGrant(tools=_GRANT.tools, visible=frozenset({_GH_PR.name}))
    assert bound == ScopedGrant(tools=_GRANT.tools, visible=frozenset({_JIRA.name}))
    # 收窄只动可见名单，不会把原本不可见的放出来。
    hidden = ScopedGrant(tools=_GRANT.tools, visible=frozenset())
    assert hidden.bound(ToolBinding.all()).visible == frozenset()


@pytest.mark.parametrize(
    ("agent_type", "expected"),
    [
        (AgentType.GENERAL_PURPOSE, {_GH_ISSUE.name, _JIRA.name}),
        (AgentType.EXPLORE, set()),
        (AgentType.PLAN, set()),
        (AgentType.VERIFICATION, set()),
    ],
)
def test_spec_inherits_scoped_only_without_tool_allowlist(
    agent_type: AgentType, expected: set[str]
) -> None:
    spec = get_subagent_spec(agent_type)

    inherited = spec.inherit_scoped(_GRANT, {"Read", _GH_ISSUE.name, _JIRA.name})

    assert inherited is not None
    assert inherited.tools == _GRANT.tools
    assert inherited.visible == frozenset(expected)
    assert spec.inherit_scoped(None, None) is None


# ---------- agent 域：写入校验 ----------


class _FakeAgentRepo:
    """AgentRepository 的内存替身：只给 replace_mcp_bindings 与运行配置用到的方法。"""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.replaced: list[tuple[int, list[McpBindingRow]]] = []
        self.unbound: list[int] = []

    async def get(self, workspace_id: int, agent_id: int) -> Any:
        return self.agent

    async def get_for_update(self, workspace_id: int, agent_id: int) -> Any:
        return self.agent

    async def replace_mcp_bindings(self, agent_id: int, rows: list[McpBindingRow]) -> None:
        self.replaced.append((agent_id, rows))

    async def soft_delete_mcp_bindings_for_server(self, server_id: int) -> None:
        self.unbound.append(server_id)


def _agent(*bindings: tuple[int, list[str] | None]) -> Any:
    return SimpleNamespace(
        id=7,
        is_enabled=True,
        system_prompt="你是审稿人",
        model_name=None,
        name="审稿人",
        description=None,
        tool_bindings=[],
        skill_bindings=[],
        mcp_bindings=[
            AgentMcpServerRecord(agent_id=7, server_id=server_id, tool_names=names)
            for server_id, names in bindings
        ],
    )


def _agent_service(
    monkeypatch: pytest.MonkeyPatch,
    agent: Any,
    *,
    usable: dict[int, str] | None = None,
    error: AgentException | None = None,
) -> tuple[AgentDefinitionService, _FakeAgentRepo, list[list[int]]]:
    """工作区角色放行；require_usable 记下收到的 server_ids，按 usable 返回或抛 error。"""
    checks: list[list[int]] = []

    async def _role(*args: Any, **kwargs: Any) -> None:
        return None

    async def _require_usable(
        self: McpServerService, workspace_id: int, server_ids: list[int], *, actor_user_id: int
    ) -> dict[int, str]:
        checks.append(list(server_ids))
        if error is not None:
            raise error
        return usable or {}

    service = AgentDefinitionService(db=None)  # type: ignore[arg-type]
    repo = _FakeAgentRepo(agent)
    service.repo = repo  # type: ignore[assignment]
    monkeypatch.setattr(service.workspace_service, "require_workspace_role", _role)
    monkeypatch.setattr(McpServerService, "require_usable", _require_usable)
    return service, repo, checks


async def test_empty_tool_allowlist_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    service, repo, checks = _agent_service(monkeypatch, _agent())

    with pytest.raises(AgentException) as exc_info:
        await service.replace_mcp_bindings(
            _WORKSPACE_ID, 7, _USER_ID, [AgentMcpBindingInput(server_id=11, tool_names=())]
        )

    assert exc_info.value.status_code == 400
    assert "不能为空列表" in exc_info.value.message
    assert exc_info.value.details == {"server_id": 11}
    # 规范化失败就不再查 server，也不写库。
    assert checks == [] and repo.replaced == []


async def test_duplicate_server_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    service, repo, _ = _agent_service(monkeypatch, _agent())

    with pytest.raises(AgentException) as exc_info:
        await service.replace_mcp_bindings(
            _WORKSPACE_ID,
            7,
            _USER_ID,
            [AgentMcpBindingInput(server_id=11), AgentMcpBindingInput(server_id=11)],
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.details == {"server_ids": [11]}
    assert repo.replaced == []


async def test_replace_checks_usable_then_writes_deduplicated_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, checks = _agent_service(monkeypatch, _agent(), usable={11: "github", 12: "jira"})

    await service.replace_mcp_bindings(
        _WORKSPACE_ID,
        7,
        _USER_ID,
        [
            AgentMcpBindingInput(server_id=11, tool_names=("b", "a", "b")),
            AgentMcpBindingInput(server_id=12),
        ],
    )

    assert checks == [[11, 12]]
    assert repo.replaced == [
        (7, [McpBindingRow(11, ["b", "a"]), McpBindingRow(12, None)]),
    ]


async def test_replace_propagates_require_usable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = AgentException.message("私有 MCP server 不能绑定到 Agent", status_code=400)
    service, repo, _ = _agent_service(monkeypatch, _agent(), error=error)

    with pytest.raises(AgentException) as exc_info:
        await service.replace_mcp_bindings(
            _WORKSPACE_ID, 7, _USER_ID, [AgentMcpBindingInput(server_id=11)]
        )

    assert exc_info.value is error
    assert repo.replaced == []


async def test_unbind_mcp_server_delegates_to_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    service, repo, _ = _agent_service(monkeypatch, _agent())

    await service.unbind_mcp_server(11)

    assert repo.unbound == [11]


# ---------- agent 域：运行配置与市场快照 ----------


async def test_runtime_config_keeps_usable_servers_as_sources_with_model_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 12 已停用或已删除：enabled_shared_keys 不返回它，运行配置里直接略过、不报错。
    agent = _agent((12, None), (11, ["create_issue", "a.b"]), (13, None))
    service, _, _ = _agent_service(monkeypatch, agent)

    async def _keys(self: McpServerService, workspace_id: int, server_ids: list[int]):
        assert sorted(server_ids) == [11, 12, 13]
        return {11: "github", 13: "jira"}

    async def _no_tools(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(McpServerService, "enabled_shared_keys", _keys)
    monkeypatch.setattr(service.tool_catalog_service, "get_runtime_names", _no_tools)
    monkeypatch.setattr(service.skill_service, "get_enabled_names_by_ids", _no_tools)

    config = await service.get_runtime_config(_WORKSPACE_ID, 7)

    github_names = model_tool_names("github", ["create_issue", "a.b"]).names.values()
    assert config.mcp_bindings == (
        AgentMcpBinding(source="mcp:github", tool_names=frozenset(github_names)),
        AgentMcpBinding(source="mcp:jira", tool_names=None),
    )
    assert config.tool_binding() == ToolBinding.servers(
        {"mcp:github": config.mcp_bindings[0].tool_names, "mcp:jira": None}
    )


def test_runtime_config_defaults_to_no_mcp_bindings() -> None:
    config = AgentRuntimeConfig(
        agent_id=7,
        system_prompt="p",
        model_name=None,
        tool_runtime_names=(),
        skill_ids=(),
        skill_names=(),
        name="n",
        description=None,
    )

    assert config.mcp_bindings == ()
    assert config.tool_binding() == ToolBinding.none()


async def test_market_snapshot_does_not_export_mcp_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _agent_service(monkeypatch, _agent((11, None)))

    async def _keys(*args: Any, **kwargs: Any) -> dict[int, str]:
        return {}

    monkeypatch.setattr(service.tool_catalog_service, "get_tool_keys_by_ids", _keys)

    snapshot = await service.build_snapshot(_WORKSPACE_ID, 7)

    # server 是工作区私有的，地址和凭证不能跨工作区带走，快照里不出现任何 MCP 字段。
    assert snapshot == {
        "agent": {
            "name": "审稿人",
            "description": None,
            "system_prompt": "你是审稿人",
            "tool_keys": [],
        },
        "skills": [],
    }


# ---------- mcp_server 域：require_usable 与删除级联 ----------


class _FakeServerRepo:
    """McpServerRepository 的内存替身：只按工作区与软删过滤，不区分共享与私有。"""

    def __init__(self, *rows: McpServerRecord) -> None:
        self.rows = {row.id: row for row in rows}

    async def list_by_ids(self, workspace_id: int, server_ids: list[int]) -> list[McpServerRecord]:
        return [
            self.rows[server_id]
            for server_id in sorted(server_ids)
            if server_id in self.rows
            and self.rows[server_id].workspace_id == workspace_id
            and not self.rows[server_id].is_deleted
        ]


def _server(server_id: int, key: str, **overrides: Any) -> McpServerRecord:
    values: dict[str, Any] = {
        "id": server_id,
        "workspace_id": _WORKSPACE_ID,
        "owner_user_id": None,
        "server_key": key,
        "is_enabled": True,
        "is_deleted": False,
    }
    values.update(overrides)
    return McpServerRecord(**values)


def _server_service() -> McpServerService:
    service = McpServerService(db=None)  # type: ignore[arg-type]
    service.repo = _FakeServerRepo(  # type: ignore[assignment]
        _server(11, "github"),
        _server(12, "mine", owner_user_id=_USER_ID),
        _server(13, "theirs", owner_user_id=99),
        _server(14, "off", is_enabled=False),
        _server(15, "elsewhere", workspace_id=_WORKSPACE_ID + 1),
        _server(16, "gone", is_deleted=True),
    )
    return service


async def test_require_usable_returns_server_keys_for_enabled_shared_servers() -> None:
    keys = await _server_service().require_usable(_WORKSPACE_ID, [11, 11], actor_user_id=_USER_ID)

    assert keys == {11: "github"}
    assert await _server_service().require_usable(_WORKSPACE_ID, [], actor_user_id=_USER_ID) == {}


@pytest.mark.parametrize(
    ("server_ids", "status_code", "message", "details"),
    [
        ([11, 12], 400, "私有 MCP server 不能绑定到 Agent，只能绑定工作区共享的 server", [12]),
        ([14], 409, "MCP server 已停用，不能绑定", [14]),
        # 别人的私有 server、别的工作区、已删除、不存在一律 404，同一句话，不泄露存在与否。
        ([13], 404, "MCP server 不存在", [13]),
        ([15], 404, "MCP server 不存在", [15]),
        ([16], 404, "MCP server 不存在", [16]),
        ([11, 404], 404, "MCP server 不存在", [404]),
    ],
    ids=["own_private", "disabled", "others_private", "cross_workspace", "deleted", "missing"],
)
async def test_require_usable_rejects_unbindable_servers(
    server_ids: list[int], status_code: int, message: str, details: list[int]
) -> None:
    with pytest.raises(AgentException) as exc_info:
        await _server_service().require_usable(
            _WORKSPACE_ID, server_ids, actor_user_id=_USER_ID
        )

    assert exc_info.value.status_code == status_code
    assert exc_info.value.message == message
    assert exc_info.value.details == {"server_ids": details}


async def test_enabled_shared_keys_skips_unusable_servers_without_error() -> None:
    keys = await _server_service().enabled_shared_keys(_WORKSPACE_ID, [11, 12, 13, 14, 15, 16, 17])

    assert keys == {11: "github"}


# ---------- Agent 工具：把 ScopedGrant 交给子代理启动器 ----------


class _Launcher:
    """子代理启动器替身：记下每次收到的规格、父名单与作用域工具。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, session_id: int, prompt: str, spec: Any, **kwargs: Any) -> str:
        self.calls.append({"spec": spec, **kwargs})
        return "报告"


def _tool_ctx(launcher: _Launcher, round_names: set[str], **overrides: Any) -> ToolContext:
    values: dict[str, Any] = {
        "session_id": 1,
        "db": object(),
        "workspace_id": _WORKSPACE_ID,
        "user_id": _USER_ID,
        "profile": "lead",
        "allowed_tool_names": frozenset({"Read", "Agent", *_GRANT.visible}),
        "round_tool_names": frozenset(round_names),
        "subagents": launcher,
        "scoped": _GRANT,
    }
    values.update(overrides)
    return ToolContext(**values)


@pytest.mark.parametrize(
    ("agent_type", "expected"),
    [("general-purpose", {_GH_ISSUE.name, _JIRA.name}), ("Explore", set())],
)
async def test_subagent_inherits_round_visible_scoped_by_spec(
    agent_type: str, expected: set[str]
) -> None:
    launcher = _Launcher()
    ctx = _tool_ctx(launcher, {"Read", "Agent", _GH_ISSUE.name, _JIRA.name})

    await AgentTool().execute({"agent_type": agent_type, "prompt": "去查"}, ctx)

    (call,) = launcher.calls
    assert call["scoped"] == ScopedGrant(tools=_GRANT.tools, visible=frozenset(expected))


def _delegate_config(*bindings: AgentMcpBinding) -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        agent_id=7,
        system_prompt="你是审稿人",
        model_name=None,
        tool_runtime_names=("Read",),
        skill_ids=(),
        skill_names=(),
        name="审稿人",
        description=None,
        mcp_bindings=bindings,
    )


@pytest.mark.parametrize(
    ("round_names", "expected"),
    [
        # 绑定 github 全部 + jira；父回合本轮只见 create_issue 与 jira：交集为这两个。
        ({"Read", "Agent", _GH_ISSUE.name, _JIRA.name}, {_GH_ISSUE.name, _JIRA.name}),
        # Plan 模式：父回合的本轮名单本来就剔除了 MCP 工具，交集为空。
        ({"Read", "Agent"}, set()),
    ],
    ids=["intersection", "plan_mode"],
)
async def test_delegate_gets_binding_intersect_parent_round(
    monkeypatch: pytest.MonkeyPatch, round_names: set[str], expected: set[str]
) -> None:
    config = _delegate_config(
        AgentMcpBinding(source="mcp:github", tool_names=None),
        AgentMcpBinding(source="mcp:jira", tool_names=frozenset({_JIRA.name})),
    )

    class _Agents:
        def __init__(self, db: object) -> None:
            pass

        async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
            return config

    monkeypatch.setattr("agent.service.AgentDefinitionService", _Agents)
    launcher = _Launcher()

    await AgentTool().execute({"agent_id": 7, "prompt": "审"}, _tool_ctx(launcher, round_names))

    (call,) = launcher.calls
    assert call["scoped"] == ScopedGrant(tools=_GRANT.tools, visible=frozenset(expected))
    assert call["spec"].tool_profile == "delegate"


async def test_no_parent_scoped_hands_none_to_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _delegate_config(AgentMcpBinding(source="mcp:github", tool_names=None))

    class _Agents:
        def __init__(self, db: object) -> None:
            pass

        async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
            return config

    monkeypatch.setattr("agent.service.AgentDefinitionService", _Agents)
    launcher = _Launcher()
    ctx = _tool_ctx(launcher, {"Read", "Agent"}, scoped=None)

    await AgentTool().execute({"agent_id": 7, "prompt": "审"}, ctx)
    await AgentTool().execute({"agent_type": "general-purpose", "prompt": "查"}, ctx)

    assert [call["scoped"] for call in launcher.calls] == [None, None]
