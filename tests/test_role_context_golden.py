"""角色与 Context 组装黄金测试：固化发给模型的系统指令、消息与工具，以及子代理规格与输出协议。

主 Agent 驱动真实的 AgentRuntime._run_llm_loop（含轮次耗尽后不带工具的收尾），子代理驱动真实的
SubAgentRunner.run 与 run_teammate，逐次记录模型请求：模型名、系统指令（块序列记 text 与
cache_breakpoint，纯文本原样记）、消息与工具名；队友另记写回的成员历史。另固化上下文压缩
（L1 微压缩的输出、L2 压缩请求与摘要、快照写入与日志）、四种内置子代理规格的全部字段与派生结果
（含按场景解析模型）、子代理输出协议解析与路由目录、验收触发提示，以及进入 Plan Mode 的工具结果。
与 tests/fixtures/role_context_golden.py 的字面量逐项比对（含字典键序），并用 SHA-256 再校验字面量本身。
字面量由阶段 5 重构前的代码实际产出生成；此后只允许改动 import 与本文件「驱动」一节，
字面量、情况、替身、观测与断言一个都不许改。
"""

import hashlib
import json
import logging
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from datetime import date
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import hooks as hooks_module
from agent.service import AgentRuntimeConfig
from core.config import settings as app_settings
from core.errors import AgentException
from core.events import ORCHESTRATOR_ACTOR, BlockType
from harness.adapters.models.factory import resolve_subagent_model
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.app import subagent_catalog
from harness.app.compaction import ContextCompaction
from harness.app.ports import (
    ModelEventKind,
    ModelSettings,
    ModelStreamEvent,
    ModelTurnResult,
    ResolvedModel,
)
from harness.app.subagent_catalog import build_agent_spec
from harness.domain.memory import MemoryRecall
from harness.domain.messages import ToolCall
from harness.domain.roles import reports as subagent_reports
from harness.domain.roles import verification_trigger
from harness.domain.roles.compactor import Compactor
from harness.domain.roles.subagent import GLOBAL_SUBAGENT_DENIED_TOOLS, AgentType, SubAgentSpec
from harness.domain.tool import ShellAccess
from hooks import HookEvent, HookOutcome, HookRegistry
from plan.service import PlanService
from session.repository import SessionRepository
from session.service import SessionService
from skill.service import SkillService
from team.service import TeamService
from team.subagent_run_repository import SubAgentRunRepository
from tests.fixtures.harness_factory import make_agent_runtime, make_subagent_runner
from tests.fixtures.role_context_golden import EXPECTED, EXPECTED_SHA256

pytestmark = pytest.mark.anyio

_SESSION_ID = 1
_USER_ID = 3
_WORKSPACE_ID = 2
_TURN_ID = UUID("00000000-0000-4000-8000-0000000f0001")
_PARENT_TURN_ID = UUID("00000000-0000-4000-8000-0000000f0002")
_THROUGH_MESSAGE_ID = 40
_CWD = "/srv/agentos"
_TODAY = date(2026, 3, 9)
_TMP_DIR = "/tmp/golden-tmp"

# 固定与 .env 无关的配置；轮次上限按情况另行设置。
_FIXED_SETTINGS: dict[str, Any] = {
    "llm_provider": "anthropic",
    "anthropic_model": "claude-env",
    "anthropic_subagent_fast_model": "claude-fast",
    "max_tool_iterations": 3,
}

# ---------- 组装输入（字面量的输入，不许改） ----------

# 目录与提示词故意带首尾空白，固化组装时的 strip 规则。
_SKILLS = "\n- weekly-report: 周报写作指引\n- code-review: 代码审查清单\n"
_DELEGATE = "- agent_id=7 名称:审稿人 描述:负责审稿 工具:Read、Grep\n"
_SESSION_PROMPT = "  你是团队的周报助手。\n"
_AGENT_PROMPT = "\n你是自建的审稿 Agent。  "
_MEMORY = MemoryRecall(
    instruction="- [tone](memory:13@1) - 喜欢简洁的语气\n",
    message_prefix="<relevant_memories>周报分三段</relevant_memories>",
)
_PLAN_TEXT = "  # 计划\n1. 阅读代码\n2. 修改实现\n"
_BLANK = "  \n\t"
_ADDITIONAL = "审批已通过，可以继续。"
_USER_CONTENT = "帮我写本周周报"
_SUB_PROMPT = "找出周报模板所在的文件"
_SUB_MEMORY = "<relevant_memories>父回合正文</relevant_memories>"
_MAIN_TOOLS = ("Read", "Write", "Agent", "WritePlan", "ExitPlanMode")


def _tool_row(call_id: str, tool_name: str | None, output: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "content": {
            "tool_use_id": call_id,
            "tool_name": tool_name,
            "input_args": {"file_path": f"/srv/{call_id}.md"},
            "output": output,
            "is_error": False,
        },
    }


# ---------- 情况：主 Agent（字面量的输入，不许改） ----------

# 历史含 5 条长工具结果（第 2 条是保留型的 read_file），经 L1 微压缩后最旧的一条被替换为占位符。
_HISTORY: list[dict[str, Any]] = [
    {"role": "user", "content": "上周的周报写了什么"},
    {
        "role": "assistant",
        "content": [
            {"kind": "text", "text": "我先读一下历史周报。"},
            *[
                {"kind": "tool_use", "id": f"h{i}", "name": "Read", "input": {"path": f"h{i}"}}
                for i in range(1, 6)
            ],
        ],
    },
    _tool_row("h1", "Read", "甲" * 600),
    _tool_row("h2", "read_file", "乙" * 600),
    _tool_row("h3", "Grep", "丙" * 600),
    _tool_row("h4", "Read", "丁" * 600),
    _tool_row("h5", "Read", "戊" * 600),
    {"role": "assistant", "content": [{"kind": "text", "text": "上周写了进展和计划两段。"}]},
]


@dataclass(frozen=True)
class MainCase:
    """一种主 Agent 运行态。

    agent_prompt 非 None 表示会话绑定了自建 Agent（此时没有可委派目录）；plan 为 None 表示不在
    Plan Mode；stop_continuations 是 Stop hook 连续要求续跑的次数，用尽轮次后走不带工具的收尾；
    compaction_threshold 非 None 时把压缩阈值调到该值。
    """

    skills: str = ""
    delegate: str | None = None
    session_prompt: str | None = _SESSION_PROMPT
    agent_prompt: str | None = None
    memory: MemoryRecall | None = None
    plan: str | None = None
    turbo: bool = False
    additional_context: str | None = None
    max_tool_iterations: int = 3
    stop_continuations: int = 0
    compaction_threshold: int | None = None
    long_history: bool = False
    snapshot: bool = False


def _main_matrix() -> dict[str, MainCase]:
    """skills × 提示来源（会话提示、会话提示 + 委派目录、自建 Agent 提示）× 记忆目录 × Plan。

    绑定自建 Agent 的会话不能委派，所以没有「Agent 提示 + 委派目录」的组合。
    """
    prompts: dict[str, dict[str, Any]] = {
        "session": {},
        "session_delegate": {"delegate": _DELEGATE},
        "agent": {"agent_prompt": _AGENT_PROMPT},
    }
    plans = {"no_plan": None, "plan_empty": "", "plan": _PLAN_TEXT}
    cases: dict[str, MainCase] = {}
    for skills_name, skills in (("no_skills", ""), ("skills", _SKILLS)):
        for prompt_name, prompt_values in prompts.items():
            for memory_name, memory in (("no_memory", None), ("memory", _MEMORY)):
                for plan_name, plan in plans.items():
                    name = f"matrix/{skills_name}/{prompt_name}/{memory_name}/{plan_name}"
                    cases[name] = MainCase(skills=skills, memory=memory, plan=plan, **prompt_values)
    return cases


MAIN_CASES: dict[str, MainCase] = {
    **_main_matrix(),
    # 极速模式忽略 skills、委派目录、记忆与 Plan，只用固定提示词，也不带工具。
    "turbo/session": MainCase(
        skills=_SKILLS, delegate=_DELEGATE, memory=_MEMORY, plan=_PLAN_TEXT, turbo=True
    ),
    "turbo/agent": MainCase(
        skills=_SKILLS, agent_prompt=_AGENT_PROMPT, memory=_MEMORY, plan=_PLAN_TEXT, turbo=True
    ),
    "extra/session_prompt_none": MainCase(session_prompt=None),
    "extra/blank_sections": MainCase(
        skills=_BLANK,
        delegate=_BLANK,
        session_prompt=_BLANK,
        memory=MemoryRecall(instruction=_BLANK, message_prefix=None),
    ),
    "extra/agent_prompt_blank": MainCase(agent_prompt=_BLANK),
    "extra/memory_prefix_only": MainCase(memory=MemoryRecall(message_prefix="<m>仅正文</m>")),
    "extra/additional_context": MainCase(memory=_MEMORY, additional_context=_ADDITIONAL),
    "extra/finalize_without_tools": MainCase(
        skills=_SKILLS,
        memory=_MEMORY,
        plan=_PLAN_TEXT,
        additional_context=_ADDITIONAL,
        max_tool_iterations=2,
        stop_continuations=2,
    ),
    "extra/snapshot_history": MainCase(snapshot=True),
    "extra/micro_compaction": MainCase(long_history=True),
    "extra/compaction_triggered": MainCase(
        long_history=True, memory=_MEMORY, compaction_threshold=200
    ),
}


# ---------- 情况：子代理与队友（字面量的输入，不许改） ----------


def _dynamic_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        agent_id=7,
        system_prompt="\n你是审稿人，只做审阅。  ",
        model_name="reviewer-model",
        tool_runtime_names=("Read", "Grep", "Write"),
        skill_ids=(),
        skill_names=(),
        name="审稿人",
        description="负责审稿",
    )


@dataclass(frozen=True)
class SubCase:
    """一种子代理运行态。agent_type 为 None 表示动态 spec（数据库 Agent 委派）。

    kind 为 teammate 时驱动队友单次推进；history 是成员已落库的历史（None 表示首次唤醒），
    inbox 是待读消息，claimable 是可认领任务。memory 是父回合冻结的记忆正文。
    """

    agent_type: AgentType | None = AgentType.GENERAL_PURPOSE
    memory: str | None = None
    kind: str = "run"
    role: str = "资料员"
    history: list[dict[str, Any]] | None = None
    inbox: tuple[str, ...] = ()
    claimable: bool = False


def _sub_matrix() -> dict[str, SubCase]:
    cases: dict[str, SubCase] = {}
    for agent_type in AgentType:
        for memory_name, memory in (("no_memory", None), ("memory", _SUB_MEMORY)):
            cases[f"run/{agent_type.value}/{memory_name}"] = SubCase(
                agent_type=agent_type, memory=memory
            )
    return cases


_TEAMMATE_HISTORY: list[dict[str, Any]] = [
    {"role": "user", "content": "<identity>You are 'writer', role: 资料员, team session 1. "
     "Continue your work.</identity>"},
    {"role": "user", "content": "整理本周周报要点"},
    {"role": "assistant", "content": [{"kind": "text", "text": "已整理两条。"}]},
]

SUB_CASES: dict[str, SubCase] = {
    **_sub_matrix(),
    "run/dynamic/no_memory": SubCase(agent_type=None),
    "run/dynamic/memory": SubCase(agent_type=None, memory=_SUB_MEMORY),
    "teammate/first/no_memory": SubCase(kind="teammate"),
    "teammate/first/memory": SubCase(kind="teammate", memory=_SUB_MEMORY),
    "teammate/resume/memory": SubCase(
        kind="teammate",
        memory=_SUB_MEMORY,
        history=_TEAMMATE_HISTORY,
        inbox=("请补充风险一节",),
        claimable=True,
    ),
    "teammate/verification/memory": SubCase(
        kind="teammate", agent_type=AgentType.VERIFICATION, memory=_SUB_MEMORY, role="验收员"
    ),
    "teammate/explore/no_memory": SubCase(kind="teammate", agent_type=AgentType.EXPLORE),
    "teammate/resume/no_memory": SubCase(kind="teammate", history=_TEAMMATE_HISTORY),
}


# ---------- 情况：上下文压缩（字面量的输入，不许改） ----------

_L1_ROWS: list[dict[str, Any]] = [
    {"role": "user", "content": "排查一下报错"},
    _tool_row("c1", "Grep", "长" * 501),
    _tool_row("c2", "read_file", "读" * 900),
    _tool_row("c3", None, "空" * 700),
    {
        "role": "tool",
        "content": {"tool_use_id": "c4", "input_args": {}, "output": "缺" * 800, "is_error": False},
    },
    _tool_row("c5", "Grep", "短" * 500),
    _tool_row("c6", "Grep", ["非字符串输出"]),
    {"role": "tool", "content": "不是字典的工具结果"},
    _tool_row("c7", "Grep", "近" * 900),
    {"role": "assistant", "content": [{"kind": "text", "text": "定位到了。"}]},
    _tool_row("c8", "Bash", "近" * 900),
    _tool_row("c9", "Read", "近" * 900),
]

_L2_ROWS: list[dict[str, Any]] = [
    {"role": "user", "content": "查一下日志 <tag> & \"引号\""},
    {
        "role": "assistant",
        "content": [
            {"kind": "thinking", "text": "先 grep", "signature": "sig-1", "redacted_data": None},
            {"kind": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "error"}},
        ],
    },
    _tool_row("t1", "Grep", "ERROR 连接超时"),
]


@dataclass(frozen=True)
class CompactCase:
    """kind 为 l1 时只跑微压缩；为 l2 时跑完整的 maybe_compact。summary 为 None 表示模型流没有终局结果。"""

    kind: str
    rows: list[dict[str, Any]]
    threshold: int = 10
    through_message_id: int | None = None
    summary: str | None = "已完成：定位超时。当前：待修复。决策：加重试。"


COMPACT_CASES: dict[str, CompactCase] = {
    "l1/keep_recent_only": CompactCase(kind="l1", rows=_L1_ROWS[:2] + _L1_ROWS[8:]),
    "l1/mixed": CompactCase(kind="l1", rows=_L1_ROWS),
    "l2/below_threshold": CompactCase(kind="l2", rows=_L1_ROWS, threshold=1_000_000),
    "l2/snapshot": CompactCase(kind="l2", rows=_L2_ROWS, through_message_id=_THROUGH_MESSAGE_ID),
    "l2/no_anchor": CompactCase(kind="l2", rows=_L2_ROWS),
    "l2/micro_then_summary": CompactCase(
        kind="l2", rows=_L1_ROWS, threshold=100, through_message_id=_THROUGH_MESSAGE_ID
    ),
    "l2/no_turn_result": CompactCase(kind="l2", rows=_L2_ROWS, summary=None),
}


# ---------- 情况：子代理规格、输出协议与验收触发（字面量的输入，不许改） ----------

# 内置规格的轮次上限故意取与 .env 不同的值，确认它们取自配置。
_SPEC_SETTINGS: dict[str, Any] = {
    "subagent_max_rounds": 5,
    "subagent_plan_max_rounds": 9,
    "subagent_verification_max_rounds": 17,
}

# 按场景解析模型名的几种配置：速度档有覆盖、速度档留空回落主模型、换 provider、未配置 provider。
_MODEL_WORLDS: dict[str, dict[str, Any]] = {
    "fast_configured": {},
    "fast_blank": {"anthropic_subagent_fast_model": "   "},
    "openai": {
        "llm_provider": "openai",
        "openai_model": "gpt-env",
        "openai_subagent_fast_model": None,
    },
    "no_provider": {"llm_provider": None},
}
_MODEL_OVERRIDES: dict[str, str | None] = {"none": None, "blank": "  ", "explicit": " override "}


def _dynamic_specs() -> dict[str, SubAgentSpec]:
    return {
        "dynamic/delegate": build_agent_spec(
            _dynamic_config(), display_name="delegate-审稿人", tool_profile="delegate"
        ),
        "dynamic/tmp_writable": SubAgentSpec(
            agent_type=AgentType.VERIFICATION,
            system_prompt="临时验收",
            allowed_tools=("Read", "Bash", "Agent", "Write"),
            disallowed_tools=("Write",),
            model="  ",
            model_scenario="subagent_fast",
            shell_access=ShellAccess.TMP_WRITABLE,
            display_name="wf-验收",
            agent_id=9,
            tool_profile="workflow_node",
        ),
    }


_VERDICT_REPORTS: dict[str, str] = {
    "pass_with_evidence": (
        "**执行的命令:**\n```\nuv run pytest -q\n```\n**观察到的输出:**\n```\n3 passed\n```\n"
        "\nVERDICT: PASS"
    ),
    "pass_without_evidence": "读了代码,逻辑正确。\n\nVERDICT: PASS",
    "fail_bold_period": "执行的命令：\n```\nls\n```\n**VERDICT: FAIL**.",
    "partial_spaced": "执行的命令:\n\n  ```\nmake\n```\n   VERDICT:   PARTIAL   ",
    "inline_mention": "报告提到 VERDICT: PASS 但没有独占一行",
    "last_match_wins": "VERDICT: FAIL\n执行的命令:```\nx\n```\nVERDICT: PASS\n",
    "missing": "没有结论",
    "empty": "",
}

_PLAN_REPORTS: dict[str, str] = {
    "mixed_entries": (
        "方案正文\n\n### 实施关键文件\n"
        "- `session/service.py` — 事务边界\n"
        "* tools/subagents/trigger.py： 阈值判定\n"
        "+ runtime/agent.py， 回合主循环\n"
        "- \n"
        "不是清单的行\n"
        "- docs/plan.md。\n"
        "## 下一节\n"
        "- ignored.py\n"
    ),
    "last_heading_wins": "### 实施关键文件\n- a.py\n\n### 实施关键文件\n- b.py: 说明\n",
    "missing_heading": "### 关键文件\n- a.py\n",
    "empty": "",
}

_TRIGGER_MESSAGES: dict[str, list[dict[str, Any]]] = {
    "three_files": [
        {
            "role": "assistant",
            "content": [
                {"kind": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "a.md"}},
                {"kind": "tool_use", "id": "w2", "name": "Edit", "input": {"path": " b.md "}},
                {"kind": "tool_use", "id": "w3", "name": "NotebookEdit",
                 "input": {"notebook_path": "c.ipynb"}},
                {"kind": "tool_use", "id": "w4", "name": "Write", "input": {"file_path": "a.md"}},
            ],
        }
    ],
    "backend_and_infra": [
        {
            "role": "assistant",
            "content": [
                {"kind": "tool_use", "id": "w1", "name": "Edit",
                 "input": {"file_path": "session/service.py"}},
                {"kind": "tool_use", "id": "w2", "name": "Write",
                 "input": {"file_path": "alembic/versions/x.py"}},
            ],
        }
    ],
    "already_verified": [
        {
            "role": "assistant",
            "content": [
                {"kind": "tool_use", "id": "w1", "name": "Edit",
                 "input": {"file_path": "user/api/routes.py"}},
                {"kind": "tool_use", "id": "a1", "name": "Agent",
                 "input": {"agent_type": "verification", "prompt": "验收"}},
            ],
        }
    ],
    "below_threshold": [
        {"role": "user", "content": "只改文档"},
        {
            "role": "assistant",
            "content": [
                {"kind": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "a.md"}},
                {"kind": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "b.py"}},
                {"kind": "tool_use", "id": "w2", "name": "Write", "input": {"content": "无路径"}},
            ],
        },
    ],
}


# ---------- 替身与记录（不许改） ----------

# 耗时类日志字段每次运行都不同，归一化后再比对。
_TIMING_FIELDS = frozenset({"duration_ms", "ttft_ms"})
# LogRecord 的内置属性，其余属性都来自 extra。
_LOG_BUILTIN_FIELDS = set(logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
}


def _jsonable(value: Any) -> Any:
    """转成 JSON 原生结构：集合按序展开，UUID、Path 与枚举取字符串，dataclass 与命名空间取字段。"""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, SimpleNamespace):
        return _jsonable(vars(value))
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


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
    """一种情况的全部观测：模型请求按发生顺序记录，其余读写记进 journal。"""

    requests: list[dict[str, Any]] = field(default_factory=list)
    journal: list[dict[str, Any]] = field(default_factory=list)

    def record(self, op: str, **fields: Any) -> None:
        self.journal.append({"op": op, **_jsonable(fields)})


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
    """块序列记 text 与 cache_breakpoint；纯文本与 None 原样记录，连同类型一起固化。"""
    if isinstance(instruction, tuple):
        return [
            {"text": block.text, "cache_breakpoint": block.cache_breakpoint}
            for block in instruction
        ]
    return instruction


class _FakeLLM:
    """模型替身：记录每次请求，固定回复一段文本并结束本轮；reply 为 None 时流里没有终局结果。"""

    provider = "anthropic"

    def __init__(self, world: _World, label: str, model: str, reply: str | None) -> None:
        self._world = world
        self._label = label
        self.model = model
        self._reply = reply

    async def stream(self, context: Any, settings: ModelSettings) -> AsyncIterator[ModelStreamEvent]:
        self._world.requests.append(
            {
                "client": self._label,
                "model": settings.model_id,
                "system": _instruction(context.instruction),
                "messages": _message_rows(context.messages),
                "tools": [tool.name for tool in context.tools],
            }
        )
        if self._reply is None:
            return
        yield ModelStreamEvent(ModelEventKind.BLOCK_START, block_ref=0, block_type=BlockType.TEXT)
        yield ModelStreamEvent(ModelEventKind.TEXT_DELTA, block_ref=0, text=self._reply)
        yield ModelStreamEvent(ModelEventKind.BLOCK_STOP, block_ref=0)
        yield ModelStreamEvent(
            ModelEventKind.TURN_RESULT,
            result=ModelTurnResult(text=self._reply, stop_reason="end_turn"),
        )


class _FakeDb:
    """数据库会话替身：只记事务动作，读写都由打桩的仓储与服务完成。"""

    def __init__(self, world: _World) -> None:
        self._world = world

    async def commit(self) -> None:
        self._world.record("db.commit")

    async def rollback(self) -> None:
        self._world.record("db.rollback")

    async def flush(self) -> None:
        self._world.record("db.flush")


class _FakeMemory:
    """记忆 Provider 替身：召回与继承都返回固定结果并记录调用。"""

    def __init__(self, world: _World, recall: MemoryRecall | None) -> None:
        self._world = world
        self._recall = recall

    async def recall(self, turn: Any) -> MemoryRecall | None:
        self._world.record("memory.recall", turn=turn)
        return self._recall

    async def inherited(self, turn: Any) -> MemoryRecall | None:
        self._world.record("memory.inherited", turn=turn)
        return self._recall


def _row(message_id: int, role: str, content: Any, turn_id: UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=message_id, role=role, content=deepcopy(content), turn_id=turn_id)


_STARTED_MESSAGE_ID = 41
_TEAM_TASK = SimpleNamespace(id=21, subject="补充风险", description="写风险一节")


class _Fakes:
    """各域读写的记录替身，挂到类上后忽略 self（都按类替换，与谁来构造无关）。"""

    def __init__(self, world: _World, rows: list[SimpleNamespace], snapshot: Any = None) -> None:
        self.world = world
        self.rows = rows
        self.snapshot = snapshot
        self.skills = ""
        self.plan: str | None = None
        self.member: SimpleNamespace | None = None
        self.inbox: tuple[str, ...] = ()
        self.claimable = False
        self.continuations = 0

    # ---- 会话仓储 ----

    async def latest_snapshot(self, session_id: int) -> Any:
        self.world.record("session.latest_snapshot", session_id=session_id)
        return deepcopy(self.snapshot)

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
        selected = [
            row
            for row in self.rows
            if (after_message_id is None or row.id > after_message_id)
            and (before_message_id is None or row.id < before_message_id)
            and (turn_id is None or row.turn_id == turn_id)
        ]
        return [_row(row.id, row.role, row.content, row.turn_id) for row in selected]

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
        self.world.record(
            "session.add_message",
            role=role,
            content=content,
            token_estimate=token_estimate,
            turn_id=turn_id,
            provider=provider,
            model=model,
        )
        row = _row(max(row.id for row in self.rows) + 1, role, content, turn_id)
        self.rows.append(row)
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
            session_id=session_id,
            messages=messages,
            summary=summary,
            through_message_id=through_message_id,
            snapshot_type=snapshot_type,
        )
        self.snapshot = SimpleNamespace(
            messages=deepcopy(messages), through_message_id=through_message_id
        )
        return SimpleNamespace(id=1)

    # ---- skill 目录与 Plan ----

    async def get_catalog(self, workspace_id: int, skill_ids: list[int] | None = None) -> str:
        self.world.record("skill.get_catalog", workspace_id=workspace_id, skill_ids=skill_ids)
        return self.skills

    async def get_active(self, session_id: int) -> SimpleNamespace | None:
        self.world.record("plan.get_active", session_id=session_id)
        if self.plan is None:
            return None
        return SimpleNamespace(content=self.plan)

    # ---- 团队成员与子代理运行记录 ----

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
        self.world.record("team.list_claimable_tasks")
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

    async def run_create(
        self, session_id: int, member_name: str, prompt: str, *, agent_id: int | None = None
    ) -> SimpleNamespace:
        self.world.record("run.create", member_name=member_name, prompt=prompt, agent_id=agent_id)
        return SimpleNamespace(id=11)

    async def run_succeed(self, run: Any, report: str) -> Any:
        self.world.record("run.succeed", report=report)
        return run

    async def run_fail(self, run: Any, error_message: str) -> Any:
        self.world.record("run.fail", error_message=error_message)
        return run

    # ---- Stop hook、Plan Mode 与工具结果 ----

    async def stop_hook(self, ctx: Any) -> HookOutcome | None:
        """记录 Stop hook 看到的上下文；还有续跑次数时要求续跑一次。"""
        self.world.record("hook.stop", messages=ctx.messages)
        if self.continuations <= 0:
            return None
        self.continuations -= 1
        return HookOutcome(continuation=f"[系统检查] 请继续完善周报（剩余 {self.continuations} 次）")

    async def plan_enter(self, session_id: int, turn_id: UUID) -> None:
        self.world.record("plan.enter", session_id=session_id, turn_id=turn_id)

    async def record_tool_response(
        self,
        session_id: int,
        turn_id: UUID,
        actor: Any,
        target: ToolCall,
        output: str,
        *,
        is_error: bool,
        record: Any,
    ) -> None:
        self.world.record(
            "tool.response", tool_call_id=target.id, output=output, is_error=is_error
        )


class _NullBus:
    """事件总线替身：事件形状由其他测试固化，这里只吞掉。"""

    async def emit(self, event: Any) -> None:
        return None


def _main_rows(case: MainCase) -> tuple[list[SimpleNamespace], Any]:
    """主 Agent 的落库消息与最新快照：历史从 31 号开始，本轮起始用户消息是 41 号。"""
    if case.long_history:
        history = _HISTORY
    else:
        history = [_HISTORY[0], _HISTORY[-1]]
    rows = [_row(31 + index, item["role"], item["content"]) for index, item in enumerate(history)]
    rows.append(_row(_STARTED_MESSAGE_ID, "user", _USER_CONTENT, _TURN_ID))
    snapshot = None
    if case.snapshot:
        snapshot = SimpleNamespace(
            messages=[{"role": "user", "content": "[Conversation compressed.]\n\n早先的摘要"}],
            through_message_id=31,
        )
    return rows, snapshot


def _member(case: SubCase) -> SimpleNamespace:
    history = None
    if case.history is not None:
        history = json.dumps(case.history, ensure_ascii=False)
    return SimpleNamespace(
        name="writer", role=case.role, team_id=None, prompt="整理本周周报要点", history=history
    )


# ---------- 驱动：如何构造运行时、运行器与被测对象，如何把替身接进去（重构时只改这一节） ----------


class _FixedDate(date):
    @classmethod
    def today(cls) -> _FixedDate:
        return cls(_TODAY.year, _TODAY.month, _TODAY.day)


def _install(monkeypatch: pytest.MonkeyPatch, fakes: _Fakes, overrides: dict[str, Any]) -> None:
    """固定配置、环境段与临时目录，把各域的仓储与服务方法换成记录替身。"""
    for key, value in {**_FIXED_SETTINGS, **overrides}.items():
        monkeypatch.setattr(app_settings, key, value)
    monkeypatch.setattr("runtime.lead_loop.date", _FixedDate)
    monkeypatch.setattr("runtime.lead_loop.Path", SimpleNamespace(cwd=lambda: _CWD))
    monkeypatch.setattr(tempfile, "tempdir", _TMP_DIR)
    registry = HookRegistry()
    registry.register(HookEvent.STOP, fakes.stop_hook)
    monkeypatch.setattr(hooks_module, "_registry", registry)
    monkeypatch.setattr("runtime.subagent.new_team_task_instance_id", lambda: "run-golden")
    for name in ("latest_snapshot", "list_messages_range", "add_message", "add_snapshot"):
        monkeypatch.setattr(SessionRepository, name, getattr(fakes, name))
    monkeypatch.setattr(SkillService, "get_catalog", fakes.get_catalog)
    monkeypatch.setattr(PlanService, "get_active", fakes.get_active)
    monkeypatch.setattr(PlanService, "enter", fakes.plan_enter)
    for name in (
        "get_member",
        "read_inbox",
        "peek_inbox",
        "list_claimable_tasks",
        "claim_task",
        "save_member_history",
        "update_member_runtime",
    ):
        monkeypatch.setattr(TeamService, name, getattr(fakes, name))
    monkeypatch.setattr(SubAgentRunRepository, "create", fakes.run_create)
    monkeypatch.setattr(SubAgentRunRepository, "succeed", fakes.run_succeed)
    monkeypatch.setattr(SubAgentRunRepository, "fail", fakes.run_fail)


def _compact_llm(world: _World, summary: str | None) -> _FakeLLM:
    return _FakeLLM(world, "compact", "claude-compact", summary)


async def _drive_main(case: MainCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _World()
    rows, snapshot = _main_rows(case)
    fakes = _Fakes(world, rows, snapshot)
    fakes.skills = case.skills
    fakes.plan = case.plan
    fakes.continuations = case.stop_continuations
    _install(monkeypatch, fakes, {"max_tool_iterations": case.max_tool_iterations})
    runtime = make_agent_runtime(
        _FakeDb(world),
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        llm=_FakeLLM(world, "agent", "claude-golden", "本周周报如下。"),
        memory=_FakeMemory(world, case.memory),
    )
    runtime.bus = _NullBus()  # type: ignore[assignment]
    overrides: dict[str, Any] = {
        "tools": builtin_tool_set().only(*_MAIN_TOOLS),
        "delegate_catalog": case.delegate,
    }
    if case.turbo:
        overrides["mode_slug"] = "turbo"
    if case.agent_prompt is not None:
        overrides.update(agent_id=7, profile="user_agent", agent_prompt=case.agent_prompt)
    runtime.configuration = replace(runtime.configuration, **overrides)
    runtime.compact_service.model = ResolvedModel.from_client(
        _compact_llm(world, "早先对话的摘要。")  # type: ignore[arg-type]
    )
    if case.compaction_threshold is not None:
        runtime.compact_service.threshold = case.compaction_threshold
    session = SimpleNamespace(id=_SESSION_ID, system_prompt=case.session_prompt, model_name=None)
    turn = SimpleNamespace(
        id=_TURN_ID,
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        started_message_id=_STARTED_MESSAGE_ID,
    )
    with _capture_logs() as logs:
        result = await runtime._run_llm_loop(_SESSION_ID, session, turn, case.additional_context)  # type: ignore[arg-type]
    return _main_observation(world, logs, result)


def _builtin_spec(agent_type: AgentType) -> SubAgentSpec:
    """在固定配置下现建内置规格（模块级注册表在导入期按 .env 建好，不随打桩变化）。"""
    return subagent_catalog._build_specs()[agent_type]


def _sub_spec(case: SubCase) -> SubAgentSpec:
    if case.agent_type is None:
        return _dynamic_specs()["dynamic/delegate"]
    return _builtin_spec(case.agent_type)


async def _drive_sub(case: SubCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _World()
    fakes = _Fakes(world, [_row(1, "user", "占位")])
    fakes.member = _member(case)
    fakes.inbox = case.inbox
    fakes.claimable = case.claimable
    _install(monkeypatch, fakes, _SPEC_SETTINGS)
    recall = None
    if case.memory is not None:
        # 父回合的目录不进子代理，这里故意带上，确认只继承正文。
        recall = MemoryRecall(instruction="- [tone](memory:13@1) - 父目录", message_prefix=case.memory)
    runner = make_subagent_runner(
        _FakeDb(world),
        bus=_NullBus(),
        llm=_FakeLLM(world, "subagent", "claude-sub", "子代理报告。"),
        memory=_FakeMemory(world, recall),
    )
    spec = _sub_spec(case)
    scope: dict[str, Any] = {
        "parent_turn_id": _PARENT_TURN_ID,
        "user_id": _USER_ID,
        "workspace_id": _WORKSPACE_ID,
        "parent_allowed_tool_names": frozenset({"Read", "Grep", "Bash", "Write", "TeamList"}),
    }
    with _capture_logs() as logs:
        if case.kind == "run":
            report = await runner.run(_SESSION_ID, _SUB_PROMPT, spec, **scope)
        else:
            report = await runner.run_teammate(_SESSION_ID, "writer", spec, **scope)
    return _sub_observation(world, logs, report)


async def _drive_compact(case: CompactCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _World()
    fakes = _Fakes(world, [_row(1, "user", "占位")])
    _install(monkeypatch, fakes, {})
    service = ContextCompaction(
        SessionService(_FakeDb(world)),  # type: ignore[arg-type]
        llm=_compact_llm(world, case.summary),  # type: ignore[arg-type]
        threshold=case.threshold,
    )
    messages = deepcopy(case.rows)
    with _capture_logs() as logs:
        if case.kind == "l1":
            output: Any = _micro_compact(messages)
        else:
            try:
                output = await service.maybe_compact(
                    _SESSION_ID, messages, through_message_id=case.through_message_id
                )
            except AgentException as exc:
                output = {"error": exc.message}
    return _compact_observation(world, logs, output)


def _specs_under_test() -> dict[str, SubAgentSpec]:
    builtin = {f"builtin/{agent_type.value}": _builtin_spec(agent_type) for agent_type in AgentType}
    return {**builtin, **_dynamic_specs()}


def _micro_compact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """L1 微压缩；旧实现原地改写入参，新实现返回新列表，取返回值观测，两者一致。"""
    return Compactor().micro_compact(messages)


def _resolve_model(spec: SubAgentSpec, override: str | None) -> str | None:
    return resolve_subagent_model(spec, override)


def _shell_tmp_root(spec: SubAgentSpec) -> Path | None:
    return spec.shell_tmp_root(Path(tempfile.gettempdir()))


def _resolved_tool_names(spec: SubAgentSpec) -> list[str]:
    return [tool.name for tool in spec.resolve_tools(builtin_tool_set()).tools]


async def _drive_spec(name: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _World()
    _install(monkeypatch, _Fakes(world, [_row(1, "user", "占位")]), _SPEC_SETTINGS)
    spec = _specs_under_test()[name]
    models: dict[str, dict[str, Any]] = {}
    for world_name, overrides in _MODEL_WORLDS.items():
        for key, value in {**_FIXED_SETTINGS, **overrides}.items():
            monkeypatch.setattr(app_settings, key, value)
        models[world_name] = {}
        for override_name, override in _MODEL_OVERRIDES.items():
            try:
                models[world_name][override_name] = _resolve_model(spec, override)
            except AgentException as exc:
                models[world_name][override_name] = {"error": exc.message}
    return _spec_observation(spec, models)


def _routing_catalog() -> str:
    return subagent_catalog.routing_catalog()


def _global_denied_tools() -> tuple[str, ...]:
    return GLOBAL_SUBAGENT_DENIED_TOOLS


def _report_parsers() -> SimpleNamespace:
    return SimpleNamespace(
        parse_verdict=subagent_reports.parse_verdict,
        count_command_evidence=subagent_reports.count_command_evidence,
        validate_verification_report=subagent_reports.validate_verification_report,
        parse_plan_critical_files=subagent_reports.parse_plan_critical_files,
    )


def _trigger() -> SimpleNamespace:
    return SimpleNamespace(
        assess=verification_trigger.assess,
        compose_hint=verification_trigger.compose_hint,
        verification_already_ran=verification_trigger.verification_already_ran,
    )


async def _drive_enter_plan_mode(
    response: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    world = _World()
    fakes = _Fakes(world, [_row(1, "user", "占位")])
    _install(monkeypatch, fakes, {})
    runtime = make_agent_runtime(
        _FakeDb(world),
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        llm=_FakeLLM(world, "agent", "claude-golden", "好的。"),
    )
    flow = runtime._interactions()
    flow._executor.record_response = fakes.record_tool_response  # type: ignore[method-assign]
    turn = SimpleNamespace(id=_TURN_ID, session_id=_SESSION_ID)
    target = ToolCall(id="p1", tool_name="EnterPlanMode", arguments={})
    await flow._apply_enter_plan_mode(
        turn, ORCHESTRATOR_ACTOR, target, SimpleNamespace(id=5), response  # type: ignore[arg-type]
    )
    return {"journal": world.journal}


# ---------- 断言：观测的组成与比对（不许改） ----------


def _main_observation(world: _World, logs: _LogCollector, result: Any) -> dict[str, Any]:
    return {
        "requests": world.requests,
        "journal": world.journal,
        "result": _jsonable(result),
        "logs": logs.entries,
    }


def _sub_observation(world: _World, logs: _LogCollector, report: str) -> dict[str, Any]:
    return {
        "requests": world.requests,
        "journal": world.journal,
        "report": report,
        "logs": logs.entries,
    }


def _compact_observation(world: _World, logs: _LogCollector, output: Any) -> dict[str, Any]:
    return {
        "requests": world.requests,
        "journal": world.journal,
        "output": _jsonable(output),
        "logs": logs.entries,
    }


def _spec_observation(spec: SubAgentSpec, models: dict[str, Any]) -> dict[str, Any]:
    return {
        "fields": {item.name: _jsonable(getattr(spec, item.name)) for item in fields(spec)},
        "runtime_name": spec.runtime_name,
        "effective_denied_tools": list(spec.effective_denied_tools()),
        "resolve_tools_names": list(spec.resolve_tools_names()),
        "resolved_tools": _resolved_tool_names(spec),
        "shell_tmp_root": _jsonable(_shell_tmp_root(spec)),
        "resolve_model": models,
    }


def _protocol_observation() -> dict[str, Any]:
    parsers = _report_parsers()
    verdicts: dict[str, Any] = {}
    for name, report in _VERDICT_REPORTS.items():
        verdict, note = parsers.validate_verification_report(report)
        verdicts[name] = {
            "parse_verdict": parsers.parse_verdict(report),
            "count_command_evidence": parsers.count_command_evidence(report),
            "validate_verification_report": {"verdict": verdict, "note": note},
        }
    plans = {
        name: parsers.parse_plan_critical_files(report) for name, report in _PLAN_REPORTS.items()
    }
    return {
        "verdicts": verdicts,
        "plan_critical_files": plans,
        "routing_catalog": _routing_catalog(),
        "global_denied_tools": list(_global_denied_tools()),
    }


def _trigger_observation() -> dict[str, Any]:
    trigger = _trigger()
    observed: dict[str, Any] = {}
    for name, messages in _TRIGGER_MESSAGES.items():
        assessment = trigger.assess(deepcopy(messages))
        observed[name] = {
            "assessment": _jsonable(assessment),
            "reason_text": assessment.reason_text,
            "hint": trigger.compose_hint(assessment),
            "already_ran": trigger.verification_already_ran(messages),
        }
    return observed


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


_SPEC_NAMES = (*(f"builtin/{agent_type.value}" for agent_type in AgentType), *_dynamic_specs())
_PLAN_RESPONSES: dict[str, dict[str, Any]] = {
    "approve": {"decision": "approve"},
    "reject": {"decision": "reject", "feedback": "先别规划"},
}


def _case_ids() -> set[str]:
    return (
        {f"main/{name}" for name in MAIN_CASES}
        | {f"subagent/{name}" for name in SUB_CASES}
        | {f"compact/{name}" for name in COMPACT_CASES}
        | {f"spec/{name}" for name in _SPEC_NAMES}
        | {f"enter_plan_mode/{name}" for name in _PLAN_RESPONSES}
        | {"protocol", "trigger"}
    )


def test_golden_literal_matches_its_digest() -> None:
    """字面量自身与摘要一致，防止字面量被悄悄改动。"""
    assert _sha256(EXPECTED) == EXPECTED_SHA256


def test_every_case_has_a_literal() -> None:
    assert set(EXPECTED) == _case_ids()


@pytest.mark.parametrize("name", list(MAIN_CASES))
async def test_main_agent_context_matches_golden(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = await _drive_main(MAIN_CASES[name], monkeypatch)

    _assert_matches(f"main/{name}", actual)


@pytest.mark.parametrize("name", list(SUB_CASES))
async def test_subagent_context_matches_golden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = await _drive_sub(SUB_CASES[name], monkeypatch)

    _assert_matches(f"subagent/{name}", actual)


@pytest.mark.parametrize("name", list(COMPACT_CASES))
async def test_compaction_matches_golden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = await _drive_compact(COMPACT_CASES[name], monkeypatch)

    _assert_matches(f"compact/{name}", actual)


@pytest.mark.parametrize("name", _SPEC_NAMES)
async def test_subagent_spec_matches_golden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = await _drive_spec(name, monkeypatch)

    _assert_matches(f"spec/{name}", actual)


def test_output_protocol_matches_golden() -> None:
    _assert_matches("protocol", _protocol_observation())


def test_verification_trigger_matches_golden() -> None:
    _assert_matches("trigger", _trigger_observation())


@pytest.mark.parametrize("name", list(_PLAN_RESPONSES))
async def test_enter_plan_mode_result_matches_golden(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = await _drive_enter_plan_mode(_PLAN_RESPONSES[name], monkeypatch)

    _assert_matches(f"enter_plan_mode/{name}", actual)
