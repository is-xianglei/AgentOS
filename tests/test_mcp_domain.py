"""MCP 领域规则：命名算法的性质、审批真值表、server_key 校验、授权分区与配置值类型。"""

import itertools
import random
import re

import pytest

from core.errors import AgentException
from harness.domain.mcp import (
    MCP_PREFIX,
    MODEL_TOOL_NAME_MAX,
    SERVER_KEY_MAX,
    ApprovalMode,
    McpAuthType,
    McpSecret,
    McpServerConfig,
    McpToolAnnotations,
    auth_partition,
    mcp_tool_policy,
    model_tool_names,
    requires_approval,
    schema_digest,
    validate_server_key,
)
from harness.domain.tool import ALL_PROFILES_AND_DELEGATE

_LEGAL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")

_SAMPLE_NAMES = [
    "create_issue",
    "list-prs",
    "a.b",
    "a_b",
    "工具",
    "with space",
    "x" * 80,
    "y" * 33,
    "z" * 34,
    "CamelCase",
    "dots.and-dashes_ok",
    "emoji😀",
]


def test_known_inputs_get_fixed_names() -> None:
    """已知输入得到固定的期望名：原样合法的不加后缀，有损清洗与超长加原名 sha1 前 8 位。"""
    naming = model_tool_names("github", ["create_issue", "list-prs", "a.b", "x" * 80])

    assert naming.names == {
        "a.b": "mcp__github__a_b_69f6642c",
        "create_issue": "mcp__github__create_issue",
        "list-prs": "mcp__github__list-prs",
        "x" * 80: "mcp__github__" + "x" * 42 + "_9a163175",
    }
    assert naming.collided == ()


def test_names_are_legal_and_bounded() -> None:
    """模型名只含 [A-Za-z0-9_-]、不超过 64 个字符、以 mcp__{key}__ 开头。"""
    for key in ("a", "github", "k" * SERVER_KEY_MAX):
        naming = model_tool_names(key, _SAMPLE_NAMES)
        for model_name in naming.names.values():
            assert len(model_name) <= MODEL_TOOL_NAME_MAX
            assert _LEGAL_NAME.fullmatch(model_name)
            assert model_name.startswith(f"{MCP_PREFIX}{key}__")


def test_names_do_not_depend_on_order_or_siblings() -> None:
    """模型名与输入顺序无关，也不受同一 server 上其他工具的影响。"""
    expected = model_tool_names("jira", _SAMPLE_NAMES).names
    rng = random.Random(7)
    for _ in range(20):
        shuffled = _SAMPLE_NAMES[:]
        rng.shuffle(shuffled)
        assert model_tool_names("jira", shuffled).names == expected
    for name in _SAMPLE_NAMES:
        assert model_tool_names("jira", [name]).names == {name: expected[name]}


def test_different_servers_never_share_names() -> None:
    """不同 server_key 产出的模型名互不相同（前缀用 __ 分隔，key 里不允许 __）。"""
    keys = ["a", "a_b", "ab", "a1", "github", "git", "hub"]
    seen: dict[str, str] = {}
    for key in keys:
        for model_name in model_tool_names(key, _SAMPLE_NAMES).names.values():
            assert model_name not in seen, (key, seen.get(model_name))
            seen[model_name] = key


def test_lossy_cleaning_adds_suffix_even_when_short() -> None:
    """清洗有损时一律加后缀：`a.b` 与原样合法的 `a_b` 不会撞名。"""
    names = model_tool_names("s", ["a.b", "a_b"]).names

    assert names["a_b"] == "mcp__s__a_b"
    assert names["a.b"].startswith("mcp__s__a_b_")
    assert names["a.b"] != names["a_b"]


def test_truncation_boundary() -> None:
    """恰好 64 个字符不截断，多一个字符就截短并加后缀，总长仍是 64。"""
    prefix_len = len("mcp__s__")
    fits = "t" * (MODEL_TOOL_NAME_MAX - prefix_len)
    over = fits + "t"

    names = model_tool_names("s", [fits, over]).names

    assert names[fits] == f"mcp__s__{fits}"
    assert len(names[over]) == MODEL_TOOL_NAME_MAX
    assert names[over] != names[fits]


def test_colliding_names_are_both_dropped() -> None:
    """同一 server 内算出相同模型名的工具都剔除并交给调用方记 fault；重复的原名同样剔除。"""
    clash = "a_b_69f6642c"  # 与 `a.b` 清洗加后缀后的结果相同
    naming = model_tool_names("github", ["a.b", clash, "ok", "dup", "dup"])

    assert naming.names == {"ok": "mcp__github__ok"}
    assert naming.collided == ("a.b", clash, "dup")


def test_naming_rejects_invalid_server_key() -> None:
    with pytest.raises(AgentException):
        model_tool_names("Bad__Key", ["x"])


_TRI_STATE = (True, False, None)


@pytest.mark.parametrize(
    ("mode", "read_only", "destructive"),
    list(itertools.product(ApprovalMode, _TRI_STATE, _TRI_STATE)),
)
def test_requires_approval_truth_table(
    mode: ApprovalMode,
    read_only: bool | None,
    destructive: bool | None,
) -> None:
    """完整真值表：always 全审、never 全免；trust_annotations 只有显式只读且非显式破坏性才免审。"""
    annotations = McpToolAnnotations(read_only=read_only, destructive=destructive)
    expected = {
        ApprovalMode.ALWAYS: True,
        ApprovalMode.NEVER: False,
        ApprovalMode.TRUST_ANNOTATIONS: not (read_only is True and destructive is not True),
    }[mode]

    assert requires_approval(mode, annotations) is expected


def test_trust_annotations_spot_checks() -> None:
    mode = ApprovalMode.TRUST_ANNOTATIONS
    assert requires_approval(mode, McpToolAnnotations(read_only=True)) is False
    assert requires_approval(mode, McpToolAnnotations(read_only=True, destructive=True)) is True
    assert requires_approval(mode, McpToolAnnotations()) is True
    assert requires_approval(mode, McpToolAnnotations(destructive=False)) is True


def test_mcp_tool_policy_is_callable_everywhere_and_not_bindable() -> None:
    policy = mcp_tool_policy("mcp__s__t", requires_approval=True)

    assert policy.name == "mcp__s__t"
    assert policy.category == "mcp"
    assert policy.callable_profiles == ALL_PROFILES_AND_DELEGATE
    assert len(policy.callable_profiles) == 7
    assert policy.bindable_profiles == frozenset()
    assert policy.requires_approval is True


@pytest.mark.parametrize(
    "key",
    ["a", "github", "a_b", "a1_b2", "k" * SERVER_KEY_MAX, "x9"],
)
def test_validate_server_key_accepts(key: str) -> None:
    assert validate_server_key(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "Github",
        "1abc",
        "_abc",
        "abc_",
        "a__b",
        "a-b",
        "a.b",
        "a b",
        "k" * (SERVER_KEY_MAX + 1),
        "ä",
    ],
)
def test_validate_server_key_rejects(key: str) -> None:
    with pytest.raises(AgentException, match="server_key 不合法"):
        validate_server_key(key)


def _config(**overrides: object) -> McpServerConfig:
    values: dict[str, object] = {
        "server_id": 7,
        "workspace_id": 1,
        "owner_user_id": None,
        "server_key": "github",
        "url": "https://mcp.example.com/mcp",
    }
    values.update(overrides)
    return McpServerConfig(**values)  # type: ignore[arg-type]


def test_auth_partition_rules() -> None:
    """共享 server 静态请求头归工作区，私有 server 归 owner，共享 server 的 OAuth 归调用者。"""
    shared = _config(auth_type=McpAuthType.STATIC_HEADERS)
    private = _config(owner_user_id=5)
    oauth = _config(auth_type=McpAuthType.OAUTH)

    assert auth_partition(shared, workspace_id=1, user_id=9) == "ws:1"
    assert auth_partition(_config(), workspace_id=1, user_id=9) == "ws:1"
    assert auth_partition(private, workspace_id=1, user_id=5) == "user:5"
    assert auth_partition(oauth, workspace_id=1, user_id=9) == "user:9"


def test_auth_partition_rejects_foreign_identity() -> None:
    with pytest.raises(AgentException, match="不属于当前工作区"):
        auth_partition(_config(), workspace_id=2, user_id=9)
    with pytest.raises(AgentException, match="他人的私有"):
        auth_partition(_config(owner_user_id=5), workspace_id=1, user_id=6)


def test_config_filters_and_timeout() -> None:
    config = _config(tool_allowlist=("a", "b"), tool_denylist=("b",), call_timeout_seconds=30)

    assert config.allows("a") is True
    assert config.allows("b") is False
    assert config.allows("c") is False
    assert _config(tool_denylist=("x",)).allows("y") is True
    assert config.call_timeout(120) == 30
    assert config.call_timeout(10) == 10
    assert _config().call_timeout(120) == 120
    assert config.source == "mcp:github"


def test_secret_validates_and_hides_values() -> None:
    secret = McpSecret.from_headers({"Authorization": "Bearer t0ken", "X-Api-Key": "k"})

    assert secret.as_dict() == {"Authorization": "Bearer t0ken", "X-Api-Key": "k"}
    assert "t0ken" not in repr(secret)
    for bad in ({"Host": "x"}, {"Mcp-Session-Id": "x"}, {"Cookie": "x"}, {"bad name": "x"}):
        with pytest.raises(AgentException, match="名称不合法"):
            McpSecret.from_headers(bad)
    with pytest.raises(AgentException, match="换行") as info:
        McpSecret.from_headers({"Authorization": "Bearer a\r\nX-Evil: 1"})
    assert "Bearer" not in str(info.value.details)


def test_schema_digest_ignores_key_order() -> None:
    a = {"type": "object", "properties": {"x": {"type": "string"}}}
    b = {"properties": {"x": {"type": "string"}}, "type": "object"}

    assert schema_digest(a) == schema_digest(b)
    assert schema_digest(a) != schema_digest({"type": "object"})
