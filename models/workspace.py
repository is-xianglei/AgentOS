from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, json_type

if TYPE_CHECKING:
    from models.user import UserRecord
    from session.models import SessionRecord


class WorkspaceRecord(Base):
    __tablename__ = "workspaces"
    __table_args__ = {"comment": "工作区表（个人/团队/企业）"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="工作区ID")
    name: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, comment="工作区名称(唯一)"
    )
    slug: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, comment="工作区标识(URL友好)"
    )
    display_name: Mapped[str] = mapped_column(String(255), comment="显示名称")
    logo_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="工作区Logo URL"
    )

    # 类型与状态
    workspace_type: Mapped[str] = mapped_column(
        String(32),
        default="personal",
        index=True,
        comment="类型: personal/team/enterprise",
    )
    suspended: Mapped[bool] = mapped_column(
        default=False, index=True, comment="是否被暂停使用"
    )

    # 企业信息
    industry: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="所属行业"
    )
    company_size: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="公司规模"
    )
    billing_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="账单邮箱"
    )

    # 配额与计费
    plan: Mapped[str] = mapped_column(
        String(32), default="free", comment="订阅计划: free/pro/enterprise"
    )
    quotas: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="配额配置 {sessions_per_month: 100}",
    )

    # 设置
    settings: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(json_type()), default=dict, comment="工作区设置"
    )

    # 关联关系
    members: Mapped[list["WorkspaceMemberRecord"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    sessions: Mapped[list[SessionRecord]] = relationship(
        "SessionRecord",
        foreign_keys="SessionRecord.workspace_id",
        back_populates="workspace"
    )


class WorkspaceMemberRecord(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        Index("ix_workspace_members_workspace_user", "workspace_id", "user_id", unique=True),
        {"comment": "工作区成员关系表"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="成员记录ID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, comment="工作区ID"
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, comment="用户ID"
    )

    # 邀请与加入
    invited_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, comment="邀请人用户ID"
    )
    invited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="邀请时间"
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="加入时间(接受邀请的时间)"
    )

    # 关联关系
    workspace: Mapped["WorkspaceRecord"] = relationship(back_populates="members")
    user: Mapped["UserRecord"] = relationship("UserRecord", foreign_keys=[user_id], back_populates="workspace_memberships")
    inviter: Mapped["UserRecord"] = relationship("UserRecord", foreign_keys=[invited_by])
