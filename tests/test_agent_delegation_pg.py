"""默认 Agent 委派自建 Agent 的真实 PostgreSQL 集成测试:目录的工作区隔离、启用状态与软删除,
以及委派时经 agent 域运行配置校验的拒绝路径。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库,整个用例包在外层事务里,结束时回滚,不留数据。
库不可达时跳过并给出原因。
"""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.models import AgentRecord
from agent.repository import AgentRepository
from agent.service import AgentDefinitionService
from core.config import settings
from harness.adapters.tools.builtin.agent import AgentTool, AgentToolInput
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.domain.tool import ToolContext
from runtime.agent import AgentRuntime
from tools.repository import ToolCatalogRepository
from tools.service import ToolCatalogService
from user.models import UserRecord
from workspace.models import WorkspaceRecord

pytestmark = pytest.mark.anyio

_TOOL_NAMES = ("Read", "Write", "Grep")


async def _enabled_tool_ids(db: AsyncSession) -> dict[str, int]:
    """取测试用内置工具的目录 ID;目录缺行时先同步,改动都随外层事务回滚。"""
    repo = ToolCatalogRepository(db)
    records = await repo.list_by_runtime_names(list(_TOOL_NAMES))
    if len(records) < len(_TOOL_NAMES):
        await ToolCatalogService(db).sync_builtins()
        records = await repo.list_by_runtime_names(list(_TOOL_NAMES))
    for record in records:
        record.is_enabled = True
    await db.flush()
    return {record.runtime_name: record.id for record in records}


@pytest.fixture
async def pg():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    user = UserRecord(
        email=f"delegate-{suffix}@test.local", username=f"delegate-{suffix}", password="x"
    )
    workspaces = [
        WorkspaceRecord(
            name=f"delegate-{i}-{suffix}", slug=f"delegate-{i}-{suffix}", display_name="委派测试"
        )
        for i in range(2)
    ]
    session.add_all([user, *workspaces])
    await session.flush()
    try:
        yield SimpleNamespace(
            db=session,
            user_id=user.id,
            workspace_id=workspaces[0].id,
            other_workspace_id=workspaces[1].id,
            tool_ids=await _enabled_tool_ids(session),
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _agent(
    env: SimpleNamespace,
    name: str,
    *,
    workspace_id: int | None = None,
    enabled: bool = True,
    tools: tuple[str, ...] = (),
) -> AgentRecord:
    repo = AgentRepository(env.db)
    record = await repo.create(
        workspace_id=workspace_id or env.workspace_id,
        name=name,
        description=f"{name}的描述",
        system_prompt=f"你是{name}",
        model_name=None,
        is_enabled=enabled,
        created_by_user_id=env.user_id,
    )
    await repo.replace_tool_bindings(record.id, [env.tool_ids[tool] for tool in tools])
    return record


def _names_by_tool_id(env: SimpleNamespace, *tools: str) -> tuple[str, ...]:
    """目录与运行配置都按工具 ID 排序,断言时用同一顺序。"""
    return tuple(sorted(tools, key=lambda tool: env.tool_ids[tool]))


class _FakeLLM:
    provider = "openai"
    model = "model-a"


def _ctx(env: SimpleNamespace) -> ToolContext:
    return ToolContext(
        session_id=1,
        db=env.db,
        workspace_id=env.workspace_id,
        user_id=env.user_id,
        profile="lead",
        allowed_tool_names=builtin_tool_set().names,
    )


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """替换真正跑子代理的 _dispatch:它另开连接,看不到外层未提交的测试数据。"""
    dispatches: list[dict[str, Any]] = []

    async def _fake_dispatch(self, session_id, prompt, spec, *args: Any) -> str:
        dispatches.append({"spec": spec, "allowed_skill_names": args[-1]})
        return "委派报告"

    monkeypatch.setattr(AgentTool, "_dispatch", _fake_dispatch)
    return dispatches


# ---------- 目录:只含当前工作区已启用、未软删除的 Agent ----------


async def test_catalog_lists_only_enabled_agents_of_current_workspace(pg) -> None:
    reviewer = await _agent(pg, "审稿人", tools=("Write", "Read"))
    writer = await _agent(pg, "写手")
    await _agent(pg, "停用者", enabled=False, tools=("Read",))
    deleted = await _agent(pg, "已删除", tools=("Read",))
    await AgentRepository(pg.db).soft_delete(deleted)
    outsider = await _agent(pg, "别家", workspace_id=pg.other_workspace_id, tools=("Read",))

    entries = await AgentDefinitionService(pg.db).list_delegate_catalog(pg.workspace_id)
    other_entries = await AgentDefinitionService(pg.db).list_delegate_catalog(pg.other_workspace_id)

    assert [entry.agent_id for entry in entries] == [reviewer.id, writer.id]
    assert entries[0].name == "审稿人"
    assert entries[0].description == "审稿人的描述"
    assert entries[0].tool_names == _names_by_tool_id(pg, "Read", "Write")
    assert entries[1].tool_names == ()
    assert [entry.agent_id for entry in other_entries] == [outsider.id]


async def test_catalog_skips_disabled_tool_while_delegation_rejects_it(pg, monkeypatch) -> None:
    """个别绑定失效不拖垮整份目录;委派时由运行配置校验给出中文原因。"""
    searcher = await _agent(pg, "检索员", tools=("Grep", "Read"))
    grep = await ToolCatalogRepository(pg.db).get_by_id(pg.tool_ids["Grep"])
    grep.is_enabled = False
    await pg.db.flush()
    _patch_dispatch(monkeypatch)

    (entry,) = await AgentDefinitionService(pg.db).list_delegate_catalog(pg.workspace_id)
    output = await AgentTool().run(AgentToolInput(agent_id=searcher.id, prompt="检索"), _ctx(pg))

    assert entry.tool_names == ("Read",)
    assert "绑定的工具已停用" in json.loads(output)["error"]


# ---------- 运行时:只给默认 Agent 注入目录 ----------


async def test_runtime_injects_catalog_only_for_lead_session(pg) -> None:
    reviewer = await _agent(pg, "审稿人", tools=("Read",))
    await _agent(pg, "停用者", enabled=False)
    await _agent(pg, "别家", workspace_id=pg.other_workspace_id)
    runtime = AgentRuntime(pg.db, user_id=pg.user_id, workspace_id=pg.workspace_id, llm=_FakeLLM())

    await runtime._configure_agent(SimpleNamespace(agent_id=None, mode_slug="standard"))
    lead_catalog = runtime.configuration.delegate_catalog
    await runtime._configure_agent(SimpleNamespace(agent_id=reviewer.id, mode_slug="standard"))

    assert lead_catalog == f"- agent_id={reviewer.id} 名称:审稿人 描述:审稿人的描述 工具:Read"
    # 绑定了自建 Agent 的会话不注入目录。
    assert runtime.configuration.delegate_catalog is None


async def test_runtime_without_enabled_agents_injects_no_catalog(pg) -> None:
    await _agent(pg, "停用者", enabled=False)
    runtime = AgentRuntime(pg.db, user_id=pg.user_id, workspace_id=pg.workspace_id, llm=_FakeLLM())

    await runtime._configure_agent(SimpleNamespace(agent_id=None, mode_slug="standard"))

    assert runtime.configuration.delegate_catalog is None


# ---------- 委派:经 agent 域运行配置校验 ----------


async def test_delegation_uses_agent_runtime_config(pg, monkeypatch) -> None:
    reviewer = await _agent(pg, "审稿人", tools=("Write", "Read"))
    dispatches = _patch_dispatch(monkeypatch)

    output = await AgentTool().run(AgentToolInput(agent_id=reviewer.id, prompt="审稿"), _ctx(pg))

    assert json.loads(output) == {
        "agent": "delegate-审稿人",
        "report": "委派报告",
        "error": None,
        "verdict": None,
        "verdict_note": None,
    }
    spec = dispatches[0]["spec"]
    assert (spec.agent_id, spec.tool_profile) == (reviewer.id, "delegate")
    assert spec.system_prompt == "你是审稿人"
    assert spec.allowed_tools == _names_by_tool_id(pg, "Read", "Write")
    assert dispatches[0]["allowed_skill_names"] == frozenset()


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("disabled", "Agent已停用"),
        ("other_workspace", "Agent不存在"),
        ("soft_deleted", "Agent不存在"),
        ("missing", "Agent不存在"),
    ],
)
async def test_delegation_rejects_unavailable_agent(pg, monkeypatch, target, message) -> None:
    if target == "disabled":
        agent_id = (await _agent(pg, "停用者", enabled=False, tools=("Read",))).id
    elif target == "other_workspace":
        agent_id = (await _agent(pg, "别家", workspace_id=pg.other_workspace_id)).id
    elif target == "soft_deleted":
        deleted = await _agent(pg, "已删除", tools=("Read",))
        await AgentRepository(pg.db).soft_delete(deleted)
        agent_id = deleted.id
    else:
        agent_id = 2_000_000_000
    dispatches = _patch_dispatch(monkeypatch)

    output = await AgentTool().run(AgentToolInput(agent_id=agent_id, prompt="审稿"), _ctx(pg))

    result = json.loads(output)
    assert result["report"] is None
    assert result["error"] == f"无法委派给 agent_id={agent_id} 的 Agent:{message}"
    assert dispatches == []
