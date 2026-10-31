"""AgentRuntime 准入:建轮次与认领租约同一事务提交,提交之后才启动后台执行。"""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.errors import AgentException
from runtime import agent as agent_module
from runtime.agent import AgentRuntime

pytestmark = pytest.mark.anyio


class _Log:
    def __init__(self) -> None:
        self.calls: list[str] = []


class _Db:
    def __init__(self, log: _Log) -> None:
        self.log = log

    async def commit(self) -> None:
        self.log.calls.append("commit")


class _SessionService:
    def __init__(self, log: _Log, *, claim_conflict: bool = False) -> None:
        self.log = log
        self.turn = SimpleNamespace(id=uuid4())
        self.claim_conflict = claim_conflict
        self.claimed: tuple[Any, str] | None = None

    async def prepare_for_message(self, session_id, content, user_id, workspace_id, agent_id):
        self.log.calls.append("prepare")
        return SimpleNamespace(id=42)

    async def start_turn(self, session, user_id, workspace_id, content):
        self.log.calls.append("start_turn")
        return self.turn, SimpleNamespace(id=1)

    async def claim_turn_lease(self, turn_id, owner, lease_seconds):
        self.log.calls.append("claim")
        if self.claim_conflict:
            raise AgentException.message("该轮次已有执行中的任务", status_code=409)
        self.claimed = (turn_id, owner)
        return 5


def _runtime(log: _Log, service: _SessionService) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.db = _Db(log)
    runtime.user_id = 1
    runtime.workspace_id = 1
    runtime.llm = object()
    runtime.session_service = service
    return runtime


def _fake_launch(log: _Log, launched: list[dict[str, Any]]):
    async def _launch(**kwargs: Any) -> Any:
        log.calls.append("launch")
        launched.append(kwargs)
        return SimpleNamespace(turn_id=kwargs["turn_id"], start_offset=kwargs["start_offset"])

    return _launch


async def test_start_commits_turn_and_lease_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    log, launched = _Log(), []
    service = _SessionService(log)
    monkeypatch.setattr(agent_module, "launch_turn_execution", _fake_launch(log, launched))

    handle = await _runtime(log, service).start(None, "你好")

    assert log.calls == ["prepare", "start_turn", "claim", "commit", "launch"]
    assert handle.turn_id == service.turn.id and handle.start_offset == 5
    assert launched[0]["owner"] == service.claimed[1]
    assert launched[0]["session_id"] == 42


async def test_start_resume_claims_existing_turn_after_saving_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log, launched = _Log(), []
    service = _SessionService(log)
    runtime = _runtime(log, service)
    monkeypatch.setattr(agent_module, "launch_turn_execution", _fake_launch(log, launched))

    async def _prepare(session_id, request_id, kind, response_payload):
        log.calls.append("prepare_response")
        return service.turn.id

    monkeypatch.setattr(runtime, "prepare_interaction_response", _prepare)

    await runtime.start_resume(42, uuid4(), "tool_approval", {"decision": "approve"})

    assert log.calls == ["prepare_response", "claim", "commit", "launch"]
    assert service.claimed[0] == service.turn.id


async def test_start_resume_conflict_does_not_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    log, launched = _Log(), []
    service = _SessionService(log, claim_conflict=True)
    runtime = _runtime(log, service)
    monkeypatch.setattr(agent_module, "launch_turn_execution", _fake_launch(log, launched))

    async def _prepare(session_id, request_id, kind, response_payload):
        return service.turn.id

    monkeypatch.setattr(runtime, "prepare_interaction_response", _prepare)

    with pytest.raises(AgentException) as exc_info:
        await runtime.start_resume(42, uuid4(), "tool_approval", {"decision": "approve"})
    assert exc_info.value.status_code == 409
    assert launched == [] and "commit" not in log.calls
