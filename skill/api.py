from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from skill.schemas import (
    SkillCreateRequest,
    SkillDetailResponse,
    SkillEnabledRequest,
    SkillResponse,
    SkillUpdateRequest,
    SkillValidateResult,
)
from skill.service import SkillService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]

# 上传 zip 大小上限(字节):8MB,对齐官方 Agent Skills 规范。
_MAX_BUNDLE_BYTES = 8 * 1024 * 1024


def _read_and_guard(data: bytes) -> bytes:
    """校验上传体大小上限。"""
    if len(data) > _MAX_BUNDLE_BYTES:
        raise AgentException.message(
            f"bundle 体积超过上限 {_MAX_BUNDLE_BYTES} 字节({len(data)})",
            status_code=400,
        )
    return data


@router.post(
    "/upload",
    summary="上传 zip 新建 skill(同名有效 skill 返回 409)",
    response_model=ApiResponse[SkillResponse],
)
async def upload_skill(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    file: UploadFile = File(description="skill 的 zip 打包文件"),
):
    data = _read_and_guard(await file.read())
    service = SkillService(db)
    record = await service.create_from_bundle(workspace_id, current_user.id, data)
    return ok(SkillResponse.model_validate(record), request)


@router.post(
    "/validate",
    summary="预检 skill bundle(不落库)",
    response_model=ApiResponse[SkillValidateResult],
)
async def validate_skill(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    file: UploadFile = File(description="待预检的 skill zip 打包文件"),
):
    data = _read_and_guard(await file.read())
    service = SkillService(db)
    result = await service.validate(data)
    return ok(result, request)


@router.post(
    "",
    summary="JSON 新建 skill(服务端拼 SKILL.md,同名有效 skill 返回 409)",
    response_model=ApiResponse[SkillResponse],
)
async def create_skill(
    payload: SkillCreateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SkillService(db)
    record = await service.create_from_fields(
        workspace_id,
        current_user.id,
        **payload.model_dump(),
    )
    return ok(SkillResponse.model_validate(record), request)


@router.get(
    "",
    summary="列出全部 skill(精简元数据,含禁用)",
    response_model=ApiResponse[list[SkillResponse]],
)
async def list_skills(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SkillService(db)
    records = await service.list_skills(workspace_id)
    return ok([SkillResponse.model_validate(r) for r in records], request)


@router.get(
    "/{skill_id}",
    summary="查询 skill 详情(含 frontmatter、附属文件清单、正文)",
    response_model=ApiResponse[SkillDetailResponse],
)
async def get_skill(
    skill_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SkillService(db)
    detail = await service.get_detail(workspace_id, skill_id)
    return ok(detail, request)


@router.put(
    "/{skill_id}",
    summary="JSON 更新 skill(name 不可改,可选字段缺省保留原值,保留附属文件)",
    response_model=ApiResponse[SkillResponse],
)
async def update_skill(
    skill_id: int,
    payload: SkillUpdateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SkillService(db)
    record = await service.update_from_fields(
        workspace_id,
        skill_id,
        current_user.id,
        **payload.model_dump(),
    )
    return ok(SkillResponse.model_validate(record), request)


@router.put(
    "/{skill_id}/bundle",
    summary="用 zip 覆盖指定 skill(SKILL.md 的 name 须与记录一致)",
    response_model=ApiResponse[SkillResponse],
)
async def replace_skill_bundle(
    skill_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    file: UploadFile = File(description="skill 的 zip 打包文件"),
):
    data = _read_and_guard(await file.read())
    service = SkillService(db)
    record = await service.replace_bundle(workspace_id, skill_id, current_user.id, data)
    return ok(SkillResponse.model_validate(record), request)


@router.patch(
    "/{skill_id}/enabled",
    summary="启用或禁用 skill(agent 绑定保留)",
    response_model=ApiResponse[SkillResponse],
)
async def set_skill_enabled(
    skill_id: int,
    payload: SkillEnabledRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SkillService(db)
    record = await service.set_enabled(workspace_id, skill_id, current_user.id, payload.enabled)
    return ok(SkillResponse.model_validate(record), request)


@router.get(
    "/{skill_id}/export",
    summary="导出 skill 为 zip(可直接重新上传)",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
async def export_skill(
    skill_id: int,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
) -> Response:
    service = SkillService(db)
    archive = await service.export_bundle(workspace_id, skill_id)
    # 与 SSE 一样属于统一响应外壳的例外:二进制下载无法套 {data, error, request_id}。
    return Response(
        content=archive.data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{archive.name}.zip"'},
    )


@router.delete(
    "/{skill_id}",
    summary="软删除 skill(同时清理 agent 绑定)",
    response_model=ApiResponse[dict],
)
async def delete_skill(
    skill_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SkillService(db)
    deleted = await service.delete_skill(workspace_id, skill_id, current_user.id)
    # 提交交给请求边界（get_db）统一处理
    return ok({"deleted": deleted}, request)
