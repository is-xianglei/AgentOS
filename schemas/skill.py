from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SkillResourceItem(BaseModel):
    """资源清单元素(由 service 经 list_prefix 实时重建,非 DB 字段)。"""

    relative_path: str = Field(description="资源相对 skill 根的路径,如 references/cli.md")
    size: int = Field(description="对象字节数(取自对象存储元数据)")
    mime: str = Field(description="按扩展名推断的 MIME 类型,猜不出用 application/octet-stream")

    model_config = {"from_attributes": True}


class SkillResponse(BaseModel):
    """列表精简视图:仅元数据,无正文、无资源清单。"""

    id: int = Field(description="skillID")
    name: str = Field(description="skill 名称(kebab-case)")
    description: str = Field(description="skill 描述")
    version: str | None = Field(default=None, description="版本号")
    scope: str = Field(description="作用域,本期恒为 global")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class SkillDetailResponse(SkillResponse):
    """详情视图:在精简视图上附 frontmatter + 资源清单 + 正文。

    resources / body 非 DB 字段,由 service 组装后传入。
    """

    frontmatter: dict[str, Any] = Field(description="解析后的完整 frontmatter 快照")
    resources: list[SkillResourceItem] = Field(
        default_factory=list, description="资源清单(经 list_prefix 重建,详情含 SKILL.md)"
    )
    body: str = Field(description="SKILL.md 正文(已剥离 frontmatter)")


class SkillValidateResult(BaseModel):
    """上传预检结果:全过 ok=True 带 name/description;否则 ok=False 带 errors。"""

    ok: bool = Field(description="是否通过预检")
    name: str | None = Field(default=None, description="解析出的 skill 名称(通过时)")
    description: str | None = Field(default=None, description="解析出的描述(通过时)")
    errors: list[str] = Field(default_factory=list, description="校验错误信息列表")
