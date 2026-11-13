"""MCP OAuth 测试替身：假授权服务器与带 PRM、Bearer 校验的假 MCP server，全部经 httpx2.ASGITransport 挂载。

不监听端口、不访问外网：内层传输按请求主机分派到不同的 ASGI 应用（MCP server 与授权服务器在不同主机上，
各自经 PinnedTransport 解析、校验、固定 IP）。假授权服务器按规范校验 PKCE（S256）、resource（RFC 8707）、
redirect_uri 与 client_id，回跳时带 iss（RFC 9207）；刷新时轮换 refresh_token，旧的一经使用即作废。
"""

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from harness.adapters.mcp.http import McpHttpFactory
from harness.adapters.mcp.oauth import McpOAuth, OAuthClientSettings
from harness.adapters.mcp.oauth_state import LocalOAuthStateStore
from harness.adapters.mcp.runtime import McpRuntime, McpRuntimeLimits
from harness.domain.mcp import McpAuthType, McpServerConfig, McpToolSummary
from harness.domain.mcp_oauth import McpOAuthClient, McpOAuthToken
from tests.fixtures.mcp_fake import FAKE_HOST, FAKE_URL, FakeMcpServer, FixedDns, _Gate, make_config

AUTH_HOST = "auth.example.com"
ISSUER = f"https://{AUTH_HOST}"
RESOURCE = FAKE_URL


@dataclass
class IssuedCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    scope: str | None


@dataclass
class FakeAuthServer:
    """可编程的假授权服务器：开关控制 CIMD / DCR、iss、PKCE 声明、拒绝刷新与 token 有效期。"""

    cimd: bool = False
    dcr: bool = True
    iss_supported: bool = True
    pkce: bool = True
    expires_in: int | None = 3600
    refresh_status: int = 200
    rotate_refresh: bool = True
    issuer_override: str | None = None
    scopes: tuple[str, ...] = ("tools.read",)
    # 非空时动态注册签发 client_secret，并要求 token 请求用 client_secret_basic 认证。
    client_secret: str | None = None
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    basic_auth_headers: list[str | None] = field(default_factory=list)
    codes: dict[str, IssuedCode] = field(default_factory=dict)
    access_tokens: set[str] = field(default_factory=set)
    refresh_tokens: dict[str, str] = field(default_factory=dict)  # refresh → client_id
    token_requests: list[dict[str, str]] = field(default_factory=list)
    registrations: list[dict[str, Any]] = field(default_factory=list)
    refresh_calls: int = 0

    def metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "issuer": self.issuer_override or ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "scopes_supported": list(self.scopes),
            "authorization_response_iss_parameter_supported": self.iss_supported,
            "client_id_metadata_document_supported": self.cimd,
        }
        if self.dcr:
            data["registration_endpoint"] = f"{ISSUER}/register"
        if self.pkce:
            data["code_challenge_methods_supported"] = ["S256"]
        return data

    def authorize(self, authorization_url: str) -> tuple[str, str, str | None]:
        """模拟用户在浏览器里同意：校验授权请求并签发授权码，返回 (state, code, iss)。"""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert query["resource"] == RESOURCE, query
        client_id = query["client_id"]
        if not client_id.startswith("https://"):
            assert client_id in self.clients, "未注册的 client_id"
            assert query["redirect_uri"] in self.clients[client_id]["redirect_uris"]
        code = secrets.token_urlsafe(16)
        self.codes[code] = IssuedCode(
            client_id=client_id,
            redirect_uri=query["redirect_uri"],
            code_challenge=query["code_challenge"],
            resource=query["resource"],
            scope=query.get("scope"),
        )
        return query["state"], code, ISSUER if self.iss_supported else None

    def _issue(self, client_id: str) -> dict[str, Any]:
        access = f"at-{secrets.token_urlsafe(12)}"
        refresh = f"rt-{secrets.token_urlsafe(12)}"
        self.access_tokens.add(access)
        self.refresh_tokens[refresh] = client_id
        body: dict[str, Any] = {
            "access_token": access,
            "token_type": "bearer",
            "refresh_token": refresh,
            "scope": " ".join(self.scopes),
        }
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return body

    async def _token(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        self.token_requests.append(form)
        authorization = request.headers.get("authorization")
        self.basic_auth_headers.append(authorization)
        if self.client_secret is not None:
            expected = base64.b64encode(
                f"{form.get('client_id')}:{self.client_secret}".encode(),
            ).decode()
            if authorization != f"Basic {expected}" or "client_secret" in form:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
        if form.get("resource") != RESOURCE:
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        if form["grant_type"] == "authorization_code":
            issued = self.codes.pop(form.get("code", ""), None)
            if issued is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            verifier = form.get("code_verifier", "")
            digest = hashlib.sha256(verifier.encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            redirect_ok = form.get("redirect_uri") == issued.redirect_uri
            if challenge != issued.code_challenge or not redirect_ok:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if form.get("client_id") != issued.client_id:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            return JSONResponse(self._issue(issued.client_id))
        if form["grant_type"] == "refresh_token":
            self.refresh_calls += 1
            if self.refresh_status != 200:
                return JSONResponse({"error": "invalid_grant"}, status_code=self.refresh_status)
            client_id = self.refresh_tokens.pop(form.get("refresh_token", ""), None)
            if client_id is None:
                # 旧 refresh_token 被重放：真实授权服务器会吊销整组授权（LibreChat #15977）。
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            body = self._issue(client_id)
            if not self.rotate_refresh:
                self.refresh_tokens[form["refresh_token"]] = client_id
                del body["refresh_token"]
            return JSONResponse(body)
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def _register(self, request: Request) -> Response:
        body = await request.json()
        self.registrations.append(body)
        client_id = f"client-{secrets.token_hex(4)}"
        record = {**body, "client_id": client_id, "token_endpoint_auth_method": "none"}
        if self.client_secret is not None:
            record["client_secret"] = self.client_secret
            record["token_endpoint_auth_method"] = "client_secret_basic"
        self.clients[client_id] = record
        return JSONResponse(record, status_code=201)

    def app(self) -> Starlette:
        async def metadata(request: Request) -> Response:
            return JSONResponse(self.metadata())

        return Starlette(
            routes=[
                Route("/.well-known/oauth-authorization-server", metadata),
                Route("/token", self._token, methods=["POST"]),
                Route("/register", self._register, methods=["POST"]),
            ],
        )

    def revoke_all(self) -> None:
        self.access_tokens.clear()


class ProtectedMcp:
    """包在假 MCP server 外面：/.well-known/oauth-protected-resource 返回 PRM；其余请求要求有效 Bearer，
    否则 401 并带 WWW-Authenticate（resource_metadata）。scope_required 非空时 403 insufficient_scope。"""

    def __init__(self, gate: _Gate, auth: FakeAuthServer) -> None:
        self._gate = gate
        self._auth = auth
        self.prm_enabled = True
        self.scope_required = False
        self.unauthorized = 0
        self.bearers: list[str | None] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._gate(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith("/.well-known/oauth-protected-resource"):
            if not self.prm_enabled:
                await JSONResponse({}, status_code=404)(scope, receive, send)
                return
            prm = {"resource": RESOURCE, "authorization_servers": [ISSUER]}
            await JSONResponse(prm)(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        bearer = headers.get("authorization", "")
        token = bearer[len("Bearer ") :] if bearer.startswith("Bearer ") else None
        self.bearers.append(token)
        if token is None or token not in self._auth.access_tokens:
            self.unauthorized += 1
            prm_url = f"https://{FAKE_HOST}/.well-known/oauth-protected-resource/mcp"
            challenge = f'Bearer resource_metadata="{prm_url}"'
            await Response(status_code=401, headers={"WWW-Authenticate": challenge})(
                scope,
                receive,
                send,
            )
            return
        if self.scope_required:
            challenge = 'Bearer error="insufficient_scope", scope="tools.write"'
            await Response(status_code=403, headers={"WWW-Authenticate": challenge})(
                scope,
                receive,
                send,
            )
            return
        await self._gate(scope, receive, send)


def host_router(apps: dict[str, Callable[[Scope, Receive, Send], Any]]) -> Callable[..., Any]:
    """按请求主机分派到不同 ASGI 应用；PinnedTransport 把 URL 换成 IP，原主机名在 Host 头里。"""

    async def route(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        host = headers.get("host", "").split(":")[0]
        app = apps.get(host)
        if app is None:
            await JSONResponse({"error": "unknown host"}, status_code=404)(scope, receive, send)
            return
        await app(scope, receive, send)

    return route


class MemoryOAuthStore:
    """McpOAuthStore 的内存替身：按 (server, 用户) 存 token，持锁刷新用 asyncio.Lock 模拟行锁。

    refresh_calls 记下进入锁的次数；snapshots 记下回写的快照；invalidated 记下被标记失效的 token。
    """

    def __init__(self) -> None:
        import asyncio

        self.clients: dict[int, McpOAuthClient] = {}
        self.tokens: dict[tuple[int, int], McpOAuthToken] = {}
        self.snapshots: dict[int, tuple[McpToolSummary, ...]] = {}
        self.invalidated: list[tuple[int, int]] = []
        self.token_reads = 0
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._asyncio = asyncio

    async def get_client(self, server_id: int) -> McpOAuthClient | None:
        return self.clients.get(server_id)

    async def save_client(self, server_id: int, client: McpOAuthClient) -> None:
        self.clients[server_id] = client

    async def get_token(self, server_id: int, user_id: int) -> McpOAuthToken | None:
        self.token_reads += 1
        return self.tokens.get((server_id, user_id))

    async def refresh_token_locked(
        self,
        server_id: int,
        user_id: int,
        refresh: Callable[[McpOAuthToken], Any],
    ) -> McpOAuthToken | None:
        key = (server_id, user_id)
        lock = self._locks.setdefault(key, self._asyncio.Lock())
        async with lock:
            current = self.tokens.get(key)
            if current is None:
                return None
            fresh = await refresh(current)
            if fresh is None:
                self.tokens.pop(key, None)
                self.invalidated.append(key)
                return None
            self.tokens[key] = fresh
            return fresh

    async def invalidate_token(self, server_id: int, user_id: int, access_token: str) -> bool:
        key = (server_id, user_id)
        current = self.tokens.get(key)
        if current is None or current.access_token != access_token:
            return False
        del self.tokens[key]
        self.invalidated.append(key)
        return True

    async def record_tools_snapshot(
        self,
        server_id: int,
        config_rev: int,
        tools: Any,
    ) -> bool:
        snapshot = tuple(tools)
        if self.snapshots.get(server_id) == snapshot:
            return False
        self.snapshots[server_id] = snapshot
        return True


@dataclass
class OAuthHarness:
    """一套 OAuth 测试装配：假 MCP server（带 PRM 与 Bearer 校验）、假授权服务器、内存 token 存储与运行时。"""

    fake: FakeMcpServer
    auth: FakeAuthServer
    protected: ProtectedMcp
    store: MemoryOAuthStore
    states: Any
    runtime: McpRuntime
    oauth: McpOAuth
    clock: list[float]
    dns: FixedDns
    dns_overrides: dict[str, list[str]]


@asynccontextmanager
async def oauth_harness(
    fake: FakeMcpServer | None = None,
    auth: FakeAuthServer | None = None,
    *,
    public_base_url: str | None = "https://agentos.example.com",
    states: Any = None,
    refresh_skew_seconds: int = 300,
    jitter: float = 0.0,
) -> AsyncIterator[OAuthHarness]:
    """起假 MCP server（ASGI）与假授权服务器，装好带 McpOAuth 的 McpRuntime；退出时关闭。"""
    fake = fake or FakeMcpServer()
    auth = auth or FakeAuthServer()
    server = fake.server()
    mcp_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    gate = _Gate(mcp_app)
    protected = ProtectedMcp(gate, auth)
    router = host_router({FAKE_HOST: protected, AUTH_HOST: auth.app()})
    dns = FixedDns()
    dns_overrides: dict[str, list[str]] = {}

    async def resolve(host: str, port: int) -> list[str]:
        # 按主机覆盖解析结果，用来模拟授权服务器解析到内网。
        if host in dns_overrides:
            dns.calls.append(host)
            return list(dns_overrides[host])
        return await dns(host, port)

    clock = [1_000_000.0]
    http = McpHttpFactory(
        allow_insecure_http=False,
        allowed_private_hosts=frozenset(),
        connect_timeout=2.0,
        read_timeout=30.0,
        resolver=resolve,
        inner_transport=lambda: httpx2.ASGITransport(app=router),
    )
    store = MemoryOAuthStore()
    state_store = states or LocalOAuthStateStore(clock=lambda: clock[0])
    oauth = McpOAuth(
        http=http,
        store=store,
        states=lambda: state_store,
        settings=OAuthClientSettings(
            public_base_url=public_base_url,
            refresh_skew_seconds=refresh_skew_seconds,
            jitter=lambda: jitter,
        ),
        clock=lambda: clock[0],
    )
    runtime = McpRuntime(
        http=http,
        limits=McpRuntimeLimits(
            connect_timeout=2.0,
            idle_seconds=300,
            pool_max_connections=8,
            tools_cache_seconds=300,
        ),
        oauth=oauth,
        clock=lambda: clock[0],
    )
    async with mcp_app.router.lifespan_context(mcp_app):
        try:
            yield OAuthHarness(
                fake=fake,
                auth=auth,
                protected=protected,
                store=store,
                states=state_store,
                runtime=runtime,
                oauth=oauth,
                clock=clock,
                dns=dns,
                dns_overrides=dns_overrides,
            )
        finally:
            await runtime.aclose()


def oauth_config(**overrides: Any) -> McpServerConfig:
    """一个 OAuth server 的配置，默认共享 server、地址指向假 MCP server。"""
    values: dict[str, Any] = {"auth_type": McpAuthType.OAUTH}
    values.update(overrides)
    return make_config(**values)
