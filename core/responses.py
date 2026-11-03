"""统一响应外壳：{data, error, request_id} 的 Schema 定义与构造函数。

原先 Schema 在 schemas/common.py、构造函数在本文件，属同一契约的两半，现合并。
"""

from typing import Any

from fastapi import Request
from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str | None = Field(default=None, description="错误码")
    message: str = Field(description="错误信息")
    details: dict[str, Any] = Field(default_factory=dict, description="错误详情")


class ApiResponse[T](BaseModel):
    data: T | None = Field(default=None, description="响应数据")
    error: ErrorBody | None = Field(default=None, description="错误信息")
    request_id: str | None = Field(default=None, description="请求ID")


def ok(data: Any, request: Request) -> dict[str, Any]:
    """构造成功响应外壳;request_id 由请求上下文中间件写入 request.state。"""
    return {
        "data": data,
        "error": None,
        "request_id": getattr(request.state, "request_id", None),
    }
