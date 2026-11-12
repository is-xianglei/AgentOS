"""MCP 工具黄金测试：固化 MCP 工具对模型与审批的对外行为（第八份黄金测试）。

固化三样东西，字面量在 tests/fixtures/mcp_tool_golden.py，由 M1c 实现的实际产出生成：
- 一组假 server 工具经命名、schema 预处理后，经两家客户端 _encode_tools 发给模型的 tools 字面量，
  以及被剔除工具的 fault；
- 审批推导矩阵：三档审批模式 × 两项注解（各取 None / True / False）推导出的 requires_approval、
  MCP 工具的能力策略，以及权限服务据此给出的默认判定；
- 结果转换：各类 CallToolResult 经 to_tool_output 转成的 (text, is_error)。
此后只允许改动本文件「驱动」一节，字面量与断言都不许改；工具描述连同字典键序一起比对，
并用 SHA-256 再校验一遍序列化字节。
"""

import hashlib
import json
from typing import Any

import mcp_types as t
import pytest

from harness.adapters.mcp.provider import McpProviderLimits, McpToolProvider
from harness.adapters.mcp.results import to_tool_output
from harness.adapters.models.anthropic_client import AnthropicClient
from harness.adapters.models.openai_client import OpenAIClient
from harness.adapters.tools.builtin.echo import EchoTool
from harness.app.tool_set import ToolSet
from harness.domain.mcp import (
    ApprovalMode,
    McpServerEntry,
    McpToolAnnotations,
    mcp_tool_policy,
    requires_approval,
)
from harness.domain.tool import ToolScope
from permission.service import PermissionService
from tests.fixtures.mcp_fake import FakeMcpServer, StaticServers, make_config, mcp_harness
from tests.fixtures.mcp_tool_golden import (
    EXPECTED_APPROVAL,
    EXPECTED_APPROVAL_SHA256,
    EXPECTED_FAULTS,
    EXPECTED_RESULTS,
    EXPECTED_RESULTS_SHA256,
    EXPECTED_TOOLS,
    EXPECTED_TOOLS_SHA256,
)

pytestmark = pytest.mark.anyio


# ---------- 驱动：假 server 的工具、如何取得发给模型的 tools、审批与结果（只改这一节） ----------

_SCOPE = ToolScope(workspace_id=1, user_id=9)
_LONG_NAME = "export_" + "quarterly_financial_report_" * 2 + "pdf"


def _fake_server() -> FakeMcpServer:
    """一组覆盖命名与 schema 预处理各分支的假 server 工具（顺序故意打乱）。"""
    fake = FakeMcpServer(page_size=3)
    fake.add(
        "search_issues",
        description="按关键字搜索 issue",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键字"},
                "limit": {"type": "integer", "minimum": 1, "default": 20},
            },
            "required": ["query"],
        },
        annotations=t.ToolAnnotations(read_only_hint=True),
    )
    # 缺 properties：补空对象。
    fake.add("ping", description="连通性检查", schema={"type": "object"})
    # 清洗有损（含 . 与 /）：加原名哈希后缀。
    fake.add(
        "repo.get/file",
        description="读取仓库文件",
        schema={"type": "object", "properties": {}},
    )
    # 超长：截短后加哈希后缀，总长 64。
    fake.add(_LONG_NAME, description="导出报表")
    fake.add(
        "delete_repo",
        description="删除仓库",
        annotations=t.ToolAnnotations(destructive_hint=True),
    )
    # 描述超过 2000 字符：截断。
    fake.add("summarize", description="生成摘要 " * 500)
    # schema 超过上限：剔除并记 invalid fault。
    fake.add(
        "huge",
        description="schema 过大",
        schema={"type": "object", "properties": {"x": {"description": "x" * 600}}},
    )
    # 两个原名算出同一个模型名：都剔除并各记一条 invalid fault。
    fake.add("a.b")
    fake.add("a_b_69f6642c")
    return fake


_LIMITS = McpProviderLimits(
    list_timeout=3.0, call_timeout=3.0, max_output_chars=20000, max_schema_bytes=512
)


async def _listed() -> Any:
    entry = McpServerEntry(make_config(server_key="github", approval_mode=ApprovalMode.ALWAYS))
    async with mcp_harness(_fake_server()) as h:
        provider = McpToolProvider(runtime=h.runtime, servers=StaticServers(entry), limits=_LIMITS)
        return await provider.list_tools(_SCOPE)


async def _encoded_tools() -> dict[str, Any]:
    """MCP 工具经两家客户端 _encode_tools 发给模型的 tools；排在一个内置工具之后，验证合并顺序。"""
    listed = await _listed()
    specs = ToolSet((EchoTool(), *listed.tools)).to_specs()
    return {
        "anthropic": [dict(item) for item in AnthropicClient._encode_tools(specs)],
        "openai": [dict(item) for item in OpenAIClient._encode_tools(specs)],
    }


async def _faults() -> list[dict[str, Any]]:
    return [fault.to_dict() for fault in (await _listed()).faults]


_TRISTATE = (None, True, False)


def _approval_matrix() -> list[dict[str, Any]]:
    """三档模式 × read_only × destructive：推导结果、能力策略与无规则命中时的默认判定。"""
    permissions = PermissionService(db=None)  # type: ignore[arg-type]
    rows: list[dict[str, Any]] = []
    for mode in ApprovalMode:
        for read_only in _TRISTATE:
            for destructive in _TRISTATE:
                annotations = McpToolAnnotations(read_only=read_only, destructive=destructive)
                approval = requires_approval(mode, annotations)
                policy = mcp_tool_policy("mcp__github__tool", requires_approval=approval)
                rows.append(
                    {
                        "mode": mode.value,
                        "read_only": read_only,
                        "destructive": destructive,
                        "requires_approval": approval,
                        "category": policy.category,
                        "bindable_profiles": sorted(policy.bindable_profiles),
                        "callable_profiles": sorted(policy.callable_profiles),
                        "default_behavior": permissions.default_behavior(
                            "mcp__github__tool", "lead", policy=policy
                        ),
                    }
                )
    return rows


def _result_cases() -> dict[str, t.CallToolResult]:
    """结果转换的各类输入：文本、isError、媒体占位、资源、结构化补文本、未知块与截断。"""
    long_text = "甲" * 120 + "乙" * 120
    return {
        "text": t.CallToolResult(
            content=[t.TextContent(text="第一段"), t.TextContent(text="第二段")]
        ),
        "is_error": t.CallToolResult(content=[t.TextContent(text="参数不对")], is_error=True),
        "image_and_audio": t.CallToolResult(
            content=[
                t.TextContent(text="见附件"),
                t.ImageContent(data="A" * 4000, mime_type="image/png"),
                t.AudioContent(data="AAAA", mime_type="audio/wav"),
            ]
        ),
        "resources": t.CallToolResult(
            content=[
                t.ResourceLink(name="设计稿", uri="https://example.com/d.md", type="resource_link"),
                t.EmbeddedResource(
                    type="resource",
                    resource=t.TextResourceContents(uri="file:///a.txt", text="内嵌文本"),
                ),
                t.EmbeddedResource(
                    type="resource",
                    resource=t.BlobResourceContents(uri="file:///b.bin", blob="AAAAAAAA"),
                ),
            ]
        ),
        "structured_only": t.CallToolResult(content=[], structured_content={"总数": 2, "ok": True}),
        "structured_with_text": t.CallToolResult(
            content=[t.TextContent(text="已有文本")], structured_content={"总数": 2}
        ),
        "empty": t.CallToolResult(content=[]),
        "truncated": t.CallToolResult(content=[t.TextContent(text=long_text)]),
    }


_RESULT_MAX_CHARS = 100


def _converted_results() -> dict[str, dict[str, Any]]:
    converted: dict[str, dict[str, Any]] = {}
    for name, result in _result_cases().items():
        output = to_tool_output(result, max_chars=_RESULT_MAX_CHARS)
        converted[name] = {"text": output.text, "is_error": output.is_error}
    return converted


# ---------- 断言（字面量与断言都不许改） ----------


def _ordered(value: Any) -> Any:
    """连同字典键序一起比较：键序决定 SDK 序列化出的请求体字节。"""
    if isinstance(value, dict):
        return ("dict", tuple((key, _ordered(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_ordered(item) for item in value))
    return value


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()


def test_golden_literals_match_their_digests() -> None:
    """字面量自身与摘要一致，防止字面量被悄悄改动。"""
    assert _sha256(EXPECTED_TOOLS) == EXPECTED_TOOLS_SHA256
    assert _sha256(EXPECTED_APPROVAL) == EXPECTED_APPROVAL_SHA256
    assert _sha256(EXPECTED_RESULTS) == EXPECTED_RESULTS_SHA256


async def test_mcp_tools_sent_to_both_providers_match_golden() -> None:
    """命名、schema 预处理、排序后的 tools，逐字节不变（含字典键序），两家各一份。"""
    actual = await _encoded_tools()

    assert actual == EXPECTED_TOOLS
    assert _ordered(actual) == _ordered(EXPECTED_TOOLS), "取值相同但字典键序不同"
    assert _sha256(actual) == EXPECTED_TOOLS_SHA256


async def test_dropped_tools_faults_match_golden() -> None:
    assert await _faults() == EXPECTED_FAULTS


def test_approval_matrix_matches_golden() -> None:
    actual = _approval_matrix()

    assert actual == EXPECTED_APPROVAL
    assert _sha256(actual) == EXPECTED_APPROVAL_SHA256


def test_result_conversion_matches_golden() -> None:
    actual = _converted_results()

    assert actual == EXPECTED_RESULTS
    assert _sha256(actual) == EXPECTED_RESULTS_SHA256
