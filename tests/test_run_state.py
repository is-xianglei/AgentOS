"""reducer:对每个切分点 k,fold(全部事件) == fold(k 处快照经 JSON 往返, 其后事件)。

事件全部用真实工厂构造并经 JSON 往返,形状与订阅方从 Stream 读到的一致。
"""

import json
from typing import Any

import pytest

from core.events import (
    ORCHESTRATOR_ACTOR,
    BlockType,
    ErrorInfo,
    InteractionInfo,
    RunEvent,
    RuntimeEvent,
    StreamEvent,
    ToolInfo,
    Usage,
    WorkflowPhase,
    WorkflowProgressInfo,
    WorkflowProgressStatus,
    subagent_actor,
)
from core.run_state import TurnState, fold, fold_all, restore

_SID = 7
_TURN = "turn-reducer"
_LEAD = ORCHESTRATOR_ACTOR


def _seq(*events: Any, start: int = 1) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for offset, event in enumerate(events, start=start):
        event.sequence = offset
        out.append(json.loads(json.dumps(event.to_dict(), ensure_ascii=False, default=str)))
    return out


def _text_block(actor: Any, index: int, *parts: str) -> list[Any]:
    return [
        StreamEvent.block_start(actor, _SID, index, BlockType.TEXT),
        *(StreamEvent.text_delta(actor, _SID, index, part) for part in parts),
        StreamEvent.block_stop(actor, _SID, index),
    ]


def _plain_text() -> list[dict[str, Any]]:
    return _seq(
        RunEvent.run_started(_TURN, _SID),
        RuntimeEvent.session_ready(_SID, "标题", "running"),
        StreamEvent.turn_start(_LEAD, _SID),
        *_text_block(_LEAD, 0, "你好", "，世界"),
        StreamEvent.turn_end(_LEAD, _SID, "end_turn", Usage(input_tokens=3, output_tokens=4)),
        RunEvent.run_end(_TURN, "completed"),
    )


def _thinking() -> list[dict[str, Any]]:
    return _seq(
        RunEvent.run_started(_TURN, _SID),
        StreamEvent.turn_start(_LEAD, _SID),
        StreamEvent.block_start(_LEAD, _SID, 0, BlockType.THINKING),
        StreamEvent.thinking_delta(_LEAD, _SID, 0, "先想"),
        StreamEvent.thinking_delta(_LEAD, _SID, 0, "再答"),
        StreamEvent.block_stop(_LEAD, _SID, 0),
        *_text_block(_LEAD, 1, "答案"),
        StreamEvent.turn_end(_LEAD, _SID, "end_turn", None),
        RunEvent.run_end(_TURN, "completed"),
    )


def _tool_call() -> list[dict[str, Any]]:
    tool = ToolInfo(id="tu-1", name="Read", input={"path": "a.md"})
    return _seq(
        RunEvent.run_started(_TURN, _SID),
        StreamEvent.turn_start(_LEAD, _SID),
        StreamEvent.block_start(_LEAD, _SID, 0, BlockType.TOOL_USE),
        StreamEvent.tool_use(_LEAD, _SID, 0, tool),
        StreamEvent.block_stop(_LEAD, _SID, 0),
        StreamEvent.tool_result(
            _LEAD, _SID, ToolInfo(id="tu-1", name="Read", output="内容", is_error=False)
        ),
        *_text_block(_LEAD, 1, "读完了"),
        StreamEvent.turn_end(_LEAD, _SID, "end_turn", None),
        RunEvent.run_end(_TURN, "completed"),
    )


def _interaction() -> list[dict[str, Any]]:
    """交互挂起后恢复:第二次执行的块 index 从 0 重新编号,tool_result 回挂到上一 segment。"""
    tool = ToolInfo(id="tu-ask", name="Bash", input={"cmd": "ls"})
    request = InteractionInfo(
        request_id="req-1", suspension_id="sus-1", kind="tool_approval", request_payload={"a": 1}
    )
    first = _seq(
        RunEvent.run_started(_TURN, _SID),
        StreamEvent.turn_start(_LEAD, _SID),
        *_text_block(_LEAD, 0, "需要执行命令"),
        StreamEvent.block_start(_LEAD, _SID, 1, BlockType.TOOL_USE),
        StreamEvent.tool_use(_LEAD, _SID, 1, tool),
        StreamEvent.block_stop(_LEAD, _SID, 1),
        StreamEvent.interaction_request(_LEAD, _SID, request),
        RunEvent.run_end(_TURN, "awaiting_interaction"),
    )
    second = _seq(
        RunEvent.run_started(_TURN, _SID),
        RuntimeEvent.session_ready(_SID, "标题", "running"),
        StreamEvent.tool_result(
            _LEAD, _SID, ToolInfo(id="tu-ask", name="Bash", output="ok", is_error=False)
        ),
        StreamEvent.turn_start(_LEAD, _SID),
        *_text_block(_LEAD, 0, "命令完成"),
        StreamEvent.turn_end(_LEAD, _SID, "end_turn", None),
        RunEvent.run_end(_TURN, "completed"),
        start=len(first) + 70,  # 预留未用的 offset 留下间隙,offset 不要求连续稠密
    )
    return first + second


def _subagent() -> list[dict[str, Any]]:
    sub = subagent_actor("explore", run_id="run-9", task="找文件")
    return _seq(
        RunEvent.run_started(_TURN, _SID),
        StreamEvent.turn_start(_LEAD, _SID),
        StreamEvent.block_start(_LEAD, _SID, 0, BlockType.TOOL_USE),
        StreamEvent.tool_use(_LEAD, _SID, 0, ToolInfo(id="tu-a", name="Agent", input={})),
        StreamEvent.block_stop(_LEAD, _SID, 0),
        StreamEvent.turn_start(sub, _SID),
        *_text_block(sub, 0, "子代理", "报告"),
        StreamEvent.turn_end(sub, _SID, "end_turn", None),
        StreamEvent.tool_result(
            _LEAD, _SID, ToolInfo(id="tu-a", name="Agent", output="报告", is_error=False)
        ),
        StreamEvent.turn_end(_LEAD, _SID, "end_turn", None),
        RunEvent.run_end(_TURN, "completed"),
    )


def _progress(phase: WorkflowPhase, status: WorkflowProgressStatus, agent_id: int | None) -> Any:
    info = WorkflowProgressInfo(phase=phase, status=status, agent_id=agent_id, member_name="成员")
    return StreamEvent.workflow_progress(_LEAD, _SID, info)


def _parallel_workflow() -> list[dict[str, Any]]:
    a = subagent_actor("general-purpose", run_id="run-a", agent_id=1)
    b = subagent_actor("general-purpose", run_id="run-b", agent_id=2)
    running, done = WorkflowProgressStatus.RUNNING, WorkflowProgressStatus.DONE
    member, merging = WorkflowPhase.MEMBER, WorkflowPhase.MERGING
    return _seq(
        RunEvent.run_started(_TURN, _SID),
        StreamEvent.turn_start(_LEAD, _SID),
        _progress(member, running, 1),
        _progress(member, running, 2),
        StreamEvent.turn_start(a, _SID),
        StreamEvent.turn_start(b, _SID),
        StreamEvent.block_start(a, _SID, 0, BlockType.TEXT),
        StreamEvent.block_start(b, _SID, 0, BlockType.TEXT),
        StreamEvent.text_delta(b, _SID, 0, "乙"),
        StreamEvent.text_delta(a, _SID, 0, "甲"),
        StreamEvent.block_stop(a, _SID, 0),
        StreamEvent.block_stop(b, _SID, 0),
        _progress(member, done, 2),
        _progress(member, done, 1),
        _merging_event(merging, running),
        _merging_event(merging, done),
        *_text_block(_LEAD, 0, "汇总"),
        StreamEvent.turn_end(_LEAD, _SID, "end_turn", None),
        RunEvent.run_end(_TURN, "completed"),
    )


def _merging_event(phase: WorkflowPhase, status: WorkflowProgressStatus) -> Any:
    return StreamEvent.workflow_progress(_LEAD, _SID, WorkflowProgressInfo(phase, status))


def _tasks_and_error() -> list[dict[str, Any]]:
    return _seq(
        RunEvent.run_started(_TURN, _SID),
        RuntimeEvent.task_snapshot(_SID, [], "baseline"),
        StreamEvent.turn_start(_LEAD, _SID),
        RuntimeEvent.task_snapshot(_SID, [{"id": 1, "subject": "任务一"}], "created"),
        *_text_block(_LEAD, 0, "半截"),
        StreamEvent.error_event(
            _LEAD, _SID, ErrorInfo(code="LLM_FAILED", message="模型失败", fatal=True)
        ),
        RunEvent.run_end(_TURN, "failed"),
    )


SEQUENCES = {
    "plain_text": _plain_text,
    "thinking": _thinking,
    "tool_call": _tool_call,
    "interaction": _interaction,
    "subagent": _subagent,
    "parallel_workflow": _parallel_workflow,
    "tasks_and_error": _tasks_and_error,
}


def _roundtrip(state: TurnState) -> TurnState:
    return TurnState.from_dict(json.loads(json.dumps(state.to_dict(), ensure_ascii=False)))


@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_snapshot_at_every_split_point_equals_full_fold(name: str) -> None:
    events = SEQUENCES[name]()
    full = fold_all(TurnState(), events)

    assert _roundtrip(full) == full
    for k in range(len(events) + 1):
        snapshot = _roundtrip(fold_all(TurnState(), events[:k]))
        assert fold_all(snapshot, events[k:]) == full, f"切分点 {k}"
        # restore 与订阅方一致:projection 之后还带着已折叠过的事件,也不会重复折叠。
        assert restore(snapshot.to_dict(), events) == full, f"restore 切分点 {k}"


def test_plain_text_state_shape() -> None:
    state = fold_all(TurnState(), _plain_text()).to_dict()

    assert state["status"] == "completed" and state["truncated"] is False
    assert state["session"]["title"] == "标题"
    (actor,) = state["actors"]
    assert actor["in_turn"] is False and actor["usage"]["output_tokens"] == 4
    assert [(b["kind"], b["text"], b["closed"]) for b in actor["blocks"]] == [
        ("text", "你好，世界", True)
    ]


def test_interaction_resume_keeps_segments_and_attaches_result() -> None:
    events = _interaction()
    suspended = fold_all(TurnState(), events[:10])
    assert suspended.status == "awaiting_interaction"
    assert suspended.interaction is not None and suspended.interaction["kind"] == "tool_approval"

    state = fold_all(TurnState(), events)
    assert state.status == "completed" and state.interaction is None
    blocks = state.actors[0].blocks
    assert [(b.segment, b.index, b.kind) for b in blocks] == [
        (1, 0, "text"),
        (1, 1, "tool_use"),
        (2, 0, "text"),
    ]
    assert blocks[1].result is not None and blocks[1].result["output"] == "ok"
    assert state.execution_start_offset == events[10]["sequence"]


def test_parallel_members_and_workflow_nodes() -> None:
    state = fold_all(TurnState(), _parallel_workflow())

    texts = {actor.actor.get("agent_id"): actor.blocks[0].text for actor in state.actors}
    assert texts == {None: "汇总", 1: "甲", 2: "乙"}
    assert [(n["phase"], n.get("agent_id"), n["status"]) for n in state.workflow] == [
        ("member", 1, "done"),
        ("member", 2, "done"),
        ("merging", None, "done"),
    ]


def test_tasks_and_error_are_recorded() -> None:
    state = fold_all(TurnState(), _tasks_and_error())

    assert state.status == "failed"
    assert state.tasks == [{"id": 1, "subject": "任务一"}]
    assert [item["error"]["code"] for item in state.errors] == ["LLM_FAILED"]
    assert state.actors[0].blocks[0].text == "半截"


def test_duplicate_and_late_events_are_ignored() -> None:
    events = _plain_text()
    state = fold_all(TurnState(), events)

    assert fold(state, events[3]) == state
    stale_end = {"type": "run_end", "turn_id": _TURN, "status": "interrupted", "sequence": 2}
    assert fold(state, stale_end) == state


def test_missing_stream_head_marks_truncated_and_keeps_text() -> None:
    events = _plain_text()
    state = fold_all(TurnState(), events[5:])

    assert state.truncated is True
    assert state.actors[0].blocks[0].text == "，世界"
    assert state.status == "completed"
