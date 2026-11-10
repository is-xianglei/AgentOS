"""规则链审核员测试：静态能力检查与权限三态、两套规则的顺序、
Decision 工厂方法的非法状态，以及 ToolCallReviewer 端口可替换。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.errors import AgentException
from core.events import ORCHESTRATOR_ACTOR
from harness.adapters.reviewers.policy_reviewer import (
    ArgumentsRule,
    BudgetRule,
    CapabilityRule,
    InteractionToolRule,
    NoApprovalChannelRule,
    PermissionRule,
    PlanModeAgentRule,
    PlanModeToolsRule,
    PolicyReviewer,
)
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.app.ports import ToolCallReviewer
from harness.domain.budget import ToolCallBudget
from harness.domain.messages import ToolCall
from harness.domain.review import Decision, ReviewRequest, check_capability
from harness.domain.tool import ToolProfile
from permission.service import PermissionService
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner

pytestmark = pytest.mark.anyio


def _request(
    profile: ToolProfile = "user_agent",
    *,
    plan_content: str | None = None,
    budget: ToolCallBudget | None = None,
    allowed_skill_names: frozenset[str] | None = None,
) -> ReviewRequest:
    if budget is None:
        budget = ToolCallBudget(total_limit=10, per_tool_limit=10)
    return ReviewRequest(
        session_id=1,
        profile=profile,
        agent_type=profile,
        budget=budget,
        workspace_id=3,
        allowed_skill_names=allowed_skill_names,
        plan_content=plan_content,
        tools=builtin_tool_set(),
    )


def _call(tool_name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id="t1", tool_name=tool_name, arguments=arguments or {})


def _permission_service(behavior: str, calls: list[str] | None = None) -> PermissionService:
    service = PermissionService(None)  # type: ignore[arg-type]

    async def evaluate(_session_id, tool_name, _input=None, **_kwargs):
        if calls is not None:
            calls.append(tool_name)
        return behavior

    service.evaluate = evaluate  # type: ignore[method-assign]
    return service


# ---------- 静态能力：档位与 Skill 白名单 ----------


def test_static_capability_checks_profile_and_skill() -> None:
    tool_set = builtin_tool_set()

    denied = check_capability(
        "AskUserQuestion", tool_set.policy_for("AskUserQuestion"), "user_agent", {}, None
    )
    assert denied is not None
    assert "profile=user_agent" in denied

    missing_skill = check_capability(
        "SkillRun",
        tool_set.policy_for("SkillRun"),
        "user_agent",
        {"skill_name": "private"},
        frozenset({"public"}),
    )
    assert missing_skill is not None
    assert "已绑定的 skill" in missing_skill

    allowed_skill = check_capability(
        "SkillRun",
        tool_set.policy_for("SkillRun"),
        "user_agent",
        {"skill_name": "public"},
        frozenset({"public"}),
    )
    assert allowed_skill is None


# ---------- 权限规则三态与静态拒绝短路 ----------


async def test_permission_rule_preserves_three_states() -> None:
    reviewer = PolicyReviewer.for_main_agent(_permission_service("ask"))

    decision = await reviewer.review(
        _call("Write", {"file_path": "a.txt", "content": "x"}), _request()
    )

    assert decision.behavior == "ask"
    assert decision.interaction == "tool_approval"
    assert decision.reason == "权限策略要求用户审批"


async def test_static_denial_short_circuits_permission() -> None:
    calls: list[str] = []
    reviewer = PolicyReviewer.for_subagent(_permission_service("allow", calls))

    decision = await reviewer.review(_call("TeamCreate"), _request())

    assert decision.behavior == "deny"
    assert calls == []


# ---------- 两套规则的顺序 ----------


def test_main_agent_rule_order() -> None:
    reviewer = PolicyReviewer.for_main_agent(_permission_service("allow"))

    assert [type(rule) for rule in reviewer.rules] == [
        BudgetRule,
        PlanModeToolsRule,
        ArgumentsRule,
        CapabilityRule,
        PlanModeAgentRule,
        InteractionToolRule,
        PermissionRule,
    ]


def test_subagent_rule_order() -> None:
    reviewer = PolicyReviewer.for_subagent(_permission_service("allow"))

    assert [type(rule) for rule in reviewer.rules] == [BudgetRule, NoApprovalChannelRule]
    no_channel = reviewer.rules[1]
    assert isinstance(no_channel, NoApprovalChannelRule)
    assert [type(rule) for rule in no_channel.rules] == [CapabilityRule, PermissionRule]


class _RecordingRule(ToolCallReviewer):
    """记录被调用的顺序与收到的入参，按给定结论返回。"""

    def __init__(self, name: str, log: list[tuple[str, dict[str, Any]]], decision: Any) -> None:
        self._name = name
        self._log = log
        self._decision = decision

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        self._log.append((self._name, call.arguments))
        if self._decision is None:
            return Decision.allow(call.arguments, source=self._name)
        return self._decision(call)


async def test_chain_stops_at_first_terminal_decision_and_passes_rewritten_arguments() -> None:
    log: list[tuple[str, dict[str, Any]]] = []
    reviewer = PolicyReviewer(
        [
            _RecordingRule("a", log, None),
            _RecordingRule("b", log, lambda call: Decision.allow({"x": 2}, source="b")),
            _RecordingRule(
                "c", log, lambda call: Decision.deny("不行", arguments=call.arguments, source="c")
            ),
            _RecordingRule("d", log, None),
        ]
    )

    decision = await reviewer.review(_call("Read", {"x": 1}), _request())

    assert log == [("a", {"x": 1}), ("b", {"x": 1}), ("c", {"x": 2})]
    assert (decision.behavior, decision.source, decision.arguments) == ("deny", "c", {"x": 2})


async def test_budget_is_consumed_even_when_later_rule_denies() -> None:
    budget = ToolCallBudget(total_limit=5, per_tool_limit=5)
    reviewer = PolicyReviewer.for_main_agent(_permission_service("deny"))

    decision = await reviewer.review(_call("TeamCreate"), _request(budget=budget))

    assert decision.source == "capability"
    assert budget.calls_by_tool == {"TeamCreate": 1}


async def test_plan_allowlist_runs_before_argument_validation() -> None:
    reviewer = PolicyReviewer.for_main_agent(_permission_service("allow"))

    decision = await reviewer.review(_call("Write"), _request("lead", plan_content="计划"))

    assert decision.source == "plan_mode_tools"


async def test_arguments_run_before_capability_and_interaction_after_capability() -> None:
    reviewer = PolicyReviewer.for_main_agent(_permission_service("allow"))

    invalid = await reviewer.review(_call("TeamCreate", {"team_name": 1}), _request())
    interaction = await reviewer.review(_call("EnterPlanMode"), _request())

    assert invalid.source == "arguments"
    assert interaction.source == "capability"


async def test_interaction_tools_skip_permission_rule() -> None:
    calls: list[str] = []
    reviewer = PolicyReviewer.for_main_agent(_permission_service("deny", calls))

    decision = await reviewer.review(_call("EnterPlanMode"), _request("lead"))

    assert decision.behavior == "ask"
    assert decision.interaction == "enter_plan_mode"
    assert calls == []


async def test_subagent_chain_collapses_ask_and_skips_argument_validation() -> None:
    reviewer = PolicyReviewer.for_subagent(_permission_service("ask"))

    decision = await reviewer.review(_call("Read", {"offset": None}), _request("subagent"))

    assert decision.behavior == "deny"
    assert decision.needs_approval is True
    assert decision.arguments == {"offset": None}
    assert decision.reason == "权限策略要求用户审批"
    assert decision.message.startswith("error: 工具 Read 需要用户审批")


# ---------- Decision 工厂方法与非法状态 ----------


def test_decision_factories_build_legal_states() -> None:
    allowed = Decision.allow({"a": 1}, source="s")
    denied = Decision.deny("回给模型", arguments={}, source="s")
    denied_with_reason = Decision.deny("回给模型", arguments={}, source="s", reason="内部原因")
    asked = Decision.ask("user_question", arguments={}, reason="交互", source="s")

    assert (allowed.behavior, allowed.message, allowed.interaction) == ("allow", "", None)
    assert (denied.message, denied.reason) == ("回给模型", "回给模型")
    assert denied_with_reason.reason == "内部原因"
    assert asked.require_interaction() == "user_question"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: Decision.allow({}, source=""), id="缺少来源"),
        pytest.param(lambda: Decision.deny("", arguments={}, source="s"), id="拒绝缺少文本"),
        pytest.param(
            lambda: Decision.ask("unknown", arguments={}, reason="r", source="s"),  # type: ignore[arg-type]
            id="未知交互类型",
        ),
        pytest.param(
            lambda: Decision.ask("tool_approval", arguments={}, reason="", source="s"),
            id="挂起缺少原因",
        ),
        pytest.param(
            lambda: Decision(behavior="allow", arguments={}, source="s", message="x"),
            id="放行带拒绝文本",
        ),
        pytest.param(
            lambda: Decision(behavior="allow", arguments={}, source="s", needs_approval=True),
            id="放行带审批标记",
        ),
        pytest.param(
            lambda: Decision(
                behavior="deny",
                arguments={},
                source="s",
                message="m",
                reason="r",
                interaction="tool_approval",
            ),
            id="拒绝带交互类型",
        ),
        pytest.param(
            lambda: Decision(
                behavior="ask",
                arguments={},
                source="s",
                reason="r",
                interaction="tool_approval",
                message="m",
            ),
            id="挂起带拒绝文本",
        ),
        pytest.param(
            lambda: Decision(behavior="maybe", arguments={}, source="s"),  # type: ignore[arg-type]
            id="未知结论",
        ),
    ],
)
def test_decision_rejects_illegal_states(build: Any) -> None:
    with pytest.raises(AgentException):
        build()


def test_require_interaction_rejects_non_ask_decision() -> None:
    with pytest.raises(AgentException, match="只有挂起结论才有交互类型"):
        Decision.allow({}, source="s").require_interaction()


def test_decision_is_immutable() -> None:
    decision = Decision.allow({}, source="s")

    with pytest.raises(AttributeError):
        decision.behavior = "deny"  # type: ignore[misc]


# ---------- 端口可替换：注入的审核员被运行时调用 ----------


class _FakeReviewer(ToolCallReviewer):
    """按工具名给出结论的审核员替身，记录收到的调用与上下文。"""

    def __init__(self) -> None:
        self.calls: list[tuple[ToolCall, ReviewRequest]] = []

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        self.calls.append((call, request))
        if call.tool_name == "Write":
            return Decision.deny("替身拒绝", arguments=call.arguments, source="fake")
        return Decision.allow({**call.arguments, "reviewed": True}, source="fake")


async def test_injected_reviewer_replaces_policy_reviewer_in_agent_runtime() -> None:
    reviewer = _FakeReviewer()
    runtime = AgentRuntime(object(), user_id=1, workspace_id=2, llm=object(), reviewer=reviewer)  # type: ignore[arg-type]
    runtime.plan_service = SimpleNamespace(get_active=AsyncMock(return_value=None))
    runtime.sessions = SimpleNamespace(add_message=AsyncMock())
    runtime.bus = SimpleNamespace(emit=AsyncMock())
    executed: list[dict[str, Any]] = []

    async def _gateway_run(session_id, tool_name, input_args, **kwargs):
        executed.append({"tool_name": tool_name, "input_args": input_args})
        return "ok"

    runtime.configuration.gateway.run = _gateway_run  # type: ignore[method-assign]
    done_ids: list[str] = []

    await runtime._interactions().run_tool_calls(
        1,
        SimpleNamespace(),
        SimpleNamespace(id=uuid4()),
        ORCHESTRATOR_ACTOR,
        99,
        [
            ToolCall(id="t1", tool_name="Write", arguments={"file_path": "a"}),
            ToolCall(id="t2", tool_name="Read", arguments={"file_path": "b"}),
        ],
        done_ids,
        None,
    )

    assert runtime.reviewer is reviewer
    assert [call.tool_name for call, _ in reviewer.calls] == ["Write", "Read"]
    assert reviewer.calls[0][1].profile == "lead"
    assert done_ids == ["t1", "t2"]
    assert executed == [{"tool_name": "Read", "input_args": {"file_path": "b", "reviewed": True}}]
    denied_event = runtime.bus.emit.await_args_list[0].args[0].to_dict()
    assert denied_event["tool"] == {
        "id": "t1",
        "name": "Write",
        "output": "替身拒绝",
        "is_error": True,
    }


def test_runtimes_default_to_policy_reviewer() -> None:
    runtime = AgentRuntime(object(), user_id=1, workspace_id=2, llm=object())  # type: ignore[arg-type]
    runner = SubAgentRunner(object(), llm=object())  # type: ignore[arg-type]

    assert isinstance(runtime.reviewer, PolicyReviewer)
    assert isinstance(runner.reviewer, PolicyReviewer)
    assert type(runtime.reviewer.rules[-1]) is PermissionRule
    assert type(runner.reviewer.rules[-1]) is NoApprovalChannelRule


async def test_injected_reviewer_replaces_policy_reviewer_in_subagent_runner() -> None:
    reviewer = _FakeReviewer()
    runner = SubAgentRunner(object(), llm=object(), reviewer=reviewer)  # type: ignore[arg-type]
    executed: list[dict[str, Any]] = []

    async def _gateway_run(session_id, tool_name, input_args, **kwargs):
        executed.append({"tool_name": tool_name, "input_args": input_args})
        return "ok"

    gateway = SimpleNamespace(
        context=SimpleNamespace(profile="subagent", allowed_skill_names=None), run=_gateway_run
    )
    messages: list[dict[str, Any]] = []
    spec = SimpleNamespace(runtime_name="Explore", max_rounds=2)

    await runner._execute_tool_calls(
        1,
        ORCHESTRATOR_ACTOR,
        [ToolCall(id="t1", tool_name="Read", arguments={"file_path": "b"})],
        messages,
        gateway,  # type: ignore[arg-type]
        spec,  # type: ignore[arg-type]
        None,
        None,
        None,
    )

    assert [call.tool_name for call, _ in reviewer.calls] == ["Read"]
    assert reviewer.calls[0][1].agent_type == "Explore"
    assert executed == [{"tool_name": "Read", "input_args": {"file_path": "b", "reviewed": True}}]
    assert messages[0]["content"]["input_args"] == {"file_path": "b", "reviewed": True}
