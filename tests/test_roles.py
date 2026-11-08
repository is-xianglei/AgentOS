"""角色单元测试：每个角色的 context 都是纯函数，不传任何 I/O 依赖也能调用，同样的输入得到相同的 Context。

逐字节的提示文本由 tests/test_role_context_golden.py 固化，这里只测角色自身的契约：
不改写入参、段的取舍、记忆正文的位置、工具与收尾变体。
"""

from datetime import date
from pathlib import Path

import pytest

from harness.domain.context import Context, PromptBlock, ToolSpec
from harness.domain.memory import MemoryRecall
from harness.domain.messages import AssistantMessage, TextBlock, ToolResult, UserMessage
from harness.domain.prompts.lead import TURBO_PROMPT
from harness.domain.prompts.memory import MEMORY_TRUST_RULES
from harness.domain.roles.assistant import Assistant
from harness.domain.roles.compactor import Compactor
from harness.domain.roles.subagent import AgentType, SubAgent, SubAgentSpec, render_routing_catalog
from harness.domain.roles.teammate import Teammate, is_identity_block
from harness.domain.tool import ShellAccess

_TODAY = date(2026, 3, 9)
_TOOLS = (ToolSpec(name="Read", description="读文件"), ToolSpec(name="Write", description="写文件"))
_MESSAGES = (
    UserMessage(text="上周写了什么"),
    AssistantMessage(blocks=(TextBlock(text="两段。"),)),
    UserMessage(text="写本周的"),
)


def _assistant(**overrides: object) -> Assistant:
    values: dict[str, object] = {
        "cwd": "/srv",
        "today": _TODAY,
        "skills_catalog": "- s: 技能",
        "session_prompt": "会话指令",
        "memory": MemoryRecall(instruction="- m: 记忆", message_prefix="<召回正文>"),
        "plan_content": "# 计划",
    }
    values.update(overrides)
    return Assistant(**values)  # type: ignore[arg-type]


# ---------- Assistant ----------


def test_assistant_context_is_pure() -> None:
    first = _assistant().context(_MESSAGES, _TOOLS)
    second = _assistant().context(_MESSAGES, _TOOLS)

    assert first == second
    assert isinstance(first, Context)
    assert first.messages == _MESSAGES
    assert first.tools == _TOOLS
    assert first.output_type is None


def test_assistant_places_catalog_and_plan_after_stable_prefix() -> None:
    blocks = _assistant().instruction()

    assert [block.cache_breakpoint for block in blocks] == [True, True, False]
    assert "/srv" in blocks[0].text and "2026-03-09" in blocks[0].text
    assert blocks[1].text.startswith("## Memory Catalog")
    assert blocks[2].text.startswith("## Plan Mode 已启用")


def test_assistant_leaves_memory_prefix_to_session_loading() -> None:
    """记忆正文由会话加载按本轮起始消息 ID 拼进历史，角色不再重复拼接。"""
    context = _assistant().context(_MESSAGES, ())

    assert context.messages == _MESSAGES
    assert all("<召回正文>" not in block.text for block in context.instruction)  # type: ignore[union-attr]


def test_assistant_without_plan_or_memory_has_single_block() -> None:
    blocks = _assistant(memory=None, plan_content=None).instruction()

    assert blocks == (blocks[0],)
    assert blocks[0].cache_breakpoint is True


def test_assistant_turbo_replaces_whole_instruction() -> None:
    assert _assistant(turbo=True).instruction() == (PromptBlock(text=TURBO_PROMPT),)


def test_assistant_final_context_has_no_tools() -> None:
    role = _assistant()
    final = role.final_context(_MESSAGES)

    assert final.tools == ()
    assert final.instruction == role.context(_MESSAGES, _TOOLS).instruction


# ---------- SubAgent ----------

_SPEC = SubAgentSpec(
    agent_type=AgentType.VERIFICATION,
    system_prompt="  你是验收者。\n",
    critical_reminder="再次确认约束。",
)


def test_subagent_context_is_pure_and_prefixes_first_user_message() -> None:
    role = SubAgent(spec=_SPEC, memory_prefix="<m>正文</m>")
    first = role.context(_MESSAGES, _TOOLS)

    assert first == role.context(_MESSAGES, _TOOLS)
    assert first.messages[0] == UserMessage(text="<m>正文</m>\n\n上周写了什么")
    assert first.messages[1:] == _MESSAGES[1:]
    # 入参的消息元组不被改写。
    assert _MESSAGES[0] == UserMessage(text="上周写了什么")
    assert first.instruction == f"你是验收者。\n\n{MEMORY_TRUST_RULES}\n\n再次确认约束。"


def test_subagent_without_memory_keeps_prompt_and_messages() -> None:
    context = SubAgent(spec=_SPEC).context(_MESSAGES, ())

    assert context.instruction == "  你是验收者。\n\n\n再次确认约束。"
    assert context.messages == _MESSAGES


def test_subagent_prefix_skips_non_user_messages() -> None:
    messages = (
        ToolResult(tool_call_id="t1", tool_name="Read", arguments={}, content="x"),
        UserMessage(text="任务"),
    )
    context = SubAgent(spec=_SPEC, memory_prefix="M").context(messages, ())

    assert context.messages == (messages[0], UserMessage(text="M\n\n任务"))


def test_spec_shell_tmp_root_takes_temp_dir_from_caller() -> None:
    spec = SubAgentSpec(
        agent_type=AgentType.VERIFICATION,
        system_prompt="x",
        shell_access=ShellAccess.TMP_WRITABLE,
        display_name="wf-验收",
    )

    assert spec.shell_tmp_root(Path("/t")) == Path("/t/agentos-wf-验收")
    assert _SPEC.shell_tmp_root(Path("/t")) is None


def test_routing_catalog_lists_given_specs() -> None:
    specs = [
        SubAgentSpec(agent_type=AgentType.EXPLORE, system_prompt="x", when_to_use="检索。",
                     allowed_tools=("Read", "Agent")),
        SubAgentSpec(agent_type=AgentType.GENERAL_PURPOSE, system_prompt="x", when_to_use="通用。"),
    ]

    assert render_routing_catalog(specs) == (
        "- Explore: 检索。 可用工具: Read。\n"
        "- general-purpose: 通用。 可用工具: 除全局禁用外全部工具。"
    )


# ---------- Teammate ----------


def _teammate(memory_prefix: str | None = "<m>正文</m>") -> Teammate:
    return Teammate(
        spec=_SPEC, session_id=5, name="writer", role="资料员", memory_prefix=memory_prefix
    )


def test_teammate_context_skips_identity_block() -> None:
    role = _teammate()
    messages = (UserMessage(text=role.identity_block()), UserMessage(text="整理要点"))
    context = role.context(messages, _TOOLS)

    assert context == role.context(messages, _TOOLS)
    assert context.messages == (messages[0], UserMessage(text="<m>正文</m>\n\n整理要点"))
    assert context.tools == _TOOLS


def test_teammate_instruction_has_header_and_no_critical_reminder() -> None:
    instruction = _teammate().instruction()

    assert instruction.startswith("你是团队成员 'writer',角色: 资料员,隶属会话 5 的团队。\n")
    assert instruction.endswith(MEMORY_TRUST_RULES)
    assert "再次确认约束" not in instruction
    assert _teammate(None).instruction().endswith("  你是验收者。\n")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<identity>You are 'w', role: r, team session 1. Continue your work.</identity>", True),
        ("<identity>别的内容</identity>", False),
        ("普通消息", False),
        (["不是字符串"], False),
        (None, False),
    ],
)
def test_is_identity_block(content: object, expected: bool) -> None:
    assert is_identity_block(content) is expected


# ---------- Compactor ----------


def _tool(call_id: str, tool_name: str, output: str) -> dict[str, object]:
    return {
        "role": "tool",
        "content": ToolResult(
            tool_call_id=call_id, tool_name=tool_name, arguments={}, content=output
        ).to_content(),
    }


def test_micro_compact_returns_new_list_without_mutating_input() -> None:
    rows = [_tool(f"t{i}", "Grep", "长" * 600) for i in range(5)]
    snapshot = [dict(row, content=dict(row["content"])) for row in rows]  # type: ignore[arg-type]

    compacted = Compactor().micro_compact(rows)

    assert rows == snapshot
    assert [row["content"]["output"] for row in compacted[:2]] == ["[Previous: used Grep]"] * 2  # type: ignore[index]
    assert compacted[2:] == rows[2:]
    assert compacted == Compactor().micro_compact(rows)


def test_compactor_context_carries_history_json_without_instruction() -> None:
    context = Compactor().context([{"role": "user", "content": "你好"}])

    assert context.instruction is None
    assert context.tools == ()
    assert len(context.messages) == 1
    text = context.messages[0].text  # type: ignore[union-attr]
    assert text.startswith("为保持上下文连贯")
    assert text.endswith('[{"role": "user", "content": "你好"}]')


def test_compactor_summary_messages() -> None:
    assert Compactor().summary_messages("摘要") == [
        {"role": "user", "content": "[Conversation compressed.]\n\n摘要"}
    ]
