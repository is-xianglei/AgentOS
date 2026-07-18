"""添加团队任务消费实例隔离字段

Revision ID: 0003_team_msg_consumer_inst
Revises: 0002_create_subagent_runs
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_team_msg_consumer_inst"
down_revision = "0002_create_subagent_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "team_messages",
        sa.Column(
            "consumer_instance_id",
            sa.String(length=120),
            nullable=True,
            comment="限定消费实例ID",
        ),
    )
    op.create_index(
        "ix_team_messages_consumer_instance_id",
        "team_messages",
        ["consumer_instance_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_team_messages_consumer_instance_id", table_name="team_messages")
    op.drop_column("team_messages", "consumer_instance_id")
