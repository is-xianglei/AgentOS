"""新增独立的 Alembic 迁移执行历史表。

Revision ID: 0018_alembic_history
Revises: 0017_workspace_org_units
Create Date: 2026-08-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018_alembic_history"
down_revision = "0017_workspace_org_units"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alembic_version_history",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
            comment="历史记录ID",
        ),
        sa.Column("revision", sa.String(length=255), nullable=False, comment="迁移版本"),
        sa.Column(
            "operation",
            sa.String(length=16),
            nullable=False,
            comment="操作类型: upgrade/downgrade/stamp/baseline",
        ),
        sa.Column(
            "source_revisions",
            postgresql.JSONB(),
            nullable=False,
            comment="执行前的活动版本",
        ),
        sa.Column(
            "destination_revisions",
            postgresql.JSONB(),
            nullable=False,
            comment="执行后的目标版本",
        ),
        sa.Column(
            "resulting_heads",
            postgresql.JSONB(),
            nullable=False,
            comment="执行后的全部Head",
        ),
        sa.Column(
            "is_backfilled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
            comment="是否为无法还原执行时间的历史回填记录",
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="记录时间",
        ),
        sa.CheckConstraint(
            "operation IN ('upgrade', 'downgrade', 'stamp', 'baseline')",
            name="ck_alembic_version_history_operation",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alembic_version_history"),
        comment="Alembic迁移执行历史；当前版本仍以alembic_version为准",
    )
    op.create_index(
        "ix_alembic_version_history_revision",
        "alembic_version_history",
        ["revision"],
    )
    op.create_index(
        "ix_alembic_version_history_recorded_at",
        "alembic_version_history",
        ["recorded_at"],
    )

    # 旧版本只有当前 Head 记录，无法还原真实执行时间；按迁移图回填为 baseline。
    op.execute(
        """
        WITH revisions AS (
            SELECT item.revision, item.ordinality
            FROM unnest(ARRAY[
                '0001_create_agentos_base_tables',
                '0002_create_subagent_runs',
                '0003_team_msg_consumer_inst',
                '0004_create_teams',
                '0005_permission_rules',
                '0006_add_audit_fields',
                '0007_create_skills',
                'd5245f5576f8',
                '0008_create_workspaces',
                '0009_workspace_members',
                '0010_add_user_workspace',
                '0011_fix_users_table_schema',
                '0012_add_session_turns',
                '0013_add_memory_storage',
                '0014_add_turn_memory_contexts',
                '0015_add_memory_jobs',
                '0016_interaction_plan_mode',
                '0017_workspace_org_units'
            ]::varchar[]) WITH ORDINALITY AS item(revision, ordinality)
        ),
        chained AS (
            SELECT revision,
                   lag(revision) OVER (ORDER BY ordinality) AS previous_revision
            FROM revisions
        )
        INSERT INTO alembic_version_history (
            revision,
            operation,
            source_revisions,
            destination_revisions,
            resulting_heads,
            is_backfilled
        )
        SELECT revision,
               'baseline',
               CASE
                   WHEN previous_revision IS NULL THEN '[]'::jsonb
                   ELSE jsonb_build_array(previous_revision)
               END,
               jsonb_build_array(revision),
               jsonb_build_array(revision),
               true
        FROM chained
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_alembic_version_history_recorded_at",
        table_name="alembic_version_history",
    )
    op.drop_index(
        "ix_alembic_version_history_revision",
        table_name="alembic_version_history",
    )
    op.drop_table("alembic_version_history")
