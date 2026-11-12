"""MCP 领域规则：工具命名、注解到审核策略的映射、server 配置值类型与授权分区。

全部是纯函数和不可变值类型：不做 I/O，不读配置和进程状态，不导入 mcp SDK。
SDK 类型到这些值类型的翻译在 harness/adapters/mcp 里完成。
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.errors import AgentException
from harness.domain.tool import ALL_PROFILES_AND_DELEGATE, ToolPolicy

SERVER_KEY_PATTERN = r"^[a-z](?:_?[a-z0-9])*$"  # 小写字母开头，不允许连续或结尾下划线
SERVER_KEY_MAX = 24  # 工作区内唯一，不可修改；决定工具名前缀
MODEL_TOOL_NAME_MAX = 64  # 取两家上限中较小的：OpenAI Chat Completions 为 64
MCP_PREFIX = "mcp__"

_SERVER_KEY_RE = re.compile(SERVER_KEY_PATTERN)
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_HASH_LEN = 8
# RFC 9110 token：请求头名称的合法字符。
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# 由传输层或协议自己维护的请求头，配置里不允许出现（8.1 节）。
_FORBIDDEN_HEADER_NAMES = frozenset({"host", "content-length", "cookie"})
_FORBIDDEN_HEADER_PREFIX = "mcp-"


class ApprovalMode(StrEnum):
    """server 级审批模式，值与 mcp_servers.approval_mode 一致。"""

    ALWAYS = "always"  # 每次调用都需要审批（默认）
    TRUST_ANNOTATIONS = "trust_annotations"  # 按注解推导：只读免审，其余需审批
    NEVER = "never"  # 全部免审（仅限可信的内部 server）


class McpAuthType(StrEnum):
    """server 的认证方式，值与 mcp_servers.auth_type 一致。"""

    NONE = "none"
    STATIC_HEADERS = "static_headers"
    OAUTH = "oauth"


@dataclass(frozen=True)
class McpToolAnnotations:
    """MCP ToolAnnotations 的领域投影。None 表示 server 没给，判定时按规范默认值处理。

    只保留参与判定的两项；idempotentHint、openWorldHint 本期不影响任何行为，不建模。
    """

    read_only: bool | None = None
    destructive: bool | None = None

    def to_dict(self) -> dict[str, bool | None]:
        return {"read_only": self.read_only, "destructive": self.destructive}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> McpToolAnnotations:
        return cls(read_only=data.get("read_only"), destructive=data.get("destructive"))


@dataclass(frozen=True)
class McpToolRef:
    """模型可见名到远程工具的路由键。调用时用 remote_name，不反解 model_name。"""

    server_id: int
    server_key: str
    config_rev: int
    remote_name: str
    model_name: str
    schema_sha256: str  # inputSchema 规范化序列化后的摘要，挂起快照核对用


@dataclass(frozen=True)
class McpToolNaming:
    """一个 server 的命名结果。

    names 为原名 → 模型名，已去掉相撞的工具；collided 是模型名相撞（含原名重复）而被剔除的原名，
    按原名排序，由调用方记 invalid fault。
    """

    names: dict[str, str]
    collided: tuple[str, ...] = ()


def validate_server_key(server_key: str) -> str:
    """校验 server_key：1～24 个字符，小写字母开头，不含连续下划线、不以下划线结尾。"""
    if len(server_key) > SERVER_KEY_MAX or not _SERVER_KEY_RE.fullmatch(server_key):
        raise AgentException.message(
            "server_key 不合法：须为 1～24 个字符，小写字母开头，只含小写字母、数字和单个下划线，"
            "且不以下划线结尾",
            {"server_key": server_key, "max_length": SERVER_KEY_MAX},
            status_code=400,
        )
    return server_key


def server_key_of(tool_name: str) -> str | None:
    """从 MCP 工具的模型名里取出 server_key；不是 MCP 工具名时返回 None。

    server_key 不含连续下划线、不以下划线结尾，所以前缀后第一个 ``__`` 之前就是它，切分结果唯一。
    """
    if not tool_name.startswith(MCP_PREFIX):
        return None
    server_key, separator, remote = tool_name[len(MCP_PREFIX) :].partition("__")
    if not separator or not remote or not _SERVER_KEY_RE.fullmatch(server_key):
        return None
    return server_key


def mcp_source(server_key: str) -> str:
    """MCP 工具的来源标识（Tool.source、ToolSourceFault.source）。"""
    return f"mcp:{server_key}"


def model_tool_names(server_key: str, remote_names: Iterable[str]) -> McpToolNaming:
    """把一个 server 的工具原名映射为模型可见名，结果与输入顺序无关。

    每个工具的模型名只由 (server_key, 它自己的原名) 决定；同一 server 内算出相同模型名的原名
    （含重复的原名）一律剔除，不靠重新哈希消歧，否则模型名会依赖整组工具。
    """
    prefix = f"{MCP_PREFIX}{validate_server_key(server_key)}__"
    groups: dict[str, list[str]] = {}
    for remote_name in remote_names:
        groups.setdefault(_model_tool_name(prefix, remote_name), []).append(remote_name)
    names: dict[str, str] = {}
    collided: set[str] = set()
    for model_name, members in groups.items():
        if len(members) == 1:
            names[members[0]] = model_name
        else:
            collided.update(members)
    return McpToolNaming(
        names=dict(sorted(names.items())),
        collided=tuple(sorted(collided)),
    )


def _model_tool_name(prefix: str, remote_name: str) -> str:
    """清洗有损或超长时加原名的 sha1 前 8 位作后缀；超长时先从右侧截短清洗结果。"""
    clean = _UNSAFE_NAME_CHARS.sub("_", remote_name)
    candidate = f"{prefix}{clean}"
    if clean == remote_name and len(candidate) <= MODEL_TOOL_NAME_MAX:
        return candidate
    digest = hashlib.sha1(remote_name.encode("utf-8")).hexdigest()[:_HASH_LEN]
    room = MODEL_TOOL_NAME_MAX - len(prefix) - 1 - _HASH_LEN
    return f"{prefix}{clean[:room]}_{digest}"


def requires_approval(mode: ApprovalMode, annotations: McpToolAnnotations) -> bool:
    """审批模式与注解 → ToolPolicy.requires_approval。

    只有 trust_annotations 才看注解：显式 destructive 需审批；否则显式 read_only 免审；
    其余按规范默认值（destructive、open_world 缺省为 true）需审批。
    """
    if mode is ApprovalMode.ALWAYS:
        return True
    if mode is ApprovalMode.NEVER:
        return False
    if annotations.destructive is True:
        return True
    return annotations.read_only is not True


def mcp_tool_policy(model_name: str, *, requires_approval: bool) -> ToolPolicy:
    """MCP 工具的能力策略：全部 7 个档位可调，不可绑定。

    哪些档位能看到 MCP 工具由 ToolBinding 决定，不靠档位策略；Agent 按 server 绑定，
    不经 tools 表的 tool_id 绑定，所以不需要 bindable。
    """
    return ToolPolicy(
        name=model_name,
        category="mcp",
        bindable_profiles=frozenset(),
        callable_profiles=ALL_PROFILES_AND_DELEGATE,
        requires_approval=requires_approval,
    )


def schema_digest(schema: Mapping[str, Any]) -> str:
    """inputSchema 规范化序列化（键排序、紧凑分隔）后的 SHA-256，挂起快照据此核对 schema 变化。"""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_static_headers(headers: Mapping[str, str]) -> None:
    """静态请求头的名称须是 HTTP token 且不是受保护的头，值不得含 CR、LF；错误信息不带值。"""
    for name, value in headers.items():
        lowered = name.lower()
        if (
            not _HEADER_NAME_RE.fullmatch(name)
            or lowered in _FORBIDDEN_HEADER_NAMES
            or lowered.startswith(_FORBIDDEN_HEADER_PREFIX)
        ):
            raise AgentException.message(
                "MCP 请求头名称不合法或不允许配置",
                {"header": name},
                status_code=400,
            )
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise AgentException.message(
                "MCP 请求头的值不能包含换行符",
                {"header": name},
                status_code=400,
            )


@dataclass(frozen=True)
class McpServerConfig:
    """一个 server 的配置（mcp_servers 的一行），不含凭证。

    server_id 为 None 表示尚未落库（创建前的探测）；owner_user_id 为 None 是共享 server，
    否则是该用户的私有 server。config_rev 在地址、认证、过滤、超时、启用状态变更时加一。
    """

    server_id: int | None
    workspace_id: int
    owner_user_id: int | None
    server_key: str
    url: str
    auth_type: McpAuthType = McpAuthType.NONE
    approval_mode: ApprovalMode = ApprovalMode.ALWAYS
    tool_allowlist: tuple[str, ...] = ()
    tool_denylist: tuple[str, ...] = ()
    call_timeout_seconds: float | None = None
    config_rev: int = 1
    is_enabled: bool = True

    @property
    def is_private(self) -> bool:
        return self.owner_user_id is not None

    @property
    def source(self) -> str:
        return mcp_source(self.server_key)

    def require_id(self) -> int:
        """取已落库 server 的 id；运行时只处理已落库的配置，缺 id 属编码错误。"""
        if self.server_id is None:
            raise AgentException.message("MCP server 配置缺少 id", {"server_key": self.server_key})
        return self.server_id

    def allows(self, remote_name: str) -> bool:
        """按 MCP 原名过滤：denylist 优先；allowlist 为空表示不限制。"""
        if remote_name in self.tool_denylist:
            return False
        return not self.tool_allowlist or remote_name in self.tool_allowlist

    def call_timeout(self, global_cap: float) -> float:
        """单次调用超时：server 配置与全局上限取较小值。"""
        if self.call_timeout_seconds is None:
            return global_cap
        return min(self.call_timeout_seconds, global_cap)


@dataclass(frozen=True)
class McpSecret:
    """server 的静态请求头（明文），只经 from_headers 构造；repr 不输出值。"""

    headers: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> McpSecret:
        validate_static_headers(headers)
        return cls(headers=tuple(sorted(headers.items())))

    def as_dict(self) -> dict[str, str]:
        return dict(self.headers)


@dataclass(frozen=True)
class McpServerEntry:
    """某个身份可用的一个 server：配置加上凭证（静态请求头），没有凭证时为 None。"""

    config: McpServerConfig
    secret: McpSecret | None = None


@dataclass(frozen=True)
class McpToolSummary:
    """工具清单摘要的一项，即 tools_snapshot 的元素；只有元数据，不含用户数据。"""

    name: str
    description: str
    input_schema_sha256: str
    annotations: McpToolAnnotations

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema_sha256": self.input_schema_sha256,
            "annotations": self.annotations.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> McpToolSummary:
        """从 tools_snapshot 的一项还原，形状即 to_dict() 的输出。"""
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            input_schema_sha256=data["input_schema_sha256"],
            annotations=McpToolAnnotations.from_dict(data.get("annotations") or {}),
        )


@dataclass(frozen=True)
class McpProbeResult:
    """一次探测的结果：成功时带 server 信息与工具摘要，失败时只带可展示的中文错误。"""

    ok: bool
    error: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    protocol_version: str | None = None
    tools: tuple[McpToolSummary, ...] = ()

    @classmethod
    def success(
        cls,
        *,
        server_name: str | None,
        server_version: str | None,
        protocol_version: str | None,
        tools: Iterable[McpToolSummary],
    ) -> McpProbeResult:
        return cls(
            ok=True,
            server_name=server_name,
            server_version=server_version,
            protocol_version=protocol_version,
            tools=tuple(sorted(tools, key=lambda tool: tool.name)),
        )

    @classmethod
    def failure(cls, error: str) -> McpProbeResult:
        return cls(ok=False, error=error)


# ---------- 授权分区：凭证归谁，连接池与 SDK 响应缓存都按它隔离 ----------


def workspace_partition(workspace_id: int) -> str:
    return f"ws:{workspace_id}"


def user_partition(user_id: int) -> str:
    return f"user:{user_id}"


def auth_partition(config: McpServerConfig, *, workspace_id: int, user_id: int) -> str:
    """按可信的回合身份算授权分区。

    - 私有 server：归 owner 本人，只有 owner 的身份能用；
    - 共享 server 的 OAuth：归调用的用户（M3 用）；
    - 共享 server 的静态请求头或无认证：归工作区。
    身份与配置不符（跨工作区、别人的私有 server）属越权，抛中文异常而不回落。
    """
    if config.workspace_id != workspace_id:
        raise AgentException.message(
            "MCP server 不属于当前工作区",
            {"server_key": config.server_key, "workspace_id": workspace_id},
        )
    if config.owner_user_id is not None:
        if config.owner_user_id != user_id:
            raise AgentException.message(
                "无权使用他人的私有 MCP server",
                {"server_key": config.server_key},
            )
        return user_partition(config.owner_user_id)
    if config.auth_type is McpAuthType.OAUTH:
        return user_partition(user_id)
    return workspace_partition(workspace_id)
