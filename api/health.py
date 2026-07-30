from fastapi import APIRouter, Request

from core.responses import ApiResponse, HealthData, ok

router = APIRouter()


@router.get("/health", summary="健康检查", response_model=ApiResponse[HealthData])
def health(request: Request):
    return ok({"status": "ok"}, request)
