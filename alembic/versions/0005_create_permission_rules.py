"""创建 permission_rules 表(工具权限规则, 全局/会话双作用域)

Revision ID: 0005_permission_rules
Revises: 0004_create_teams
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_permission_rules"
down_revision = "0004_create_teams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "permission_rules",
        sa.Column("id", sa.Integer(), primary_key=True, comment="规则ID"),
        sa.Column("scope", sa.String(length=16), nullable=False, comment="作用域: global 全局 / session 会话级"),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=True,
            comment="所属会话ID(scope=session 时必填, global 时为空)",
        ),
        sa.Column("tool_name", sa.String(length=120), nullable=False, comment="规则针对的工具名"),
        sa.Column("behavior", sa.String(length=16), nullable=False, comment="判定行为: allow / ask / deny"),
        sa.Column("matcher", postgresql.JSONB(), nullable=True, comment="细化匹配条件(预留)"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="user", comment="规则来源: user / always_allow"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="更新时间"),
        comment="工具权限规则(全局 / 会话双作用域)",
    )
    op.create_index("ix_permission_rules_scope", "permission_rules", ["scope"])
    op.create_index("ix_permission_rules_session_id", "permission_rules", ["session_id"])
    op.create_index("ix_permission_rules_tool_name", "permission_rules", ["tool_name"])
    op.create_index(
        "ix_permission_rules_scope_session_tool",
        "permission_rules",
        ["scope", "session_id", "tool_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_permission_rules_scope_session_tool", table_name="permission_rules")
    op.drop_index("ix_permission_rules_tool_name", table_name="permission_rules")
    op.drop_index("ix_permission_rules_session_id", table_name="permission_rules")
    op.drop_index("ix_permission_rules_scope", table_name="permission_rules")
    op.drop_table("permission_rules")
