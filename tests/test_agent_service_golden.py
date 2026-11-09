"""HTTP 入口黄金测试：固化发消息、取消轮次与人工交互接口到运行时边界的行为。

经真实 FastAPI 应用（httpx ASGITransport）发请求；鉴权依赖、请求作用域数据库会话、会话与 Workflow
服务、运行时、轮次取消与 SSE 跟随换成按发生顺序记账的替身。逐种情况记录：替身收到的调用（运行时被
构造的参数，start / start_resume / cancel_interaction / 轮次取消的参数，鉴权与前置校验的先后）、
请求作用域数据库会话的提交与回滚、日志（logger 名、级别、正文与 extra，request_id 与耗时归一化），
以及 HTTP 状态码、关键响应头与响应体，与 tests/fixtures/agent_service_golden.py 的字面量逐项比对
（含字典键序），并用 SHA-256 再校验字面量本身。
字面量由阶段 6b 重构前的代码实际产出生成；此后只允许改动 import 与本文件「驱动」一节，
字面量、情况、替身、观测与断言一个都不许改。
"""

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from api import deps as api_deps
from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from interaction import api as interaction_api
from main import app
from session import api as session_api
from session.service import SessionService
from tests.fixtures.agent_service_golden import EXPECTED, EXPECTED_SHA256
from workflow.service import WorkflowService

pytestmark = pytest.mark.anyio

_USER_ID = 3
_WORKSPACE_ID = 2
_SESSION_ID = 10
# 新会话由运行时准入时建出，执行句柄上带的就是这个 ID。
_NEW_SESSION_ID = 77
_TURN_ID = UUID("00000000-0000-4000-8000-00000000b001")
_REQUEST_ID = UUID("00000000-0000-4000-8000-00000000b002")
_SUSPENSION_ID = UUID("00000000-0000-4000-8000-00000000b003")
_STAMP = datetime(2026, 11, 8, 9, 30, tzinfo=UTC)

_SEND = "/api/sessions/messages"
_CANCEL_TURN = f"/api/sessions/{_SESSION_ID}/turns/{_TURN_ID}/cancel"
_RESOLVE = f"/api/interactions/sessions/{_SESSION_ID}/requests/{_REQUEST_ID}/resolve"
_CANCEL_INTERACTION = (
    f"/api/interactions/sessions/{_SESSION_ID}/suspensions/{_SUSPENSION_ID}/cancel"
)


# ---------- 情况（字面量的输入，不许改） ----------


@dataclass(frozen=True)
class Case:
    """一种 HTTP 情况，全部是 POST。

    session 是鉴权读到的会话 (agent_id, workflow_id)，None 表示会话不存在（请求不带 session_id
    时不会读取）；write_denied 表示当前用户对会话没有写权限；workflow_error 是 Workflow 前置校验
    抛出的 (文案, 状态码)；runtime_error 是运行时入口或轮次取消抛出的 (文案, 状态码)。
    """

    path: str
    body: dict[str, Any] | None = None
    session: tuple[int | None, int | None] | None = None
    write_denied: bool = False
    workflow_error: tuple[str, int] | None = None
    runtime_error: tuple[str, int | None] | None = None


_PLAIN = (None, None)
_AGENT_SESSION = (3, None)
_WORKFLOW_SESSION = (None, 8)


def _resolve(kind: str, response_payload: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "response_payload": response_payload}


CASES: dict[str, Case] = {
    # 发消息：分发到 Agent 运行时。
    "send/new_session_default_agent": Case(_SEND, {"content": "你好"}),
    "send/new_session_with_agent": Case(_SEND, {"content": "你好", "agent_id": 3}),
    "send/existing_plain_session": Case(
        _SEND, {"session_id": _SESSION_ID, "content": "继续"}, session=_PLAIN
    ),
    "send/existing_agent_session_same_agent": Case(
        _SEND, {"session_id": _SESSION_ID, "agent_id": 3, "content": "继续"}, session=_AGENT_SESSION
    ),
    "send/existing_agent_session_without_agent_id": Case(
        _SEND, {"session_id": _SESSION_ID, "content": "继续"}, session=_AGENT_SESSION
    ),
    # 发消息：分发到 Workflow 运行时（请求体带 workflow_id，或会话已锁定 Workflow）。
    "send/new_session_workflow": Case(_SEND, {"content": "写周报", "workflow_id": 8}),
    "send/existing_workflow_session_without_workflow_id": Case(
        _SEND, {"session_id": _SESSION_ID, "content": "继续"}, session=_WORKFLOW_SESSION
    ),
    "send/existing_workflow_session_same_workflow": Case(
        _SEND,
        {"session_id": _SESSION_ID, "workflow_id": 8, "content": "继续"},
        session=_WORKFLOW_SESSION,
    ),
    "send/existing_agent_session_with_null_workflow_id": Case(
        _SEND,
        {"session_id": _SESSION_ID, "workflow_id": None, "content": "继续"},
        session=_AGENT_SESSION,
    ),
    # 模型选项：只透传客户端显式提交的字段，显式提交默认值或 null 也算提交。
    "send/model_options_partial": Case(
        _SEND, {"content": "你好", "mode_slug": "turbo", "thinking_effort": "high"}
    ),
    "send/model_options_explicit_defaults": Case(
        _SEND, {"content": "你好", "model_provider_id": None, "mode_slug": "standard"}
    ),
    "send/model_options_all_on_existing_session": Case(
        _SEND,
        {
            "session_id": _SESSION_ID,
            "content": "继续",
            "model_provider_id": "provider-a",
            "mode_slug": "standard",
            "thinking_effort": "max",
        },
        session=_PLAIN,
    ),
    "send/model_options_with_workflow": Case(
        _SEND, {"content": "写周报", "workflow_id": 8, "thinking_effort": "low"}
    ),
    # 校验：Agent 与 Workflow 同传、已有会话切换、Workflow 前置校验、写权限、运行时准入失败与 Schema。
    "send/agent_and_workflow_together": Case(
        _SEND, {"content": "你好", "agent_id": 1, "workflow_id": 8}
    ),
    "send/agent_and_workflow_together_on_existing_session": Case(
        _SEND,
        {"session_id": _SESSION_ID, "content": "你好", "agent_id": 3, "workflow_id": 8},
        session=_AGENT_SESSION,
    ),
    "send/switch_agent": Case(
        _SEND, {"session_id": _SESSION_ID, "agent_id": 4, "content": "继续"}, session=_AGENT_SESSION
    ),
    "send/agent_on_plain_session": Case(
        _SEND, {"session_id": _SESSION_ID, "agent_id": 3, "content": "继续"}, session=_PLAIN
    ),
    "send/switch_workflow": Case(
        _SEND,
        {"session_id": _SESSION_ID, "workflow_id": 9, "content": "继续"},
        session=_WORKFLOW_SESSION,
    ),
    "send/workflow_on_agent_session": Case(
        _SEND, {"session_id": _SESSION_ID, "workflow_id": 8, "content": "继续"}, session=_AGENT_SESSION
    ),
    "send/workflow_on_plain_session": Case(
        _SEND, {"session_id": _SESSION_ID, "workflow_id": 8, "content": "继续"}, session=_PLAIN
    ),
    "send/new_workflow_not_found": Case(
        _SEND, {"content": "写周报", "workflow_id": 8}, workflow_error=("Workflow不存在", 404)
    ),
    "send/new_workflow_disabled": Case(
        _SEND, {"content": "写周报", "workflow_id": 8}, workflow_error=("Workflow已停用", 409)
    ),
    "send/session_not_found": Case(_SEND, {"session_id": _SESSION_ID, "content": "继续"}),
    "send/write_denied": Case(
        _SEND, {"session_id": _SESSION_ID, "content": "继续"}, session=_PLAIN, write_denied=True
    ),
    "send/agent_runtime_conflict": Case(
        _SEND,
        {"session_id": _SESSION_ID, "content": "继续"},
        session=_PLAIN,
        runtime_error=("该轮次已有执行中的任务", 409),
    ),
    "send/workflow_runtime_error": Case(
        _SEND,
        {"content": "写周报", "workflow_id": 8},
        runtime_error=("缺少可信用户或工作区上下文", None),
    ),
    "send/unknown_field": Case(_SEND, {"content": "你好", "extra_field": 1}),
    "send/orchestration_plan_id": Case(_SEND, {"content": "你好", "orchestration_plan_id": 1}),
    # 取消轮次。
    "cancel_turn/ok": Case(_CANCEL_TURN, session=_PLAIN),
    "cancel_turn/write_denied": Case(_CANCEL_TURN, session=_PLAIN, write_denied=True),
    "cancel_turn/session_not_found": Case(_CANCEL_TURN),
    "cancel_turn/not_executing": Case(
        _CANCEL_TURN, session=_PLAIN, runtime_error=("该轮次没有执行中的任务", 409)
    ),
    # 响应人工交互：四类交互的响应体按 Schema 规范化后原样交给恢复入口。
    "resolve/tool_approval_allow_once": Case(
        _RESOLVE, _resolve("tool_approval", {"decision": "allow_once"}), session=_PLAIN
    ),
    "resolve/tool_approval_always_allow_updated_input": Case(
        _RESOLVE,
        _resolve(
            "tool_approval",
            {
                "decision": "always_allow",
                "updated_input": {"file_path": "/srv/a.py"},
                "always_scope": "global",
            },
        ),
        session=_PLAIN,
    ),
    "resolve/tool_approval_deny_message": Case(
        _RESOLVE,
        _resolve("tool_approval", {"decision": "deny", "message": "  不允许写文件  "}),
        session=_PLAIN,
    ),
    "resolve/user_question_submit": Case(
        _RESOLVE,
        _resolve(
            "user_question",
            {
                "answers": {"用哪种格式？": "Markdown"},
                "annotations": {"用哪种格式？": {"notes": "简短"}},
            },
        ),
        session=_PLAIN,
    ),
    "resolve/user_question_cancel": Case(
        _RESOLVE, _resolve("user_question", {"decision": "cancel"}), session=_PLAIN
    ),
    "resolve/enter_plan_mode_reject": Case(
        _RESOLVE,
        _resolve("enter_plan_mode", {"decision": "reject", "feedback": "直接改"}),
        session=_PLAIN,
    ),
    "resolve/exit_plan_mode_approve": Case(
        _RESOLVE,
        _resolve("exit_plan_mode", {"decision": "approve", "edited_plan": "1. 先读代码"}),
        session=_PLAIN,
    ),
    "resolve/write_denied": Case(
        _RESOLVE,
        _resolve("tool_approval", {"decision": "allow_once"}),
        session=_PLAIN,
        write_denied=True,
    ),
    "resolve/session_not_found": Case(
        _RESOLVE, _resolve("tool_approval", {"decision": "allow_once"})
    ),
    "resolve/runtime_conflict": Case(
        _RESOLVE,
        _resolve("tool_approval", {"decision": "allow_once"}),
        session=_PLAIN,
        runtime_error=("该轮次已有执行中的任务", 409),
    ),
    "resolve/runtime_business_error": Case(
        _RESOLVE,
        _resolve("enter_plan_mode", {"decision": "approve"}),
        session=_PLAIN,
        runtime_error=("会话当前没有待处理的人工交互", None),
    ),
    "resolve/invalid_payload": Case(
        _RESOLVE,
        _resolve("tool_approval", {"decision": "deny", "updated_input": {"x": 1}}),
        session=_PLAIN,
    ),
    "resolve/unknown_kind": Case(
        _RESOLVE, _resolve("plan_review", {"decision": "approve"}), session=_PLAIN
    ),
    # 取消运行暂停点。
    "cancel_interaction/ok": Case(_CANCEL_INTERACTION, session=_PLAIN),
    "cancel_interaction/write_denied": Case(
        _CANCEL_INTERACTION, session=_PLAIN, write_denied=True
    ),
    "cancel_interaction/session_not_found": Case(_CANCEL_INTERACTION),
    "cancel_interaction/runtime_error": Case(
        _CANCEL_INTERACTION,
        session=_PLAIN,
        runtime_error=("无权限取消该交互轮次", 403),
    ),
}


# ---------- 替身与记录（不许改） ----------


class _World:
    """替身们共用的记录本：journal 按发生顺序记下每一次调用。"""

    def __init__(self, case: Case) -> None:
        self.case = case
        self.journal: list[dict[str, Any]] = []
        self.db = _FakeDb(self)

    def record(self, op: str, **fields: Any) -> None:
        self.journal.append({"op": op, **_jsonable(fields)})


class _FakeDb:
    """请求作用域的数据库会话替身：只记提交与回滚。"""

    def __init__(self, world: _World) -> None:
        self._world = world

    async def commit(self) -> None:
        self._world.record("db.commit")

    async def rollback(self) -> None:
        self._world.record("db.rollback")


def _raise_case_error(error: tuple[str, int | None] | None) -> None:
    if error is not None:
        message, status_code = error
        raise AgentException.message(message, status_code=status_code)


def _session_fakes(world: _World) -> dict[str, Any]:
    """会话与 Workflow 服务替身：按类替换，与谁来构造无关。"""

    async def get_required(self: SessionService, session_id: int) -> Any:
        world.record("session.get_required", session_id=session_id)
        if world.case.session is None:
            raise AgentException.message("会话不存在")
        agent_id, workflow_id = world.case.session
        return SimpleNamespace(id=session_id, agent_id=agent_id, workflow_id=workflow_id)

    async def check_write_access(
        self: SessionService, session: Any, user_id: int, workspace_id: int
    ) -> bool:
        world.record(
            "session.check_write_access",
            session_id=session.id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        return not world.case.write_denied

    async def require_runnable(self: Any, workflow_id: int, workspace_id: int) -> Any:
        world.record("workflow.require_runnable", workflow_id=workflow_id, workspace_id=workspace_id)
        _raise_case_error(world.case.workflow_error)
        return SimpleNamespace(id=workflow_id)

    return {
        "get_required": get_required,
        "check_write_access": check_write_access,
        "require_runnable": require_runnable,
    }


def _label(world: _World, value: Any) -> Any:
    """请求作用域的数据库会话记成固定标签，其余入参转成 JSON 原生结构。"""
    if value is world.db:
        return "<request-db>"
    return _jsonable(value)


def _call(world: _World, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "args": [_label(world, item) for item in args],
        "kwargs": {key: _label(world, item) for key, item in kwargs.items()},
    }


def _handle(session_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        turn_id=_TURN_ID, session_id=session_id, start_offset=5, start_cursor="0-0"
    )


def _runtime_class(world: _World, name: str) -> type:
    """运行时替身：记录构造与入口调用的完整参数，按情况抛出准入错误。"""

    class _Runtime:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            world.journal.append({"op": f"{name}.init", **_call(world, args, kwargs)})

        async def start(self, *args: Any, **kwargs: Any) -> Any:
            world.journal.append({"op": f"{name}.start", **_call(world, args, kwargs)})
            _raise_case_error(world.case.runtime_error)
            return _handle(args[0] or _NEW_SESSION_ID)

        async def start_resume(self, *args: Any, **kwargs: Any) -> Any:
            world.journal.append({"op": f"{name}.start_resume", **_call(world, args, kwargs)})
            _raise_case_error(world.case.runtime_error)
            return _handle(args[0])

        async def cancel_interaction(self, *args: Any, **kwargs: Any) -> Any:
            world.journal.append(
                {"op": f"{name}.cancel_interaction", **_call(world, args, kwargs)}
            )
            _raise_case_error(world.case.runtime_error)
            return SimpleNamespace(
                id=args[1],
                session_id=args[0],
                turn_id=_TURN_ID,
                status="cancelled",
                version=2,
                resolved_at=_STAMP,
                created_at=_STAMP,
                updated_at=_STAMP,
                continuation_payload={"secret": "续跑状态不出现在响应里"},
            )

    return _Runtime


def _cancel_turn_fake(world: _World) -> Any:
    async def cancel_turn(*args: Any, **kwargs: Any) -> Any:
        world.journal.append({"op": "cancel_turn", **_call(world, args, kwargs)})
        _raise_case_error(world.case.runtime_error)
        return SimpleNamespace(
            turn_id=args[2], session_id=args[1], status="interrupted", session_status="idle", offset=9
        )

    return cancel_turn


def _follow_fake(world: _World) -> Any:
    async def follow_execution(handle: Any) -> AsyncIterator[dict[str, Any]]:
        world.record(
            "follow_execution",
            turn_id=handle.turn_id,
            session_id=handle.session_id,
            start_offset=handle.start_offset,
            start_cursor=handle.start_cursor,
        )
        yield {"type": "run_started", "turn_id": str(handle.turn_id), "sequence": 6}
        yield {"type": "run_end", "turn_id": str(handle.turn_id), "status": "completed"}

    return follow_execution


def _request_fakes(world: _World) -> dict[str, Any]:
    """鉴权依赖与请求作用域数据库会话的替身；数据库会话按 get_db 的事务边界提交或回滚。"""

    async def fake_get_db() -> AsyncIterator[Any]:
        try:
            yield world.db
            await world.db.commit()
        except Exception:
            await world.db.rollback()
            raise

    return {
        get_db: fake_get_db,
        get_current_active_user: lambda: SimpleNamespace(id=_USER_ID),
        get_current_workspace_id: lambda: _WORKSPACE_ID,
    }


# ---------- 观测：响应、调用记录与日志（不许改） ----------


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


_LOG_BUILTIN_FIELDS = set(logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
}
# HTTP 客户端与事件循环自身的日志不属于被测代码。
_FOREIGN_LOGGERS = ("httpx", "httpcore", "asyncio", "anyio")


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.split(".")[0] in _FOREIGN_LOGGERS:
            return
        self.records.append(record)


@contextmanager
def _capture_logs() -> Iterator[_LogCollector]:
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


def _normalize(value: Any, request_id: str | None) -> Any:
    """request_id 每次请求随机生成，换成固定标签。"""
    if isinstance(value, dict):
        return {key: _normalize(item, request_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, request_id) for item in value]
    if request_id is not None and value == request_id:
        return "<request-id>"
    return value


def _log_entries(logs: _LogCollector, request_id: str | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for record in logs.records:
        extra = {
            key: _jsonable(item)
            for key, item in record.__dict__.items()
            if key not in _LOG_BUILTIN_FIELDS
        }
        if "duration_ms" in extra:
            extra["duration_ms"] = "<ms>"
        entries.append(
            {
                "logger": record.name,
                "level": record.levelname,
                "message": record.getMessage(),
                "extra": _normalize(extra, request_id),
            }
        )
    return entries


def _observation(world: _World, logs: _LogCollector, response: httpx.Response) -> dict[str, Any]:
    request_id = response.headers.get("x-request-id")
    content_type = response.headers.get("content-type", "")
    body: Any = response.text
    if content_type.startswith("application/json"):
        body = _normalize(response.json(), request_id)
    return {
        "status": response.status_code,
        "headers": {
            "content-type": content_type,
            "cache-control": response.headers.get("cache-control"),
            "x-accel-buffering": response.headers.get("x-accel-buffering"),
            "x-request-id": _normalize(request_id, request_id),
        },
        "body": body,
        "journal": world.journal,
        "logs": _log_entries(logs, request_id),
    }


# ---------- 驱动：如何把替身接进去、如何发请求（重构时只改这一节） ----------


def _install(monkeypatch: pytest.MonkeyPatch, world: _World) -> None:
    """会话与 Workflow 服务按类替换；运行时与轮次取消替换 get_agent_service 装配用的名字，
    走真实的 AgentService 依赖；SSE 跟随按路由模块里的名字替换。"""
    fakes = _session_fakes(world)
    monkeypatch.setattr(SessionService, "get_required", fakes["get_required"])
    monkeypatch.setattr(SessionService, "check_write_access", fakes["check_write_access"])
    monkeypatch.setattr(WorkflowService, "require_runnable", fakes["require_runnable"])
    monkeypatch.setattr(api_deps, "AgentRuntime", _runtime_class(world, "AgentRuntime"))
    monkeypatch.setattr(api_deps, "WorkflowRuntime", _runtime_class(world, "WorkflowRuntime"))
    monkeypatch.setattr(api_deps, "cancel_turn", _cancel_turn_fake(world))
    monkeypatch.setattr(session_api, "follow_execution", _follow_fake(world))
    monkeypatch.setattr(interaction_api, "follow_execution", _follow_fake(world))


async def _drive(case: Case, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = _World(case)
    _install(monkeypatch, world)
    saved = dict(app.dependency_overrides)
    app.dependency_overrides.update(_request_fakes(world))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        with _capture_logs() as logs:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(case.path, json=case.body)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
    return _observation(world, logs, response)


# ---------- 断言：观测的组成与比对（不许改） ----------


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
    assert set(EXPECTED) == set(CASES)


@pytest.mark.parametrize("name", list(CASES))
async def test_http_entry_matches_golden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = await _drive(CASES[name], monkeypatch)

    _assert_matches(name, actual)
