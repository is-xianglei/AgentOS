"""MCP OAuth 适配器测试：ASGI 假授权服务器 + 带 PRM 与 Bearer 校验的假 MCP server，不联网、不监听端口。

覆盖：授权码 + PKCE（S256）+ resource（RFC 8707）+ iss（RFC 9207）；CIMD 与 DCR；state 一次性、过期、用户不一致；
回合解析（无 token 缺席并记 auth_required、提前刷新、刷新失败映射 auth_required、快照回写）；
调用中 401 先读库再刷新、刷新失败返回「需要重新授权」并标记失效；403 insufficient_scope；撤销后重新授权；
探测 OAuth server 不需要 token；token、code、code_verifier、client_secret 不进日志与错误。
"""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from core.errors import AgentException
from harness.adapters.mcp.connector import McpConnector
from harness.adapters.mcp.oauth import McpOAuthError
from harness.adapters.mcp.provider import McpProviderLimits, McpToolProvider
from harness.domain.mcp import McpServerEntry
from harness.domain.mcp_oauth import McpOAuthToken, OAuthCallback
from harness.domain.tool import ToolContext, ToolScope
from tests.fixtures.mcp_fake import FakeMcpServer, StaticServers, text_result
from tests.fixtures.mcp_oauth_fake import (
    ISSUER,
    RESOURCE,
    FakeAuthServer,
    OAuthHarness,
    oauth_config,
    oauth_harness,
)

pytestmark = pytest.mark.anyio

WORKSPACE_ID = 1
USER_ID = 9
SCOPE = ToolScope(workspace_id=WORKSPACE_ID, user_id=USER_ID)
_LIMITS = McpProviderLimits(
    list_timeout=5.0,
    call_timeout=5.0,
    max_output_chars=20000,
    max_schema_bytes=16384,
)


def _echo_server() -> FakeMcpServer:
    fake = FakeMcpServer()
    fake.add("lookup", lambda args: text_result(f"found {args.get('q', '')}"))
    return fake


async def _config_for(config: Any) -> Any:
    async def lookup(server_id: int) -> Any:
        assert server_id == config.server_id
        return config

    return lookup


async def _authorize(
    h: OAuthHarness,
    config: Any = None,
    *,
    user_id: int = USER_ID,
) -> McpOAuthToken:
    """跑完两个半程：发起 → 假授权服务器签发授权码 → 完成，返回换到的 token 并存进内存存储。"""
    config = config or oauth_config()
    url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=user_id)
    state, code, iss = h.auth.authorize(url)
    grant = await h.oauth.complete(
        OAuthCallback(state=state, code=code, iss=iss),
        workspace_id=WORKSPACE_ID,
        user_id=user_id,
        config_for=await _config_for(config),
    )
    h.store.tokens[(grant.server_id, user_id)] = grant.token
    return grant.token


def _provider(h: OAuthHarness, *entries: McpServerEntry) -> McpToolProvider:
    return McpToolProvider(runtime=h.runtime, servers=StaticServers(*entries), limits=_LIMITS)


def _entry(h: OAuthHarness, config: Any = None, *, user_id: int = USER_ID) -> McpServerEntry:
    config = config or oauth_config()
    return McpServerEntry(config, oauth_token=h.store.tokens.get((config.server_id, user_id)))


def _ctx(user_id: int = USER_ID) -> ToolContext:
    db: Any = None  # 调用路径不读数据库
    return ToolContext(session_id=1, db=db, user_id=user_id, workspace_id=WORKSPACE_ID)


# ---------- 授权的两个半程 ----------


async def test_authorization_code_flow_with_dcr_pkce_resource_and_iss() -> None:
    async with oauth_harness(_echo_server()) as h:
        config = oauth_config()
        url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}

        # 授权地址：授权端点 + PKCE S256 + resource + 回跳到前端路由；DCR 注册的 client_id。
        assert url.startswith(f"{ISSUER}/authorize?")
        assert query["code_challenge_method"] == "S256"
        assert query["resource"] == RESOURCE
        assert query["redirect_uri"] == "https://agentos.example.com/mcp/oauth/callback"
        assert query["client_id"].startswith("client-")
        assert query["scope"] == "tools.read"
        (registration,) = h.auth.registrations
        assert registration["application_type"] == "web"
        assert registration["redirect_uris"] == ["https://agentos.example.com/mcp/oauth/callback"]
        assert h.store.clients[1].issuer == ISSUER

        state, code, iss = h.auth.authorize(url)
        grant = await h.oauth.complete(
            OAuthCallback(state=state, code=code, iss=iss),
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            config_for=await _config_for(config),
        )

    exchange = h.auth.token_requests[-1]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["resource"] == RESOURCE
    assert len(exchange["code_verifier"]) == 128
    assert grant.server_id == 1 and grant.config_rev == 1
    assert grant.token.issuer == ISSUER
    assert grant.token.refresh_token is not None
    assert grant.token.expires_at == datetime.fromtimestamp(1_000_000 + 3600, tz=UTC)


async def test_cimd_preferred_when_authorization_server_supports_it() -> None:
    async with oauth_harness(_echo_server(), FakeAuthServer(cimd=True)) as h:
        await _authorize(h)

    assert h.auth.registrations == []
    client = h.store.clients[1]
    assert client.client_id == "https://agentos.example.com/api/mcp/oauth/client-metadata.json"
    assert h.auth.token_requests[-1]["client_id"] == client.client_id


async def test_dcr_used_when_cimd_unsupported_and_registration_reused() -> None:
    async with oauth_harness(_echo_server(), FakeAuthServer(cimd=False)) as h:
        await _authorize(h)
        await _authorize(h)

    # 同一授权服务器的注册复用，不重复注册。
    assert len(h.auth.registrations) == 1


async def test_state_is_single_use() -> None:
    async with oauth_harness(_echo_server()) as h:
        config = oauth_config()
        url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
        state, code, iss = h.auth.authorize(url)
        callback = OAuthCallback(state=state, code=code, iss=iss)
        await h.oauth.complete(
            callback,
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            config_for=await _config_for(config),
        )
        with pytest.raises(McpOAuthError, match="不存在或已过期"):
            await h.oauth.complete(
                callback,
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                config_for=await _config_for(config),
            )


async def test_state_expires_after_ten_minutes() -> None:
    async with oauth_harness(_echo_server()) as h:
        config = oauth_config()
        url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
        state, code, iss = h.auth.authorize(url)
        h.clock[0] += 601
        with pytest.raises(McpOAuthError, match="不存在或已过期"):
            await h.oauth.complete(
                OAuthCallback(state=state, code=code, iss=iss),
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                config_for=await _config_for(config),
            )
    # 没有换 token。
    assert all(req["grant_type"] != "authorization_code" for req in h.auth.token_requests)


@pytest.mark.parametrize(("workspace_id", "user_id"), [(WORKSPACE_ID, 10), (2, USER_ID)])
async def test_state_from_another_user_is_rejected_and_consumed(
    workspace_id: int,
    user_id: int,
) -> None:
    """把受害者发起的授权绑到攻击者账户上（GHSA-vf7j）：发起人与当前登录用户不一致就拒绝，state 也作废。"""
    async with oauth_harness(_echo_server()) as h:
        config = oauth_config()
        url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
        state, code, iss = h.auth.authorize(url)
        callback = OAuthCallback(state=state, code=code, iss=iss)
        with pytest.raises(AgentException) as exc_info:
            await h.oauth.complete(
                callback,
                workspace_id=workspace_id,
                user_id=user_id,
                config_for=await _config_for(config),
            )
        with pytest.raises(McpOAuthError, match="不存在或已过期"):
            await h.oauth.complete(
                callback,
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                config_for=await _config_for(config),
            )
    assert exc_info.value.status_code == 403
    assert all(req["grant_type"] != "authorization_code" for req in h.auth.token_requests)


async def test_iss_mismatch_and_missing_iss_are_rejected() -> None:
    async with oauth_harness(_echo_server()) as h:
        config = oauth_config()
        for iss in ("https://evil.example.com", None):
            url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
            state, code, _ = h.auth.authorize(url)
            with pytest.raises(McpOAuthError, match="iss"):
                await h.oauth.complete(
                    OAuthCallback(state=state, code=code, iss=iss),
                    workspace_id=WORKSPACE_ID,
                    user_id=USER_ID,
                    config_for=await _config_for(config),
                )
    assert all(req["grant_type"] != "authorization_code" for req in h.auth.token_requests)


async def test_missing_iss_allowed_when_server_does_not_advertise_it() -> None:
    async with oauth_harness(_echo_server(), FakeAuthServer(iss_supported=False)) as h:
        token = await _authorize(h)
    assert token.access_token in h.auth.access_tokens


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        (FakeAuthServer(pkce=False), "PKCE"),
        (FakeAuthServer(issuer_override="https://other.example.com"), "issuer"),
        (FakeAuthServer(dcr=False, cimd=False), "动态注册"),
    ],
)
async def test_discovery_rejections(auth: FakeAuthServer, message: str) -> None:
    async with oauth_harness(_echo_server(), auth) as h:
        with pytest.raises(McpOAuthError, match=message):
            await h.oauth.begin(oauth_config(), workspace_id=WORKSPACE_ID, user_id=USER_ID)


async def test_missing_prm_is_rejected() -> None:
    async with oauth_harness(_echo_server()) as h:
        h.protected.prm_enabled = False
        with pytest.raises(McpOAuthError, match="PRM"):
            await h.oauth.begin(oauth_config(), workspace_id=WORKSPACE_ID, user_id=USER_ID)


async def test_begin_requires_public_base_url() -> None:
    async with oauth_harness(_echo_server(), public_base_url=None) as h:
        with pytest.raises(AgentException, match="AGENTOS_PUBLIC_BASE_URL"):
            await h.oauth.begin(oauth_config(), workspace_id=WORKSPACE_ID, user_id=USER_ID)


async def test_config_change_during_authorization_is_rejected() -> None:
    async with oauth_harness(_echo_server()) as h:
        config = oauth_config()
        url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
        state, code, iss = h.auth.authorize(url)
        changed = replace(config, config_rev=2)
        with pytest.raises(AgentException, match="配置已被修改") as exc_info:
            await h.oauth.complete(
                OAuthCallback(state=state, code=code, iss=iss),
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                config_for=await _config_for(changed),
            )
    assert exc_info.value.status_code == 409


async def test_authorization_server_on_private_address_is_blocked() -> None:
    """授权服务器在另一个主机上，各自解析校验：它解析到内网时不发请求。"""
    async with oauth_harness(_echo_server()) as h:
        h.dns_overrides["auth.example.com"] = ["10.0.0.5"]
        with pytest.raises(McpOAuthError, match="无法连接"):
            await h.oauth.begin(oauth_config(), workspace_id=WORKSPACE_ID, user_id=USER_ID)
    assert h.auth.registrations == []


# ---------- 回合解析 ----------


async def test_server_without_token_is_absent_with_auth_required_fault() -> None:
    async with oauth_harness(_echo_server()) as h:
        listed = await _provider(h, _entry(h)).list_tools(SCOPE)

    assert listed.tools == ()
    (fault,) = listed.faults
    assert (fault.kind, fault.server_id, fault.source) == ("auth_required", 1, "mcp:fake")
    # fault 不带授权地址。
    assert "http" not in fault.message
    # 没有 token 时不发 MCP 请求。
    assert h.protected.bearers == []


async def test_authorized_user_gets_tools_and_snapshot_is_recorded() -> None:
    async with oauth_harness(_echo_server()) as h:
        token = await _authorize(h)
        # 授权前半程发现时有一次不带 token 的挑战请求，之后的 MCP 请求都带 token。
        h.protected.bearers.clear()
        listed = await _provider(h, _entry(h)).list_tools(SCOPE)
        again = await _provider(h, _entry(h)).list_tools(SCOPE)

    assert [tool.name for tool in listed.tools] == ["mcp__fake__lookup"]
    assert listed.faults == ()
    assert set(h.protected.bearers) == {token.access_token}
    assert [tool.name for tool in h.store.snapshots[1]] == ["lookup"]
    # 同一份清单不重复回写。
    assert again.faults == ()


async def test_other_user_without_token_is_absent_even_if_someone_authorized() -> None:
    async with oauth_harness(_echo_server()) as h:
        await _authorize(h, user_id=USER_ID)
        other = ToolScope(workspace_id=WORKSPACE_ID, user_id=10)
        listed = await _provider(h, _entry(h, user_id=10)).list_tools(other)
    assert listed.tools == ()
    assert [fault.kind for fault in listed.faults] == ["auth_required"]


async def test_expiring_token_is_refreshed_before_listing() -> None:
    async with oauth_harness(_echo_server(), refresh_skew_seconds=300, jitter=20.0) as h:
        token = await _authorize(h)
        h.protected.bearers.clear()
        # 离过期 310 秒：不到 300 + 抖动 20 秒，主动刷新。
        h.clock[0] += 3600 - 310
        listed = await _provider(h, _entry(h)).list_tools(SCOPE)

    assert h.auth.refresh_calls == 1
    refreshed = h.store.tokens[(1, USER_ID)]
    assert refreshed.access_token != token.access_token
    assert refreshed.refresh_token != token.refresh_token
    assert h.auth.token_requests[-1]["resource"] == RESOURCE
    assert [tool.name for tool in listed.tools] == ["mcp__fake__lookup"]
    # 刷新前重新发现（缓存已过期）会发一次不带 token 的挑战请求；MCP 调用都带新 token。
    assert {bearer for bearer in h.protected.bearers if bearer} == {refreshed.access_token}


async def test_token_outside_skew_window_is_not_refreshed() -> None:
    async with oauth_harness(_echo_server(), refresh_skew_seconds=300, jitter=20.0) as h:
        await _authorize(h)
        h.clock[0] += 3600 - 400
        await _provider(h, _entry(h)).list_tools(SCOPE)
    assert h.auth.refresh_calls == 0


async def test_refresh_failure_maps_to_auth_required_and_invalidates_token() -> None:
    async with oauth_harness(_echo_server(), FakeAuthServer(refresh_status=400)) as h:
        await _authorize(h)
        h.clock[0] += 3600 + 1
        listed = await _provider(h, _entry(h)).list_tools(SCOPE)

    assert listed.tools == ()
    assert [fault.kind for fault in listed.faults] == ["auth_required"]
    # 授权服务器明确拒绝：token 在锁内标记失效。
    assert (1, USER_ID) not in h.store.tokens
    assert h.store.invalidated == [(1, USER_ID)]


async def test_token_without_refresh_token_expired_is_auth_required() -> None:
    async with oauth_harness(_echo_server()) as h:
        token = await _authorize(h)
        h.store.tokens[(1, USER_ID)] = replace(token, refresh_token=None)
        h.clock[0] += 3600 + 1
        listed = await _provider(h, _entry(h)).list_tools(SCOPE)
    assert [fault.kind for fault in listed.faults] == ["auth_required"]
    assert h.auth.refresh_calls == 0


# ---------- 调用 ----------


async def _tool(h: OAuthHarness) -> Any:
    listed = await _provider(h, _entry(h)).list_tools(SCOPE)
    (tool,) = listed.tools
    return tool


async def test_call_401_rereads_store_and_uses_token_refreshed_elsewhere() -> None:
    """另一个实例已经刷新过：收到 401 后先重新读库，换用新 token 重发，自己不刷新。"""
    async with oauth_harness(_echo_server()) as h:
        old = await _authorize(h)
        tool = await _tool(h)
        # 模拟别的实例刷新：授权服务器吊销旧 token，库里换成新的一份。
        new = replace(old, access_token="at-from-other-instance")
        h.auth.access_tokens.discard(old.access_token)
        h.auth.access_tokens.add(new.access_token)
        h.store.tokens[(1, USER_ID)] = new
        output = await tool.execute({"q": "x"}, _ctx())

    assert output.is_error is False and output.text == "found x"
    assert h.auth.refresh_calls == 0
    assert h.protected.bearers[-2:] == [old.access_token, new.access_token]


async def test_call_401_refreshes_once_then_retries() -> None:
    async with oauth_harness(_echo_server()) as h:
        old = await _authorize(h)
        tool = await _tool(h)
        h.auth.access_tokens.discard(old.access_token)
        output = await tool.execute({"q": "y"}, _ctx())

    assert output.is_error is False and output.text == "found y"
    assert h.auth.refresh_calls == 1
    assert h.store.tokens[(1, USER_ID)].access_token in h.auth.access_tokens


async def test_call_401_with_failed_refresh_asks_for_reauthorization() -> None:
    async with oauth_harness(_echo_server(), FakeAuthServer(refresh_status=400)) as h:
        old = await _authorize(h)
        tool = await _tool(h)
        h.auth.revoke_all()
        output = await tool.execute({"q": "z"}, _ctx())
        listed = await _provider(h, _entry(h)).list_tools(SCOPE)

    assert output.is_error is True
    assert output.text == "需要重新授权 MCP server fake"
    assert old.access_token not in output.text
    assert (1, USER_ID) not in h.store.tokens
    # 下一回合缺席并提示授权。
    assert [fault.kind for fault in listed.faults] == ["auth_required"]


async def test_call_403_insufficient_scope_asks_for_reauthorization_without_invalidating() -> None:
    async with oauth_harness(_echo_server()) as h:
        await _authorize(h)
        tool = await _tool(h)
        h.protected.scope_required = True
        output = await tool.execute({"q": "z"}, _ctx())
    assert output.is_error is True
    assert output.text == "需要重新授权 MCP server fake"
    # scope 不够不代表 token 失效。
    assert (1, USER_ID) in h.store.tokens
    assert h.auth.refresh_calls == 0


async def test_concurrent_calls_in_one_process_refresh_once() -> None:
    """同一进程里多条请求同时撞上过期：TokenCell 串行，只刷新一次（跨实例的串行见 PG 测试）。"""
    import asyncio

    async with oauth_harness(_echo_server()) as h:
        await _authorize(h)
        tool = await _tool(h)
        h.clock[0] += 3600 + 1
        outputs = await asyncio.gather(*(tool.execute({"q": str(i)}, _ctx()) for i in range(5)))
    assert [output.is_error for output in outputs] == [False] * 5
    assert h.auth.refresh_calls == 1


# ---------- 撤销后重新授权、探测 ----------


def _connector(h: OAuthHarness) -> McpConnector:
    return McpConnector(runtime=h.runtime, probe_timeout=5, max_schema_bytes=16384)


async def test_revoke_then_reauthorize() -> None:
    async with oauth_harness(_echo_server()) as h:
        first = await _authorize(h)
        listed = await _provider(h, _entry(h)).list_tools(SCOPE)
        assert [tool.name for tool in listed.tools] == ["mcp__fake__lookup"]

        # 撤销：库里的 token 被软删（由 McpServerService 做），本进程的缓存与连接随之作废。
        del h.store.tokens[(1, USER_ID)]
        await _connector(h).forget_user(1, USER_ID)
        revoked = await _provider(h, _entry(h)).list_tools(SCOPE)

        second = await _authorize(h)
        restored = await _provider(h, _entry(h)).list_tools(SCOPE)

    assert [fault.kind for fault in revoked.faults] == ["auth_required"]
    assert second.access_token != first.access_token
    assert [tool.name for tool in restored.tools] == ["mcp__fake__lookup"]
    assert restored.faults == ()


async def test_probe_oauth_server_checks_metadata_without_token() -> None:
    async with oauth_harness(_echo_server()) as h:
        result = await _connector(h).probe(oauth_config(server_id=None), None)
        h.protected.prm_enabled = False
        failed = await _connector(h).probe(oauth_config(server_id=None), None)

    assert result.ok is True and result.tools_listed is False and result.tools == ()
    # 没有任何带 token 的请求，也没有注册客户端或换 token。
    assert all(bearer is None for bearer in h.protected.bearers)
    assert h.auth.registrations == [] and h.auth.token_requests == []
    assert failed.ok is False and "PRM" in (failed.error or "")


async def test_connector_snapshot_after_authorization() -> None:
    async with oauth_harness(_echo_server()) as h:
        token = await _authorize(h)
        written = await _connector(h).refresh_oauth_snapshot(oauth_config(), USER_ID, token)
        again = await _connector(h).refresh_oauth_snapshot(oauth_config(), USER_ID, token)
    assert written is True and again is False
    assert [tool.name for tool in h.store.snapshots[1]] == ["lookup"]


async def test_runtime_without_oauth_reports_invalid_for_oauth_server() -> None:
    """装配没有提供 OAuth 时不回落成无认证连接：该 server 缺席并记 invalid。"""
    from tests.fixtures.mcp_fake import mcp_harness

    async with mcp_harness(_echo_server()) as plain:
        provider = McpToolProvider(
            runtime=plain.runtime,
            servers=StaticServers(McpServerEntry(oauth_config())),
            limits=_LIMITS,
        )
        listed = await provider.list_tools(SCOPE)
    assert listed.tools == ()
    assert [fault.kind for fault in listed.faults] == ["invalid"]


# ---------- 凭证不进日志与错误 ----------


async def test_secrets_never_reach_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    errors: list[str] = []
    async with oauth_harness(_echo_server(), FakeAuthServer(refresh_status=400)) as h:
        config = oauth_config()
        url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
        state, code, iss = h.auth.authorize(url)
        # 用错的 iss 先失败一次（错误信息里不能有 code），再用对的完成。
        url2 = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
        state2, code2, _ = h.auth.authorize(url2)
        try:
            await h.oauth.complete(
                OAuthCallback(state=state2, code=code2, iss="https://evil.example.com"),
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                config_for=await _config_for(config),
            )
        except AgentException as exc:
            errors.append(f"{exc.message} {exc.details} {exc!r}")
        grant = await h.oauth.complete(
            OAuthCallback(state=state, code=code, iss=iss),
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            config_for=await _config_for(config),
        )
        h.store.tokens[(1, USER_ID)] = grant.token
        pending_verifiers = [req.get("code_verifier") for req in h.auth.token_requests]
        tool = await _tool(h)
        h.auth.revoke_all()
        output = await tool.execute({"q": "z"}, _ctx())
        errors.append(output.text)

    secrets_ = [
        grant.token.access_token,
        grant.token.refresh_token or "",
        code,
        code2,
        *(verifier for verifier in pending_verifiers if verifier),
    ]
    logged = caplog.text + "".join(repr(record.__dict__) for record in caplog.records)
    for secret in secrets_:
        assert secret and secret not in logged, "日志里出现了凭证"
        for text in errors:
            assert secret not in text, "错误信息里出现了凭证"
    assert repr(grant.token).count(grant.token.access_token) == 0


async def test_client_secret_from_dcr_uses_basic_auth_and_stays_out_of_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "dcr-client-secret-value"
    async with oauth_harness(_echo_server(), FakeAuthServer(client_secret=secret)) as h:
        config = oauth_config()
        url = await h.oauth.begin(config, workspace_id=WORKSPACE_ID, user_id=USER_ID)
        state, code, iss = h.auth.authorize(url)
        grant = await h.oauth.complete(
            OAuthCallback(state=state, code=code, iss=iss),
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            config_for=await _config_for(config),
        )
        h.store.tokens[(1, USER_ID)] = grant.token
        h.clock[0] += 3600 + 1
        await _provider(h, _entry(h)).list_tools(SCOPE)

    assert h.auth.refresh_calls == 1
    assert all(header and header.startswith("Basic ") for header in h.auth.basic_auth_headers)
    assert all("client_secret" not in req for req in h.auth.token_requests)
    assert secret not in url
    # 客户端注册信息里存着 secret（明文存储，7.1 节），但不进 repr 与日志。
    assert h.store.clients[1].info["client_secret"] == secret
    assert secret not in repr(h.store.clients[1])
    assert secret not in caplog.text
