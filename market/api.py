"""市场接口：分类、社区上架、平台管理员审核、浏览、安装与升级。

普通接口要求当前工作区的有效成员，写操作由 Service 再校验 owner/admin；
审核接口使用 get_current_platform_admin，不看工作区。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import (
    get_current_active_user,
    get_current_platform_admin,
    get_current_workspace_id,
    get_db,
)
from core.responses import ApiResponse, ok
from market.schemas import (
    ListingKind,
    MarketApproveRequest,
    MarketBrowsePage,
    MarketCategoryResponse,
    MarketInstallResult,
    MarketInstalledTargetResponse,
    MarketListingDetailResponse,
    MarketListingResponse,
    MarketMyListingItem,
    MarketReasonRequest,
    MarketReviewPage,
    MarketSubmitRequest,
    MarketVersionResponse,
    VersionStatus,
)
from market.service import MARKET_CATEGORIES, MarketService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]
PlatformAdmin = Annotated[UserRecord, Depends(get_current_platform_admin)]


@router.get(
    "/categories",
    summary="查询市场分类",
    response_model=ApiResponse[list[MarketCategoryResponse]],
)
async def list_categories(request: Request, current_user: CurrentUser):
    categories: list[MarketCategoryResponse] = []
    for key, label in MARKET_CATEGORIES.items():
        categories.append(MarketCategoryResponse(key=key, label=label))
    return ok(categories, request)


# ---------- 社区上架 ----------


@router.post(
    "/submissions",
    summary="提交上架申请或新版本",
    response_model=ApiResponse[MarketVersionResponse],
)
async def submit(
    payload: MarketSubmitRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    version = await MarketService(db).submit(
        workspace_id,
        current_user.id,
        kind=payload.kind,
        source_id=payload.source_id,
        category=payload.category,
        release_note=payload.release_note,
    )
    return ok(MarketVersionResponse.model_validate(version), request)


@router.post(
    "/versions/{version_id}/withdraw",
    summary="撤回待审版本",
    response_model=ApiResponse[MarketVersionResponse],
)
async def withdraw_version(
    version_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    version = await MarketService(db).withdraw_version(workspace_id, current_user.id, version_id)
    return ok(MarketVersionResponse.model_validate(version), request)


@router.post(
    "/listings/{listing_id}/delist",
    summary="作者下架条目",
    response_model=ApiResponse[MarketListingResponse],
)
async def delist_listing(
    listing_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    listing = await MarketService(db).delist_listing(workspace_id, current_user.id, listing_id)
    return ok(MarketListingResponse.model_validate(listing), request)


@router.post(
    "/listings/{listing_id}/relist",
    summary="作者重新上架条目",
    response_model=ApiResponse[MarketListingResponse],
)
async def relist_listing(
    listing_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    listing = await MarketService(db).relist_listing(workspace_id, current_user.id, listing_id)
    return ok(MarketListingResponse.model_validate(listing), request)


@router.get(
    "/mine",
    summary="查询当前工作区的上架条目及全部版本",
    response_model=ApiResponse[list[MarketMyListingItem]],
)
async def list_my_listings(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    items = await MarketService(db).list_my_listings(workspace_id, current_user.id)
    return ok(items, request)


@router.get(
    "/mine/by-source",
    summary="按源 Agent 或 Skill 查询当前工作区的上架条目",
    response_model=ApiResponse[MarketMyListingItem],
)
async def get_my_listing_by_source(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    kind: Annotated[ListingKind, Query()],
    source_id: Annotated[int, Query(gt=0)],
):
    # 没有条目是正常情况（前端据此显示「申请上架」），data 返回 null，不报 404。
    item = await MarketService(db).get_my_listing_by_source(
        workspace_id,
        current_user.id,
        kind=kind,
        source_id=source_id,
    )
    return ok(item, request)


# ---------- 平台管理员审核 ----------


@router.get(
    "/admin/versions",
    summary="平台管理员查询待审版本",
    response_model=ApiResponse[MarketReviewPage],
)
async def list_versions_for_review(
    request: Request,
    db: DatabaseSession,
    admin_user: PlatformAdmin,
    status: Annotated[VersionStatus, Query()] = "pending",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    page = await MarketService(db).list_versions_for_review(
        status=status,
        limit=limit,
        offset=offset,
    )
    return ok(page, request)


@router.post(
    "/admin/versions/{version_id}/approve",
    summary="平台管理员审核通过",
    response_model=ApiResponse[MarketVersionResponse],
)
async def approve_version(
    version_id: int,
    request: Request,
    db: DatabaseSession,
    admin_user: PlatformAdmin,
    payload: MarketApproveRequest | None = None,
):
    # 审核意见可不填，请求体也可以整个省略。
    comment = None
    if payload is not None:
        comment = payload.comment
    version = await MarketService(db).approve_version(admin_user.id, version_id, comment)
    return ok(MarketVersionResponse.model_validate(version), request)


@router.post(
    "/admin/versions/{version_id}/reject",
    summary="平台管理员驳回",
    response_model=ApiResponse[MarketVersionResponse],
)
async def reject_version(
    version_id: int,
    payload: MarketReasonRequest,
    request: Request,
    db: DatabaseSession,
    admin_user: PlatformAdmin,
):
    version = await MarketService(db).reject_version(admin_user.id, version_id, payload.reason)
    return ok(MarketVersionResponse.model_validate(version), request)


@router.post(
    "/admin/listings/{listing_id}/remove",
    summary="平台管理员强制下架",
    response_model=ApiResponse[MarketListingResponse],
)
async def remove_listing(
    listing_id: int,
    payload: MarketReasonRequest,
    request: Request,
    db: DatabaseSession,
    admin_user: PlatformAdmin,
):
    listing = await MarketService(db).remove_listing(admin_user.id, listing_id, payload.reason)
    return ok(MarketListingResponse.model_validate(listing), request)


# ---------- 浏览 ----------


@router.get(
    "/listings",
    summary="分页浏览条目：默认只有已上架的，按已安装筛选时含已下架的",
    response_model=ApiResponse[MarketBrowsePage],
)
async def list_listings(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    kind: Annotated[ListingKind | None, Query()] = None,
    category: str | None = Query(default=None, max_length=32),
    keyword: str | None = Query(default=None, max_length=128),
    installed: bool | None = Query(default=None, description="按当前工作区是否有有效安装筛选"),
    has_update: bool | None = Query(default=None, description="按是否已安装且有新版本筛选"),
    is_official: bool | None = Query(default=None, description="按是否官方条目筛选"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    page = await MarketService(db).list_listings(
        workspace_id,
        current_user.id,
        kind=kind,
        category=category,
        keyword=keyword,
        limit=limit,
        offset=offset,
        installed=installed,
        has_update=has_update,
        is_official=is_official,
    )
    return ok(page, request)


@router.get(
    "/listings/{listing_id}",
    summary="查询条目详情和安装预览",
    response_model=ApiResponse[MarketListingDetailResponse],
)
async def get_listing_detail(
    listing_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    detail = await MarketService(db).get_listing_detail(
        workspace_id,
        current_user.id,
        listing_id,
        is_platform_admin=current_user.is_platform_admin,
    )
    return ok(detail, request)


# ---------- 安装与升级 ----------


@router.get(
    "/installs/by-target",
    summary="查询当前工作区的 Agent 或 Skill 是否从市场安装",
    response_model=ApiResponse[MarketInstalledTargetResponse],
)
async def get_install_by_target(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    kind: Annotated[ListingKind, Query()],
    target_id: Annotated[int, Query(gt=0)],
):
    # 不是从市场装来的是正常情况（前端据此显示「申请上架」），data 返回 null，不报 404。
    installed_from = await MarketService(db).get_install_by_target(
        workspace_id,
        current_user.id,
        kind=kind,
        target_id=target_id,
    )
    return ok(installed_from, request)


@router.post(
    "/listings/{listing_id}/install",
    summary="安装条目到当前工作区",
    response_model=ApiResponse[MarketInstallResult],
)
async def install_listing(
    listing_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    result = await MarketService(db).install_listing(workspace_id, current_user.id, listing_id)
    return ok(result, request)


@router.post(
    "/listings/{listing_id}/upgrade",
    summary="把已安装的条目升级到当前版本",
    response_model=ApiResponse[MarketInstallResult],
)
async def upgrade_listing(
    listing_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    result = await MarketService(db).upgrade_listing(workspace_id, current_user.id, listing_id)
    return ok(result, request)
