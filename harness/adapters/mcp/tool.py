"""McpTool：MCP 远程工具包装成领域层的 Tool，调用时按 McpToolRef 路由。

工具对象冻结路由与 schema，调用闭包里带着本回合解析时读到的配置与静态请求头，只在本回合内使用、
不落库也不进续跑状态。调用时用回合的可信身份重新算授权分区并核对 server 归属，所以同一个工具对象
可以在一个回合的主执行与子执行之间共用。
所有失败都转成 ToolOutput.error 交给模型，不往外抛，单个 server 故障不会让回合失败。
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from jsonschema import Draft202012Validator, SchemaError
from jsonschema.exceptions import best_match
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from mcp import InputRequiredRoundsExceededError, MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT, CallToolResult
from pydantic import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from core.errors import AgentException
from harness.adapters.mcp.pool import McpConnectFailed, McpConnectionLost, root_cause
from harness.adapters.mcp.results import to_tool_output
from harness.domain.mcp import McpToolRef
from harness.domain.tool import Tool, ToolContext, ToolOutput, ToolPolicy

logger = logging.getLogger(__name__)

CallFn = Callable[[McpToolRef, dict[str, Any], ToolContext, float], Awaitable[CallToolResult]]
"""发起一次 tools/call：(路由键, 入参, 工具上下文, 调用超时秒数) → CallToolResult。

由 McpToolProvider 提供。超时只从借到连接后开始计，建连另受建连超时限制。建连阶段失败抛
McpConnectFailed，调用中连接断开抛 McpConnectionLost，调用超时抛 TimeoutError，
OAuth server 401 且刷新失败（或 403 insufficient_scope）抛 McpAuthRequiredError，
其余 SDK 异常原样上抛，由 McpTool.execute 统一映射。
"""


class McpAuthRequiredError(Exception):
    """OAuth server 需要用户重新授权：401 且刷新失败，或 403 insufficient_scope（需要更大的 scope）。"""

    def __init__(self, *, insufficient_scope: bool = False) -> None:
        super().__init__("需要重新授权")
        self.insufficient_scope = insufficient_scope


# 只有建连阶段失败（请求确实没发出去）才自动重试，且只重试 1 次。
_CONNECT_RETRIES = 1


def compile_validator(schema: dict[str, Any]) -> Validator:
    """按 schema 声明的方言编译校验器，未声明时用 2020-12；$ref 只在 schema 文档内解析，不联网。"""
    validator_cls = validator_for(schema, default=Draft202012Validator)
    validator_cls.check_schema(schema)
    return validator_cls(schema, registry=Registry())


class McpTool(Tool):
    """一个 MCP 远程工具：source="mcp:{server_key}"，持有 McpToolRef；参数 schema 用 server 下发的。"""

    def __init__(
        self,
        *,
        ref: McpToolRef,
        description: str,
        parameters: dict[str, Any],
        policy: ToolPolicy,
        call: CallFn,
        call_timeout: float,
        max_output_chars: int,
    ) -> None:
        self.ref = ref
        self.name = ref.model_name
        self.description = description
        self.parameters = parameters
        self.policy = policy
        self.source = f"mcp:{ref.server_key}"
        self._call = call
        self._call_timeout = call_timeout
        self._max_output_chars = max_output_chars
        self._validator: Validator | None = None

    def route_snapshot(self) -> dict[str, Any]:
        """路由键（含 config_rev 与 schema 摘要）：审批挂起时冻结，恢复时不一致就不执行。"""
        return self.ref.to_dict()

    def normalize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 inputSchema 校验入参（JSON Schema 2020-12），不合法时抛中文异常，审核链据此拒绝。

        MCP 入参原样透传给 server，不做规范化。
        """
        try:
            if self._validator is None:
                self._validator = compile_validator(self.parameters)
            error = best_match(self._validator.iter_errors(arguments))
        except (SchemaError, Unresolvable) as exc:
            raise AgentException.message(
                "MCP 工具的参数 schema 无效，无法校验入参",
                {"tool_name": self.name},
            ) from exc
        if error is not None:
            path = "/".join(str(part) for part in error.absolute_path)
            raise AgentException.message(
                "工具参数校验失败",
                {"tool_name": self.name, "path": path or "/", "error": error.message},
            )
        return arguments

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        """调用远程工具；按 5.4 节把所有失败转成 ToolOutput.error，不往外抛。

        只有建连阶段失败（请求确实没发出去）才自动重试 1 次；调用超时判为结果未知，不重试。
        """
        attempts = 0
        while True:
            try:
                result = await self._call(self.ref, arguments, ctx, self._call_timeout)
            except McpConnectFailed as exc:
                if exc.retryable and attempts < _CONNECT_RETRIES:
                    attempts += 1
                    self._log("MCP 建连失败，自动重试 1 次", ctx, exc)
                    continue
                return self._failure(f"MCP server {self.ref.server_key} 连接失败：{exc.user_message}")
            except Exception as exc:  # noqa: BLE001  一切失败都交给模型，不让单个 server 拖垮回合
                return self._map_error(exc, ctx)
            return to_tool_output(result, max_chars=self._max_output_chars)

    def _timeout_text(self) -> str:
        seconds = f"{self._call_timeout:g}"
        return f"MCP 工具 {self.name} 调用超时（{seconds} 秒），结果未知"

    def _failure(self, text: str) -> ToolOutput:
        return ToolOutput.error(text)

    def _map_error(self, exc: Exception, ctx: ToolContext) -> ToolOutput:
        cause = root_cause(exc)
        key = self.ref.server_key
        if isinstance(cause, McpAuthRequiredError):
            self._log("MCP 工具调用需要重新授权", ctx, cause)
            return self._failure(f"需要重新授权 MCP server {key}")
        if isinstance(cause, TimeoutError):
            self._log("MCP 工具调用超时，结果未知", ctx, cause)
            return self._failure(self._timeout_text())
        if isinstance(cause, McpConnectionLost | httpx2.TransportError):
            self._log("MCP 调用中连接断开，结果未知", ctx, cause)
            return self._failure(f"MCP server {key} 连接失败")
        if isinstance(cause, AgentException):
            # 身份与 server 不符等业务拒绝，消息本身就是可展示的中文。
            self._log("MCP 工具调用被拒绝", ctx, cause)
            return self._failure(cause.message)
        if isinstance(cause, InputRequiredRoundsExceededError):
            return self._failure("该工具需要交互输入，暂不支持")
        if isinstance(cause, MCPError):
            if cause.code == REQUEST_TIMEOUT:
                self._log("MCP 工具调用超时，结果未知", ctx, cause)
                return self._failure(self._timeout_text())
            if cause.code == CONNECTION_CLOSED:
                self._log("MCP 调用中连接断开，结果未知", ctx, cause)
                return self._failure(f"MCP server {key} 连接失败")
            self._log("MCP 工具调用返回协议错误", ctx, cause, error_code=cause.code)
            return self._failure(f"MCP 协议错误 {cause.code}：{cause.message}")
        if isinstance(cause, RuntimeError) and "structured content" in str(cause):
            # SDK 按 outputSchema 校验结构化结果失败时抛 RuntimeError（client/session.py validate_tool_result）。
            self._log("MCP 工具结果不符合 outputSchema", ctx, cause)
            return self._failure("MCP 工具返回的结构不符合其声明的 outputSchema")
        if isinstance(cause, ValidationError):
            self._log("MCP 工具结果格式不符合协议", ctx, cause)
            return self._failure("MCP 工具返回的结果格式不符合协议")
        logger.exception(
            "MCP 工具调用失败，工具=%s，错误类型=%s",
            self.name,
            type(cause).__name__,
            extra=self._log_context(ctx),
        )
        return self._failure(f"MCP 工具 {self.name} 调用失败")

    def _log_context(self, ctx: ToolContext) -> dict[str, Any]:
        return {
            "session_id": ctx.session_id,
            "tool_name": self.name,
            "mcp_server_id": self.ref.server_id,
            "mcp_server_key": self.ref.server_key,
        }

    def _log(
        self,
        message: str,
        ctx: ToolContext,
        exc: BaseException,
        *,
        error_code: int | None = None,
    ) -> None:
        context = self._log_context(ctx)
        if error_code is not None:
            context["error_code"] = error_code
        logger.warning(
            "%s，工具=%s，错误类型=%s",
            message,
            self.name,
            type(exc).__name__,
            extra=context,
        )
