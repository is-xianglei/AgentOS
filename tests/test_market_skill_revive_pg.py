"""Skill 删除后被同名新建「原地复活」时，市场的安装关联不能跟着复活：真实 PostgreSQL 集成测试。

skill 域新建同名 Skill 时会把软删行原地复活、沿用原 ID，而市场按 ID 记录安装对象和托管 Skill。
修复后 Skill 删除时由 market 域清除指向它的安装关联。这里覆盖：
1. 已复现：Skill 条目装出来的 Skill 删除后，安装 Agent 条目恢复了同一个 ID，Skill 条目不能变回已安装；
2. 用户删掉托管 Skill 后手动新建同名 Skill：不再算市场托管，可以上架，升级因同名冲突被拒、内容不变；
3. Skill 条目装完删掉再重装：照常安装，并复用原来的安装记录行；
4. 删除只清除这一个 Skill 的关联：其余托管 Skill 保留，已软删除的安装记录也处理，别的工作区不受影响；
5. 迁移 0029 的存量数据修复：在本用例的外层事务里执行 upgrade()，随用例结束一起回滚。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库，整个用例包在外层事务里，结束时回滚，不留数据。
测试数据都带唯一后缀，浏览时用后缀作关键词，避开库里已有的条目。库不可达时跳过并给出原因。
"""

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.service import AgentService
from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.config import settings
from market.models import MarketInstallRecord
from market.schemas import MarketBrowseItem, MarketInstallResult
from market.service import MarketService
from skill.models import SkillRecord
from skill.parser import SkillBundle
from skill.repository import SkillRepository
from skill.service import SkillService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0029_market_stale_skill_links.py"
)

# 用户键 -> (所在工作区键, 角色)；平台管理员不属于任何工作区，只负责审核。
_MEMBERSHIPS = {
    "author": ("publisher", "owner"),
    "installer": ("installer", "owner"),
    "second_installer": ("second", "owner"),
}
_WORKSPACE_KEYS = ("publisher", "installer", "second")


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

    users: dict[str, UserRecord] = {}
    for user_key in [*_MEMBERSHIPS, "platform_admin"]:
        users[user_key] = UserRecord(
            email=f"mkr-{user_key}-{suffix}@test.local",
            username=f"mkr-{user_key}-{suffix}",
            password="x",
            is_platform_admin=user_key == "platform_admin",
        )
    workspaces: dict[str, WorkspaceRecord] = {}
    for workspace_key in _WORKSPACE_KEYS:
        workspaces[workspace_key] = WorkspaceRecord(
            name=f"mkr-{workspace_key}-{suffix}",
            slug=f"mkr-{workspace_key}-{suffix}",
            display_name=f"复活测试{workspace_key}{suffix}",
        )
    session.add_all([*users.values(), *workspaces.values()])
    await session.flush()
    now = datetime.now(UTC)
    for user_key, (workspace_key, role) in _MEMBERSHIPS.items():
        session.add(
            WorkspaceMemberRecord(
                workspace_id=workspaces[workspace_key].id,
                user_id=users[user_key].id,
                role=role,
                joined_at=now,
            )
        )
    await session.flush()
    try:
        yield SimpleNamespace(
            db=session,
            suffix=suffix,
            author_id=users["author"].id,
            installer_id=users["installer"].id,
            second_installer_id=users["second_installer"].id,
            platform_admin_id=users["platform_admin"].id,
            publisher_ws=workspaces["publisher"].id,
            installer_ws=workspaces["installer"].id,
            second_ws=workspaces["second"].id,
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture
async def api(pg):
    """按给定的当前用户和工作区构造客户端；请求共用外层事务里的会话，不 commit。"""
    from main import create_app

    async def test_db():
        yield pg.db

    clients: list[httpx.AsyncClient] = []

    def build(user_id: int, workspace_id: int) -> httpx.AsyncClient:
        # 成员身份和角色仍由 Service 对着真实数据校验，这里只跳过 Token 解析。
        app = create_app()
        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
            id=user_id, is_platform_admin=False
        )
        app.dependency_overrides[get_current_workspace_id] = lambda: workspace_id
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(client)
        return client

    yield build
    for client in clients:
        await client.aclose()


# ---------- 准备数据 ----------


async def _save_skill(env: SimpleNamespace, name: str) -> SkillRecord:
    """在发布工作区直接建一个 Skill，作为上架的源对象，或者作为 Agent 自带的 Skill。"""
    content = f"---\nname: {name}\ndescription: {name} 的描述\n---\n\n作者写的正文\n"
    bundle = SkillBundle.from_content(content, {})
    return await SkillRepository(env.db).save(
        None,
        workspace_id=env.publisher_ws,
        name=bundle.name,
        description=bundle.description,
        frontmatter=bundle.parsed.frontmatter,
        version=bundle.version,
        skill_hash=bundle.skill_hash,
        content=bundle.content,
        extra_files=bundle.extra_files,
        actor_user_id=env.author_id,
    )


async def _create_agent(env: SimpleNamespace, name: str, skill_ids: list[int]) -> int:
    """在发布工作区建一个不绑工具的 Agent，返回 Agent ID。"""
    agent = await AgentService(env.db).create_agent(
        workspace_id=env.publisher_ws,
        actor_user_id=env.author_id,
        name=name,
        description=f"{name}的描述",
        system_prompt=f"你是{name}",
        model_name=None,
        is_enabled=True,
        tool_ids=[],
        skill_ids=skill_ids,
    )
    return agent.id


async def _publish(env: SimpleNamespace, kind: str, source_id: int) -> int:
    """以发布工作区的 owner 提交并审核通过，返回条目 ID。同一个源对象再调用一次就是发布新版本。"""
    service = MarketService(env.db)
    version = await service.submit(
        env.publisher_ws,
        env.author_id,
        kind=kind,
        source_id=source_id,
        category="data",
        release_note=None,
    )
    await service.approve_version(env.platform_admin_id, version.id, None)
    return version.listing_id


async def _install(
    env: SimpleNamespace,
    listing_id: int,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
) -> MarketInstallResult:
    """安装条目；默认以安装工作区的 owner 装到安装工作区。"""
    if workspace_id is None:
        workspace_id = env.installer_ws
        user_id = env.installer_id
    return await MarketService(env.db).install_listing(workspace_id, user_id, listing_id)


async def _delete_skill(env: SimpleNamespace, skill_id: int) -> None:
    """以安装工作区的 owner 走 SkillService 删除 Skill，与删除接口的路径相同。"""
    deleted = await SkillService(env.db).delete_skill(env.installer_ws, skill_id, env.installer_id)
    assert deleted is True


async def _browse_items(env: SimpleNamespace) -> dict[int, MarketBrowseItem]:
    """以安装工作区的 owner 浏览，关键词固定为本用例的后缀，只看得到本用例建的条目。"""
    page = await MarketService(env.db).list_listings(
        env.installer_ws,
        env.installer_id,
        kind=None,
        category=None,
        keyword=env.suffix,
        limit=100,
        offset=0,
    )
    items: dict[int, MarketBrowseItem] = {}
    for item in page.items:
        items[item.id] = item
    return items


async def _load_install(env: SimpleNamespace, install_id: int) -> MarketInstallRecord:
    """按 ID 重新读安装记录，已软删除的也能读到；populate_existing 保证读到的是库里的最新值。"""
    stmt = (
        select(MarketInstallRecord)
        .where(MarketInstallRecord.id == install_id)
        .execution_options(include_deleted=True, populate_existing=True)
    )
    return (await env.db.scalars(stmt)).one()


async def _by_target(client: httpx.AsyncClient, kind: str, target_id: int) -> httpx.Response:
    return await client.get(
        "/api/market/installs/by-target",
        params={"kind": kind, "target_id": target_id},
    )


# ---------- 1. 已复现：恢复同一个 Skill ID 不能让旧的 Skill 条目安装跟着复活 ----------


async def test_revived_skill_does_not_bring_back_old_skill_install(pg, api) -> None:
    agent_name = f"复活助手{pg.suffix}"
    source_skill = await _save_skill(pg, f"revive-{pg.suffix}")
    skill_listing = await _publish(pg, "skill", source_skill.id)
    # Agent 自带的 Skill 就是上面的源 Skill，所以和 Skill 条目同名同内容。
    agent_id = await _create_agent(pg, agent_name, [source_skill.id])
    agent_listing = await _publish(pg, "agent", agent_id)

    # 1. 安装 Skill 条目，再删掉装出来的 Skill：Skill 条目变回未安装。
    skill_install = await _install(pg, skill_listing)
    skill_id = skill_install.install.target_skill_id
    await _delete_skill(pg, skill_id)
    items = await _browse_items(pg)
    assert (items[skill_listing].installed, items[skill_listing].install_count) == (False, 0)

    # 2. 安装 Agent 条目：同名的软删 Skill 被原地恢复，沿用原来的 ID，记为托管 Skill。
    agent_install = await _install(pg, agent_listing)
    assert agent_install.report.skills[0].action == "restore"
    assert agent_install.report.skills[0].skill_id == skill_id
    assert agent_install.install.managed_skill_ids == [skill_id]

    # 3. Skill 条目仍是未安装，安装次数没有多算；旧安装记录不再指向这个 Skill。
    items = await _browse_items(pg)
    assert (items[skill_listing].installed, items[skill_listing].install_count) == (False, 0)
    assert (items[agent_listing].installed, items[agent_listing].install_count) == (True, 1)
    old_install = await _load_install(pg, skill_install.install.id)
    assert old_install.target_skill_id is None

    # 4. 按安装对象查：这个 Skill 是随 Agent 装来的，指向 Agent 条目。
    response = await _by_target(api(pg.installer_id, pg.installer_ws), "skill", skill_id)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "listing_id": agent_listing,
        "listing_name": agent_name,
        "listing_status": "listed",
        "installed_version_number": 1,
        "installed_with_agent": True,
    }


# ---------- 2. 用户删掉托管 Skill 后手动新建同名 Skill ----------


@pytest.fixture
async def recreated_skill(pg, api):
    """安装 Agent 条目后，用户删掉随它装来的托管 Skill，再用 skill 接口手动新建同名但内容不同的 Skill。"""
    skill_name = f"own-{pg.suffix}"
    bundled = await _save_skill(pg, skill_name)
    agent_id = await _create_agent(pg, f"托管助手{pg.suffix}", [bundled.id])
    agent_listing = await _publish(pg, "agent", agent_id)
    agent_install = await _install(pg, agent_listing)
    managed_skill_id = agent_install.install.managed_skill_ids[0]

    client = api(pg.installer_id, pg.installer_ws)
    deleted = await client.delete(f"/api/skills/{managed_skill_id}")
    created = await client.post(
        "/api/skills",
        json={"name": skill_name, "description": "用户自己写的描述", "body": "用户自己写的正文"},
    )

    assert deleted.status_code == 200, deleted.text
    assert created.status_code == 200, created.text
    # 同名软删行被原地复活，ID 和原来的托管 Skill 一样。
    assert created.json()["data"]["id"] == managed_skill_id
    return SimpleNamespace(
        name=skill_name,
        agent_id=agent_id,
        agent_listing=agent_listing,
        install_id=agent_install.install.id,
        skill_id=managed_skill_id,
        client=client,
    )


async def test_recreated_skill_is_not_market_managed(pg, recreated_skill) -> None:
    case = recreated_skill

    by_target = await _by_target(case.client, "skill", case.skill_id)
    submitted = await case.client.post(
        "/api/market/submissions",
        json={"kind": "skill", "source_id": case.skill_id, "category": "data"},
    )

    # 托管关联在删除时已经清掉，复活后的 Skill 是用户自己的：查不到来源，也能正常提交上架。
    assert by_target.status_code == 200, by_target.text
    assert by_target.json()["data"] is None
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "pending"
    install = await _load_install(pg, case.install_id)
    assert install.managed_skill_ids == []


async def test_upgrade_does_not_overwrite_recreated_skill(pg, recreated_skill) -> None:
    case = recreated_skill
    skill_service = SkillService(pg.db)
    snapshot_before = await skill_service.build_snapshot(pg.installer_ws, case.skill_id)

    # 作者改了 Agent 的系统提示词，发布第二版；第二版自带的 Skill 仍是作者写的内容。
    await AgentService(pg.db).update_agent(
        pg.publisher_ws,
        case.agent_id,
        pg.author_id,
        {"system_prompt": "第二版提示词"},
    )
    await _publish(pg, "agent", case.agent_id)
    upgraded = await case.client.post(f"/api/market/listings/{case.agent_listing}/upgrade")

    # 用户的 Skill 不再算托管，按安装规则判定为同名但内容不同，整个升级被拒，用户内容不变。
    assert upgraded.status_code == 409, upgraded.text
    assert upgraded.json()["error"]["message"] == (
        f"本工作区已有同名但内容不同的 Skill: {case.name}"
    )
    snapshot_after = await skill_service.build_snapshot(pg.installer_ws, case.skill_id)
    assert snapshot_after == snapshot_before
    install = await _load_install(pg, case.install_id)
    assert install.installed_version_number == 1


# ---------- 3. 删除后重装 ----------


async def test_reinstall_after_delete_reuses_install_row(pg) -> None:
    source_skill = await _save_skill(pg, f"reinstall-{pg.suffix}")
    listing_id = await _publish(pg, "skill", source_skill.id)
    first = await _install(pg, listing_id)
    skill_id = first.install.target_skill_id

    await _delete_skill(pg, skill_id)
    # 安装记录还在，只是不再指向被删的 Skill，视为未安装。
    install = await _load_install(pg, first.install.id)
    assert install.target_skill_id is None
    assert (await _browse_items(pg))[listing_id].installed is False

    second = await _install(pg, listing_id)

    # 重新安装复用原来的安装记录行；同名软删 Skill 原地恢复，沿用原来的 ID。
    assert second.install.id == first.install.id
    assert second.install.target_skill_id == skill_id
    assert second.report.skills[0].action == "restore"
    item = (await _browse_items(pg))[listing_id]
    assert (item.installed, item.install_count) == (True, 1)


# ---------- 4. 删除只清除这一个 Skill 的关联 ----------


async def _add_deleted_install(
    env: SimpleNamespace,
    listing_id: int,
    *,
    target_skill_id: int | None,
    managed_skill_ids: list[int],
) -> int:
    """在安装工作区直接写一条已软删除的旧安装记录，返回它的 ID；部分唯一索引不约束已删除的行。"""
    record = MarketInstallRecord(
        listing_id=listing_id,
        workspace_id=env.installer_ws,
        installed_version_number=1,
        target_skill_id=target_skill_id,
        managed_skill_ids=managed_skill_ids,
        installed_by_user_id=env.installer_id,
        is_deleted=True,
        deleted_at=datetime.now(UTC),
    )
    env.db.add(record)
    await env.db.flush()
    return record.id


async def test_delete_clears_only_this_skill_in_its_workspace(pg) -> None:
    first_skill = await _save_skill(pg, f"first-{pg.suffix}")
    second_skill = await _save_skill(pg, f"second-{pg.suffix}")
    agent_id = await _create_agent(pg, f"双技能助手{pg.suffix}", [first_skill.id, second_skill.id])
    agent_listing = await _publish(pg, "agent", agent_id)
    installed = await _install(pg, agent_listing)
    other_workspace = await _install(
        pg, agent_listing, workspace_id=pg.second_ws, user_id=pg.second_installer_id
    )
    managed_ids = list(installed.install.managed_skill_ids)
    assert len(managed_ids) == 2
    deleted_id = managed_ids[0]
    kept_id = managed_ids[1]
    stale_id = await _add_deleted_install(
        pg, agent_listing, target_skill_id=deleted_id, managed_skill_ids=[deleted_id, kept_id]
    )

    await _delete_skill(pg, deleted_id)

    # 有效的和已软删除的安装记录都只去掉被删的那个 ID，其余托管 Skill 保留。
    active = await _load_install(pg, installed.install.id)
    stale = await _load_install(pg, stale_id)
    assert active.managed_skill_ids == [kept_id]
    assert (stale.target_skill_id, stale.managed_skill_ids) == (None, [kept_id])
    # 别的工作区装出来的是另一组 Skill，不受影响。
    other = await _load_install(pg, other_workspace.install.id)
    assert other.managed_skill_ids == list(other_workspace.install.managed_skill_ids)
    assert len(other.managed_skill_ids) == 2


# ---------- 5. 迁移 0029 的存量数据修复 ----------


def _load_migration() -> ModuleType:
    """按文件路径加载迁移脚本：文件名以数字开头，不能用普通 import。"""
    spec = importlib.util.spec_from_file_location("migration_0029", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_migration_upgrade(env: SimpleNamespace) -> None:
    """在本用例的外层事务里执行迁移的 upgrade()，改动随用例结束一起回滚。"""
    migration = _load_migration()

    def upgrade(sync_connection: Connection) -> None:
        context = MigrationContext.configure(sync_connection)
        with Operations.context(context):
            migration.upgrade()

    await env.db.flush()
    connection = await env.db.connection()
    await connection.run_sync(upgrade)
    # 迁移绕过 ORM 直接改库，会话里已加载的对象作废，之后重新读。
    env.db.expire_all()


async def test_migration_clears_links_to_soft_deleted_skills(pg) -> None:
    solo_skill = await _save_skill(pg, f"legacy-solo-{pg.suffix}")
    skill_listing = await _publish(pg, "skill", solo_skill.id)
    first_skill = await _save_skill(pg, f"legacy-first-{pg.suffix}")
    second_skill = await _save_skill(pg, f"legacy-second-{pg.suffix}")
    agent_id = await _create_agent(pg, f"存量助手{pg.suffix}", [first_skill.id, second_skill.id])
    agent_listing = await _publish(pg, "agent", agent_id)
    skill_install = await _install(pg, skill_listing)
    agent_install = await _install(pg, agent_listing)
    other_workspace = await _install(
        pg, agent_listing, workspace_id=pg.second_ws, user_id=pg.second_installer_id
    )
    managed_ids = list(agent_install.install.managed_skill_ids)
    deleted_managed_id = managed_ids[0]
    kept_managed_id = managed_ids[1]
    stale_id = await _add_deleted_install(
        pg,
        agent_listing,
        target_skill_id=deleted_managed_id,
        managed_skill_ids=[deleted_managed_id, kept_managed_id],
    )
    # 修复前删除 Skill 不会通知市场：这里绕过 SkillService 直接软删，造出同样的存量旧关联。
    skill_repo = SkillRepository(pg.db)
    for skill_id in (skill_install.install.target_skill_id, deleted_managed_id):
        record = await skill_repo.get_by_id(pg.installer_ws, skill_id)
        assert record is not None
        await skill_repo.soft_delete(record)

    await _run_migration_upgrade(pg)

    # 指向已软删除 Skill 的目标置空、托管 ID 移除，仍存在的托管 Skill 保留；已软删除的安装记录同样修复。
    skill_row = await _load_install(pg, skill_install.install.id)
    agent_row = await _load_install(pg, agent_install.install.id)
    stale_row = await _load_install(pg, stale_id)
    other_row = await _load_install(pg, other_workspace.install.id)
    assert skill_row.target_skill_id is None
    assert (agent_row.target_agent_id, agent_row.managed_skill_ids) == (
        agent_install.install.target_agent_id,
        [kept_managed_id],
    )
    assert (stale_row.target_skill_id, stale_row.managed_skill_ids) == (None, [kept_managed_id])
    # 别的工作区装出来的 Skill 都还在，不受影响。
    assert other_row.managed_skill_ids == list(other_workspace.install.managed_skill_ids)
    # 修复后 Skill 条目视为未安装，Agent 条目仍是有效安装。
    items = await _browse_items(pg)
    assert items[skill_listing].installed is False
    assert items[agent_listing].installed is True

