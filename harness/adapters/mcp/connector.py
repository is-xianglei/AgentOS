"""McpConnector：McpServerConnector 的实现，探测、OAuth 的两个半程、连接失效与工具清单缓存清除。

探测不进连接池、不读写共享缓存：用一条临时连接建连并翻页拉完整工具清单（强制刷新），
整体受 probe_timeout 限制，结束即关闭。失败不抛异常，结果里带可展示的中文错误；
地址不合法（SSRF）属于调用方输入错误，抛中文 AgentException。
OAuth server 的探测只验证 PRM 与授权服务器元数据能被发现，不需要 token（8.4 节）；
两个半程与快照回写交给运行时持有的 McpOAuth。
"""

import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

import anyio

from harness.adapters.mcp.http import McpAddressError
from harness.adapters.mcp.oauth import McpOAuthError, McpTokenAuth, track_auth
from harness.adapters.mcp.pool import McpConnectFailed, root_cause
from harness.adapters.mcp.provider import (
    McpListError,
    list_all_tools,
    prepare_tool,
    summarize_tools,
)
from harness.adapters.mcp.runtime import McpRuntime
from harness.app.mcp_port import McpServerConnector
from harness.domain.mcp import (
    McpAuthType,
    McpProbeResult,
    McpSecret,
    McpServerConfig,
    validate_server_key,
)
from harness.domain.mcp_oauth import McpOAuthGrant, McpOAuthToken, OAuthCallback

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
        if config.auth_type is McpAuthType.OAUTH:
            return await self._probe_oauth(config, context)
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
        for tool in tools:
            reason = prepare_tool(tool, max_schema_bytes=self._max_schema_bytes)
            if isinstance(reason, str):
                logger.warning("MCP 探测剔除工具：%s", reason, extra=context)
        summaries = summarize_tools(tools, max_schema_bytes=self._max_schema_bytes)
        return McpProbeResult.success(
            server_name=server_info.name if server_info is not None else None,
            server_version=server_info.version if server_info is not None else None,
            protocol_version=protocol_version,
            tools=summaries,
        )

    async def _probe_oauth(
        self,
        config: McpServerConfig,
        context: dict[str, object],
    ) -> McpProbeResult:
        """OAuth server 的探测只验证元数据能被发现；失败只记与身份无关的错误（8.4 节）。"""
        try:
            with anyio.fail_after(self._probe_timeout):
                await self._runtime.oauth.check_metadata(config)
        except McpOAuthError as exc:
            logger.warning("MCP OAuth 探测失败：%s", exc.message, extra=context)
            return McpProbeResult.failure(exc.message)
        except TimeoutError:
            logger.warning("MCP OAuth 探测超时", extra=context)
            return McpProbeResult.failure("探测 MCP server 超时")
        return McpProbeResult.metadata_only()

    async def begin_oauth(self, config: McpServerConfig, *, workspace_id: int, user_id: int) -> str:
        with anyio.fail_after(self._probe_timeout):
            return await self._runtime.oauth.begin(
                config,
                workspace_id=workspace_id,
                user_id=user_id,
            )

    async def complete_oauth(
        self,
        callback: OAuthCallback,
        *,
        workspace_id: int,
        user_id: int,
        config_for: Callable[[int], Awaitable[McpServerConfig]],
    ) -> McpOAuthGrant:
        with anyio.fail_after(self._probe_timeout):
            return await self._runtime.oauth.complete(
                callback,
                workspace_id=workspace_id,
                user_id=user_id,
                config_for=config_for,
            )

    async def refresh_oauth_snapshot(
        self,
        config: McpServerConfig,
        user_id: int,
        token: McpOAuthToken,
    ) -> bool:
        """授权成功后用新 token 建一条临时连接拉清单并回写快照；任何失败只记日志、返回 False。"""
        server_id = config.require_id()
        context = {"mcp_server_id": server_id, "mcp_server_key": config.server_key}
        oauth = self._runtime.oauth
        cell = oauth.cell(config, user_id)
        cell.seed(token)
        try:
            with anyio.fail_after(self._probe_timeout), track_auth() as outcome:
                async with AsyncExitStack() as stack:
                    client = await self._runtime.open_client(
                        stack,
                        config,
                        None,
                        cache=None,
                        auth=McpTokenAuth(cell),
                    )
                    tools = await list_all_tools(client, refresh=True)
            if outcome.auth_required:
                return False
        except Exception as exc:  # noqa: BLE001  快照回写失败不影响授权本身
            logger.warning(
                "授权后拉取工具清单失败，未回写快照，错误类型=%s",
                type(root_cause(exc)).__name__,
                extra=context,
            )
            return False
        summaries = summarize_tools(tools, max_schema_bytes=self._max_schema_bytes)
        return await oauth.record_snapshot(config, tuple(summaries))

    async def forget_user(self, server_id: int, user_id: int) -> None:
        self._runtime.forget_oauth_user(server_id, user_id)

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
