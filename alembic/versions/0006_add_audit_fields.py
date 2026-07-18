"""为所有表补充统一审计字段(创建/更新时间 + 软删除标记)

Revision ID: 0006_add_audit_fields
Revises: 0005_permission_rules
Create Date: 2026-07-17

统一审计字段现由 db.base.Base 直接提供(列下发给所有子类)。本迁移把已有表
补齐到 Base 定义的四列:created_at / updated_at / is_deleted / deleted_at。
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_add_audit_fields"
down_revision = "0005_permission_rules"
branch_labels = None
depends_on = None

# 全部业务表,软删除字段统一新增
ALL_TABLES = [
    "sessions",
    "session_messages",
    "session_snapshots",
    "tasks",
    "teams",
    "team_members",
    "team_messages",
    "subagent_runs",
    "permission_rules",
    "tool_calls",
]

# 之前只有 started_at/finished_at,缺 created_at 的表
NEED_CREATED_AT = ["subagent_runs", "tool_calls"]

# 仅有 created_at、缺 updated_at 的表(含上面两张)
NEED_UPDATED_AT = [
    "session_messages",
    "session_snapshots",
    "team_messages",
    "subagent_runs",
    "tool_calls",
]


def upgrade() -> None:
    for table in NEED_CREATED_AT:
        op.add_column(
            table,
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
                comment="创建时间",
            ),
        )

    for table in NEED_UPDATED_AT:
        op.add_column(
            table,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
                comment="更新时间",
            ),
        )

    for table in ALL_TABLES:
        op.add_column(
            table,
            sa.Column(
                "is_deleted",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
                comment="软删除标记: false 有效 / true 已删除",
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "deleted_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="软删除时间, 空表示未删除",
            ),
        )
        op.create_index(f"ix_{table}_is_deleted", table, ["is_deleted"])


def downgrade() -> None:
    for table in ALL_TABLES:
        op.drop_index(f"ix_{table}_is_deleted", table_name=table)
        op.drop_column(table, "deleted_at")
        op.drop_column(table, "is_deleted")

    for table in NEED_UPDATED_AT:
        op.drop_column(table, "updated_at")

    for table in NEED_CREATED_AT:
        op.drop_column(table, "created_at")
