"""MCP server 管理的值类型与纯规则：地址规范化、请求头掩码、改动计划、工具清单差异。

McpAdminService（harness/app/mcp_admin.py）与 mcp_server 域经这些类型交换数据，应用层因此不认识 ORM，
业务域也不认识 HTTP Schema。全部是纯函数和不可变值类型，不做 I/O，不读配置和进程状态。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

from core.errors import AgentException
from harness.domain.mcp import (
    ApprovalMode,
    McpAuthType,
    McpSecret,
    McpServerConfig,
    McpToolSummary,
    model_tool_names,
    requires_approval,
    validate_server_key,
)

SECRET_MASK = "********"  # 接口回显凭证时的掩码；更新时提交掩码表示沿用原值
_DEFAULT_PORTS = {"https": 443, "http": 80}
# 这些配置项变化时 config_rev 加一：连接池的键与挂起快照核对都依赖它。审批模式每回合现算，不在其中。
REVISION_FIELDS = ("tool_allowlist", "tool_denylist", "call_timeout_seconds", "is_enabled")


def canonical_server_url(url: str) -> str:
    """规范化 server 地址：协议和主机名转小写，去掉默认端口和路径末尾的斜杠，其余原样保留。

    只做无损规范化，不做安全校验：https、userinfo、片段和内网地址由适配层探测时校验。
    空白与控制字符直接拒绝：解析器会悄悄剥掉它们，规范化结果就不是用户填的那个地址了。
    """
    if not url or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise AgentException.message(
            "MCP server 地址不能为空，也不能包含空白或控制字符",
            status_code=400,
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise AgentException.message("MCP server 地址格式不正确", status_code=400) from exc
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not scheme or not host:
        raise AgentException.message("MCP server 地址格式不正确", status_code=400)
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    if parts.username is not None or parts.password is not None:
        # userinfo 原样保留，交给适配层的地址校验拒绝；这里不改写，避免把凭证悄悄去掉后存库。
        netloc = f"{parts.netloc.rpartition('@')[0]}@{netloc}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


def merge_secret_headers(
    submitted: Mapping[str, str],
    existing: Mapping[str, str],
) -> dict[str, str]:
    """合并更新时提交的请求头：值为掩码表示沿用原值，原来没有这个头时报错。"""
    merged: dict[str, str] = {}
    for name, value in submitted.items():
        if value != SECRET_MASK:
            merged[name] = value
            continue
        if name not in existing:
            raise AgentException.message(
                "请求头的值不能是掩码，请填写实际的值",
                {"header": name},
                status_code=400,
            )
        merged[name] = existing[name]
    return merged


def masked_headers(names: Iterable[str]) -> dict[str, str]:
    """回显用：请求头只露出名字，值一律掩码。"""
    return dict.fromkeys(sorted(names), SECRET_MASK)


def redact_url_query(url: str) -> str:
    """给没有写权限的成员看的地址：查询参数可能带着凭证，整段换成掩码。"""
    parts = urlsplit(url)
    if not parts.query:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, SECRET_MASK, ""))


def validate_tool_filter(names: Iterable[str], *, field_name: str) -> tuple[str, ...]:
    """工具过滤名单按 MCP 原名匹配：去重并保持顺序，不允许空名。"""
    result: list[str] = []
    for name in names:
        if not name or not name.strip():
            raise AgentException.message(
                "工具过滤名单里不能有空的工具名",
                {"field": field_name},
                status_code=400,
            )
        if name not in result:
            result.append(name)
    return tuple(result)


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 120:
        raise AgentException.message("MCP server 名称须为 1～120 个字符", status_code=400)
    return cleaned


def _clean_description(description: str | None) -> str | None:
    if description is None:
        return None
    return description.strip() or None


def _clean_url(url: str) -> str:
    canonical = canonical_server_url(url.strip())
    if len(canonical) > 2048:
        raise AgentException.message("MCP server 地址不能超过 2048 个字符", status_code=400)
    return canonical


def _clean_timeout(seconds: int | None) -> int | None:
    if seconds is not None and seconds <= 0:
        raise AgentException.message("调用超时须为正整数秒", status_code=400)
    return seconds


def _build_secret(auth_type: McpAuthType, headers: Mapping[str, str] | None) -> McpSecret | None:
    """按认证方式校验请求头：static_headers 必须至少一个，其余方式不能带请求头。"""
    if auth_type is McpAuthType.STATIC_HEADERS:
        if not headers:
            raise AgentException.message(
                "认证方式为 static_headers 时至少要配置一个请求头",
                status_code=400,
            )
        return McpSecret.from_headers(headers)
    if headers:
        raise AgentException.message(
            "只有 static_headers 认证方式可以配置请求头",
            {"auth_type": auth_type.value},
            status_code=400,
        )
    return None


def _reject_oauth(auth_type: McpAuthType) -> None:
    # OAuth 的授权流程、token 存储与探测方式在 M3 才有，现在建出来也连不上。
    if auth_type is McpAuthType.OAUTH:
        raise AgentException.message(
            "OAuth 认证尚未开放，请使用 none 或 static_headers",
            status_code=400,
        )


class McpServerChanges(TypedDict, total=False):
    """更新时提交的字段，只含客户端显式给出的项；server_key 与 private 只用于核对，不能改。"""

    server_key: str
    private: bool
    name: str
    description: str | None
    url: str
    auth_type: McpAuthType
    headers: dict[str, str]
    approval_mode: ApprovalMode
    tool_allowlist: list[str]
    tool_denylist: list[str]
    call_timeout_seconds: int | None
    is_enabled: bool


@dataclass(frozen=True)
class McpServerDraft:
    """新建 server 的输入，只经 create() 构造：构造时完成规范化与校验，凭证不进 repr。"""

    server_key: str
    name: str
    description: str | None
    url: str
    private: bool
    auth_type: McpAuthType
    approval_mode: ApprovalMode
    tool_allowlist: tuple[str, ...]
    tool_denylist: tuple[str, ...]
    call_timeout_seconds: int | None
    is_enabled: bool
    secret: McpSecret | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls,
        *,
        server_key: str,
        name: str,
        url: str,
        private: bool,
        description: str | None = None,
        auth_type: McpAuthType = McpAuthType.NONE,
        headers: Mapping[str, str] | None = None,
        approval_mode: ApprovalMode = ApprovalMode.ALWAYS,
        tool_allowlist: Iterable[str] = (),
        tool_denylist: Iterable[str] = (),
        call_timeout_seconds: int | None = None,
        is_enabled: bool = True,
    ) -> McpServerDraft:
        _reject_oauth(auth_type)
        return cls(
            server_key=validate_server_key(server_key),
            name=_clean_name(name),
            description=_clean_description(description),
            url=_clean_url(url),
            private=private,
            auth_type=auth_type,
            approval_mode=approval_mode,
            tool_allowlist=validate_tool_filter(tool_allowlist, field_name="tool_allowlist"),
            tool_denylist=validate_tool_filter(tool_denylist, field_name="tool_denylist"),
            call_timeout_seconds=_clean_timeout(call_timeout_seconds),
            is_enabled=is_enabled,
            secret=_build_secret(auth_type, headers),
        )

    def probe_config(self, *, workspace_id: int, owner_user_id: int | None) -> McpServerConfig:
        """创建前探测用的配置：尚未落库，server_id 为 None。"""
        return McpServerConfig(
            server_id=None,
            workspace_id=workspace_id,
            owner_user_id=owner_user_id,
            server_key=self.server_key,
            url=self.url,
            auth_type=self.auth_type,
            approval_mode=self.approval_mode,
            tool_allowlist=self.tool_allowlist,
            tool_denylist=self.tool_denylist,
            call_timeout_seconds=self.call_timeout_seconds,
            is_enabled=self.is_enabled,
        )


@dataclass(frozen=True)
class McpServerView:
    """一个 server 的展示视图（mcp_servers 的一行），不含凭证。

    writable 表示当前用户能否修改它；header_names 只在读过凭证表的写操作里填入，
    列表与详情不读凭证表，为 None。
    """

    config: McpServerConfig
    name: str
    description: str | None
    created_by: int | None
    writable: bool
    last_probe_at: datetime | None
    last_probe_error: str | None
    tools: tuple[McpToolSummary, ...]
    created_at: datetime
    updated_at: datetime
    header_names: tuple[str, ...] | None = None

    @property
    def server_id(self) -> int:
        return self.config.require_id()


@dataclass(frozen=True)
class McpServerUpdate:
    """一次更新的计划：更新后的配置（config_rev 已按规则加一）、展示字段与凭证，以及是否要重新探测。

    只经 plan_update() 构造；base_rev 是计划所依据的 config_rev，写库时据此核对没有被别人改过。
    """

    config: McpServerConfig
    base_rev: int
    name: str
    description: str | None
    needs_probe: bool
    secret_changed: bool
    secret: McpSecret | None = field(default=None, repr=False)

    @property
    def revision_changed(self) -> bool:
        return self.config.config_rev != self.base_rev


def _updated_secret(
    auth_type: McpAuthType,
    existing: Mapping[str, str],
    changes: McpServerChanges,
) -> McpSecret | None:
    """更新后的凭证：提交了请求头就按掩码规则合并，没提交就沿用原值。"""
    if "headers" in changes:
        submitted = merge_secret_headers(changes["headers"], existing)
    elif auth_type is McpAuthType.STATIC_HEADERS:
        submitted = dict(existing)
    else:
        submitted = {}
    return _build_secret(auth_type, submitted)


@dataclass(frozen=True)
class McpWritableServer:
    """有写权限时读到的 server：展示视图加凭证；只有需要凭证的流程（更新、探测）才读它。"""

    view: McpServerView
    secret: McpSecret | None = field(default=None, repr=False)


def plan_update(current: McpWritableServer, changes: McpServerChanges) -> McpServerUpdate:
    """算出一次更新的结果：server_key 不可改、不能在共享和私有之间切换；

    地址、认证（含请求头）、过滤、超时、启用状态任一变化 config_rev 加一；只有地址或认证变化才重新探测。
    """
    config = current.view.config
    if "server_key" in changes and changes["server_key"] != config.server_key:
        raise AgentException.message("server_key 创建后不能修改", status_code=400)
    if "private" in changes and changes["private"] != config.is_private:
        raise AgentException.message("server 不能在共享和私有之间切换", status_code=400)
    url = _clean_url(changes["url"]) if "url" in changes else config.url
    auth_type = changes.get("auth_type", config.auth_type)
    if auth_type is not config.auth_type:
        _reject_oauth(auth_type)
    existing = current.secret.as_dict() if current.secret is not None else {}
    secret = _updated_secret(auth_type, existing, changes)
    secret_changed = (secret.as_dict() if secret is not None else {}) != existing
    updated = McpServerConfig(
        server_id=config.server_id,
        workspace_id=config.workspace_id,
        owner_user_id=config.owner_user_id,
        server_key=config.server_key,
        url=url,
        auth_type=auth_type,
        approval_mode=changes.get("approval_mode", config.approval_mode),
        tool_allowlist=_updated_filter(changes, "tool_allowlist", config.tool_allowlist),
        tool_denylist=_updated_filter(changes, "tool_denylist", config.tool_denylist),
        call_timeout_seconds=(
            _clean_timeout(changes["call_timeout_seconds"])
            if "call_timeout_seconds" in changes
            else config.call_timeout_seconds
        ),
        config_rev=config.config_rev,
        is_enabled=changes.get("is_enabled", config.is_enabled),
    )
    connection_changed = url != config.url or auth_type is not config.auth_type or secret_changed
    revision_changed = connection_changed or any(
        getattr(updated, name) != getattr(config, name) for name in REVISION_FIELDS
    )
    if revision_changed:
        updated = replace(updated, config_rev=config.config_rev + 1)
    return McpServerUpdate(
        config=updated,
        base_rev=config.config_rev,
        name=_clean_name(changes["name"]) if "name" in changes else current.view.name,
        description=(
            _clean_description(changes["description"])
            if "description" in changes
            else current.view.description
        ),
        # 停用状态下改了地址也要探测：否则重新启用时没有任何一步验证过新地址。
        needs_probe=connection_changed,
        secret_changed=secret_changed,
        secret=secret,
    )


def _updated_filter(
    changes: McpServerChanges,
    key: str,
    current: tuple[str, ...],
) -> tuple[str, ...]:
    if key not in changes:
        return current
    return validate_tool_filter(changes[key], field_name=key)  # type: ignore[literal-required]


@dataclass(frozen=True)
class McpToolChange:
    """工具清单差异里 schema 变化的一项：同名工具的 inputSchema 摘要前后不同。"""

    name: str
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True)
class McpToolDiff:
    """新清单与快照的差异，三项都按工具原名排序。"""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    schema_changed: tuple[McpToolChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.schema_changed)


def diff_tools(
    before: Iterable[McpToolSummary],
    after: Iterable[McpToolSummary],
) -> McpToolDiff:
    """按工具原名比较两份清单：新增、删除，以及 inputSchema 摘要变化。描述和注解变化不计入。"""
    old = {tool.name: tool for tool in before}
    new = {tool.name: tool for tool in after}
    changed = [
        McpToolChange(
            name=name,
            before_sha256=old[name].input_schema_sha256,
            after_sha256=new[name].input_schema_sha256,
        )
        for name in sorted(old.keys() & new.keys())
        if old[name].input_schema_sha256 != new[name].input_schema_sha256
    ]
    return McpToolDiff(
        added=tuple(sorted(new.keys() - old.keys())),
        removed=tuple(sorted(old.keys() - new.keys())),
        schema_changed=tuple(changed),
    )


@dataclass(frozen=True)
class McpToolListing:
    """管理界面的一项工具：快照里的原名与元数据，加上模型名、是否被过滤和推导出的审批要求。

    model_name 为 None 表示这个原名与同一 server 的其他工具算出了相同的模型名，回合里会被剔除。
    """

    summary: McpToolSummary
    model_name: str | None
    filtered_out: bool
    requires_approval: bool


def list_server_tools(
    config: McpServerConfig,
    tools: Iterable[McpToolSummary],
) -> list[McpToolListing]:
    """按回合同一套规则（命名、过滤、审批推导）展示快照里的工具，按原名排序。"""
    snapshot = sorted(tools, key=lambda tool: tool.name)
    naming = model_tool_names(config.server_key, [tool.name for tool in snapshot])
    return [
        McpToolListing(
            summary=tool,
            model_name=naming.names.get(tool.name),
            filtered_out=not config.allows(tool.name),
            requires_approval=requires_approval(config.approval_mode, tool.annotations),
        )
        for tool in snapshot
    ]
