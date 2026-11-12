"""ToolSet 与 ToolProvider：多来源聚合、重名校验、稳定顺序、按名查策略，以及 JSON Schema 工具接入。"""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from core.errors import AgentException
from harness.adapters.tools.builtin_provider import BuiltinToolProvider, builtin_tool_set
from harness.app.ports import ToolProvider
from harness.app.tool_set import ToolSet
from harness.domain.context import ToolSpec
from harness.domain.tool import (
    PydanticTool,
    Tool,
    ToolContext,
    ToolOutput,
    ToolPolicy,
    tool_policy,
    unknown_tool_policy,
)

pytestmark = pytest.mark.anyio


class _NamedInput(BaseModel):
    text: str = Field(description="文本")


def _pydantic_tool(tool_name: str) -> PydanticTool:
    """构造一个只回显入参的 Pydantic 工具，名称可定制。"""

    class _Tool(PydanticTool):
        name = tool_name
        description = f"{tool_name} 工具"
        input_model = _NamedInput
        policy = tool_policy(tool_name, "leaf")

        async def run(self, args: BaseModel, ctx: ToolContext) -> str:
            return f"{tool_name}:{_NamedInput.model_validate(args).text}"

    return _Tool()


class _StaticProvider(ToolProvider):
    def __init__(self, *tools: Tool) -> None:
        self._tools = list(tools)

    def list_tools(self) -> list[Tool]:
        return list(self._tools)


class _JsonSchemaTool(Tool):
    """模拟 MCP 式工具：没有 Pydantic 模型，参数 schema 直接给 dict，自己负责校验。"""

    def __init__(self) -> None:
        self.name = "mcp__demo__lookup"
        self.description = "按关键字查找条目"
        self.parameters = {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "关键字"}},
            "required": ["query"],
        }
        self.policy = ToolPolicy(
            name=self.name,
            category="mcp",
            bindable_profiles=frozenset({"lead"}),
            callable_profiles=frozenset({"lead"}),
            requires_approval=True,
        )

    def normalize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments.get("query"), str):
            raise AgentException.message("工具参数校验失败", {"tool_name": self.name})
        return {"query": arguments["query"]}

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        if not isinstance(arguments.get("query"), str):
            return ToolOutput.ok("error: query 必须是字符串")
        return ToolOutput.ok(f"找到 {arguments['query']}")


def _ctx() -> ToolContext:
    return ToolContext(session_id=1, db=None, principal_name="lead")


def test_duplicate_tool_names_raise_chinese_error() -> None:
    with pytest.raises(AgentException) as exc_info:
        ToolSet([_pydantic_tool("A"), _pydantic_tool("A")])

    assert exc_info.value.message == "工具名称重复"
    assert exc_info.value.details == {"tool_name": "A"}


def test_duplicate_names_across_providers_raise() -> None:
    with pytest.raises(AgentException, match="工具名称重复"):
        ToolSet.from_providers(
            _StaticProvider(_pydantic_tool("A")),
            _StaticProvider(_pydantic_tool("A")),
        )


def test_multiple_providers_keep_stable_order() -> None:
    first = _StaticProvider(_pydantic_tool("B"), _pydantic_tool("A"))
    second = _StaticProvider(_pydantic_tool("D"), _pydantic_tool("C"))

    tool_set = ToolSet.from_providers(first, second)

    assert [tool.name for tool in tool_set.tools] == ["B", "A", "D", "C"]
    assert [spec.name for spec in tool_set.to_specs()] == ["B", "A", "D", "C"]
    # 裁剪与追加保持原有相对顺序，不按传入参数或字母序重排。
    assert [tool.name for tool in tool_set.only("C", "B").tools] == ["B", "C"]
    assert [tool.name for tool in tool_set.without("A").tools] == ["B", "D", "C"]
    replaced = tool_set.with_tools(_pydantic_tool("A"), _pydantic_tool("E"))
    assert [tool.name for tool in replaced.tools] == ["B", "A", "D", "C", "E"]


def test_builtin_provider_order_is_reproducible() -> None:
    names = [tool.name for tool in BuiltinToolProvider().list_tools()]

    assert names == [tool.name for tool in builtin_tool_set().tools]
    assert len(names) == len(set(names)) == 26


def test_unknown_tool_policy_cannot_bind_or_call() -> None:
    policy = ToolSet([_pydantic_tool("A")]).policy_for("Missing")

    assert policy == unknown_tool_policy("Missing")
    assert policy.category == "unknown"
    assert policy.bindable_profiles == frozenset()
    assert policy.callable_profiles == frozenset()


def test_get_missing_tool_keeps_error_text() -> None:
    with pytest.raises(AgentException) as exc_info:
        ToolSet().get("Missing")

    assert exc_info.value.message == "工具不存在"


def test_validate_input_delegates_to_tool_normalize_arguments() -> None:
    tool_set = ToolSet([_pydantic_tool("A")])

    assert tool_set.validate_input("A", {"text": "hi"}) == {"text": "hi"}
    with pytest.raises(AgentException) as exc_info:
        tool_set.validate_input("A", {})
    assert exc_info.value.message == "工具参数校验失败"
    assert exc_info.value.details["tool_name"] == "A"
    assert exc_info.value.details["errors"][0]["loc"] == ("text",)


async def test_pydantic_tool_returns_validation_error_as_result() -> None:
    output = await _pydantic_tool("A").execute({}, _ctx())

    assert output.is_error is False
    assert output.text.startswith("error: ")
    assert output.text.endswith(". hint: Please fix the parameters and try again")


async def test_json_schema_tool_aggregates_specs_and_executes() -> None:
    mcp_tool = _JsonSchemaTool()
    tool_set = ToolSet.from_providers(
        _StaticProvider(_pydantic_tool("Read")),
        _StaticProvider(mcp_tool),
    )

    assert tool_set.to_specs()[-1] == ToolSpec(
        name="mcp__demo__lookup",
        description="按关键字查找条目",
        input_schema=mcp_tool.parameters,
    )
    assert tool_set.policy_for("mcp__demo__lookup").requires_approval is True
    assert tool_set.callable_names(["Read", "mcp__demo__lookup"], "lead") == (
        "Read",
        "mcp__demo__lookup",
    )
    assert tool_set.callable_names(["mcp__demo__lookup"], "user_agent") == ()
    assert tool_set.validate_input("mcp__demo__lookup", {"query": "x"}) == {"query": "x"}
    tool = tool_set.get("mcp__demo__lookup")
    assert await tool.execute({"query": "订单"}, _ctx()) == ToolOutput.ok("找到 订单")


def test_callable_and_bindable_names_keep_input_order_and_strict_mode() -> None:
    tool_set = builtin_tool_set()

    assert tool_set.callable_names(["Read", "Read", "SkillRun", "missing"], "user_agent") == (
        "Read",
        "SkillRun",
    )
    assert tool_set.bindable_names(["Read", "SkillRun"], "user_agent") == ("Read",)
    with pytest.raises(ValueError, match="工具绑定策略拒绝 profile=user_agent: SkillRun"):
        tool_set.bindable_names(["Read", "SkillRun"], "user_agent", strict=True)
    assert tool_set.denied_names(["Read", "TeamCreate", "missing"], "user_agent") == (
        "TeamCreate",
        "missing",
    )
