from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.base import json_type


class ToolRecord(Base):
    __tablename__ = "tools"
    __table_args__ = {"comment": "内置工具目录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="工具ID")
    tool_key: Mapped[str] = mapped_column(
        String(120),
        unique=True,
        index=True,
        comment="工具稳定标识",
    )
    name: Mapped[str] = mapped_column(String(120), comment="工具名称")
    description: Mapped[str] = mapped_column(Text, comment="工具描述")
    tool_type: Mapped[str] = mapped_column(
        String(32),
        default="builtin",
        index=True,
        comment="工具类型，MVP仅支持builtin",
    )
    runtime_name: Mapped[str] = mapped_column(
        String(120),
        unique=True,
        index=True,
        comment="代码注册表名称",
    )
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="工具输入JSON Schema",
    )
    implementation_config: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="工具实现配置，内置工具为空",
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        comment="是否系统内置工具",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        index=True,
        comment="是否启用",
    )


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
    tool_id: Mapped[int | None] = mapped_column(
        ForeignKey("tools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="调用的工具ID",
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="发起调用的Agent ID",
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
