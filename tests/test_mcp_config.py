"""MCP server 管理的纯规则：地址规范化、请求头掩码合并、新建输入校验、更新计划与 config_rev、
工具清单差异、管理界面的工具展示，以及权限规则用的 server_key 切分。"""

import pytest

from core.errors import AgentException
from harness.domain.mcp import (
    ApprovalMode,
    McpAuthType,
    McpSecret,
    McpServerConfig,
    McpToolAnnotations,
    McpToolSummary,
    server_key_of,
)
from harness.domain.mcp_config import (
    SECRET_MASK,
    McpServerDraft,
    McpServerView,
    McpWritableServer,
    canonical_server_url,
    diff_tools,
    list_server_tools,
    masked_headers,
    merge_secret_headers,
    plan_update,
    redact_url_query,
)


def _tool(name: str, digest: str = "d1", **annotations: bool) -> McpToolSummary:
    return McpToolSummary(
        name=name,
        description=f"{name} 的描述",
        input_schema_sha256=digest,
        annotations=McpToolAnnotations(**annotations),
    )


def _config(**overrides: object) -> McpServerConfig:
    values: dict[str, object] = {
        "server_id": 7,
        "workspace_id": 1,
        "owner_user_id": None,
        "server_key": "github",
        "url": "https://mcp.example.com/mcp",
        "auth_type": McpAuthType.STATIC_HEADERS,
        "config_rev": 3,
    }
    values.update(overrides)
    return McpServerConfig(**values)  # type: ignore[arg-type]


def _writable(config: McpServerConfig | None = None, **headers: str) -> McpWritableServer:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 12, tzinfo=UTC)
    secret = McpSecret.from_headers(headers or {"Authorization": "Bearer old"})
    view = McpServerView(
        config=config or _config(),
        name="GitHub",
        description="代码托管",
        created_by=1,
        writable=True,
        last_probe_at=now,
        last_probe_error=None,
        tools=(_tool("create_issue"),),
        created_at=now,
        updated_at=now,
        header_names=tuple(secret.as_dict()),
    )
    return McpWritableServer(view=view, secret=secret)


# ---------- 地址规范化 ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://MCP.Example.COM/mcp/", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com:443/mcp", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com:8443/", "https://mcp.example.com:8443"),
        ("https://mcp.example.com/a/b//?x=1", "https://mcp.example.com/a/b?x=1"),
        ("http://Example.com:80/", "http://example.com"),
        ("https://[2001:db8::1]:443/mcp", "https://[2001:db8::1]/mcp"),
    ],
)
def test_canonical_server_url(raw: str, expected: str) -> None:
    assert canonical_server_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "not a url", "https://exa mple.com", "https://example.com/\nx", "//no-scheme", "https://"],
)
def test_canonical_server_url_rejects_garbage(raw: str) -> None:
    with pytest.raises(AgentException) as exc_info:
        canonical_server_url(raw)
    assert exc_info.value.status_code == 400


def test_redact_url_query_hides_possible_credentials() -> None:
    assert redact_url_query("https://h.com/mcp?token=abc") == f"https://h.com/mcp?{SECRET_MASK}"
    assert redact_url_query("https://h.com/mcp") == "https://h.com/mcp"


# ---------- 请求头掩码 ----------


def test_merge_keeps_masked_values_and_replaces_others() -> None:
    merged = merge_secret_headers(
        {"Authorization": SECRET_MASK, "X-Team": "new"},
        {"Authorization": "Bearer old", "X-Old": "gone"},
    )

    assert merged == {"Authorization": "Bearer old", "X-Team": "new"}


def test_merge_rejects_mask_for_unknown_header() -> None:
    with pytest.raises(AgentException, match="不能是掩码") as exc_info:
        merge_secret_headers({"X-New": SECRET_MASK}, {"Authorization": "Bearer old"})

    assert exc_info.value.status_code == 400
    assert "Bearer old" not in str(exc_info.value.details)


def test_masked_headers_only_expose_names() -> None:
    assert masked_headers(["b", "a"]) == {"a": SECRET_MASK, "b": SECRET_MASK}


# ---------- 新建输入 ----------


def test_draft_normalizes_and_validates() -> None:
    draft = McpServerDraft.create(
        server_key="github",
        name="  GitHub  ",
        url="HTTPS://Api.GitHub.com/mcp/",
        private=False,
        description="  ",
        auth_type=McpAuthType.STATIC_HEADERS,
        headers={"Authorization": "Bearer t"},
        tool_allowlist=["a", "a", "b"],
    )

    assert draft.name == "GitHub"
    assert draft.description is None
    assert draft.url == "https://api.github.com/mcp"
    assert draft.tool_allowlist == ("a", "b")
    assert draft.secret is not None and draft.secret.as_dict() == {"Authorization": "Bearer t"}
    assert "Bearer t" not in repr(draft)
    probe_config = draft.probe_config(workspace_id=5, owner_user_id=9)
    assert (probe_config.server_id, probe_config.workspace_id, probe_config.owner_user_id) == (
        None,
        5,
        9,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"server_key": "Bad_Key"}, "server_key 不合法"),
        ({"auth_type": McpAuthType.STATIC_HEADERS}, "至少要配置一个请求头"),
        ({"headers": {"X-A": "1"}}, "只有 static_headers"),
        # M3 放开 OAuth 后，OAuth server 同样不能带静态请求头。
        ({"auth_type": McpAuthType.OAUTH, "headers": {"X-A": "1"}}, "只有 static_headers"),
        ({"name": "   "}, "名称"),
        ({"tool_denylist": ["ok", " "]}, "空的工具名"),
        ({"call_timeout_seconds": 0}, "调用超时"),
        (
            {"auth_type": McpAuthType.STATIC_HEADERS, "headers": {"Cookie": "x"}},
            "请求头名称不合法",
        ),
    ],
)
def test_draft_rejects_invalid_input(overrides: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "server_key": "github",
        "name": "GitHub",
        "url": "https://mcp.example.com",
        "private": True,
    }
    values.update(overrides)

    with pytest.raises(AgentException, match=message) as exc_info:
        McpServerDraft.create(**values)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400


# ---------- 更新计划与 config_rev ----------


def test_display_and_approval_changes_keep_revision_and_skip_probe() -> None:
    update = plan_update(
        _writable(),
        {"name": "新名字", "description": None, "approval_mode": ApprovalMode.NEVER},
    )

    assert update.config.config_rev == 3
    assert not update.revision_changed
    assert not update.needs_probe
    assert not update.secret_changed
    assert (update.name, update.description) == ("新名字", None)
    assert update.config.approval_mode is ApprovalMode.NEVER


@pytest.mark.parametrize(
    "changes",
    [
        {"tool_allowlist": ["create_issue"]},
        {"tool_denylist": ["delete_repo"]},
        {"call_timeout_seconds": 30},
        {"is_enabled": False},
    ],
)
def test_filter_timeout_and_enable_changes_bump_revision_without_probe(
    changes: dict[str, object],
) -> None:
    update = plan_update(_writable(), changes)  # type: ignore[arg-type]

    assert update.config.config_rev == 4
    assert update.revision_changed
    assert not update.needs_probe


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://other.example.com/mcp"},
        {"headers": {"Authorization": "Bearer new"}},
        {"auth_type": McpAuthType.NONE, "headers": {}},
    ],
)
def test_connection_changes_bump_revision_and_probe(changes: dict[str, object]) -> None:
    update = plan_update(_writable(), changes)  # type: ignore[arg-type]

    assert update.config.config_rev == 4
    assert update.needs_probe


def test_same_values_after_normalization_are_not_changes() -> None:
    update = plan_update(
        _writable(),
        {
            "url": "HTTPS://MCP.example.com:443/mcp/",
            "headers": {"Authorization": SECRET_MASK},
            "is_enabled": True,
            "tool_allowlist": [],
        },
    )

    assert not update.revision_changed
    assert not update.needs_probe
    assert not update.secret_changed
    assert update.secret is not None and update.secret.as_dict() == {"Authorization": "Bearer old"}


def test_switching_to_none_drops_stored_headers() -> None:
    update = plan_update(_writable(), {"auth_type": McpAuthType.NONE})

    assert update.secret is None
    assert update.secret_changed and update.needs_probe and update.revision_changed


def test_switching_to_none_rejects_submitted_headers() -> None:
    with pytest.raises(AgentException, match="只有 static_headers"):
        plan_update(
            _writable(),
            {"auth_type": McpAuthType.NONE, "headers": {"Authorization": "Bearer x"}},
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"server_key": "gitlab"}, "server_key 创建后不能修改"),
        ({"private": True}, "共享和私有之间切换"),
    ],
)
def test_update_rejects_immutable_changes(changes: dict[str, object], message: str) -> None:
    with pytest.raises(AgentException, match=message) as exc_info:
        plan_update(_writable(), changes)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400


def test_resubmitting_same_key_and_visibility_is_allowed() -> None:
    update = plan_update(_writable(), {"server_key": "github", "private": False})

    assert not update.revision_changed


# ---------- 工具清单差异与展示 ----------


def test_diff_tools_reports_added_removed_and_schema_changes() -> None:
    before = [_tool("a"), _tool("b"), _tool("c", "old")]
    after = [_tool("c", "new"), _tool("d"), _tool("a")]

    diff = diff_tools(before, after)

    assert diff.added == ("d",)
    assert diff.removed == ("b",)
    assert [(c.name, c.before_sha256, c.after_sha256) for c in diff.schema_changed] == [
        ("c", "old", "new"),
    ]
    assert not diff.is_empty
    assert diff_tools(before, before).is_empty


def test_list_server_tools_uses_turn_rules() -> None:
    config = _config(
        approval_mode=ApprovalMode.TRUST_ANNOTATIONS,
        tool_denylist=("drop",),
    )
    tools = [_tool("read", read_only=True), _tool("drop", destructive=True), _tool("a.b")]

    listings = {item.summary.name: item for item in list_server_tools(config, tools)}

    assert listings["read"].model_name == "mcp__github__read"
    assert not listings["read"].requires_approval
    assert listings["drop"].filtered_out and listings["drop"].requires_approval
    assert listings["a.b"].model_name is not None
    assert listings["a.b"].model_name.startswith("mcp__github__a_b_")


def test_summary_round_trips_through_snapshot_shape() -> None:
    tool = _tool("x", read_only=True)

    assert McpToolSummary.from_dict(tool.to_dict()) == tool


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__github__create_issue", "github"),
        ("mcp__my_srv__a__b", "my_srv"),
        ("mcp__github__", None),
        ("mcp__Bad__x", None),
        ("mcp___x__y", None),
        ("Bash", None),
    ],
)
def test_server_key_of(name: str, expected: str | None) -> None:
    assert server_key_of(name) == expected
