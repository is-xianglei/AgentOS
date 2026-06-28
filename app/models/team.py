from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TeamRecord(Base):
    __tablename__ = "teams"
    __table_args__ = {"comment": "会话团队"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="团队ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        comment="所属会话ID(会话与团队 1:1)",
    )
    team_name: Mapped[str] = mapped_column(String(120), default="default", comment="团队名称")
    creator_type: Mapped[str] = mapped_column(
        String(16),
        comment="团队创建者类型: user 用户手动组建 / agent 大模型自行组建",
    )
    creator_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="触发团队功能的人的唯一标识,仅用于展示不参与过滤",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )


class TeamMemberRecord(Base):
    __tablename__ = "team_members"
    __table_args__ = {"comment": "会话团队成员"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="成员ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="所属团队ID",
    )
    name: Mapped[str] = mapped_column(String(120), index=True, comment="成员名称")
    role: Mapped[str] = mapped_column(String(120), comment="成员角色")
    status: Mapped[str] = mapped_column(String(32), default="idle", index=True, comment="成员状态")
    agent_type: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="成员对应的子代理类型",
    )
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="成员初始任务提示")
    history: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="成员对话历史 JSON,用于跨请求恢复",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )


class TeamMessageRecord(Base):
    __tablename__ = "team_messages"
    __table_args__ = {"comment": "会话团队消息"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="消息ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    sender: Mapped[str] = mapped_column(String(120), index=True, comment="发送方")
    recipient: Mapped[str] = mapped_column(String(120), index=True, comment="接收方")
    message_type: Mapped[str] = mapped_column(String(64), default="direct", comment="消息类型")
    content: Mapped[str] = mapped_column(Text, comment="消息内容")
    consumer_instance_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        index=True,
        comment="限定消费实例ID",
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="读取时间",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )


class SubAgentRunRecord(Base):
    __tablename__ = "subagent_runs"
    __table_args__ = {"comment": "子代理运行记录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="运行ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    member_name: Mapped[str] = mapped_column(String(120), index=True, comment="成员名称")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True, comment="运行状态")
    prompt: Mapped[str] = mapped_column(Text, comment="任务提示")
    report: Mapped[str | None] = mapped_column(Text, nullable=True, comment="运行报告")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="开始时间",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="结束时间",
    )
