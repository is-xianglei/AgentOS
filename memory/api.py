from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from api.deps import DatabaseSession, get_current_active_user, get_current_workspace_id
from core.responses import ApiResponse, ok
from user.models import UserRecord
from memory.schemas import (
    MemoryCreateRequest,
    MemoryDeleteRequest,
    MemoryDeleteResponse,
    MemoryDetailResponse,
    MemoryDreamRollbackRequest,
    MemoryDreamRollbackResponse,
    MemoryExportContextResponse,
    MemoryExportItemResponse,
    MemoryExportJobResponse,
    MemoryExportResponse,
    MemoryExportRevisionResponse,
    MemoryExportSourceResponse,
    MemoryExportSpaceResponse,
    MemoryItemSummaryResponse,
    MemoryListResponse,
    MemoryPurgeConfirmationResponse,
    MemoryPurgeRequest,
    MemoryPurgeResponse,
    MemoryRevisionResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySourceResponse,
    MemoryStatus,
    MemoryType,
    MemoryUpdateRequest,
)
from memory.jobs.dream import MemoryDreamService
from memory.service import (
    MemoryDetail,
    MemoryExportResult,
    MemoryExportRunner,
    MemoryService,
)

router = APIRouter()
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]
MemoryStatusFilter = Annotated[MemoryStatus | None, Query()]
MemoryTypeFilter = Annotated[MemoryType | None, Query()]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


def _to_detail_response(detail: MemoryDetail) -> MemoryDetailResponse:
    """在 API 边界把 ORM 和服务值对象转换为响应 Schema。"""
    summary = MemoryItemSummaryResponse.model_validate(detail.item)
    return MemoryDetailResponse(
        **summary.model_dump(),
        body=detail.item.body,
        catalog_version=detail.catalog_version,
        revisions=[MemoryRevisionResponse.model_validate(item) for item in detail.revisions],
        sources=[MemorySourceResponse.model_validate(item) for item in detail.sources],
    )


def _to_export_response(
    export_result: MemoryExportResult,
    *,
    workspace_id: int,
    user_id: int,
) -> MemoryExportResponse:
    """把完整数据库投影转换为稳定且显式的导出结构。"""
    bundle = export_result.bundle
    revisions_by_item: dict[int, list[MemoryExportRevisionResponse]] = {}
    for revision in bundle.revisions:
        revisions_by_item.setdefault(revision.memory_id, []).append(
            MemoryExportRevisionResponse.model_validate(revision)
        )
    sources_by_item: dict[int, list[MemoryExportSourceResponse]] = {}
    for source in bundle.sources:
        sources_by_item.setdefault(source.memory_id, []).append(
            MemoryExportSourceResponse.model_validate(source)
        )
    items = [
        MemoryExportItemResponse(
            id=item.id,
            space_id=item.space_id,
            memory_key=item.memory_key,
            memory_type=item.memory_type,
            name=item.name,
            description=item.description,
            body=item.body,
            status=item.status,
            version=item.version,
            superseded_by_id=item.superseded_by_id,
            source_kind=item.source_kind,
            last_used_at=item.last_used_at,
            use_count=item.use_count,
            created_at=item.created_at,
            updated_at=item.updated_at,
            is_deleted=item.is_deleted,
            deleted_at=item.deleted_at,
            revisions=revisions_by_item.get(item.id, []),
            sources=sources_by_item.get(item.id, []),
        )
        for item in bundle.items
    ]
    return MemoryExportResponse(
        exported_at=export_result.exported_at,
        workspace_id=workspace_id,
        user_id=user_id,
        space=(
            MemoryExportSpaceResponse.model_validate(bundle.space)
            if bundle.space is not None
            else None
        ),
        items=items,
        jobs=[MemoryExportJobResponse.model_validate(job) for job in bundle.jobs],
        contexts=[
            MemoryExportContextResponse.model_validate(context) for context in bundle.contexts
        ],
    )


@router.get(
    "",
    summary="分页列出当前用户的 Memory",
    response_model=ApiResponse[MemoryListResponse],
)
async def list_memories(
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
    status: MemoryStatusFilter = "active",
    memory_type: MemoryTypeFilter = None,
    limit: PageLimit = 20,
    offset: PageOffset = 0,
):
    service = MemoryService(db)
    page = await service.list_memories(
        workspace_id,
        current_user.id,
        status=status,
        memory_type=memory_type,
        limit=limit,
        offset=offset,
    )
    response = MemoryListResponse(
        items=[MemoryItemSummaryResponse.model_validate(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )
    return ok(response, request)


@router.post(
    "",
    summary="显式创建 Memory",
    response_model=ApiResponse[MemoryDetailResponse],
)
async def create_memory(
    payload: MemoryCreateRequest,
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
):
    detail = await MemoryService(db).create_memory(
        workspace_id,
        current_user.id,
        memory_key=payload.memory_key,
        memory_type=payload.memory_type,
        name=payload.name,
        description=payload.description,
        body=payload.body,
    )
    return ok(_to_detail_response(detail), request)


@router.post(
    "/search",
    summary="词法搜索当前用户的 Memory",
    response_model=ApiResponse[MemorySearchResponse],
)
async def search_memories(
    payload: MemorySearchRequest,
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
):
    items = await MemoryService(db).search_memories(
        workspace_id,
        current_user.id,
        query=payload.query,
        memory_type=payload.memory_type,
        limit=payload.limit,
    )
    response = MemorySearchResponse(
        items=[MemoryItemSummaryResponse.model_validate(item) for item in items],
        total=len(items),
    )
    return ok(response, request)


@router.get(
    "/export",
    summary="导出当前用户的完整 Memory 数据",
    response_model=ApiResponse[MemoryExportResponse],
)
async def export_memories(
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    export_result = await MemoryExportRunner().export(workspace_id, current_user.id)
    return ok(
        _to_export_response(
            export_result,
            workspace_id=workspace_id,
            user_id=current_user.id,
        ),
        request,
    )


@router.post(
    "/purge-confirmation",
    summary="签发 Memory 彻底删除确认令牌",
    response_model=ApiResponse[MemoryPurgeConfirmationResponse],
)
async def issue_purge_confirmation(
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
):
    confirmation = await MemoryService(db).issue_purge_confirmation(
        workspace_id,
        current_user.id,
    )
    response = MemoryPurgeConfirmationResponse(
        confirmation_token=confirmation.confirmation_token,
        expected_catalog_version=confirmation.expected_catalog_version,
        expires_at=confirmation.expires_at,
    )
    return ok(response, request)


@router.delete(
    "/purge",
    summary="彻底删除当前用户的完整 Memory 数据",
    response_model=ApiResponse[MemoryPurgeResponse],
)
async def purge_memories(
    payload: MemoryPurgeRequest,
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
):
    counts = await MemoryService(db).purge_memories(
        workspace_id,
        current_user.id,
        confirmation_token=payload.confirmation_token,
        expected_catalog_version=payload.expected_catalog_version,
    )
    response = MemoryPurgeResponse(
        spaces_deleted=counts.spaces,
        items_deleted=counts.items,
        revisions_deleted=counts.revisions,
        sources_deleted=counts.sources,
        jobs_deleted=counts.jobs,
        contexts_deleted=counts.contexts,
    )
    return ok(response, request)


@router.post(
    "/dreams/{dream_job_id}/rollback",
    summary="按审计快照回滚一次已完成的 Dream",
    response_model=ApiResponse[MemoryDreamRollbackResponse],
)
async def rollback_dream(
    dream_job_id: UUID,
    payload: MemoryDreamRollbackRequest,
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
):
    result = await MemoryDreamService(db).rollback_dream_for_api(
        workspace_id,
        current_user.id,
        dream_job_id,
        expected_after_catalog_version=payload.expected_after_catalog_version,
        actor_id=f"user:{current_user.id}",
    )
    response = MemoryDreamRollbackResponse(
        dream_job_id=dream_job_id,
        restored_item_ids=list(result.restored_item_ids),
        rolled_back_catalog_version=result.rolled_back_catalog_version,
        catalog_version=result.catalog_version,
    )
    return ok(response, request)


@router.get(
    "/{memory_id}",
    summary="查询 Memory 详情",
    response_model=ApiResponse[MemoryDetailResponse],
)
async def get_memory(
    memory_id: int,
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
):
    detail = await MemoryService(db).get_memory(
        workspace_id,
        current_user.id,
        memory_id,
    )
    return ok(_to_detail_response(detail), request)


@router.patch(
    "/{memory_id}",
    summary="按版本更新 Memory",
    response_model=ApiResponse[MemoryDetailResponse],
)
async def update_memory(
    memory_id: int,
    payload: MemoryUpdateRequest,
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
):
    detail = await MemoryService(db).update_memory(
        workspace_id,
        current_user.id,
        memory_id,
        expected_version=payload.version,
        memory_type=payload.memory_type,
        name=payload.name,
        description=payload.description,
        body=payload.body,
    )
    return ok(_to_detail_response(detail), request)


@router.delete(
    "/{memory_id}",
    summary="按版本软删除 Memory",
    response_model=ApiResponse[MemoryDeleteResponse],
)
async def delete_memory(
    memory_id: int,
    payload: MemoryDeleteRequest,
    request: Request,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    db: DatabaseSession,
):
    result = await MemoryService(db).delete_memory(
        workspace_id,
        current_user.id,
        memory_id,
        expected_version=payload.version,
    )
    response = MemoryDeleteResponse(
        id=result.memory_id,
        deleted=True,
        version=result.version,
        catalog_version=result.catalog_version,
    )
    return ok(response, request)
