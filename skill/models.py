from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.base import json_type


class SkillRecord(Base):

    __tablename__ = "skills"
    __table_args__ = {"comment": "web端上传的skill"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="skillID")
    name: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        comment="skill名称;覆盖更新与工具路由的键",
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
        String(16), default="global", comment="作用域:预留隔离维度,本期恒为global"
    )
