from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorBody(BaseModel):
    code: str = Field(description="错误码")
    message: str = Field(description="错误信息")
    details: dict[str, Any] = Field(default_factory=dict, description="错误详情")


class ApiResponse(BaseModel, Generic[T]):
    data: T | None = Field(default=None, description="响应数据")
    error: ErrorBody | None = Field(default=None, description="错误信息")
    request_id: str | None = Field(default=None, description="请求ID")


class HealthData(BaseModel):
    status: str = Field(description="服务状态")
