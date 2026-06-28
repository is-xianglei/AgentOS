"""创建 teams 表并为 team_members 增加团队/运行时字段

Revision ID: 0004_create_teams
Revises: 0003_team_msg_consumer_inst
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_create_teams"
down_revision = "0003_team_msg_consumer_inst"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), primary_key=True, comment="团队ID"),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属会话ID(会话与团队 1:1)",
        ),
        sa.Column("team_name", sa.String(length=120), nullable=False, comment="团队名称"),
        sa.Column(
            "creator_type",
            sa.String(length=16),
            nullable=False,
            comment="团队创建者类型: user 用户手动组建 / agent 大模型自行组建",
        ),
        sa.Column(
            "creator_id",
            sa.String(length=120),
            nullable=True,
            comment="触发团队功能的人的唯一标识,仅用于展示不参与过滤",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="更新时间"),
        comment="会话团队",
    )
    op.create_index("ix_teams_session_id", "teams", ["session_id"], unique=True)

    op.add_column(
        "team_members",
        sa.Column(
            "team_id",
            sa.Integer(),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=True,
            comment="所属团队ID",
        ),
    )
    op.add_column(
        "team_members",
        sa.Column("agent_type", sa.String(length=120), nullable=True, comment="成员对应的子代理类型"),
    )
    op.add_column(
        "team_members",
        sa.Column("prompt", sa.Text(), nullable=True, comment="成员初始任务提示"),
    )
    op.add_column(
        "team_members",
        sa.Column("history", sa.Text(), nullable=True, comment="成员对话历史 JSON,用于跨请求恢复"),
    )
    op.create_index("ix_team_members_team_id", "team_members", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_team_members_team_id", table_name="team_members")
    op.drop_column("team_members", "history")
    op.drop_column("team_members", "prompt")
    op.drop_column("team_members", "agent_type")
    op.drop_column("team_members", "team_id")
    op.drop_index("ix_teams_session_id", table_name="teams")
    op.drop_table("teams")
