"""新增 MCP OAuth 的客户端注册表 mcp_oauth_clients 与用户 token 表 mcp_oauth_tokens（MCP 接入 M3）。

客户端注册（CIMD 或 DCR 的结果，可能含 client_secret）一个 server 一行；token 按 (server, 用户) 一行。
两张表都明文存储、接口不回显，有效行由部分唯一索引（is_deleted = false）约束：
授权服务器更换时软删旧注册再插新行，撤销授权即软删 token，之后可以重新授权。

Revision ID: 0034_mcp_oauth
Revises: 0033_agent_mcp_servers
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0034_mcp_oauth"
down_revision = "0033_agent_mcp_servers"
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
        "mcp_oauth_clients",
        sa.Column("id", sa.Integer(), primary_key=True, comment="客户端注册记录ID"),
        sa.Column(
            "server_id",
            sa.Integer(),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属 MCP server ID",
        ),
        sa.Column(
            "issuer",
            sa.String(2048),
            nullable=True,
            comment="注册时针对的授权服务器 issuer，用于排查与展示",
        ),
        sa.Column(
            "client_info",
            postgresql.JSONB(),
            nullable=False,
            comment="SDK 的 OAuthClientInformationFull，可能含 client_secret，明文，不回显",
        ),
        *_audit_columns(),
        comment="MCP server 的 OAuth 客户端注册信息（明文）",
    )
    for column in ("server_id", "is_deleted"):
        op.create_index(f"ix_mcp_oauth_clients_{column}", "mcp_oauth_clients", [column])
    op.create_index(
        "uq_mcp_oauth_clients_active_server",
        "mcp_oauth_clients",
        ["server_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.create_table(
        "mcp_oauth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, comment="token 记录ID"),
        sa.Column(
            "server_id",
            sa.Integer(),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属 MCP server ID",
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            comment="授权用户ID",
        ),
        sa.Column(
            "issuer",
            sa.String(2048),
            nullable=True,
            comment="签发 token 的授权服务器 issuer",
        ),
        sa.Column(
            "token",
            postgresql.JSONB(),
            nullable=False,
            comment="SDK 的 OAuthToken（含 access_token、refresh_token），明文，不回显",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="access_token 过期时间，空表示授权服务器未给出，用于提前刷新",
        ),
        sa.Column("scope", sa.Text(), nullable=True, comment="授权的 scope，用于展示与排查"),
        *_audit_columns(),
        comment="MCP server 的用户 OAuth token（明文）",
    )
    for column in ("server_id", "user_id", "is_deleted"):
        op.create_index(f"ix_mcp_oauth_tokens_{column}", "mcp_oauth_tokens", [column])
    # 一个用户在一个 server 上只有一份有效 token；撤销（软删）之后可以重新授权。
    op.create_index(
        "uq_mcp_oauth_tokens_active_server_user",
        "mcp_oauth_tokens",
        ["server_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )


def downgrade() -> None:
    # 表上的索引随表一起删除；表里的客户端注册与 token 无法还原，用户需要重新授权。
    op.drop_table("mcp_oauth_tokens")
    op.drop_table("mcp_oauth_clients")
