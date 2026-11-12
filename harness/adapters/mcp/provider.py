"""McpToolProvider：ScopedToolProvider 的 MCP 实现，按身份并发列出各 server 的工具。

每个 server 用 list_timeout 单独限时，一个慢 server 不拖垮其他 server；失败只记一条 fault。
工具清单走 SDK 按授权分区隔离的响应缓存；缓存过期后重新拉取失败时，沿用进程内最近一次成功的清单，
同时记一条 unavailable fault。命名、过滤、审批推导都在每回合现算，不进缓存。
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from mcp import Client
from mcp.shared.inbound import find_invalid_x_mcp_header
from mcp_types import CallToolResult, ListToolsResult
from mcp_types import Tool as SdkTool

from core.errors import AgentException
from harness.adapters.mcp.pool import McpConnectFailed, PoolKey, root_cause
from harness.adapters.mcp.runtime import McpRuntime
from harness.adapters.mcp.tool import McpTool
from harness.app.mcp_port import McpServerSource
from harness.app.ports import ScopedToolProvider, ScopedTools, ToolSourceFault
from harness.domain.mcp import (
    McpServerConfig,
    McpServerEntry,
    McpToolAnnotations,
    McpToolRef,
    auth_partition,
    mcp_tool_policy,
    model_tool_names,
    requires_approval,
    schema_digest,
)
from harness.domain.tool import ToolContext, ToolScope

logger = logging.getLogger(__name__)

MAX_LIST_PAGES = 50
MAX_LIST_TOOLS = 1000
MAX_DESCRIPTION_CHARS = 2000

FaultKind = Literal["unavailable", "auth_required", "timeout", "invalid"]


@dataclass(frozen=True)
class McpProviderLimits:
    """McpToolProvider 用到的超时与上限，由组装根从 Settings 读好后传入。"""

    list_timeout: float
    call_timeout: float
    max_output_chars: int
    max_schema_bytes: int


class McpListError(Exception):
    """列工具失败，带 fault 的种类和可展示的中文说明。"""

    def __init__(self, kind: FaultKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


async def list_all_tools(client: Client, *, refresh: bool = False) -> list[SdkTool]:
    """沿 cursor 翻页拉完整工具清单：最多 50 页、1000 个工具，遇到重复 cursor 判为 invalid。

    第一页走 SDK 的响应缓存（refresh=True 时强制刷新）；续页一律直连 server。
    不用 SDK 的 ClientSessionGroup，它只取第一页。
    """
    tools: list[SdkTool] = []
    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(MAX_LIST_PAGES):
        page: ListToolsResult = await client.list_tools(
            cursor=cursor,
            cache_mode="refresh" if refresh else "use",
        )
        tools.extend(page.tools)
        if len(tools) > MAX_LIST_TOOLS:
            raise McpListError("invalid", f"MCP server 的工具超过 {MAX_LIST_TOOLS} 个")
        cursor = page.next_cursor
        if cursor is None:
            return tools
        if cursor in seen:
            raise McpListError("invalid", "MCP server 的工具清单分页游标重复")
        seen.add(cursor)
    raise McpListError("invalid", f"MCP server 的工具清单超过 {MAX_LIST_PAGES} 页")


@dataclass(frozen=True)
class PreparedTool:
    """预处理后可用的工具：原名、描述（已截断）、参数 schema（已补 properties）与注解。"""

    remote_name: str
    description: str
    parameters: dict[str, Any]
    annotations: McpToolAnnotations


def prepare_tool(tool: SdkTool, *, max_schema_bytes: int) -> PreparedTool | str:
    """schema 预处理：补 properties、超限剔除、description 截断到 2000 字符。

    返回 PreparedTool，或者剔除原因（中文）。2026-07-28 连接上 SDK 已按规范剔除非法 x-mcp-header
    的工具（client/session.py 的 _absorb_tool_listing），旧版协议的连接 SDK 不查，这里统一再查一次。
    """
    if find_invalid_x_mcp_header(tool.input_schema) is not None:
        return f"工具 {tool.name} 的 x-mcp-header 注解不合法"
    schema = dict(tool.input_schema)
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    size = len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > max_schema_bytes:
        return f"工具 {tool.name} 的参数 schema 超过 {max_schema_bytes} 字节"
    description = tool.description or ""
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]
    annotations = tool.annotations
    return PreparedTool(
        remote_name=tool.name,
        description=description,
        parameters=schema,
        annotations=McpToolAnnotations(
            read_only=annotations.read_only_hint if annotations is not None else None,
            destructive=annotations.destructive_hint if annotations is not None else None,
        ),
    )


def _fault(config: McpServerConfig, kind: FaultKind, message: str) -> ToolSourceFault:
    return ToolSourceFault(
        source=config.source,
        server_id=config.server_id,
        kind=kind,
        message=message,
    )


@dataclass(frozen=True)
class _ServerTools:
    tools: tuple[McpTool, ...]
    faults: tuple[ToolSourceFault, ...] = ()


class McpToolProvider(ScopedToolProvider):
    """MCP 作用域工具来源：每回合按身份列出全部可用 server 的工具，不按档位和绑定裁剪。"""

    def __init__(
        self,
        *,
        runtime: McpRuntime,
        servers: McpServerSource,
        limits: McpProviderLimits,
    ) -> None:
        self._runtime = runtime
        self._servers = servers
        self._limits = limits

    @property
    def name(self) -> str:
        return "mcp"

    async def list_tools(self, scope: ToolScope) -> ScopedTools:
        entries = list(await self._servers.list_servers(scope))
        results = await asyncio.gather(*(self._list_server(entry, scope) for entry in entries))
        tools = [tool for result in results for tool in result.tools]
        faults = [fault for result in results for fault in result.faults]
        return ScopedTools(
            tools=tuple(sorted(tools, key=lambda tool: tool.name)),
            faults=tuple(faults),
        )

    async def _list_server(self, entry: McpServerEntry, scope: ToolScope) -> _ServerTools:
        """列一个 server：单独限时，任何失败都只换成一条 fault，不影响其他 server。"""
        config = entry.config
        context = self._log_context(config, scope)
        try:
            partition = auth_partition(
                config,
                workspace_id=scope.workspace_id,
                user_id=scope.user_id,
            )
            key = PoolKey(
                server_id=config.require_id(),
                config_rev=config.config_rev,
                partition=partition,
            )
        except AgentException as exc:
            # 来源返回了不属于该身份或未落库的 server，属实现错误；只让这一个 server 缺席。
            logger.error("MCP server 配置与回合身份不符，已跳过：%s", exc.message, extra=context)
            return _ServerTools(tools=(), faults=(_fault(config, "invalid", exc.message),))
        fault: ToolSourceFault | None = None
        try:
            async with asyncio.timeout(self._limits.list_timeout):
                async with self._runtime.connection(
                    config,
                    entry.secret,
                    partition=partition,
                ) as lease:
                    raw = tuple(await lease.run(list_all_tools(lease.client)))
            self._runtime.remember_tools(key, raw)
        except Exception as exc:  # noqa: BLE001  一个 server 失败只记 fault，回合照常
            fault = self._list_fault(config, exc, context)
            stale = self._runtime.last_good_tools(key)
            if stale is None:
                return _ServerTools(tools=(), faults=(fault,))
            logger.warning(
                "MCP 工具清单刷新失败，沿用上次成功的清单，server=%s",
                config.server_key,
                extra=context,
            )
            fault = _fault(config, "unavailable", "MCP server 暂时不可用，沿用上次获取的工具清单")
            raw = stale
        built = self._build_tools(entry, raw, context)
        if fault is not None:
            return _ServerTools(tools=built.tools, faults=(*built.faults, fault))
        return built

    def _list_fault(
        self,
        config: McpServerConfig,
        exc: Exception,
        context: dict[str, Any],
    ) -> ToolSourceFault:
        """把列工具的异常换成 fault；异常类型只进服务端日志，fault 文本不带地址和凭证。"""
        cause = root_cause(exc)
        if isinstance(cause, McpListError):
            kind, message = cause.kind, cause.message
        elif isinstance(cause, TimeoutError):
            kind, message = "timeout", "MCP server 获取工具清单超时"
        elif isinstance(cause, McpConnectFailed):
            kind, message = "unavailable", f"MCP server 连接失败：{cause.user_message}"
        else:
            kind, message = "unavailable", "MCP server 暂时不可用"
        logger.warning(
            "MCP 工具清单获取失败，server=%s，种类=%s，错误类型=%s",
            config.server_key,
            kind,
            type(cause).__name__,
            extra=context,
        )
        return _fault(config, kind, message)

    def _build_tools(
        self,
        entry: McpServerEntry,
        raw: Sequence[SdkTool],
        context: dict[str, Any],
    ) -> _ServerTools:
        """过滤 → 预处理 → 命名 → 推导策略，产出 McpTool；被剔除的工具各记一条 invalid fault。"""
        config = entry.config
        faults: list[ToolSourceFault] = []
        prepared: dict[str, PreparedTool] = {}
        for tool in raw:
            if not config.allows(tool.name):
                continue
            result = prepare_tool(tool, max_schema_bytes=self._limits.max_schema_bytes)
            if isinstance(result, str):
                logger.warning("MCP 工具已剔除：%s", result, extra=context)
                faults.append(_fault(config, "invalid", result))
                continue
            prepared[tool.name] = result
        naming = model_tool_names(config.server_key, list(prepared))
        for remote_name in naming.collided:
            prepared.pop(remote_name, None)
            message = f"工具 {remote_name} 的模型名与同一 server 的其他工具相同，已剔除"
            logger.warning("MCP 工具已剔除：%s", message, extra=context)
            faults.append(_fault(config, "invalid", message))
        call_timeout = config.call_timeout(self._limits.call_timeout)
        tools = [
            self._make_tool(entry, prepared[remote], model_name, call_timeout)
            for remote, model_name in naming.names.items()
        ]
        return _ServerTools(tools=tuple(tools), faults=tuple(faults))

    def _make_tool(
        self,
        entry: McpServerEntry,
        prepared: PreparedTool,
        model_name: str,
        call_timeout: float,
    ) -> McpTool:
        config = entry.config
        ref = McpToolRef(
            server_id=config.require_id(),
            server_key=config.server_key,
            config_rev=config.config_rev,
            remote_name=prepared.remote_name,
            model_name=model_name,
            schema_sha256=schema_digest(prepared.parameters),
        )
        approval = requires_approval(config.approval_mode, prepared.annotations)

        async def call(
            target: McpToolRef,
            arguments: dict[str, Any],
            ctx: ToolContext,
            timeout: float,
        ) -> CallToolResult:
            return await self._call(entry, target, arguments, ctx, timeout)

        return McpTool(
            ref=ref,
            description=prepared.description,
            parameters=prepared.parameters,
            policy=mcp_tool_policy(model_name, requires_approval=approval),
            call=call,
            call_timeout=call_timeout,
            max_output_chars=self._limits.max_output_chars,
        )

    async def _call(
        self,
        entry: McpServerEntry,
        ref: McpToolRef,
        arguments: dict[str, Any],
        ctx: ToolContext,
        timeout: float,
    ) -> CallToolResult:
        """用回合的可信身份重新算授权分区、借连接，再发 tools/call；超时从借到连接后开始计。"""
        if ctx.workspace_id is None or ctx.user_id is None:
            raise McpConnectFailed("缺少调用身份", retryable=False)
        config = entry.config
        partition = auth_partition(config, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
        async with self._runtime.connection(config, entry.secret, partition=partition) as lease:
            # SDK 的读超时到期后按传输方式通知 server 取消（HTTP 关闭响应流）。
            return await lease.run(
                lease.client.call_tool(ref.remote_name, arguments, read_timeout_seconds=timeout),
            )

    @staticmethod
    def _log_context(config: McpServerConfig, scope: ToolScope) -> dict[str, Any]:
        return {
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
            "mcp_server_id": config.server_id,
            "mcp_server_key": config.server_key,
        }
