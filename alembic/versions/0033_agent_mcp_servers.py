"""新增 Agent 的 MCP server 绑定表 agent_mcp_servers（MCP 接入 M2）。

表归 agent 域：一行是自建 Agent 对一个共享 MCP server 的绑定。tool_names 是 MCP 原名白名单，
NULL 表示该 server 的全部工具；空列表由 Service 报中文错误，库里再用 CHECK 兜底。
解除绑定是软删，(agent_id, server_id) 只对有效行唯一（部分唯一索引），重新绑定插新行。

Revision ID: 0033_agent_mcp_servers
Revises: 0032_tool_calls_source
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033_agent_mcp_servers"
down_revision = "0032_tool_calls_source"
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
        "agent_mcp_servers",
        sa.Column("id", sa.Integer(), primary_key=True, comment="绑定ID"),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
            comment="Agent ID",
        ),
        sa.Column(
            "server_id",
            sa.Integer(),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
            comment="MCP server ID",
        ),
        sa.Column(
            "tool_names",
            postgresql.JSONB(none_as_null=True),
            nullable=True,
            comment="MCP 原名白名单，NULL 表示该 server 的全部工具，不允许空列表",
        ),
        *_audit_columns(),
        sa.CheckConstraint(
            "tool_names IS NULL OR tool_names <> '[]'::jsonb",
            name="ck_agent_mcp_servers_tool_names",
        ),
        comment="Agent MCP server绑定表",
    )
    for column in ("agent_id", "server_id", "is_deleted"):
        op.create_index(f"ix_agent_mcp_servers_{column}", "agent_mcp_servers", [column])
    op.create_index(
        "uq_agent_mcp_servers_active_agent_server",
        "agent_mcp_servers",
        ["agent_id", "server_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )


def downgrade() -> None:
    # 索引与约束随表一起删除；表里的绑定无法还原。
    op.drop_table("agent_mcp_servers")
