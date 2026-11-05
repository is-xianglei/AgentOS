"""market 域的请求与响应 Schema。"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

PositiveId = Annotated[int, Field(gt=0)]
ListingKind = Literal["agent", "skill"]
VersionStatus = Literal["pending", "approved", "rejected", "withdrawn"]
ToolDropReason = Literal["not_found", "disabled", "not_bindable"]
# 报告里每个 Skill 的处理结果：create 新建，restore 恢复同名的软删记录，
# reuse 复用内容相同的同名记录，conflict 同名但内容不同（整个安装或升级会被拒绝），
# overwrite 升级时用新内容覆盖托管 Skill，unbind 升级时只解绑、不删除。
SkillOutcomeAction = Literal["create", "restore", "reuse", "conflict", "overwrite", "unbind"]


def _strip_or_none(value: str | None) -> str | None:
    """去掉首尾空白；只有空白时按未填写处理。"""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return value


class MarketCategoryResponse(BaseModel):
    key: str = Field(description="分类标识")
    label: str = Field(description="分类中文名")


class MarketSubmitRequest(BaseModel):
    kind: ListingKind = Field(description="条目类型: agent/skill")
    source_id: PositiveId = Field(description="要上架的 Agent 或 Skill 的 ID，必须属于当前工作区")
    category: str = Field(min_length=1, max_length=32, description="分类标识，取值见分类接口")
    release_note: str | None = Field(default=None, description="版本说明")

    model_config = {"extra": "forbid"}

    @field_validator("release_note")
    @classmethod
    def normalize_release_note(cls, value: str | None) -> str | None:
        return _strip_or_none(value)


class MarketApproveRequest(BaseModel):
    comment: str | None = Field(default=None, description="审核意见，可不填")

    model_config = {"extra": "forbid"}

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        return _strip_or_none(value)


class MarketReasonRequest(BaseModel):
    """驳回版本和强制下架共用的请求体，原因必填。"""

    reason: str = Field(min_length=1, description="原因")

    model_config = {"extra": "forbid"}

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("原因不能为空")
        return value


class MarketListingResponse(BaseModel):
    id: int = Field(description="市场条目ID")
    kind: str = Field(description="条目类型: agent/skill")
    is_official: bool = Field(description="是否官方预置")
    official_key: str | None = Field(default=None, description="官方预置的目录名，社区条目为空")
    publisher_workspace_id: int | None = Field(default=None, description="发布工作区ID")
    source_agent_id: int | None = Field(default=None, description="源Agent ID")
    source_skill_id: int | None = Field(default=None, description="源Skill ID")
    status: str = Field(description="条目状态: unlisted/listed/delisted/removed")
    status_reason: str | None = Field(default=None, description="下架原因")
    current_version_number: int | None = Field(default=None, description="当前版本号")
    name: str | None = Field(default=None, description="当前版本名称，首次审核通过前为空")
    summary: str | None = Field(default=None, description="当前版本简介")
    category: str | None = Field(default=None, description="当前版本分类")
    created_by_user_id: int | None = Field(default=None, description="创建条目的用户ID")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class MarketVersionResponse(BaseModel):
    """版本元数据，不含快照全文。"""

    id: int = Field(description="版本ID")
    listing_id: int = Field(description="所属市场条目ID")
    version_number: int = Field(description="版本号")
    name: str = Field(
        description="该版本快照里的名称；条目的 name 在首次审核通过前为空，可用最新版本的名称展示"
    )
    status: str = Field(description="审核状态: pending/approved/rejected/withdrawn")
    category: str = Field(description="提交时选择的分类")
    release_note: str | None = Field(default=None, description="版本说明")
    snapshot_hash: str = Field(description="快照哈希")
    submitted_by_user_id: int | None = Field(default=None, description="提交人用户ID")
    reviewed_by_user_id: int | None = Field(default=None, description="审核人用户ID")
    reviewed_at: datetime | None = Field(default=None, description="审核时间")
    review_comment: str | None = Field(default=None, description="审核意见，驳回时为驳回理由")
    created_at: datetime = Field(description="提交时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class MarketVersionDetailResponse(MarketVersionResponse):
    snapshot: dict[str, Any] = Field(description="快照全文，含系统提示词和 Skill 的全部文件")


class MarketMyListingItem(BaseModel):
    listing: MarketListingResponse = Field(description="市场条目")
    versions: list[MarketVersionResponse] = Field(description="全部版本，版本号大的在前")


class MarketReviewItem(BaseModel):
    version: MarketVersionDetailResponse = Field(description="待审核的版本，含快照全文")
    listing: MarketListingResponse = Field(description="所属市场条目")
    publisher_name: str | None = Field(default=None, description="发布工作区名称")


class MarketReviewPage(BaseModel):
    items: list[MarketReviewItem] = Field(description="当前页版本")
    total: int = Field(description="版本总数")
    limit: int = Field(description="分页大小")
    offset: int = Field(description="分页偏移")


class MarketBrowseItem(BaseModel):
    id: int = Field(description="市场条目ID")
    kind: str = Field(description="条目类型: agent/skill")
    name: str | None = Field(default=None, description="名称")
    summary: str | None = Field(default=None, description="简介")
    category: str | None = Field(default=None, description="分类")
    is_official: bool = Field(description="是否官方预置")
    status: str = Field(
        description="条目状态: unlisted/listed/delisted/removed；列表只在按已安装筛选时有非 listed"
    )
    status_reason: str | None = Field(default=None, description="下架原因")
    current_version_number: int | None = Field(default=None, description="当前版本号")
    publisher_name: str | None = Field(default=None, description="发布方：官方条目为「官方」")
    install_count: int = Field(
        description="有效安装数：安装记录未删除、所在工作区未删除、安装出来的对象仍存在"
    )
    installed: bool = Field(description="当前工作区是否已安装")
    installed_version_number: int | None = Field(default=None, description="当前工作区已安装的版本")
    has_update: bool = Field(description="已安装、条目仍在上架且有更新的版本，即可以升级")
    updated_at: datetime = Field(description="更新时间")


class MarketBrowsePage(BaseModel):
    items: list[MarketBrowseItem] = Field(description="当前页条目")
    total: int = Field(description="条目总数")
    limit: int = Field(description="分页大小")
    offset: int = Field(description="分页偏移")


class MarketDroppedTool(BaseModel):
    tool_key: str = Field(description="工具标识")
    reason: ToolDropReason = Field(description="丢弃原因: not_found/disabled/not_bindable")
    message: str = Field(description="丢弃原因的中文说明")


class MarketSkillOutcome(BaseModel):
    name: str = Field(description="Skill 名称")
    action: SkillOutcomeAction = Field(description="处理结果")
    skill_id: int | None = Field(
        default=None, description="对应的 Skill ID，预览时要新建的还没有ID"
    )


class MarketInstallReport(BaseModel):
    bound_tool_keys: list[str] = Field(default_factory=list, description="绑定的工具")
    dropped_tools: list[MarketDroppedTool] = Field(
        default_factory=list,
        description="不可用而被丢弃的工具",
    )
    skills: list[MarketSkillOutcome] = Field(
        default_factory=list, description="每个Skill的处理结果"
    )


class MarketInstallPreview(BaseModel):
    mode: Literal["install", "upgrade"] = Field(description="没装过时为安装，已装过时为升级")
    installed_version_number: int | None = Field(default=None, description="已安装的版本号")
    target_version_number: int = Field(description="将要安装或升级到的版本号")
    can_proceed: bool = Field(description="能否执行；为 false 时原因见 blockers")
    blockers: list[str] = Field(default_factory=list, description="阻止执行的原因")
    agent_name_conflict: bool = Field(description="安装 Agent 时本工作区是否已有同名 Agent")
    report: MarketInstallReport = Field(description="执行后会发生什么")


class MarketListingDetailResponse(BaseModel):
    listing: MarketBrowseItem = Field(description="条目信息和当前工作区的安装状态")
    status: str = Field(description="条目状态: unlisted/listed/delisted/removed")
    status_reason: str | None = Field(default=None, description="下架原因")
    current_version: MarketVersionDetailResponse | None = Field(
        default=None,
        description="当前版本，含快照全文；还没有审核通过的版本时为空",
    )
    install_preview: MarketInstallPreview | None = Field(
        default=None,
        description="当前工作区的安装或升级预览；条目不是已上架状态时为空",
    )


class MarketInstallResponse(BaseModel):
    id: int = Field(description="安装记录ID")
    listing_id: int = Field(description="市场条目ID")
    workspace_id: int = Field(description="安装到的工作区ID")
    installed_version_number: int = Field(description="已安装的版本号")
    target_agent_id: int | None = Field(default=None, description="安装出来的Agent ID")
    target_skill_id: int | None = Field(default=None, description="安装出来的Skill ID")
    managed_skill_ids: list[int] = Field(description="安装Agent时新建或恢复的Skill ID")
    installed_by_user_id: int = Field(description="安装人用户ID")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class MarketInstalledTargetResponse(BaseModel):
    """当前工作区里的 Agent 或 Skill 是从哪个市场条目装来的。"""

    listing_id: int = Field(description="市场条目ID")
    listing_name: str | None = Field(
        default=None,
        description="条目当前的名称；装过的条目都审核通过过，实际总有值",
    )
    listing_status: str = Field(description="条目状态: listed/delisted/removed")
    installed_version_number: int = Field(description="已安装的版本号")
    installed_with_agent: bool = Field(
        description="为 true 表示这是随 Agent 条目一起装来的托管 Skill，不是直接安装的对象"
    )


class MarketInstallResult(BaseModel):
    install: MarketInstallResponse = Field(description="安装记录")
    report: MarketInstallReport = Field(description="安装或升级报告")
