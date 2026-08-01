"""新增人工交互暂停点与Plan Mode持久状态。

Revision ID: 0016_interaction_plan_mode
Revises: 0015_add_memory_jobs
Create Date: 2026-08-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016_interaction_plan_mode"
down_revision = "0015_add_memory_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_session_turns_status", "session_turns", type_="check")
    op.execute(
        "UPDATE session_turns "
        "SET status = 'interrupted' "
        "WHERE status = 'awaiting_approval'"
    )
    # 旧架构没有可迁移的稳定 suspension。部署时安全中止在途审批，避免构造
    # 状态为 awaiting_interaction、实际却没有恢复点的假挂起会话。
    op.execute(
        "UPDATE sessions "
        "SET status = 'idle' "
        "WHERE status = 'awaiting_approval'"
    )
    op.execute(
        "UPDATE sessions "
        "SET metadata = COALESCE(metadata, '{}'::jsonb) - 'pending_approval' "
        "WHERE COALESCE(metadata, '{}'::jsonb) ? 'pending_approval'"
    )
    op.execute(
        "UPDATE tool_calls "
        "SET status = 'rejected', "
        "error_message = '升级人工交互架构时已中止旧审批', "
        "finished_at = COALESCE(finished_at, now()) "
        "WHERE status = 'awaiting_approval'"
    )
    op.create_check_constraint(
        "ck_session_turns_status",
        "session_turns",
        "status IN ('running', 'awaiting_interaction', 'completed', 'failed', 'interrupted')",
    )

    op.create_table(
        "runtime_suspensions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="暂停点ID",
        ),
        sa.Column("session_id", sa.Integer(), nullable=False, comment="所属会话ID"),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="所属交互轮次ID",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
            comment="状态: pending/resuming/resolved/cancelled/failed",
        ),
        sa.Column(
            "continuation_payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="当前顺序交互恢复运行所需的上下文",
        ),
        sa.Column(
            "resolution_policy",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="本次暂停冻结的裁决策略",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="暂停点状态变更版本",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="进入终态的时间",
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
            "status IN ('pending', 'resuming', 'resolved', 'cancelled', 'failed')",
            name="ck_runtime_suspensions_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_runtime_suspensions_version"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_runtime_suspensions_session_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["session_turns.id"],
            name="fk_runtime_suspensions_turn_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runtime_suspensions"),
        comment="Agent运行暂停点",
    )
    op.create_index(
        "ix_runtime_suspensions_session_id",
        "runtime_suspensions",
        ["session_id"],
    )
    op.create_index(
        "ix_runtime_suspensions_turn_id",
        "runtime_suspensions",
        ["turn_id"],
    )
    op.create_index(
        "ix_runtime_suspensions_status",
        "runtime_suspensions",
        ["status"],
    )
    op.create_index(
        "ix_runtime_suspensions_is_deleted",
        "runtime_suspensions",
        ["is_deleted"],
    )
    op.create_index(
        "uq_runtime_suspensions_active_session",
        "runtime_suspensions",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(
            "is_deleted = false AND status IN ('pending', 'resuming')"
        ),
    )

    op.create_table(
        "interaction_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="交互请求ID",
        ),
        sa.Column(
            "suspension_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="所属暂停点ID",
        ),
        sa.Column("session_id", sa.Integer(), nullable=False, comment="所属会话ID"),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="所属交互轮次ID",
        ),
        sa.Column(
            "tool_call_id",
            sa.Integer(),
            nullable=True,
            comment="关联工具调用记录ID",
        ),
        sa.Column(
            "tool_use_id",
            sa.String(length=255),
            nullable=True,
            comment="模型返回的工具调用ID",
        ),
        sa.Column("kind", sa.String(length=32), nullable=False, comment="交互类型"),
        sa.Column(
            "schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="请求载荷结构版本",
        ),
        sa.Column(
            "sequence",
            sa.Integer(),
            nullable=False,
            comment="暂停点内严格递增的请求序号",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
            comment="状态: pending/resolved/cancelled/expired",
        ),
        sa.Column(
            "request_payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="冻结的交互请求载荷",
        ),
        sa.Column(
            "response_payload",
            postgresql.JSONB(),
            nullable=True,
            comment="用户响应载荷",
        ),
        sa.Column(
            "responded_by",
            sa.Integer(),
            nullable=True,
            comment="响应用户ID",
        ),
        sa.Column(
            "responded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="响应时间",
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
            "kind IN ('tool_approval', 'user_question', 'enter_plan_mode', "
            "'exit_plan_mode')",
            name="ck_interaction_requests_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved', 'cancelled', 'expired')",
            name="ck_interaction_requests_status",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_interaction_requests_schema_version",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_interaction_requests_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["suspension_id"],
            ["runtime_suspensions.id"],
            name="fk_interaction_requests_suspension_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_interaction_requests_session_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["session_turns.id"],
            name="fk_interaction_requests_turn_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_calls.id"],
            name="fk_interaction_requests_tool_call_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["responded_by"],
            ["users.id"],
            name="fk_interaction_requests_responded_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_interaction_requests"),
        sa.UniqueConstraint(
            "suspension_id",
            "sequence",
            name="uq_interaction_requests_suspension_sequence",
        ),
        comment="人工交互请求",
    )
    for column_name in (
        "suspension_id",
        "session_id",
        "turn_id",
        "tool_call_id",
        "tool_use_id",
        "kind",
        "status",
        "responded_by",
        "is_deleted",
    ):
        op.create_index(
            f"ix_interaction_requests_{column_name}",
            "interaction_requests",
            [column_name],
        )
    op.create_index(
        "ix_interaction_requests_session_status_created",
        "interaction_requests",
        ["session_id", "status", "created_at"],
    )

    op.create_table(
        "session_plans",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="计划ID",
        ),
        sa.Column("session_id", sa.Integer(), nullable=False, comment="所属会话ID"),
        sa.Column(
            "entered_turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="进入Plan Mode的交互轮次ID",
        ),
        sa.Column(
            "exited_turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="退出Plan Mode的交互轮次ID",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="状态: active/approved/cancelled",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="Markdown计划正文",
        ),
        sa.Column(
            "previous_mode",
            sa.String(length=32),
            nullable=False,
            comment="进入前的运行模式",
        ),
        sa.Column(
            "allowed_prompts",
            postgresql.JSONB(),
            nullable=False,
            comment="获批计划请求的语义权限声明，仅持久化不自动授权",
        ),
        sa.Column(
            "feedback",
            sa.Text(),
            nullable=True,
            comment="最近一次用户反馈",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="内容版本号",
        ),
        sa.Column(
            "approved_by",
            sa.Integer(),
            nullable=True,
            comment="批准退出的用户ID",
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="计划批准时间",
        ),
        sa.Column(
            "exited_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="退出Plan Mode时间",
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
            "status IN ('active', 'approved', 'cancelled')",
            name="ck_session_plans_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_session_plans_version"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_session_plans_session_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entered_turn_id"],
            ["session_turns.id"],
            name="fk_session_plans_entered_turn_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["exited_turn_id"],
            ["session_turns.id"],
            name="fk_session_plans_exited_turn_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_session_plans_approved_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_session_plans"),
        comment="会话Plan Mode生命周期记录",
    )
    op.create_index("ix_session_plans_session_id", "session_plans", ["session_id"])
    op.create_index(
        "ix_session_plans_entered_turn_id",
        "session_plans",
        ["entered_turn_id"],
    )
    op.create_index(
        "ix_session_plans_exited_turn_id",
        "session_plans",
        ["exited_turn_id"],
    )
    op.create_index("ix_session_plans_status", "session_plans", ["status"])
    op.create_index("ix_session_plans_is_deleted", "session_plans", ["is_deleted"])
    op.create_index(
        "uq_session_plans_one_active",
        "session_plans",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND is_deleted = false"),
    )


def downgrade() -> None:
    op.drop_table("session_plans")
    op.drop_table("interaction_requests")
    op.drop_table("runtime_suspensions")

    op.drop_constraint("ck_session_turns_status", "session_turns", type_="check")
    op.execute(
        "UPDATE session_turns "
        "SET status = 'interrupted' "
        "WHERE status = 'awaiting_interaction'"
    )
    op.execute(
        "UPDATE sessions SET status = 'idle' "
        "WHERE status = 'awaiting_interaction'"
    )
    op.execute(
        "UPDATE tool_calls "
        "SET status = 'rejected', "
        "error_message = '降级人工交互架构时已中止交互', "
        "finished_at = COALESCE(finished_at, now()) "
        "WHERE status = 'awaiting_interaction'"
    )
    op.create_check_constraint(
        "ck_session_turns_status",
        "session_turns",
        "status IN ('running', 'awaiting_approval', 'completed', 'failed', 'interrupted')",
    )
