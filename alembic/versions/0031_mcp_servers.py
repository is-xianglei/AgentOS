"""新增 MCP server 配置表 mcp_servers 与凭证表 mcp_server_secrets。

共享 server 与成员私有 server 同表，owner_user_id 非空即私有；server_key 在工作区内唯一，
由部分唯一索引（is_deleted = false）兜底，软删后可以重建同一个 key。
凭证（静态请求头）单独成表、明文存储，一个 server 一行，同样按部分唯一索引约束。
OAuth 的两张表留到 M3，Agent 绑定表 agent_mcp_servers 留到 M2。

Revision ID: 0031_mcp_servers
Revises: 0030_drop_orchestration_ckpts
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0031_mcp_servers"
down_revision = "0030_drop_orchestration_ckpts"
branch_labels = None
depends_on = None


def _audit_columns() -> list[sa.Column]:
    # 注释与 database/base.py 中 Base 的审计字段保持一致，避免 autogenerate 报出注释差异。
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="更新时间",
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="软删除标记: false 有效 / true 已删除",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间, 空表示未删除",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Integer(), primary_key=True, comment="MCP server ID"),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属工作区ID",
        ),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
            comment="私有 server 的所有者用户ID，空表示工作区共享 server",
        ),
        sa.Column(
            "server_key",
            sa.String(24),
            nullable=False,
            comment="工作区内唯一的短标识，决定工具名前缀，创建后不可修改",
        ),
        sa.Column("name", sa.String(120), nullable=False, comment="展示名称"),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="配置者填写的描述，不取 server 自报内容",
        ),
        sa.Column("url", sa.String(2048), nullable=False, comment="规范化后的 server 地址"),
        sa.Column(
            "auth_type",
            sa.String(16),
            nullable=False,
            comment="认证方式: none / static_headers / oauth",
        ),
        sa.Column(
            "approval_mode",
            sa.String(24),
            nullable=False,
            comment="审批模式: always 一律审批 / trust_annotations 按注解 / never 免审",
        ),
        sa.Column(
            "tool_allowlist",
            postgresql.JSONB(),
            nullable=False,
            comment="工具白名单（MCP 原名），空表示不限制",
        ),
        sa.Column(
            "tool_denylist",
            postgresql.JSONB(),
            nullable=False,
            comment="工具黑名单（MCP 原名），优先于白名单",
        ),
        sa.Column(
            "call_timeout_seconds",
            sa.Integer(),
            nullable=True,
            comment="单次调用超时秒数，空表示用全局上限，非空时与全局上限取较小值",
        ),
        sa.Column(
            "config_rev",
            sa.Integer(),
            nullable=False,
            comment="配置版本：地址、认证、过滤、超时、启用状态任一变更就加一",
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, comment="是否启用"),
        sa.Column(
            "last_probe_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次探测时间",
        ),
        sa.Column(
            "last_probe_error",
            sa.Text(),
            nullable=True,
            comment="最近一次探测的错误（已脱敏），成功时为空",
        ),
        sa.Column(
            "tools_snapshot",
            postgresql.JSONB(),
            nullable=False,
            comment="最近一次成功拉到的工具元数据，只用于管理展示、差异对比与权限规则校验",
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="创建者用户ID",
        ),
        *_audit_columns(),
        sa.CheckConstraint(
            "auth_type IN ('none', 'static_headers', 'oauth')",
            name="ck_mcp_servers_auth_type",
        ),
        sa.CheckConstraint(
            "approval_mode IN ('always', 'trust_annotations', 'never')",
            name="ck_mcp_servers_approval_mode",
        ),
        comment="MCP server 配置（共享与私有）",
    )
    for column in ("workspace_id", "owner_user_id", "created_by", "is_deleted"):
        op.create_index(f"ix_mcp_servers_{column}", "mcp_servers", [column])
    # server_key 在工作区内唯一，共享和私有 server 共用一个命名空间；并发创建同一个 key 时由它兜底。
    op.create_index(
        "uq_mcp_servers_active_workspace_key",
        "mcp_servers",
        ["workspace_id", "server_key"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.create_table(
        "mcp_server_secrets",
        sa.Column("id", sa.Integer(), primary_key=True, comment="凭证记录ID"),
        sa.Column(
            "server_id",
            sa.Integer(),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属 MCP server ID",
        ),
        sa.Column(
            "headers",
            postgresql.JSONB(),
            nullable=False,
            comment="静态请求头 {名称: 值}，明文，接口只回显名称",
        ),
        *_audit_columns(),
        comment="MCP server 凭证（静态请求头，明文）",
    )
    for column in ("server_id", "is_deleted"):
        op.create_index(f"ix_mcp_server_secrets_{column}", "mcp_server_secrets", [column])
    op.create_index(
        "uq_mcp_server_secrets_active_server",
        "mcp_server_secrets",
        ["server_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )


def downgrade() -> None:
    # 先删引用方，表上的索引和约束随表一起删除；表里的配置与凭证无法还原。
    op.drop_table("mcp_server_secrets")
    op.drop_table("mcp_servers")
