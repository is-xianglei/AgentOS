"""MCP OAuth 的领域值类型与纯规则：token、客户端注册、授权中的 state、授权状态与回跳身份。

全部是不可变值类型和纯函数：不做 I/O，不读配置、时间与随机数（当前时间由调用方传入），不导入 mcp SDK。
SDK 的 OAuthToken、OAuthClientInformationFull 与这些类型之间的翻译在 harness/adapters/mcp 里完成；
凭证（access_token、refresh_token、code_verifier、client_secret 所在的 payload）一律不进 repr。
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from core.errors import AgentException

# 前端回跳路由：授权服务器把浏览器带回这里，前端取下 state、code、iss 后调完成接口。
OAUTH_CALLBACK_PATH = "/mcp/oauth/callback"
# CIMD（Client ID Metadata Document）的发布路径，经前端的 /api 代理转给后端。
OAUTH_CLIENT_METADATA_PATH = "/api/mcp/oauth/client-metadata.json"
OAUTH_CLIENT_NAME = "AgentOS"
OAUTH_GRANT_TYPES = ("authorization_code", "refresh_token")


class McpOAuthStatus(StrEnum):
    """当前用户在一个 OAuth server 上的授权状态，接口只回显它和 scope，不回显 token。"""

    AUTHORIZED = "authorized"
    UNAUTHORIZED = "unauthorized"
    EXPIRED = "expired"


@dataclass(frozen=True)
class McpOAuthToken:
    """一个用户在一个 server 上的 token（明文）；只用于建连与刷新，不进日志和接口响应。"""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = "Bearer"
    scope: str | None = None
    expires_at: datetime | None = None
    issuer: str | None = None

    def payload(self) -> dict[str, Any]:
        """落库到 mcp_oauth_tokens.token 的形状：SDK OAuthToken 的字段，过期时间另存 expires_at 列。"""
        data: dict[str, Any] = {"access_token": self.access_token, "token_type": self.token_type}
        if self.refresh_token is not None:
            data["refresh_token"] = self.refresh_token
        if self.scope is not None:
            data["scope"] = self.scope
        return data

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        issuer: str | None,
        expires_at: datetime | None,
        scope: str | None,
    ) -> McpOAuthToken:
        return cls(
            access_token=str(payload["access_token"]),
            refresh_token=payload.get("refresh_token"),
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=scope if scope is not None else payload.get("scope"),
            expires_at=expires_at,
            issuer=issuer,
        )

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def expires_within(self, now: datetime, seconds: float) -> bool:
        """离过期不到 seconds 秒（含已过期）；没有过期时间的 token 视为不会过期。"""
        return self.expires_at is not None and self.expires_at - now <= timedelta(seconds=seconds)


type McpTokenRefresh = Callable[[McpOAuthToken], Awaitable[McpOAuthToken | None]]
"""持锁刷新的回调，由适配器给出（业务域不做网络 I/O）：拿锁内读到的当前 token，

- 已被别处刷新过：原样返回它，不写库；
- 否则向授权服务器换新 token 并返回，由调用方在锁内写回；
- 授权服务器明确拒绝（或没有 refresh_token）：返回 None，由调用方在锁内把 token 标记为失效。
网络错误等暂时性失败直接抛异常，事务回滚、不写库。
"""


@dataclass(frozen=True)
class McpOAuthClient:
    """一个 server 的 OAuth 客户端注册信息（SDK 的 OAuthClientInformationFull，可能含 client_secret，明文）。

    issuer 是注册时所针对的授权服务器，授权服务器一换就要重新注册（规范的 Authorization Server Binding）。
    """

    client_id: str
    issuer: str | None
    info: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class McpOAuthAuthorization:
    """接口回显的授权状态：只有状态、scope 与过期时间，不含 token。"""

    status: McpOAuthStatus
    scope: str | None = None
    expires_at: datetime | None = None


def oauth_authorization(token: McpOAuthToken | None, now: datetime) -> McpOAuthAuthorization:
    """按当前 token 算授权状态：没有 token 为未授权；已过期且没有 refresh_token 为已过期；其余为已授权。"""
    if token is None:
        return McpOAuthAuthorization(status=McpOAuthStatus.UNAUTHORIZED)
    status = McpOAuthStatus.AUTHORIZED
    if token.is_expired(now) and token.refresh_token is None:
        status = McpOAuthStatus.EXPIRED
    return McpOAuthAuthorization(status=status, scope=token.scope, expires_at=token.expires_at)


@dataclass(frozen=True)
class OAuthCallback:
    """前端从授权回跳地址上取下、再转交后端的参数；code 不进 repr。"""

    state: str
    code: str = field(repr=False)
    iss: str | None = None


@dataclass(frozen=True)
class McpOAuthPending:
    """授权进行中的流程状态，按 state 存进 state 存储（Redis，TTL 10 分钟），完成时一次性取出。

    记下发起人身份与 server 版本，完成时据此核对；issuer 与授权服务器元数据取自前半程已校验的结果
    （规范要求把它与 code_verifier 记在同一条记录里），后半程不再重新发现。code_verifier 不进 repr。
    """

    workspace_id: int
    user_id: int
    server_id: int
    config_rev: int
    issuer: str
    as_metadata: dict[str, Any] = field(repr=False)
    resource: str
    redirect_uri: str
    client_id: str
    code_verifier: str = field(repr=False)
    scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "server_id": self.server_id,
            "config_rev": self.config_rev,
            "issuer": self.issuer,
            "as_metadata": self.as_metadata,
            "resource": self.resource,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": self.code_verifier,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> McpOAuthPending:
        return cls(
            workspace_id=int(data["workspace_id"]),
            user_id=int(data["user_id"]),
            server_id=int(data["server_id"]),
            config_rev=int(data["config_rev"]),
            issuer=str(data["issuer"]),
            as_metadata=dict(data["as_metadata"]),
            resource=str(data["resource"]),
            redirect_uri=str(data["redirect_uri"]),
            client_id=str(data["client_id"]),
            code_verifier=str(data["code_verifier"]),
            scope=data.get("scope"),
        )

    def require_owner(self, *, workspace_id: int, user_id: int) -> None:
        """核对 state 的发起人就是当前登录用户，防止把受害者的授权绑到攻击者账户上（LibreChat GHSA-vf7j）。"""
        if self.workspace_id != workspace_id or self.user_id != user_id:
            raise AgentException.message(
                "授权请求与当前登录用户不一致，请重新发起授权",
                status_code=403,
            )


def oauth_redirect_uri(public_base_url: str) -> str:
    """回跳地址：前端路由 {public_base_url}/mcp/oauth/callback。"""
    return f"{public_base_url.rstrip('/')}{OAUTH_CALLBACK_PATH}"


def oauth_client_metadata_url(public_base_url: str) -> str:
    """CIMD 的 client_id：后端发布的客户端元数据文档地址，经前端同域的 /api 代理访问。"""
    return f"{public_base_url.rstrip('/')}{OAUTH_CLIENT_METADATA_PATH}"


def oauth_client_metadata_document(public_base_url: str) -> dict[str, Any]:
    """CIMD 文档：client_id 必须等于文档地址本身，公开客户端（不带 client_secret）。"""
    return {
        "client_id": oauth_client_metadata_url(public_base_url),
        "client_name": OAUTH_CLIENT_NAME,
        "redirect_uris": [oauth_redirect_uri(public_base_url)],
        "grant_types": list(OAUTH_GRANT_TYPES),
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "web",
    }


@dataclass(frozen=True)
class McpOAuthGrant:
    """授权后半程换到的 token：属于哪个 server、发起时的配置版本；写库前要核对配置没有变。"""

    server_id: int
    config_rev: int
    token: McpOAuthToken
