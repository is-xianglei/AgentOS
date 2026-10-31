"""取消时还原主代理未落库消息的纯函数测试:按事件流构造 TurnState,不依赖数据库。"""

import json
from typing import Any

import pytest

from core.events import (
    ORCHESTRATOR_ACTOR,
    BlockType,
    InteractionInfo,
    RunEvent,
    StreamEvent,
    ToolInfo,
    subagent_actor,
)
from core.run_state import TurnState, fold_all, restore
from runtime.cancel_partial import CANCELLED_TOOL_OUTPUT, extract_unsaved_messages

_SID = 7
_LEAD = ORCHESTRATOR_ACTOR


class _Script:
    """按顺序给事件分配 sequence,产出与订阅方读到的 JSON 同形的 dict。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(self, event: Any) -> None:
        event.sequence = len(self.events) + 1
        self.events.append(json.loads(json.dumps(event.to_dict(), default=str)))

    def run_started(self) -> None:
        self.add(RunEvent.run_started("t", _SID))

    def run_end(self, status: str) -> None:
        self.add(RunEvent.run_end("t", status))

    def turn_start(self, actor=_LEAD) -> None:
        self.add(StreamEvent.turn_start(actor, _SID))

    def text(self, index: int, *chunks: str, close: bool = True, actor=_LEAD) -> None:
        self.add(StreamEvent.block_start(actor, _SID, index, BlockType.TEXT))
        for chunk in chunks:
            self.add(StreamEvent.text_delta(actor, _SID, index, chunk))
        if close:
            self.add(StreamEvent.block_stop(actor, _SID, index))

    def thinking(self, index: int, text: str, *, close: bool) -> None:
        self.add(StreamEvent.block_start(_LEAD, _SID, index, BlockType.THINKING))
        self.add(StreamEvent.thinking_delta(_LEAD, _SID, index, text))
        if close:
            self.add(StreamEvent.block_stop(_LEAD, _SID, index))

    def tool_use(self, index: int, tool_id: str, *, complete: bool = True) -> None:
        self.add(StreamEvent.block_start(_LEAD, _SID, index, BlockType.TOOL_USE))
        if complete:
            info = ToolInfo(id=tool_id, name="Read", input={"path": f"{tool_id}.txt"})
            self.add(StreamEvent.tool_use(_LEAD, _SID, index, info))
            self.add(StreamEvent.block_stop(_LEAD, _SID, index))

    def tool_result(self, tool_id: str, output: str, *, is_error: bool = False) -> None:
        info = ToolInfo(id=tool_id, name="Read", output=output, is_error=is_error)
        self.add(StreamEvent.tool_result(_LEAD, _SID, info))

    def state(self) -> TurnState:
        return fold_all(TurnState(), self.events)


def _contents(drafts) -> list[tuple[str, Any]]:
    return [(draft.role, draft.content) for draft in drafts]


def _tool(tool_id: str, output: str, *, is_error: bool = False) -> tuple[str, dict]:
    return (
        "tool",
        {
            "tool_use_id": tool_id,
            "tool_name": "Read",
            "input_args": {"path": f"{tool_id}.txt"},
            "output": output,
            "is_error": is_error,
        },
    )


def _use(tool_id: str) -> dict:
    return {"kind": "tool_use", "id": tool_id, "name": "Read", "input": {"path": f"{tool_id}.txt"}}


def _mid_text() -> _Script:
    script = _Script()
    script.run_started()
    script.turn_start()
    script.text(0, "已经写了", "一半", close=False)
    return script


def _mid_tool() -> _Script:
    """第一次调用的工具已完成,第二次调用的工具执行中被取消。"""
    script = _Script()
    script.run_started()
    script.turn_start()
    script.text(0, "先读文件")
    script.tool_use(1, "a")
    script.tool_result("a", "内容A")
    script.text(2, "再读一个")
    script.tool_use(3, "b")
    return script


def _parallel() -> _Script:
    """同一次响应里两个工具调用,第一个已完成、第二个执行中被取消。"""
    script = _Script()
    script.run_started()
    script.turn_start()
    script.text(0, "并行读取")
    script.tool_use(1, "a")
    script.tool_use(2, "b")
    script.tool_result("a", "内容A")
    return script


def _half_tool_use() -> _Script:
    script = _Script()
    script.run_started()
    script.turn_start()
    script.text(0, "准备调用")
    script.tool_use(1, "a", complete=False)
    return script


def test_cancel_mid_text_keeps_streamed_text() -> None:
    drafts = extract_unsaved_messages(_mid_text().state())

    assert _contents(drafts) == [("assistant", [{"kind": "text", "text": "已经写了一半"}])]


def test_cancel_during_tool_execution_pairs_every_tool_use() -> None:
    drafts = extract_unsaved_messages(_mid_tool().state())

    assert _contents(drafts) == [
        ("assistant", [{"kind": "text", "text": "先读文件"}, _use("a")]),
        _tool("a", "内容A"),
        ("assistant", [{"kind": "text", "text": "再读一个"}, _use("b")]),
        _tool("b", CANCELLED_TOOL_OUTPUT, is_error=True),
    ]


def test_parallel_tool_uses_stay_in_one_assistant_message() -> None:
    drafts = extract_unsaved_messages(_parallel().state())

    assert _contents(drafts) == [
        ("assistant", [{"kind": "text", "text": "并行读取"}, _use("a"), _use("b")]),
        _tool("a", "内容A"),
        _tool("b", CANCELLED_TOOL_OUTPUT, is_error=True),
    ]


def test_half_streamed_tool_use_is_dropped() -> None:
    drafts = extract_unsaved_messages(_half_tool_use().state())

    assert _contents(drafts) == [("assistant", [{"kind": "text", "text": "准备调用"}])]


def test_no_new_text_creates_no_message() -> None:
    script = _Script()
    script.run_started()
    script.turn_start()
    script.thinking(0, "思考到一半", close=False)
    script.text(1, "  \n", close=False)
    script.tool_use(2, "a", complete=False)

    assert extract_unsaved_messages(script.state()) == []
    empty = _Script()
    empty.run_started()
    assert extract_unsaved_messages(empty.state()) == []


def test_thinking_is_never_saved() -> None:
    script = _Script()
    script.run_started()
    script.turn_start()
    script.thinking(0, "完整的思考", close=True)
    script.text(1, "正文", close=False)
    script.thinking(2, "没想完", close=False)

    drafts = extract_unsaved_messages(script.state())

    assert _contents(drafts) == [("assistant", [{"kind": "text", "text": "正文"}])]


def _awaiting_then_resumed() -> _Script:
    """第一次执行挂起等待交互(已提交),恢复执行写入工具结果后继续输出。"""
    script = _Script()
    script.run_started()
    script.turn_start()
    script.text(0, "需要确认")
    script.tool_use(1, "q")
    info = InteractionInfo(
        request_id="r", suspension_id="s", kind="tool_approval", schema_version=1,
        request_payload={},
    )
    script.add(StreamEvent.interaction_request(_LEAD, _SID, info))
    script.run_end("awaiting_interaction")
    script.run_started()
    script.tool_result("q", "已批准")
    return script


def test_resume_excludes_blocks_of_committed_execution() -> None:
    resumed = _awaiting_then_resumed()
    # 恢复后尚未开始新的 LLM 调用就被取消:挂起前的块与恢复时的工具结果都已提交。
    assert extract_unsaved_messages(resumed.state()) == []

    resumed.turn_start()
    resumed.text(0, "批准后继续", close=False)
    drafts = extract_unsaved_messages(resumed.state())
    assert _contents(drafts) == [("assistant", [{"kind": "text", "text": "批准后继续"}])]


def test_only_orchestrator_blocks_are_restored() -> None:
    script = _Script()
    script.run_started()
    script.turn_start()
    script.text(0, "派发子代理")
    sub = subagent_actor("Explore", run_id="run-1", task="探索")
    script.turn_start(sub)
    script.text(0, "子代理的输出", close=False, actor=sub)

    drafts = extract_unsaved_messages(script.state())

    assert _contents(drafts) == [("assistant", [{"kind": "text", "text": "派发子代理"}])]


def test_projection_plus_tail_matches_full_fold() -> None:
    script = _mid_tool()
    head, tail = script.events[:6], script.events[6:]
    projection = fold_all(TurnState(), head).to_dict()

    restored = restore(json.loads(json.dumps(projection)), tail)

    assert extract_unsaved_messages(restored) == extract_unsaved_messages(script.state())


def test_legacy_projection_without_sequence_restores_nothing() -> None:
    data = _mid_text().state().to_dict()
    for actor in data["actors"]:
        for block in actor["blocks"]:
            block.pop("sequence")

    assert extract_unsaved_messages(TurnState.from_dict(data)) == []


@pytest.mark.parametrize("factory", [_mid_text, _mid_tool, _parallel, _half_tool_use])
def test_restored_history_encodes_for_both_providers(factory, assert_replayable) -> None:
    drafts = extract_unsaved_messages(factory().state())
    messages = [
        {"role": "user", "content": "第一问"},
        *({"role": draft.role, "content": draft.content} for draft in drafts),
        {"role": "user", "content": "第二问"},
    ]

    assert_replayable(messages)
