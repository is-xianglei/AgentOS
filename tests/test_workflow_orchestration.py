"""Workflow 编排运行时与发消息入口的单元测试(AC3–AC10)。

全程 Fake/monkeypatch,不碰真实数据库与 LLM:
- 三种编排器的输入形状、并发隔离、汇总/路由与失败语义;
- SessionService.prepare_for_message 的 Workflow 锁定规则与 effective_workflow_id 判据。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest
from pydantic import BaseModel

from agent.service import AgentRuntimeConfig
from core.errors import AgentException
from core.events import BlockType, EventType
from llm.types import LLMEventKind, LLMStreamEvent, LLMTurnResult
from runtime.workflow import (
    MEMBER_DISPLAY_NAME_MAX_LENGTH,
    LlmRoutingOrchestrator,
    ParallelOrchestrator,
    SequentialOrchestrator,
    WorkflowRuntime,
    build_member_display_name,
)
from session import api as session_api
from session.schemas import SessionSendMessageRequest
from session.service import SessionService
from workflow.service import WorkflowRunMember

pytestmark = pytest.mark.anyio

_SESSION_ID = 42
_WORKFLOW_ID = 9
_USER_ID = 1
_WORKSPACE_ID = 1
_QUESTION = "帮我评估这个方案"


# ---------- 替身 ----------


def _config(agent_id: int, name: str, description: str | None = None) -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        agent_id=agent_id,
        system_prompt=f"你是{name}",
        model_name=None,
        tool_runtime_names=(),
        skill_ids=(),
        skill_names=(),
        name=name,
        description=description,
    )


class _RecordingBus:
    """替身 StreamBus:只记录 emit 的事件对象,不入队。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> dict[str, Any]:
        self.events.append(event)
        return {}


class _FakeSessionScope:
    """替身 AsyncSessionLocal:进出上下文都不建立数据库连接,每次实例唯一以便断言隔离。"""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@dataclass
class _MemberCall:
    display_name: str
    prompt: str
    db: Any


class _Harness:
    """把 AgentService / SubAgentRunner 替身接到 runtime.workflow 模块上,并记录调用。"""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        configs: dict[int, AgentRuntimeConfig],
        *,
        outputs: dict[str, str] | None = None,
        failing: set[str] = frozenset(),
        disabled_ids: set[int] = frozenset(),
        rendezvous: int | None = None,
    ) -> None:
        """rendezvous 非空时,执行器替身进入后要等齐该数量的成员才返回:串行实现会在此死等。"""
        self.calls: list[_MemberCall] = []
        self.config_requests: list[int] = []
        self.timeouts = 0
        outputs = outputs or {}
        harness = self
        arrived = 0
        all_arrived = asyncio.Event()

        class _FakeAgentService:
            def __init__(self, db: object) -> None:
                pass

            async def get_runtime_config(
                self, workspace_id: int, agent_id: int
            ) -> AgentRuntimeConfig:
                harness.config_requests.append(agent_id)
                if agent_id in disabled_ids:
                    raise AgentException.message("Agent已停用", status_code=409)
                return configs[agent_id]

        class _FakeSubAgentRunner:
            def __init__(self, db: object, bus: object | None = None) -> None:
                self.db = db

            async def run(self, session_id: int, prompt: str, spec: Any, **kwargs: Any) -> str:
                nonlocal arrived
                harness.calls.append(_MemberCall(spec.display_name, prompt, self.db))
                if rendezvous is not None:
                    arrived += 1
                    if arrived >= rendezvous:
                        all_arrived.set()
                    # 超时防死锁:串行实现下第 1 个成员永远等不到其余成员,在此超时失败。
                    try:
                        await asyncio.wait_for(all_arrived.wait(), timeout=1.0)
                    except TimeoutError:
                        harness.timeouts += 1
                        raise
                if spec.display_name in failing:
                    raise RuntimeError("成员内部故障：含敏感堆栈细节")
                return outputs.get(spec.display_name, f"{spec.display_name}的结论")

        monkeypatch.setattr("runtime.workflow.AgentService", _FakeAgentService)
        monkeypatch.setattr("runtime.workflow.SubAgentRunner", _FakeSubAgentRunner)
        monkeypatch.setattr("runtime.workflow.AsyncSessionLocal", _FakeSessionScope)

    def names(self) -> list[str]:
        return [call.display_name for call in self.calls]


class _FakeLLM:
    """LLM 替身:stream() 产出一段固定文本,complete_structured() 返回预设的路由选择。"""

    def __init__(self, *, merged_text: str = "汇总结论", choice: int | None = None) -> None:
        self.merged_text = merged_text
        self.choice = choice
        self.stream_prompts: list[str] = []
        self.structured_prompts: list[str] = []
        self.fail_structured = False

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system_prompt: Any = None,
        tools: Any = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        self.stream_prompts.append(messages[-1]["content"])
        yield LLMStreamEvent(LLMEventKind.BLOCK_START, block_ref=0, block_type=BlockType.TEXT)
        yield LLMStreamEvent(LLMEventKind.TEXT_DELTA, block_ref=0, text=self.merged_text)
        yield LLMStreamEvent(LLMEventKind.BLOCK_STOP, block_ref=0)
        yield LLMStreamEvent(
            LLMEventKind.TURN_RESULT,
            result=LLMTurnResult(text=self.merged_text, stop_reason="end_turn"),
        )

    async def complete_structured[T: BaseModel](
        self,
        messages: list[dict[str, Any]],
        output_format: type[T],
        *,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int = 256,
        timeout: float | None = None,
    ) -> T:
        self.structured_prompts.append(messages[-1]["content"])
        if self.fail_structured:
            raise RuntimeError("路由模型不可用")
        return output_format(agent_id=self.choice)


def _members(*agent_ids: int) -> tuple[WorkflowRunMember, ...]:
    return tuple(
        WorkflowRunMember(agent_id=agent_id, sort_order=index)
        for index, agent_id in enumerate(agent_ids)
    )


def _deps(members: tuple[WorkflowRunMember, ...], bus: _RecordingBus | None = None) -> dict:
    return {
        "db": object(),
        "bus": bus or _RecordingBus(),
        "user_id": _USER_ID,
        "workspace_id": _WORKSPACE_ID,
        "session_id": _SESSION_ID,
        "turn_id": uuid4(),
        "workflow_id": _WORKFLOW_ID,
        "members": members,
    }


_CONFIGS = {
    1: _config(1, "分析师", "擅长拆解问题与列出风险"),
    2: _config(2, "审稿人", "擅长审阅方案并给出修改意见"),
    3: _config(3, "总结者", "擅长归纳结论"),
}


# ---------- Sequential(AC3 / AC7 / AC10) ----------


async def test_sequential_passes_original_question_and_previous_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3:第 1 个成员只收到原始提问;第 N 个同时收到原始提问与第 N-1 个成员输出。"""
    harness = _Harness(monkeypatch, _CONFIGS)
    orchestrator = SequentialOrchestrator(**_deps(_members(1, 2, 3)))

    result = await orchestrator.run(_QUESTION)

    assert harness.names() == ["wf-分析师", "wf-审稿人", "wf-总结者"]
    first, second, third = harness.calls
    assert first.prompt == _QUESTION
    assert _QUESTION in second.prompt
    assert "wf-分析师的结论" in second.prompt
    # 不累积更早的输出:第 3 个只带第 2 个的结果,不带第 1 个的。
    assert "wf-审稿人的结论" in third.prompt
    assert "wf-分析师的结论" not in third.prompt
    assert result.text == "wf-总结者的结论"
    # 最终文本以编排者文本块推送。
    types = [event.type for event in orchestrator.bus.events]
    assert types == [EventType.BLOCK_START, EventType.TEXT_DELTA, EventType.BLOCK_STOP]


async def test_sequential_aborts_remaining_members_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC10(Sequential):任一成员失败即整体失败,后续成员不再执行,响应不含异常细节。"""
    harness = _Harness(monkeypatch, _CONFIGS, failing={"wf-审稿人"})
    orchestrator = SequentialOrchestrator(**_deps(_members(1, 2, 3)))

    with pytest.raises(AgentException) as exc_info:
        await orchestrator.run(_QUESTION)

    assert harness.names() == ["wf-分析师", "wf-审稿人"]
    assert exc_info.value.message.startswith("顺序编排已中止")
    assert exc_info.value.details["member_name"] == "审稿人"
    assert exc_info.value.details["workflow_id"] == _WORKFLOW_ID
    assert "敏感堆栈细节" not in exc_info.value.message
    assert "敏感堆栈细节" not in str(exc_info.value.details)


async def test_sequential_aborts_when_member_agent_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7:成员 Agent 停用时 get_runtime_config 抛 409,顺序编排中止并指明是哪个成员。"""
    harness = _Harness(monkeypatch, _CONFIGS, disabled_ids={2})
    orchestrator = SequentialOrchestrator(**_deps(_members(1, 2, 3)))

    with pytest.raises(AgentException) as exc_info:
        await orchestrator.run(_QUESTION)

    assert exc_info.value.status_code == 409
    assert "Agent已停用" in exc_info.value.message
    assert exc_info.value.details["agent_id"] == 2
    # 停用成员本身与其后续成员均未执行。
    assert harness.names() == ["wf-分析师"]


# ---------- Parallel(AC4 / AC6 / AC10) ----------


async def test_parallel_runs_all_members_in_isolated_sessions_and_merges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4:全部成员执行,每个成员持独立 Session,汇总 LLM 被调用且入参含全部成员输出。"""
    harness = _Harness(monkeypatch, _CONFIGS)
    llm = _FakeLLM(merged_text="综合三位成员的结论")
    orchestrator = ParallelOrchestrator(llm=llm, **_deps(_members(1, 2, 3)))

    result = await orchestrator.run(_QUESTION)

    assert sorted(harness.names()) == ["wf-分析师", "wf-审稿人", "wf-总结者"]
    assert all(call.prompt == _QUESTION for call in harness.calls)
    # 并发成员不得共用运行时的 db:每个都拿到独立的 AsyncSessionLocal 实例。
    sessions = [call.db for call in harness.calls]
    assert all(isinstance(db, _FakeSessionScope) for db in sessions)
    assert len({id(db) for db in sessions}) == 3
    assert len(llm.stream_prompts) == 1
    merge_prompt = llm.stream_prompts[0]
    assert _QUESTION in merge_prompt
    for name in ("wf-分析师的结论", "wf-审稿人的结论", "wf-总结者的结论"):
        assert name in merge_prompt
    assert result.text == "综合三位成员的结论"
    deltas = [e for e in orchestrator.bus.events if e.type is EventType.TEXT_DELTA]
    assert [e.text for e in deltas] == ["综合三位成员的结论"]


async def test_parallel_tolerates_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC10(Parallel):单个成员失败不中止其余;汇总只含成功结果,失败详情只进日志。"""
    harness = _Harness(monkeypatch, _CONFIGS, failing={"wf-审稿人"})
    llm = _FakeLLM()
    orchestrator = ParallelOrchestrator(llm=llm, **_deps(_members(1, 2, 3)))

    with caplog.at_level(logging.ERROR, logger="runtime.workflow"):
        result = await orchestrator.run(_QUESTION)

    assert sorted(harness.names()) == ["wf-分析师", "wf-审稿人", "wf-总结者"]
    merge_prompt = llm.stream_prompts[0]
    assert "wf-分析师的结论" in merge_prompt
    assert "wf-总结者的结论" in merge_prompt
    assert "审稿人" not in merge_prompt
    assert "敏感堆栈细节" not in merge_prompt
    assert result.text == "汇总结论"
    failures = [r for r in caplog.records if "并行编排成员执行失败" in r.message]
    assert len(failures) == 1
    assert failures[0].workflow_id == _WORKFLOW_ID
    assert failures[0].agent_id == 2
    assert failures[0].exc_info is not None


async def test_parallel_raises_when_all_members_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC10(Parallel):全部成员失败才判整体失败,且不会调用汇总 LLM。"""
    _Harness(monkeypatch, _CONFIGS, failing={"wf-分析师", "wf-审稿人"})
    llm = _FakeLLM()
    orchestrator = ParallelOrchestrator(llm=llm, **_deps(_members(1, 2)))

    with pytest.raises(AgentException) as exc_info:
        await orchestrator.run(_QUESTION)

    assert exc_info.value.message == "并行编排的全部成员均执行失败"
    assert exc_info.value.details["workflow_id"] == _WORKFLOW_ID
    assert llm.stream_prompts == []


async def test_parallel_requires_at_least_two_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC6:并行模式少于 2 个成员时拒绝运行,任何成员都不执行。"""
    harness = _Harness(monkeypatch, _CONFIGS)
    orchestrator = ParallelOrchestrator(llm=_FakeLLM(), **_deps(_members(1)))

    with pytest.raises(AgentException) as exc_info:
        await orchestrator.run(_QUESTION)

    assert "至少需要2个成员" in exc_info.value.message
    assert harness.calls == []


async def test_parallel_members_actually_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4(并发性):三个成员的执行区间必须重叠。

    替身进入后等齐全部成员才返回;若 gather 被改成串行 for,前两个成员会在会合点超时失败,
    汇总入参就缺它们的输出——因此断言三份输出全部进入汇总提示词。
    """
    harness = _Harness(monkeypatch, _CONFIGS, rendezvous=3)
    llm = _FakeLLM()
    orchestrator = ParallelOrchestrator(llm=llm, **_deps(_members(1, 2, 3)))

    result = await orchestrator.run(_QUESTION)

    assert len(harness.calls) == 3
    assert harness.timeouts == 0
    merge_prompt = llm.stream_prompts[0]
    for name in ("wf-分析师的结论", "wf-审稿人的结论", "wf-总结者的结论"):
        assert name in merge_prompt
    assert result.text == "汇总结论"


async def test_parallel_fails_whole_when_member_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7(Parallel):配置期任一成员不可运行即整体失败,不派发任何成员、不调汇总 LLM。"""
    harness = _Harness(monkeypatch, _CONFIGS, disabled_ids={2})
    llm = _FakeLLM()
    orchestrator = ParallelOrchestrator(llm=llm, **_deps(_members(1, 2, 3)))

    with pytest.raises(AgentException) as exc_info:
        await orchestrator.run(_QUESTION)

    assert exc_info.value.status_code == 409
    assert "Agent已停用" in exc_info.value.message
    assert exc_info.value.details == {"workflow_id": _WORKFLOW_ID, "agent_id": 2}
    assert harness.calls == []
    assert llm.stream_prompts == []


def test_member_display_name_is_truncated_to_column_width() -> None:
    """subagent_runs.member_name 为 String(120),而 Agent 名可达 128 字符,拼前缀后必须截断。"""
    long_name = "甲" * 128
    display_name = build_member_display_name(long_name)
    assert display_name.startswith("wf-")
    assert len(display_name) == MEMBER_DISPLAY_NAME_MAX_LENGTH
    assert build_member_display_name("分析师") == "wf-分析师"


# ---------- LlmRouting(AC5 / AC6 / AC10) ----------


async def test_routing_executes_only_selected_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5:仅被路由选中的成员执行,路由提示词包含各成员描述。"""
    harness = _Harness(monkeypatch, _CONFIGS)
    llm = _FakeLLM(choice=2)
    orchestrator = LlmRoutingOrchestrator(llm=llm, **_deps(_members(1, 2, 3)))

    result = await orchestrator.run(_QUESTION)

    assert harness.names() == ["wf-审稿人"]
    assert harness.calls[0].prompt == _QUESTION
    assert result.text == "wf-审稿人的结论"
    routing_prompt = llm.structured_prompts[0]
    assert _QUESTION in routing_prompt
    for description in ("擅长拆解问题与列出风险", "擅长审阅方案并给出修改意见", "擅长归纳结论"):
        assert description in routing_prompt


async def test_routing_rejects_choice_outside_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5/AC10:路由返回不在成员列表中的 id 时抛中文异常,不回落第一个成员,执行器零调用。"""
    harness = _Harness(monkeypatch, _CONFIGS)
    orchestrator = LlmRoutingOrchestrator(llm=_FakeLLM(choice=99), **_deps(_members(1, 2)))

    with pytest.raises(AgentException) as exc_info:
        await orchestrator.run(_QUESTION)

    assert "不在Workflow成员列表中" in exc_info.value.message
    assert exc_info.value.details["chosen_agent_id"] == 99
    assert harness.calls == []


async def test_routing_decision_failure_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC10(Routing):路由决策失败即整体失败,执行器零调用,响应不含底层异常。"""
    harness = _Harness(monkeypatch, _CONFIGS)
    llm = _FakeLLM(choice=1)
    llm.fail_structured = True
    orchestrator = LlmRoutingOrchestrator(llm=llm, **_deps(_members(1, 2)))

    with pytest.raises(AgentException) as exc_info:
        await orchestrator.run(_QUESTION)

    assert exc_info.value.message == "智能路由决策失败"
    assert "路由模型不可用" not in str(exc_info.value.details)
    assert harness.calls == []


async def test_routing_requires_at_least_two_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC6:路由模式少于 2 个成员时拒绝运行,不调用路由 LLM。"""
    llm = _FakeLLM(choice=1)
    _Harness(monkeypatch, _CONFIGS)
    orchestrator = LlmRoutingOrchestrator(llm=llm, **_deps(_members(1)))

    with pytest.raises(AgentException) as exc_info:
        await orchestrator.run(_QUESTION)

    assert "至少需要2个成员" in exc_info.value.message
    assert llm.structured_prompts == []


# ---------- 发消息入口(AC8 / AC9) ----------


@dataclass
class _FakeSession:
    id: int = _SESSION_ID
    status: str = "idle"
    agent_id: int | None = None
    workflow_id: int | None = None


@dataclass
class _FakeSessionRepo:
    """只覆盖 prepare_for_message 链路用到的仓储方法。"""

    session: _FakeSession | None = None
    created: list[dict[str, Any]] = field(default_factory=list)

    async def get(self, session_id: int) -> _FakeSession | None:
        return self.session

    async def get_for_update(self, session_id: int) -> _FakeSession | None:
        return self.session

    async def update_status(self, session: _FakeSession, status: str) -> _FakeSession:
        session.status = status
        return session

    async def create(self, **fields: Any) -> _FakeSession:
        self.created.append(fields)
        self.session = _FakeSession(
            id=100,
            agent_id=fields["agent_id"],
            workflow_id=fields["workflow_id"],
        )
        return self.session


def _session_service(repo: _FakeSessionRepo) -> SessionService:
    service = SessionService.__new__(SessionService)
    service.db = object()
    service.repo = repo
    return service


def _install_domain_fakes(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """替换 prepare_for_message 内局部导入的 WorkflowService / AgentService,记录复检调用。"""
    calls = SimpleNamespace(workflow=[], agent=[])

    class _FakeWorkflowService:
        def __init__(self, db: object) -> None:
            pass

        async def require_runnable(self, workflow_id: int, workspace_id: int) -> object:
            calls.workflow.append(workflow_id)
            return SimpleNamespace(id=workflow_id)

    class _FakeAgentService:
        def __init__(self, db: object) -> None:
            pass

        async def require_runnable(self, agent_id: int, workspace_id: int) -> object:
            calls.agent.append(agent_id)
            return SimpleNamespace(id=agent_id)

    monkeypatch.setattr("workflow.service.WorkflowService", _FakeWorkflowService)
    monkeypatch.setattr("agent.service.AgentService", _FakeAgentService)
    return calls


async def test_prepare_new_session_with_workflow_checks_and_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8①:首轮带 workflow_id 新建会话时先复检 Workflow 可运行,并把 workflow_id 写入会话。"""
    calls = _install_domain_fakes(monkeypatch)
    repo = _FakeSessionRepo()
    service = _session_service(repo)

    session = await service.prepare_for_message(
        None, _QUESTION, _USER_ID, _WORKSPACE_ID, workflow_id=_WORKFLOW_ID
    )

    assert calls.workflow == [_WORKFLOW_ID]
    assert calls.agent == []
    assert repo.created[0]["workflow_id"] == _WORKFLOW_ID
    assert repo.created[0]["agent_id"] is None
    assert session.workflow_id == _WORKFLOW_ID
    assert session.status == "running"


async def test_prepare_existing_workflow_session_rechecks_without_workflow_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8②:第二轮只带 session_id,仍按会话锁定的 workflow 复检,不静默降级。"""
    calls = _install_domain_fakes(monkeypatch)
    repo = _FakeSessionRepo(session=_FakeSession(workflow_id=_WORKFLOW_ID))
    service = _session_service(repo)

    session = await service.prepare_for_message(_SESSION_ID, _QUESTION, _USER_ID, _WORKSPACE_ID)

    assert calls.workflow == [_WORKFLOW_ID]
    assert session.status == "running"


def test_effective_workflow_id_falls_back_to_locked_session() -> None:
    """AC8②(API 判据):请求体不带 workflow_id 时取会话锁定值;带时以请求体为准;都无则 None。"""
    locked = _FakeSession(workflow_id=_WORKFLOW_ID)
    assert SessionService.resolve_effective_workflow_id(None, locked) == _WORKFLOW_ID
    assert SessionService.resolve_effective_workflow_id(_WORKFLOW_ID, None) == _WORKFLOW_ID
    assert SessionService.resolve_effective_workflow_id(None, _FakeSession()) is None
    assert SessionService.resolve_effective_workflow_id(None, None) is None


async def test_prepare_plain_agent_request_never_touches_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8③:不带 workflow_id 且会话无 workflow 的请求,不触发任何 WorkflowService 调用。"""
    calls = _install_domain_fakes(monkeypatch)
    repo = _FakeSessionRepo(session=_FakeSession(agent_id=3))
    service = _session_service(repo)

    await service.prepare_for_message(_SESSION_ID, _QUESTION, _USER_ID, _WORKSPACE_ID, agent_id=3)
    await _session_service(_FakeSessionRepo()).prepare_for_message(
        None, _QUESTION, _USER_ID, _WORKSPACE_ID
    )

    assert calls.workflow == []
    assert calls.agent == [3]


async def test_prepare_rejects_agent_and_workflow_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9:workflow_id 与 agent_id 同时传入被拒绝,且不进入任何域校验。"""
    calls = _install_domain_fakes(monkeypatch)
    service = _session_service(_FakeSessionRepo())

    with pytest.raises(AgentException) as exc_info:
        await service.prepare_for_message(
            None, _QUESTION, _USER_ID, _WORKSPACE_ID, agent_id=1, workflow_id=_WORKFLOW_ID
        )

    assert exc_info.value.message == "同一次请求不能同时指定Agent和Workflow"
    assert calls.workflow == [] and calls.agent == []


async def test_prepare_rejects_switching_workflow_on_existing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9:已锁定 Workflow 的会话再传不同 workflow_id 返回 409;锁定 Agent 的会话传 workflow_id 同样 409。"""
    _install_domain_fakes(monkeypatch)
    locked_workflow = _session_service(_FakeSessionRepo(session=_FakeSession(workflow_id=5)))
    locked_agent = _session_service(_FakeSessionRepo(session=_FakeSession(agent_id=3)))

    for service in (locked_workflow, locked_agent):
        with pytest.raises(AgentException) as exc_info:
            await service.prepare_for_message(
                _SESSION_ID, _QUESTION, _USER_ID, _WORKSPACE_ID, workflow_id=_WORKFLOW_ID
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.message == "已有会话不能切换Workflow"


# ---------- send_message 分发(AC8,API 层) ----------


class _FakeRuntimeFactory:
    """替身 AgentRuntime / WorkflowRuntime:记录构造与 run 入参,产出一条事件。"""

    def __init__(self) -> None:
        self.constructed = 0
        self.run_kwargs: list[dict[str, Any]] = []
        factory = self

        class _Runtime:
            def __init__(self, db: object, user_id: int, workspace_id: int) -> None:
                factory.constructed += 1

            async def run(self, session_id: int | None, content: str, **kwargs: Any):
                factory.run_kwargs.append(kwargs)
                yield {"type": "turn_end"}

        self.cls = _Runtime


def _install_api_fakes(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeSession | None,
) -> tuple[_FakeRuntimeFactory, _FakeRuntimeFactory, list[int]]:
    """替换 session.api 内的两个运行时、SessionService 与 WorkflowService。"""
    workflow_rt = _FakeRuntimeFactory()
    agent_rt = _FakeRuntimeFactory()
    workflow_checks: list[int] = []

    class _FakeSessionService:
        def __init__(self, db: object) -> None:
            pass

        async def require_write_access(self, session_id: int, user_id: int, ws: int):
            return session

        resolve_effective_workflow_id = staticmethod(SessionService.resolve_effective_workflow_id)

    class _FakeWorkflowService:
        def __init__(self, db: object) -> None:
            pass

        async def require_runnable(self, workflow_id: int, workspace_id: int) -> object:
            workflow_checks.append(workflow_id)
            return SimpleNamespace(id=workflow_id)

    monkeypatch.setattr(session_api, "WorkflowRuntime", workflow_rt.cls)
    monkeypatch.setattr(session_api, "AgentRuntime", agent_rt.cls)
    monkeypatch.setattr(session_api, "SessionService", _FakeSessionService)
    monkeypatch.setattr(session_api, "WorkflowService", _FakeWorkflowService)
    return workflow_rt, agent_rt, workflow_checks


async def _call_send_message(payload: SessionSendMessageRequest) -> list[str]:
    """直接调用路由函数并抽干 SSE 生成器,返回各帧文本。"""
    response = await session_api.send_message(
        payload,
        object(),
        SimpleNamespace(id=_USER_ID),
        _WORKSPACE_ID,
    )
    return [chunk async for chunk in response.body_iterator]


async def test_send_message_first_round_with_workflow_dispatches_workflow_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8①:首轮带 workflow_id → 前置复检 Workflow、构造 WorkflowRuntime,AgentRuntime 不构造。"""
    workflow_rt, agent_rt, checks = _install_api_fakes(monkeypatch, session=None)

    frames = await _call_send_message(
        SessionSendMessageRequest(content=_QUESTION, workflow_id=_WORKFLOW_ID)
    )

    assert checks == [_WORKFLOW_ID]
    assert workflow_rt.constructed == 1 and agent_rt.constructed == 0
    assert workflow_rt.run_kwargs == [{"workflow_id": _WORKFLOW_ID}]
    assert len(frames) == 1 and frames[0].startswith("data: ")


async def test_send_message_second_round_without_workflow_id_still_dispatches_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8②:第二轮只带 session_id,会话已锁定 workflow → 仍走 WorkflowRuntime(防静默降级)。"""
    locked = _FakeSession(workflow_id=_WORKFLOW_ID)
    workflow_rt, agent_rt, checks = _install_api_fakes(monkeypatch, session=locked)

    await _call_send_message(SessionSendMessageRequest(session_id=_SESSION_ID, content=_QUESTION))

    assert workflow_rt.constructed == 1 and agent_rt.constructed == 0
    assert workflow_rt.run_kwargs == [{"workflow_id": _WORKFLOW_ID}]
    # 已有会话不再前置复检,复检由 Service 的 prepare_for_message 负责。
    assert checks == []


async def test_send_message_plain_agent_request_dispatches_agent_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8③:纯 Agent 请求 → AgentRuntime,WorkflowRuntime 不构造,WorkflowService 零调用。"""
    workflow_rt, agent_rt, checks = _install_api_fakes(
        monkeypatch, session=_FakeSession(agent_id=3)
    )

    await _call_send_message(
        SessionSendMessageRequest(session_id=_SESSION_ID, agent_id=3, content=_QUESTION)
    )

    assert agent_rt.constructed == 1 and workflow_rt.constructed == 0
    assert agent_rt.run_kwargs == [{"agent_id": 3}]
    assert checks == []


async def test_send_message_rejects_workflow_switch_and_mixing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9(API 层):锁定 Agent 的会话带 workflow_id → 409;agent_id 与 workflow_id 同传 → 拒绝。"""
    workflow_rt, agent_rt, _ = _install_api_fakes(monkeypatch, session=_FakeSession(agent_id=3))

    with pytest.raises(AgentException) as switch_exc:
        await _call_send_message(
            SessionSendMessageRequest(
                session_id=_SESSION_ID, workflow_id=_WORKFLOW_ID, content=_QUESTION
            )
        )
    with pytest.raises(AgentException) as mix_exc:
        await _call_send_message(
            SessionSendMessageRequest(agent_id=1, workflow_id=_WORKFLOW_ID, content=_QUESTION)
        )

    assert switch_exc.value.status_code == 409
    assert mix_exc.value.message == "同一次请求不能同时指定Agent和Workflow"
    assert workflow_rt.constructed == 0 and agent_rt.constructed == 0


# ---------- WorkflowRuntime._produce(生命周期与失败终态) ----------


@dataclass
class _FakeDb:
    commits: int = 0
    rollbacks: int = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@dataclass
class _FakeTurn:
    id: Any = field(default_factory=uuid4)
    status: str = "running"


class _FakeRuntimeSessionService:
    """替身 SessionService:记录 _produce 的持久化调用序列。"""

    def __init__(self) -> None:
        self.session = _FakeSession(status="running", workflow_id=_WORKFLOW_ID)
        self.turn = _FakeTurn()
        self.messages: list[tuple[str, Any]] = []
        self.turn_statuses: list[str] = []
        self.session_statuses: list[str] = []
        self.prepare_kwargs: dict[str, Any] = {}
        service = self

        class _Repo:
            async def update_status(self, session: Any, status: str) -> Any:
                service.session_statuses.append(status)
                return session

            async def get(self, session_id: int) -> Any:
                return service.session

            async def get_turn(self, turn_id: Any) -> Any:
                return service.turn

        self.repo = _Repo()

    async def prepare_for_message(self, session_id, content, user_id, workspace_id, **kwargs):
        self.prepare_kwargs = kwargs
        return SimpleNamespace(id=_SESSION_ID, title="标题", status="running")

    async def start_turn(self, session, user_id, workspace_id, content):
        self.messages.append(("user", content))
        return self.turn, SimpleNamespace(id=1)

    async def add_message(self, session_id, role, content, turn_id=None, provider=None, model=None):
        self.messages.append((role, content))
        return SimpleNamespace(id=2)

    async def mark_turn_status(self, turn, status, completed_message_id=None):
        self.turn_statuses.append(status)
        turn.status = status
        return turn


def _make_runtime(
    monkeypatch: pytest.MonkeyPatch,
    orchestrator_run: Any,
) -> tuple[WorkflowRuntime, _FakeDb, _FakeRuntimeSessionService]:
    """构造带替身依赖的 WorkflowRuntime,并把 _build_orchestrator 替换为给定 run 协程的编排器。"""
    db = _FakeDb()
    runtime = WorkflowRuntime(db, user_id=_USER_ID, workspace_id=_WORKSPACE_ID)
    session_service = _FakeRuntimeSessionService()
    runtime.session_service = session_service

    async def _build(session_id: int, turn_id: Any, workflow_id: int) -> Any:
        return SimpleNamespace(run=orchestrator_run)

    monkeypatch.setattr(runtime, "_build_orchestrator", _build)
    return runtime, db, session_service


async def test_runtime_run_commits_user_message_then_final_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功路径:首次 commit 落 user 消息,末次 commit 完成 turn;assistant 落中立块;bus 关闭。"""

    async def _run(user_content: str):
        from runtime.workflow import OrchestrationResult

        return OrchestrationResult(text="最终回答", provider="anthropic", model="m")

    runtime, db, session_service = _make_runtime(monkeypatch, _run)

    events = [event async for event in runtime.run(None, _QUESTION, workflow_id=_WORKFLOW_ID)]

    assert db.commits >= 2 and db.rollbacks == 0
    assert session_service.prepare_kwargs == {"workflow_id": _WORKFLOW_ID}
    assert session_service.messages[0] == ("user", _QUESTION)
    assert session_service.messages[-1] == ("assistant", [{"kind": "text", "text": "最终回答"}])
    assert session_service.turn_statuses == ["completed"]
    assert session_service.session_statuses == ["idle"]
    assert [event["type"] for event in events] == ["session_ready", "turn_start", "turn_end"]
    assert runtime.bus._closed is True


async def test_runtime_run_marks_failed_and_emits_error_on_orchestrator_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败路径:编排器抛 AgentException → turn 标 failed、session 标 failed、error 事件带中文消息。"""

    async def _run(user_content: str):
        raise AgentException.message("并行编排的全部成员均执行失败", {"workflow_id": _WORKFLOW_ID})

    runtime, db, session_service = _make_runtime(monkeypatch, _run)
    marked: list[tuple[Any, Any, str]] = []
    original = runtime._mark_abnormal_end

    async def _spy(session_id, turn_id, turn_status):
        marked.append((session_id, turn_id, turn_status))
        await original(session_id, turn_id, turn_status)

    monkeypatch.setattr(runtime, "_mark_abnormal_end", _spy)

    events = [event async for event in runtime.run(None, _QUESTION, workflow_id=_WORKFLOW_ID)]

    assert marked == [(_SESSION_ID, session_service.turn.id, "failed")]
    assert db.rollbacks == 1
    assert session_service.turn_statuses == ["failed"]
    assert session_service.session_statuses == ["failed"]
    error_events = [event for event in events if event["type"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["error"]["message"] == "并行编排的全部成员均执行失败"
    assert error_events[0]["error"]["code"] == "WORKFLOW_RUN_FAILED"
    assert runtime.bus._closed is True
