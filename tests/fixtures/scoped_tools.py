"""作用域工具的测试替身：内存版 ScopedToolProvider 与 source="mcp:fake" 的假工具，不碰网络和数据库。

经 ``replace(default_harness(), scoped_tools=(FakeScopedSource(...),))`` 注入组装根。
"""

import asyncio
from typing import Any

from core.errors import AgentException
from harness.app.ports import ScopedToolProvider, ScopedTools, ToolSourceFault
from harness.domain.tool import (
    ALL_PROFILES_AND_DELEGATE,
    Tool,
    ToolContext,
    ToolOutput,
    ToolPolicy,
    ToolScope,
)

FAKE_SOURCE = "mcp:fake"


class FakeScopedTool(Tool):
    """假 MCP 工具：参数直接用 JSON Schema；策略同 MCP 工具的设想，全部档位可调、不可绑定。

    每次执行记下入参与工具上下文，返回构造时给定的结果（默认成功）。
    """

    def __init__(
        self,
        name: str,
        *,
        requires_approval: bool = False,
        output: ToolOutput | None = None,
        source: str = FAKE_SOURCE,
    ) -> None:
        self.name = name
        self.description = f"假作用域工具 {name}"
        self.parameters = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.policy = ToolPolicy(
            name=name,
            category="mcp",
            bindable_profiles=frozenset(),
            callable_profiles=ALL_PROFILES_AND_DELEGATE,
            requires_approval=requires_approval,
        )
        self.source = source
        self._output = output or ToolOutput.ok(f"{name} 的结果")
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []

    def normalize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        if not isinstance(query, str):
            raise AgentException.message("工具参数校验失败", {"tool_name": self.name})
        return {"query": query}

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.calls.append((arguments, ctx))
        return self._output


class FakeScopedSource(ScopedToolProvider):
    """内存版作用域来源：记下每次解析的身份；可配置为抛异常或拖延，用来模拟故障与超时。"""

    def __init__(
        self,
        *tools: Tool,
        name: str = "mcp",
        faults: tuple[ToolSourceFault, ...] = (),
        error: Exception | None = None,
        delay: float | None = None,
    ) -> None:
        self._name = name
        self._tools = tools
        self._faults = faults
        self._error = error
        self._delay = delay
        self.scopes: list[ToolScope] = []
        self.cancelled = False

    @property
    def name(self) -> str:
        return self._name

    async def list_tools(self, scope: ToolScope) -> ScopedTools:
        self.scopes.append(scope)
        if self._delay is not None:
            try:
                await asyncio.sleep(self._delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self._error is not None:
            raise self._error
        return ScopedTools(tools=self._tools, faults=self._faults)
