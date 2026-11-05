"""官方预置同步的真实 PostgreSQL 集成测试：新建、内容变化后新增版本、内容未变不动
（removed 的恢复上架）、代码里删除后标为 removed，以及 Agent 预置与社区快照格式一致。

预置目录用 tmp_path 临时构造，official_key 带随机前缀，不和开发库里真实的官方条目冲突。
连接 .env 中 AGENTOS_DATABASE_URL 指向的库，整个用例包在外层事务里，结束时回滚，不留数据；
同步时开发库里真实的官方条目因不在临时目录中会被标为 removed，也随外层事务一起回滚。
库不可达时跳过并给出原因。
"""

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.service import AgentService
from core.config import settings
from core.errors import AgentException
from market.presets import REMOVED_REASON, PresetSyncOutcome, load_presets, sync_official_presets
from market.repository import MarketRepository
from market.service import compute_snapshot_hash
from skill.parser import SkillBundle, build_skill_zip
from skill.service import SkillService
from tools.repository import ToolCatalogRepository
from tools.service import ToolCatalogService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

# Bash 也确保启用，用来验证「档位不允许绑定」的报错不会被「已停用」抢先。
_TOOL_NAMES = ("Read", "Grep", "Bash")


async def _enabled_tool_ids(db: AsyncSession) -> dict[str, int]:
    """取测试用到的内置工具的目录 ID；目录缺行时先同步，改动都随外层事务回滚。"""
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
    suffix = uuid.uuid4().hex[:10]
    user = UserRecord(
        email=f"preset-{suffix}@test.local", username=f"preset-{suffix}", password="x"
    )
    workspace = WorkspaceRecord(
        name=f"preset-{suffix}", slug=f"preset-{suffix}", display_name=f"预置测试{suffix}"
    )
    session.add_all([user, workspace])
    await session.flush()
    session.add(
        WorkspaceMemberRecord(
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
            joined_at=datetime.now(UTC),
        )
    )
    await session.flush()
    try:
        yield SimpleNamespace(
            db=session,
            suffix=suffix,
            user_id=user.id,
            workspace_id=workspace.id,
            tool_ids=await _enabled_tool_ids(session),
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


def _write_skill(root: Path, name: str, body: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\nname: {name}\ndescription: {name} 的描述\n"
        f"metadata:\n  market_category: office\n---\n\n{body}\n"
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    (skill_dir / "templates").mkdir(exist_ok=True)
    (skill_dir / "templates" / "minutes.md").write_text("# 纪要模板\n", encoding="utf-8")


def _write_agent(root: Path, key: str, skill_name: str, prompt: str = "你是纪要助手。") -> None:
    agent_dir = root / "agents" / key
    agent_dir.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\nname: 纪要助手{key}\ndescription: 整理会议纪要\ncategory: office\n"
        f"tools: [Read, Grep]\nskills: [{skill_name}]\n---\n\n{prompt}\n"
    )
    (agent_dir / "AGENT.md").write_text(text, encoding="utf-8")


async def _sync(env: SimpleNamespace, root: Path) -> dict[tuple[str, str], PresetSyncOutcome]:
    """同步一次，只返回本用例自己的条目（official_key 带随机后缀）。"""
    outcomes = await sync_official_presets(env.db, root)
    mine: dict[tuple[str, str], PresetSyncOutcome] = {}
    for outcome in outcomes:
        if outcome.official_key is not None and env.suffix in outcome.official_key:
            mine[(outcome.kind, outcome.official_key)] = outcome
    return mine


def _actions(outcomes: dict[tuple[str, str], PresetSyncOutcome]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for key, outcome in outcomes.items():
        result[key] = outcome.action
    return result


async def test_sync_creates_updates_removes_and_restores(pg, tmp_path: Path) -> None:
    root = tmp_path / "presets"
    skill_name = f"t{pg.suffix}-notes"
    agent_key = f"t{pg.suffix}-helper"
    skill_key = ("skill", skill_name)
    agent_key_pair = ("agent", agent_key)
    _write_skill(root, skill_name, "第一版正文")
    _write_agent(root, agent_key, skill_name)
    repo = MarketRepository(pg.db)

    # 1. 代码里有、库里没有：新建条目和版本 1，直接上架，不经审核。
    first = await _sync(pg, root)

    assert _actions(first) == {skill_key: "created", agent_key_pair: "created"}
    expected = {}
    for preset in load_presets(root):
        expected[(preset.kind, preset.key)] = preset.snapshot
    for key, outcome in first.items():
        listing = await repo.get_listing(outcome.listing_id)
        assert listing is not None
        assert (listing.is_official, listing.official_key) == (True, key[1])
        assert listing.status == "listed"
        assert (listing.publisher_workspace_id, listing.created_by_user_id) == (None, None)
        assert (listing.current_version_number, listing.category) == (1, "office")
        version = await repo.get_version_by_number(listing.id, 1)
        assert version is not None
        assert version.status == "approved"
        assert (version.submitted_by_user_id, version.reviewed_by_user_id) == (None, None)
        assert version.snapshot == expected[key]
        assert version.snapshot_hash == compute_snapshot_hash(expected[key])
    agent_listing = await repo.get_listing(first[agent_key_pair].listing_id)
    assert agent_listing is not None
    assert (agent_listing.name, agent_listing.summary) == (f"纪要助手{agent_key}", "整理会议纪要")

    # 2. 内容没变：不新增版本。
    second = await _sync(pg, root)

    assert _actions(second) == {skill_key: "unchanged", agent_key_pair: "unchanged"}
    assert await repo.get_max_version_number(first[skill_key].listing_id) == 1
    assert await repo.get_max_version_number(first[agent_key_pair].listing_id) == 1

    # 3. Skill 内容变了：Skill 条目新增版本 2；内嵌它的 Agent 快照也变了，同样新增版本 2。
    _write_skill(root, skill_name, "第二版正文")
    third = await _sync(pg, root)

    assert _actions(third) == {skill_key: "updated", agent_key_pair: "updated"}
    assert third[skill_key].version_number == 2
    assert third[agent_key_pair].version_number == 2
    agent_v2 = await repo.get_version_by_number(first[agent_key_pair].listing_id, 2)
    assert agent_v2 is not None
    assert "第二版正文" in agent_v2.snapshot["skills"][0]["content"]

    # 4. 代码里删除 Agent：条目改为 removed 并写明原因；Skill 不受影响。
    shutil.rmtree(root / "agents" / agent_key)
    fourth = await _sync(pg, root)

    assert _actions(fourth) == {skill_key: "unchanged", agent_key_pair: "removed"}
    removed = await repo.get_listing(first[agent_key_pair].listing_id)
    assert removed is not None
    assert (removed.status, removed.status_reason) == ("removed", REMOVED_REASON)
    # 已经是 removed 的，再同步一次不重复处理。
    assert agent_key_pair not in await _sync(pg, root)

    # 5. 原样加回来：内容没变，不新增版本，只恢复上架。
    _write_agent(root, agent_key, skill_name)
    fifth = await _sync(pg, root)

    assert _actions(fifth) == {skill_key: "unchanged", agent_key_pair: "restored"}
    restored = await repo.get_listing(first[agent_key_pair].listing_id)
    assert restored is not None
    assert (restored.status, restored.status_reason) == ("listed", None)
    assert restored.current_version_number == 2
    assert await repo.get_max_version_number(restored.id) == 2

    # 6. 删除后改了内容再加回来：新增版本并重新上架。
    shutil.rmtree(root / "agents" / agent_key)
    await _sync(pg, root)
    _write_agent(root, agent_key, skill_name, prompt="你是第三版纪要助手。")
    sixth = await _sync(pg, root)

    assert _actions(sixth) == {skill_key: "unchanged", agent_key_pair: "updated"}
    relisted = await repo.get_listing(first[agent_key_pair].listing_id)
    assert relisted is not None
    assert (relisted.status, relisted.current_version_number) == ("listed", 3)


async def test_sync_rejects_unbindable_tool_without_writing(pg, tmp_path: Path) -> None:
    root = tmp_path / "presets"
    agent_key = f"t{pg.suffix}-shell"
    agent_dir = root / "agents" / agent_key
    agent_dir.mkdir(parents=True)
    text = (
        "---\nname: 越权助手\ndescription: 引用了不能绑定的工具\n"
        "tools: [Read, Bash, NoSuchTool]\n---\n\n你是越权助手。\n"
    )
    (agent_dir / "AGENT.md").write_text(text, encoding="utf-8")

    with pytest.raises(AgentException) as exc_info:
        async with pg.db.begin_nested():
            await sync_official_presets(pg.db, root)

    message = exc_info.value.message
    assert f"agents/{agent_key}" in message
    assert "tools 字段" in message
    assert "Bash（自建 Agent 不允许绑定该工具）" in message
    assert "NoSuchTool（工具不存在）" in message
    assert await MarketRepository(pg.db).get_official_listing_for_update("agent", agent_key) is None


async def test_agent_preset_snapshot_matches_community_snapshot(pg, tmp_path: Path) -> None:
    """同样的内容，预置生成的 Agent 快照与 AgentService.build_snapshot 完全一致，哈希也一致。"""
    root = tmp_path / "presets"
    skill_name = f"t{pg.suffix}-notes"
    agent_key = f"t{pg.suffix}-helper"
    _write_skill(root, skill_name, "正文")
    _write_agent(root, agent_key, skill_name)
    presets = {(preset.kind, preset.key): preset for preset in load_presets(root)}
    preset_snapshot = presets[("agent", agent_key)].snapshot

    # 在工作区里用社区方式建一份同样内容的 Skill 和 Agent。
    skill_snapshot = presets[("skill", skill_name)].snapshot["skill"]
    bundle = SkillBundle.from_content(skill_snapshot["content"], skill_snapshot["extra_files"])
    skill = await SkillService(pg.db).create_from_bundle(
        pg.workspace_id, pg.user_id, build_skill_zip(bundle)
    )
    agent_snapshot = preset_snapshot["agent"]
    agent = await AgentService(pg.db).create_agent(
        workspace_id=pg.workspace_id,
        actor_user_id=pg.user_id,
        name=agent_snapshot["name"],
        description=agent_snapshot["description"],
        system_prompt=agent_snapshot["system_prompt"],
        model_name=None,
        is_enabled=True,
        tool_ids=[pg.tool_ids["Read"], pg.tool_ids["Grep"]],
        skill_ids=[skill.id],
    )

    community_snapshot = await AgentService(pg.db).build_snapshot(pg.workspace_id, agent.id)

    assert community_snapshot == preset_snapshot
    assert compute_snapshot_hash(community_snapshot) == compute_snapshot_hash(preset_snapshot)
