"""MCP OAuth 2.1 适配器：授权的两个半程、调用路径上的 McpTokenAuth（httpx2.Auth）与持锁刷新的回调。

协议细节复用 SDK 的工具函数（mcp.client.auth.utils、PKCEParameters、shared.auth_utils，清单见设计文档 8.2 节
「M3 spike 结论」），token 的读取、刷新与写回由 AgentOS 自己掌握，经 McpOAuthStore 端口落库。
不用 SDK 的 OAuthClientProvider：它把库里读出的过期 token 当成有效、401 时直接走完整授权、看不到别的实例的刷新。

自己发出的请求（PRM / AS 元数据、注册、换 token、刷新）一律经 McpHttpFactory.oauth_client() 建的客户端：
不预置固定 IP 的 PinnedTransport，每个主机首次请求时解析、校验、固定，地址规则与该 server 相同；
follow_redirects=False，3xx 当作「此处没有」或失败；响应体读取有上限。

凭证（access_token、refresh_token、code、code_verifier、client_secret）不进日志、异常信息与错误响应：
授权服务器返回的错误只取 RFC 6749 的 error 码（白名单字符），不回显响应体。
"""

import asyncio
import contextvars
import logging
import secrets
import time
from base64 import b64encode
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx2
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.auth.oauth2 import PKCEParameters, check_registration_usable
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_info_from_metadata_url,
    create_client_registration_request,
    create_oauth_metadata_request,
    credentials_match_issuer,
    extract_field_from_www_auth,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    handle_token_response_scopes,
    is_valid_client_metadata_url,
    should_use_client_metadata_url,
    validate_authorization_response_iss,
    validate_metadata_issuer,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from mcp_types import LATEST_PROTOCOL_VERSION
from pydantic import AnyUrl, ValidationError

from core.errors import AgentException
from harness.adapters.mcp.http import McpAddressError, McpHttpFactory, validate_server_url
from harness.app.mcp_port import McpOAuthStore, OAuthStateStore
from harness.domain.mcp import McpServerConfig, McpToolSummary
from harness.domain.mcp_oauth import (
    OAUTH_CLIENT_NAME,
    OAUTH_GRANT_TYPES,
    McpOAuthClient,
    McpOAuthGrant,
    McpOAuthPending,
    McpOAuthToken,
    OAuthCallback,
    oauth_client_metadata_url,
    oauth_redirect_uri,
)

logger = logging.getLogger(__name__)

# state 在存储里的有效期：用户须在 10 分钟内完成浏览器里的授权（8.2 节）。
STATE_TTL_SECONDS = 600
# 自己发出的 OAuth 请求的响应体上限：元数据、注册与 token 响应都很小，超出视为异常。
_MAX_BODY_BYTES = 1024 * 1024
# 授权服务器错误码（RFC 6749 的 error）只放行这些字符，其余一律不回显。
_ERROR_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_")
# 规范要求 AS 元数据声明支持 S256，否则拒绝继续（security-considerations「PKCE」一节，MUST）。
_PKCE_METHOD = "S256"
# 发现结果（PRM 与 AS 元数据）在本进程缓存的秒数：刷新时不必每次重新发现；config_rev 变化即换键。
_DISCOVERY_TTL_SECONDS = 300
# 本进程 token 缓存（TokenCell）的条目上限，超出时丢最早的。
_MAX_CELLS = 4096


class McpOAuthError(AgentException):
    """OAuth 流程失败：消息是可展示的中文，不含 token、code、client_secret 与授权服务器响应体。"""

    status_code = 400


class McpAuthRequired(Exception):
    """没有有效 token 或刷新失败：该 server 需要用户（重新）授权。只在适配器内部流转。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _error_code(response: httpx2.Response) -> str | None:
    """取授权服务器响应里的 RFC 6749 error 码；只放行小写字母与下划线，不回显其余内容。"""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and code and len(code) <= 64 and set(code) <= _ERROR_CODE_CHARS:
        return code
    return None


async def _send(client: httpx2.AsyncClient, request: httpx2.Request) -> httpx2.Response:
    """发一个 OAuth 请求并读完响应体（有上限）；地址不合法、连不上都转成中文的 McpOAuthError。"""
    host = request.url.host
    try:
        response = await client.send(request, stream=True)
    except httpx2.HTTPError as exc:
        logger.warning(
            "MCP OAuth 请求失败，主机=%s，错误类型=%s",
            host,
            type(exc).__name__,
            extra={"mcp_oauth_host": host},
        )
        raise McpOAuthError.message("无法连接 OAuth 相关服务", {"host": host}) from exc
    try:
        size = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > _MAX_BODY_BYTES:
                raise McpOAuthError.message("OAuth 响应过大", {"host": host})
            chunks.append(chunk)
    finally:
        await response.aclose()
    # 读完的响应重新装一份，后续 SDK 的解析函数可以照常 aread()。
    return httpx2.Response(
        response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=request,
    )


@dataclass(frozen=True)
class OAuthDiscovery:
    """一次发现的结果：PRM、已校验 issuer 的 AS 元数据，以及 resource（RFC 8707）。"""

    prm: ProtectedResourceMetadata
    issuer: str
    metadata: OAuthMetadata
    resource: str
    scope: str | None


async def _challenge(client: httpx2.AsyncClient, server_url: str) -> httpx2.Response | None:
    """不带 token 向 MCP server 发一个请求，取 401 响应里的 WWW-Authenticate（规范要求优先用其中的
    resource_metadata）。只读响应头、立即关闭，不读响应体；不是 401 或连不上都返回 None，改走 well-known。
    """
    request = httpx2.Request(
        "POST",
        server_url,
        json={"jsonrpc": "2.0", "id": 0, "method": "server/discover"},
        headers={
            "Accept": "application/json, text/event-stream",
            MCP_PROTOCOL_VERSION_HEADER: LATEST_PROTOCOL_VERSION,
        },
    )
    try:
        response = await client.send(request, stream=True)
    except httpx2.HTTPError:
        return None
    await response.aclose()
    return response if response.status_code == 401 else None


def _require_endpoint(url: str, *, allow_insecure_http: bool, what: str) -> None:
    """授权服务器给出的地址只做格式校验（协议、userinfo、片段、空白）；连接时由 PinnedTransport 校验 IP。"""
    try:
        validate_server_url(url, allow_insecure_http=allow_insecure_http)
    except McpAddressError as exc:
        raise McpOAuthError.message(f"授权服务器的{what}地址不合法") from exc


async def discover(
    client: httpx2.AsyncClient,
    server_url: str,
    *,
    allow_insecure_http: bool,
) -> OAuthDiscovery:
    """401 挑战 → PRM 发现 → resource 校验 → AS 元数据发现与 issuer 校验 → PKCE 支持校验。

    MCP server 必须提供 PRM（规范 MUST），找不到就报错，不走 2025-03-26 的旧路径；授权服务器取 PRM 列出的第一个。
    """
    www_authenticate = await _challenge(client, server_url)
    www_url = None
    if www_authenticate is not None:
        www_url = extract_resource_metadata_from_www_auth(www_authenticate)
    if www_url is not None:
        _require_endpoint(www_url, allow_insecure_http=allow_insecure_http, what="受保护资源元数据")
    prm: ProtectedResourceMetadata | None = None
    for url in build_protected_resource_metadata_discovery_urls(www_url, server_url):
        prm = await handle_protected_resource_response(
            await _send(client, create_oauth_metadata_request(url)),
        )
        if prm is not None:
            break
    if prm is None:
        raise McpOAuthError.message("MCP server 没有提供 OAuth 受保护资源元数据（PRM）")
    # resource 用 PRM 声明的资源，前提是它是 server 地址本身或其上级（RFC 8707，同 SDK 的 get_resource_url）。
    requested = resource_url_from_server_url(server_url)
    resource = str(prm.resource)
    if not check_resource_allowed(requested_resource=requested, configured_resource=resource):
        raise McpOAuthError.message("MCP server 声明的受保护资源与其地址不符")
    issuer = str(prm.authorization_servers[0])
    _require_endpoint(issuer, allow_insecure_http=allow_insecure_http, what=" issuer ")
    metadata: OAuthMetadata | None = None
    for url in build_oauth_authorization_server_metadata_discovery_urls(issuer, server_url):
        ok, found = await handle_auth_metadata_response(
            await _send(client, create_oauth_metadata_request(url)),
        )
        if not ok:
            break
        if found is not None:
            metadata = found
            break
    if metadata is None:
        raise McpOAuthError.message("无法获取授权服务器元数据")
    try:
        validate_metadata_issuer(metadata, issuer)
    except OAuthFlowError as exc:
        raise McpOAuthError.message("授权服务器元数据的 issuer 与声明不符") from exc
    if _PKCE_METHOD not in (metadata.code_challenge_methods_supported or []):
        raise McpOAuthError.message("授权服务器不支持 PKCE（S256），不能授权")
    _require_endpoint(
        str(metadata.token_endpoint),
        allow_insecure_http=allow_insecure_http,
        what=" token 端点",
    )
    if metadata.registration_endpoint is not None:
        _require_endpoint(
            str(metadata.registration_endpoint),
            allow_insecure_http=allow_insecure_http,
            what="注册端点",
        )
    scope = get_client_metadata_scopes(
        extract_scope_from_www_auth(www_authenticate) if www_authenticate else None,
        prm,
        metadata,
        list(OAUTH_GRANT_TYPES),
    )
    return OAuthDiscovery(
        prm=prm,
        issuer=issuer,
        metadata=metadata,
        resource=resource,
        scope=scope,
    )


def _client_info(client: McpOAuthClient) -> OAuthClientInformationFull:
    return OAuthClientInformationFull.model_validate(client.info)


def token_request(
    endpoint: str,
    client: OAuthClientInformationFull,
    data: dict[str, str],
) -> httpx2.Request:
    """组装发往 token 端点的请求：字段同 SDK 的 _exchange_token_authorization_code / _refresh_token，

    客户端认证按 token_endpoint_auth_method 照 SDK 的 OAuthContext.prepare_token_auth 三个分支处理；
    未知方法在注册时已被 check_registration_usable 拒绝，存量记录遇到时报错。
    """
    form = {**data, "client_id": client.client_id}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    method = client.token_endpoint_auth_method
    if method == "client_secret_basic" and client.client_secret:
        # RFC 6749 §2.3.1：先 URL 编码再 Basic；secret 不放表单。
        raw = f"{quote(client.client_id, safe='')}:{quote(client.client_secret, safe='')}"
        headers["Authorization"] = f"Basic {b64encode(raw.encode()).decode()}"
    elif method == "client_secret_post" and client.client_secret:
        form["client_secret"] = client.client_secret
    elif method not in (None, "none", "client_secret_basic", "client_secret_post"):
        raise McpOAuthError.message("OAuth 客户端的认证方式不受支持，请撤销后重新授权")
    return httpx2.Request("POST", endpoint, data=form, headers=headers)


def expires_at_of(token: OAuthToken, now: float) -> datetime | None:
    """按注入的当前时间算过期时刻；SDK 的 calculate_token_expiry 读 time.time()，这里不用它。"""
    if token.expires_in is None:
        return None
    return datetime.fromtimestamp(now + int(token.expires_in), tz=UTC)


def to_domain_token(token: OAuthToken, *, issuer: str, now: float) -> McpOAuthToken:
    return McpOAuthToken(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        token_type=token.token_type,
        scope=token.scope,
        expires_at=expires_at_of(token, now),
        issuer=issuer,
    )


@dataclass(frozen=True)
class OAuthClientSettings:
    """OAuth 客户端的部署相关设置：对外地址决定回跳地址与 CIMD 的 client_id；由组装根从 Settings 读好后给出。

    public_base_url 为 None 表示未配置 AGENTOS_PUBLIC_BASE_URL，用到授权时抛具体的中文错误。
    refresh_skew_seconds 是回合解析时提前刷新的时间窗；jitter 返回 0～30 秒的随机抖动（适配层读随机数）。
    """

    public_base_url: str | None
    refresh_skew_seconds: int
    jitter: Callable[[], float] = field(default=lambda: secrets.randbelow(30_001) / 1000)

    def require_base_url(self) -> str:
        if not self.public_base_url:
            raise AgentException.message(
                "未配置 AGENTOS_PUBLIC_BASE_URL（前端对外地址），无法发起 MCP OAuth 授权",
                status_code=500,
            )
        return self.public_base_url.rstrip("/")


def _client_metadata(base_url: str, scope: str | None) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        redirect_uris=[AnyUrl(oauth_redirect_uri(base_url))],
        token_endpoint_auth_method="none",
        grant_types=list(OAUTH_GRANT_TYPES),
        response_types=["code"],
        client_name=OAUTH_CLIENT_NAME,
        scope=scope,
        # 回跳地址在远程前端（非 localhost）上：规范要求声明 application_type，web 应用用 "web"。
        application_type="web",
    )


async def ensure_client(
    http: httpx2.AsyncClient,
    store: McpOAuthStore,
    server_id: int,
    discovery: OAuthDiscovery,
    base_url: str,
) -> OAuthClientInformationFull:
    """取得可用的客户端注册：已有且属于同一授权服务器就复用；否则优先 CIMD，其次 DCR，结果写入 mcp_oauth_clients。

    授权服务器换了（issuer 不符）时不复用旧注册（规范 Authorization Server Binding，SDK 的 credentials_match_issuer）。
    """
    metadata_url = oauth_client_metadata_url(base_url)
    redirect = oauth_redirect_uri(base_url)
    existing = await store.get_client(server_id)
    if existing is not None:
        info = _client_info(existing)
        redirect_ok = redirect in [str(uri) for uri in info.redirect_uris or []]
        if redirect_ok and credentials_match_issuer(info, discovery.issuer, metadata_url):
            return info
    # CIMD 要求 client_id 是 https 且路径非根（开发环境 http 的对外地址不满足，走 DCR）。
    cimd = is_valid_client_metadata_url(metadata_url)
    if cimd and should_use_client_metadata_url(discovery.metadata, metadata_url):
        info = create_client_info_from_metadata_url(
            metadata_url,
            redirect_uris=[AnyUrl(redirect)],
        )
        mode = "CIMD"
    else:
        if discovery.metadata.registration_endpoint is None:
            raise McpOAuthError.message("授权服务器既不支持客户端元数据文档（CIMD），也不支持动态注册")
        request = create_client_registration_request(
            discovery.metadata,
            _client_metadata(base_url, discovery.scope),
            discovery.issuer,
        )
        response = await _send(http, request)
        try:
            info = await handle_registration_response(response)
            check_registration_usable(info)
        except OAuthRegistrationError as exc:
            logger.warning(
                "MCP OAuth 动态注册失败，server_id=%s，HTTP 状态=%s，错误码=%s",
                server_id,
                response.status_code,
                _error_code(response),
                extra={"mcp_server_id": server_id},
            )
            raise McpOAuthError.message("向授权服务器注册客户端失败") from exc
        mode = "DCR"
    info.issuer = discovery.issuer
    await store.save_client(
        server_id,
        McpOAuthClient(
            client_id=info.client_id,
            issuer=discovery.issuer,
            info=info.model_dump(mode="json", exclude_none=True),
        ),
    )
    logger.info(
        "MCP OAuth 客户端已注册，server_id=%s，方式=%s",
        server_id,
        mode,
        extra={"mcp_server_id": server_id},
    )
    return info


class McpOAuth:
    """OAuth 流程的进程级服务：两个半程、持锁刷新与失效，供 McpConnector、McpToolProvider 与 McpTokenAuth 共用。

    store 是 McpOAuthStore（mcp_server 域的短事务实现），states 取 state 存储（惰性：只有授权流程才连 Redis），
    均由组装根注入。本进程另持有三份缓存：按 (server, config_rev, 用户) 的 TokenCell、发现结果与快照摘要。
    """

    def __init__(
        self,
        *,
        http: McpHttpFactory,
        store: McpOAuthStore,
        states: Callable[[], OAuthStateStore],
        settings: OAuthClientSettings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._store = store
        self._states = states
        self._settings = settings
        self._clock = clock
        self._cells: dict[tuple[int, int, int], TokenCell] = {}
        self._discoveries: dict[tuple[int, int], tuple[float, OAuthDiscovery]] = {}
        # 每个 (server_id, config_rev) 最近一次回写或核对过的快照摘要，相同就不再碰数据库。
        self._snapshots: dict[tuple[int, int], tuple[str, ...]] = {}

    @property
    def store(self) -> McpOAuthStore:
        return self._store

    @property
    def settings(self) -> OAuthClientSettings:
        return self._settings

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=UTC)

    def cell(self, config: McpServerConfig, user_id: int) -> TokenCell:
        """这个身份在这个 server（这一配置版本）上的 token 缓存；同一进程内的连接共用。"""
        key = (config.require_id(), config.config_rev, user_id)
        cell = self._cells.get(key)
        if cell is None:
            cell = TokenCell(self, config, user_id)
            self._cells[key] = cell
            if len(self._cells) > _MAX_CELLS:
                del self._cells[next(iter(self._cells))]
        return cell

    def forget_user(self, server_id: int, user_id: int) -> None:
        """撤销或重新授权后：这个用户在这个 server 上的 token 缓存作废，下次从库里重新读。

        cell 留在原处：池里已建的连接仍引用它，作废后下一个请求就会重新读库。
        """
        for key in [k for k in self._cells if k[0] == server_id and k[2] == user_id]:
            self._cells[key].forget()

    def forget_server(self, server_id: int) -> None:
        """配置变更或删除后：这个 server 的 token 缓存、发现结果与快照摘要全部作废。"""
        for key in [k for k in self._cells if k[0] == server_id]:
            self._cells.pop(key).forget()
        for key in [k for k in self._discoveries if k[0] == server_id]:
            del self._discoveries[key]
        for key in [k for k in self._snapshots if k[0] == server_id]:
            del self._snapshots[key]

    async def _discover(self, http: httpx2.AsyncClient, config: McpServerConfig) -> OAuthDiscovery:
        """发现结果按 (server_id, config_rev) 缓存 5 分钟；授权前半程总是重新发现（见 begin）。"""
        key = (config.require_id(), config.config_rev)
        cached = self._discoveries.get(key)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        discovery = await discover(
            http,
            config.url,
            allow_insecure_http=self._http.allow_insecure_http,
        )
        self._discoveries[key] = (self._clock() + _DISCOVERY_TTL_SECONDS, discovery)
        return discovery

    async def check_metadata(self, config: McpServerConfig) -> None:
        """管理员探测 OAuth server：只验证 PRM 与 AS 元数据能被发现（8.4 节），不需要 token、不拉工具清单。"""
        async with self._http.oauth_client(config) as http:
            await discover(http, config.url, allow_insecure_http=self._http.allow_insecure_http)

    async def record_snapshot(
        self,
        config: McpServerConfig,
        tools: tuple[McpToolSummary, ...],
    ) -> bool:
        """OAuth server 的快照回写：本进程已核对过同一份清单就跳过；失败只记日志、返回 False。"""
        key = (config.require_id(), config.config_rev)
        digest = tuple(
            f"{tool.name}:{tool.input_schema_sha256}:{tool.description}:{tool.annotations}"
            for tool in sorted(tools, key=lambda tool: tool.name)
        )
        if self._snapshots.get(key) == digest:
            return False
        try:
            written = await self._store.record_tools_snapshot(key[0], key[1], tools)
        except Exception as exc:  # noqa: BLE001  快照只用于管理展示与权限校验，失败不影响回合
            logger.warning(
                "MCP OAuth server 工具快照回写失败，server_id=%s，错误类型=%s",
                key[0],
                type(exc).__name__,
                extra={"mcp_server_id": key[0]},
            )
            return False
        self._snapshots[key] = digest
        return written

    async def begin(self, config: McpServerConfig, *, workspace_id: int, user_id: int) -> str:
        """前半程：发现 → 注册 → PKCE 与 state → 写 state 存储 → 返回授权地址（带 resource）。"""
        base_url = self._settings.require_base_url()
        server_id = config.require_id()
        context = {"mcp_server_id": server_id, "user_id": user_id}
        # 前半程总是重新发现：授权服务器可能已更换，用户发起授权时以当下的元数据为准。
        self._discoveries.pop((server_id, config.config_rev), None)
        async with self._http.oauth_client(config) as http:
            discovery = await self._discover(http, config)
            client = await ensure_client(http, self._store, server_id, discovery, base_url)
        # 授权端点只交给浏览器打开、服务端不连它，只做格式校验。
        endpoint = str(discovery.metadata.authorization_endpoint)
        _require_endpoint(
            endpoint,
            allow_insecure_http=self._http.allow_insecure_http,
            what="授权端点",
        )
        pkce = PKCEParameters.generate()
        state = secrets.token_urlsafe(32)
        redirect = oauth_redirect_uri(base_url)
        pending = McpOAuthPending(
            workspace_id=workspace_id,
            user_id=user_id,
            server_id=server_id,
            config_rev=config.config_rev,
            issuer=discovery.issuer,
            as_metadata=discovery.metadata.model_dump(mode="json", exclude_none=True),
            resource=discovery.resource,
            redirect_uri=redirect,
            client_id=client.client_id,
            code_verifier=pkce.code_verifier,
            scope=discovery.scope,
        )
        await self._states().put(state, pending, ttl_seconds=STATE_TTL_SECONDS)
        params = {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": redirect,
            "state": state,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": _PKCE_METHOD,
            "resource": discovery.resource,
        }
        if discovery.scope:
            params["scope"] = discovery.scope
            # OIDC 要求申请 offline_access 时带 prompt=consent（同 SDK）。
            if "offline_access" in discovery.scope.split():
                params["prompt"] = "consent"
        separator = "&" if urlsplit(endpoint).query else "?"
        logger.info("MCP OAuth 授权已发起，server_id=%s", server_id, extra=context)
        return f"{endpoint}{separator}{urlencode(params)}"

    async def complete(
        self,
        callback: OAuthCallback,
        *,
        workspace_id: int,
        user_id: int,
        config_for: Callable[[int], Awaitable[McpServerConfig]],
    ) -> McpOAuthGrant:
        """后半程：GETDEL 取出 state → 核对发起人 → 校验 iss → 带 code_verifier 与 resource 换 token。

        config_for(server_id) 返回当前用户能授权的 server 配置（由调用方经 registry 取得，看不到即 404），
        换 token 的请求按该 server 的地址规则走 SSRF 防护。只做网络动作，不写 token。
        """
        pending = await self._states().take(callback.state)
        if pending is None:
            raise McpOAuthError.message("授权请求不存在或已过期，请重新发起授权")
        pending.require_owner(workspace_id=workspace_id, user_id=user_id)
        context = {"mcp_server_id": pending.server_id, "user_id": user_id}
        metadata = OAuthMetadata.model_validate(pending.as_metadata)
        try:
            validate_authorization_response_iss(callback.iss, metadata)
        except OAuthFlowError as exc:
            logger.warning("MCP OAuth 回跳的 iss 校验失败，server_id=%s", pending.server_id, extra=context)
            raise McpOAuthError.message("授权回跳的签发方（iss）与授权服务器不符") from exc
        config: McpServerConfig = await config_for(pending.server_id)
        if config.config_rev != pending.config_rev:
            raise McpOAuthError.message("授权期间 MCP server 配置已被修改，请重新发起授权", status_code=409)
        stored = await self._store.get_client(pending.server_id)
        if stored is None or stored.client_id != pending.client_id:
            raise McpOAuthError.message("OAuth 客户端注册已变化，请重新发起授权")
        client = _client_info(stored)
        request = token_request(
            str(metadata.token_endpoint),
            client,
            {
                "grant_type": "authorization_code",
                "code": callback.code,
                "redirect_uri": pending.redirect_uri,
                "code_verifier": pending.code_verifier,
                "resource": pending.resource,
            },
        )
        async with self._http.oauth_client(config) as http:
            response = await _send(http, request)
        if response.status_code not in (200, 201):
            logger.warning(
                "MCP OAuth 换取 token 失败，server_id=%s，HTTP 状态=%s，错误码=%s",
                pending.server_id,
                response.status_code,
                _error_code(response),
                extra=context,
            )
            raise McpOAuthError.message("授权服务器拒绝了授权码，请重新发起授权")
        try:
            token = await handle_token_response_scopes(response)
        except OAuthTokenError as exc:
            raise McpOAuthError.message("授权服务器返回的 token 格式不正确") from exc
        if token.scope is None:
            # RFC 6749 §5.1：省略 scope 表示与申请的相同（同 SDK 的 _handle_token_response）。
            token.scope = pending.scope
        logger.info("MCP OAuth 授权已完成，server_id=%s", pending.server_id, extra=context)
        return McpOAuthGrant(
            server_id=pending.server_id,
            config_rev=pending.config_rev,
            token=to_domain_token(token, issuer=pending.issuer, now=self._clock()),
        )

    async def refresh(
        self,
        config: McpServerConfig,
        user_id: int,
        stale: McpOAuthToken,
    ) -> McpOAuthToken:
        """持行锁刷新：锁内读到的 token 已不是手里这份（别的实例刷新过）就直接用；否则向授权服务器刷新。

        客户端注册与发现结果在加锁之前取好，锁里只有一次 token 请求的往返。
        失败一律抛 McpAuthRequired（原因只进服务端日志）：没有 refresh_token、授权服务器拒绝或已更换
        （token 随之在锁内标记失效）、等锁超时、网络错误（不写库）。
        """
        server_id = config.require_id()
        context = {"mcp_server_id": server_id, "user_id": user_id}
        try:
            stored = await self._store.get_client(server_id)
            async with self._http.oauth_client(config) as http:
                discovery = await self._discover(http, config)

                async def exchange(current: McpOAuthToken) -> McpOAuthToken | None:
                    if current.access_token != stale.access_token:
                        return current
                    if current.refresh_token is None or stored is None:
                        return None
                    if current.issuer is not None and current.issuer != discovery.issuer:
                        # 授权服务器换了：旧 refresh_token 不属于它，不发给新服务器。
                        logger.warning("MCP OAuth 授权服务器已更换，需要重新授权", extra=context)
                        return None
                    return await self._refresh_request(http, discovery, stored, current, context)

                fresh = await self._store.refresh_token_locked(server_id, user_id, exchange)
        except McpOAuthError as exc:
            logger.warning("MCP OAuth 刷新失败：%s", exc.message, extra=context)
            raise McpAuthRequired("刷新失败") from exc
        except AgentException as exc:
            # 等锁超时（McpTokenLockTimeout）等业务异常：不无限等待，判为需要重新授权。
            logger.warning("MCP OAuth 刷新未完成：%s", exc.message, extra=context)
            raise McpAuthRequired("刷新未完成") from exc
        if fresh is None:
            raise McpAuthRequired("没有可用的授权")
        if fresh.access_token != stale.access_token:
            logger.info("MCP OAuth token 已刷新，server_id=%s", server_id, extra=context)
        return fresh

    async def _refresh_request(
        self,
        http: httpx2.AsyncClient,
        discovery: OAuthDiscovery,
        stored: McpOAuthClient,
        current: McpOAuthToken,
        context: dict[str, Any],
    ) -> McpOAuthToken | None:
        """向授权服务器发刷新请求；4xx 视为明确拒绝（返回 None，token 失效），其余失败抛 McpOAuthError（不写库）。"""
        request = token_request(
            str(discovery.metadata.token_endpoint),
            _client_info(stored),
            {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token or "",
                "resource": discovery.resource,
            },
        )
        response = await _send(http, request)
        if 400 <= response.status_code < 500:
            logger.warning(
                "MCP OAuth 刷新被拒绝，HTTP 状态=%s，错误码=%s",
                response.status_code,
                _error_code(response),
                extra=context,
            )
            return None
        if response.status_code != 200:
            raise McpOAuthError.message("授权服务器暂时不可用", {"status": response.status_code})
        try:
            token = OAuthToken.model_validate_json(response.content)
        except ValidationError as exc:
            raise McpOAuthError.message("授权服务器返回的 token 格式不正确") from exc
        # RFC 6749 §6：刷新响应可以省略 scope 与 refresh_token，沿用旧值（同 SDK 的 _handle_refresh_response）。
        if token.scope is None:
            token.scope = current.scope
        if token.refresh_token is None:
            token.refresh_token = current.refresh_token
        return to_domain_token(token, issuer=current.issuer or discovery.issuer, now=self._clock())

    async def invalidate(self, config: McpServerConfig, user_id: int, token: McpOAuthToken) -> None:
        """调用中 401 且刷新失败：把这份 token 标记为失效；库里已是别的 token 时不动。失败只记日志。"""
        server_id = config.require_id()
        try:
            await self._store.invalidate_token(server_id, user_id, token.access_token)
        except Exception as exc:  # noqa: BLE001  标记失效失败不影响给模型的错误文本
            logger.warning(
                "MCP OAuth token 标记失效失败，server_id=%s，错误类型=%s",
                server_id,
                type(exc).__name__,
                extra={"mcp_server_id": server_id, "user_id": user_id},
            )


# ---------- 调用路径：McpTokenAuth 与本进程的 token 缓存 ----------


@dataclass
class AuthOutcome:
    """一次借用连接期间认证插件留下的结论：auth_required 为真表示没有有效 token、刷新失败或 scope 不足。

    认证插件不能抛异常（spike：在 httpx2.Auth 里抛异常会让 SDK 的传输任务组整体退出，调用方只拿到
    CancelledError），所以把结论写进这里，调用方据此把 SDK 合成的错误映射成「需要重新授权」。
    token 是被拒的那份；insufficient_scope 为真时 token 本身仍有效（只是 scope 不够），不标记失效。
    """

    auth_required: bool = False
    insufficient_scope: bool = False
    token: McpOAuthToken | None = None


_OUTCOME: contextvars.ContextVar[AuthOutcome | None] = contextvars.ContextVar(
    "mcp_oauth_outcome",
    default=None,
)


@contextmanager
def track_auth() -> Iterator[AuthOutcome]:
    """在当前上下文里收集认证结论；SDK 发请求时拷贝发送方上下文，认证插件因此写得到这里。"""
    outcome = AuthOutcome()
    reset = _OUTCOME.set(outcome)
    try:
        yield outcome
    finally:
        _OUTCOME.reset(reset)


def _report(token: McpOAuthToken | None, *, insufficient_scope: bool = False) -> None:
    outcome = _OUTCOME.get()
    if outcome is not None:
        outcome.auth_required = True
        outcome.insufficient_scope = outcome.insufficient_scope or insufficient_scope
        if token is not None:
            outcome.token = token


class TokenCell:
    """本进程里一个 (server, 用户) 的 token 缓存：首次读库后放在内存里，同一进程内的刷新串行。

    连接池里同一身份可能有多条连接（config_rev 变化前后），它们共用一个 cell，刷新只做一次。
    """

    def __init__(self, oauth: McpOAuth, config: McpServerConfig, user_id: int) -> None:
        self._oauth = oauth
        self._config = config
        self._user_id = user_id
        self._token: McpOAuthToken | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    @property
    def config(self) -> McpServerConfig:
        return self._config

    def seed(self, token: McpOAuthToken) -> None:
        """用库里读到的 token 更新缓存（回合解析与授权完成时），省一次读库。

        缓存里的更新（本进程刚刷新出、过期时间更晚）时保留缓存。
        """
        cached = self._token
        if (
            cached is not None
            and self._loaded
            and cached.expires_at is not None
            and token.expires_at is not None
            and cached.expires_at > token.expires_at
        ):
            return
        self._token = token
        self._loaded = True

    async def current(self) -> McpOAuthToken | None:
        """当前 token：缓存里没有才读库；过期先刷新。失败抛 McpAuthRequired。"""
        async with self._lock:
            if not self._loaded:
                self._token = await self._oauth.store.get_token(
                    self._config.require_id(),
                    self._user_id,
                )
                self._loaded = True
            token = self._token
            if token is None:
                raise McpAuthRequired("没有授权")
            if token.is_expired(self._oauth.now()):
                token = await self._refresh_locked(token)
            return token

    async def after_unauthorized(self, rejected: McpOAuthToken) -> McpOAuthToken:
        """收到 401：先重新读库（别的实例可能已刷新），库里还是被拒的那份就自己刷新一次。失败抛 McpAuthRequired。"""
        async with self._lock:
            if self._token is not None and self._token.access_token != rejected.access_token:
                return self._token
            stored = await self._oauth.store.get_token(self._config.require_id(), self._user_id)
            self._token = stored
            self._loaded = True
            if stored is None:
                raise McpAuthRequired("授权已失效")
            if stored.access_token != rejected.access_token:
                return stored
            return await self._refresh_locked(stored)

    async def refresh_if_expiring(self, skew_seconds: float) -> McpOAuthToken:
        """回合解析时调用：离过期不到 skew_seconds 秒就主动刷新。失败抛 McpAuthRequired。"""
        token = await self.current()
        if not token.expires_within(self._oauth.now(), skew_seconds):
            return token
        async with self._lock:
            current = self._token
            if current is None:
                raise McpAuthRequired("授权已失效")
            if current.access_token != token.access_token:
                return current
            return await self._refresh_locked(current)

    async def _refresh_locked(self, token: McpOAuthToken) -> McpOAuthToken:
        try:
            fresh = await self._oauth.refresh(self._config, self._user_id, token)
        except McpAuthRequired:
            self._token = None
            self._loaded = False
            raise
        self._token = fresh
        return fresh

    def forget(self) -> None:
        self._token = None
        self._loaded = False


class McpTokenAuth(httpx2.Auth):
    """调用路径上的认证插件，按 (server, 用户) 构造，挂在这个用户的 MCP 连接上。

    - 请求前取缓存的 token，过期先刷新；
    - 收到 401：先重新读库换用别处刷新过的 token，否则自己刷新一次再重发；
    - 刷新也失败、或 403 insufficient_scope：不再重发，把结论记进 AuthOutcome，响应原样交还 SDK；
    - 任何失败都不抛异常（见 AuthOutcome 的说明），没有 token 时不带 Authorization 照常发出。
    """

    requires_response_body = False

    def __init__(self, cell: TokenCell) -> None:
        self._cell = cell

    async def async_auth_flow(
        self,
        request: httpx2.Request,
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        token = await self._token_or_none(self._cell.current())
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token.access_token}"
        response = yield request
        if response.status_code == 403 and _insufficient_scope(response):
            _report(token, insufficient_scope=True)
            return
        if response.status_code != 401:
            return
        if token is None:
            _report(None)
            return
        retry = await self._token_or_none(self._cell.after_unauthorized(token))
        if retry is None:
            _report(token)
            return
        request.headers["Authorization"] = f"Bearer {retry.access_token}"
        response = yield request
        if response.status_code == 401:
            _report(retry)
        elif response.status_code == 403 and _insufficient_scope(response):
            _report(retry, insufficient_scope=True)

    async def _token_or_none(self, pending: Any) -> McpOAuthToken | None:
        try:
            return await pending
        except McpAuthRequired:
            return None
        except Exception as exc:  # noqa: BLE001  认证插件不能抛异常，否则整条连接报废
            logger.warning(
                "MCP OAuth 取 token 失败，错误类型=%s",
                type(exc).__name__,
                extra={"mcp_server_id": self._cell.config.server_id},
            )
            return None


def _insufficient_scope(response: httpx2.Response) -> bool:
    return extract_field_from_www_auth(response, "error") == "insufficient_scope"
