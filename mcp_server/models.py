"""MCP server 配置与凭证的持久化记录。

共享 server 与私有 server 在同一张表里，owner_user_id 非空即为私有；凭证单独成表，
列表与详情只读 mcp_servers，只有建连与探测才读 mcp_server_secrets。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, json_type


class McpServerRecord(Base):
    """工作区里配置的一个 MCP server：地址、认证方式、审批模式、工具过滤与最近一次探测结果。"""

    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint(
            "auth_type IN ('none', 'static_headers', 'oauth')",
            name="ck_mcp_servers_auth_type",
        ),
        CheckConstraint(
            "approval_mode IN ('always', 'trust_annotations', 'never')",
            name="ck_mcp_servers_approval_mode",
        ),
        # server_key 决定工具名前缀，共享和私有 server 共用一个命名空间，软删后可以重建同一个 key。
        Index(
            "uq_mcp_servers_active_workspace_key",
            "workspace_id",
            "server_key",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "MCP server 配置（共享与私有）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="MCP server ID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属工作区ID",
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="私有 server 的所有者用户ID，空表示工作区共享 server",
    )
    server_key: Mapped[str] = mapped_column(
        String(24),
        comment="工作区内唯一的短标识，决定工具名前缀，创建后不可修改",
    )
    name: Mapped[str] = mapped_column(String(120), comment="展示名称")
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="配置者填写的描述，不取 server 自报内容",
    )
    url: Mapped[str] = mapped_column(String(2048), comment="规范化后的 server 地址")
    auth_type: Mapped[str] = mapped_column(
        String(16),
        default="none",
        comment="认证方式: none / static_headers / oauth",
    )
    approval_mode: Mapped[str] = mapped_column(
        String(24),
        default="always",
        comment="审批模式: always 一律审批 / trust_annotations 按注解 / never 免审",
    )
    tool_allowlist: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="工具白名单（MCP 原名），空表示不限制",
    )
    tool_denylist: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="工具黑名单（MCP 原名），优先于白名单",
    )
    call_timeout_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="单次调用超时秒数，空表示用全局上限，非空时与全局上限取较小值",
    )
    config_rev: Mapped[int] = mapped_column(
        Integer,
        default=1,
        comment="配置版本：地址、认证、过滤、超时、启用状态任一变更就加一",
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    last_probe_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次探测时间",
    )
    last_probe_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="最近一次探测的错误（已脱敏），成功时为空",
    )
    tools_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="最近一次成功拉到的工具元数据，只用于管理展示、差异对比与权限规则校验",
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="创建者用户ID",
    )


class McpServerSecretRecord(Base):
    """server 的静态请求头（明文），一个 server 一行；只有建连与探测才读。"""

    __tablename__ = "mcp_server_secrets"
    __table_args__ = (
        Index(
            "uq_mcp_server_secrets_active_server",
            "server_id",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "MCP server 凭证（静态请求头，明文）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="凭证记录ID")
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        index=True,
        comment="所属 MCP server ID",
    )
    headers: Mapped[dict[str, str]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="静态请求头 {名称: 值}，明文，接口只回显名称",
    )
