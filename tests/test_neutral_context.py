"""阶段 3 读路径中立化的最小覆盖测试。

钉住 implement.md 标注的静默失效点:快照形状(步 12)、verification 触发
(步 13)、记忆抽取取文(步 14),以及读路径本身(_messages_to_context /
_micro_compact,步 10-11)。这些路径此前零覆盖,失效形式是静默空值而非抛错,
必须用断言守住,否则写路径与读路径的判别键漂移不会被任何测试发现。
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from llm.types import (
    LLMEventKind,
    LLMStreamEvent,
    LLMTurnResult,
    ToolResultMessage,
    ToolUse,
    build_assistant_blocks,
)
from memory.jobs.extraction import MemoryExtractionService
from runtime.compact import CompactService
from session.service import SessionService
from tools.subagents.trigger import assess


def _msg(message_id: int, role: str, content: Any) -> Any:
    """_messages_to_context 只读 id/role/content,用轻量替身代替 ORM 实例。"""
    return SimpleNamespace(id=message_id, role=role, content=content)


def _tool_content(tool_use_id: str, tool_name: str, output: str, is_error: bool = False) -> dict:
    return ToolResultMessage(
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        input_args={},
        output=output,
        is_error=is_error,
    ).to_content_dict()


def _service() -> SessionService:
    """_messages_to_context 不触碰任何实例状态,跳过需要 db 的构造器。"""
    return SessionService.__new__(SessionService)


def test_messages_to_context_passes_neutral_shapes_through() -> None:
    """步 10:三类角色原样透出中立形状,不再编 tool_result 块、不再合并。"""
    assistant_blocks = build_assistant_blocks(
        "读两个文件",
        [
            ToolUse(id="t1", name="file_read", input={"path": "a.txt"}),
            ToolUse(id="t2", name="file_read", input={"path": "b.txt"}),
        ],
    )
    messages = [
        _msg(1, "user", "看下 a 和 b"),
        _msg(2, "assistant", assistant_blocks),
        _msg(3, "tool", _tool_content("t1", "file_read", "内容A")),
        _msg(4, "tool", _tool_content("t2", "file_read", "不存在", is_error=True)),
        _msg(5, "user", "只用 a"),
    ]
    context = _service()._messages_to_context(messages)

    # 连续 tool 消息不再合并进 user 消息:五进五出,角色一一对应。
    assert [m["role"] for m in context] == ["user", "assistant", "tool", "tool", "user"]
    assert context[0]["content"] == "看下 a 和 b"
    assert context[1]["content"] == assistant_blocks
    assert context[2]["content"]["output"] == "内容A"
    assert context[3]["content"]["is_error"] is True
    # 上下文中无任何 provider wire 判别键(tool_use_id 属 ToolResultMessage 自有协议,不算)。
    serialized = json.dumps(context, ensure_ascii=False)
    assert '"type"' not in serialized
    # deepcopy 隔离:改上下文不得写回落库对象。
    context[2]["content"]["output"] = "改掉"
    assert messages[2].content["output"] == "内容A"


def test_messages_to_context_injects_memories_and_additional_context() -> None:
    """步 10:Memory 正文与临时上下文仍按消息 ID 注入 user 纯文本副本。"""
    context = _service()._messages_to_context(
        [_msg(7, "user", "原始请求")],
        additional_context_message_id=7,
        additional_context="CTX",
        rendered_memories_message_id=7,
        rendered_memories="MEM",
    )
    assert context == [{"role": "user", "content": "MEM\n\n原始请求\n\nCTX"}]


def test_micro_compact_reads_neutral_tool_messages() -> None:
    """步 11:微压缩改读 role="tool" 的中立 dict,占位符与白名单语义不变。"""
    long_a = "A" * 600
    long_b = "B" * 600
    messages = [
        {"role": "tool", "content": _tool_content("t1", "grep", long_a)},
        {"role": "tool", "content": _tool_content("t2", "read_file", long_b)},
        {"role": "tool", "content": _tool_content("t3", "grep", "C" * 600)},
        {"role": "tool", "content": _tool_content("t4", "grep", "D" * 600)},
        {"role": "tool", "content": _tool_content("t5", "grep", "E" * 600)},
    ]
    service = CompactService(session_repo=None, llm=object())
    compacted = service._micro_compact(messages)

    # 前 2 条是旧结果:非白名单的被替换,read_file 原样保留。
    assert compacted[0]["content"]["output"] == "[Previous: used grep]"
    assert compacted[1]["content"]["output"] == long_b
    # 最近 3 条完整保留。
    assert compacted[2]["content"]["output"] == "C" * 600
    assert compacted[4]["content"]["output"] == "E" * 600


@pytest.mark.anyio
async def test_auto_compact_snapshot_stores_neutral_shape() -> None:
    """步 12(AC11):快照落库的是中立形状,切 provider 后旧快照仍可用。"""

    class _FakeLLM:
        provider = "anthropic"

        async def stream(self, messages: list[dict[str, Any]], **kwargs: Any):
            yield LLMStreamEvent(
                kind=LLMEventKind.TURN_RESULT,
                result=LLMTurnResult(text="历史总结"),
            )

    class _FakeRepo:
        def __init__(self) -> None:
            self.snapshots: list[list[Any]] = []

        async def add_snapshot(self, session_id, messages, summary, through_message_id):
            self.snapshots.append(messages)

    repo = _FakeRepo()
    service = CompactService(session_repo=repo, llm=_FakeLLM(), threshold=10)
    history = [
        {"role": "user", "content": "查一下日志"},
        {
            "role": "assistant",
            "content": build_assistant_blocks(
                "", [ToolUse(id="t1", name="grep", input={"pattern": "error"})]
            ),
        },
        {"role": "tool", "content": _tool_content("t1", "grep", "X" * 600)},
    ]
    compacted = await service.maybe_compact(1, history, through_message_id=99)

    assert repo.snapshots == [compacted]
    # 快照内容是中立形状:纯 user 文本,无任何 provider wire 判别键。
    serialized = json.dumps(repo.snapshots[0], ensure_ascii=False)
    assert '"type"' not in serialized
    assert "tool_use" not in serialized
    assert compacted == [{"role": "user", "content": "[Conversation compressed.]\n\n历史总结"}]


def test_verification_trigger_matches_neutral_kind() -> None:
    """步 13:阈值判定按中立 kind 取 tool_use 块;旧 wire 的 type 键不再命中。"""
    neutral = [
        {
            "role": "assistant",
            "content": build_assistant_blocks(
                "", [ToolUse(id="t1", name="Write", input={"file_path": "user/service.py"})]
            ),
        }
    ]
    assessment = assess(neutral)
    assert assessment.should_verify is True
    assert assessment.changed_files == ("user/service.py",)

    legacy_wire = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Write",
                    "input": {"file_path": "user/service.py"},
                }
            ],
        }
    ]
    assert assess(legacy_wire).should_verify is False


def test_extraction_projects_neutral_assistant_text() -> None:
    """步 14:记忆抽取按 kind == "text" 取 assistant 文本,不再拿到空串。"""
    blocks = build_assistant_blocks(
        "结论在这", [ToolUse(id="t1", name="grep", input={})]
    )
    projected = MemoryExtractionService._project_message(
        _msg(11, "assistant", blocks)
    )
    assert projected.content == "结论在这"

    # 纯字符串形态(无块数组)的兼容路径保持不变。
    plain = MemoryExtractionService._project_message(_msg(12, "assistant", "纯文本"))
    assert plain.content == "纯文本"
