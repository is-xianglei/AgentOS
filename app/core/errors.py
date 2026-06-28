from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """业务错误基类。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, code: str, message: str):
        super().__init__(code=code, message=message, status_code=404)


class ConflictError(AppError):
    def __init__(self, code: str, message: str):
        super().__init__(code=code, message=message, status_code=409)


class ValidationAppError(AppError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(code=code, message=message, status_code=422, details=details)


class ToolExecutionError(AppError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(
            code="TOOL_EXECUTION_FAILED",
            message=message,
            status_code=500,
            details=details,
        )


def error_payload(request: Request, code: str, message: str, details: dict[str, Any] | None = None):
    return {
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
        "request_id": getattr(request.state, "request_id", None),
    }


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(request, exc.code, exc.message, exc.details),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=error_payload(
            request,
            "VALIDATION_ERROR",
            "参数校验失败",
            {"errors": exc.errors()},
        ),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_payload(request, "INTERNAL_ERROR", "内部错误"),
    )
