"""MCP 出站 HTTP 的 SSRF 防护：地址判定、URL 校验、DNS 全量校验、按 IP 连接与内网白名单。

DNS 一律用固定结果替身，传输层用 MockTransport，不访问外网、不监听端口。
"""

import ipaddress

import httpx2
import pytest

from harness.adapters.mcp.http import (
    AddressPolicy,
    McpAddressError,
    McpHttpFactory,
    PinnedTransport,
    is_public_address,
    parse_allowed_hosts,
    resolve_public,
    sanitize_headers,
    validate_server_url,
)
from harness.domain.mcp import McpSecret, McpServerConfig

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.1",  # 运营商级 NAT
        "100.100.100.200",  # 阿里云元数据
        "169.254.169.254",  # 链路本地 / 云元数据
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "0.0.0.0",
        "224.0.0.1",  # 组播：3.14 上 is_global 为 True
        "239.255.255.250",
        "240.0.0.1",
        "255.255.255.255",
        "198.18.0.1",
        "::1",
        "::",
        "fd00::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4 映射
        "::ffff:169.254.169.254",
        "::7f00:1",  # IPv4 兼容：3.14 上 is_global 为 True
        "::a9fe:a9fe",
        "64:ff9b::a9fe:a9fe",  # NAT64 → 169.254.169.254：3.14 上 is_global 为 True
        "64:ff9b::7f00:1",
        "64:ff9b:1:a9fe:a9:fe00::",  # 本地 NAT64 /48 内嵌 169.254.169.254
        "64:ff9b:1::a9fe:a9fe",
        "2002:a9fe:a9fe::1",  # 6to4 → 169.254.169.254
        "2002:7f00:1::1",
        "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo，客户端地址 127.0.0.1
        "2001:0:a00:1:8000:63bf:3fff:fdd2",  # Teredo，服务器地址 10.0.0.1
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "1.1.1.1",
        "2606:4700:4700::1111",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",  # NAT64 → 8.8.8.8：还原后是公网
    ],
)
def test_public_addresses_are_accepted(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is True


def test_python_is_global_alone_is_not_enough() -> None:
    """固化 5.6 节的实测前提：这几个地址 is_global 为 True，只靠 is_global 会放行。"""
    for address in ("224.0.0.1", "64:ff9b::a9fe:a9fe", "::7f00:1"):
        assert ipaddress.ip_address(address).is_global is True
        assert is_public_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://mcp.example.com/mcp", "只支持 https"),
        ("ftp://mcp.example.com/mcp", "只支持 https"),
        ("https://user:pass@mcp.example.com/mcp", "用户名或密码"),
        ("https://token@mcp.example.com/mcp", "用户名或密码"),
        ("https://mcp.example.com/mcp#frag", "片段"),
        ("https://mcp.example.com/${TOKEN}/mcp", "占位符"),
        ("https://${HOST}/mcp", "占位符"),
        ("https://mcp.example.com/m\ncp", "控制字符"),
        ("https://mcp.exa mple.com/mcp", "空白"),
        ("https:///mcp", "主机名"),
    ],
)
def test_url_validation_rejects(url: str, message: str) -> None:
    with pytest.raises(McpAddressError, match=message):
        validate_server_url(url, allow_insecure_http=False)


def test_url_validation_accepts_and_normalizes() -> None:
    origin = validate_server_url("https://MCP.Example.com:8443/mcp?x=1", allow_insecure_http=False)

    assert (origin.scheme, origin.host, origin.port) == ("https", "mcp.example.com", 8443)
    insecure = validate_server_url("http://mcp.example.com/mcp", allow_insecure_http=True)
    assert insecure.port == 80


def test_parse_allowed_hosts() -> None:
    expected = frozenset({"a.internal", "b.internal"})
    assert parse_allowed_hosts(" a.internal, B.internal ,,") == expected
    assert parse_allowed_hosts("") == frozenset()


class FixedDns:
    """固定 DNS 结果；answers 是按调用次序依次返回的结果，最后一份一直沿用（模拟 rebinding）。"""

    def __init__(self, *answers: list[str]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        index = min(len(self.calls) - 1, len(self._answers) - 1)
        return self._answers[index]


_STRICT = AddressPolicy(allow_insecure_http=False)


async def test_resolve_rejects_any_private_record_among_many() -> None:
    """多条 A 记录里混有一条私网地址就整体拒绝，不挑公网地址凑合连。"""
    dns = FixedDns(["8.8.8.8", "10.0.0.5", "1.1.1.1"])

    with pytest.raises(McpAddressError, match="内网或保留地址"):
        await resolve_public("mcp.example.com", 443, policy=_STRICT, resolver=dns)


async def test_resolve_returns_first_public_record() -> None:
    dns = FixedDns(["8.8.8.8", "2606:4700:4700::1111"])

    assert await resolve_public("mcp.example.com", 443, policy=_STRICT, resolver=dns) == "8.8.8.8"


async def test_resolve_ip_literal_skips_dns() -> None:
    dns = FixedDns(["8.8.8.8"])

    with pytest.raises(McpAddressError):
        await resolve_public("[::ffff:127.0.0.1]", 443, policy=_STRICT, resolver=dns)
    assert await resolve_public("1.1.1.1", 443, policy=_STRICT, resolver=dns) == "1.1.1.1"
    assert dns.calls == []


async def test_resolve_failure_and_empty_answer() -> None:
    async def broken(host: str, port: int) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(McpAddressError, match="解析失败"):
        await resolve_public("nope.example.com", 443, policy=_STRICT, resolver=broken)
    with pytest.raises(McpAddressError, match="没有解析结果"):
        await resolve_public("empty.example.com", 443, policy=_STRICT, resolver=FixedDns([]))


def _server(*, owner: int | None = None, url: str = "https://mcp.internal/mcp") -> McpServerConfig:
    return McpServerConfig(
        server_id=1,
        workspace_id=1,
        owner_user_id=owner,
        server_key="internal",
        url=url,
    )


def _factory(dns: FixedDns, handler: httpx2.MockTransport, **kwargs: object) -> McpHttpFactory:
    values: dict[str, object] = {
        "allow_insecure_http": False,
        "allowed_private_hosts": frozenset({"mcp.internal"}),
        "connect_timeout": 5.0,
        "read_timeout": 5.0,
        "resolver": dns,
        "inner_transport": lambda: handler,
    }
    values.update(kwargs)
    return McpHttpFactory(**values)  # type: ignore[arg-type]


def _recording_transport() -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    return httpx2.MockTransport(handle), seen


async def test_allowlist_applies_to_shared_server_only() -> None:
    """内网白名单只对共享 server 生效：私有 server 命中白名单仍被拒绝。"""
    transport, _ = _recording_transport()
    factory = _factory(FixedDns(["10.1.2.3"]), transport)

    shared = await factory.check(_server())
    assert shared.address == "10.1.2.3"
    with pytest.raises(McpAddressError, match="内网或保留地址"):
        await factory.check(_server(owner=42))


async def test_client_connects_to_validated_ip_despite_rebinding() -> None:
    """rebinding：校验时解析到公网地址，之后 DNS 改成内网地址；连接仍固定到校验过的 IP，不再解析。"""
    dns = FixedDns(["93.184.216.34"], ["127.0.0.1"])
    transport, seen = _recording_transport()
    factory = _factory(dns, transport, allowed_private_hosts=frozenset())
    config = _server(url="https://mcp.example.com/mcp")

    pinned = await factory.check(config)
    async with factory.create(config, None, pinned) as client:
        first = await client.post("https://mcp.example.com/mcp", json={})
        second = await client.post("https://mcp.example.com/mcp", json={})

    assert dns.calls == [("mcp.example.com", 443)]
    assert [request.url.host for request in seen] == ["93.184.216.34", "93.184.216.34"]
    assert all(request.headers["host"] == "mcp.example.com" for request in seen)
    assert all(request.extensions["sni_hostname"] == "mcp.example.com" for request in seen)
    # 上层看到的仍是主机名，重定向判定与日志按主机名走。
    assert first.request.url.host == "mcp.example.com"
    assert second.status_code == 200


async def test_rebinding_on_other_host_is_rejected_at_connect() -> None:
    """另一主机首次连接时才解析；解析到内网地址时在传输层拒绝，请求不会发出。"""
    dns = FixedDns(["127.0.0.1"])
    transport, seen = _recording_transport()
    pinned_transport = PinnedTransport(transport, policy=_STRICT, resolver=dns)

    async with httpx2.AsyncClient(transport=pinned_transport) as client:
        with pytest.raises(httpx2.ConnectError, match="内网或保留地址"):
            await client.get("https://other.example.com/.well-known/oauth-protected-resource")

    assert seen == []


async def test_pinned_transport_rejects_plain_http() -> None:
    transport, seen = _recording_transport()
    pinned_transport = PinnedTransport(transport, policy=_STRICT, resolver=FixedDns(["8.8.8.8"]))

    async with httpx2.AsyncClient(transport=pinned_transport) as client:
        with pytest.raises(httpx2.UnsupportedProtocol):
            await client.get("http://mcp.example.com/mcp")
    assert seen == []


async def test_own_requests_do_not_follow_redirects() -> None:
    """适配器自己发出的请求不跟随重定向：跨源 3xx 原样交回调用方当作失败。"""

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "https://169.254.169.254/latest"})

    factory = _factory(
        FixedDns(["93.184.216.34"]),
        httpx2.MockTransport(handle),
        allowed_private_hosts=frozenset(),
    )
    config = _server(url="https://mcp.example.com/mcp")
    pinned = await factory.check(config)
    async with factory.create(config, None, pinned) as client:
        response = await client.get("https://mcp.example.com/mcp")

    assert response.status_code == 302
    assert response.next_request is not None


async def test_client_sends_only_configured_headers() -> None:
    transport, seen = _recording_transport()
    factory = _factory(FixedDns(["93.184.216.34"]), transport, allowed_private_hosts=frozenset())
    config = _server(url="https://mcp.example.com/mcp")
    secret = McpSecret.from_headers({"X-Api-Key": "secret-value"})

    async with factory.create(config, secret, await factory.check(config)) as client:
        await client.post("https://mcp.example.com/mcp", json={})

    assert seen[0].headers["x-api-key"] == "secret-value"
    assert "authorization" not in seen[0].headers


def test_sanitize_headers_rejects_crlf() -> None:
    with pytest.raises(Exception, match="换行"):
        sanitize_headers({"X-Api-Key": "a\nb"})
    assert sanitize_headers([("X-A", "1")]) == {"X-A": "1"}
