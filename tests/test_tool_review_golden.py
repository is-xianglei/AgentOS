"""工具审核黄金测试：固化主 Agent 与子代理逐个工具调用的审核结果。

驱动主运行时 AgentRuntime._execute_tool_calls 与 SubAgentRunner._execute_tool_calls，逐种情况记录：
写出的 tool_result（事件与落库消息，含文本与 is_error）、done_ids、是否挂起及交互类型（含续跑状态与
交互请求）、实际执行时传给网关的入参、被拒摘要记录、权限规则的查询参数、日志（不含 logger 名）与
预算计数，与 tests/fixtures/tool_review_golden.py 的字面量逐项比对（含字典键序），
并用 SHA-256 再校验一遍字面量本身。
字面量由阶段 3b 重构前的代码实际产出生成；此后只允许改动本文件「驱动」一节
（如何构造运行时与运行器、如何发起一批调用），字面量、观测与断言一个都不许改。
"""

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent.service import AgentRuntimeConfig
from core.config import settings as app_settings
from core.events import ORCHESTRATOR_ACTOR, Actor, subagent_actor, teammate_actor
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.app.subagent_catalog import get_subagent_spec
from harness.domain.budget import ToolCallBudget
from harness.domain.messages import ToolCall
from harness.domain.roles.subagent import AgentType, SubAgentSpec
from harness.domain.tool import ToolProfile
from permission.service import PermissionService
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner, _render_denied_summary, build_agent_spec
from tests.fixtures.tool_review_golden import EXPECTED, EXPECTED_SHA256

pytestmark = pytest.mark.anyio

_SESSION_ID = 1
_ASSISTANT_MESSAGE_ID = 99
_USER_ID = 3
_WORKSPACE_ID = 2
_TURN_ID = UUID("00000000-0000-4000-8000-00000000a001")
_PLAN_ID = UUID("00000000-0000-4000-8000-00000000d001")
_SUSPENSION_ID = UUID("00000000-0000-4000-8000-00000000b001")
_EXISTING_SUSPENSION_ID = UUID("00000000-0000-4000-8000-00000000b002")
_REQUEST_ID = UUID("00000000-0000-4000-8000-00000000c001")
_INTERACTION_RECORD_ID = 501
_PLAN_CONTENT = "# 计划\n1. 阅读代码\n2. 修改实现"
_QUESTION_ARGS: dict[str, Any] = {
    "questions": [
        {
            "question": "使用哪种数据库？",
            "header": "数据库",
            "options": [
                {"label": "PostgreSQL", "description": "关系型数据库"},
                {"label": "SQLite", "description": "嵌入式数据库"},
            ],
        }
    ]
}
_READ_A = {"file_path": "/srv/app.py"}
_READ_B = {"file_path": "/srv/util.py"}
_WRITE = {"file_path": "/srv/app.py", "content": "print(1)"}
_BASH = {"command": "ls"}
_SKILL_RUN_PRIVATE = {"skill_name": "private", "relative_path": "scripts/check.py"}
_DEFAULT_BUDGET: dict[str, Any] = {"total_limit": 20, "per_tool_limit": 5}


def _call(call_id: str, tool_name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=call_id, tool_name=tool_name, arguments=arguments)


# ---------- 情况：主运行时（字面量的输入，不许改） ----------


@dataclass(frozen=True)
class MainCase:
    """主运行时的一种情况。

    plans 是依次读到的 Plan 正文（None 表示不在 Plan 模式），读完后一直返回最后一个；
    registry 为 None 表示本轮可见完整内置工具集；budget 是 ToolCallBudget 的构造参数，
    None 表示由运行时按 max_tool_iterations 自建；permissions 是权限规则按工具名给出的判定。
    """

    calls: tuple[ToolCall, ...]
    profile: ToolProfile = "lead"
    allowed_skill_names: frozenset[str] | None = None
    registry: tuple[str, ...] | None = None
    mode_slug: str = "standard"
    plans: tuple[str | None, ...] = (None,)
    permissions: dict[str, str] = field(default_factory=dict)
    budget: dict[str, Any] | None = field(default_factory=lambda: dict(_DEFAULT_BUDGET))
    done_ids: tuple[str, ...] = ()
    existing_suspension: bool = False
    max_tool_iterations: int | None = None


MAIN_CASES: dict[str, MainCase] = {
    # 预算：总额度与单工具额度耗尽，以及按配置自建的默认预算。
    "budget_total_exhausted": MainCase(
        calls=(_call("t1", "Read", _READ_A),),
        budget={"total_limit": 2, "per_tool_limit": 2, "total_calls": 2, "calls_by_tool": {"Grep": 2}},
    ),
    "budget_per_tool_exhausted": MainCase(
        calls=(_call("t1", "Read", _READ_A), _call("t2", "Read", _READ_B)),
        permissions={"Read": "allow"},
        budget={"total_limit": 10, "per_tool_limit": 1},
    ),
    "default_budget_from_settings": MainCase(
        calls=(_call("t1", "Read", _READ_A), _call("t2", "Read", _READ_B)),
        permissions={"Read": "allow"},
        budget=None,
        max_tool_iterations=1,
    ),
    # Plan 白名单：EnterPlanMode 不在白名单里，所以 Plan 模式下先被白名单拒绝。
    "plan_allowlist_denied": MainCase(
        calls=(_call("t1", "Write", _WRITE),),
        plans=(_PLAN_CONTENT,),
    ),
    "enter_plan_mode_while_planning": MainCase(
        calls=(_call("t1", "EnterPlanMode", {}),),
        plans=(_PLAN_CONTENT,),
    ),
    # 入参规范化：非法入参、未知工具、不在本轮可见工具集里的工具。
    "invalid_arguments": MainCase(calls=(_call("t1", "Read", {}),)),
    "unknown_tool": MainCase(calls=(_call("t1", "NoSuchTool", {"x": 1}),)),
    "tool_outside_visible_set": MainCase(
        calls=(_call("t1", "TeamCreate", {}),),
        profile="user_agent",
        registry=("Read", "Grep"),
    ),
    "arguments_before_capability": MainCase(
        calls=(_call("t1", "TeamCreate", {"team_name": 123}),),
        profile="user_agent",
    ),
    # 能力：档位不可调用、Skill 白名单，以及能力检查先于交互工具挂起。
    "capability_denied": MainCase(
        calls=(_call("t1", "TeamCreate", {"team_name": "评审组"}),),
        profile="user_agent",
    ),
    "capability_before_interaction": MainCase(
        calls=(_call("t1", "AskUserQuestion", _QUESTION_ARGS),),
        profile="user_agent",
    ),
    "skill_not_in_allowlist": MainCase(
        calls=(_call("t1", "SkillRun", _SKILL_RUN_PRIVATE),),
        profile="user_agent",
        allowed_skill_names=frozenset({"public"}),
    ),
    "skill_in_allowlist": MainCase(
        calls=(_call("t1", "SkillRun", {**_SKILL_RUN_PRIVATE, "skill_name": "public"}),),
        profile="user_agent",
        allowed_skill_names=frozenset({"public"}),
        permissions={"SkillRun": "allow"},
    ),
    "lead_skill_without_allowlist": MainCase(
        calls=(_call("t1", "Skill", {"name": "anything"}),),
        permissions={"Skill": "allow"},
    ),
    # Plan 模式下的 Agent：只放行 Explore / Plan 两种只读子代理。
    "plan_agent_general_purpose": MainCase(
        calls=(_call("t1", "Agent", {"agent_type": "general-purpose", "prompt": "改代码"}),),
        plans=(_PLAN_CONTENT,),
    ),
    "plan_agent_delegation": MainCase(
        calls=(_call("t1", "Agent", {"agent_id": 7, "prompt": "审稿"}),),
        plans=(_PLAN_CONTENT,),
    ),
    "plan_agent_explore": MainCase(
        calls=(_call("t1", "Agent", {"agent_type": "Explore", "prompt": "找代码"}),),
        plans=(_PLAN_CONTENT,),
        permissions={"Agent": "allow"},
    ),
    # 交互工具：先校验 Plan 状态，再挂起。
    "exit_plan_not_planning": MainCase(calls=(_call("t1", "ExitPlanMode", {}),)),
    "exit_plan_empty_plan": MainCase(
        calls=(_call("t1", "ExitPlanMode", {}),),
        plans=("  \n",),
    ),
    "ask_user_question_suspends": MainCase(calls=(_call("t1", "AskUserQuestion", _QUESTION_ARGS),)),
    "enter_plan_mode_suspends": MainCase(calls=(_call("t1", "EnterPlanMode", {}),)),
    "exit_plan_mode_suspends": MainCase(
        calls=(
            _call(
                "t1",
                "ExitPlanMode",
                {"allowedPrompts": [{"tool": "Bash", "prompt": "运行测试"}]},
            ),
        ),
        plans=(_PLAN_CONTENT,),
    ),
    # 权限规则：allow / ask / deny；ask 的审批原因区分危险工具。
    "permission_allow": MainCase(calls=(_call("t1", "Write", _WRITE),), permissions={"Write": "allow"}),
    "permission_ask_dangerous": MainCase(
        calls=(_call("t1", "Bash", _BASH),),
        permissions={"Bash": "ask"},
    ),
    "permission_ask_configured": MainCase(
        calls=(_call("t1", "Read", _READ_A),),
        profile="user_agent",
        permissions={"Read": "ask"},
    ),
    "permission_deny": MainCase(calls=(_call("t1", "Write", _WRITE),), permissions={"Write": "deny"}),
    # 规范化后的入参用于执行、落库和挂起：去掉 None、补齐默认值。
    "arguments_normalized": MainCase(
        calls=(_call("t1", "Read", {**_READ_A, "offset": None}),),
        permissions={"Read": "allow"},
    ),
    "normalized_then_denied": MainCase(
        calls=(_call("t1", "Grep", {"pattern": "TODO"}),),
        permissions={"Grep": "deny"},
    ),
    "plan_write_plan_allowed": MainCase(
        calls=(_call("t1", "WritePlan", {"plan": "  # 新计划  "}),),
        plans=(_PLAN_CONTENT,),
        permissions={"WritePlan": "allow"},
    ),
    # 一批多个调用：挂起后剩余调用的处理、done_ids 顺序、恢复时跳过已完成调用、Plan 状态逐个读取。
    "batch_suspend_keeps_remaining": MainCase(
        calls=(
            _call("t1", "Read", _READ_A),
            _call("t2", "Write", _WRITE),
            _call("t3", "Grep", {"pattern": "TODO"}),
        ),
        permissions={"Read": "allow", "Write": "ask"},
    ),
    "batch_denials_then_continue": MainCase(
        calls=(
            _call("t1", "Bash", _BASH),
            _call("t2", "Read", _READ_A),
            _call("t3", "Write", {}),
            _call("t4", "Grep", {"pattern": "TODO"}),
        ),
        permissions={"Bash": "deny", "Read": "allow", "Grep": "allow"},
    ),
    "batch_resume_skips_done": MainCase(
        calls=(_call("t1", "Read", _READ_A), _call("t2", "Read", _READ_B)),
        permissions={"Read": "allow"},
        done_ids=("t1",),
    ),
    "batch_existing_suspension": MainCase(
        calls=(_call("t1", "Read", _READ_A), _call("t2", "AskUserQuestion", _QUESTION_ARGS)),
        permissions={"Read": "allow"},
        existing_suspension=True,
    ),
    "batch_interaction_after_denial": MainCase(
        calls=(_call("t1", "Write", _WRITE), _call("t2", "EnterPlanMode", {})),
        permissions={"Write": "deny"},
    ),
    "batch_plan_read_per_call": MainCase(
        calls=(_call("t1", "Read", _READ_A), _call("t2", "Write", _WRITE)),
        plans=(None, _PLAN_CONTENT),
        permissions={"Read": "allow"},
    ),
    "turbo_ignores_plan": MainCase(
        calls=(_call("t1", "Write", _WRITE),),
        mode_slug="turbo",
        plans=(_PLAN_CONTENT,),
        permissions={"Write": "allow"},
    ),
}


# ---------- 情况：子代理（字面量的输入，不许改） ----------


@dataclass(frozen=True)
class SubCase:
    """子代理的一种情况。runner 取 subagent / teammate / delegate / workflow_node，决定规格与档位；
    只有 delegate 收集被拒记录。budget 为 None 表示由运行器按规格的 max_rounds 自建。"""

    calls: tuple[ToolCall, ...]
    runner: str = "subagent"
    allowed_skill_names: frozenset[str] | None = None
    permissions: dict[str, str] = field(default_factory=dict)
    budget: dict[str, Any] | None = field(default_factory=lambda: dict(_DEFAULT_BUDGET))


SUB_CASES: dict[str, SubCase] = {
    "budget_exhausted": SubCase(
        calls=(_call("t1", "Read", _READ_A),),
        budget={"total_limit": 1, "per_tool_limit": 1, "total_calls": 1, "calls_by_tool": {"Grep": 1}},
    ),
    "default_budget_from_spec": SubCase(
        calls=(
            _call("t1", "Read", _READ_A),
            _call("t2", "Read", _READ_B),
            _call("t3", "Read", _READ_A),
        ),
        runner="subagent_one_round",
        permissions={"Read": "allow"},
        budget=None,
    ),
    "capability_denied": SubCase(calls=(_call("t1", "TeamCreate", {"team_name": "评审组"}),)),
    "unknown_tool": SubCase(calls=(_call("t1", "NoSuchTool", {"x": 1}),)),
    "skill_without_allowlist": SubCase(calls=(_call("t1", "SkillRun", _SKILL_RUN_PRIVATE),)),
    "skill_in_allowlist": SubCase(
        calls=(_call("t1", "SkillRun", _SKILL_RUN_PRIVATE),),
        allowed_skill_names=frozenset({"private"}),
        permissions={"SkillRun": "allow"},
    ),
    "permission_ask_collapsed": SubCase(calls=(_call("t1", "Bash", _BASH),), permissions={"Bash": "ask"}),
    "permission_deny": SubCase(calls=(_call("t1", "Write", _WRITE),), permissions={"Write": "deny"}),
    "permission_allow": SubCase(calls=(_call("t1", "Read", _READ_A),), permissions={"Read": "allow"}),
    # 子代理不做入参校验，原样交给网关。
    "arguments_not_validated": SubCase(
        calls=(_call("t1", "Read", {"offset": None}),),
        permissions={"Read": "allow"},
    ),
    "delegate_denials_recorded": SubCase(
        calls=(
            _call("t1", "Read", _READ_A),
            _call("t2", "Write", {"file_path": "/srv/b.py", "content": "x" * 500}),
            _call("t3", "Bash", _BASH),
            _call("t4", "Agent", {"prompt": "再派一个"}),
            _call("t5", "Grep", {"pattern": "TODO"}),
        ),
        runner="delegate",
        permissions={"Read": "allow", "Write": "ask", "Bash": "deny"},
        budget={"total_limit": 4, "per_tool_limit": 4},
    ),
    "teammate_mixed": SubCase(
        calls=(
            _call("t1", "SendMessage", {"recipient": "lead", "content": "进度"}),
            _call("t2", "TeamCreate", {}),
            _call("t3", "Bash", _BASH),
        ),
        runner="teammate",
        permissions={"SendMessage": "allow", "Bash": "ask"},
    ),
    "workflow_node_no_summary": SubCase(
        calls=(_call("t1", "Write", _WRITE), _call("t2", "Read", _READ_A)),
        runner="workflow_node",
        permissions={"Write": "ask", "Read": "allow"},
    ),
}


def _reviewer_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        agent_id=7,
        system_prompt="你是审稿人",
        model_name=None,
        tool_runtime_names=("Read", "Write", "Grep", "Bash"),
        skill_ids=(),
        skill_names=(),
        name="审稿人",
        description=None,
    )


def _runner_identity(runner: str) -> tuple[SubAgentSpec, ToolProfile, Actor, list[Any] | None]:
    """子代理情况的规格、档位、事件来源与被拒记录（只有 delegate 收集被拒记录）。"""
    if runner == "subagent":
        spec = get_subagent_spec(AgentType.EXPLORE)
        return spec, "subagent", subagent_actor(spec.runtime_name, run_id="run-11"), None
    if runner == "subagent_one_round":
        spec = SubAgentSpec(agent_type=AgentType.GENERAL_PURPOSE, system_prompt="通用", max_rounds=1)
        return spec, "subagent", subagent_actor(spec.runtime_name, run_id="run-11"), None
    if runner == "teammate":
        spec = get_subagent_spec(AgentType.GENERAL_PURPOSE)
        return spec, "teammate", teammate_actor("alice", run_id="inst-1"), None
    if runner == "delegate":
        spec = build_agent_spec(
            _reviewer_config(), display_name="delegate-审稿人", tool_profile="delegate"
        )
        actor = subagent_actor(spec.runtime_name, run_id="run-11", agent_id=spec.agent_id)
        return spec, "delegate", actor, []
    spec = build_agent_spec(
        _reviewer_config(), display_name="wf-审稿人", tool_profile="workflow_node"
    )
    actor = subagent_actor(spec.runtime_name, run_id="run-11", agent_id=spec.agent_id)
    return spec, "workflow_node", actor, None


# ---------- 观测：记录写出的结果、执行、权限查询、挂起与日志（不许改） ----------

# LogRecord 的内置属性，其余属性都来自 extra。
_LOG_BUILTIN_FIELDS = set(logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
}


def _jsonable(value: Any) -> Any:
    """转成 JSON 原生结构：集合按字母序展开，UUID 与枚举取字符串，事件来源取完整形态。"""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Actor):
        return value.to_dict_full()
    return value


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.entries: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        extra = {
            key: _jsonable(item)
            for key, item in record.__dict__.items()
            if key not in _LOG_BUILTIN_FIELDS
        }
        self.entries.append(
            {"level": record.levelname, "message": record.getMessage(), "extra": extra}
        )


@contextmanager
def _capture_logs() -> Iterator[_LogCollector]:
    """在根 logger 上收集全部日志；只记级别、正文与 extra，不记 logger 名。"""
    collector = _LogCollector()
    root = logging.getLogger()
    saved_level = root.level
    root.addHandler(collector)
    root.setLevel(logging.DEBUG)
    try:
        yield collector
    finally:
        root.removeHandler(collector)
        root.setLevel(saved_level)


@dataclass
class _Recorder:
    """替身们共用的记录本：每类副作用一个列表，按发生顺序追加。"""

    events: list[Any] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)
    executions: list[Any] = field(default_factory=list)
    evaluations: list[Any] = field(default_factory=list)
    start_calls: list[Any] = field(default_factory=list)
    awaiting: list[Any] = field(default_factory=list)
    suspensions: list[Any] = field(default_factory=list)
    statuses: list[Any] = field(default_factory=list)


class _FakeBus:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def emit(self, event: Any) -> dict[str, Any]:
        self._recorder.events.append(event.to_dict())
        return {}


class _FakeGateway:
    """工具网关替身：记录实际执行时传入的入参与上下文，不真正执行工具。"""

    def __init__(
        self,
        recorder: _Recorder,
        profile: ToolProfile,
        allowed_skill_names: frozenset[str] | None,
    ) -> None:
        self._recorder = recorder
        self.context = SimpleNamespace(profile=profile, allowed_skill_names=allowed_skill_names)

    async def run(
        self, session_id: int, tool_name: str, input_args: dict[str, Any], **kwargs: Any
    ) -> str:
        kwargs.pop("bus", None)
        self._recorder.executions.append(
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "input_args": _jsonable(input_args),
                **_jsonable(kwargs),
            }
        )
        return f"执行结果:{tool_name}"

    async def start_call(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        *,
        message_id: int | None = None,
        agent_id: int | None = None,
    ) -> Any:
        self._recorder.start_calls.append(
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "input_args": _jsonable(input_args),
                "message_id": message_id,
                "agent_id": agent_id,
            }
        )
        return SimpleNamespace(id=_INTERACTION_RECORD_ID)


class _FakeSessionService:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self.repo = SimpleNamespace(update_status=self._update_session_status)

    async def add_message(self, session_id: int, role: str, content: Any, **kwargs: Any) -> Any:
        self._recorder.messages.append(
            {"session_id": session_id, "role": role, "content": _jsonable(content), **_jsonable(kwargs)}
        )
        return SimpleNamespace(id=1000 + len(self._recorder.messages))

    async def mark_turn_status(self, turn: Any, status: str) -> None:
        self._recorder.statuses.append({"target": "turn", "id": str(turn.id), "status": status})

    async def _update_session_status(self, session: Any, status: str) -> None:
        self._recorder.statuses.append({"target": "session", "id": session.id, "status": status})


class _FakeInteractionService:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def _created(self, suspension_id: UUID, request_create: Any) -> tuple[Any, Any]:
        request = SimpleNamespace(
            id=_REQUEST_ID,
            kind=request_create.kind,
            request_payload=request_create.request_payload,
            schema_version=request_create.schema_version,
        )
        return SimpleNamespace(id=suspension_id), request

    async def create_suspension_with_request(
        self,
        session_id: int,
        turn_id: UUID,
        continuation: dict[str, Any],
        options: dict[str, Any],
        request_create: Any,
    ) -> tuple[Any, Any]:
        self._recorder.suspensions.append(
            {
                "op": "create",
                "session_id": session_id,
                "turn_id": str(turn_id),
                "continuation": _jsonable(continuation),
                "options": _jsonable(options),
                "request": request_create.model_dump(mode="json"),
            }
        )
        return self._created(_SUSPENSION_ID, request_create)

    async def continue_suspension_with_request(
        self,
        suspension_id: UUID,
        continuation: dict[str, Any],
        request_create: Any,
    ) -> tuple[Any, Any]:
        self._recorder.suspensions.append(
            {
                "op": "continue",
                "suspension_id": str(suspension_id),
                "continuation": _jsonable(continuation),
                "request": request_create.model_dump(mode="json"),
            }
        )
        return self._created(suspension_id, request_create)


class _FakePlanService:
    """依次返回给定的 Plan 正文；读完后一直返回最后一个。"""

    def __init__(self, plans: tuple[str | None, ...]) -> None:
        self._plans = plans
        self._reads = 0
        self._last: Any = None

    async def get_active(self, session_id: int) -> Any:
        content = self._plans[min(self._reads, len(self._plans) - 1)]
        self._reads += 1
        self._last = None
        if content is not None:
            self._last = SimpleNamespace(id=_PLAN_ID, content=content, version=3)
        return self._last

    async def get_active_required(self, session_id: int) -> Any:
        return self._last


def _install_permissions(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, permissions: dict[str, str]
) -> None:
    """权限规则按工具名给出判定，未列出的工具判 allow；每次查询的参数都记下来。"""

    async def _evaluate(
        self: Any,
        session_id: int,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        agent_type: str | None = None,
        workspace_id: int | None = None,
    ) -> str:
        recorder.evaluations.append(
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": _jsonable(tool_input),
                "agent_type": agent_type,
                "workspace_id": workspace_id,
            }
        )
        return permissions.get(tool_name, "allow")

    monkeypatch.setattr(PermissionService, "evaluate", _evaluate)


# ---------- 驱动：如何构造运行时与运行器、如何发起一批调用（重构时只改这一节） ----------


def _main_runtime(case: MainCase, recorder: _Recorder) -> AgentRuntime:
    """真实 __init__ 的主运行时；会话、交互、Plan、事件总线与工具网关换成记录替身。"""
    runtime = AgentRuntime(object(), user_id=_USER_ID, workspace_id=_WORKSPACE_ID, llm=object())
    overrides: dict[str, Any] = {
        "profile": case.profile,
        "allowed_skill_names": case.allowed_skill_names,
        "mode_slug": case.mode_slug,
        "gateway": _FakeGateway(recorder, case.profile, case.allowed_skill_names),
    }
    if case.profile == "user_agent":
        overrides["agent_id"] = 7
    if case.registry is not None:
        overrides["tools"] = builtin_tool_set().only(*case.registry)
    runtime.configuration = replace(runtime.configuration, **overrides)
    runtime.plan_service = _FakePlanService(case.plans)
    session_service = _FakeSessionService(recorder)
    # 会话状态改经 SessionService.set_status 写入，替身的记录方法挂在 repo 上，这里转接过去。
    session_service.set_status = session_service.repo.update_status  # type: ignore[attr-defined]
    runtime.sessions = session_service
    runtime.interaction_service = _FakeInteractionService(recorder)
    runtime.bus = _FakeBus(recorder)

    async def _mark_awaiting(record: Any) -> None:
        recorder.awaiting.append(record.id)

    runtime.tool_service = SimpleNamespace(mark_call_awaiting=_mark_awaiting)
    return runtime


async def _drive_main(case: MainCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    recorder = _Recorder()
    _install_permissions(monkeypatch, recorder, case.permissions)
    if case.max_tool_iterations is not None:
        monkeypatch.setattr(app_settings, "max_tool_iterations", case.max_tool_iterations)
    runtime = _main_runtime(case, recorder)
    budget = None
    if case.budget is not None:
        budget = ToolCallBudget(**json.loads(json.dumps(case.budget)))
    existing_suspension = None
    if case.existing_suspension:
        existing_suspension = SimpleNamespace(id=_EXISTING_SUSPENSION_ID)
    tool_calls = list(case.calls)
    done_ids = list(case.done_ids)
    with _capture_logs() as logs:
        result = await runtime._interactions().run_tool_calls(
            _SESSION_ID,
            SimpleNamespace(id=_SESSION_ID),
            SimpleNamespace(id=_TURN_ID, session_id=_SESSION_ID),
            ORCHESTRATOR_ACTOR,
            _ASSISTANT_MESSAGE_ID,
            tool_calls,
            done_ids,
            "附加上下文",
            existing_suspension=existing_suspension,
            budget=budget,
        )
    suspended = None
    if result is not None:
        suspended = _jsonable(asdict(result))
    return _main_observation(recorder, logs, suspended, done_ids, tool_calls, budget)


async def _drive_sub(case: SubCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    recorder = _Recorder()
    _install_permissions(monkeypatch, recorder, case.permissions)
    spec, profile, actor, denied_calls = _runner_identity(case.runner)
    runner = SubAgentRunner(object(), bus=_FakeBus(recorder), llm=object())  # type: ignore[arg-type]
    gateway = _FakeGateway(recorder, profile, case.allowed_skill_names)
    budget = None
    if case.budget is not None:
        budget = ToolCallBudget(**json.loads(json.dumps(case.budget)))
    messages: list[dict[str, Any]] = []
    with _capture_logs() as logs:
        await runner._execute_tool_calls(
            _SESSION_ID,
            actor,
            list(case.calls),
            messages,
            gateway,  # type: ignore[arg-type]
            spec,
            _TURN_ID,
            _USER_ID,
            _WORKSPACE_ID,
            allowed_skill_names=case.allowed_skill_names,
            budget=budget,
            denied_calls=denied_calls,
        )
    return _sub_observation(recorder, logs, messages, denied_calls, budget)


# ---------- 断言：观测的组成与比对（不许改） ----------


def _main_observation(
    recorder: _Recorder,
    logs: _LogCollector,
    suspended: dict[str, Any] | None,
    done_ids: list[str],
    tool_calls: list[ToolCall],
    budget: ToolCallBudget | None,
) -> dict[str, Any]:
    return {
        "suspended": suspended,
        "done_ids": list(done_ids),
        "tool_calls_after": [_jsonable(tool_call.to_dict()) for tool_call in tool_calls],
        "events": recorder.events,
        "messages": recorder.messages,
        "executions": recorder.executions,
        "permission_queries": recorder.evaluations,
        "start_calls": recorder.start_calls,
        "awaiting": recorder.awaiting,
        "suspensions": recorder.suspensions,
        "statuses": recorder.statuses,
        "logs": logs.entries,
        "budget": budget.to_dict() if budget is not None else None,
    }


def _sub_observation(
    recorder: _Recorder,
    logs: _LogCollector,
    messages: list[dict[str, Any]],
    denied_calls: list[Any] | None,
    budget: ToolCallBudget | None,
) -> dict[str, Any]:
    denied = None
    summary = None
    if denied_calls is not None:
        denied = [_jsonable(asdict(item)) for item in denied_calls]
        if denied_calls:
            summary = _render_denied_summary(denied_calls)
    return {
        "events": recorder.events,
        "messages": _jsonable(messages),
        "executions": recorder.executions,
        "permission_queries": recorder.evaluations,
        "denied_calls": denied,
        "denied_summary": summary,
        "logs": logs.entries,
        "budget": budget.to_dict() if budget is not None else None,
    }


def _ordered(value: Any) -> Any:
    """连同字典键序一起比较。"""
    if isinstance(value, dict):
        return ("dict", tuple((key, _ordered(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_ordered(item) for item in value))
    return value


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()


def _assert_matches(case_id: str, actual: dict[str, Any]) -> None:
    expected = EXPECTED[case_id]
    for key in expected:
        assert actual[key] == expected[key], f"{case_id} 的 {key} 与黄金字面量不一致"
    assert actual == expected
    assert _ordered(actual) == _ordered(expected), f"{case_id} 取值相同但字典键序不同"


def test_golden_literal_matches_its_digest() -> None:
    """字面量自身与摘要一致，防止字面量被悄悄改动。"""
    assert _sha256(EXPECTED) == EXPECTED_SHA256


def test_every_case_has_a_literal() -> None:
    expected_ids = {f"main/{name}" for name in MAIN_CASES} | {f"subagent/{name}" for name in SUB_CASES}
    assert set(EXPECTED) == expected_ids


@pytest.mark.parametrize("name", list(MAIN_CASES))
async def test_main_agent_review_matches_golden(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = await _drive_main(MAIN_CASES[name], monkeypatch)

    _assert_matches(f"main/{name}", actual)


@pytest.mark.parametrize("name", list(SUB_CASES))
async def test_subagent_review_matches_golden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = await _drive_sub(SUB_CASES[name], monkeypatch)

    _assert_matches(f"subagent/{name}", actual)
