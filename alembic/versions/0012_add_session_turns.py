"""新增会话交互轮次与快照消息水位。

Revision ID: 0012_add_session_turns
Revises: 0011_fix_users_table_schema
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_add_session_turns"
down_revision = "0011_fix_users_table_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, comment="轮次ID"),
        sa.Column("session_id", sa.Integer(), nullable=False, comment="所属会话ID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="发起用户ID"),
        sa.Column("workspace_id", sa.Integer(), nullable=False, comment="可信工作区ID"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="轮次状态"),
        sa.Column(
            "started_message_id",
            sa.Integer(),
            nullable=False,
            comment="启动轮次的原始用户消息ID",
        ),
        sa.Column(
            "completed_message_id",
            sa.Integer(),
            nullable=True,
            comment="成功完成轮次的最终助手消息ID",
        ),
        sa.Column(
            "memory_context_id",
            sa.BigInteger(),
            nullable=True,
            comment="冻结的Memory上下文ID",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="轮次开始处理时间",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="轮次成功完成时间",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="更新时间",
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
            comment="软删除标记: false 有效 / true 已删除",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间, 空表示未删除",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'awaiting_approval', 'completed', 'failed', 'interrupted')",
            name="ck_session_turns_status",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_message_id IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND completed_message_id IS NULL "
            "AND completed_at IS NULL)",
            name="ck_session_turns_completion",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_session_turns_session_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_session_turns_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_session_turns_workspace_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["started_message_id"],
            ["session_messages.id"],
            name="fk_session_turns_started_message_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["completed_message_id"],
            ["session_messages.id"],
            name="fk_session_turns_completed_message_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_session_turns"),
        sa.UniqueConstraint("started_message_id", name="uq_session_turns_started_message_id"),
        sa.UniqueConstraint(
            "completed_message_id",
            name="uq_session_turns_completed_message_id",
        ),
        comment="Agent交互轮次记录",
    )
    op.create_index("ix_session_turns_session_id", "session_turns", ["session_id"])
    op.create_index("ix_session_turns_user_id", "session_turns", ["user_id"])
    op.create_index("ix_session_turns_workspace_id", "session_turns", ["workspace_id"])
    op.create_index("ix_session_turns_status", "session_turns", ["status"])
    op.create_index("ix_session_turns_is_deleted", "session_turns", ["is_deleted"])

    op.add_column(
        "session_messages",
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="所属交互轮次ID，历史消息可为空",
        ),
    )
    op.create_foreign_key(
        "fk_session_messages_turn_id",
        "session_messages",
        "session_turns",
        ["turn_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_session_messages_turn_id", "session_messages", ["turn_id"])

    op.add_column(
        "session_snapshots",
        sa.Column(
            "through_message_id",
            sa.Integer(),
            nullable=True,
            comment="快照已覆盖到的消息ID，旧快照为空时视为无效",
        ),
    )
    op.create_foreign_key(
        "fk_session_snapshots_through_message_id",
        "session_snapshots",
        "session_messages",
        ["through_message_id"],
        ["id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "ix_session_snapshots_through_message_id",
        "session_snapshots",
        ["through_message_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_snapshots_through_message_id",
        table_name="session_snapshots",
    )
    op.drop_constraint(
        "fk_session_snapshots_through_message_id",
        "session_snapshots",
        type_="foreignkey",
    )
    op.drop_column("session_snapshots", "through_message_id")

    op.drop_index("ix_session_messages_turn_id", table_name="session_messages")
    op.drop_constraint(
        "fk_session_messages_turn_id",
        "session_messages",
        type_="foreignkey",
    )
    op.drop_column("session_messages", "turn_id")

    op.drop_index("ix_session_turns_is_deleted", table_name="session_turns")
    op.drop_index("ix_session_turns_status", table_name="session_turns")
    op.drop_index("ix_session_turns_workspace_id", table_name="session_turns")
    op.drop_index("ix_session_turns_user_id", table_name="session_turns")
    op.drop_index("ix_session_turns_session_id", table_name="session_turns")
    op.drop_table("session_turns")
