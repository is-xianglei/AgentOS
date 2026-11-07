"""统一编排上下文的状态更新契约测试。"""

from uuid import uuid4

from core.event_bus import NullStreamBus
from core.events import ORCHESTRATOR_ACTOR
from runtime.orchestrator import OrchestrationContext


def _context() -> OrchestrationContext:
    turn_id = uuid4()
    return OrchestrationContext(
        run_id=turn_id,
        session_id=42,
        turn_id=turn_id,
        actor=ORCHESTRATOR_ACTOR,
        bus=NullStreamBus(),
    )


def test_context_update_is_isolated_from_caller_state() -> None:
    context = _context()
    state = {"members": {"1": "running"}}

    context.update(cursor="member:running", state_updates=state)
    state["members"]["1"] = "mutated-after-update"

    assert context.cursor == "member:running"
    assert context.revision == 1
    assert context.state == {"members": {"1": "running"}}
