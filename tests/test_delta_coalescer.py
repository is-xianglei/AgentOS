"""DeltaCoalescer:合并规则(同 actor run_id 同 block 的相邻增量)与刷新时机。"""

from core.delta_coalescer import DELTA_FLUSH_CHARS, DELTA_FLUSH_INTERVAL_SECONDS, DeltaCoalescer
from core.events import ORCHESTRATOR_ACTOR, StreamEvent, ToolInfo, subagent_actor

_SID = 1
_LEAD = ORCHESTRATOR_ACTOR
_STEP = DELTA_FLUSH_INTERVAL_SECONDS / 10


def _text(index: int, text: str, actor=_LEAD) -> StreamEvent:
    return StreamEvent.text_delta(actor, _SID, index, text)


def _texts(events) -> list[tuple[str, str | None]]:
    return [(event.type.value, event.text) for event in events]


def test_first_delta_is_emitted_immediately_then_buffered() -> None:
    coalescer = DeltaCoalescer(clock=lambda: 0.0)

    assert _texts(coalescer.feed(_text(0, "你"), now=0.0)) == [("text_delta", "你")]
    assert coalescer.feed(_text(0, "好"), now=_STEP) == []
    assert coalescer.feed(_text(0, "呀"), now=2 * _STEP) == []
    assert coalescer.deadline() == DELTA_FLUSH_INTERVAL_SECONDS
    assert _texts(coalescer.drain(now=3 * _STEP)) == [("text_delta", "好呀")]


def test_interval_and_size_trigger_flush() -> None:
    coalescer = DeltaCoalescer()
    coalescer.feed(_text(0, "a"), now=0.0)

    assert coalescer.feed(_text(0, "b"), now=_STEP) == []
    flushed = coalescer.feed(_text(0, "c"), now=DELTA_FLUSH_INTERVAL_SECONDS + _STEP)
    assert _texts(flushed) == [("text_delta", "bc")]

    big = "x" * DELTA_FLUSH_CHARS
    assert _texts(coalescer.feed(_text(0, big), now=DELTA_FLUSH_INTERVAL_SECONDS + 2 * _STEP)) == [
        ("text_delta", big)
    ]


def test_other_event_flushes_buffer_first_in_order() -> None:
    coalescer = DeltaCoalescer()
    coalescer.feed(_text(0, "a"), now=0.0)
    coalescer.feed(_text(0, "b"), now=_STEP)
    stop = StreamEvent.block_stop(_LEAD, _SID, 0)

    out = coalescer.feed(stop, now=2 * _STEP)

    assert [event.type.value for event in out] == ["text_delta", "block_stop"]
    assert out[0].text == "b" and out[1] is stop


def test_only_same_actor_run_and_block_are_merged() -> None:
    coalescer = DeltaCoalescer()
    sub_a = subagent_actor("explore", run_id="run-a")
    sub_b = subagent_actor("explore", run_id="run-b")
    coalescer.feed(_text(0, "lead"), now=0.0)

    assert coalescer.feed(_text(0, "x", sub_a), now=_STEP) == []
    out_b = coalescer.feed(_text(0, "y", sub_b), now=2 * _STEP)
    out_index = coalescer.feed(_text(1, "z", sub_b), now=3 * _STEP)
    thinking = StreamEvent.thinking_delta(sub_b, _SID, 1, "想")
    out_kind = coalescer.feed(thinking, now=4 * _STEP)

    assert [(e.actor.run_id, e.text) for e in out_b] == [("run-a", "x")]
    assert [(e.actor.run_id, e.block.index, e.text) for e in out_index] == [("run-b", 0, "y")]
    assert [(e.type.value, e.text) for e in out_kind] == [("text_delta", "z")]


def test_merged_event_keeps_identity_and_drops_empty_text() -> None:
    coalescer = DeltaCoalescer()
    sub = subagent_actor("explore", run_id="run-a", agent_id=3)
    coalescer.feed(StreamEvent.text_delta(sub, _SID, 2, "a"), now=0.0)
    coalescer.feed(StreamEvent.text_delta(sub, _SID, 2, "b"), now=_STEP)

    assert coalescer.feed(StreamEvent.text_delta(sub, _SID, 2, ""), now=2 * _STEP) == []
    (merged,) = coalescer.flush_due(now=DELTA_FLUSH_INTERVAL_SECONDS + _STEP)
    assert merged.actor == sub and merged.block.index == 2
    assert merged.text == "b" and merged.sequence is None
    assert coalescer.flush_due(now=10.0) == []


def test_tool_events_pass_through_untouched() -> None:
    coalescer = DeltaCoalescer()
    tool = StreamEvent.tool_use(_LEAD, _SID, 0, ToolInfo(id="t", name="Read", input={}))

    assert coalescer.feed(tool, now=0.0) == [tool]
    assert coalescer.deadline() is None
