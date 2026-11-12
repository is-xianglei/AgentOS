"""MCP 假 server 与测试装配：python-sdk v2 的低层 Server，经 httpx2.ASGITransport 挂到适配器上。

整条链路走真实的 Streamable HTTP 客户端、SSRF 固定 IP 传输层和 SDK Client，不访问外网、不监听端口：
DNS 用固定结果替身，PinnedTransport 的内层传输换成 ASGITransport，请求直接送进假 server 的 ASGI 应用。
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
import mcp_types as t
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from harness.adapters.mcp.http import McpHttpFactory
from harness.adapters.mcp.runtime import McpRuntime, McpRuntimeLimits
from harness.domain.mcp import ApprovalMode, McpServerConfig, McpServerEntry
from harness.domain.tool import ToolScope

FAKE_HOST = "mcp.example.com"
FAKE_URL = f"https://{FAKE_HOST}/mcp"
FAKE_IP = "93.184.216.34"


def text_result(text: str, **kwargs: Any) -> t.CallToolResult:
    return t.CallToolResult(content=[t.TextContent(text=text)], **kwargs)


@dataclass
class FakeMcpServer:
    """可编程的假 MCP server：工具清单、分页大小、调用处理函数与故障开关都能在用例里改。

    calls 记下每次 tools/call 的 (工具名, 入参)；list_calls 记下 tools/list 的次数。
    """

    tools: list[t.Tool] = field(default_factory=list)
    page_size: int | None = None
    ttl_ms: int | None = None
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = field(default_factory=dict)
    # 非空时每一页都返回这个 cursor，用来模拟 server 返回重复游标。
    cursor_override: str | None = None
    list_error: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    list_calls: int = 0
    cancelled: list[str] = field(default_factory=list)

    def add(
        self,
        name: str,
        handler: Callable[[dict[str, Any]], Any] | None = None,
        *,
        schema: dict[str, Any] | None = None,
        description: str | None = None,
        annotations: t.ToolAnnotations | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        self.tools.append(
            t.Tool(
                name=name,
                description=description if description is not None else f"{name} 工具",
                input_schema=schema or {"type": "object", "properties": {}},
                annotations=annotations,
                output_schema=output_schema,
            ),
        )
        if handler is not None:
            self.handlers[name] = handler

    async def _list_tools(
        self,
        ctx: ServerRequestContext[Any],
        params: t.PaginatedRequestParams | None,
    ) -> t.ListToolsResult:
        self.list_calls += 1
        if self.list_error:
            raise RuntimeError("列工具失败")
        cursor = params.cursor if params is not None else None
        start = 0 if cursor is None or cursor == self.cursor_override else int(cursor)
        size = self.page_size or len(self.tools) or 1
        page = self.tools[start : start + size]
        next_cursor = str(start + size) if start + size < len(self.tools) else None
        if next_cursor is not None and self.cursor_override is not None:
            next_cursor = self.cursor_override
        extra: dict[str, Any] = {}
        if self.ttl_ms is not None:
            extra["ttl_ms"] = self.ttl_ms
        return t.ListToolsResult(tools=page, next_cursor=next_cursor, **extra)

    async def _call_tool(
        self,
        ctx: ServerRequestContext[Any],
        params: t.CallToolRequestParams,
    ) -> t.CallToolResult | t.InputRequiredResult:
        arguments = dict(params.arguments or {})
        self.calls.append((params.name, arguments))
        handler = self.handlers.get(params.name)
        if handler is None:
            return text_result(f"{params.name} ok")
        try:
            result = handler(arguments)
            if hasattr(result, "__await__"):
                result = await result
        except anyio.get_cancelled_exc_class():
            self.cancelled.append(params.name)
            raise
        return result

    def server(self) -> Server[Any]:
        return Server(
            "fake-mcp",
            version="1.0",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )


class _Gate:
    """包在假 server 的 ASGI 应用外面：记录请求，按开关返回 HTTP 错误或模拟连接断开。"""

    def __init__(self, app: Callable[[Scope, Receive, Send], Any]) -> None:
        self._app = app
        self.status: int | None = None
        self.disconnect = False
        self.requests: list[dict[str, str]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            self.requests.append(headers)
            if self.disconnect:
                raise ConnectionResetError("连接被重置")
            if self.status is not None:
                await send({"type": "http.response.start", "status": self.status, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await self._app(scope, receive, send)


class FixedDns:
    """固定 DNS：每个主机都解析到 addresses，记下调用。"""

    def __init__(self, addresses: Sequence[str] = (FAKE_IP,)) -> None:
        self.addresses = list(addresses)
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        return list(self.addresses)


@dataclass
class McpHarness:
    """一套测试装配：假 server、它的 ASGI 闸门、固定 DNS 与 McpRuntime。"""

    fake: FakeMcpServer
    gate: _Gate
    dns: FixedDns
    runtime: McpRuntime
    clock: list[float]


def make_config(
    *,
    server_id: int = 1,
    server_key: str = "fake",
    workspace_id: int = 1,
    owner_user_id: int | None = None,
    approval_mode: ApprovalMode = ApprovalMode.ALWAYS,
    config_rev: int = 1,
    url: str = FAKE_URL,
    **kwargs: Any,
) -> McpServerConfig:
    return McpServerConfig(
        server_id=server_id,
        workspace_id=workspace_id,
        owner_user_id=owner_user_id,
        server_key=server_key,
        url=url,
        approval_mode=approval_mode,
        config_rev=config_rev,
        **kwargs,
    )


class StaticServers:
    """McpServerSource 的内存替身：按身份返回固定的 server 清单，记下每次查询的身份。"""

    def __init__(self, *entries: McpServerEntry) -> None:
        self.entries = list(entries)
        self.scopes: list[ToolScope] = []

    async def list_servers(self, scope: ToolScope) -> list[McpServerEntry]:
        self.scopes.append(scope)
        return [
            entry
            for entry in self.entries
            if entry.config.workspace_id == scope.workspace_id
            and entry.config.owner_user_id in (None, scope.user_id)
        ]


@asynccontextmanager
async def mcp_harness(
    fake: FakeMcpServer | None = None,
    *,
    connect_timeout: float = 2.0,
    idle_seconds: float = 300,
    pool_max_connections: int = 8,
    tools_cache_seconds: int = 300,
    dns: FixedDns | None = None,
) -> AsyncIterator[McpHarness]:
    """起假 server 的 ASGI 应用与生命周期，装好 McpRuntime；退出时关闭运行时与假 server。"""
    fake = fake or FakeMcpServer()
    server = fake.server()
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # 假 server 只经 ASGITransport 访问，Host 头是测试域名，关掉本机 rebinding 防护。
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    gate = _Gate(app)
    dns = dns or FixedDns()
    clock = [1_000_000.0]
    http = McpHttpFactory(
        allow_insecure_http=False,
        allowed_private_hosts=frozenset(),
        connect_timeout=connect_timeout,
        read_timeout=30.0,
        resolver=dns,
        inner_transport=lambda: httpx2.ASGITransport(app=gate),
    )
    runtime = McpRuntime(
        http=http,
        limits=McpRuntimeLimits(
            connect_timeout=connect_timeout,
            idle_seconds=idle_seconds,
            pool_max_connections=pool_max_connections,
            tools_cache_seconds=tools_cache_seconds,
        ),
        clock=lambda: clock[0],
    )
    async with app.router.lifespan_context(app):
        try:
            yield McpHarness(fake=fake, gate=gate, dns=dns, runtime=runtime, clock=clock)
        finally:
            await runtime.aclose()
