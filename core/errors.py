"""业务异常与全局异常处理器:所有错误响应统一为 {data, error, request_id} 外壳。"""

import json
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AgentException(Exception):
    """业务异常，携带可读消息、可选详情和 HTTP 状态码。"""

    status_code = 500

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        # 实例属性 message 会遮蔽同名类方法:实例上取到的是消息文本,类上仍是构造入口。
        self.message = message
        self.details = details or {}
        self.status_code = status_code or type(self).status_code
        super().__init__(message)

    @classmethod
    def message(
        cls,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        status_code: int | None = None,
    ) -> AgentException:
        """业务异常唯一入口。"""
        return cls(message, details, status_code)


class LeaseLostError(AgentException):
    """执行租约已不属于当前持有者(被用户取消、被回收或续约失败)。

    持有者据此停止写入并静默收尾:终态与 run_end 由清除租约的一方负责。
    """

    status_code = 409


def error_payload(
    request: Request,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造错误响应外壳;request_id 由请求上下文中间件写入 request.state。"""
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
    # 模型服务等请求携带密钥；校验失败不应把原始字段或整个请求体回显给客户端，
    # 故去掉每条错误里回显原始输入的 input 字段。
    errors: list[dict[str, Any]] = []
    for error in exc.errors():
        errors.append({key: value for key, value in error.items() if key != "input"})
    # ctx 里可能含异常对象等不可 JSON 序列化的值，先往返一次转成纯 JSON 结构。
    safe_errors = json.loads(json.dumps(errors, default=str))
    return JSONResponse(
        status_code=422,
        content=error_payload(
            request,
            "参数校验失败",
            {"errors": safe_errors},
        ),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # 只返回通用文案，不把异常细节泄漏给客户端；
    # Starlette 的 ServerErrorMiddleware 发出该响应后会继续抛出原异常，堆栈由服务端记录。
    return JSONResponse(
        status_code=500,
        content=error_payload(request, "内部错误"),
    )
