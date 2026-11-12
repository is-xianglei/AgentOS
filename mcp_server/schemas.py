"""MCP server 管理接口的请求与响应契约。

凭证只出现在请求里：响应里的请求头一律只回显名称，值为掩码 ``********``；
更新时提交掩码表示沿用原值。错误与响应都不带请求头的值。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.domain.mcp import ApprovalMode, McpAuthType, McpToolSummary
from harness.domain.mcp_config import (
    McpServerChanges,
    McpServerDraft,
    McpServerView,
    McpToolDiff,
    McpToolListing,
    masked_headers,
    redact_url_query,
)

Visibility = Literal["workspace", "private"]
# 本阶段可选的认证方式；oauth 在 M3 开放。
AuthType = Literal["none", "static_headers"]


class McpServerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visibility: Visibility = Field(description="workspace 工作区共享 / private 仅本人可见")
    server_key: str = Field(min_length=1, max_length=24, description="工作区内唯一的短标识")
    name: str = Field(min_length=1, max_length=120, description="展示名称")
    description: str | None = Field(default=None, description="描述")
    url: str = Field(min_length=1, max_length=2048, description="server 地址，只支持 https")
    auth_type: AuthType = Field(default="none", description="认证方式")
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="静态请求头，auth_type=static_headers 时必填",
    )
    approval_mode: ApprovalMode = Field(default=ApprovalMode.ALWAYS, description="审批模式")
    tool_allowlist: list[str] = Field(default_factory=list, description="工具白名单（MCP 原名）")
    tool_denylist: list[str] = Field(default_factory=list, description="工具黑名单（MCP 原名）")
    call_timeout_seconds: int | None = Field(default=None, gt=0, description="单次调用超时秒数")
    is_enabled: bool = Field(default=True, description="是否启用")

    def to_draft(self) -> McpServerDraft:
        """转成领域输入；规范化与业务校验（key、地址、请求头、过滤名单）在构造时完成。"""
        return McpServerDraft.create(
            server_key=self.server_key,
            name=self.name,
            description=self.description,
            url=self.url,
            private=self.visibility == "private",
            auth_type=McpAuthType(self.auth_type),
            headers=self.headers,
            approval_mode=self.approval_mode,
            tool_allowlist=self.tool_allowlist,
            tool_denylist=self.tool_denylist,
            call_timeout_seconds=self.call_timeout_seconds,
            is_enabled=self.is_enabled,
        )


class McpServerUpdateRequest(BaseModel):
    """更新允许省略字段；server_key 与 visibility 只用于核对，与原值不同时报错。

    description 与 call_timeout_seconds 可以显式传 null 清空，其余字段不能为 null。
    """

    model_config = ConfigDict(extra="forbid")

    visibility: Visibility | None = None
    server_key: str | None = Field(default=None, max_length=24)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    auth_type: AuthType | None = None
    headers: dict[str, str] | None = Field(
        default=None,
        description="提交即整体替换；值为 ******** 表示沿用该请求头的原值",
    )
    approval_mode: ApprovalMode | None = None
    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] | None = None
    call_timeout_seconds: int | None = Field(default=None, gt=0)
    is_enabled: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        nullable = {"description", "call_timeout_seconds"}
        for name, item in value.items():
            if item is None and name not in nullable:
                raise ValueError(f"更新字段 {name} 不能为 null")
        return value

    def to_changes(self) -> McpServerChanges:
        """只取客户端显式提交的字段。"""
        submitted = self.model_dump(exclude_unset=True)
        changes: McpServerChanges = {}
        if "visibility" in submitted:
            changes["private"] = submitted.pop("visibility") == "private"
        if "auth_type" in submitted:
            changes["auth_type"] = McpAuthType(submitted.pop("auth_type"))
        if "approval_mode" in submitted:
            changes["approval_mode"] = ApprovalMode(submitted.pop("approval_mode"))
        changes.update(submitted)  # type: ignore[typeddict-item]
        return changes


class McpToolSummaryResponse(BaseModel):
    name: str = Field(description="MCP 原名")
    description: str
    input_schema_sha256: str
    read_only: bool | None = Field(default=None, description="注解 readOnlyHint，未给为空")
    destructive: bool | None = Field(default=None, description="注解 destructiveHint，未给为空")

    @classmethod
    def from_summary(cls, tool: McpToolSummary) -> McpToolSummaryResponse:
        return cls(
            name=tool.name,
            description=tool.description,
            input_schema_sha256=tool.input_schema_sha256,
            read_only=tool.annotations.read_only,
            destructive=tool.annotations.destructive,
        )


class McpServerResponse(BaseModel):
    """server 的对外视图：不含凭证；没有写权限的成员看不到地址里的查询参数和请求头名称。"""

    id: int
    server_key: str
    visibility: Visibility
    name: str
    description: str | None = None
    url: str
    auth_type: str
    approval_mode: str
    tool_allowlist: list[str]
    tool_denylist: list[str]
    call_timeout_seconds: int | None = None
    config_rev: int
    is_enabled: bool
    writable: bool = Field(description="当前用户能否修改")
    headers: dict[str, str] | None = Field(
        default=None,
        description="请求头名称，值一律为 ********；只在写操作的响应里给出",
    )
    tool_count: int
    last_probe_at: datetime | None = None
    last_probe_error: str | None = None
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: McpServerView) -> McpServerResponse:
        config = view.config
        timeout = config.call_timeout_seconds
        return cls(
            id=view.server_id,
            server_key=config.server_key,
            visibility="private" if config.is_private else "workspace",
            name=view.name,
            description=view.description,
            url=config.url if view.writable else redact_url_query(config.url),
            auth_type=config.auth_type.value,
            approval_mode=config.approval_mode.value,
            tool_allowlist=list(config.tool_allowlist),
            tool_denylist=list(config.tool_denylist),
            call_timeout_seconds=int(timeout) if timeout is not None else None,
            config_rev=config.config_rev,
            is_enabled=config.is_enabled,
            writable=view.writable,
            headers=masked_headers(view.header_names) if view.header_names is not None else None,
            tool_count=len(view.tools),
            last_probe_at=view.last_probe_at,
            last_probe_error=view.last_probe_error,
            created_by=view.created_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class McpServerCreateResponse(BaseModel):
    """新建的结果：server 视图加上探测到的工具清单摘要。"""

    server: McpServerResponse
    tools: list[McpToolSummaryResponse]


class McpToolResponse(McpToolSummaryResponse):
    """管理界面的一项工具：在摘要之外附上模型名、是否被过滤和推导出的审批要求。"""

    model_name: str | None = Field(description="发给模型的工具名；与同 server 其他工具撞名时为空")
    filtered_out: bool = Field(description="是否被 tool_allowlist / tool_denylist 过滤掉")
    requires_approval: bool = Field(description="按审批模式与注解推导出的默认审批要求")

    @classmethod
    def from_listing(cls, listing: McpToolListing) -> McpToolResponse:
        base = McpToolSummaryResponse.from_summary(listing.summary)
        return cls(
            **base.model_dump(),
            model_name=listing.model_name,
            filtered_out=listing.filtered_out,
            requires_approval=listing.requires_approval,
        )


class McpToolSchemaChange(BaseModel):
    name: str
    before_sha256: str
    after_sha256: str


class McpToolDiffResponse(BaseModel):
    added: list[str]
    removed: list[str]
    schema_changed: list[McpToolSchemaChange]

    @classmethod
    def from_diff(cls, diff: McpToolDiff) -> McpToolDiffResponse:
        return cls(
            added=list(diff.added),
            removed=list(diff.removed),
            schema_changed=[
                McpToolSchemaChange(
                    name=change.name,
                    before_sha256=change.before_sha256,
                    after_sha256=change.after_sha256,
                )
                for change in diff.schema_changed
            ],
        )


class McpProbeResponse(BaseModel):
    """重新探测的结果：成功时给出与上次快照的差异，失败时 error 非空、快照不变。"""

    ok: bool
    error: str | None = None
    server: McpServerResponse
    diff: McpToolDiffResponse
