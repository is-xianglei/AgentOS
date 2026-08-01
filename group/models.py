from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, json_type

if TYPE_CHECKING:
    from user.models import UserRecord
    from workspace.models import WorkspaceRecord


class GroupRecord(Base):
    """工作区内的扁平协作群组。"""

    __tablename__ = "groups"
    __table_args__ = (
        CheckConstraint(
            "group_type IN ('team', 'community', 'region', 'custom')",
            name="ck_groups_type",
        ),
        CheckConstraint(
            "visibility IN ('workspace', 'private', 'public')",
            name="ck_groups_visibility",
        ),
        CheckConstraint(
            "join_mode IN ('invite', 'open', 'approval')",
            name="ck_groups_join_mode",
        ),
        Index("ix_groups_workspace_type", "workspace_id", "group_type"),
        Index(
            "uq_groups_active_workspace_slug",
            "workspace_id",
            "slug",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "群组表（扁平结构，用于灵活协作）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="群组ID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属工作区ID",
    )
    name: Mapped[str] = mapped_column(String(128), comment="群组名称")
    slug: Mapped[str] = mapped_column(String(64), comment="群组标识（URL友好）")
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="群组描述",
    )
    avatar_url: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="群组头像URL",
    )
    group_type: Mapped[str] = mapped_column(
        String(32),
        default="team",
        comment="群组类型: team/community/region/custom",
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        comment="创建者用户ID",
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        comment="负责人用户ID",
    )
    visibility: Mapped[str] = mapped_column(
        String(32),
        default="workspace",
        index=True,
        comment="可见性: workspace/private/public",
    )
    join_mode: Mapped[str] = mapped_column(
        String(32),
        default="invite",
        comment="加入方式: invite/open/approval",
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="群组设置",
    )
    is_active: Mapped[bool] = mapped_column(default=True, comment="是否启用")

    workspace: Mapped[WorkspaceRecord] = relationship(back_populates="groups")
    members: Mapped[list[GroupMemberRecord]] = relationship(
        back_populates="group",
    )
    creator: Mapped[UserRecord] = relationship(foreign_keys=[created_by])
    owner: Mapped[UserRecord] = relationship(foreign_keys=[owner_id])


class GroupMemberRecord(Base):
    """群组成员及群组内角色。"""

    __tablename__ = "group_members"
    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')",
            name="ck_group_members_role",
        ),
        Index(
            "ix_group_members_group_user",
            "group_id",
            "user_id",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "群组成员关系表"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="群组成员记录ID")
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"),
        index=True,
        comment="群组ID",
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        comment="用户ID",
    )
    role: Mapped[str] = mapped_column(
        String(32),
        default="member",
        comment="群组内角色: owner/admin/member",
    )
    invited_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="邀请人用户ID",
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="加入时间（NULL表示等待审批）",
    )

    group: Mapped[GroupRecord] = relationship(back_populates="members")
    user: Mapped[UserRecord] = relationship(foreign_keys=[user_id])
    inviter: Mapped[UserRecord | None] = relationship(foreign_keys=[invited_by])

    @property
    def status(self) -> str:
        """根据加入时间呈现审批状态，避免维护重复状态字段。"""
        return "active" if self.joined_at is not None else "pending"
