"""运行时取消分支:绑定执行时只回滚,终态与半截回答交给执行包装层;未绑定时保持原行为。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.event_bus import NullStreamBus
from runtime import agent as agent_module
from runtime.agent import AgentRuntime
from runtime.workflow import WorkflowRuntime

pytestmark = pytest.mark.anyio


class _Db:
    def __init__(self) -> None:
        self.rollbacks = 0
        self.commits = 0

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def commit(self) -> None:
        self.commits += 1


class _Service:
    async def get_required(self, session_id):
        return SimpleNamespace(id=session_id, title="标题", status="running")

    async def get_turn_required(self, turn_id):
        return SimpleNamespace(id=turn_id)


async def _noop(*args, **kwargs) -> None:
    return None


async def _cancelled(*args, **kwargs) -> None:
    raise asyncio.CancelledError


def _spy(marked: list[str]):
    async def _mark(session_id, turn_id, turn_status) -> None:
        marked.append(turn_status)

    return _mark


@pytest.mark.parametrize(("owner", "expected"), [("owner-a", []), (None, ["interrupted"])])
async def test_agent_cancel_defers_terminal_state_when_bound(
    monkeypatch: pytest.MonkeyPatch, owner, expected
) -> None:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.db = _Db()
    runtime.session_service = _Service()
    runtime.bus = NullStreamBus()
    runtime.lease_owner = owner
    marked: list[str] = []
    monkeypatch.setattr(runtime, "_configure_agent", _noop)
    monkeypatch.setattr(runtime, "_apply_user_prompt_hooks", _noop)
    monkeypatch.setattr(runtime, "_run_llm_loop", _cancelled)
    monkeypatch.setattr(runtime, "_mark_abnormal_end", _spy(marked))
    monkeypatch.setattr(
        agent_module,
        "TaskService",
        lambda db, bus: SimpleNamespace(emit_snapshot=_noop),
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime._produce(1, uuid4(), "问题")

    assert marked == expected
    assert runtime.db.rollbacks == (1 if owner else 0)
    assert runtime.db.commits == 0


@pytest.mark.parametrize(("owner", "expected"), [("owner-a", []), (None, ["interrupted"])])
async def test_workflow_cancel_defers_terminal_state_when_bound(
    monkeypatch: pytest.MonkeyPatch, owner, expected
) -> None:
    runtime = WorkflowRuntime(_Db(), user_id=1, workspace_id=1)
    runtime.session_service = _Service()
    runtime.lease_owner = owner
    marked: list[str] = []

    async def _build(session_id, turn_id, workflow_id):
        return SimpleNamespace(run=_cancelled)

    monkeypatch.setattr(runtime, "_build_orchestrator", _build)
    monkeypatch.setattr(runtime, "_mark_abnormal_end", _spy(marked))

    with pytest.raises(asyncio.CancelledError):
        await runtime._produce(1, uuid4(), "问题", 9)

    assert marked == expected
    assert runtime.db.rollbacks == (1 if owner else 0)
