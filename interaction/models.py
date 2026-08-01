from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, json_type


class RuntimeSuspensionRecord(Base):
    """可跨请求恢复的 Agent 运行暂停点。"""

    __tablename__ = "runtime_suspensions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'resuming', 'resolved', 'cancelled', 'failed')",
            name="ck_runtime_suspensions_status",
        ),
        CheckConstraint("version >= 1", name="ck_runtime_suspensions_version"),
        Index(
            "uq_runtime_suspensions_active_session",
            "session_id",
            unique=True,
            postgresql_where=text(
                "is_deleted = false AND status IN ('pending', 'resuming')"
            ),
        ),
        {"comment": "Agent运行暂停点"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="暂停点ID",
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    turn_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="CASCADE"),
        index=True,
        comment="所属交互轮次ID",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default=text("'pending'"),
        index=True,
        comment="状态: pending/resuming/resolved/cancelled/failed",
    )
    continuation_payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="当前顺序交互恢复运行所需的上下文",
    )
    resolution_policy: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="本次暂停冻结的裁决策略",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        comment="暂停点状态变更版本",
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="进入终态的时间",
    )

    requests: Mapped[list[InteractionRequestRecord]] = relationship(
        back_populates="suspension",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="InteractionRequestRecord.sequence",
    )


class InteractionRequestRecord(Base):
    """暂停点下的一项人工交互请求。"""

    __tablename__ = "interaction_requests"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('tool_approval', 'user_question', 'enter_plan_mode', "
            "'exit_plan_mode')",
            name="ck_interaction_requests_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'resolved', 'cancelled', 'expired')",
            name="ck_interaction_requests_status",
        ),
        CheckConstraint("schema_version >= 1", name="ck_interaction_requests_schema_version"),
        CheckConstraint("sequence >= 1", name="ck_interaction_requests_sequence"),
        UniqueConstraint(
            "suspension_id",
            "sequence",
            name="uq_interaction_requests_suspension_sequence",
        ),
        Index(
            "ix_interaction_requests_session_status_created",
            "session_id",
            "status",
            "created_at",
        ),
        {"comment": "人工交互请求"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="交互请求ID",
    )
    suspension_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("runtime_suspensions.id", ondelete="CASCADE"),
        index=True,
        comment="所属暂停点ID",
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    turn_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="CASCADE"),
        index=True,
        comment="所属交互轮次ID",
    )
    tool_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="关联工具调用记录ID",
    )
    tool_use_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="模型返回的工具调用ID",
    )
    kind: Mapped[str] = mapped_column(
        String(32),
        index=True,
        comment="交互类型",
    )
    schema_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        comment="请求载荷结构版本",
    )
    sequence: Mapped[int] = mapped_column(
        Integer,
        comment="暂停点内严格递增的请求序号",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default=text("'pending'"),
        index=True,
        comment="状态: pending/resolved/cancelled/expired",
    )
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="冻结的交互请求载荷",
    )
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(
        MutableDict.as_mutable(json_type()),
        nullable=True,
        comment="用户响应载荷",
    )
    responded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="响应用户ID",
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="响应时间",
    )

    suspension: Mapped[RuntimeSuspensionRecord] = relationship(back_populates="requests")
