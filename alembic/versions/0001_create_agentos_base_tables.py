"""创建 AgentOS 第一阶段基础表

Revision ID: 0001_create_agentos_base_tables
Revises:
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_create_agentos_base_tables"
down_revision = None
branch_labels = None
depends_on = None


def jsonb_type():
    return postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True, comment="会话ID"),
        sa.Column("title", sa.String(length=200), nullable=False, comment="会话标题"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="会话状态"),
        sa.Column("model_name", sa.String(length=120), nullable=True, comment="模型名称"),
        sa.Column("system_prompt", sa.Text(), nullable=True, comment="系统提示词"),
        sa.Column("metadata", jsonb_type(), nullable=False, comment="扩展信息"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="更新时间"),
        sa.Column("last_active_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="最后活跃时间"),
        comment="会话主记录",
    )
    op.create_index("ix_sessions_status", "sessions", ["status"])

    op.create_table(
        "session_messages",
        sa.Column("id", sa.Integer(), primary_key=True, comment="消息ID"),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, comment="所属会话ID"),
        sa.Column("role", sa.String(length=32), nullable=False, comment="消息角色"),
        sa.Column("content", jsonb_type(), nullable=False, comment="消息内容"),
        sa.Column("token_estimate", sa.Integer(), nullable=False, comment="预估token数"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        comment="会话上下文消息",
    )
    op.create_index("ix_session_messages_session_id", "session_messages", ["session_id"])
    op.create_index("ix_session_messages_role", "session_messages", ["role"])

    op.create_table(
        "session_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, comment="快照ID"),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, comment="所属会话ID"),
        sa.Column("snapshot_type", sa.String(length=64), nullable=False, comment="快照类型"),
        sa.Column("messages", jsonb_type(), nullable=False, comment="压缩后消息"),
        sa.Column("summary", sa.Text(), nullable=False, comment="压缩摘要"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        comment="会话压缩快照",
    )
    op.create_index("ix_session_snapshots_session_id", "session_snapshots", ["session_id"])

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True, comment="工具调用ID"),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, comment="所属会话ID"),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("session_messages.id", ondelete="SET NULL"), nullable=True, comment="关联消息ID"),
        sa.Column("tool_name", sa.String(length=120), nullable=False, comment="工具名称"),
        sa.Column("input_args", jsonb_type(), nullable=False, comment="输入参数"),
        sa.Column("output_data", jsonb_type(), nullable=True, comment="输出结果"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="调用状态"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="错误信息"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="开始时间"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间"),
        comment="工具调用记录",
    )
    op.create_index("ix_tool_calls_session_id", "tool_calls", ["session_id"])
    op.create_index("ix_tool_calls_message_id", "tool_calls", ["message_id"])
    op.create_index("ix_tool_calls_tool_name", "tool_calls", ["tool_name"])
    op.create_index("ix_tool_calls_status", "tool_calls", ["status"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True, comment="任务ID"),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, comment="所属会话ID"),
        sa.Column("subject", sa.String(length=200), nullable=False, comment="任务主题"),
        sa.Column("description", sa.Text(), nullable=False, comment="任务描述"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="任务状态"),
        sa.Column("owner", sa.String(length=120), nullable=False, comment="任务负责人"),
        sa.Column("blocked_by", jsonb_type(), nullable=False, comment="阻塞任务列表"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="更新时间"),
        comment="会话任务记录",
    )
    op.create_index("ix_tasks_session_id", "tasks", ["session_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "team_members",
        sa.Column("id", sa.Integer(), primary_key=True, comment="成员ID"),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, comment="所属会话ID"),
        sa.Column("name", sa.String(length=120), nullable=False, comment="成员名称"),
        sa.Column("role", sa.String(length=120), nullable=False, comment="成员角色"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="成员状态"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="更新时间"),
        comment="会话团队成员",
    )
    op.create_index("ix_team_members_session_id", "team_members", ["session_id"])
    op.create_index("ix_team_members_name", "team_members", ["name"])
    op.create_index("ix_team_members_status", "team_members", ["status"])

    op.create_table(
        "team_messages",
        sa.Column("id", sa.Integer(), primary_key=True, comment="消息ID"),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, comment="所属会话ID"),
        sa.Column("sender", sa.String(length=120), nullable=False, comment="发送方"),
        sa.Column("recipient", sa.String(length=120), nullable=False, comment="接收方"),
        sa.Column("message_type", sa.String(length=64), nullable=False, comment="消息类型"),
        sa.Column("content", sa.Text(), nullable=False, comment="消息内容"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True, comment="读取时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        comment="会话团队消息",
    )
    op.create_index("ix_team_messages_session_id", "team_messages", ["session_id"])
    op.create_index("ix_team_messages_sender", "team_messages", ["sender"])
    op.create_index("ix_team_messages_recipient", "team_messages", ["recipient"])


def downgrade() -> None:
    op.drop_table("team_messages")
    op.drop_table("team_members")
    op.drop_table("tasks")
    op.drop_table("tool_calls")
    op.drop_table("session_snapshots")
    op.drop_table("session_messages")
    op.drop_table("sessions")
