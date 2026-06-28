from fastapi import APIRouter, Request

from app.core.responses import ok
from app.schemas.common import ApiResponse, HealthData

router = APIRouter()


@router.get("/health", summary="健康检查", response_model=ApiResponse[HealthData])
def health(request: Request):
    return ok({"status": "ok"}, request)
