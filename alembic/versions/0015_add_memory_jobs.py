"""新增Memory持久后台任务表。

Revision ID: 0015_add_memory_jobs
Revises: 0014_add_turn_memory_contexts
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_add_memory_jobs"
down_revision = "0014_add_turn_memory_contexts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, comment="任务ID"),
        sa.Column(
            "job_type",
            sa.String(length=16),
            nullable=False,
            comment="任务类型: extract/dream",
        ),
        sa.Column("space_id", sa.BigInteger(), nullable=False, comment="目标Memory空间ID"),
        sa.Column("session_id", sa.Integer(), nullable=True, comment="来源会话ID"),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="来源交互轮次ID",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=255),
            nullable=False,
            comment="全局唯一幂等键",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
            comment="状态: pending/running/retry/succeeded/dead",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="累计尝试次数",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
            comment="最大尝试次数",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="允许认领时间",
        ),
        sa.Column(
            "lease_until",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="租约到期时间",
        ),
        sa.Column(
            "worker_id",
            sa.String(length=120),
            nullable=True,
            comment="当前租约持有者ID",
        ),
        sa.Column(
            "model",
            sa.String(length=120),
            nullable=True,
            comment="执行任务使用的模型",
        ),
        sa.Column(
            "prompt_version",
            sa.String(length=64),
            nullable=True,
            comment="任务提示词版本",
        ),
        sa.Column(
            "base_catalog_version",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="任务基于的Catalog版本",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="首次开始执行时间",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最终完成时间",
        ),
        sa.Column(
            "last_error_code",
            sa.String(length=64),
            nullable=True,
            comment="最近一次失败代码",
        ),
        sa.Column(
            "last_error_message",
            sa.String(length=1000),
            nullable=True,
            comment="最近一次失败摘要",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="不含正文和原始对话的任务扩展参数",
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
            "job_type IN ('extract', 'dream')",
            name="ck_memory_jobs_job_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retry', 'succeeded', 'dead')",
            name="ck_memory_jobs_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_jobs_attempts"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_memory_jobs_max_attempts"),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name="ck_memory_jobs_attempt_limit",
        ),
        sa.CheckConstraint(
            "base_catalog_version >= 0",
            name="ck_memory_jobs_base_catalog_version",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["memory_spaces.id"],
            name="fk_memory_jobs_space_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_memory_jobs_session_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["session_turns.id"],
            name="fk_memory_jobs_turn_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_jobs"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_memory_jobs_idempotency_key",
        ),
        comment="Memory持久后台任务",
    )
    op.create_index("ix_memory_jobs_space_id", "memory_jobs", ["space_id"])
    op.create_index("ix_memory_jobs_session_id", "memory_jobs", ["session_id"])
    op.create_index("ix_memory_jobs_turn_id", "memory_jobs", ["turn_id"])
    op.create_index("ix_memory_jobs_status", "memory_jobs", ["status"])
    op.create_index("ix_memory_jobs_is_deleted", "memory_jobs", ["is_deleted"])
    # 认领 SQL 除 pending/retry 外还有直接接管 lease 过期 running Job 的 OR 分支，
    # 部分索引必须覆盖这三种状态，否则 OR 条件会使索引失效退化为全表扫描。
    op.create_index(
        "ix_memory_jobs_claim",
        "memory_jobs",
        ["status", "available_at", "lease_until", "id"],
        postgresql_where=sa.text(
            "is_deleted = false AND status IN ('pending', 'retry', 'running')"
        ),
    )
    op.create_index(
        "ix_memory_jobs_space_type_status",
        "memory_jobs",
        ["space_id", "job_type", "status"],
    )


def downgrade() -> None:
    op.drop_table("memory_jobs")
