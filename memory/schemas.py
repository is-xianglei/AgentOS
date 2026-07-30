from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

MemoryType = Literal["user", "feedback", "project", "reference"]
MemoryStatus = Literal["active", "superseded", "archived"]
MemorySourceKind = Literal["explicit", "extracted", "dream", "manual"]

_MEMORY_BODY_MAX_BYTES = 16 * 1024
_MUTABLE_FIELDS = {"memory_type", "name", "description", "body"}


def _validate_single_line(value: str) -> str:
    """保证 Catalog 元数据不会破坏一条记忆一行的格式。"""
    if "\n" in value or "\r" in value:
        raise ValueError("不能包含换行符")
    return value


def _validate_body_bytes(value: str) -> str:
    """按 PostgreSQL UTF-8 字节语义校验正文大小。"""
    if not value.strip():
        raise ValueError("Memory 正文不能为空")
    if len(value.encode("utf-8")) > _MEMORY_BODY_MAX_BYTES:
        raise ValueError(f"Memory 正文不能超过 {_MEMORY_BODY_MAX_BYTES} 字节")
    return value


MemoryKeyValue = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=160,
        pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$",
    ),
]
MemoryNameValue = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(_validate_single_line),
]
MemoryDescriptionValue = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    AfterValidator(_validate_single_line),
]
MemoryBodyValue = Annotated[
    str,
    StringConstraints(min_length=1, max_length=_MEMORY_BODY_MAX_BYTES),
    AfterValidator(_validate_body_bytes),
]


class MemoryCreateRequest(BaseModel):
    """用户显式创建长期记忆。"""

    memory_key: MemoryKeyValue = Field(description="Space 内稳定且唯一的业务标识")
    memory_type: MemoryType = Field(description="Memory 类型")
    name: MemoryNameValue = Field(description="人类可读标题")
    description: MemoryDescriptionValue = Field(description="供选择器使用的摘要")
    body: MemoryBodyValue = Field(description="Markdown 正文，最多 16 KiB")


class MemoryUpdateRequest(BaseModel):
    """携带乐观锁版本的部分更新。"""

    version: int = Field(ge=1, description="当前乐观锁版本")
    memory_type: MemoryType | None = Field(default=None, description="Memory 类型")
    name: MemoryNameValue | None = Field(default=None, description="人类可读标题")
    description: MemoryDescriptionValue | None = Field(
        default=None,
        description="供选择器使用的摘要",
    )
    body: MemoryBodyValue | None = Field(default=None, description="Markdown 正文")

    @model_validator(mode="after")
    def validate_changes(self) -> MemoryUpdateRequest:
        supplied = self.model_fields_set & _MUTABLE_FIELDS
        if not supplied:
            raise ValueError("至少提供一个需要更新的字段")
        if any(getattr(self, field) is None for field in supplied):
            raise ValueError("更新字段不能为 null")
        return self


class MemoryDeleteRequest(BaseModel):
    """携带乐观锁版本的软删除请求。"""

    version: int = Field(ge=1, description="当前乐观锁版本")


class MemorySearchRequest(BaseModel):
    """管理端词法搜索请求。"""

    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ] = Field(description="搜索词")
    memory_type: MemoryType | None = Field(default=None, description="可选类型过滤")
    limit: int = Field(default=20, ge=1, le=100, description="返回数量上限")


class MemoryItemSummaryResponse(BaseModel):
    """Memory 列表和搜索结果的精简视图。"""

    id: int = Field(description="Memory ID")
    memory_key: str = Field(description="稳定业务标识")
    memory_type: MemoryType = Field(description="Memory 类型")
    name: str = Field(description="人类可读标题")
    description: str = Field(description="供选择器使用的摘要")
    status: MemoryStatus = Field(description="Memory 状态")
    version: int = Field(description="乐观锁版本")
    superseded_by_id: int | None = Field(description="替代当前记忆的新 Memory ID")
    source_kind: MemorySourceKind = Field(description="初始来源类型")
    last_used_at: datetime | None = Field(description="最近 Recall 时间")
    use_count: int = Field(description="Recall 次数")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class MemoryRevisionResponse(BaseModel):
    """不可变 Memory 修订。"""

    id: int = Field(description="修订 ID")
    revision: int = Field(description="修订序号")
    memory_type: MemoryType = Field(description="该修订的 Memory 类型")
    name: str = Field(description="该修订的标题")
    description: str = Field(description="该修订的摘要")
    body: str = Field(description="该修订的正文")
    actor_type: str = Field(description="写入者类型")
    actor_id: str | None = Field(description="写入者标识")
    created_at: datetime = Field(description="创建时间")

    model_config = {"from_attributes": True}


class MemorySourceResponse(BaseModel):
    """Memory 来源证据。"""

    id: int = Field(description="来源 ID")
    revision_id: int = Field(description="关联修订 ID")
    session_id: int | None = Field(description="来源会话 ID")
    turn_id: UUID | None = Field(description="来源轮次 ID")
    from_message_id: int | None = Field(description="来源消息起点 ID")
    to_message_id: int | None = Field(description="来源消息终点 ID")
    source_kind: MemorySourceKind = Field(description="来源类型")
    source_excerpt: str | None = Field(description="短来源证据")
    created_at: datetime = Field(description="创建时间")

    model_config = {"from_attributes": True}


class MemoryDetailResponse(MemoryItemSummaryResponse):
    """包含正文、修订和来源的 Memory 详情。"""

    body: str = Field(description="Markdown 正文")
    catalog_version: int = Field(description="当前 Space 的 Catalog 版本")
    revisions: list[MemoryRevisionResponse] = Field(description="修订历史，最新在前")
    sources: list[MemorySourceResponse] = Field(description="来源历史，最新在前")


class MemoryListResponse(BaseModel):
    """Memory 分页列表。"""

    items: list[MemoryItemSummaryResponse] = Field(description="当前页数据")
    total: int = Field(description="匹配总数")
    limit: int = Field(description="分页大小")
    offset: int = Field(description="分页偏移")


class MemorySearchResponse(BaseModel):
    """Memory 搜索结果。"""

    items: list[MemoryItemSummaryResponse] = Field(description="搜索结果")
    total: int = Field(description="返回结果数")


class MemoryDeleteResponse(BaseModel):
    """Memory 软删除结果。"""

    id: int = Field(description="已删除的 Memory ID")
    deleted: bool = Field(description="是否已删除")
    version: int = Field(description="删除后的版本")
    catalog_version: int = Field(description="删除后的 Catalog 版本")


class MemoryDreamRollbackRequest(BaseModel):
    """携带 Dream 后 Catalog 版本 CAS 的回滚请求。"""

    expected_after_catalog_version: int = Field(
        ge=0,
        description="该 Dream 完成后的 Catalog 版本，用作并发保护；与审计或当前版本不一致时返回 409",
    )


class MemoryDreamRollbackResponse(BaseModel):
    """Dream 回滚结果摘要。"""

    dream_job_id: UUID = Field(description="被回滚的 Dream 任务 ID")
    restored_item_ids: list[int] = Field(description="按 Dream 前 Revision 恢复的 Memory ID 列表")
    rolled_back_catalog_version: int = Field(description="被回滚的 Dream 后 Catalog 版本")
    catalog_version: int = Field(description="回滚完成后的 Catalog 版本")


class MemoryExportRevisionResponse(BaseModel):
    """数据导出中的不可变 Memory 修订。"""

    id: int
    memory_id: int
    revision: int
    memory_type: MemoryType
    name: str
    description: str
    body: str
    actor_type: str
    actor_id: str | None
    run_id: UUID | None
    created_at: datetime
    updated_at: datetime
    is_deleted: bool
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


class MemoryExportSourceResponse(BaseModel):
    """数据导出中的来源证据。"""

    id: int
    memory_id: int
    revision_id: int
    session_id: int | None
    turn_id: UUID | None
    from_message_id: int | None
    to_message_id: int | None
    source_kind: MemorySourceKind
    source_excerpt: str | None
    created_at: datetime
    updated_at: datetime
    is_deleted: bool
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


class MemoryExportItemResponse(BaseModel):
    """包含全部正文和审计链的 Memory 导出项。"""

    id: int
    space_id: int
    memory_key: str
    memory_type: MemoryType
    name: str
    description: str
    body: str
    status: MemoryStatus
    version: int
    superseded_by_id: int | None
    source_kind: MemorySourceKind
    last_used_at: datetime | None
    use_count: int
    created_at: datetime
    updated_at: datetime
    is_deleted: bool
    deleted_at: datetime | None
    revisions: list[MemoryExportRevisionResponse]
    sources: list[MemoryExportSourceResponse]


class MemoryExportJobResponse(BaseModel):
    """数据导出中的 Memory 后台任务。"""

    id: UUID
    space_id: int
    job_type: Literal["extract", "dream"]
    session_id: int | None
    turn_id: UUID | None
    idempotency_key: str
    status: Literal["pending", "running", "retry", "succeeded", "dead"]
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_until: datetime | None
    worker_id: str | None
    model: str | None
    prompt_version: str | None
    base_catalog_version: int
    started_at: datetime | None
    finished_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    is_deleted: bool
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


class MemoryExportContextResponse(BaseModel):
    """数据导出中的轮次冻结 Memory 上下文。"""

    id: int
    turn_id: UUID
    space_id: int | None
    catalog_version: int
    selected_revision_ids: list[int]
    rendered_catalog: str
    rendered_memories: str
    selector_status: Literal["selected", "empty", "degraded"]
    degraded_reason: str | None
    byte_count: int
    created_at: datetime
    updated_at: datetime
    is_deleted: bool
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


class MemoryExportSpaceResponse(BaseModel):
    """数据导出中的 Space 元数据。"""

    id: int
    workspace_id: int
    user_id: int
    catalog_version: int
    last_dream_at: datetime | None
    last_scan_at: datetime | None
    sessions_since_dream: int
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    is_deleted: bool
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


class MemoryExportResponse(BaseModel):
    """当前用户在当前工作区内的完整 Memory 数据导出。"""

    schema_version: Literal["1.0"] = "1.0"
    exported_at: datetime
    workspace_id: int
    user_id: int
    space: MemoryExportSpaceResponse | None
    items: list[MemoryExportItemResponse]
    jobs: list[MemoryExportJobResponse]
    contexts: list[MemoryExportContextResponse]


class MemoryPurgeConfirmationResponse(BaseModel):
    """短时有效且绑定租户与 Catalog 的彻底删除确认。"""

    confirmation_token: str
    expected_catalog_version: int
    expires_at: datetime


class MemoryPurgeRequest(BaseModel):
    """彻底删除当前 Memory Space 的双重确认请求。"""

    confirmation_token: str = Field(min_length=32, max_length=2048)
    expected_catalog_version: int = Field(ge=0)


class MemoryPurgeResponse(BaseModel):
    """彻底删除产生的各类物理删除计数。"""

    spaces_deleted: int
    items_deleted: int
    revisions_deleted: int
    sources_deleted: int
    jobs_deleted: int
    contexts_deleted: int
