"""Actor.agent_id 与 workflow_progress 事件的序列化契约测试(09-04 AC4 / AC5)。

内置子代理 / 主循环 actor 的 agent_id 恒为 None,序列化不得出现该键(事件形状零变化);
workflow 成员 actor 在 full 与 compact 两种形态都要带 agent_id,前端才能按增量事件归节点。
末段跑真实 SubAgentRunner.run(ReAct 循环打桩),验证 spec.agent_id 透传到运行记录与 actor。
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.events import (
    ORCHESTRATOR_ACTOR,
    EventType,
    StreamEvent,
    WorkflowPhase,
    WorkflowProgressInfo,
    WorkflowProgressStatus,
    subagent_actor,
    teammate_actor,
)
from llm.types import LLMTurnResult
from tools.subagents.definition import AgentType, SubAgentSpec
from tools.subagents.registry import get_subagent_spec

_SID = 42


def test_builtin_subagent_actor_has_no_agent_id_key() -> None:
    actor = subagent_actor("explore", run_id="run-1")
    assert actor.agent_id is None
    assert actor.to_dict_compact() == {"role": "subagent", "name": "explore", "run_id": "run-1"}
    assert actor.to_dict_full() == {"role": "subagent", "name": "explore", "run_id": "run-1"}


def test_orchestrator_and_teammate_actor_shape_unchanged() -> None:
    assert ORCHESTRATOR_ACTOR.to_dict_full() == {"role": "orchestrator", "name": "orchestrator"}
    assert ORCHESTRATOR_ACTOR.to_dict_compact() == {"role": "orchestrator", "name": "orchestrator"}
    mate = teammate_actor("拆弹员", run_id="run-9", task="拆")
    assert "agent_id" not in mate.to_dict_full()
    assert "agent_id" not in mate.to_dict_compact()


def test_workflow_member_actor_carries_agent_id_in_both_shapes() -> None:
    actor = subagent_actor("wf-x", run_id="run-2", task="做事", agent_id=7)
    assert actor.to_dict_full() == {
        "role": "subagent",
        "name": "wf-x",
        "run_id": "run-2",
        "task": "做事",
        "agent_id": 7,
    }
    assert actor.to_dict_compact() == {
        "role": "subagent",
        "name": "wf-x",
        "run_id": "run-2",
        "agent_id": 7,
    }


def test_agent_id_flows_into_increment_event_actor() -> None:
    """增量事件(text_delta)走 compact 形态,也必须带 agent_id。"""
    actor = subagent_actor("wf-x", run_id="run-2", agent_id=7)
    event = StreamEvent.text_delta(actor, _SID, 0, "字").to_dict()
    assert event["actor"]["agent_id"] == 7


def test_workflow_progress_event_serialization() -> None:
    info = WorkflowProgressInfo(
        phase=WorkflowPhase.MEMBER,
        status=WorkflowProgressStatus.RUNNING,
        agent_id=2,
        member_name="审稿人",
        ordinal=2,
        total=3,
    )
    event = StreamEvent.workflow_progress(ORCHESTRATOR_ACTOR, _SID, info)
    assert event.type is EventType.WORKFLOW_PROGRESS
    assert event.to_dict() == {
        "type": "workflow_progress",
        "session_id": _SID,
        "actor": {"role": "orchestrator", "name": "orchestrator"},
        "progress": {
            "phase": "member",
            "status": "running",
            "agent_id": 2,
            "member_name": "审稿人",
            "ordinal": 2,
            "total": 3,
        },
    }


def test_workflow_progress_info_omits_none_fields() -> None:
    routing_done = WorkflowProgressInfo(
        phase=WorkflowPhase.ROUTING,
        status=WorkflowProgressStatus.DONE,
        agent_id=3,
        member_name="总结者",
    )
    assert routing_done.to_dict() == {
        "phase": "routing",
        "status": "done",
        "agent_id": 3,
        "member_name": "总结者",
    }
    merging = WorkflowProgressInfo(
        phase=WorkflowPhase.MERGING, status=WorkflowProgressStatus.FAILED
    )
    assert merging.to_dict() == {"phase": "merging", "status": "failed"}


def test_member_progress_requires_agent_id() -> None:
    """member 阶段缺 agent_id 在构造期即被拦下,不会流到前端状态机;有 member_name 也救不回。"""
    with pytest.raises(ValueError, match="member 阶段"):
        WorkflowProgressInfo(phase=WorkflowPhase.MEMBER, status=WorkflowProgressStatus.RUNNING)
    with pytest.raises(ValueError, match="member 阶段"):
        WorkflowProgressInfo(
            phase=WorkflowPhase.MEMBER,
            status=WorkflowProgressStatus.FAILED,
            member_name="审稿人",
        )


def test_member_failed_progress_allows_missing_member_name() -> None:
    """配置期失败(成员停用/软删/不存在)拿不到 Agent 名:只带 agent_id 合法,序列化不出 member_name 键。"""
    failed = WorkflowProgressInfo(
        phase=WorkflowPhase.MEMBER, status=WorkflowProgressStatus.FAILED, agent_id=2
    )
    assert failed.to_dict() == {"phase": "member", "status": "failed", "agent_id": 2}


def test_non_member_progress_rejects_ordinal_and_total() -> None:
    """routing / merging 阶段不得携带 Sequential 专用的 ordinal / total。"""
    with pytest.raises(ValueError, match="routing 阶段"):
        WorkflowProgressInfo(
            phase=WorkflowPhase.ROUTING, status=WorkflowProgressStatus.RUNNING, ordinal=1
        )
    with pytest.raises(ValueError, match="merging 阶段"):
        WorkflowProgressInfo(
            phase=WorkflowPhase.MERGING, status=WorkflowProgressStatus.DONE, total=3
        )


# ---------- SubAgentRunner.run 的 agent_id 透传(AC5) ----------


@dataclass
class _FakeRun:
    id: int = 11


@dataclass
class _RecordingRunRepo:
    """记录 create 收到的关键字参数;succeed / fail 为空操作。"""

    create_kwargs: list[dict[str, Any]] = field(default_factory=list)

    async def create(
        self, session_id: int, member_name: str, prompt: str, **kwargs: Any
    ) -> _FakeRun:
        self.create_kwargs.append({"member_name": member_name, **kwargs})
        return _FakeRun()

    async def succeed(self, run: _FakeRun, report: str) -> _FakeRun:
        return run

    async def fail(self, run: _FakeRun, error_message: str) -> _FakeRun:
        return run


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    async def emit(self, event: StreamEvent) -> dict[str, Any]:
        self.events.append(event)
        return {}


class _FakeDb:
    async def commit(self) -> None:
        return None


class _FakeAssembler:
    """替身装配器:只提供 turn_end 所需的 last_result。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.last_result = LLMTurnResult(text="报告", stop_reason="end_turn")


def _stub_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, _RecordingRunRepo, _RecordingBus]:
    """构造跳过 LLM / 工具 / 记忆 / 权限依赖的运行器,只保留建记录与发事件两步。

    runtime.subagent 作为首个被导入的模块会触发存量循环导入(team.service → session.service
    → tools → task.service → session.service),故在 tools 包已加载后再局部导入。
    """
    from runtime.subagent import SubAgentRunner

    runner = SubAgentRunner(_FakeDb(), bus=_RecordingBus(), llm=object())  # type: ignore[arg-type]
    repo = _RecordingRunRepo()
    runner.run_repo = repo  # type: ignore[assignment]

    async def _fake_loop(*args: Any, **kwargs: Any) -> tuple[str, int]:
        return "报告", 1

    monkeypatch.setattr(runner, "_run_react_loop", _fake_loop)
    monkeypatch.setattr(runner, "_resolve_tools", lambda spec, parent: object())
    monkeypatch.setattr("runtime.subagent.ToolService", lambda db, registry: object())
    monkeypatch.setattr("runtime.subagent.TurnEventAssembler", _FakeAssembler)
    return runner, repo, runner.bus  # type: ignore[return-value]


@pytest.mark.anyio
async def test_runner_passes_spec_agent_id_to_run_record_and_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, bus = _stub_runner(monkeypatch)
    spec = SubAgentSpec(
        agent_type=AgentType.GENERAL_PURPOSE,
        system_prompt="人格",
        display_name="wf-审稿人",
        agent_id=2,
        omit_inherited_memory=True,
    )

    report = await runner.run(_SID, "任务", spec)

    assert report == "报告"
    assert repo.create_kwargs == [{"member_name": "wf-审稿人", "agent_id": 2}]
    turn_start = bus.events[0].to_dict()
    assert turn_start["type"] == "turn_start"
    assert turn_start["actor"] == {
        "role": "subagent",
        "name": "wf-审稿人",
        "run_id": "run-11",
        "task": "任务",
        "agent_id": 2,
    }
    turn_end = bus.events[-1].to_dict()
    assert turn_end["actor"]["agent_id"] == 2


@pytest.mark.anyio
async def test_runner_keeps_builtin_spec_record_and_actor_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置 spec 走同一条路径:运行记录 agent_id 为 None,actor 序列化无 agent_id 键。"""
    runner, repo, bus = _stub_runner(monkeypatch)

    await runner.run(_SID, "找代码", get_subagent_spec(AgentType.EXPLORE))

    assert repo.create_kwargs == [{"member_name": "Explore", "agent_id": None}]
    assert bus.events[0].to_dict()["actor"] == {
        "role": "subagent",
        "name": "Explore",
        "run_id": "run-11",
        "task": "找代码",
    }
