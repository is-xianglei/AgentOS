from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.session import json_type


class ToolCallRecord(Base):
    __tablename__ = "tool_calls"
    __table_args__ = {"comment": "工具调用记录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="工具调用ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("session_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="关联消息ID",
    )
    tool_name: Mapped[str] = mapped_column(String(120), index=True, comment="工具名称")
    input_args: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="输入参数",
    )
    output_data: Mapped[Any | None] = mapped_column(json_type(), nullable=True, comment="输出结果")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True, comment="调用状态")
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
