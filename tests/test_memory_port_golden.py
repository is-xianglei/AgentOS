"""记忆端口黄金测试：固化主 Agent 召回与回合结束写入、子代理继承父回合记忆的可观测行为。

主 Agent 驱动真实的 AgentRuntime._produce 与 _produce_resume（含回合完成和提交后的就地提取），
子代理驱动真实的 SubAgentRunner.run 与 run_teammate。记忆域只替换仓储、模型解析器和任务执行器，
召回渲染、会话预算、入队参数与 SAVEPOINT 用法都走真实代码。逐种情况记录：发给模型的系统指令与消息、
记忆仓储与服务的调用参数（含 turn_memory_contexts 冻结与 memory_jobs 入队）、就地执行的认领范围、
提交 / SAVEPOINT / 回滚与关键读写的先后顺序、会话写入与最终状态、日志（级别、正文与 extra，
不含 logger 名，耗时字段归一化），与 tests/fixtures/memory_port_golden.py 的字面量逐项比对（含字典键序），
并用 SHA-256 再校验一遍字面量本身。
字面量由阶段 4 重构前的代码实际产出生成；此后只允许改动本文件「驱动」一节
（如何构造运行时与运行器、如何把替身接进去），字面量、情况、替身、观测与断言一个都不许改。
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
from agent.service import AgentDefinitionService
from core.config import settings as app_settings
from core.events import BlockType
from harness.adapters.models.resolver import DatabaseModelResolver
from harness.app.ports import (
    ModelEventKind,
    ModelSettings,
    ModelStreamEvent,
    ModelTurnResult,
    ResolvedModel,
)
from harness.app.subagent_catalog import get_subagent_spec
from harness.domain.roles.subagent import AgentType
from hooks import HookRegistry
from interaction.service import InteractionService
from memory.jobs.repository import MemoryJobRepository
from memory.jobs.service import MemoryJobRunner
from memory.recall.repository import MemoryRecallRepository
from memory.repository import MemoryCatalogEntry, MemoryCatalogSnapshot, MemoryRepository
from model_service.service import ModelService
from plan.service import PlanService
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner
from session.repository import SessionRepository
from session.service import SessionService
from skill.service import SkillService
from task.service import TaskService
from team.service import TeamService
from team.subagent_run_repository import SubAgentRunRepository
from tests.fixtures.memory_port_golden import EXPECTED, EXPECTED_SHA256

pytestmark = pytest.mark.anyio

_SESSION_ID = 1
_USER_ID = 3
_WORKSPACE_ID = 2
_TURN_ID = UUID("00000000-0000-4000-8000-00000000e001")
_JOB_ID = UUID("00000000-0000-4000-8000-00000000e002")
_STARTED_MESSAGE_ID = 41
_SPACE_ID = 5
_CATALOG_VERSION = 7
_OWNER = "owner-golden"
_USER_CONTENT = "帮我写本周周报"
_SUB_PROMPT = "整理本周周报要点"

# 固定与 .env 无关的配置；召回灰度与提取开关按情况另行设置。
_FIXED_SETTINGS: dict[str, Any] = {
    "llm_provider": "anthropic",
    "anthropic_model": "claude-env",
    "anthropic_memory_extractor_model": None,
    "anthropic_subagent_fast_model": "claude-fast",
    "max_tool_iterations": 3,
    "memory_rollout_salt": "golden-salt",
    "memory_selector_timeout_seconds": 5.0,
    "memory_selector_query_max_chars": 12_000,
    "memory_recall_max_items": 5,
    "memory_recall_item_max_bytes": 4096,
    "memory_recall_item_max_lines": 200,
    "memory_session_max_bytes": 60 * 1024,
    "llm_memory_selector_max_tokens": 256,
    "memory_extractor_prompt_version": "v1",
    "memory_job_max_attempts": 5,
}
_REQUEST_ID = UUID("00000000-0000-4000-8000-00000000e003")
_SUSPENSION_ID = UUID("00000000-0000-4000-8000-00000000e004")
_ADDITIONAL_CONTEXT = "审批已通过，可以继续写周报。"

# ---------- 记忆数据（字面量的输入，不许改） ----------

_CATALOG: tuple[MemoryCatalogEntry, ...] = (
    MemoryCatalogEntry(
        id=11,
        version=2,
        memory_key="report-format",
        memory_type="feedback",
        name="周报格式",
        description="周报按进展、风险、计划三段写",
    ),
    MemoryCatalogEntry(
        id=12,
        version=3,
        memory_key="team-members",
        memory_type="project",
        name="团队成员",
        description="团队五人，组长是王敏",
    ),
    MemoryCatalogEntry(
        id=13,
        version=1,
        memory_key="tone",
        memory_type="user",
        name="语气偏好",
        description="喜欢简洁的语气\n不要客套",
    ),
)
# 按 (memory_id, revision) 取的不可变正文；1102 在本会话之前的回合呈现过。
_REVISIONS: dict[tuple[int, int], dict[str, Any]] = {
    (11, 2): {
        "id": 1102,
        "memory_id": 11,
        "memory_type": "feedback",
        "revision": 2,
        "body": "周报分三段：\n1. 进展\n2. 风险\n3. 计划",
    },
    (12, 3): {
        "id": 1203,
        "memory_id": 12,
        "memory_type": "project",
        "revision": 3,
        "body": "团队五人，组长王敏 <负责评审> & `发布`",
    },
    (13, 1): {
        "id": 1301,
        "memory_id": 13,
        "memory_type": "user",
        "revision": 1,
        "body": "语气简洁\x07，不要客套",
    },
}
_SURFACED_REVISION_IDS = [1102]
_RECENT_USER_TEXTS = ["上周的周报写了什么", _USER_CONTENT]
# 本回合（或父回合）先前已冻结的召回结果，重复召回与子代理继承都读它。
_FROZEN_CATALOG = "- [report-format](memory:11@2) - 周报按进展、风险、计划三段写"
_FROZEN_MEMORIES = (
    '<relevant_memories data-trust="historical-reference">\n'
    '  <memory id="11" type="feedback" revision="2">\n'
    "周报分三段：进展、风险、计划\n"
    "  </memory>\n"
    "</relevant_memories>"
)


# ---------- 情况：主 Agent（字面量的输入，不许改） ----------


@dataclass(frozen=True)
class MainCase:
    """主 Agent 的一种情况。

    frozen_before 表示本回合已冻结过召回结果（同一回合重复召回）；selection 是选择器返回的
    Memory ID，None 表示选择器抛异常；lexical 是词法降级查到的候选 ID。session_user_id 与轮次
    不一致时入队校验失败。resume 为 True 时走人工交互恢复路径。
    """

    mode_slug: str = "standard"
    recall_percent: int = 0
    frozen_before: bool = False
    selection: tuple[int, ...] | None = (12, 11)
    lexical: tuple[int, ...] = ()
    extraction: bool = False
    shadow: bool = False
    inline: bool = False
    enqueue_error: bool = False
    session_user_id: int = _USER_ID
    extractor_provider: bool = False
    inline_error: bool = False
    resume: bool = False


MAIN_CASES: dict[str, MainCase] = {
    # 召回：选中正文、命中已冻结结果、灰度未命中、选中为空、选择器越界后词法降级。
    "recall_selected": MainCase(recall_percent=100),
    "recall_frozen_before": MainCase(recall_percent=100, frozen_before=True),
    "recall_rollout_miss": MainCase(recall_percent=0),
    "recall_empty_selection": MainCase(recall_percent=100, selection=()),
    "recall_degraded_lexical": MainCase(recall_percent=100, selection=(99,), lexical=(13, 12)),
    "recall_selector_error": MainCase(recall_percent=100, selection=None),
    # 极速模式：不召回、不入队、不就地执行。
    "turbo_skips_memory": MainCase(
        mode_slug="turbo", recall_percent=100, extraction=True, inline=True
    ),
    # 回合结束写入。
    "extraction_disabled": MainCase(extraction=False, inline=True),
    "extraction_enqueued": MainCase(extraction=True),
    "extraction_enqueued_shadow": MainCase(extraction=True, shadow=True),
    "extraction_enqueued_workspace_model": MainCase(extraction=True, extractor_provider=True),
    "extraction_enqueue_failed": MainCase(extraction=True, inline=True, enqueue_error=True),
    "extraction_validation_rejected": MainCase(
        extraction=True, inline=True, session_user_id=99
    ),
    "inline_succeeded": MainCase(recall_percent=100, extraction=True, inline=True),
    "inline_failed": MainCase(extraction=True, inline=True, inline_error=True),
    # 人工交互恢复：同一回合再次召回拿到冻结结果，完成后照常入队并就地执行。
    "resume_reuses_frozen_recall": MainCase(
        recall_percent=100, frozen_before=True, extraction=True, inline=True, resume=True
    ),
}


# ---------- 情况：子代理与队友（字面量的输入，不许改） ----------


@dataclass(frozen=True)
class SubCase:
    """子代理的一种情况。

    kind 为 run 时驱动一次性子代理，为 teammate 时驱动队友单次推进。frozen 是父回合冻结的结果：
    memories 有正文、empty 正文为空串、none 没有冻结记录。session_id 是子代理所在会话，
    与父回合所属会话不一致时按租户边界查不到。
    """

    kind: str = "run"
    agent_type: AgentType = AgentType.GENERAL_PURPOSE
    parent_turn_id: UUID | None = _TURN_ID
    user_id: int | None = _USER_ID
    workspace_id: int | None = _WORKSPACE_ID
    session_id: int = _SESSION_ID
    frozen: str = "memories"


SUB_CASES: dict[str, SubCase] = {
    "run_inherits_parent_memory": SubCase(),
    "run_omit_inherited_memory": SubCase(agent_type=AgentType.EXPLORE),
    "run_missing_parent_turn": SubCase(parent_turn_id=None),
    "run_missing_user": SubCase(user_id=None),
    "run_missing_workspace": SubCase(workspace_id=None),
    "run_parent_not_frozen": SubCase(frozen="none"),
    "run_parent_frozen_empty": SubCase(frozen="empty"),
    "run_other_session": SubCase(session_id=99),
    "teammate_inherits_skipping_identity": SubCase(kind="teammate"),
    # 队友不看 omit_inherited_memory，Explore 规格也照样继承（现状）。
    "teammate_explore_spec_still_inherits": SubCase(kind="teammate", agent_type=AgentType.EXPLORE),
    "teammate_missing_scope": SubCase(kind="teammate", user_id=None),
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


@dataclass(frozen=True)
class _MemoryScript:
    """记忆域替身的脚本：选择器返回（None 表示抛异常）、词法候选、入队失败、工作区提取模型、就地执行失败。"""

    selection: tuple[int, ...] | None = (12, 11)
    lexical: tuple[int, ...] = ()
    enqueue_error: bool = False
    extractor_provider: bool = False
    inline_error: bool = False


@dataclass
class _World:
    """一种情况的全部替身状态：会话与轮次、已冻结的召回结果，以及按发生顺序记下的副作用。"""

    session: SimpleNamespace
    turn: SimpleNamespace
    contexts: dict[UUID, SimpleNamespace] = field(default_factory=dict)
    journal: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    next_message_id: int = 100

    def record(self, op: str, **fields: Any) -> None:
        self.journal.append({"op": op, **_jsonable(fields)})


def _frozen_record(rendered_memories: str) -> SimpleNamespace:
    """父回合（或本回合）先前冻结的 turn_memory_contexts 记录。"""
    return SimpleNamespace(
        workspace_id=_WORKSPACE_ID,
        user_id=_USER_ID,
        session_id=_SESSION_ID,
        turn_id=_TURN_ID,
        rendered_catalog=_FROZEN_CATALOG,
        rendered_memories=rendered_memories,
    )


def _main_world(case: MainCase) -> _World:
    status = "awaiting_interaction" if case.resume else "running"
    session = SimpleNamespace(
        id=_SESSION_ID,
        title="周报",
        status=status,
        user_id=case.session_user_id,
        workspace_id=_WORKSPACE_ID,
        agent_id=None,
        mode_slug=case.mode_slug,
        system_prompt="你是团队的周报助手。",
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
    if case.frozen_before:
        world.contexts[_TURN_ID] = _frozen_record(_FROZEN_MEMORIES)
    return world


def _sub_world(case: SubCase) -> _World:
    world = _main_world(MainCase())
    if case.frozen == "memories":
        world.contexts[_TURN_ID] = _frozen_record(_FROZEN_MEMORIES)
    elif case.frozen == "empty":
        world.contexts[_TURN_ID] = _frozen_record("")
    return world


class _FakeNested:
    def __init__(self, world: _World) -> None:
        self._world = world

    async def commit(self) -> None:
        self._world.record("savepoint.commit")

    async def rollback(self) -> None:
        self._world.record("savepoint.rollback")


class _FakeDb:
    """数据库会话替身：只记事务动作，读写都由打桩的仓储完成。"""

    def __init__(self, world: _World) -> None:
        self._world = world

    async def commit(self) -> None:
        self._world.record("db.commit")

    async def rollback(self) -> None:
        self._world.record("db.rollback")

    async def flush(self) -> None:
        self._world.record("db.flush")

    async def begin_nested(self) -> _FakeNested:
        self._world.record("db.begin_nested")
        return _FakeNested(self._world)


class _FakeBus:
    def __init__(self, world: _World) -> None:
        self._world = world

    async def emit(self, event: Any) -> None:
        self._world.events.append(_jsonable(event.to_dict()))


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
    if isinstance(instruction, tuple):
        return [
            {"text": block.text, "cache_breakpoint": block.cache_breakpoint}
            for block in instruction
        ]
    return instruction


class _FakeLLM:
    """主模型替身：记录每次请求，固定回复一段文本并结束本轮。"""

    provider = "anthropic"
    model = "claude-golden"

    def __init__(self, world: _World, reply: str) -> None:
        self._world = world
        self._reply = reply

    async def stream(self, context: Any, settings: ModelSettings) -> AsyncIterator[ModelStreamEvent]:
        self._world.requests.append(
            {
                "model": settings.model_id,
                "system": _instruction(context.instruction),
                "messages": _message_rows(context.messages),
                "tools": [tool.name for tool in context.tools],
            }
        )
        yield ModelStreamEvent(ModelEventKind.BLOCK_START, block_ref=0, block_type=BlockType.TEXT)
        yield ModelStreamEvent(ModelEventKind.TEXT_DELTA, block_ref=0, text=self._reply)
        yield ModelStreamEvent(ModelEventKind.BLOCK_STOP, block_ref=0)
        yield ModelStreamEvent(
            ModelEventKind.TURN_RESULT,
            result=ModelTurnResult(text=self._reply, stop_reason="end_turn"),
        )


class _SelectorClient:
    """记忆选择器替身：按脚本返回 Memory ID，脚本为 None 时抛出上游错误。"""

    provider = "anthropic"
    model = "claude-selector"

    def __init__(self, world: _World, script: _MemoryScript) -> None:
        self._world = world
        self._script = script

    async def complete_structured(self, context: Any, settings: ModelSettings) -> Any:
        output_type = context.require_output_type()
        self._world.record(
            "selector.complete_structured",
            instruction=context.instruction,
            messages=_message_rows(context.messages),
            output_type=output_type.__name__,
            settings=settings,
        )
        if self._script.selection is None:
            raise RuntimeError("选择器上游返回 503")
        return output_type.model_validate({"selected_memory_ids": list(self._script.selection)})


# 本会话本回合之前的历史消息：一问一答。
_HISTORY_ROWS: tuple[tuple[int, str, Any], ...] = (
    (30, "user", "上周的周报写了什么"),
    (31, "assistant", [{"kind": "text", "text": "上周写了进展和计划两段。"}]),
)
# 词法降级候选的 trigram 相似度。
_TRIGRAM: dict[int, float] = {12: 0.2, 13: 0.1}


class _Fakes:
    """记忆、会话、团队、交互等域的仓储与服务替身；读操作直接返回，写操作与事务相关调用记入 journal。"""

    def __init__(self, world: _World, script: _MemoryScript) -> None:
        self.world = world
        self.script = script

    # ---- 记忆召回仓储 ----

    async def get_context(
        self,
        workspace_id: int,
        user_id: int,
        turn_id: UUID,
        *,
        session_id: int | None = None,
    ) -> Any:
        self.world.record(
            "recall.get_context",
            workspace_id=workspace_id,
            user_id=user_id,
            turn_id=turn_id,
            session_id=session_id,
        )
        frozen = self.world.contexts.get(turn_id)
        if frozen is None:
            return None
        if frozen.workspace_id != workspace_id or frozen.user_id != user_id:
            return None
        if session_id is not None and frozen.session_id != session_id:
            return None
        return frozen

    async def list_recent_user_texts(
        self,
        workspace_id: int,
        user_id: int,
        session_id: int,
        through_message_id: int,
        *,
        limit: int,
    ) -> list[str]:
        self.world.record(
            "recall.list_recent_user_texts",
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
            through_message_id=through_message_id,
            limit=limit,
        )
        return list(_RECENT_USER_TEXTS)

    async def get_catalog_snapshot(
        self, workspace_id: int, user_id: int, *, limit: int
    ) -> MemoryCatalogSnapshot:
        self.world.record(
            "memory.get_catalog_snapshot", workspace_id=workspace_id, user_id=user_id, limit=limit
        )
        return MemoryCatalogSnapshot(
            space_id=_SPACE_ID, catalog_version=_CATALOG_VERSION, entries=_CATALOG
        )

    async def get_catalog_revisions(
        self, workspace_id: int, user_id: int, entries: list[MemoryCatalogEntry]
    ) -> list[SimpleNamespace]:
        self.world.record(
            "recall.get_catalog_revisions",
            workspace_id=workspace_id,
            user_id=user_id,
            entries=[(entry.id, entry.version) for entry in entries],
        )
        revisions: list[SimpleNamespace] = []
        for entry in entries:
            data = _REVISIONS.get((entry.id, entry.version))
            if data is not None:
                revisions.append(SimpleNamespace(**data))
        return revisions

    async def list_surfaced_revision_ids(
        self, workspace_id: int, user_id: int, session_id: int, *, exclude_turn_id: UUID
    ) -> list[int]:
        self.world.record(
            "recall.list_surfaced_revision_ids",
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
            exclude_turn_id=exclude_turn_id,
        )
        return list(_SURFACED_REVISION_IDS)

    async def get_revisions_by_ids(
        self, workspace_id: int, user_id: int, revision_ids: list[int]
    ) -> list[SimpleNamespace]:
        self.world.record(
            "recall.get_revisions_by_ids",
            workspace_id=workspace_id,
            user_id=user_id,
            revision_ids=revision_ids,
        )
        by_id = {data["id"]: data for data in _REVISIONS.values()}
        return [SimpleNamespace(**by_id[i]) for i in revision_ids if i in by_id]

    async def search_lexical_candidates(
        self,
        workspace_id: int,
        user_id: int,
        query: str,
        catalog_entries: tuple[MemoryCatalogEntry, ...],
        *,
        limit: int,
    ) -> list[Any]:
        from memory.recall.repository import MemoryLexicalCandidate

        self.world.record(
            "recall.search_lexical_candidates",
            workspace_id=workspace_id,
            user_id=user_id,
            query=query,
            entries=[entry.id for entry in catalog_entries],
            limit=limit,
        )
        by_id = {entry.id: entry for entry in catalog_entries}
        return [
            MemoryLexicalCandidate(entry=by_id[i], trigram_similarity=_TRIGRAM.get(i, 0.0))
            for i in self.script.lexical
            if i in by_id
        ]

    async def freeze_context(
        self, workspace_id: int, user_id: int, turn_id: UUID, **values: Any
    ) -> SimpleNamespace:
        self.world.record(
            "recall.freeze_context",
            workspace_id=workspace_id,
            user_id=user_id,
            turn_id=turn_id,
            **values,
        )
        record = SimpleNamespace(
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=_SESSION_ID,
            turn_id=turn_id,
            rendered_catalog=values["rendered_catalog"],
            rendered_memories=values["rendered_memories"],
        )
        self.world.contexts[turn_id] = record
        return record

    async def touch_used_items(self, workspace_id: int, user_id: int, item_ids: list[int]) -> None:
        self.world.record(
            "memory.touch_used_items",
            workspace_id=workspace_id,
            user_id=user_id,
            item_ids=sorted(item_ids),
        )

    async def resolve_selector(
        self, workspace_id: int | None, user_id: int | None, scenario: str, **kwargs: Any
    ) -> ResolvedModel:
        self.world.record(
            "resolver.resolve",
            workspace_id=workspace_id,
            user_id=user_id,
            scenario=scenario,
            kwargs=kwargs,
        )
        client = _SelectorClient(self.world, self.script)
        return ResolvedModel(client=client, settings=ModelSettings(model_id="claude-selector"))

    # ---- 记忆任务 ----

    async def get_or_create_space_for_update(
        self, workspace_id: int, user_id: int
    ) -> SimpleNamespace:
        self.world.record(
            "memory.get_or_create_space_for_update", workspace_id=workspace_id, user_id=user_id
        )
        if self.script.enqueue_error:
            raise RuntimeError("memory_spaces 加锁失败")
        return SimpleNamespace(id=_SPACE_ID, catalog_version=_CATALOG_VERSION)

    async def select_provider(
        self,
        workspace_id: int,
        user_id: int | None,
        provider_id: str | None = None,
        scenario: str = "agent",
    ) -> Any:
        self.world.record(
            "model_service.select_provider",
            workspace_id=workspace_id,
            user_id=user_id,
            provider_id=provider_id,
            scenario=scenario,
        )
        if self.script.extractor_provider:
            return SimpleNamespace(provider_id="provider-extract", model_name="db-extractor")
        return None

    async def enqueue_job(self, **kwargs: Any) -> SimpleNamespace:
        self.world.record("jobs.enqueue_extract", **kwargs)
        return SimpleNamespace(id=_JOB_ID, payload=dict(kwargs["payload"]))

    async def run_job(self, runner: Any, *, job_id: UUID | None = None) -> int:
        self.world.record("jobs.run_once", job_id=job_id, claim_scope=runner.claim_scope)
        if self.script.inline_error:
            raise RuntimeError("提取任务认领失败")
        return 1

    # ---- 会话 ----

    async def get_required(self, session_id: int) -> SimpleNamespace:
        return self.world.session

    async def lock_required(self, session_id: int) -> SimpleNamespace:
        self.world.record("session.lock_required", session_id=session_id)
        return self.world.session

    async def get_turn_required(self, turn_id: UUID) -> SimpleNamespace:
        return self.world.turn

    async def lock_owned_turn(
        self, session_id: int, turn_id: UUID, owner: str
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        self.world.record("session.lock_owned_turn", turn_id=turn_id, owner=owner)
        return self.world.session, self.world.turn

    async def mark_turn_status(
        self, turn: SimpleNamespace, status: str, completed_message_id: int | None = None
    ) -> SimpleNamespace:
        self.world.record(
            "session.mark_turn_status", status=status, completed_message_id=completed_message_id
        )
        turn.status = status
        if status == "completed":
            turn.completed_message_id = completed_message_id
        return turn

    async def update_session_status(self, session: SimpleNamespace, status: str) -> SimpleNamespace:
        self.world.record("session.update_status", status=status)
        session.status = status
        return session

    async def load_context_for_turn(
        self,
        turn: SimpleNamespace,
        additional_context: str | None = None,
        rendered_memories: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
        self.world.record(
            "session.load_context_for_turn",
            additional_context=additional_context,
            rendered_memories=rendered_memories,
        )
        history: list[dict[str, Any]] = [
            {"role": role, "content": deepcopy(content)} for _, role, content in _HISTORY_ROWS
        ]
        content = _USER_CONTENT
        if rendered_memories:
            content = f"{rendered_memories}\n\n{content}"
        if additional_context:
            content = f"{content}\n\n{additional_context}"
        return history, [{"role": "user", "content": content}], _HISTORY_ROWS[-1][0]

    async def add_message(
        self,
        session_id: int,
        role: str,
        content: Any,
        turn_id: UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> SimpleNamespace:
        self.world.next_message_id += 1
        self.world.record(
            "session.add_message",
            id=self.world.next_message_id,
            role=role,
            content=content,
            turn_id=turn_id,
            provider=provider,
            model=model,
        )
        return SimpleNamespace(id=self.world.next_message_id)

    async def runtime_model_options(self, session_id: int) -> SimpleNamespace:
        return self.world.session

    # ---- 人工交互（恢复路径） ----

    async def get_request_required(
        self, request_id: UUID, *, session_id: int | None = None
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=_REQUEST_ID,
            suspension_id=_SUSPENSION_ID,
            kind="tool_approval",
            response_payload={"decision": "approve"},
        )

    async def lock_suspension_required(
        self, suspension_id: UUID, *, session_id: int | None = None
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=_SUSPENSION_ID,
            status="resuming",
            turn_id=_TURN_ID,
            continuation_payload={
                "tool_budget": {"total_limit": 12, "per_tool_limit": 3},
                "additional_context": _ADDITIONAL_CONTEXT,
            },
        )

    async def mark_suspension_resolved(self, suspension_id: UUID) -> None:
        self.world.record("interaction.mark_suspension_resolved", suspension_id=suspension_id)

    async def apply_interaction(self, *args: Any, **kwargs: Any) -> None:
        self.world.record("runtime.apply_interaction_and_continue")

    # ---- 团队与子代理运行记录 ----

    async def get_member(self, session_id: int, name: str) -> SimpleNamespace:
        history = json.dumps(
            [
                {
                    "role": "user",
                    "content": (
                        f"<identity>You are '{name}', role: 写手, "
                        f"team session {session_id}. Continue your work.</identity>"
                    ),
                },
                {"role": "user", "content": "起草周报的风险一节"},
                {"role": "assistant", "content": [{"kind": "text", "text": "好的，先列风险。"}]},
            ],
            ensure_ascii=False,
        )
        return SimpleNamespace(
            id=8, name=name, role="写手", team_id=None, prompt="起草周报", history=history
        )

    async def read_inbox(self, session_id: int, recipient: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(sender="lead", content="风险一节写两条即可")]

    async def peek_inbox(self, session_id: int, recipient: str) -> list[Any]:
        return []

    async def list_claimable_tasks(self, session_id: int) -> list[Any]:
        return []

    async def save_member_history(self, session_id: int, name: str, history: str | None) -> None:
        self.world.record("team.save_member_history", name=name, history=json.loads(history or "[]"))

    async def update_member_runtime(self, session_id: int, name: str, **kwargs: Any) -> None:
        self.world.record("team.update_member_runtime", name=name, **kwargs)

    async def run_create(
        self, session_id: int, member_name: str, prompt: str, *, agent_id: int | None = None
    ) -> SimpleNamespace:
        self.world.record("run.create", member_name=member_name, prompt=prompt, agent_id=agent_id)
        return SimpleNamespace(id=21)

    async def run_succeed(self, run: SimpleNamespace, report: str) -> SimpleNamespace:
        self.world.record("run.succeed", report=report)
        return run

    async def run_fail(self, run: SimpleNamespace, error_message: str) -> SimpleNamespace:
        self.world.record("run.fail", error_message=error_message)
        return run


def _returning(value: Any) -> Any:
    """返回固定值的异步替身，挂到类上后忽略 self 与全部入参。"""

    async def _call(*args: Any, **kwargs: Any) -> Any:
        return value

    return _call


class _FixedDate(date):
    @classmethod
    def today(cls) -> _FixedDate:
        return cls(2026, 1, 1)


def _main_settings(case: MainCase) -> dict[str, Any]:
    return {
        "memory_recall_rollout_percent": case.recall_percent,
        "memory_extraction_enabled": case.extraction,
        "memory_extraction_inline": case.inline,
        "memory_extraction_shadow_enabled": case.shadow,
    }


def _main_script(case: MainCase) -> _MemoryScript:
    return _MemoryScript(
        selection=case.selection,
        lexical=case.lexical,
        enqueue_error=case.enqueue_error,
        extractor_provider=case.extractor_provider,
        inline_error=case.inline_error,
    )


# ---------- 驱动：如何构造运行时与运行器、如何把替身接进去（重构时只改这一节） ----------


def _install(
    monkeypatch: pytest.MonkeyPatch, fakes: _Fakes, overrides: dict[str, Any]
) -> None:
    """固定配置与环境段，把各域的仓储与服务方法换成记录替身（都按类替换，与谁来构造无关）。"""
    for key, value in {**_FIXED_SETTINGS, **overrides}.items():
        monkeypatch.setattr(app_settings, key, value)
    monkeypatch.setattr("runtime.agent.date", _FixedDate)
    monkeypatch.setattr("runtime.agent.Path", SimpleNamespace(cwd=lambda: "/srv"))
    monkeypatch.setattr(hooks_module, "_registry", HookRegistry())
    monkeypatch.setattr("runtime.subagent.new_team_task_instance_id", lambda: "run-golden")

    for name in (
        "get_context",
        "list_recent_user_texts",
        "get_catalog_revisions",
        "list_surfaced_revision_ids",
        "get_revisions_by_ids",
        "search_lexical_candidates",
        "freeze_context",
    ):
        monkeypatch.setattr(MemoryRecallRepository, name, getattr(fakes, name))
    for name in ("get_catalog_snapshot", "touch_used_items", "get_or_create_space_for_update"):
        monkeypatch.setattr(MemoryRepository, name, getattr(fakes, name))
    monkeypatch.setattr(DatabaseModelResolver, "resolve", fakes.resolve_selector)
    monkeypatch.setattr(ModelService, "select_provider", fakes.select_provider)
    monkeypatch.setattr(MemoryJobRepository, "enqueue_extract", fakes.enqueue_job)

    async def _run_once(runner: MemoryJobRunner, *, job_id: UUID | None = None) -> int:
        return await fakes.run_job(runner, job_id=job_id)

    monkeypatch.setattr(MemoryJobRunner, "run_once", _run_once)

    for name in (
        "get_required",
        "lock_required",
        "get_turn_required",
        "lock_owned_turn",
        "mark_turn_status",
        "load_context_for_turn",
        "add_message",
        "runtime_model_options",
    ):
        monkeypatch.setattr(SessionService, name, getattr(fakes, name))
    monkeypatch.setattr(SessionRepository, "update_status", fakes.update_session_status)

    monkeypatch.setattr(SkillService, "get_catalog", _returning("- weekly-report: 周报写作指引"))
    monkeypatch.setattr(AgentDefinitionService, "list_delegate_catalog", _returning(()))
    monkeypatch.setattr(PlanService, "get_active", _returning(None))
    monkeypatch.setattr(TaskService, "emit_snapshot", _returning(None))
    monkeypatch.setattr(TeamService, "list_members", _returning([]))
    for name in (
        "get_member",
        "read_inbox",
        "peek_inbox",
        "list_claimable_tasks",
        "save_member_history",
        "update_member_runtime",
    ):
        monkeypatch.setattr(TeamService, name, getattr(fakes, name))
    for name in ("get_request_required", "lock_suspension_required", "mark_suspension_resolved"):
        monkeypatch.setattr(InteractionService, name, getattr(fakes, name))
    monkeypatch.setattr(AgentRuntime, "_apply_interaction_and_continue", fakes.apply_interaction)
    monkeypatch.setattr(SubAgentRunRepository, "create", fakes.run_create)
    monkeypatch.setattr(SubAgentRunRepository, "succeed", fakes.run_succeed)
    monkeypatch.setattr(SubAgentRunRepository, "fail", fakes.run_fail)


async def _drive_main(case: MainCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _main_world(case)
    _install(monkeypatch, _Fakes(world, _main_script(case)), _main_settings(case))
    runtime = AgentRuntime(
        _FakeDb(world),  # type: ignore[arg-type]
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        llm=_FakeLLM(world, "本周周报如下。"),  # type: ignore[arg-type]
    )
    runtime.bind_execution(_FakeBus(world), _OWNER)  # type: ignore[arg-type]
    with _capture_logs() as logs:
        if case.resume:
            await runtime._produce_resume(_SESSION_ID, _REQUEST_ID)
        else:
            await runtime._produce(_SESSION_ID, _TURN_ID, _USER_CONTENT)
    return _main_observation(world, logs)


async def _drive_sub(case: SubCase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _sub_world(case)
    _install(monkeypatch, _Fakes(world, _MemoryScript()), {})
    runner = SubAgentRunner(
        _FakeDb(world),  # type: ignore[arg-type]
        bus=_FakeBus(world),  # type: ignore[arg-type]
        llm=_FakeLLM(world, "子代理报告。"),  # type: ignore[arg-type]
    )
    spec = get_subagent_spec(case.agent_type)
    scope: dict[str, Any] = {
        "parent_turn_id": case.parent_turn_id,
        "user_id": case.user_id,
        "workspace_id": case.workspace_id,
    }
    with _capture_logs() as logs:
        if case.kind == "run":
            report = await runner.run(case.session_id, _SUB_PROMPT, spec, **scope)
        else:
            report = await runner.run_teammate(case.session_id, "writer", spec, **scope)
    return _sub_observation(world, logs, report)


# ---------- 断言：观测的组成与比对（不许改） ----------


def _main_observation(world: _World, logs: _LogCollector) -> dict[str, Any]:
    return {
        "requests": world.requests,
        "journal": world.journal,
        "events": world.events,
        "final_status": {"session": world.session.status, "turn": world.turn.status},
        "logs": logs.entries,
    }


def _sub_observation(world: _World, logs: _LogCollector, report: str) -> dict[str, Any]:
    return {
        "report": report,
        "requests": world.requests,
        "journal": world.journal,
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
    expected_ids = {f"main/{name}" for name in MAIN_CASES} | {f"subagent/{name}" for name in SUB_CASES}
    assert set(EXPECTED) == expected_ids


@pytest.mark.parametrize("name", list(MAIN_CASES))
async def test_main_agent_memory_matches_golden(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = await _drive_main(MAIN_CASES[name], monkeypatch)

    _assert_matches(f"main/{name}", actual)


@pytest.mark.parametrize("name", list(SUB_CASES))
async def test_subagent_memory_matches_golden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = await _drive_sub(SUB_CASES[name], monkeypatch)

    _assert_matches(f"subagent/{name}", actual)
