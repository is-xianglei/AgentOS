"""SubAgentRunner 运行被委派 Agent 的测试:工具三方交集、档位区分、事件与运行记录、被拒摘要。

LLM、权限规则查询与工具执行全部打桩,只保留运行器自身的裁剪、裁决与收尾逻辑。
"""

import json
from dataclasses import dataclass, field
from typing import Any, Self

import pytest

from agent.service import AgentRuntimeConfig
from llm.types import LLMTurnResult, ToolUse
from runtime.agent import PLAN_MODE_ALLOWED_TOOLS
from runtime.subagent import SubAgentRunner, build_agent_spec
from tools.base import ToolContext
from tools.builtin.agent import AgentTool, AgentToolInput
from tools.gateway import ToolGateway
from tools.registry import build_tool_registry
from tools.subagents.definition import AgentType, SubAgentSpec
from tools.subagents.registry import get_subagent_spec

pytestmark = pytest.mark.anyio

_SID = 42
# 白名单故意混入 delegate 不可调的 TeamList 与全局禁用的 Agent,
# 用来验证有效工具 = 白名单 ∩ delegate 档位 ∩ 父 Agent 本轮生效集。
_WHITELIST = ("Read", "Write", "Grep", "Bash", "Skill", "SkillRun", "TeamList", "Agent")
_NORMAL_ROUND = build_tool_registry().without("ExitPlanMode", "WritePlan").names
_PLAN_ROUND = build_tool_registry().only(*PLAN_MODE_ALLOWED_TOOLS).names


def _config(**overrides: Any) -> AgentRuntimeConfig:
    values: dict[str, Any] = {
        "agent_id": 7,
        "system_prompt": "你是审稿人",
        "model_name": None,
        "tool_runtime_names": _WHITELIST,
        "skill_ids": (),
        "skill_names": (),
        "name": "审稿人",
        "description": None,
    }
    values.update(overrides)
    return AgentRuntimeConfig(**values)


def _delegate_spec() -> SubAgentSpec:
    return build_agent_spec(_config(), display_name="delegate-审稿人", tool_profile="delegate")


@dataclass
class _FakeRun:
    id: int = 11


@dataclass
class _RecordingRunRepo:
    """记录运行记录的创建参数与最终报告。"""

    create_kwargs: list[dict[str, Any]] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)

    async def create(
        self, session_id: int, member_name: str, prompt: str, **kwargs: Any
    ) -> _FakeRun:
        self.create_kwargs.append({"member_name": member_name, **kwargs})
        return _FakeRun()

    async def succeed(self, run: _FakeRun, report: str) -> _FakeRun:
        self.reports.append(report)
        return run

    async def fail(self, run: _FakeRun, error_message: str) -> _FakeRun:
        return run


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> dict[str, Any]:
        self.events.append(event)
        return {}


class _FakeDb:
    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None


class _FakeAssembler:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.last_result = LLMTurnResult(text="报告", stop_reason="end_turn")


@dataclass
class _LoopCapture:
    """替身 ReAct 循环收到的工具集与网关。"""

    tool_registry: Any = None
    tool_gateway: Any = None


async def _capture_loop(
    capture: _LoopCapture,
    tool_registry: Any,
    tool_gateway: Any,
) -> tuple[str, int]:
    capture.tool_registry = tool_registry
    capture.tool_gateway = tool_gateway
    return "报告", 1


def _stub_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_loop: bool = True,
) -> tuple[SubAgentRunner, _RecordingRunRepo, _RecordingBus, _LoopCapture]:
    """构造跳过 LLM 的运行器;fake_loop=False 时保留真实 ReAct 循环与工具裁决。"""
    runner = SubAgentRunner(_FakeDb(), bus=_RecordingBus(), llm=object())  # type: ignore[arg-type]
    repo = _RecordingRunRepo()
    runner.run_repo = repo  # type: ignore[assignment]
    capture = _LoopCapture()
    monkeypatch.setattr("runtime.subagent.TurnEventAssembler", _FakeAssembler)
    if fake_loop:

        async def _fake_loop(*args: Any, **kwargs: Any) -> tuple[str, int]:
            # 位置参数顺序见 SubAgentRunner.run 对 _run_react_loop 的调用。
            return await _capture_loop(capture, args[6], args[7])

        monkeypatch.setattr(runner, "_run_react_loop", _fake_loop)
    return runner, repo, runner.bus, capture  # type: ignore[return-value]


# ---------- 工具:白名单 ∩ delegate 档位 ∩ 父 Agent 本轮生效集 ----------


@pytest.mark.parametrize(
    ("parent_round", "expected"),
    [
        (_NORMAL_ROUND, {"Read", "Write", "Grep", "Bash", "Skill", "SkillRun"}),
        # Plan Mode 只读:本轮生效集里没有 Write/Bash/SkillRun,被委派方也拿不到。
        (_PLAN_ROUND, {"Read", "Grep", "Skill"}),
    ],
)
async def test_delegate_tools_are_three_way_intersection(
    monkeypatch: pytest.MonkeyPatch,
    parent_round: frozenset[str],
    expected: set[str],
) -> None:
    runner, _, _, capture = _stub_runner(monkeypatch)

    await runner.run(_SID, "审稿", _delegate_spec(), parent_allowed_tool_names=parent_round)

    assert capture.tool_registry.names == expected
    assert capture.tool_gateway.registry.names == expected
    assert capture.tool_gateway.context.profile == "delegate"
    assert "Agent" not in capture.tool_registry.names
    assert "TeamList" not in capture.tool_registry.names


@pytest.mark.parametrize(
    ("spec", "expected_profile"),
    [
        (
            build_agent_spec(_config(), display_name="wf-审稿人", tool_profile="workflow_node"),
            "workflow_node",
        ),
        # 旧调用:带 agent_id 但未声明档位,保持按工作流节点裁决。
        (
            SubAgentSpec(
                agent_type=AgentType.GENERAL_PURPOSE,
                system_prompt="人格",
                display_name="wf-审稿人",
                agent_id=2,
                allowed_tools=_WHITELIST,
            ),
            "workflow_node",
        ),
        (get_subagent_spec(AgentType.EXPLORE), "subagent"),
    ],
)
async def test_profile_is_explicit_and_legacy_default_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    spec: SubAgentSpec,
    expected_profile: str,
) -> None:
    runner, _, _, capture = _stub_runner(monkeypatch)

    await runner.run(_SID, "任务", spec)

    assert capture.tool_gateway.context.profile == expected_profile


# ---------- 事件与运行记录 ----------


async def test_delegate_run_record_and_events_carry_agent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, bus, _ = _stub_runner(monkeypatch)

    await runner.run(_SID, "审稿", _delegate_spec())

    assert repo.create_kwargs == [{"member_name": "delegate-审稿人", "agent_id": 7}]
    turn_start = bus.events[0].to_dict()
    assert turn_start["type"] == "turn_start"
    assert turn_start["actor"] == {
        "role": "subagent",
        "name": "delegate-审稿人",
        "run_id": "run-11",
        "task": "审稿",
        "agent_id": 7,
    }
    assert bus.events[-1].to_dict()["actor"]["agent_id"] == 7


# ---------- ask 坍缩为拒绝,结果末尾追加被拒摘要 ----------


def _script_llm(runner: SubAgentRunner, tool_uses: list[ToolUse]) -> None:
    """第一轮按给定 tool_use 调工具,第二轮给出最终文本。"""
    results = [
        LLMTurnResult(tool_uses=tool_uses, stop_reason="tool_use"),
        LLMTurnResult(text="子 Agent 结论", stop_reason="end_turn"),
    ]

    async def _fake_stream(*args: Any, **kwargs: Any) -> LLMTurnResult:
        return results.pop(0)

    runner._stream_and_collect = _fake_stream  # type: ignore[method-assign]


def _script_permissions(
    monkeypatch: pytest.MonkeyPatch,
    runner: SubAgentRunner,
    behaviors: dict[str, str],
) -> tuple[list[str | None], list[str]]:
    """动态权限按工具名给出 allow/ask/deny;返回 (评估时的 agent_type, 实际执行的工具)。"""
    evaluated_agent_types: list[str | None] = []
    executed: list[str] = []

    async def _evaluate(session_id, tool_name, tool_input=None, agent_type=None, workspace_id=None):
        evaluated_agent_types.append(agent_type)
        return behaviors[tool_name]

    async def _gateway_run(self, session_id, tool_name, input_args, **kwargs):
        executed.append(tool_name)
        return "工具输出"

    monkeypatch.setattr(runner.policy_engine.permission_service, "evaluate", _evaluate)
    monkeypatch.setattr(ToolGateway, "run", _gateway_run)
    return evaluated_agent_types, executed


_DENIAL_SCRIPT = [
    ToolUse(id="t1", name="Read", input={"file_path": "a.py"}),
    ToolUse(id="t2", name="Write", input={"file_path": "b.py", "content": "x" * 500}),
    ToolUse(id="t3", name="Bash", input={"command": "ls"}),
    ToolUse(id="t4", name="Agent", input={"prompt": "再派一个"}),
]


async def test_denied_calls_are_summarized_at_end_of_delegate_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _, _ = _stub_runner(monkeypatch, fake_loop=False)
    _script_llm(runner, list(_DENIAL_SCRIPT))
    agent_types, executed = _script_permissions(
        monkeypatch, runner, {"Read": "allow", "Write": "ask", "Bash": "deny"}
    )

    report = await runner.run(_SID, "审稿", _delegate_spec())

    # ask 坍缩为拒绝:只有 Read 真正执行;Agent 在静态能力层就被拒,不进动态权限。
    assert executed == ["Read"]
    # 权限规则评估参照 Workflow 节点,用运行标识作 agent_type。
    assert agent_types == ["delegate-审稿人"] * 3
    body, summary = report.split("\n\n[运行时附注]")
    assert body == "子 Agent 结论"
    lines = summary.splitlines()[1:]
    assert len(lines) == 3
    assert lines[0].startswith("- Write {")
    assert lines[0].endswith("…(已截断):需要用户审批,被委派的 Agent 没有审批通道")
    assert lines[1] == f"- Bash {json.dumps({'command': 'ls'})}:权限策略拒绝执行该工具调用"
    assert lines[2].startswith("- Agent ")
    assert lines[2].endswith("工具能力策略拒绝执行: tool=Agent, profile=delegate")
    # 运行记录与交回默认 Agent 的结果一致。
    assert repo.reports == [report]


async def test_no_denial_means_no_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, _, _ = _stub_runner(monkeypatch, fake_loop=False)
    _script_llm(runner, [ToolUse(id="t1", name="Read", input={"file_path": "a.py"})])
    _script_permissions(monkeypatch, runner, {"Read": "allow"})

    report = await runner.run(_SID, "审稿", _delegate_spec())

    assert report == "子 Agent 结论"


async def test_workflow_member_report_is_unchanged_even_with_denials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被拒摘要只属于委派路径,Workflow 成员的输出保持原样。"""
    runner, _, _, _ = _stub_runner(monkeypatch, fake_loop=False)
    _script_llm(runner, [ToolUse(id="t2", name="Write", input={"file_path": "b.py"})])
    _script_permissions(monkeypatch, runner, {"Write": "ask"})
    spec = build_agent_spec(_config(), display_name="wf-审稿人", tool_profile="workflow_node")

    report = await runner.run(_SID, "审稿", spec)

    assert report == "子 Agent 结论"


# ---------- 端到端:Agent 工具 → 真实 _dispatch → 真实运行器 ----------


class _FakeSessionScope:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def commit(self) -> None:
        return None


async def test_plan_round_keeps_write_away_from_delegated_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使 Agent 绑了 Write,父 Agent 本轮处于 Plan Mode 时,被委派方也拿不到 Write。"""
    capture = _LoopCapture()

    class _FakeAgentService:
        def __init__(self, db: object) -> None:
            pass

        async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
            return _config()

    async def _fake_loop(self: SubAgentRunner, *args: Any, **kwargs: Any) -> tuple[str, int]:
        return await _capture_loop(capture, args[6], args[7])

    monkeypatch.setattr("agent.service.AgentService", _FakeAgentService)
    monkeypatch.setattr("database.engine.AsyncSessionLocal", _FakeSessionScope)
    monkeypatch.setattr("runtime.subagent.SubAgentRunRepository", lambda db: _RecordingRunRepo())
    monkeypatch.setattr("runtime.subagent.TurnEventAssembler", _FakeAssembler)
    monkeypatch.setattr(SubAgentRunner, "_run_react_loop", _fake_loop)
    ctx = ToolContext(
        session_id=_SID,
        db=object(),
        workspace_id=3,
        profile="lead",
        allowed_tool_names=build_tool_registry().names,
        round_tool_names=_PLAN_ROUND,
    )

    output = await AgentTool().run(AgentToolInput(agent_id=7, prompt="审稿"), ctx)

    assert json.loads(output)["report"] == "报告"
    assert capture.tool_registry.names == {"Read", "Grep", "Skill"}
    assert "Write" not in capture.tool_gateway.registry.names
