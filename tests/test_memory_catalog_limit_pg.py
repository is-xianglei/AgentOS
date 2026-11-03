"""Memory Catalog 字节上限的真实 PostgreSQL 复现：后台写入绕过预算后的连锁故障。

用户 CRUD 写入后会按 MEMORY_CATALOG_MAX_BYTES 重验 Catalog；抽取与 Dream 只检查条数，
不检查字节。本文件用伪造的 LLM 输出驱动真实的入队、认领、prepare、apply 链路写库，
再走真实的召回与轮次执行观察故障。已确认的 bug 用 strict xfail 标记，修复后会因
XPASS 失败提醒移除标记；前置条件在修复后无法再由真实路径构造时抛 _SetupError。

数据隔离沿用 test_turn_cancel_pg：整个用例包在外层事务里，结束时回滚；各会话以
create_savepoint 方式共享同一连接，commit 只释放 SAVEPOINT，不落库。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException
from core.events import BlockType
from llm.types import LLMEventKind, LLMStreamEvent, LLMTurnResult, build_assistant_blocks
from memory.jobs.dream import MemoryDreamService
from memory.jobs.service import MemoryJobRunner, MemoryJobService
from memory.models import MemoryItemRecord, MemoryJobRecord
from memory.repository import MemoryRepository
from memory.service import MEMORY_CATALOG_MAX_BYTES, MEMORY_CATALOG_MAX_ITEMS, MemoryService
from runtime import execution
from runtime.agent import AgentRuntime
from session.models import SessionMessage, SessionRecord
from session.service import SessionService
from user.models import UserRecord
from workspace.models import WorkspaceRecord

pytestmark = pytest.mark.anyio

_BUG = "已复现的 bug：抽取与 Dream 写入不校验 Catalog 字节上限，超限后读 Catalog 硬失败"
_BUDGET_ERROR = f"Memory Catalog 不能超过 {MEMORY_CATALOG_MAX_BYTES} 字节"


class _SetupError(Exception):
    """前置条件未达成；与 xfail 的 raises 类型不同，避免被误判为预期失败。"""


class _FakeLLM:
    """LLM 替身：结构化调用按场景返回预设的抽取候选或 Dream 操作，流式调用返回固定文本。"""

    def __init__(self) -> None:
        self.extractions: list[list[dict[str, Any]]] = []
        self.dream_builder: Any = None
        self.stream_calls = 0

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
        payload = json.loads(messages[-1]["content"])
        name = output_format.__name__
        if name == "MemoryExtractionResult":
            # 来源引用当前轮次的用户消息：窗口内最后一条 user 消息即本轮请求。
            source_id = [m["id"] for m in payload["messages"] if m["role"] == "user"][-1]
            batch = self.extractions.pop(0)
            memories = [{**candidate, "source_message_ids": [source_id]} for candidate in batch]
            return output_format.model_validate({"memories": memories})
        if name == "MemoryDreamResult":
            operations = self.dream_builder(payload["active_memories"])
            return output_format.model_validate({"operations": operations})
        # 召回选择器：不选任何条目，让本轮只依赖 Catalog 渲染本身。
        return output_format.model_validate({"selected_memory_ids": []})

    async def stream(self, messages: list[dict[str, Any]], *args: Any, **kwargs: Any):
        self.stream_calls += 1
        text = "好的。"
        yield LLMStreamEvent(LLMEventKind.BLOCK_START, block_ref=0, block_type=BlockType.TEXT)
        yield LLMStreamEvent(LLMEventKind.TEXT_DELTA, block_ref=0, text=text)
        yield LLMStreamEvent(LLMEventKind.BLOCK_STOP, block_ref=0)
        yield LLMStreamEvent(
            LLMEventKind.TURN_RESULT,
            result=LLMTurnResult(text=text, stop_reason="end_turn"),
        )


class _RecordingBus:
    """替身事件总线：按序记录事件类型，即 SSE 订阅方能看到的帧序列。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [str(getattr(event.type, "value", event.type)) for event in self.events]


class _Shared:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.lock:
            db = AsyncSession(
                bind=self.conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                yield db
            finally:
                await db.close()


@pytest.fixture
async def pg():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    shared = _Shared(conn)
    suffix = uuid.uuid4().hex[:12]
    async with shared.session() as db:
        user = UserRecord(
            email=f"catalog-{suffix}@test.local", username=f"catalog-{suffix}", password="x"
        )
        workspace = WorkspaceRecord(
            name=f"catalog-{suffix}", slug=f"catalog-{suffix}", display_name="Catalog上限测试"
        )
        db.add_all([user, workspace])
        await db.flush()
        await db.commit()
        ids = (user.id, workspace.id)
    try:
        yield shared, ids
    finally:
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture(autouse=True)
def memory_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定与 .env 无关的 Memory 开关：抽取走 active 写库，召回与 Dream 灰度默认关闭。

    就地执行提取会用全局 AsyncSessionLocal 跳出测试事务，这里关闭，改为显式驱动 Runner。
    """
    monkeypatch.setattr(settings, "memory_extraction_enabled", True)
    monkeypatch.setattr(settings, "memory_extraction_shadow_enabled", False)
    monkeypatch.setattr(settings, "memory_extraction_inline", False)
    monkeypatch.setattr(settings, "memory_recall_rollout_percent", 0)
    monkeypatch.setattr(settings, "memory_dream_enabled", True)
    monkeypatch.setattr(settings, "memory_dream_rollout_percent", 0)


# 500 个中文字符，抽取与 Dream 允许的摘要上限；UTF-8 下每字 3 字节。
_LONG_DESC = ("团队约定发布前需完成代码评审与回归验证并同步更新变更说明" * 20)[:500]
_SHORT_DESC = "团队发布约定"
_BODY = "团队约定：发布前完成代码评审与回归验证，并同步更新变更说明。"


def _candidates(start: int, count: int, description: str) -> list[dict[str, Any]]:
    return [
        {
            "memory_key": f"team-rule-{index:03d}",
            "type": "project",
            "name": f"团队约定{index:03d}",
            "description": description,
            "body": _BODY,
        }
        for index in range(start, start + count)
    ]


async def _new_session(shared: _Shared, ids, *, mode_slug: str = "standard") -> int:
    user_id, workspace_id = ids
    async with shared.session() as db:
        session = SessionRecord(
            title="t",
            status="idle",
            user_id=user_id,
            workspace_id=workspace_id,
            mode_slug=mode_slug,
        )
        db.add(session)
        await db.flush()
        await db.commit()
        return session.id


async def _extract_turn(shared: _Shared, ids, session_id: int, llm: _FakeLLM) -> dict[str, Any]:
    """按真实完成路径建一个已完成 Turn 并入队提取 Job，再用 Runner 走 prepare/extract/apply。"""
    user_id, workspace_id = ids
    async with shared.session() as db:
        service = SessionService(db)
        session = await service.get_required(session_id)
        turn, _ = await service.start_turn(session, user_id, workspace_id, "请记住团队发布约定")
        reply = await service.add_message(
            session_id,
            "assistant",
            build_assistant_blocks("好的，已记住。", []),
            turn_id=turn.id,
        )
        await service.mark_turn_status(turn, "completed", completed_message_id=reply.id)
        try:
            job = await MemoryJobService(db).enqueue_extract(session, turn)
        except AgentException as exc:
            raise _SetupError(f"提取 Job 入队失败: {exc.message}") from exc
        await db.commit()
        job_id = job.id

    runner = MemoryJobRunner(
        session_factory=shared.session,
        llm=llm,
        claim_scope=(workspace_id, user_id),
    )
    await runner.run_once(job_id=job_id)
    async with shared.session() as db:
        job = await db.get(MemoryJobRecord, job_id)
        if job is None or job.status != "succeeded":
            status = None if job is None else (job.status, job.last_error_code)
            raise _SetupError(f"提取 Job 未成功: {status}")
        return dict(job.payload["result"])


async def _seed(
    shared: _Shared,
    ids,
    session_id: int,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int,
    description: str,
    trigger_dream: bool = False,
) -> None:
    """按抽取单次上限分批写入；trigger_dream 时最后一批打开 Dream 灰度，由真实门控入队。"""
    batch_size = settings.memory_extractor_max_items
    starts = list(range(0, count, batch_size))
    for position, start in enumerate(starts):
        if trigger_dream and position == len(starts) - 1:
            monkeypatch.setattr(settings, "memory_dream_rollout_percent", 100)
            monkeypatch.setattr(settings, "memory_dream_min_items", 1)
            monkeypatch.setattr(settings, "memory_dream_min_sessions", 1)
        size = min(batch_size, count - start)
        llm.extractions.append(_candidates(start, size, description))
        result = await _extract_turn(shared, ids, session_id, llm)
        if len(result["created_memory_ids"]) != size or result["rejected_count"]:
            raise _SetupError(f"提取结果与预期不符: {result}")


async def _catalog_stats(shared: _Shared, ids) -> tuple[int, int]:
    """不经预算校验直接渲染 Catalog，返回 (active 条数, UTF-8 字节数)。"""
    user_id, workspace_id = ids
    async with shared.session() as db:
        snapshot = await MemoryRepository(db).get_catalog_snapshot(
            workspace_id,
            user_id,
            limit=MEMORY_CATALOG_MAX_ITEMS + 1,
        )
        content = MemoryService._render_catalog(snapshot.entries)
        return len(snapshot.entries), len(content.encode("utf-8"))


@dataclass
class _Scenario:
    shared: _Shared
    ids: tuple[int, int]
    llm: _FakeLLM
    session_id: int


@pytest.fixture
async def over_limit(pg, monkeypatch: pytest.MonkeyPatch) -> _Scenario:
    """经 4 次真实抽取写入 20 条 500 中文字符摘要，Catalog 约 30 KiB，超出上限约 5 KiB。

    超出量大于单条记忆的行长，删除或缩短任意一条都不足以回到上限内。
    """
    shared, ids = pg
    llm = _FakeLLM()
    session_id = await _new_session(shared, ids)
    await _seed(shared, ids, session_id, llm, monkeypatch, count=20, description=_LONG_DESC)
    count, byte_count = await _catalog_stats(shared, ids)
    if count != 20 or byte_count <= MEMORY_CATALOG_MAX_BYTES:
        raise _SetupError(f"未能经真实抽取构造超限 Catalog: count={count} bytes={byte_count}")
    return _Scenario(shared, ids, llm, session_id)


@dataclass(frozen=True)
class _TurnOutcome:
    turn_status: str
    session_status: str
    run_end_status: str
    events: tuple[str, ...]
    assistant_replies: int
    failure: BaseException | None


async def _run_turn(
    scenario: _Scenario,
    caplog: pytest.LogCaptureFixture,
    session_id: int,
) -> _TurnOutcome:
    """按 AgentRuntime.start 的准入顺序建 Turn 并认领租约，再执行 _produce 与持有者收尾。

    不经 launch_turn_execution：其心跳与 offset 预留会和执行体争用测试的单一连接。
    这里用记录型总线替代 RunStreamBus，收尾沿用 execution._finish 的 finish_turn_execution
    与 run_end_status 映射，得到订阅方最终看到的 run_end 状态。
    """
    shared, (user_id, workspace_id) = scenario.shared, scenario.ids
    owner = execution.new_lease_owner()
    async with shared.session() as db:
        service = SessionService(db)
        session = await service.prepare_for_message(session_id, "继续", user_id, workspace_id)
        turn, _ = await service.start_turn(session, user_id, workspace_id, "继续")
        await service.claim_turn_lease(turn.id, owner, 60.0)
        await db.commit()
        turn_id = turn.id

    bus = _RecordingBus()
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="runtime.agent"):
        async with shared.session() as db:
            runtime = AgentRuntime(db, user_id=user_id, workspace_id=workspace_id, llm=scenario.llm)
            runtime.memory_recall_service.llm = scenario.llm
            runtime.bind_execution(bus, owner)
            await runtime._produce(session_id, turn_id, "继续")
    failures = [
        record.exc_info[1]
        for record in caplog.records
        if record.getMessage() == "处理会话消息失败" and record.exc_info
    ]

    async with shared.session() as db:
        service = SessionService(db)
        termination = await service.finish_turn_execution(session_id, turn_id, owner)
        await service.release_turn_lease(turn_id, owner)
        await db.commit()
        replies = await db.scalars(
            select(SessionMessage).where(
                SessionMessage.turn_id == turn_id,
                SessionMessage.role == "assistant",
            )
        )
        return _TurnOutcome(
            turn_status=termination.status,
            session_status=termination.session_status,
            run_end_status=execution.run_end_status(termination.status),
            events=tuple(bus.types()),
            assistant_replies=len(replies.all()),
            failure=failures[0] if failures else None,
        )


@pytest.mark.xfail(strict=True, raises=AgentException, reason=_BUG)
async def test_extraction_keeps_catalog_within_byte_budget(pg, monkeypatch) -> None:
    """A：抽取 apply 只按条数拦截（extraction.py:464），17 条长摘要即可写穿字节上限。"""
    shared, ids = pg
    llm = _FakeLLM()
    session_id = await _new_session(shared, ids)
    await _seed(shared, ids, session_id, llm, monkeypatch, count=16, description=_LONG_DESC)
    count, byte_count = await _catalog_stats(shared, ids)
    if count != 16 or byte_count > MEMORY_CATALOG_MAX_BYTES:
        raise _SetupError(f"16 条长摘要应仍在上限内: count={count} bytes={byte_count}")

    # 第 17 条由一次新的真实抽取写入，单条约 1.5 KiB，足以越过 25 KiB。
    llm.extractions.append(_candidates(16, 1, _LONG_DESC))
    await _extract_turn(shared, ids, session_id, llm)

    user_id, workspace_id = ids
    async with shared.session() as db:
        catalog = await MemoryService(db).get_rendered_catalog(workspace_id, user_id)
    assert catalog.byte_count <= MEMORY_CATALOG_MAX_BYTES


def _dream_update(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每条记忆的摘要改写为 500 中文字符，条数不变。"""
    return [
        {
            "action": "update",
            "item_id": entry["item_id"],
            "expected_version": entry["version"],
            "target": {
                "memory_key": entry["memory_key"],
                "type": entry["type"],
                "name": entry["name"],
                "description": _LONG_DESC,
                "body": entry["body"],
            },
        }
        for entry in entries
    ]


def _dream_merge(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """两两合并，条数减半，但存活者的摘要扩写为 500 中文字符。"""
    ordered = sorted(entries, key=lambda entry: entry["item_id"])
    operations = []
    for survivor, source in zip(ordered[0::2], ordered[1::2], strict=True):
        operations.append(
            {
                "action": "merge",
                "source_ids": [survivor["item_id"], source["item_id"]],
                "target": {
                    "memory_key": survivor["memory_key"],
                    "type": survivor["type"],
                    "name": survivor["name"],
                    "description": _LONG_DESC,
                    "body": f"{survivor['body']}\n{source['body']}",
                },
            }
        )
    return operations


async def _run_dream(shared: _Shared, ids, llm: _FakeLLM) -> MemoryJobRecord:
    """执行门控入队的唯一 Dream Job，返回其终态记录。"""
    user_id, workspace_id = ids
    async with shared.session() as db:
        space = await MemoryRepository(db).get_space(workspace_id, user_id)
        jobs = (
            await db.scalars(
                select(MemoryJobRecord).where(
                    MemoryJobRecord.space_id == space.id,
                    MemoryJobRecord.job_type == "dream",
                )
            )
        ).all()
        if len(jobs) != 1:
            raise _SetupError(f"真实门控应恰好入队 1 个 Dream Job，实际 {len(jobs)} 个")
        job_id = jobs[0].id
    runner = MemoryJobRunner(
        session_factory=shared.session,
        llm=llm,
        claim_scope=(workspace_id, user_id),
    )
    if await runner.run_once() != 1:
        raise _SetupError("Dream Job 未被认领")
    async with shared.session() as db:
        return await db.get(MemoryJobRecord, job_id)


@pytest.mark.xfail(strict=True, raises=AgentException, reason=_BUG)
@pytest.mark.parametrize(
    ("seed_count", "builder"),
    [(17, _dream_update), (34, _dream_merge)],
    ids=["update", "merge"],
)
async def test_dream_keeps_catalog_within_byte_budget(
    pg,
    monkeypatch,
    seed_count: int,
    builder: Any,
) -> None:
    """B：Dream apply（dream.py:360-483）不重验 Catalog，可把未超限的 Catalog 推到超限。

    merge 场景条数减半仍超限，说明「合并会缩短 Catalog」不成立。
    """
    shared, ids = pg
    llm = _FakeLLM()
    session_id = await _new_session(shared, ids)
    await _seed(
        shared,
        ids,
        session_id,
        llm,
        monkeypatch,
        count=seed_count,
        description=_SHORT_DESC,
        trigger_dream=True,
    )
    _, before_bytes = await _catalog_stats(shared, ids)
    if before_bytes > MEMORY_CATALOG_MAX_BYTES:
        raise _SetupError(f"Dream 前 Catalog 应在上限内: bytes={before_bytes}")

    llm.dream_builder = builder
    await _run_dream(shared, ids, llm)

    user_id, workspace_id = ids
    async with shared.session() as db:
        catalog = await MemoryService(db).get_rendered_catalog(workspace_id, user_id)
    assert catalog.byte_count <= MEMORY_CATALOG_MAX_BYTES


async def test_dream_rollback_restores_overflowed_catalog(pg, monkeypatch) -> None:
    """D：Dream 造成的超限可经回滚 API 自救，回滚路径不重验 Catalog，不会被 409 拦住。"""
    shared, ids = pg
    llm = _FakeLLM()
    session_id = await _new_session(shared, ids)
    await _seed(
        shared,
        ids,
        session_id,
        llm,
        monkeypatch,
        count=17,
        description=_SHORT_DESC,
        trigger_dream=True,
    )
    llm.dream_builder = _dream_update
    job = await _run_dream(shared, ids, llm)
    _, after_bytes = await _catalog_stats(shared, ids)
    if job.status != "succeeded" or after_bytes <= MEMORY_CATALOG_MAX_BYTES:
        raise _SetupError(f"需要一次写穿上限的 Dream: status={job.status} bytes={after_bytes}")

    user_id, workspace_id = ids
    async with shared.session() as db:
        await MemoryDreamService(db).rollback_dream_for_api(
            workspace_id,
            user_id,
            job.id,
            expected_after_catalog_version=job.payload["dream_audit"]["after_catalog_version"],
        )
        await db.commit()
    async with shared.session() as db:
        catalog = await MemoryService(db).get_rendered_catalog(workspace_id, user_id)
    assert catalog.byte_count <= MEMORY_CATALOG_MAX_BYTES
    assert len(catalog.snapshot.entries) == 17


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_BUG)
async def test_standard_turn_survives_overflowed_catalog(over_limit, monkeypatch, caplog) -> None:
    """C：召回灰度命中时，get_rendered_catalog 的 409 未被 _run_llm_loop 捕获，整轮失败。

    _produce 吞掉异常并标 failed，因此 xfail 以断言失败呈现；仅在失败原因确为
    Catalog 字节超限时才进入断言，其他失败原因按 _SetupError 暴露。
    """
    monkeypatch.setattr(settings, "memory_recall_rollout_percent", 100)
    outcome = await _run_turn(over_limit, caplog, over_limit.session_id)
    if outcome.failure is not None and _BUDGET_ERROR not in str(outcome.failure):
        raise _SetupError(f"轮次因其他原因失败: {outcome.failure!r}")
    assert outcome.failure is None, f"{type(outcome.failure).__name__}: {outcome.failure}"
    assert outcome.turn_status == "completed"
    assert outcome.assistant_replies == 1


async def test_turbo_and_unsampled_turns_ignore_overflowed_catalog(
    over_limit,
    monkeypatch,
    caplog,
) -> None:
    """C：turbo 跳过召回（agent.py:698）；召回灰度未命中时不读 Catalog（recall/service.py:70）。"""
    # 极速模式准入要求 provider 能关闭思考；.env 未配置模型服务时只有 anthropic 满足。
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "memory_recall_rollout_percent", 100)
    turbo_session = await _new_session(over_limit.shared, over_limit.ids, mode_slug="turbo")
    turbo = await _run_turn(over_limit, caplog, turbo_session)
    assert turbo.failure is None, repr(turbo.failure)
    assert (turbo.turn_status, turbo.session_status, turbo.run_end_status) == (
        "completed",
        "idle",
        "completed",
    )

    monkeypatch.setattr(settings, "memory_recall_rollout_percent", 0)
    standard = await _run_turn(over_limit, caplog, over_limit.session_id)
    assert standard.failure is None, repr(standard.failure)
    assert (standard.turn_status, standard.assistant_replies) == ("completed", 1)


async def _first_item(scenario: _Scenario) -> MemoryItemRecord:
    user_id, workspace_id = scenario.ids
    async with scenario.shared.session() as db:
        page = await MemoryService(db).list_memories(
            workspace_id,
            user_id,
            status="active",
            memory_type=None,
            limit=1,
            offset=0,
        )
        return page.items[0]


@pytest.mark.xfail(strict=True, raises=AgentException, reason=_BUG)
async def test_user_can_delete_memory_from_overflowed_catalog(over_limit) -> None:
    """D：delete_memory 删除后仍重验整份 Catalog（service.py:344），单条删除被 409 拒绝。"""
    item = await _first_item(over_limit)
    user_id, workspace_id = over_limit.ids
    async with over_limit.shared.session() as db:
        await MemoryService(db).delete_memory(
            workspace_id,
            user_id,
            item.id,
            expected_version=item.version,
        )
        await db.commit()
    count, _ = await _catalog_stats(over_limit.shared, over_limit.ids)
    assert count == 19


@pytest.mark.xfail(strict=True, raises=AgentException, reason=_BUG)
async def test_user_can_shrink_memory_in_overflowed_catalog(over_limit) -> None:
    """D：update_memory 把摘要缩短仍被 409 拒绝（service.py:306），编辑也无法自救。"""
    item = await _first_item(over_limit)
    _, before_bytes = await _catalog_stats(over_limit.shared, over_limit.ids)
    user_id, workspace_id = over_limit.ids
    async with over_limit.shared.session() as db:
        await MemoryService(db).update_memory(
            workspace_id,
            user_id,
            item.id,
            expected_version=item.version,
            description="已精简的摘要",
        )
        await db.commit()
    _, after_bytes = await _catalog_stats(over_limit.shared, over_limit.ids)
    assert after_bytes < before_bytes


async def test_reads_export_and_purge_work_on_overflowed_catalog(over_limit) -> None:
    """D：列表、搜索、详情、导出不读渲染后的 Catalog；彻底删除不重验 Catalog，可整体清空。"""
    user_id, workspace_id = over_limit.ids
    async with over_limit.shared.session() as db:
        service = MemoryService(db)
        page = await service.list_memories(
            workspace_id,
            user_id,
            status="active",
            memory_type=None,
            limit=50,
            offset=0,
        )
        assert page.total == 20
        found = await service.search_memories(
            workspace_id,
            user_id,
            query="团队约定",
            memory_type=None,
            limit=50,
        )
        assert len(found) == 20
        detail = await service.get_memory(workspace_id, user_id, page.items[0].id)
        assert detail.item.description == _LONG_DESC
        exported = await service.export_memories(workspace_id, user_id)
        assert len(exported.bundle.items) == 20

        confirmation = await service.issue_purge_confirmation(workspace_id, user_id)
        await service.purge_memories(
            workspace_id,
            user_id,
            confirmation_token=confirmation.confirmation_token,
            expected_catalog_version=confirmation.expected_catalog_version,
        )
        await db.commit()
    async with over_limit.shared.session() as db:
        catalog = await MemoryService(db).get_rendered_catalog(workspace_id, user_id)
    assert catalog.snapshot.entries == ()
