"""统一编排上下文的快照与恢复契约测试。"""

from uuid import uuid4

import pytest

from core.event_bus import NullStreamBus
from core.events import ORCHESTRATOR_ACTOR
from runtime.orchestrator import OrchestrationCheckpoint, OrchestrationContext


def _context() -> OrchestrationContext:
    turn_id = uuid4()
    return OrchestrationContext(
        run_id=turn_id,
        session_id=42,
        turn_id=turn_id,
        actor=ORCHESTRATOR_ACTOR,
        bus=NullStreamBus(),
    )


def test_context_update_and_snapshot_are_isolated() -> None:
    context = _context()
    state = {"members": {"1": "running"}}

    context.update(cursor="member:running", state_updates=state)
    state["members"]["1"] = "mutated-after-update"

    snapshot = context.snapshot()
    assert snapshot.cursor == "member:running"
    assert snapshot.revision == 1
    assert snapshot.state == {"members": {"1": "running"}}

    encoded = snapshot.to_dict()
    encoded["state"]["members"]["1"] = "mutated-after-snapshot"
    assert context.state == {"members": {"1": "running"}}


def test_checkpoint_round_trip_restores_context() -> None:
    context = _context()
    context.update(
        cursor="member:done",
        state_updates={"last_member": {"agent_id": 7, "status": "done"}},
    )

    restored_checkpoint = OrchestrationCheckpoint.from_dict(context.snapshot().to_dict())
    restored = _context()
    restored.run_id = context.run_id
    restored.session_id = context.session_id
    restored.turn_id = context.turn_id
    restored.restore(restored_checkpoint)

    assert restored.cursor == "member:done"
    assert restored.state == {"last_member": {"agent_id": 7, "status": "done"}}
    assert restored.revision == 1


def test_restore_rejects_checkpoint_from_another_run() -> None:
    context = _context()
    other = _context()

    with pytest.raises(ValueError, match="快照与当前运行上下文不匹配"):
        context.restore(other.snapshot())
