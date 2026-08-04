from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
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
        json_type(),
        default=dict,
        comment="解析后的完整frontmatter快照",
    )
    version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="frontmatter声明的版本号"
    )
    skill_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="上传zip包的哈希,幂等上传判重"
    )
    scope: Mapped[str] = mapped_column(
        String(16),
        default="workspace",
        comment="作用域，MVP恒为workspace",
    )
