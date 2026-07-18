"""创建子代理运行记录表

Revision ID: 0002_create_subagent_runs
Revises: 0001_create_agentos_base_tables
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_create_subagent_runs"
down_revision = "0001_create_agentos_base_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subagent_runs",
        sa.Column("id", sa.Integer(), primary_key=True, comment="运行ID"),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属会话ID",
        ),
        sa.Column("member_name", sa.String(length=120), nullable=False, comment="成员名称"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="运行状态"),
        sa.Column("prompt", sa.Text(), nullable=False, comment="任务提示"),
        sa.Column("report", sa.Text(), nullable=True, comment="运行报告"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="错误信息"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            comment="开始时间",
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间"),
        comment="子代理运行记录",
    )
    op.create_index("ix_subagent_runs_session_id", "subagent_runs", ["session_id"])
    op.create_index("ix_subagent_runs_member_name", "subagent_runs", ["member_name"])
    op.create_index("ix_subagent_runs_status", "subagent_runs", ["status"])


def downgrade() -> None:
    op.drop_table("subagent_runs")
