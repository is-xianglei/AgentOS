"""OAuth state 存储：Redis 后端（真实 Redis，不可达时跳过）与进程内后端，接口与语义一致。

覆盖：GETDEL 一次性取出、TTL 过期、key 里不放 state 原文、组装根按 disable_redis_events 选后端；
以及经 McpOAuth 完整走一遍：state 存在 Redis 里，完成时一次性取出、第二次用同一个 state 被拒。
"""

import asyncio
from typing import Any

import pytest

from core.config import settings
from core.errors import AgentException
from harness import bootstrap
from harness.adapters.mcp.oauth import McpOAuthError
from harness.adapters.mcp.oauth_state import LocalOAuthStateStore, RedisOAuthStateStore
from harness.domain.mcp_oauth import McpOAuthPending, OAuthCallback
from tests.fixtures.mcp_oauth_fake import oauth_config, oauth_harness

pytestmark = pytest.mark.anyio


def _pending(**overrides: Any) -> McpOAuthPending:
    values: dict[str, Any] = {
        "workspace_id": 1,
        "user_id": 9,
        "server_id": 3,
        "config_rev": 2,
        "issuer": "https://auth.example.com",
        "as_metadata": {"issuer": "https://auth.example.com"},
        "resource": "https://mcp.example.com/mcp",
        "redirect_uri": "https://agentos.example.com/mcp/oauth/callback",
        "client_id": "client-1",
        "code_verifier": "v" * 128,
        "scope": "tools.read",
    }
    values.update(overrides)
    return McpOAuthPending(**values)


async def test_redis_state_is_single_use_and_expires(redis_test_clients) -> None:
    clients, prefix = redis_test_clients
    store = RedisOAuthStateStore(clients, key_prefix=prefix)

    await store.put("state-a", _pending(), ttl_seconds=600)
    raw_keys = [key async for key in clients.command.scan_iter(match=f"{prefix}:*")]
    ttl = await clients.command.ttl(store.key("state-a"))
    first = await store.take("state-a")
    second = await store.take("state-a")

    await store.put("state-b", _pending(), ttl_seconds=1)
    await asyncio.sleep(1.2)
    expired = await store.take("state-b")

    assert first == _pending() and second is None and expired is None
    assert 0 < ttl <= 600
    # key 里只有 state 的摘要，不放原文。
    assert raw_keys == [store.key("state-a")] and "state-a" not in raw_keys[0]


async def test_local_state_is_single_use_and_expires() -> None:
    clock = [100.0]
    store = LocalOAuthStateStore(clock=lambda: clock[0])
    await store.put("s", _pending(), ttl_seconds=600)
    assert await store.take("s") == _pending()
    assert await store.take("s") is None
    await store.put("t", _pending(), ttl_seconds=600)
    clock[0] += 601
    assert await store.take("t") is None


async def test_full_flow_through_redis_state(redis_test_clients) -> None:
    clients, prefix = redis_test_clients
    store = RedisOAuthStateStore(clients, key_prefix=prefix)
    async with oauth_harness(states=store) as h:
        config = oauth_config()
        url = await h.oauth.begin(config, workspace_id=1, user_id=9)
        state, code, iss = h.auth.authorize(url)

        async def config_for(server_id: int) -> Any:
            return config

        callback = OAuthCallback(state=state, code=code, iss=iss)
        grant = await h.oauth.complete(callback, workspace_id=1, user_id=9, config_for=config_for)
        with pytest.raises(McpOAuthError, match="不存在或已过期"):
            await h.oauth.complete(callback, workspace_id=1, user_id=9, config_for=config_for)
    assert grant.token.access_token in h.auth.access_tokens
    assert [key async for key in clients.command.scan_iter(match=f"{prefix}:*")] == []


def test_bootstrap_picks_backend_by_disable_redis_events(monkeypatch) -> None:
    bootstrap.mcp_oauth_state_store.cache_clear()
    try:
        monkeypatch.setattr(settings, "disable_redis_events", True)
        assert isinstance(bootstrap.mcp_oauth_state_store(), LocalOAuthStateStore)
        bootstrap.mcp_oauth_state_store.cache_clear()

        monkeypatch.setattr(settings, "disable_redis_events", False)
        monkeypatch.setattr(settings, "redis_url", None)
        with pytest.raises(AgentException, match="AGENTOS_REDIS_URL"):
            bootstrap.mcp_oauth_state_store()
    finally:
        bootstrap.mcp_oauth_state_store.cache_clear()
