"""ReAct 循环黄金测试：固化主 Agent、子代理与队友循环的事件序列、落库序列、日志与返回值。

主 Agent 驱动真实的 AgentRuntime._produce 与 _produce_resume（准入提交之后的后台执行体，含挂起、
恢复与回合完成），子代理驱动真实的 SubAgentRunner.run，队友驱动真实的 SubAgentRunner.run_teammate。
只替换仓储与外围服务：会话、工具调用记录与子代理运行记录换成按发生顺序记账的仓储替身，
SessionService、ToolService、ToolGateway、TeamService 与审核规则链都走真实代码，工具只用无副作用的
echo 与 Weather。逐种情况记录：发给模型的请求（调用参数、系统指令、消息、工具名）、bus 事件
（to_dict()）、按顺序记下的落库与状态变化（会话消息、工具调用记录的状态流转、会话与轮次状态、
压缩快照、交互暂停点、子代理运行记录、队友历史写回）以及 commit / flush / rollback 的先后、
日志（级别、正文、extra 与异常，不含 logger 名，耗时字段归一化）、返回值（子代理与队友的报告；
主 Agent 的执行体没有返回值，循环结果体现在轮次完成消息、挂起提交与交互事件上），
与 tests/fixtures/react_loop_golden.py 的字面量逐项比对（含字典键序），并用 SHA-256 再校验字面量本身。
字面量由阶段 6a 重构前的代码实际产出生成；此后只允许改动 import 与本文件「驱动」一节，
字面量、情况、替身、观测与断言一个都不许改。
"""

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import hooks as hooks_module
from agent.service import AgentDefinitionService, AgentRuntimeConfig
from core.config import settings as app_settings
from core.events import BlockType, ToolInfo, Usage
from harness.app.ports import (
    ModelEventKind,
    ModelSettings,
    ModelStreamEvent,
    ModelTurnResult,
    ResolvedModel,
)
from harness.domain.memory import MemoryRecall
from harness.domain.messages import ThinkingBlock, ToolCall
from harness.domain.roles.subagent import AgentType, SubAgentSpec
from harness.domain.tool import ShellAccess
from hooks import HookEvent, HookOutcome, HookRegistry
from interaction.service import InteractionService
from permission.service import PermissionService
from plan.service import PlanService
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner, build_agent_spec
from session.repository import SessionRepository
from skill.service import SkillService
from task.service import TaskService
from team.service import TeamService
from team.subagent_run_repository import SubAgentRunRepository
from tests.fixtures.react_loop_golden import EXPECTED, EXPECTED_SHA256
from tools.models import ToolCallRecord
from tools.repository import ToolCatalogRepository, ToolRepository

pytestmark = pytest.mark.anyio

_SESSION_ID = 1
_USER_ID = 3
_WORKSPACE_ID = 2
_TURN_ID = UUID("00000000-0000-4000-8000-0000000b0001")
_REQUEST_ID = UUID("00000000-0000-4000-8000-0000000b0002")
_SUSPENSION_ID = UUID("00000000-0000-4000-8000-0000000b0003")
_OWNER = "owner-loop-golden"
_STARTED_MESSAGE_ID = 41
_ASSISTANT_MESSAGE_ID = 42
_AWAITING_RECORD_ID = 7
_CWD = "/srv/loop"
_TODAY = date(2026, 5, 6)
_USER_CONTENT = "查一下北京天气，再把结果回显一遍"
_SUB_PROMPT = "查北京天气并汇报"
_INSTANCE_ID = "inst-golden"
_ADDITIONAL = "审批已通过，可以继续。"

# 固定与 .env 无关的配置；主 Agent 的轮次上限按情况另行设置。
_FIXED_SETTINGS: dict[str, Any] = {
    "llm_provider": "anthropic",
    "anthropic_model": "claude-env",
    "anthropic_subagent_fast_model": "claude-fast",
    "max_tool_iterations": 3,
}


# ---------- 模型回复与工具调用（字面量的输入，不许改） ----------


@dataclass(frozen=True)
class Reply:
    """模型的一次回复：思考、文本、工具调用依次成块下发；stop_reason 为 None 表示上游未给出。"""

    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    thinking: str = ""
    stop_reason: str | None = "end_turn"


def _echo(call_id: str, text: str) -> ToolCall:
    return ToolCall(id=call_id, tool_name="echo", arguments={"text": text})


def _weather(call_id: str, city: str) -> ToolCall:
    return ToolCall(id=call_id, tool_name="Weather", arguments={"city": city})


_QUESTION = "要查哪个城市的天气？"
_QUESTION_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": _QUESTION,
            "header": "城市",
            "options": [
                {"label": "北京", "description": "首都"},
                {"label": "上海", "description": "直辖市"},
            ],
        }
    ]
}
# 审核规范化后的提问入参：补出默认的 multiSelect。
_QUESTION_NORMALIZED: dict[str, Any] = {
    "questions": [{**_QUESTION_INPUT["questions"][0], "multiSelect": False}]
}
_RECALL = MemoryRecall(
    instruction="- [tone](memory:13@1) - 喜欢简洁的语气",
    message_prefix="<relevant_memories>回答尽量简洁</relevant_memories>",
)


# ---------- 情况：主 Agent（字面量的输入，不许改） ----------


@dataclass(frozen=True)
class MainCase:
    """主 Agent 的一种情况。

    replies 是主模型依次给出的回复；permissions 按工具名给出权限规则的判定，未列出的为 allow。
    stop_continuations 是 Stop hook 要求续跑的次数；recall 为 True 时记忆召回有结果。
    turbo_tools 是极速模式放行的工具（现状为空，这里放开 echo，才能观察到最后一轮收起工具）。
    resume 非空时走人工交互恢复：approve / deny 恢复工具审批，answer / cancel 恢复提问。
    bound 为 False 时运行时不绑定执行租约；compaction_threshold 非空时按该阈值触发压缩。
    """

    replies: tuple[Reply, ...]
    mode_slug: str = "standard"
    max_tool_iterations: int = 3
    permissions: dict[str, str] = field(default_factory=dict)
    stop_continuations: int = 0
    recall: bool = False
    turbo_tools: tuple[str, ...] = ()
    resume: str | None = None
    bound: bool = True
    compaction_threshold: int | None = None


_TOOL_ROUND = "tool_use"
_TURBO_ECHOES = tuple(
    Reply(calls=(_echo(f"t{index}", f"第{index}轮"),), stop_reason=_TOOL_ROUND)
    for index in range(1, 5)
)

MAIN_CASES: dict[str, MainCase] = {
    "direct_answer": MainCase(replies=(Reply(text="北京今天晴。", thinking="直接回答即可"),)),
    "direct_answer_with_recall": MainCase(replies=(Reply(text="北京晴，简洁回答。"),), recall=True),
    "multi_round_tools": MainCase(
        replies=(
            Reply(
                text="先查天气。",
                calls=(_weather("w1", "北京"), _echo("e1", "查询中")),
                stop_reason=_TOOL_ROUND,
            ),
            Reply(calls=(_echo("e2", "北京晴朗"),), thinking="再回显一次", stop_reason=_TOOL_ROUND),
            Reply(text="北京晴朗☀️。"),
        )
    ),
    "stop_hook_continues_once": MainCase(
        replies=(Reply(text="初稿。"), Reply(text="终稿。")), stop_continuations=1
    ),
    "max_iterations_finalize": MainCase(
        max_tool_iterations=2,
        replies=(
            Reply(calls=(_echo("e1", "一"),), stop_reason=_TOOL_ROUND),
            Reply(calls=(_echo("e2", "二"),), stop_reason=_TOOL_ROUND),
            Reply(text="收尾回答。", stop_reason=None),
        ),
    ),
    "turbo_last_round_without_tools": MainCase(
        mode_slug="turbo",
        turbo_tools=("echo",),
        replies=(*_TURBO_ECHOES[:3], Reply(text="极速回答。")),
    ),
    "turbo_exhausted_raises": MainCase(
        mode_slug="turbo", turbo_tools=("echo",), replies=_TURBO_ECHOES
    ),
    "turbo_exhausted_raises_unbound": MainCase(
        mode_slug="turbo", turbo_tools=("echo",), replies=_TURBO_ECHOES, bound=False
    ),
    "suspend_permission_ask": MainCase(
        permissions={"Weather": "ask"},
        replies=(
            Reply(
                text="先回显再查天气。",
                calls=(_echo("e1", "开始"), _weather("w1", "北京"), _echo("e2", "结束")),
                stop_reason=_TOOL_ROUND,
            ),
        ),
    ),
    "suspend_user_question": MainCase(
        replies=(
            Reply(
                text="需要先确认城市。",
                calls=(ToolCall(id="q1", tool_name="AskUserQuestion", arguments=_QUESTION_INPUT),),
                stop_reason=_TOOL_ROUND,
            ),
        ),
    ),
    "resume_approved_then_answer": MainCase(resume="approve", replies=(Reply(text="北京晴朗。"),)),
    "resume_denied_then_answer": MainCase(resume="deny", replies=(Reply(text="未能查询天气。"),)),
    "resume_question_answered": MainCase(
        resume="answer",
        replies=(
            Reply(calls=(_weather("w2", "北京"),), stop_reason=_TOOL_ROUND),
            Reply(text="已按北京查询：晴朗。"),
        ),
    ),
    "resume_suspends_again": MainCase(
        resume="approve_then_ask", permissions={"Weather": "ask"}, replies=()
    ),
    "resume_loop_suspends": MainCase(
        resume="answer",
        permissions={"Weather": "ask"},
        replies=(Reply(text="再查上海。", calls=(_weather("w2", "上海"),), stop_reason=_TOOL_ROUND),),
    ),
    "resume_question_cancelled": MainCase(resume="cancel", replies=()),
    "compaction_triggered": MainCase(
        compaction_threshold=20,
        replies=(
            Reply(calls=(_echo("e1", "压缩前"),), stop_reason=_TOOL_ROUND),
            Reply(text="压缩后回答。"),
        ),
    ),
}


@dataclass(frozen=True)
class Resume:
    """挂起时冻结的现场：本轮助手消息的块、已完成的工具结果、待处理工具（首个对应 awaiting 记录）、
    交互类型、请求载荷与用户已提交的响应。"""

    kind: str
    assistant_blocks: tuple[ToolCall, ...]
    done: tuple[str, ...]
    pending: tuple[ToolCall, ...]
    request_payload: dict[str, Any]
    response: dict[str, Any]


_APPROVAL_PAYLOAD: dict[str, Any] = {
    "tool": {"id": "w1", "name": "Weather", "input": {"city": "北京"}},
    "reason": "该工具已被配置为需审批",
    "allowed_decisions": ["allow_once", "always_allow", "deny"],
    "can_update_input": True,
}
_QUESTION_PAYLOAD: dict[str, Any] = {
    "questions": [
        {
            "question": _QUESTION,
            "header": "城市",
            "options": [
                {"label": "北京", "description": "首都"},
                {"label": "上海", "description": "直辖市"},
            ],
            "multiSelect": False,
        }
    ]
}
_APPROVAL_CALLS = (_echo("e1", "开始"), _weather("w1", "北京"), _echo("e2", "结束"))
_QUESTION_CALL = ToolCall(id="q1", tool_name="AskUserQuestion", arguments=_QUESTION_NORMALIZED)

_RESUMES: dict[str, Resume] = {
    "approve": Resume(
        kind="tool_approval",
        assistant_blocks=_APPROVAL_CALLS,
        done=("e1",),
        pending=_APPROVAL_CALLS[1:],
        request_payload=_APPROVAL_PAYLOAD,
        response={"decision": "allow_once"},
    ),
    "deny": Resume(
        kind="tool_approval",
        assistant_blocks=_APPROVAL_CALLS,
        done=("e1",),
        pending=_APPROVAL_CALLS[1:],
        request_payload=_APPROVAL_PAYLOAD,
        response={"decision": "deny", "message": "不允许查天气"},
    ),
    "approve_then_ask": Resume(
        kind="tool_approval",
        assistant_blocks=(_weather("w1", "北京"), _weather("w3", "上海")),
        done=(),
        pending=(_weather("w1", "北京"), _weather("w3", "上海")),
        request_payload=_APPROVAL_PAYLOAD,
        response={"decision": "allow_once"},
    ),
    "answer": Resume(
        kind="user_question",
        assistant_blocks=(_QUESTION_CALL,),
        done=(),
        pending=(_QUESTION_CALL,),
        request_payload=_QUESTION_PAYLOAD,
        response={"decision": "submit", "answers": {_QUESTION: "北京"}},
    ),
    "cancel": Resume(
        kind="user_question",
        assistant_blocks=(_QUESTION_CALL,),
        done=(),
        pending=(_QUESTION_CALL,),
        request_payload=_QUESTION_PAYLOAD,
        response={"decision": "cancel"},
    ),
}
# 挂起前已消耗的工具额度，随续跑状态恢复。
_RESUME_BUDGET: dict[str, Any] = {
    "total_limit": 12,
    "per_tool_limit": 3,
    "total_calls": 2,
    "calls_by_tool": {"echo": 1, "Weather": 1},
}


# ---------- 情况：子代理与队友（字面量的输入，不许改） ----------


@dataclass(frozen=True)
class SubCase:
    """子代理的一种情况。

    kind 为 run 时驱动内置档位的一次性子代理，为 delegate 时驱动被委派的自建 Agent，
    为 teammate 时驱动队友单次推进。replies 用完后模型替身抛异常，用于观察失败收尾。
    history 是队友已落库的历史（None 表示首次唤醒），inbox 是未读消息，claimable 表示有一条可认领任务。
    """

    kind: str
    replies: tuple[Reply, ...]
    permissions: dict[str, str] = field(default_factory=dict)
    max_rounds: int = 3
    history: tuple[dict[str, Any], ...] | None = None
    inbox: tuple[str, ...] = ()
    claimable: bool = False


_WRITE = ToolCall(
    id="b1", tool_name="Write", arguments={"file_path": "/tmp/loop.md", "content": "草稿"}
)
_TEAMMATE_HISTORY: tuple[dict[str, Any], ...] = (
    {
        "role": "user",
        "content": (
            "<identity>You are 'writer', role: 写手, team session 1. Continue your work.</identity>"
        ),
    },
    {"role": "user", "content": "起草天气周报"},
    {"role": "assistant", "content": [{"kind": "text", "text": "好的，先等任务。"}]},
)

SUB_CASES: dict[str, SubCase] = {
    "run_direct_answer": SubCase(
        kind="run", replies=(Reply(text="北京晴。", thinking="想一想"),)
    ),
    "run_multi_round_tools": SubCase(
        kind="run",
        permissions={"Write": "ask"},
        replies=(
            Reply(text="先查。", calls=(_weather("w1", "北京"), _WRITE), stop_reason=_TOOL_ROUND),
            Reply(calls=(_echo("e1", "北京晴朗"),), stop_reason=_TOOL_ROUND),
            Reply(text="北京晴朗。"),
        ),
    ),
    "run_exhausted_truncated": SubCase(
        kind="run",
        max_rounds=2,
        replies=(
            Reply(text="中间结论一。", calls=(_echo("e1", "一"),), stop_reason=_TOOL_ROUND),
            Reply(calls=(_echo("e2", "二"),), stop_reason=_TOOL_ROUND),
        ),
    ),
    "run_exhausted_without_text": SubCase(
        kind="run",
        max_rounds=1,
        replies=(Reply(calls=(_echo("e1", "一"),), stop_reason=_TOOL_ROUND),),
    ),
    "run_model_error": SubCase(kind="run", replies=()),
    "delegate_with_denied_calls": SubCase(
        kind="delegate",
        permissions={"Write": "ask", "Weather": "deny"},
        replies=(
            Reply(
                calls=(_echo("e1", "开始"), _weather("w1", "北京"), _WRITE),
                stop_reason=_TOOL_ROUND,
            ),
            Reply(text="委派结论。"),
        ),
    ),
    "teammate_claims_task": SubCase(
        kind="teammate",
        history=_TEAMMATE_HISTORY,
        inbox=("先查北京",),
        claimable=True,
        replies=(
            Reply(calls=(_weather("w1", "北京"),), stop_reason=_TOOL_ROUND),
            Reply(text="任务完成。"),
        ),
    ),
    "teammate_no_task": SubCase(kind="teammate", replies=(Reply(text="暂无任务。"),)),
    "teammate_exhausted": SubCase(
        kind="teammate",
        replies=tuple(
            Reply(calls=(_echo(f"e{index}", f"第{index}轮"),), stop_reason=_TOOL_ROUND)
            for index in range(1, 13)
        ),
    ),
    "teammate_model_error": SubCase(kind="teammate", history=_TEAMMATE_HISTORY, replies=()),
}

_TEAM_TASK = SimpleNamespace(id=21, subject="查天气", description="查北京天气")


# ---------- 替身与记录（不许改） ----------

# 耗时类日志字段每次运行都不同，归一化后再比对。
_TIMING_FIELDS = frozenset({"duration_ms", "ttft_ms"})
# LogRecord 的内置属性，其余属性都来自 extra。
_LOG_BUILTIN_FIELDS = set(logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
}


def _jsonable(value: Any) -> Any:
    """转成 JSON 原生结构：集合按序展开，UUID 与枚举取字符串，dataclass 与命名空间取字段。"""
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
    if isinstance(value, SimpleNamespace):
        return _jsonable(vars(value))
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(value), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.entries: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        extra: dict[str, Any] = {}
        for key, item in record.__dict__.items():
            if key in _LOG_BUILTIN_FIELDS:
                continue
            extra[key] = "<ms>" if key in _TIMING_FIELDS else _jsonable(item)
        entry: dict[str, Any] = {
            "level": record.levelname,
            "message": record.getMessage(),
            "extra": extra,
        }
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            entry["exc"] = f"{type(exc).__name__}: {exc}"
        self.entries.append(entry)


@contextmanager
def _capture_logs() -> Iterator[_LogCollector]:
    """在根 logger 上收集全部日志；只记级别、正文、extra 与异常，不记 logger 名。"""
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
class _World:
    """一种情况的全部替身状态与观测：模型请求、事件，以及按发生顺序记下的读写与事务动作。

    journal 同时记 bus.emit 的事件类型，用来固化提交与事件发布的先后。
    """

    session: SimpleNamespace
    turn: SimpleNamespace
    rows: list[SimpleNamespace] = field(default_factory=list)
    snapshot: SimpleNamespace | None = None
    tool_records: dict[int, SimpleNamespace] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    journal: list[dict[str, Any]] = field(default_factory=list)
    next_record_id: int = 100

    def record(self, op: str, **fields: Any) -> None:
        self.journal.append({"op": op, **_jsonable(fields)})


def _row(message_id: int, role: str, content: Any, turn_id: UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=message_id, role=role, content=deepcopy(content), turn_id=turn_id)


def _tool_row(call: ToolCall, output: str) -> dict[str, Any]:
    return {
        "tool_use_id": call.id,
        "tool_name": call.tool_name,
        "input_args": call.arguments,
        "output": output,
        "is_error": False,
    }


class _FakeDb:
    """数据库会话替身：记事务动作；按主键读取只服务工具调用记录。"""

    def __init__(self, world: _World) -> None:
        self._world = world

    async def commit(self) -> None:
        self._world.record("db.commit")

    async def rollback(self) -> None:
        self._world.record("db.rollback")

    async def flush(self) -> None:
        self._world.record("db.flush")

    async def get(self, model: type, ident: Any) -> Any:
        self._world.record("db.get", model=model.__name__, ident=ident)
        if model is ToolCallRecord:
            return self._world.tool_records.get(ident)
        return None


class _FakeBus:
    def __init__(self, world: _World) -> None:
        self._world = world

    async def emit(self, event: Any) -> dict[str, Any]:
        data = _jsonable(event.to_dict())
        self._world.events.append(data)
        self._world.record("bus.emit", type=data["type"])
        return {}


def _message_rows(messages: Any) -> list[dict[str, Any]]:
    """领域消息还原为 {role, content}，与落库和发给模型的中立形状一致。"""
    rows: list[dict[str, Any]] = []
    for message in messages:
        name = type(message).__name__
        if name == "UserMessage":
            role = "user"
        elif name == "AssistantMessage":
            role = "assistant"
        else:
            role = "tool"
        rows.append({"role": role, "content": _jsonable(message.to_content())})
    return rows


def _instruction(instruction: Any) -> Any:
    """系统指令只记摘要：正文已由角色黄金测试固化，这里固化它在各轮之间是否变化。"""
    if instruction is None:
        return None
    if isinstance(instruction, tuple):
        return [
            {"sha256": _digest(block.text), "cache_breakpoint": block.cache_breakpoint}
            for block in instruction
        ]
    return {"sha256": _digest(instruction)}


class _FakeLLM:
    """模型替身：按脚本依次回复并记录每次请求；脚本用完后再被调用就抛异常。"""

    provider = "anthropic"

    def __init__(self, world: _World, label: str, model: str, replies: tuple[Reply, ...]) -> None:
        self._world = world
        self._label = label
        self.model = model
        self._replies = list(replies)
        self._calls = 0

    async def stream(
        self, context: Any, settings: ModelSettings
    ) -> AsyncIterator[ModelStreamEvent]:
        self._calls += 1
        self._world.requests.append(
            {
                "client": self._label,
                "settings": _jsonable(settings),
                "instruction": _instruction(context.instruction),
                "messages": _message_rows(context.messages),
                "tools": [tool.name for tool in context.tools],
            }
        )
        if not self._replies:
            raise RuntimeError(f"模型替身 {self._label} 没有第 {self._calls} 次回复")
        reply = self._replies.pop(0)
        for event in _reply_events(reply, self._calls):
            yield event


def _reply_events(reply: Reply, call_no: int) -> list[ModelStreamEvent]:
    """一次回复的中立流：思考块、文本块、工具调用块依次下发，最后是带用量的终局结果。"""
    events: list[ModelStreamEvent] = []
    ref = 0
    thinking: list[ThinkingBlock] = []
    if reply.thinking:
        thinking.append(ThinkingBlock(text=reply.thinking, signature=f"sig-{call_no}"))
        events += [
            ModelStreamEvent(
                ModelEventKind.BLOCK_START, block_ref=ref, block_type=BlockType.THINKING
            ),
            ModelStreamEvent(ModelEventKind.THINKING_DELTA, block_ref=ref, text=reply.thinking),
            ModelStreamEvent(ModelEventKind.BLOCK_STOP, block_ref=ref),
        ]
        ref += 1
    if reply.text:
        events += [
            ModelStreamEvent(ModelEventKind.BLOCK_START, block_ref=ref, block_type=BlockType.TEXT),
            ModelStreamEvent(ModelEventKind.TEXT_DELTA, block_ref=ref, text=reply.text),
            ModelStreamEvent(ModelEventKind.BLOCK_STOP, block_ref=ref),
        ]
        ref += 1
    for call in reply.calls:
        info = ToolInfo(id=call.id, name=call.tool_name, input=call.arguments)
        events += [
            ModelStreamEvent(
                ModelEventKind.BLOCK_START, block_ref=ref, block_type=BlockType.TOOL_USE
            ),
            ModelStreamEvent(ModelEventKind.TOOL_USE, block_ref=ref, tool=info),
        ]
        ref += 1
    result = ModelTurnResult(
        text=reply.text,
        tool_calls=list(reply.calls),
        thinking=thinking,
        usage=Usage(input_tokens=100 * call_no, output_tokens=10 * call_no),
        stop_reason=reply.stop_reason,
    )
    events.append(ModelStreamEvent(ModelEventKind.TURN_RESULT, result=result))
    return events


class _FakeMemory:
    """记忆 Provider 替身：召回按情况返回固定结果，继承与回合结束写入都没有结果。"""

    def __init__(self, world: _World, recall: MemoryRecall | None) -> None:
        self._world = world
        self._recall = recall

    async def recall(self, turn: Any) -> MemoryRecall | None:
        self._world.record("memory.recall", turn=turn)
        return self._recall

    async def inherited(self, turn: Any) -> MemoryRecall | None:
        self._world.record("memory.inherited", turn=turn)
        return None

    async def on_turn_completed(self, turn: Any) -> None:
        self._world.record("memory.on_turn_completed", turn=turn)

    async def after_commit(self, ticket: Any) -> None:
        self._world.record("memory.after_commit", ticket=ticket)


class _Fakes:
    """仓储与外围服务的记录替身，挂到类上后忽略 self（都按类替换，与谁来构造无关）。"""

    def __init__(self, world: _World) -> None:
        self.world = world
        self.permissions: dict[str, str] = {}
        self.continuations = 0
        self.member: SimpleNamespace | None = None
        self.inbox: tuple[str, ...] = ()
        self.claimable = False
        self.resume: Resume | None = None

    # ---- 会话仓储 ----

    async def session_get(self, session_id: int) -> SimpleNamespace:
        self.world.record("session.get", session_id=session_id)
        return self.world.session

    async def session_get_for_update(self, session_id: int) -> SimpleNamespace:
        self.world.record("session.get_for_update", session_id=session_id)
        return self.world.session

    async def get_turn(self, turn_id: UUID) -> SimpleNamespace:
        self.world.record("session.get_turn", turn_id=turn_id)
        return self.world.turn

    async def get_turn_for_update(self, turn_id: UUID) -> SimpleNamespace:
        self.world.record("session.get_turn_for_update", turn_id=turn_id)
        return self.world.turn

    async def update_status(self, session: SimpleNamespace, status: str) -> SimpleNamespace:
        self.world.record("session.update_status", status=status)
        session.status = status
        return session

    async def update_turn_status(
        self, turn: SimpleNamespace, status: str, completed_message_id: int | None = None
    ) -> SimpleNamespace:
        self.world.record(
            "session.update_turn_status", status=status, completed_message_id=completed_message_id
        )
        turn.status = status
        turn.completed_message_id = completed_message_id
        return turn

    async def latest_snapshot(self, session_id: int) -> SimpleNamespace | None:
        self.world.record("session.latest_snapshot")
        return deepcopy(self.world.snapshot)

    async def list_messages_range(
        self,
        session_id: int,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
        turn_id: UUID | None = None,
    ) -> list[SimpleNamespace]:
        self.world.record(
            "session.list_messages_range",
            after_message_id=after_message_id,
            before_message_id=before_message_id,
            turn_id=turn_id,
        )
        return [
            _row(row.id, row.role, row.content, row.turn_id)
            for row in self.world.rows
            if (after_message_id is None or row.id > after_message_id)
            and (before_message_id is None or row.id < before_message_id)
            and (turn_id is None or row.turn_id == turn_id)
        ]

    async def add_message(
        self,
        session_id: int,
        role: str,
        content: Any,
        token_estimate: int,
        turn_id: UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> SimpleNamespace:
        row = _row(max(row.id for row in self.world.rows) + 1, role, content, turn_id)
        self.world.rows.append(row)
        self.world.record(
            "session.add_message",
            id=row.id,
            role=role,
            content=content,
            token_estimate=token_estimate,
            turn_id=turn_id,
            provider=provider,
            model=model,
        )
        return row

    async def add_snapshot(
        self,
        session_id: int,
        messages: list[Any],
        summary: str,
        through_message_id: int,
        snapshot_type: str = "compact",
    ) -> SimpleNamespace:
        self.world.record(
            "session.add_snapshot",
            messages=messages,
            summary=summary,
            through_message_id=through_message_id,
            snapshot_type=snapshot_type,
        )
        self.world.snapshot = SimpleNamespace(
            messages=deepcopy(messages), through_message_id=through_message_id
        )
        return SimpleNamespace(id=1)

    # ---- 工具调用记录仓储 ----

    async def tool_start(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        message_id: int | None = None,
        tool_id: int | None = None,
        agent_id: int | None = None,
    ) -> SimpleNamespace:
        self.world.next_record_id += 1
        record = SimpleNamespace(
            id=self.world.next_record_id,
            session_id=session_id,
            tool_name=tool_name,
            input_args=input_args,
            status="running",
        )
        self.world.tool_records[record.id] = record
        self.world.record(
            "tool.start",
            id=record.id,
            tool_name=tool_name,
            input_args=input_args,
            message_id=message_id,
            tool_id=tool_id,
            agent_id=agent_id,
        )
        return record

    async def tool_succeed(self, record: SimpleNamespace, output_data: Any) -> SimpleNamespace:
        self.world.record("tool.succeed", id=record.id, output=output_data)
        record.status = "succeeded"
        return record

    async def tool_fail(self, record: SimpleNamespace, error_message: str) -> SimpleNamespace:
        self.world.record("tool.fail", id=record.id, error_message=error_message)
        record.status = "failed"
        return record

    async def tool_mark_awaiting(self, record: SimpleNamespace) -> SimpleNamespace:
        self.world.record("tool.mark_awaiting_interaction", id=record.id)
        record.status = "awaiting_interaction"
        return record

    async def tool_reject_awaiting(self, record: SimpleNamespace, message: str) -> SimpleNamespace:
        self.world.record("tool.reject_awaiting", id=record.id, message=message)
        record.status = "rejected"
        return record

    # ---- 子代理运行记录仓储 ----

    async def run_create(
        self, session_id: int, member_name: str, prompt: str, *, agent_id: int | None = None
    ) -> SimpleNamespace:
        self.world.record(
            "run.create", session_id=session_id, member_name=member_name, prompt=prompt,
            agent_id=agent_id,
        )
        return SimpleNamespace(id=31)

    async def run_succeed(self, run: SimpleNamespace, report: str) -> SimpleNamespace:
        self.world.record("run.succeed", id=run.id, report=report)
        return run

    async def run_fail(self, run: SimpleNamespace, error_message: str) -> SimpleNamespace:
        self.world.record("run.fail", id=run.id, error_message=error_message)
        return run

    # ---- 权限规则 ----

    async def evaluate(
        self,
        session_id: int,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        agent_type: str | None = None,
        workspace_id: int | None = None,
    ) -> str:
        self.world.record(
            "permission.evaluate",
            tool_name=tool_name,
            tool_input=tool_input,
            agent_type=agent_type,
            workspace_id=workspace_id,
        )
        return self.permissions.get(tool_name, "allow")

    # ---- 人工交互 ----

    def _created(self, request_create: Any) -> tuple[SimpleNamespace, SimpleNamespace]:
        request = SimpleNamespace(
            id=_REQUEST_ID,
            kind=request_create.kind,
            request_payload=request_create.request_payload,
            schema_version=request_create.schema_version,
        )
        return SimpleNamespace(id=_SUSPENSION_ID), request

    async def create_suspension_with_request(
        self,
        session_id: int,
        turn_id: UUID,
        continuation_payload: dict[str, Any],
        resolution_policy: dict[str, Any],
        request: Any,
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        self.world.record(
            "interaction.create_suspension_with_request",
            turn_id=turn_id,
            continuation=continuation_payload,
            resolution_policy=resolution_policy,
            request=request.model_dump(mode="json"),
        )
        return self._created(request)

    async def continue_suspension_with_request(
        self, suspension_id: UUID, continuation_payload: dict[str, Any], request: Any
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        self.world.record(
            "interaction.continue_suspension_with_request",
            suspension_id=suspension_id,
            continuation=continuation_payload,
            request=request.model_dump(mode="json"),
        )
        return self._created(request)

    def _resume_request(self) -> SimpleNamespace:
        assert self.resume is not None
        return SimpleNamespace(
            id=_REQUEST_ID,
            suspension_id=_SUSPENSION_ID,
            kind=self.resume.kind,
            request_payload=deepcopy(self.resume.request_payload),
            response_payload=deepcopy(self.resume.response),
            tool_call_id=_AWAITING_RECORD_ID,
            tool_use_id=self.resume.pending[0].id,
        )

    async def get_request_required(
        self, request_id: UUID, *, session_id: int | None = None
    ) -> SimpleNamespace:
        self.world.record("interaction.get_request_required", request_id=request_id)
        return self._resume_request()

    async def lock_suspension_required(
        self, suspension_id: UUID, *, session_id: int | None = None
    ) -> SimpleNamespace:
        self.world.record("interaction.lock_suspension_required", suspension_id=suspension_id)
        assert self.resume is not None
        pending = [
            {
                "tool_use_id": call.id,
                "tool_name": call.tool_name,
                "input": call.arguments,
                "tool_call_id": _AWAITING_RECORD_ID if index == 0 else None,
            }
            for index, call in enumerate(self.resume.pending)
        ]
        return SimpleNamespace(
            id=_SUSPENSION_ID,
            status="resuming",
            turn_id=_TURN_ID,
            continuation_payload={
                "assistant_message_id": _ASSISTANT_MESSAGE_ID,
                "pending": pending,
                "done_tool_use_ids": list(self.resume.done),
                "additional_context": _ADDITIONAL,
                "tool_budget": deepcopy(_RESUME_BUDGET),
            },
        )

    async def mark_suspension_resolved(self, suspension_id: UUID) -> SimpleNamespace:
        self.world.record("interaction.mark_suspension_resolved", suspension_id=suspension_id)
        return SimpleNamespace(id=suspension_id, status="resolved")

    async def cancel_suspension(
        self, suspension_id: UUID, responded_by: int | None = None, *, session_id: int | None = None
    ) -> tuple[SimpleNamespace, list[SimpleNamespace]]:
        self.world.record(
            "interaction.cancel_suspension", suspension_id=suspension_id, responded_by=responded_by
        )
        return SimpleNamespace(id=suspension_id, status="cancelled"), [self._resume_request()]

    # ---- 团队成员、收件箱与任务认领 ----

    async def list_members(self, session_id: int) -> list[Any]:
        self.world.record("team.list_members", session_id=session_id)
        return []

    async def get_member(self, session_id: int, name: str) -> SimpleNamespace | None:
        self.world.record("team.get_member", name=name)
        return self.member

    async def read_inbox(self, session_id: int, recipient: str, *args: Any) -> list[Any]:
        self.world.record("team.read_inbox", recipient=recipient)
        return [SimpleNamespace(sender="lead", content=text) for text in self.inbox]

    async def peek_inbox(self, session_id: int, recipient: str, *args: Any) -> list[Any]:
        self.world.record("team.peek_inbox", recipient=recipient)
        return []

    async def list_claimable_tasks(self, session_id: int) -> list[Any]:
        self.world.record("team.list_claimable_tasks", session_id=session_id)
        if self.claimable:
            self.claimable = False
            return [_TEAM_TASK]
        return []

    async def claim_task(self, session_id: int, task_id: int, owner: str) -> tuple[bool, str]:
        self.world.record("team.claim_task", task_id=task_id, owner=owner)
        return True, ""

    async def save_member_history(
        self, session_id: int, name: str, history: str | None
    ) -> SimpleNamespace:
        self.world.record("team.save_member_history", name=name, history=json.loads(history or ""))
        return SimpleNamespace()

    async def update_member_runtime(self, session_id: int, name: str, **kwargs: Any) -> Any:
        self.world.record("team.update_member_runtime", name=name, **kwargs)
        return SimpleNamespace()

    # ---- 外围读取与 Stop hook ----

    async def get_catalog(self, workspace_id: int, skill_ids: list[int] | None = None) -> str:
        self.world.record("skill.get_catalog", workspace_id=workspace_id, skill_ids=skill_ids)
        return "- weather-report: 天气播报"

    async def get_active(self, session_id: int) -> None:
        self.world.record("plan.get_active", session_id=session_id)

    async def emit_snapshot(self, session_id: int, reason: str | None = None) -> None:
        self.world.record("task.emit_snapshot", session_id=session_id, reason=reason)

    async def list_delegate_catalog(self, workspace_id: int) -> tuple[()]:
        self.world.record("agent.list_delegate_catalog", workspace_id=workspace_id)
        return ()

    async def stop_hook(self, ctx: Any) -> HookOutcome | None:
        """记录 Stop hook 看到的上下文条数；还有续跑次数时要求续跑一次。"""
        self.world.record("hook.stop", actor=ctx.actor.name, message_count=len(ctx.messages))
        if self.continuations <= 0:
            return None
        self.continuations -= 1
        return HookOutcome(continuation="[系统检查] 请补充穿衣建议")


class _FixedDate(date):
    @classmethod
    def today(cls) -> _FixedDate:
        return cls(_TODAY.year, _TODAY.month, _TODAY.day)


# 本会话本回合之前的历史：一问一答。
_HISTORY_ROWS: tuple[tuple[int, str, Any], ...] = (
    (30, "user", "昨天的天气怎么样？"),
    (31, "assistant", [{"kind": "text", "text": "昨天多云。"}]),
)


def _main_world(case: MainCase) -> _World:
    """会话、轮次与已落库的消息；恢复情况另带挂起时的助手消息、已完成的工具结果与 awaiting 记录。"""
    status = "running" if case.resume is None else "awaiting_interaction"
    session = SimpleNamespace(
        id=_SESSION_ID,
        title="天气",
        status=status,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        agent_id=None,
        mode_slug=case.mode_slug,
        system_prompt="你是天气助手。",
        model_name=None,
        model_provider_id=None,
        thinking_effort="fast",
        extra={},
    )
    turn = SimpleNamespace(
        id=_TURN_ID,
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        status=status,
        started_message_id=_STARTED_MESSAGE_ID,
        completed_message_id=None,
        lease_owner=_OWNER,
    )
    world = _World(session=session, turn=turn)
    world.rows = [_row(message_id, role, content) for message_id, role, content in _HISTORY_ROWS]
    world.rows.append(_row(_STARTED_MESSAGE_ID, "user", _USER_CONTENT, _TURN_ID))
    if case.resume is None:
        return world
    resume = _RESUMES[case.resume]
    blocks = [call.to_dict() for call in resume.assistant_blocks]
    world.rows.append(_row(_ASSISTANT_MESSAGE_ID, "assistant", blocks, _TURN_ID))
    for call in resume.assistant_blocks:
        if call.id in resume.done:
            content = _tool_row(call, call.arguments["text"])
            world.rows.append(_row(world.rows[-1].id + 1, "tool", content, _TURN_ID))
    target = resume.pending[0]
    world.tool_records[_AWAITING_RECORD_ID] = SimpleNamespace(
        id=_AWAITING_RECORD_ID,
        session_id=_SESSION_ID,
        tool_name=target.tool_name,
        input_args=dict(target.arguments),
        status="awaiting_interaction",
    )
    return world


def _sub_world() -> _World:
    world = _main_world(MainCase(replies=()))
    world.rows = [_row(1, "user", "占位")]
    return world


def _member(case: SubCase) -> SimpleNamespace:
    history = None
    if case.history is not None:
        history = json.dumps(list(case.history), ensure_ascii=False)
    return SimpleNamespace(
        id=8, name="writer", role="写手", team_id=5, prompt="起草天气周报", history=history
    )


def _sub_spec(case: SubCase) -> SubAgentSpec:
    if case.kind == "delegate":
        config = AgentRuntimeConfig(
            agent_id=7,
            system_prompt="你是被委派的天气 Agent。",
            model_name=None,
            tool_runtime_names=("echo", "Weather", "Write"),
            skill_ids=(),
            skill_names=(),
            name="天气",
            description=None,
        )
        return build_agent_spec(config, display_name="delegate-天气", tool_profile="delegate")
    return SubAgentSpec(
        agent_type=AgentType.GENERAL_PURPOSE,
        system_prompt="你是天气子代理。",
        max_rounds=case.max_rounds,
        shell_access=ShellAccess.READ_ONLY,
    )


def _error(exc: BaseException) -> dict[str, str]:
    return {"error": f"{type(exc).__name__}: {exc}"}


# ---------- 驱动：如何构造运行时与运行器、如何把替身接进去（重构时只改这一节） ----------


def _install(monkeypatch: pytest.MonkeyPatch, fakes: _Fakes, overrides: dict[str, Any]) -> None:
    """固定配置与环境段，把各域的仓储与外围服务方法换成记录替身（都按类替换）。"""
    for key, value in {**_FIXED_SETTINGS, **overrides}.items():
        monkeypatch.setattr(app_settings, key, value)
    monkeypatch.setattr("runtime.agent.date", _FixedDate)
    monkeypatch.setattr("runtime.agent.Path", SimpleNamespace(cwd=lambda: _CWD))
    registry = HookRegistry()
    registry.register(HookEvent.STOP, fakes.stop_hook)
    monkeypatch.setattr(hooks_module, "_registry", registry)
    monkeypatch.setattr("runtime.subagent.new_team_task_instance_id", lambda: _INSTANCE_ID)

    session_methods = {
        "get": fakes.session_get,
        "get_for_update": fakes.session_get_for_update,
        "get_turn": fakes.get_turn,
        "get_turn_for_update": fakes.get_turn_for_update,
        "update_status": fakes.update_status,
        "update_turn_status": fakes.update_turn_status,
        "latest_snapshot": fakes.latest_snapshot,
        "list_messages_range": fakes.list_messages_range,
        "add_message": fakes.add_message,
        "add_snapshot": fakes.add_snapshot,
    }
    for name, method in session_methods.items():
        monkeypatch.setattr(SessionRepository, name, method)

    async def _no_catalog(self: Any, runtime_name: str) -> None:
        return None

    # 工具调用记录按主键读取不打桩：真实仓储经数据库会话替身的 get 读取，事务动作照常记账。
    monkeypatch.setattr(ToolRepository, "start", fakes.tool_start)
    monkeypatch.setattr(ToolRepository, "succeed", fakes.tool_succeed)
    monkeypatch.setattr(ToolRepository, "fail", fakes.tool_fail)
    monkeypatch.setattr(ToolRepository, "mark_awaiting_interaction", fakes.tool_mark_awaiting)
    monkeypatch.setattr(ToolRepository, "reject_awaiting", fakes.tool_reject_awaiting)
    monkeypatch.setattr(ToolCatalogRepository, "get_by_runtime_name", _no_catalog)
    monkeypatch.setattr(SubAgentRunRepository, "create", fakes.run_create)
    monkeypatch.setattr(SubAgentRunRepository, "succeed", fakes.run_succeed)
    monkeypatch.setattr(SubAgentRunRepository, "fail", fakes.run_fail)
    monkeypatch.setattr(PermissionService, "evaluate", fakes.evaluate)
    for name in (
        "create_suspension_with_request",
        "continue_suspension_with_request",
        "get_request_required",
        "lock_suspension_required",
        "mark_suspension_resolved",
        "cancel_suspension",
    ):
        monkeypatch.setattr(InteractionService, name, getattr(fakes, name))
    for name in (
        "list_members",
        "get_member",
        "read_inbox",
        "peek_inbox",
        "list_claimable_tasks",
        "claim_task",
        "save_member_history",
        "update_member_runtime",
    ):
        monkeypatch.setattr(TeamService, name, getattr(fakes, name))
    monkeypatch.setattr(SkillService, "get_catalog", fakes.get_catalog)
    monkeypatch.setattr(PlanService, "get_active", fakes.get_active)
    monkeypatch.setattr(TaskService, "emit_snapshot", fakes.emit_snapshot)
    monkeypatch.setattr(
        AgentDefinitionService, "list_delegate_catalog", fakes.list_delegate_catalog
    )


def _main_runtime(case: MainCase, world: _World) -> AgentRuntime:
    runtime = AgentRuntime(
        _FakeDb(world),  # type: ignore[arg-type]
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        llm=_FakeLLM(world, "agent", "claude-loop", case.replies),  # type: ignore[arg-type]
        memory=_FakeMemory(world, _RECALL if case.recall else None),  # type: ignore[arg-type]
    )
    compact_llm = _FakeLLM(world, "compact", "claude-compact", (Reply(text="早先问过昨天的天气。"),))
    runtime.compact_service.model = ResolvedModel.from_client(compact_llm)  # type: ignore[arg-type]
    if case.compaction_threshold is not None:
        runtime.compact_service.threshold = case.compaction_threshold
    if case.bound:
        runtime.bind_execution(_FakeBus(world), _OWNER)  # type: ignore[arg-type]
    else:
        runtime.bus = _FakeBus(world)  # type: ignore[assignment]
    return runtime


async def _drive_main(case: MainCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _main_world(case)
    fakes = _Fakes(world)
    fakes.permissions = dict(case.permissions)
    fakes.continuations = case.stop_continuations
    if case.resume is not None:
        fakes.resume = _RESUMES[case.resume]
    _install(monkeypatch, fakes, {"max_tool_iterations": case.max_tool_iterations})
    monkeypatch.setattr("runtime.agent.TURBO_ALLOWED_TOOLS", case.turbo_tools)
    runtime = _main_runtime(case, world)
    result: Any = None
    with _capture_logs() as logs:
        try:
            if case.resume is not None:
                result = await runtime._produce_resume(_SESSION_ID, _REQUEST_ID)
            else:
                result = await runtime._produce(_SESSION_ID, _TURN_ID, _USER_CONTENT)
        except Exception as exc:  # noqa: BLE001  异常本身也是观测项
            result = _error(exc)
    return _main_observation(world, logs, result)


async def _drive_sub(case: SubCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _sub_world()
    fakes = _Fakes(world)
    fakes.permissions = dict(case.permissions)
    fakes.member = _member(case)
    fakes.inbox = case.inbox
    fakes.claimable = case.claimable
    _install(monkeypatch, fakes, {})
    runner = SubAgentRunner(
        _FakeDb(world),  # type: ignore[arg-type]
        bus=_FakeBus(world),  # type: ignore[arg-type]
        llm=_FakeLLM(world, "subagent", "claude-sub", case.replies),  # type: ignore[arg-type]
        memory=_FakeMemory(world, None),  # type: ignore[arg-type]
    )
    spec = _sub_spec(case)
    scope: dict[str, Any] = {
        "parent_turn_id": _TURN_ID,
        "user_id": _USER_ID,
        "workspace_id": _WORKSPACE_ID,
        "parent_allowed_tool_names": frozenset({"echo", "Weather", "Write"}),
        "allowed_skill_names": frozenset(),
    }
    result: Any
    with _capture_logs() as logs:
        try:
            if case.kind == "teammate":
                result = await runner.run_teammate(_SESSION_ID, "writer", spec, **scope)
            else:
                result = await runner.run(_SESSION_ID, _SUB_PROMPT, spec, **scope)
        except Exception as exc:  # noqa: BLE001  异常本身也是观测项
            result = _error(exc)
    return _sub_observation(world, logs, result)


# ---------- 断言：观测的组成与比对（不许改） ----------


def _main_observation(world: _World, logs: _LogCollector, result: Any) -> dict[str, Any]:
    return {
        "result": _jsonable(result),
        "requests": world.requests,
        "events": world.events,
        "journal": world.journal,
        "final_status": {"session": world.session.status, "turn": world.turn.status},
        "tool_records": {str(key): record.status for key, record in world.tool_records.items()},
        "logs": logs.entries,
    }


def _sub_observation(world: _World, logs: _LogCollector, result: Any) -> dict[str, Any]:
    return {
        "result": _jsonable(result),
        "requests": world.requests,
        "events": world.events,
        "journal": world.journal,
        "tool_records": {str(key): record.status for key, record in world.tool_records.items()},
        "logs": logs.entries,
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
    expected_ids = {f"main/{name}" for name in MAIN_CASES}
    expected_ids |= {f"subagent/{name}" for name in SUB_CASES}
    assert set(EXPECTED) == expected_ids


@pytest.mark.parametrize("name", list(MAIN_CASES))
async def test_main_agent_loop_matches_golden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = await _drive_main(MAIN_CASES[name], monkeypatch)

    _assert_matches(f"main/{name}", actual)


@pytest.mark.parametrize("name", list(SUB_CASES))
async def test_subagent_loop_matches_golden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = await _drive_sub(SUB_CASES[name], monkeypatch)

    _assert_matches(f"subagent/{name}", actual)
