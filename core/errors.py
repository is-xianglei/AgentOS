import json
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AgentException(Exception):
    """业务异常:状态码固定 500,携带可读消息与可选详情。"""

    status_code = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)

    @classmethod
    def message(cls, message: str, details: dict[str, Any] | None = None) -> "AgentException":
        """业务异常唯一入口。"""
        return cls(message, details)


def error_payload(request: Request, message: str, details: dict[str, Any] | None = None):
    return {
        "data": None,
        "error": {
            "message": message,
            "details": details or {},
        },
        "request_id": getattr(request.state, "request_id", None),
    }


async def agent_error_handler(request: Request, exc: AgentException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(request, exc.message, exc.details),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    safe_errors = json.loads(json.dumps(exc.errors(), default=str))
    return JSONResponse(
        status_code=422,
        content=error_payload(
            request,
            "参数校验失败",
            {"errors": safe_errors},
        ),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_payload(request, "内部错误"),
    )
