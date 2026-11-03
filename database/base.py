from datetime import datetime

from sqlalchemy import Boolean, DateTime, false, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def json_type() -> JSONB:
    """JSON 列类型的统一入口，便于后续按方言切换实现。

    历史上各 models 模块各自定义了同名函数，新代码统一从此处导入。
    """
    return JSONB()


class Base(DeclarativeBase):
    """全部 ORM 实体的基类，统一提供审计时间与软删除字段。

    sort_order 取 100 起，让这四列排在各表自有列之后；软删除的读过滤见 database/engine.py。
    """

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
