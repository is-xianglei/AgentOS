from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, json_type

if TYPE_CHECKING:
    from workspace.models import WorkspaceMemberRecord
    from session.models import SessionRecord


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        # 第三方登录按 (auth_provider, provider_user_id) 定位用户，
        # 索引由 0011_fix_users_table_schema 建立，此处补齐声明以免 autogenerate 误判删除。
        Index("ix_users_auth_provider_provider_user_id", "auth_provider", "provider_user_id"),
        {"comment": "用户表"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, comment="用户ID")
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, comment="邮箱(唯一登录标识)"
    )
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, comment="用户名(唯一)"
    )
    password: Mapped[str] = mapped_column(String(255), comment="密码哈希(MD5)")
    full_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="真实姓名"
    )
    avatar_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="头像URL"
    )
    phone: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="手机号"
    )

    # 状态字段
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="邮箱是否验证"
    )
    suspended: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, comment="是否被暂停使用"
    )

    # OAuth 字段
    auth_provider: Mapped[str] = mapped_column(
        String(32), default="local", comment="认证提供商: local/google/github/saml"
    )
    provider_user_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="第三方用户ID"
    )

    # 偏好设置
    preferences: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="用户偏好设置",
    )

    # 时间戳（created_at, updated_at, is_deleted, deleted_at 继承自 Base）
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最后登录时间"
    )

    # 关联关系
    workspace_memberships: Mapped[list[WorkspaceMemberRecord]] = relationship(
        "WorkspaceMemberRecord",
        foreign_keys="WorkspaceMemberRecord.user_id",
        back_populates="user"
    )
    sessions: Mapped[list[SessionRecord]] = relationship(
        "SessionRecord",
        foreign_keys="SessionRecord.user_id",
        back_populates="user"
    )
