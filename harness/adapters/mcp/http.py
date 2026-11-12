"""出站 HTTP：地址校验（SSRF）、按 IP 连接防 DNS rebinding、请求头清洗与客户端工厂。

用户填写的地址由服务端去连接，所以每个 DNS 结果都要是公网地址；校验用的那份解析结果就是连接用的
地址：PinnedTransport 把请求的主机换成校验过的 IP，原主机名留在 Host 头和 TLS SNI 里。
内网白名单只对共享 server 生效。日志与错误只带主机名，不带完整 URL 和请求头值。
"""

import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

import anyio
import httpx2

from core.errors import AgentException
from harness.domain.mcp import McpSecret, McpServerConfig, validate_static_headers

logger = logging.getLogger(__name__)

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
"""DNS 解析：(主机名, 端口) → IP 字符串列表。测试注入固定结果，生产用 system_resolver。"""

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_DEFAULT_PORTS = {"https": 443, "http": 80}
_NAT64_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")


class McpAddressError(AgentException):
    """MCP server 地址不合法或指向非公网地址；属调用方输入错误，不重试。"""

    status_code = 400


async def system_resolver(host: str, port: int) -> list[str]:
    """用系统 DNS 解析主机，按返回顺序去重。"""
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for *_, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def parse_allowed_hosts(raw: str) -> frozenset[str]:
    """把逗号分隔的内网主机名白名单解析成小写集合，空项忽略。"""
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def _rfc6052_ipv4(value: int, prefix_len: int) -> ipaddress.IPv4Address:
    """按 RFC 6052 从 NAT64 地址里取出内嵌 IPv4；第 64～71 位是保留的 u 字节，跳过。"""
    if prefix_len == 96:
        embedded = value & 0xFFFFFFFF
    elif prefix_len == 64:
        embedded = (value >> 24) & 0xFFFFFFFF
    else:
        # 前缀 48 / 56：IPv4 被 u 字节拆成高、低两段。
        high_bits = 64 - prefix_len
        low_bits = 32 - high_bits
        high = (value >> 64) & ((1 << high_bits) - 1)
        low = (value >> (56 - low_bits)) & ((1 << low_bits) - 1)
        embedded = (high << low_bits) | low
    return ipaddress.IPv4Address(embedded)


def embedded_ipv4(address: ipaddress.IPv6Address) -> tuple[ipaddress.IPv4Address, ...]:
    """还原 IPv6 地址里内嵌的 IPv4：IPv4 映射、IPv4 兼容、NAT64、6to4、Teredo。

    64:ff9b:1::/48 是本地 NAT64 前缀，前缀长度由运营方决定，按 48、56、64、96 各还原一次，
    任一结果不是公网就拒绝；Teredo 的服务器与客户端地址都要校验。
    """
    value = int(address)
    found: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.extend(address.teredo)
    if address in _NAT64_PREFIXES[0]:
        found.append(_rfc6052_ipv4(value, 96))
    elif address in _NAT64_PREFIXES[1]:
        found.extend(_rfc6052_ipv4(value, length) for length in (48, 56, 64, 96))
    if address in _IPV4_COMPATIBLE:
        found.append(ipaddress.IPv4Address(value & 0xFFFFFFFF))
    return tuple(found)


def _is_global_unicast(address: IPAddress) -> bool:
    # 只看 is_global 不够：在 3.14 上组播 224.0.0.1、保留段 ::7f00:1 的 is_global 也为 True。
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def is_public_address(address: IPAddress) -> bool:
    """白名单思路判定公网地址：先还原内嵌 IPv4，再要求还原后的每个地址都是全局单播。"""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = embedded_ipv4(address)
        if embedded:
            return all(_is_global_unicast(item) for item in embedded)
    return _is_global_unicast(address)


@dataclass(frozen=True)
class ServerOrigin:
    """校验过的 server 地址：协议、主机名（小写）与端口。"""

    scheme: str
    host: str
    port: int


@dataclass(frozen=True)
class PinnedOrigin:
    """校验通过的地址与它固定连接的 IP。"""

    origin: ServerOrigin
    address: str


def validate_server_url(url: str, *, allow_insecure_http: bool) -> ServerOrigin:
    """静态校验地址：只允许 https（开发开关可放开 http），不允许 userinfo、片段和 ${...} 占位符。

    控制字符与空白一律拒绝：URL 解析器会悄悄剥掉它们，造成校验与连接看到的地址不一致。
    """
    if "${" in url or any(char.isspace() or ord(char) < 0x20 for char in url):
        raise McpAddressError.message("MCP server 地址不能包含占位符、空白或控制字符")
    if "#" in url:
        raise McpAddressError.message("MCP server 地址不能包含片段（#）")
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL as exc:
        raise McpAddressError.message("MCP server 地址格式不正确") from exc
    allowed = {"https", "http"} if allow_insecure_http else {"https"}
    if parsed.scheme not in allowed:
        raise McpAddressError.message("MCP server 地址只支持 https", {"scheme": parsed.scheme})
    if parsed.userinfo:
        raise McpAddressError.message("MCP server 地址不能包含用户名或密码", {"host": parsed.host})
    if not parsed.host:
        raise McpAddressError.message("MCP server 地址缺少主机名")
    port = parsed.port if parsed.port is not None else _DEFAULT_PORTS[parsed.scheme]
    return ServerOrigin(scheme=parsed.scheme, host=parsed.host.lower(), port=port)


@dataclass(frozen=True)
class AddressPolicy:
    """一个 server 的出站地址规则；allowed_private_hosts 只对共享 server 传入非空集合。"""

    allow_insecure_http: bool
    allowed_private_hosts: frozenset[str] = frozenset()

    @classmethod
    def for_server(
        cls,
        config: McpServerConfig,
        *,
        allow_insecure_http: bool,
        allowed_private_hosts: frozenset[str],
    ) -> AddressPolicy:
        """私有 server 不适用内网白名单：普通成员不能借运维的放行访问内网。"""
        hosts = frozenset() if config.is_private else allowed_private_hosts
        return cls(allow_insecure_http=allow_insecure_http, allowed_private_hosts=hosts)


async def resolve_public(host: str, port: int, *, policy: AddressPolicy, resolver: Resolver) -> str:
    """解析主机并校验每一个 DNS 结果，返回第一个地址用于固定连接。

    IP 字面量不做 DNS；命中内网白名单的主机跳过公网校验。多条记录里混有任一非公网地址就整体拒绝，
    不挑一个公网地址凑合连，防止解析结果被部分投毒。
    """
    try:
        literal: IPAddress | None = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        candidates = [str(literal)]
    else:
        try:
            candidates = list(await resolver(host, port))
        except OSError as exc:
            raise McpAddressError.message("MCP server 主机名解析失败", {"host": host}) from exc
    if not candidates:
        raise McpAddressError.message("MCP server 主机名没有解析结果", {"host": host})
    if host.lower() in policy.allowed_private_hosts:
        return candidates[0]
    for candidate in candidates:
        if not is_public_address(ipaddress.ip_address(candidate)):
            logger.warning(
                "MCP server 地址解析到非公网地址，已拒绝连接，主机=%s",
                host,
                extra={"mcp_host": host},
            )
            raise McpAddressError.message("MCP server 地址指向内网或保留地址", {"host": host})
    return candidates[0]


class PinnedTransport(httpx2.AsyncBaseTransport):
    """按主机固定 IP 的传输层：首次请求某主机时解析并校验，之后该主机一律连到这个 IP。

    请求 URL 的主机换成 IP，原主机名保留在 Host 头和 sni_hostname 扩展里，证书照常按主机名校验。
    固定结果跟着 Transport 走，即跟着池里的一条连接走；不同主机各自解析、各自校验、各自固定。
    """

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        *,
        policy: AddressPolicy,
        resolver: Resolver,
        pinned: dict[tuple[str, int], str] | None = None,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._resolver = resolver
        # 可预置建连前已校验的结果：校验用的那份解析就是连接用的地址，不再解析第二次。
        self._pinned: dict[tuple[str, int], str] = dict(pinned or {})
        self._lock = anyio.Lock()

    async def _pin(self, host: str, port: int) -> str:
        key = (host.lower(), port)
        async with self._lock:
            address = self._pinned.get(key)
            if address is None:
                address = await resolve_public(
                    host,
                    port,
                    policy=self._policy,
                    resolver=self._resolver,
                )
                self._pinned[key] = address
            return address

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        allowed = {"https", "http"} if self._policy.allow_insecure_http else {"https"}
        if url.scheme not in allowed:
            raise httpx2.UnsupportedProtocol(f"不允许的协议 {url.scheme}", request=request)
        port = url.port if url.port is not None else _DEFAULT_PORTS[url.scheme]
        try:
            address = await self._pin(url.host, port)
        except McpAddressError as exc:
            raise httpx2.ConnectError(exc.message, request=request) from exc
        # Host 头由客户端在构造请求时按原主机名写好，这里只换连接目标。
        pinned = httpx2.Request(
            request.method,
            url.copy_with(host=address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        response = await self._inner.handle_async_request(pinned)
        # 让上层看到的仍是原始请求（主机名而不是 IP），重定向判定与日志都按主机名走。
        response.request = request
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _default_inner_transport() -> httpx2.AsyncBaseTransport:
    # 不走环境代理：代理会替我们解析主机，固定 IP 就失效了。
    return httpx2.AsyncHTTPTransport(proxy=None, trust_env=False)


@dataclass(frozen=True)
class McpHttpFactory:
    """出站 HTTP 客户端工厂：按 server 配置建带 SSRF 防护的 httpx2 客户端。

    resolver 与 inner_transport 由组装根给出；测试注入固定 DNS 结果与挂着假 server 的
    ASGITransport，不访问外网、不监听端口。
    """

    allow_insecure_http: bool
    allowed_private_hosts: frozenset[str]
    connect_timeout: float
    read_timeout: float
    resolver: Resolver
    inner_transport: Callable[[], httpx2.AsyncBaseTransport] = field(
        default=_default_inner_transport,
    )

    def policy_for(self, config: McpServerConfig) -> AddressPolicy:
        return AddressPolicy.for_server(
            config,
            allow_insecure_http=self.allow_insecure_http,
            allowed_private_hosts=self.allowed_private_hosts,
        )

    async def check(self, config: McpServerConfig) -> PinnedOrigin:
        """建连前的完整校验：地址格式，加上 DNS 结果全部是公网地址（白名单主机除外）。

        返回的固定地址交给 create()，建连时直接连这个 IP，不再重新解析。
        """
        origin = validate_server_url(config.url, allow_insecure_http=self.allow_insecure_http)
        address = await resolve_public(
            origin.host,
            origin.port,
            policy=self.policy_for(config),
            resolver=self.resolver,
        )
        return PinnedOrigin(origin=origin, address=address)

    def create(
        self,
        config: McpServerConfig,
        secret: McpSecret | None,
        pinned: PinnedOrigin,
    ) -> httpx2.AsyncClient:
        """建客户端：请求头只来自 server 配置的静态请求头，不转发入站请求的任何头。"""
        headers = sanitize_headers(secret.as_dict() if secret is not None else {})
        origin = pinned.origin
        transport = PinnedTransport(
            self.inner_transport(),
            policy=self.policy_for(config),
            resolver=self.resolver,
            pinned={(origin.host, origin.port): pinned.address},
        )
        return httpx2.AsyncClient(
            transport=transport,
            headers=headers,
            timeout=httpx2.Timeout(self.connect_timeout, read=self.read_timeout),
            follow_redirects=False,
            # 不读 .netrc 与环境代理：凭证只来自配置，连接只走固定 IP。
            trust_env=False,
        )


def sanitize_headers(headers: Iterable[tuple[str, str]] | dict[str, str]) -> dict[str, str]:
    """校验并复制请求头：名称合法、不覆盖受保护的头，值不含 CR、LF。"""
    items = dict(headers.items() if isinstance(headers, dict) else headers)
    validate_static_headers(items)
    return items
