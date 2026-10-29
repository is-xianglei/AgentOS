from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from skill.parser import COMPATIBILITY_MAX_LENGTH, DESCRIPTION_MAX_LENGTH, VERSION_MAX_LENGTH


class SkillResourceItem(BaseModel):
    """资源清单元素,由 extra_files 实时重建(非 DB 列)。"""

    relative_path: str = Field(description="资源相对 skill 根的路径,如 references/cli.md")
    size: int = Field(description="解码后的文件字节数")
    mime: str = Field(description="按扩展名推断的 MIME 类型,猜不出用 application/octet-stream")
    encoding: str = Field(description="存储编码:utf-8 为文本,base64 为二进制")


class SkillResponse(BaseModel):
    """列表精简视图:仅元数据,无正文、无资源清单。"""

    id: int = Field(description="skillID")
    workspace_id: int = Field(description="所属工作区ID")
    name: str = Field(description="skill 名称(kebab-case)")
    description: str = Field(description="skill 描述")
    version: str | None = Field(default=None, description="版本号")
    scope: str = Field(description="作用域，MVP恒为workspace")
    is_enabled: bool = Field(description="是否启用,禁用后不进catalog且Skill工具不可访问")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class SkillDetailResponse(SkillResponse):
    frontmatter: dict[str, Any] = Field(description="解析后的完整 frontmatter 快照")
    resources: list[SkillResourceItem] = Field(
        default_factory=list, description="附属文件清单(不含 SKILL.md)"
    )
    body: str = Field(description="SKILL.md 正文(已剥离 frontmatter)")


class SkillValidateResult(BaseModel):
    """上传预检结果:全过 ok=True 带 name/description;否则 ok=False 带 errors。"""

    ok: bool = Field(description="是否通过预检")
    name: str | None = Field(default=None, description="解析出的 skill 名称(通过时)")
    description: str | None = Field(default=None, description="解析出的描述(通过时)")
    errors: list[str] = Field(default_factory=list, description="校验错误信息列表")


class SkillUpsertRequest(BaseModel):
    """JSON 创建或更新 skill,由服务端拼成 SKILL.md;name 取自路径参数。"""

    description: str = Field(
        min_length=1,
        max_length=DESCRIPTION_MAX_LENGTH,
        description="skill 描述,注入系统提示供模型发现",
    )
    body: str = Field(description="SKILL.md 正文(不含 frontmatter)")
    version: str | None = Field(
        default=None, max_length=VERSION_MAX_LENGTH, description="版本号"
    )
    compatibility: str | None = Field(
        default=None, max_length=COMPATIBILITY_MAX_LENGTH, description="环境兼容性说明"
    )
    metadata: dict[str, Any] | None = Field(default=None, description="自定义元数据")

    @field_validator("description", "body")
    @classmethod
    def _require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class SkillEnabledRequest(BaseModel):
    enabled: bool = Field(description="true 启用,false 禁用")
