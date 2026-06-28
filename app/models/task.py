from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.session import json_type


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = {"comment": "会话任务记录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="任务ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    subject: Mapped[str] = mapped_column(String(200), comment="任务主题")
    description: Mapped[str] = mapped_column(Text, default="", comment="任务描述")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True, comment="任务状态")
    owner: Mapped[str] = mapped_column(String(120), default="agent", comment="任务负责人")
    blocked_by: Mapped[list[Any]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="阻塞任务列表",
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
