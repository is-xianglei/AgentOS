"""统一错误外壳契约：OpenAPI 中 ErrorBody 的声明须与异常处理器的实际输出一致。

error_payload 不输出 code；若 ErrorBody.code 声明为必填，按 OpenAPI 生成的强类型客户端会解析失败。
"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from core.errors import (
    AgentException,
    agent_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from core.logging import request_context_middleware
from core.responses import ApiResponse

pytestmark = pytest.mark.anyio

_CASES = [
    pytest.param("/business", 404, "资源不存在", id="agent_exception"),
    pytest.param("/items/abc", 422, "参数校验失败", id="validation_error"),
    pytest.param("/crash", 500, "内部错误", id="unhandled_error"),
]


def _build_app() -> FastAPI:
    """只装配请求上下文中间件与三个异常处理器，隔离业务路由和外部依赖。"""
    app = FastAPI()
    app.middleware("http")(request_context_middleware)
    app.add_exception_handler(AgentException, agent_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.get("/business")
    async def business() -> None:
        raise AgentException.message("资源不存在", {"id": 1}, status_code=404)

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> int:
        return item_id

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("内部细节")

    return app


async def _get(path: str) -> httpx.Response:
    # ServerErrorMiddleware 发出 500 响应后会继续抛出原异常，关闭 raise_app_exceptions 以拿到响应。
    transport = httpx.ASGITransport(app=_build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def test_openapi_error_body_code_is_optional() -> None:
    from main import app

    schema = app.openapi()["components"]["schemas"]["ErrorBody"]

    assert schema["required"] == ["message"]
    assert {"type": "null"} in schema["properties"]["code"]["anyOf"]


@pytest.mark.parametrize(("path", "status", "message"), _CASES)
async def test_error_response_body_has_no_code(path: str, status: int, message: str) -> None:
    response = await _get(path)
    body = response.json()

    assert response.status_code == status
    assert set(body) == {"data", "error", "request_id"}
    assert body["data"] is None
    assert set(body["error"]) == {"message", "details"}
    assert body["error"]["message"] == message
    assert body["request_id"]


async def test_agent_exception_body_is_exact() -> None:
    response = await _get("/business")

    assert response.json() == {
        "data": None,
        "error": {"message": "资源不存在", "details": {"id": 1}},
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize(("path", "status", "message"), _CASES)
async def test_error_response_validates_against_api_response(
    path: str,
    status: int,
    message: str,
) -> None:
    response = await _get(path)

    parsed = ApiResponse[Any].model_validate(response.json())

    assert response.status_code == status
    assert parsed.error is not None
    assert parsed.error.code is None
    assert parsed.error.message == message
