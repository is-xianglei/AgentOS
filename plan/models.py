from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, json_type


class SessionPlanRecord(Base):
    """一次会话级 Plan Mode 生命周期。"""

    __tablename__ = "session_plans"
    __table_args__: ClassVar[tuple[Any, ...]] = (
        CheckConstraint(
            "status IN ('active', 'approved', 'cancelled')",
            name="ck_session_plans_status",
        ),
        CheckConstraint("version >= 1", name="ck_session_plans_version"),
        Index(
            "uq_session_plans_one_active",
            "session_id",
            unique=True,
            postgresql_where=text("status = 'active' AND is_deleted = false"),
        ),
        {"comment": "会话Plan Mode生命周期记录"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="计划ID",
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    entered_turn_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="CASCADE"),
        index=True,
        comment="进入Plan Mode的交互轮次ID",
    )
    exited_turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="退出Plan Mode的交互轮次ID",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态: active/approved/cancelled",
    )
    content: Mapped[str] = mapped_column(Text, default="", comment="Markdown计划正文")
    previous_mode: Mapped[str] = mapped_column(
        String(32),
        default="default",
        comment="进入前的运行模式",
    )
    allowed_prompts: Mapped[list[dict[str, str]]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="获批计划请求的语义权限声明，仅持久化不自动授权",
    )
    feedback: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="最近一次用户反馈",
    )
    version: Mapped[int] = mapped_column(Integer, default=1, comment="内容版本号")
    approved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="批准退出的用户ID",
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="计划批准时间",
    )
    exited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="退出Plan Mode时间",
    )
