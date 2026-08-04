from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from skill.schemas import SkillDetailResponse, SkillResponse, SkillValidateResult
from skill.service import SkillService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]

# 上传 zip 大小上限(字节):8MB,对齐官方 Agent Skills 规范。
_MAX_BUNDLE_BYTES = 8 * 1024 * 1024


def _read_and_guard(data: bytes) -> bytes:
    """校验上传体大小上限;超限抛 SKILL_BUNDLE_INVALID。"""
    if len(data) > _MAX_BUNDLE_BYTES:
        raise AgentException.message(f"bundle 体积超过上限 {_MAX_BUNDLE_BYTES} 字节({len(data)})")
    return data


@router.post(
    "",
    summary="上传或覆盖更新 skill(zip bundle)",
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
    record = await service.upload_bundle(data, workspace_id, current_user.id)
    return ok(SkillResponse.model_validate(record), request)


@router.post(
    "/validate",
    summary="预检 skill bundle(不落库、不写存储)",
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


@router.get(
    "",
    summary="列出全部 skill(精简元数据)",
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
    "/{name}",
    summary="查询 skill 详情(含 frontmatter、资源清单、正文)",
    response_model=ApiResponse[SkillDetailResponse],
)
async def get_skill(
    name: str,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SkillService(db)
    detail = await service.get_detail(workspace_id, name)
    return ok(detail, request)


@router.delete(
    "/{name}",
    summary="软删除 skill",
    response_model=ApiResponse[dict],
)
async def delete_skill(
    name: str,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SkillService(db)
    deleted = await service.delete_skill(workspace_id, name, current_user.id)
    # 提交交给请求边界（get_db）统一处理
    return ok({"deleted": deleted}, request)
