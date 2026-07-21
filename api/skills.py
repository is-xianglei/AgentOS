from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from core.errors import AgentException
from core.responses import ok
from schemas.common import ApiResponse
from schemas.skill import SkillDetailResponse, SkillResponse, SkillValidateResult
from services.skill_service import SkillService

router = APIRouter()

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
    file: UploadFile = File(description="skill 的 zip 打包文件"),
    db: AsyncSession = Depends(get_db),
):
    data = _read_and_guard(await file.read())
    service = SkillService(db)
    record = await service.upload_bundle(data)
    return ok(SkillResponse.model_validate(record), request)


@router.post(
    "/validate",
    summary="预检 skill bundle(不落库、不写存储)",
    response_model=ApiResponse[SkillValidateResult],
)
async def validate_skill(
    request: Request,
    file: UploadFile = File(description="待预检的 skill zip 打包文件"),
    db: AsyncSession = Depends(get_db),
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
async def list_skills(request: Request, db: AsyncSession = Depends(get_db)):
    service = SkillService(db)
    records = await service.list_skills()
    return ok([SkillResponse.model_validate(r) for r in records], request)


@router.get(
    "/{name}",
    summary="查询 skill 详情(含 frontmatter、资源清单、正文)",
    response_model=ApiResponse[SkillDetailResponse],
)
async def get_skill(name: str, request: Request, db: AsyncSession = Depends(get_db)):
    service = SkillService(db)
    detail = await service.get_detail(name)
    return ok(detail, request)


@router.delete(
    "/{name}",
    summary="软删除 skill",
    response_model=ApiResponse[dict],
)
async def delete_skill(name: str, request: Request, db: AsyncSession = Depends(get_db)):
    service = SkillService(db)
    deleted = await service.repo.soft_delete(name)
    # 提交交给请求边界（get_db）统一处理
    return ok({"deleted": deleted}, request)
