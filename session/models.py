from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, json_type

if TYPE_CHECKING:
    from user.models import UserRecord
    from workspace.models import WorkspaceRecord


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__: ClassVar[dict[str, str]] = {"comment": "会话主记录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="会话ID")
    title: Mapped[str] = mapped_column(String(200), default="新会话", comment="会话标题")
    status: Mapped[str] = mapped_column(
        String(32), default="created", index=True, comment="会话状态"
    )
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True, comment="模型名称")
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="系统提示词")
    extra: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="扩展信息",
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="最后活跃时间",
    )

    # 用户与工作区关联（新增字段）
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="创建用户ID",
    )
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="所属工作区ID",
    )

    # 共享与权限（新增字段）
    visibility: Mapped[str] = mapped_column(
        String(32),
        default="private",
        index=True,
        comment="可见性: private/team/workspace/public",
    )
    shared_with: Mapped[list[int]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="共享用户ID列表",
    )

    messages: Mapped[list[SessionMessage]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    snapshots: Mapped[list[SessionSnapshot]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    turns: Mapped[list[SessionTurnRecord]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )

    # 关联关系（新增）
    user: Mapped[UserRecord | None] = relationship(
        "UserRecord", foreign_keys=[user_id], back_populates="sessions"
    )
    workspace: Mapped[WorkspaceRecord | None] = relationship(
        "WorkspaceRecord", foreign_keys=[workspace_id], back_populates="sessions"
    )


class SessionMessage(Base):
    __tablename__ = "session_messages"
    __table_args__: ClassVar[dict[str, str]] = {"comment": "会话上下文消息"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="消息ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="所属交互轮次ID，历史消息可为空",
    )
    role: Mapped[str] = mapped_column(String(32), index=True, comment="消息角色")
    content: Mapped[Any] = mapped_column(json_type(), comment="消息内容")
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, comment="预估token数")

    session: Mapped[SessionRecord] = relationship(back_populates="messages")
    turn: Mapped[SessionTurnRecord | None] = relationship(
        back_populates="messages",
        foreign_keys=[turn_id],
    )


class SessionTurnRecord(Base):
    __tablename__ = "session_turns"
    __table_args__ = (
        UniqueConstraint(
            "memory_context_id",
            name="uq_session_turns_memory_context_id",
        ),
        CheckConstraint(
            "status IN ('running', 'awaiting_interaction', 'completed', 'failed', 'interrupted')",
            name="ck_session_turns_status",
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_message_id IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND completed_message_id IS NULL "
            "AND completed_at IS NULL)",
            name="ck_session_turns_completion",
        ),
        {"comment": "Agent交互轮次记录"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="轮次ID",
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        comment="发起用户ID",
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="可信工作区ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="running",
        index=True,
        comment="轮次状态",
    )
    started_message_id: Mapped[int] = mapped_column(
        ForeignKey(
            "session_messages.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        unique=True,
        comment="启动轮次的原始用户消息ID",
    )
    completed_message_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "session_messages.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        unique=True,
        comment="成功完成轮次的最终助手消息ID",
    )
    memory_context_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("turn_memory_contexts.id", ondelete="SET NULL"),
        nullable=True,
        comment="冻结的Memory上下文ID",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="轮次开始处理时间",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="轮次成功完成时间",
    )

    session: Mapped[SessionRecord] = relationship(back_populates="turns")
    messages: Mapped[list[SessionMessage]] = relationship(
        back_populates="turn",
        foreign_keys=[SessionMessage.turn_id],
    )


class SessionSnapshot(Base):
    __tablename__ = "session_snapshots"
    __table_args__: ClassVar[dict[str, str]] = {"comment": "会话压缩快照"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="快照ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    through_message_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "session_messages.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
        comment="快照已覆盖到的消息ID，旧快照为空时视为无效",
    )
    snapshot_type: Mapped[str] = mapped_column(String(64), default="compact", comment="快照类型")
    messages: Mapped[list[Any]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="压缩后消息",
    )
    summary: Mapped[str] = mapped_column(Text, comment="压缩摘要")

    session: Mapped[SessionRecord] = relationship(back_populates="snapshots")
