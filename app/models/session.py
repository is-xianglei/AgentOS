from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def json_type():
    return JSONB()


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__ = {"comment": "会话主记录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="会话ID")
    title: Mapped[str] = mapped_column(String(200), default="新会话", comment="会话标题")
    status: Mapped[str] = mapped_column(String(32), default="created", index=True, comment="会话状态")
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True, comment="模型名称")
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="系统提示词")
    extra: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="扩展信息",
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
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="最后活跃时间",
    )

    messages: Mapped[list["SessionMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    snapshots: Mapped[list["SessionSnapshot"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )


class SessionMessage(Base):
    __tablename__ = "session_messages"
    __table_args__ = {"comment": "会话上下文消息"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="消息ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    role: Mapped[str] = mapped_column(String(32), index=True, comment="消息角色")
    content: Mapped[Any] = mapped_column(json_type(), comment="消息内容")
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, comment="预估token数")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )

    session: Mapped[SessionRecord] = relationship(back_populates="messages")


class SessionSnapshot(Base):
    __tablename__ = "session_snapshots"
    __table_args__ = {"comment": "会话压缩快照"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="快照ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    snapshot_type: Mapped[str] = mapped_column(String(64), default="compact", comment="快照类型")
    messages: Mapped[list[Any]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="压缩后消息",
    )
    summary: Mapped[str] = mapped_column(Text, comment="压缩摘要")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )

    session: Mapped[SessionRecord] = relationship(back_populates="snapshots")
