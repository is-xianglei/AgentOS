"""工作区模型供应商与场景角色的持久化记录。"""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, json_type


class ModelProviderRecord(Base):
    __tablename__ = "model_providers"
    __table_args__ = ({"comment": "工作区模型服务供应商"},)

    provider_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4()), comment="供应商ID"
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属工作区ID",
    )
    display_name: Mapped[str] = mapped_column(String(100), comment="显示名称")
    provider_type: Mapped[str] = mapped_column(String(20), comment="用途类型")
    provider: Mapped[str] = mapped_column(String(40), comment="上游协议或厂商")
    base_url: Mapped[str] = mapped_column(Text, comment="上游服务地址")
    api_key: Mapped[str] = mapped_column(Text, default="", comment="上游密钥，仅服务端使用")
    model_name: Mapped[str] = mapped_column(String(200), comment="上游模型ID")
    extra_config: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()), default=dict, comment="能力与参数映射配置"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近测试时间"
    )
    last_test_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="最近测试结果"
    )


class ModelRoleRecord(Base):
    __tablename__ = "model_roles"
    __table_args__ = (
        UniqueConstraint("workspace_id", "role_key", name="uq_model_roles_workspace_role"),
        {"comment": "模型场景角色指派"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="指派ID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属工作区ID",
    )
    role_key: Mapped[str] = mapped_column(String(40), comment="场景角色")
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("model_providers.provider_id", ondelete="RESTRICT"),
        index=True,
        comment="供应商ID",
    )
