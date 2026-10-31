"""pytest 全局夹具。

异步用例统一声明 `pytestmark = pytest.mark.anyio`,由 .venv 中 anyio 自带的
pytest 插件驱动(已注册 pytest11 入口点),不引入 pytest-asyncio。
Phase 0 的基线测试均为同步,本夹具为后续装配器等异步测试预置骨架。
"""

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """限定 anyio 只跑 asyncio 后端,与生产运行时一致。"""
    return "asyncio"


@pytest.fixture
async def redis_test_clients():
    """连接 .env 中的 Redis;key 统一加 agentos:test:{uuid} 前缀,用例结束删除。"""
    from uuid import uuid4

    from core.redis_client import build_redis_clients
    from core.run_events import STREAM_BLOCK_MS

    try:
        clients = build_redis_clients(block_ms=STREAM_BLOCK_MS)
        await clients.command.ping()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        pytest.skip(f"Redis 不可达(AGENTOS_REDIS_URL): {type(exc).__name__}")
    prefix = f"agentos:test:{uuid4().hex}"
    try:
        yield clients, prefix
    finally:
        keys = [key async for key in clients.command.scan_iter(match=f"{prefix}:*")]
        if keys:
            await clients.command.delete(*keys)
        await clients.aclose()


@pytest.fixture
def assert_replayable():
    """断言中立消息历史可直接作为下一轮请求:两家出站编码都不报错,tool_use 与结果一一配对。"""
    from llm.anthropic_client import AnthropicClient
    from llm.openai_client import OpenAIClient
    from llm.types import ToolUseBlock, parse_assistant_blocks

    def check(messages: list[dict]) -> None:
        pending: set[str] = set()
        for message in messages:
            if message["role"] == "assistant":
                assert not pending, f"tool_use 缺少对应结果: {pending}"
                assert all("type" not in block for block in message["content"])
                pending = {
                    block.id
                    for block in parse_assistant_blocks(message["content"])
                    if isinstance(block, ToolUseBlock)
                }
            elif message["role"] == "tool":
                assert message["content"]["tool_use_id"] in pending
                pending.discard(message["content"]["tool_use_id"])
            else:
                assert not pending, f"tool_use 缺少对应结果: {pending}"
        assert not pending, f"tool_use 缺少对应结果: {pending}"
        AnthropicClient._encode_messages(messages)
        OpenAIClient._encode_messages(messages)

    return check


@pytest.fixture(params=["local", "redis"])
def run_store_factory(request):
    """按后端参数化的事件流存储工厂:factory(maxlen) -> RunEventStore。"""
    from core.run_events import LocalRunEventStore, RedisRunEventStore

    if request.param == "local":
        return lambda maxlen=5000: LocalRunEventStore(maxlen=maxlen)
    clients, prefix = request.getfixturevalue("redis_test_clients")
    return lambda maxlen=5000: RedisRunEventStore(clients, maxlen=maxlen, key_prefix=prefix)
