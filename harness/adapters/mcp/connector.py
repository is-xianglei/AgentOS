"""McpConnector：McpServerConnector 的实现，探测、连接失效与工具清单缓存清除。

探测不进连接池、不读写共享缓存：用一条临时连接建连并翻页拉完整工具清单（强制刷新），
整体受 probe_timeout 限制，结束即关闭。失败不抛异常，结果里带可展示的中文错误；
地址不合法（SSRF）属于调用方输入错误，抛中文 AgentException。
"""

import logging
from contextlib import AsyncExitStack

import anyio

from harness.adapters.mcp.http import McpAddressError
from harness.adapters.mcp.pool import McpConnectFailed, root_cause
from harness.adapters.mcp.provider import McpListError, list_all_tools, prepare_tool
from harness.adapters.mcp.runtime import McpRuntime
from harness.app.mcp_port import McpServerConnector
from harness.domain.mcp import (
    McpProbeResult,
    McpSecret,
    McpServerConfig,
    McpToolSummary,
    schema_digest,
    validate_server_key,
)

logger = logging.getLogger(__name__)


class McpConnector(McpServerConnector):
    """对远程 server 的一次性动作；连接池与缓存归 McpRuntime 管。"""

    def __init__(
        self,
        *,
        runtime: McpRuntime,
        probe_timeout: float,
        max_schema_bytes: int,
    ) -> None:
        self._runtime = runtime
        self._probe_timeout = probe_timeout
        self._max_schema_bytes = max_schema_bytes

    async def probe(self, config: McpServerConfig, secret: McpSecret | None) -> McpProbeResult:
        validate_server_key(config.server_key)
        # 先做地址校验：地址不合法直接抛给调用方，不当作 server 不可用。
        await self._runtime.http.check(config)
        context = {"mcp_server_id": config.server_id, "mcp_server_key": config.server_key}
        try:
            with anyio.fail_after(self._probe_timeout):
                async with AsyncExitStack() as stack:
                    client = await self._runtime.open_client(stack, config, secret, cache=None)
                    tools = await list_all_tools(client, refresh=True)
                    server_info = client.server_info
                    protocol_version = client.protocol_version
        except McpAddressError:
            raise
        except Exception as exc:  # noqa: BLE001  探测失败一律转成可展示的结果
            return self._failure(exc, context)
        summaries: list[McpToolSummary] = []
        for tool in tools:
            prepared = prepare_tool(tool, max_schema_bytes=self._max_schema_bytes)
            if isinstance(prepared, str):
                logger.warning("MCP 探测剔除工具：%s", prepared, extra=context)
                continue
            summaries.append(
                McpToolSummary(
                    name=prepared.remote_name,
                    description=prepared.description,
                    input_schema_sha256=schema_digest(prepared.parameters),
                    annotations=prepared.annotations,
                ),
            )
        return McpProbeResult.success(
            server_name=server_info.name if server_info is not None else None,
            server_version=server_info.version if server_info is not None else None,
            protocol_version=protocol_version,
            tools=summaries,
        )

    async def invalidate(self, server_id: int) -> None:
        self._runtime.invalidate(server_id)

    async def drop_cached_tools(self, server_id: int) -> None:
        await self._runtime.drop_cached_tools(server_id)

    @staticmethod
    def _failure(exc: Exception, context: dict[str, object]) -> McpProbeResult:
        cause = root_cause(exc)
        if isinstance(cause, McpListError):
            message = cause.message
        elif isinstance(cause, TimeoutError):
            message = "探测 MCP server 超时"
        elif isinstance(cause, McpConnectFailed):
            message = f"MCP server 连接失败：{cause.user_message}"
        else:
            message = "连接 MCP server 失败"
        logger.warning(
            "MCP 探测失败：%s，错误类型=%s",
            message,
            type(cause).__name__,
            extra=context,
        )
        return McpProbeResult.failure(message)
