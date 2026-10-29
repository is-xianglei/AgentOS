from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, true
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.base import json_type


class SkillRecord(Base):

    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "name",
            name="uq_skills_workspace_name",
        ),
        {"comment": "工作区上传的Skill"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="skillID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属工作区ID",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        index=True,
        comment="Skill名称，工作区内覆盖更新的键",
    )
    description: Mapped[str] = mapped_column(
        Text, comment="skill描述(注入系统提示供发现)"
    )
    frontmatter: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="解析后的完整frontmatter快照",
    )
    version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="frontmatter声明的版本号"
    )
    skill_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="规范化bundle(SKILL.md+附属文件)的sha256,幂等写入判重",
    )
    scope: Mapped[str] = mapped_column(
        String(16),
        default="workspace",
        comment="作用域，MVP恒为workspace",
    )
    content: Mapped[str] = mapped_column(
        Text,
        comment="SKILL.md原文(含frontmatter)",
    )
    # 形如 {相对路径: {"encoding": "utf-8" | "base64", "content": str}},不含 SKILL.md。
    extra_files: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="附属文件,键为相对skill根的路径,值含encoding与content",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        index=True,
        comment="是否启用,禁用后不进catalog且Skill工具不可访问",
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        comment="创建者用户ID,改PG存储前的历史记录为空",
    )
