"""MCP 适配层：经 Streamable HTTP 连进程内假 server，覆盖列工具、调用、结果转换、缓存与故障。

假 server 经 httpx2.ASGITransport 挂在 SSRF 固定 IP 传输层下面，不访问外网、不监听端口。
"""

import asyncio
from typing import Any

import anyio
import mcp_types as t
import pytest

from harness.adapters.mcp.connector import McpConnector
from harness.adapters.mcp.http import McpAddressError
from harness.adapters.mcp.provider import (
    MAX_DESCRIPTION_CHARS,
    McpProviderLimits,
    McpToolProvider,
)
from harness.adapters.mcp.results import result_text, to_tool_output, truncate_middle
from harness.adapters.mcp.tool import McpTool
from harness.domain.mcp import ApprovalMode, McpSecret, McpServerEntry
from harness.domain.tool import ToolContext, ToolScope
from tests.fixtures.mcp_fake import (
    FAKE_IP,
    FakeMcpServer,
    McpHarness,
    StaticServers,
    make_config,
    mcp_harness,
    text_result,
)

pytestmark = pytest.mark.anyio

SCOPE = ToolScope(workspace_id=1, user_id=9)


def _limits(**overrides: Any) -> McpProviderLimits:
    values: dict[str, Any] = {
        "list_timeout": 3.0,
        "call_timeout": 3.0,
        "max_output_chars": 20000,
        "max_schema_bytes": 16384,
    }
    values.update(overrides)
    return McpProviderLimits(**values)


def _provider(h: McpHarness, *entries: McpServerEntry, **limits: Any) -> McpToolProvider:
    servers = StaticServers(*(entries or (McpServerEntry(make_config()),)))
    return McpToolProvider(runtime=h.runtime, servers=servers, limits=_limits(**limits))


def _ctx(*, workspace_id: int = 1, user_id: int = 9) -> ToolContext:
    # MCP 工具不碰数据库会话，db 用 None 占位。
    return ToolContext(
        session_id=1,
        db=None,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        user_id=user_id,
    )


def _tool(listed: Any, name: str) -> McpTool:
    for tool in listed.tools:
        if tool.name == name:
            assert isinstance(tool, McpTool)
            return tool
    raise AssertionError(f"没有工具 {name}: {[tool.name for tool in listed.tools]}")


async def test_lists_tools_across_pages_and_builds_mcp_tools() -> None:
    fake = FakeMcpServer(page_size=2)
    for name in ("search", "a.b", "create_issue", "delete_repo", "zeta"):
        fake.add(name)
    fake.add(
        "read_file",
        annotations=t.ToolAnnotations(read_only_hint=True),
        schema={"type": "object"},
    )
    async with mcp_harness(fake) as h:
        config = make_config(approval_mode=ApprovalMode.TRUST_ANNOTATIONS)
        listed = await _provider(h, McpServerEntry(config)).list_tools(SCOPE)

    names = [tool.name for tool in listed.tools]
    assert names == sorted(names)
    assert "mcp__fake__search" in names
    assert any(name.startswith("mcp__fake__a_b_") for name in names)
    assert listed.faults == ()
    read_file = _tool(listed, "mcp__fake__read_file")
    assert read_file.source == "mcp:fake"
    assert read_file.parameters == {"type": "object", "properties": {}}
    assert read_file.policy.requires_approval is False
    assert _tool(listed, "mcp__fake__search").policy.requires_approval is True
    assert read_file.ref.remote_name == "read_file"
    assert read_file.ref.config_rev == 1
    assert h.dns.calls == ["mcp.example.com"]
    assert all(request["host"] == "mcp.example.com" for request in h.gate.requests)


async def test_allowlist_and_denylist_filter_by_remote_name() -> None:
    fake = FakeMcpServer()
    for name in ("a", "b", "c"):
        fake.add(name)
    async with mcp_harness(fake) as h:
        config = make_config(tool_allowlist=("a", "b"), tool_denylist=("b",))
        listed = await _provider(h, McpServerEntry(config)).list_tools(SCOPE)

    assert [tool.name for tool in listed.tools] == ["mcp__fake__a"]


async def test_schema_preprocessing_drops_and_truncates() -> None:
    """schema 超限剔除、description 截断、非法 x-mcp-header 剔除，被剔除的各记一条 invalid fault。"""
    fake = FakeMcpServer()
    fake.add("big", schema={"type": "object", "properties": {"x": {"description": "x" * 400}}})
    fake.add("long_desc", description="说" * (MAX_DESCRIPTION_CHARS + 50))
    fake.add(
        "bad_header",
        schema={
            "type": "object",
            "properties": {"q": {"type": "object", "x-mcp-header": "Q"}},
        },
    )
    fake.add("ok")
    async with mcp_harness(fake) as h:
        listed = await _provider(h, max_schema_bytes=200).list_tools(SCOPE)

    assert [tool.name for tool in listed.tools] == ["mcp__fake__long_desc", "mcp__fake__ok"]
    assert len(_tool(listed, "mcp__fake__long_desc").description) == MAX_DESCRIPTION_CHARS
    kinds = {(fault.kind, fault.server_id) for fault in listed.faults}
    assert kinds == {("invalid", 1)}
    messages = " ".join(fault.message for fault in listed.faults)
    assert "big" in messages
    # 非法 x-mcp-header 的工具：2026-07-28 连接上由 SDK 直接剔除（只记日志），不会走到这里。
    assert "bad_header" not in [tool.name for tool in listed.tools]


async def test_colliding_model_names_are_dropped_with_fault() -> None:
    fake = FakeMcpServer()
    fake.add("a.b")
    fake.add("a_b_69f6642c")
    fake.add("ok")
    async with mcp_harness(fake) as h:
        listed = await _provider(h).list_tools(SCOPE)

    assert [tool.name for tool in listed.tools] == ["mcp__fake__ok"]
    assert sorted(fault.kind for fault in listed.faults) == ["invalid", "invalid"]


async def test_duplicate_cursor_and_page_limit_are_invalid_faults() -> None:
    fake = FakeMcpServer(page_size=1, cursor_override="same")
    fake.add("a")
    fake.add("b")
    async with mcp_harness(fake) as h:
        listed = await _provider(h).list_tools(SCOPE)

    assert listed.tools == ()
    assert [(fault.kind, fault.source) for fault in listed.faults] == [("invalid", "mcp:fake")]
    assert "游标重复" in listed.faults[0].message


async def test_call_success_error_structured_and_image() -> None:
    fake = FakeMcpServer()
    fake.add("echo", lambda args: text_result(f"你好 {args['q']}"))
    fake.add("fail", lambda args: text_result("参数不对", is_error=True))
    fake.add(
        "data",
        lambda args: t.CallToolResult(content=[], structured_content={"n": 1, "名": "值"}),
    )
    fake.add(
        "pic",
        lambda args: t.CallToolResult(
            content=[
                t.TextContent(text="见图"),
                t.ImageContent(data="aGVsbG8=", mime_type="image/png"),
            ],
        ),
    )
    async with mcp_harness(fake) as h:
        listed = await _provider(h).list_tools(SCOPE)
        echo = await _tool(listed, "mcp__fake__echo").execute({"q": "世界"}, _ctx())
        fail = await _tool(listed, "mcp__fake__fail").execute({}, _ctx())
        data = await _tool(listed, "mcp__fake__data").execute({}, _ctx())
        pic = await _tool(listed, "mcp__fake__pic").execute({}, _ctx())

    assert (echo.text, echo.is_error) == ("你好 世界", False)
    assert (fail.text, fail.is_error) == ("参数不对", True)
    assert data.text == '{"n": 1, "名": "值"}'
    assert pic.text == "见图\n\n[图片 image/png，约 5 B，未发送给模型]"
    assert ("echo", {"q": "世界"}) in fake.calls


async def test_normalize_arguments_validates_with_json_schema() -> None:
    fake = FakeMcpServer()
    fake.add(
        "typed",
        schema={
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1}},
            "required": ["n"],
            "additionalProperties": False,
        },
    )
    async with mcp_harness(fake) as h:
        tool = _tool(await _provider(h).list_tools(SCOPE), "mcp__fake__typed")

    assert tool.normalize_arguments({"n": 2}) == {"n": 2}
    for bad in ({}, {"n": 0}, {"n": "2"}, {"n": 2, "x": 1}):
        with pytest.raises(Exception, match="工具参数校验失败") as info:
            tool.normalize_arguments(bad)
        assert info.value.details["tool_name"] == "mcp__fake__typed"


async def test_call_timeout_is_unknown_result_and_not_retried() -> None:
    """调用超时判为结果未知、不重试；SDK 关闭响应流，server 端的处理随之取消。"""
    fake = FakeMcpServer()

    async def slow(args: dict[str, Any]) -> t.CallToolResult:
        await anyio.sleep(10)
        return text_result("late")

    fake.add("slow", slow)
    async with mcp_harness(fake) as h:
        tool = _tool(await _provider(h, call_timeout=0.2).list_tools(SCOPE), "mcp__fake__slow")
        output = await tool.execute({}, _ctx())
        for _ in range(50):
            if fake.cancelled:
                break
            await asyncio.sleep(0.02)

    assert output.is_error is True
    assert output.text == "MCP 工具 mcp__fake__slow 调用超时（0.2 秒），结果未知"
    assert [name for name, _ in fake.calls] == ["slow"]
    assert fake.cancelled == ["slow"]


async def test_caller_cancellation_propagates_and_connection_survives() -> None:
    fake = FakeMcpServer()
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> t.CallToolResult:
        started.set()
        await anyio.sleep(10)
        return text_result("late")

    fake.add("slow", slow)
    fake.add("quick")
    async with mcp_harness(fake) as h:
        listed = await _provider(h).list_tools(SCOPE)
        call = asyncio.create_task(_tool(listed, "mcp__fake__slow").execute({}, _ctx()))
        await asyncio.wait_for(started.wait(), 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        after = await _tool(listed, "mcp__fake__quick").execute({}, _ctx())

    assert (after.text, after.is_error) == ("quick ok", False)
    assert h.runtime.pool.size == 0


async def test_disconnect_during_call_is_connection_failure() -> None:
    fake = FakeMcpServer()
    fake.add("echo")
    async with mcp_harness(fake) as h:
        tool = _tool(await _provider(h).list_tools(SCOPE), "mcp__fake__echo")
        h.gate.disconnect = True
        output = await tool.execute({}, _ctx())

    assert output.is_error is True
    assert output.text == "MCP server fake 连接失败"
    assert fake.calls == []


async def test_http_error_status_is_protocol_error() -> None:
    fake = FakeMcpServer()
    fake.add("echo")
    async with mcp_harness(fake) as h:
        tool = _tool(await _provider(h).list_tools(SCOPE), "mcp__fake__echo")
        h.gate.status = 500
        output = await tool.execute({}, _ctx())

    assert output.is_error is True
    assert output.text.startswith("MCP 协议错误 -32603")


async def test_connect_failure_retries_once_then_reports() -> None:
    fake = FakeMcpServer()
    fake.add("echo")
    async with mcp_harness(fake) as h:
        tool = _tool(await _provider(h).list_tools(SCOPE), "mcp__fake__echo")
        h.runtime.invalidate(1)
        h.gate.disconnect = True
        requests_before = len(h.gate.requests)
        output = await tool.execute({}, _ctx())

    assert output.is_error is True
    assert output.text.startswith("MCP server fake 连接失败")
    # 建连阶段（server/discover 探测）失败两次：首次加重试 1 次，tools/call 从未发出。
    assert len(h.gate.requests) - requests_before == 2
    assert fake.calls == []


async def test_call_uses_turn_identity_for_partition() -> None:
    """调用时按回合身份重新算授权分区：别人用私有 server 的工具对象会被拒绝，不发请求。"""
    fake = FakeMcpServer()
    fake.add("echo")
    private = make_config(owner_user_id=9)
    async with mcp_harness(fake) as h:
        listed = await _provider(h, McpServerEntry(private)).list_tools(SCOPE)
        tool = _tool(listed, "mcp__fake__echo")
        stranger = await tool.execute({}, _ctx(user_id=10))
        owner = await tool.execute({}, _ctx(user_id=9))

    assert stranger.is_error is True
    assert "他人的私有" in stranger.text
    assert owner.is_error is False
    assert [name for name, _ in fake.calls] == ["echo"]


async def test_input_required_is_reported_as_unsupported() -> None:
    fake = FakeMcpServer()
    fake.add(
        "ask",
        lambda args: t.InputRequiredResult(
            input_requests={
                "k": t.ElicitRequest(
                    params=t.ElicitRequestFormParams(
                        message="密码？",
                        requested_schema={"type": "object", "properties": {}},
                    ),
                ),
            },
            request_state="s",
        ),
    )
    async with mcp_harness(fake) as h:
        output = await _tool(await _provider(h).list_tools(SCOPE), "mcp__fake__ask").execute(
            {},
            _ctx(),
        )

    assert (output.text, output.is_error) == ("该工具需要交互输入，暂不支持", True)


async def test_tool_list_cache_is_partitioned() -> None:
    """同一身份第二次解析命中缓存不发请求；不同授权分区（共享 vs 私有）各自拉取，不串数据。"""
    fake = FakeMcpServer(ttl_ms=60_000)
    fake.add("echo")
    shared = McpServerEntry(make_config(server_id=1, server_key="shared"))
    private = McpServerEntry(make_config(server_id=2, server_key="mine", owner_user_id=9))
    async with mcp_harness(fake) as h:
        provider = _provider(h, shared, private)
        await provider.list_tools(SCOPE)
        assert fake.list_calls == 2
        await provider.list_tools(SCOPE)
        assert fake.list_calls == 2

        # 另一个成员：共享 server 的分区相同（ws:1）命中缓存，看不到别人的私有 server。
        other = await provider.list_tools(ToolScope(workspace_id=1, user_id=10))
        assert fake.list_calls == 2
        assert [tool.name for tool in other.tools] == ["mcp__shared__echo"]

        # 另一个工作区的同一个 server_id：分区不同（ws:2），必须重新拉取。
        foreign = McpServerEntry(make_config(server_id=1, server_key="shared", workspace_id=2))
        await _provider(h, foreign).list_tools(ToolScope(workspace_id=2, user_id=10))
        assert fake.list_calls == 3


async def test_server_ttl_is_capped_and_config_rev_changes_key() -> None:
    fake = FakeMcpServer(ttl_ms=24 * 60 * 60 * 1000)
    fake.add("echo")
    async with mcp_harness(fake, tools_cache_seconds=60) as h:
        provider = _provider(h)
        await provider.list_tools(SCOPE)
        h.clock[0] += 59
        await provider.list_tools(SCOPE)
        assert fake.list_calls == 1
        h.clock[0] += 2  # server 给了 24 小时，被截断到 60 秒
        await provider.list_tools(SCOPE)
        assert fake.list_calls == 2

        bumped = McpServerEntry(make_config(config_rev=2))
        await _provider(h, bumped).list_tools(SCOPE)
        assert fake.list_calls == 3


async def test_refresh_failure_keeps_last_good_catalog() -> None:
    fake = FakeMcpServer(ttl_ms=1000)
    fake.add("echo")
    async with mcp_harness(fake) as h:
        provider = _provider(h)
        first = await provider.list_tools(SCOPE)
        h.clock[0] += 5
        fake.list_error = True
        stale = await provider.list_tools(SCOPE)

        never = McpServerEntry(make_config(server_id=2, server_key="never"))
        missing = await _provider(h, never).list_tools(SCOPE)

    assert [tool.name for tool in stale.tools] == [tool.name for tool in first.tools]
    assert [(fault.kind, fault.server_id) for fault in stale.faults] == [("unavailable", 1)]
    assert "沿用上次" in stale.faults[0].message
    assert missing.tools == ()
    assert [fault.kind for fault in missing.faults] == ["unavailable"]


async def test_slow_server_times_out_alone() -> None:
    """一个 server 建连卡住只让它自己超时，其他 server 照常返回。"""
    fake = FakeMcpServer()
    fake.add("echo")
    async with mcp_harness(fake) as h:
        good = McpServerEntry(make_config(server_id=1, server_key="good"))
        hang = McpServerEntry(
            make_config(server_id=2, server_key="hang", url="https://hang.example.com/mcp"),
        )
        original = h.runtime.open_client

        async def open_client(stack: Any, config: Any, secret: Any, *, cache: Any) -> Any:
            if config.server_key == "hang":
                await anyio.sleep(10)
            return await original(stack, config, secret, cache=cache)

        h.runtime.open_client = open_client  # type: ignore[method-assign]
        listed = await _provider(h, good, hang, list_timeout=0.3).list_tools(SCOPE)

    assert [tool.name for tool in listed.tools] == ["mcp__good__echo"]
    assert [(fault.kind, fault.source) for fault in listed.faults] == [("timeout", "mcp:hang")]


async def test_static_headers_reach_server_and_ssrf_blocks_private_dns() -> None:
    fake = FakeMcpServer()
    fake.add("echo")
    secret = McpSecret.from_headers({"Authorization": "Bearer abc"})
    async with mcp_harness(fake) as h:
        await _provider(h, McpServerEntry(make_config(), secret)).list_tools(SCOPE)
        assert h.gate.requests[0]["authorization"] == "Bearer abc"

        h.dns.addresses = ["10.0.0.8"]
        internal = McpServerEntry(make_config(server_id=3, server_key="internal"))
        blocked = await _provider(h, internal).list_tools(SCOPE)

    assert blocked.tools == ()
    assert blocked.faults[0].kind == "unavailable"
    assert "内网或保留地址" in blocked.faults[0].message


async def test_pool_capacity_and_idle_reaping_through_runtime() -> None:
    fake = FakeMcpServer()
    fake.add("echo")
    entries = [McpServerEntry(make_config(server_id=i, server_key=f"s{i}")) for i in (1, 2, 3)]
    async with mcp_harness(fake, pool_max_connections=2, idle_seconds=0.05) as h:
        for entry in entries:
            await _provider(h, entry).list_tools(SCOPE)
            assert h.runtime.pool.size <= 2
        for _ in range(100):
            await asyncio.sleep(0.02)
            if h.runtime.pool.size == 0:
                break
        assert h.runtime.pool.size == 0


async def test_output_schema_mismatch_is_reported() -> None:
    fake = FakeMcpServer()
    fake.add(
        "typed_out",
        lambda args: t.CallToolResult(content=[], structured_content={"n": "不是数字"}),
        output_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    )
    async with mcp_harness(fake) as h:
        tool = _tool(await _provider(h).list_tools(SCOPE), "mcp__fake__typed_out")
        output = await tool.execute({}, _ctx())

    assert (output.text, output.is_error) == ("MCP 工具返回的结构不符合其声明的 outputSchema", True)


async def test_probe_lists_full_catalog_without_touching_pool() -> None:
    fake = FakeMcpServer(page_size=1)
    fake.add("b", annotations=t.ToolAnnotations(read_only_hint=True))
    fake.add("a")
    async with mcp_harness(fake) as h:
        connector = McpConnector(runtime=h.runtime, probe_timeout=5, max_schema_bytes=16384)
        result = await connector.probe(make_config(server_id=None), None)
        pool_size = h.runtime.pool.size

    assert result.ok is True
    assert (result.server_name, result.server_version) == ("fake-mcp", "1.0")
    assert result.protocol_version == "2026-07-28"
    assert [tool.name for tool in result.tools] == ["a", "b"]
    assert result.tools[1].annotations.read_only is True
    assert len(result.tools[0].input_schema_sha256) == 64
    assert pool_size == 0


async def test_probe_failures() -> None:
    fake = FakeMcpServer()
    fake.add("a")
    async with mcp_harness(fake) as h:
        connector = McpConnector(runtime=h.runtime, probe_timeout=5, max_schema_bytes=16384)
        with pytest.raises(McpAddressError, match="只支持 https"):
            await connector.probe(make_config(url="http://mcp.example.com/mcp"), None)
        h.dns.addresses = ["169.254.169.254"]
        with pytest.raises(McpAddressError, match="内网或保留地址"):
            await connector.probe(make_config(), None)
        h.dns.addresses = [FAKE_IP]

        fake.list_error = True
        failed = await connector.probe(make_config(), None)
        fake.list_error = False

        h.gate.disconnect = True
        unreachable = await connector.probe(make_config(), None)

    assert (failed.ok, failed.tools) == (False, ())
    assert failed.error == "连接 MCP server 失败"
    assert unreachable.ok is False
    assert unreachable.error is not None and "连接" in unreachable.error


async def test_probe_timeout() -> None:
    fake = FakeMcpServer()

    async def slow_list(ctx: Any, params: Any) -> t.ListToolsResult:
        await anyio.sleep(10)
        return t.ListToolsResult(tools=[])

    fake._list_tools = slow_list  # type: ignore[method-assign]
    async with mcp_harness(fake) as h:
        connector = McpConnector(runtime=h.runtime, probe_timeout=0.3, max_schema_bytes=16384)
        result = await connector.probe(make_config(), None)

    assert (result.ok, result.error) == (False, "探测 MCP server 超时")


async def test_invalidate_drops_connections_and_stale_catalog() -> None:
    fake = FakeMcpServer(ttl_ms=60_000)
    fake.add("echo")
    async with mcp_harness(fake) as h:
        provider = _provider(h)
        await provider.list_tools(SCOPE)
        assert h.runtime.pool.size == 1
        connector = McpConnector(runtime=h.runtime, probe_timeout=5, max_schema_bytes=16384)
        await connector.invalidate(1)
        await asyncio.sleep(0.05)
        assert h.runtime.pool.size == 0

        fake.list_error = True
        h.clock[0] += 120
        after = await provider.list_tools(SCOPE)

    # 失效后不再沿用旧清单：配置已变，旧清单不可信。
    assert after.tools == ()


def test_result_conversion_table() -> None:
    result = t.CallToolResult(
        content=[
            t.TextContent(text="正文"),
            t.ImageContent(data="A" * 4000, mime_type="image/jpeg"),
            t.AudioContent(data="AAAA", mime_type="audio/wav"),
            t.ResourceLink(name="readme", uri="file:///readme.md"),
            t.EmbeddedResource(resource=t.TextResourceContents(uri="mem://a", text="内嵌文本")),
            t.EmbeddedResource(
                resource=t.BlobResourceContents(
                    uri="mem://b",
                    blob="AAAA",
                    mime_type="application/pdf",
                ),
            ),
        ],
        structured_content={"ignored": True},
    )

    assert result_text(result).split("\n\n") == [
        "正文",
        "[图片 image/jpeg，约 2 KB，未发送给模型]",
        "[音频 audio/wav，约 3 B，未发送给模型]",
        "[资源链接 readme: file:///readme.md]",
        "内嵌文本",
        "[资源 mem://b application/pdf，约 3 B，未发送给模型]",
    ]


def test_structured_content_only_when_no_text() -> None:
    only_structured = t.CallToolResult(content=[], structured_content=[1, "二"])
    image_and_structured = t.CallToolResult(
        content=[t.ImageContent(data="", mime_type="image/png")],
        structured_content={"k": "值"},
    )

    assert result_text(only_structured) == '[1, "二"]'
    assert result_text(image_and_structured).endswith('{"k": "值"}')


def test_truncation_keeps_head_and_tail() -> None:
    text = "头" * 50 + "中" * 1000 + "尾" * 50
    out = truncate_middle(text, 300)

    assert len(out) <= 300
    assert out.startswith("头" * 50)
    assert out.endswith("尾" * 50)
    assert "已截断" in out
    assert truncate_middle("短文本", 300) == "短文本"
    output = to_tool_output(text_result("x" * 100, is_error=True), max_chars=60)
    assert output.is_error is True
    assert len(output.text) <= 60
