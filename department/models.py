from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from user.models import UserRecord
    from workspace.models import WorkspaceMemberRecord, WorkspaceRecord


class DepartmentRecord(Base):
    """工作区内的树形部门。"""

    __tablename__ = "departments"
    __table_args__ = (
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_departments_parent_not_self",
        ),
        Index("ix_departments_workspace_parent", "workspace_id", "parent_id"),
        {"comment": "部门表（支持层级结构）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="部门ID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属工作区ID",
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="父部门ID（NULL表示顶级部门）",
    )
    name: Mapped[str] = mapped_column(String(128), comment="部门名称")
    code: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="部门编码（用于对接HR系统）",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="部门描述",
    )
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="部门负责人用户ID",
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, comment="排序顺序")
    is_active: Mapped[bool] = mapped_column(default=True, comment="是否启用")

    workspace: Mapped[WorkspaceRecord] = relationship(back_populates="departments")
    parent: Mapped[DepartmentRecord | None] = relationship(
        remote_side=[id],
        back_populates="children",
        foreign_keys=[parent_id],
    )
    children: Mapped[list[DepartmentRecord]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_id],
    )
    members: Mapped[list[WorkspaceMemberRecord]] = relationship(
        back_populates="department",
    )
    manager: Mapped[UserRecord | None] = relationship(foreign_keys=[manager_id])
