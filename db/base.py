from datetime import datetime

from sqlalchemy import Boolean, DateTime, false, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
        sort_order=100,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
        sort_order=101,
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        server_default=false(),
        default=False,
        index=True,
        comment="软删除标记: false 有效 / true 已删除",
        sort_order=102,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="软删除时间, 空表示未删除",
        sort_order=103,
    )
