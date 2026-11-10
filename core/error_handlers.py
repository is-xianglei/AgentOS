"""全局 HTTP 异常处理器：把业务异常、参数校验失败与未处理异常统一转成 {data, error, request_id} 外壳。

与 core/errors.py 分开：业务异常被领域层、应用层广泛导入，处理器依赖 FastAPI，放在一起会让
不涉及 HTTP 的模块加载期间接加载 FastAPI。处理器只由 main.py 在创建应用时注册。
"""

import json
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.errors import AgentException, error_payload


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
