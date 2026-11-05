"""市场在浏览器端到端验证中暴露的四个问题的真实 PostgreSQL 集成测试。

1. 版本响应带快照里的名称，「我的上架」不加载快照全文也能拿到，审核队列里也有；
2. installed=true 时返回作者下架、平台下架的已安装条目并带状态，其他筛选只返回已上架的；
   有有效安装的工作区能看非 listed 条目的详情，无关工作区 404，安装和升级照旧被拒；
3. 安装次数只算有效安装：装出来的对象被删、所在工作区被删都会让它减少；
4. 从市场装来的 Agent、托管 Skill、Skill 不能上架；按安装对象查条目的命中、未命中和参数校验。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库，整个用例包在外层事务里，结束时回滚，不留数据。
测试数据都带唯一后缀，浏览时用后缀作关键词，避开库里已有的条目。库不可达时跳过并给出原因。
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.service import AgentService
from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.config import settings
from core.errors import AgentException
from market.repository import MarketRepository
from market.schemas import MarketBrowseItem, MarketBrowsePage, MarketVersionResponse
from market.service import MarketService
from skill.models import SkillRecord
from skill.parser import SkillBundle
from skill.repository import SkillRepository
from skill.service import SkillService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord
from workspace.service import WorkspaceService

pytestmark = pytest.mark.anyio

# 用户键 -> (所在工作区键, 角色)；平台管理员不属于任何工作区，只负责审核。
_MEMBERSHIPS = {
    "author": ("publisher", "owner"),
    "installer": ("installer", "owner"),
    "member": ("installer", "member"),
    "second_installer": ("second", "owner"),
    "outsider": ("other", "owner"),
}
_WORKSPACE_KEYS = ("publisher", "installer", "second", "other")


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
            email=f"mkx-{user_key}-{suffix}@test.local",
            username=f"mkx-{user_key}-{suffix}",
            password="x",
            is_platform_admin=user_key == "platform_admin",
        )
    workspaces: dict[str, WorkspaceRecord] = {}
    for workspace_key in _WORKSPACE_KEYS:
        workspaces[workspace_key] = WorkspaceRecord(
            name=f"mkx-{workspace_key}-{suffix}",
            slug=f"mkx-{workspace_key}-{suffix}",
            display_name=f"修复测试{workspace_key}{suffix}",
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
            member_id=users["member"].id,
            second_installer_id=users["second_installer"].id,
            outsider_id=users["outsider"].id,
            platform_admin_id=users["platform_admin"].id,
            publisher_ws=workspaces["publisher"].id,
            installer_ws=workspaces["installer"].id,
            second_ws=workspaces["second"].id,
            other_ws=workspaces["other"].id,
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


async def _save_skill(
    env: SimpleNamespace,
    name: str,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
) -> SkillRecord:
    """直接建一个 Skill；默认建在发布工作区，作为上架的源对象。"""
    if workspace_id is None:
        workspace_id = env.publisher_ws
        user_id = env.author_id
    content = f"---\nname: {name}\ndescription: {name} 的描述\n---\n\n正文\n"
    bundle = SkillBundle.from_content(content, {})
    return await SkillRepository(env.db).save(
        None,
        workspace_id=workspace_id,
        name=bundle.name,
        description=bundle.description,
        frontmatter=bundle.parsed.frontmatter,
        version=bundle.version,
        skill_hash=bundle.skill_hash,
        content=bundle.content,
        extra_files=bundle.extra_files,
        actor_user_id=user_id,
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


async def _submit(
    env: SimpleNamespace,
    kind: str,
    source_id: int,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
):
    """提交上架申请；默认以发布工作区的 owner 提交。"""
    if workspace_id is None:
        workspace_id = env.publisher_ws
        user_id = env.author_id
    return await MarketService(env.db).submit(
        workspace_id,
        user_id,
        kind=kind,
        source_id=source_id,
        category="data",
        release_note=None,
    )


async def _publish(env: SimpleNamespace, kind: str, source_id: int) -> int:
    """提交并审核通过，返回条目 ID。同一个源对象再调用一次就是发布新版本。"""
    version = await _submit(env, kind, source_id)
    await MarketService(env.db).approve_version(env.platform_admin_id, version.id, None)
    return version.listing_id


async def _publish_new_skill_version(env: SimpleNamespace, skill_id: int) -> None:
    """改掉源 Skill 的正文再发布一次，条目的当前版本号加 1。"""
    await SkillService(env.db).update_from_fields(
        env.publisher_ws,
        skill_id,
        env.author_id,
        name=None,
        description="第二版描述",
        body="第二版正文",
        version=None,
        compatibility=None,
        metadata=None,
    )
    await _publish(env, "skill", skill_id)


async def _install(
    env: SimpleNamespace,
    listing_id: int,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
):
    """安装条目；默认装到安装工作区。"""
    if workspace_id is None:
        workspace_id = env.installer_ws
        user_id = env.installer_id
    return await MarketService(env.db).install_listing(workspace_id, user_id, listing_id)


async def _expect_error(
    env: SimpleNamespace,
    call: Callable[[], Awaitable[Any]],
) -> AgentException:
    """像请求事务一样执行：失败时回滚到保存点，这次调用写过的数据全部撤销。"""
    with pytest.raises(AgentException) as exc_info:
        async with env.db.begin_nested():
            await call()
    return exc_info.value


async def _browse(
    env: SimpleNamespace,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
    **filters: Any,
) -> MarketBrowsePage:
    """默认以安装工作区的 owner 浏览；关键词固定为本用例的后缀，只看得到本用例建的条目。"""
    if workspace_id is None:
        workspace_id = env.installer_ws
        user_id = env.installer_id
    query: dict[str, Any] = {"kind": None, "category": None, "keyword": env.suffix}
    query.update(filters)
    return await MarketService(env.db).list_listings(
        workspace_id, user_id, limit=100, offset=0, **query
    )


def _ids(page: MarketBrowsePage) -> set[int]:
    ids: set[int] = set()
    for item in page.items:
        ids.add(item.id)
    return ids


def _items_by_id(page: MarketBrowsePage) -> dict[int, MarketBrowseItem]:
    items: dict[int, MarketBrowseItem] = {}
    for item in page.items:
        items[item.id] = item
    return items


# ---------- 1. 版本响应带名称 ----------


async def test_version_name_comes_from_snapshot(pg, api) -> None:
    skill_name = f"named-{pg.suffix}"
    agent_name = f"命名助手{pg.suffix}"
    skill = await _save_skill(pg, skill_name)
    agent_id = await _create_agent(pg, agent_name, [])
    service = MarketService(pg.db)

    # Skill 版本提交后撤回，Agent 版本提交后被驳回：两个条目都没审核通过过，条目的 name 为空。
    skill_version = await _submit(pg, "skill", skill.id)
    await service.withdraw_version(pg.publisher_ws, pg.author_id, skill_version.id)
    agent_version = await _submit(pg, "agent", agent_id)
    await service.reject_version(pg.platform_admin_id, agent_version.id, "描述不清")
    skill_listing_id = skill_version.listing_id
    agent_listing_id = agent_version.listing_id

    # 提交、撤回、驳回接口返回的版本也带名称。
    assert MarketVersionResponse.model_validate(skill_version).name == skill_name
    assert MarketVersionResponse.model_validate(agent_version).name == agent_name

    # 从库里重新读版本列表：快照没有加载，名称照样在。
    pg.db.expunge_all()
    versions = await MarketRepository(pg.db).list_versions_by_listing_ids(
        [skill_listing_id, agent_listing_id]
    )
    assert len(versions) == 2
    for version in versions:
        assert "snapshot" in inspect(version).unloaded
        assert version.name in (skill_name, agent_name)

    client = api(pg.author_id, pg.publisher_ws)
    mine = await client.get("/api/market/mine")
    by_source = await client.get(
        "/api/market/mine/by-source",
        params={"kind": "agent", "source_id": agent_id},
    )

    assert mine.status_code == 200, mine.text
    mine_by_listing_id: dict[int, dict[str, Any]] = {}
    for item in mine.json()["data"]:
        mine_by_listing_id[item["listing"]["id"]] = item
    skill_item = mine_by_listing_id[skill_listing_id]
    assert skill_item["listing"]["name"] is None
    assert len(skill_item["versions"]) == 1
    assert skill_item["versions"][0]["status"] == "withdrawn"
    assert skill_item["versions"][0]["name"] == skill_name
    assert by_source.status_code == 200, by_source.text
    agent_data = by_source.json()["data"]
    assert agent_data["listing"]["name"] is None
    assert agent_data["versions"][0]["status"] == "rejected"
    assert agent_data["versions"][0]["name"] == agent_name

    # 审核队列里的版本详情（含快照全文）也带名称；驳回的按最近在前，本用例的在第一页。
    queue = await service.list_versions_for_review(status="rejected", limit=100, offset=0)
    queued_names: list[str] = []
    for review_item in queue.items:
        if review_item.version.listing_id == agent_listing_id:
            queued_names.append(review_item.version.name)
    assert queued_names == [agent_name]


# ---------- 2. 已安装的条目下架后 ----------


@pytest.fixture
async def installed_market(pg):
    """安装工作区装了三个 Skill 条目，之后作者、平台各下架一个；另有一个已上架没装过的，
    以及一个作者下架、只被第二个工作区装过但装出来的 Skill 已删除的。"""
    service = MarketService(pg.db)
    suffix = pg.suffix

    # 已上架：装了 v1 之后作者发布了 v2，已安装且有更新。
    outdated_skill = await _save_skill(pg, f"outdated-{suffix}")
    outdated = await _publish(pg, "skill", outdated_skill.id)
    await _install(pg, outdated)
    await _publish_new_skill_version(pg, outdated_skill.id)

    # 已上架，没装过。
    fresh_skill = await _save_skill(pg, f"fresh-{suffix}")
    fresh = await _publish(pg, "skill", fresh_skill.id)

    # 作者下架：装了 v1、作者发布 v2 之后下架。有更高的版本，但下架了不能升级。
    delisted_skill = await _save_skill(pg, f"delisted-{suffix}")
    delisted = await _publish(pg, "skill", delisted_skill.id)
    await _install(pg, delisted)
    await _publish_new_skill_version(pg, delisted_skill.id)
    await service.delist_listing(pg.publisher_ws, pg.author_id, delisted)

    # 平台下架：装了之后被平台管理员强制下架。
    removed_skill = await _save_skill(pg, f"removed-{suffix}")
    removed = await _publish(pg, "skill", removed_skill.id)
    await _install(pg, removed)
    await service.remove_listing(pg.platform_admin_id, removed, "含违规内容")

    # 作者下架：安装工作区没装过；第二个工作区装过，但装出来的 Skill 已删除，不算有效安装。
    hidden_skill = await _save_skill(pg, f"hidden-{suffix}")
    hidden = await _publish(pg, "skill", hidden_skill.id)
    hidden_install = await _install(
        pg, hidden, workspace_id=pg.second_ws, user_id=pg.second_installer_id
    )
    await SkillService(pg.db).delete_skill(
        pg.second_ws, hidden_install.install.target_skill_id, pg.second_installer_id
    )
    await service.delist_listing(pg.publisher_ws, pg.author_id, hidden)

    return SimpleNamespace(
        outdated=outdated,
        fresh=fresh,
        delisted=delisted,
        removed=removed,
        hidden=hidden,
    )


async def test_installed_filter_returns_non_listed_installs_with_status(
    pg, installed_market
) -> None:
    market = installed_market

    installed = await _browse(pg, installed=True)

    items = _items_by_id(installed)
    assert set(items) == {market.outdated, market.delisted, market.removed}
    assert installed.total == 3
    outdated = items[market.outdated]
    delisted = items[market.delisted]
    removed = items[market.removed]
    assert (outdated.status, outdated.status_reason) == ("listed", None)
    assert (delisted.status, delisted.status_reason) == ("delisted", None)
    assert (removed.status, removed.status_reason) == ("removed", "含违规内容")
    for item in installed.items:
        assert item.installed is True
    assert outdated.has_update is True
    # 下架的条目有更高的版本，但不能升级，所以不算有更新。
    assert (delisted.installed_version_number, delisted.current_version_number) == (1, 2)
    assert delisted.has_update is False


async def test_other_filters_return_only_listed(pg, installed_market) -> None:
    market = installed_market

    everything = await _browse(pg)
    not_installed = await _browse(pg, installed=False)
    with_update = await _browse(pg, has_update=True)
    without_update = await _browse(pg, has_update=False)
    installed_with_update = await _browse(pg, installed=True, has_update=True)
    installed_without_update = await _browse(pg, installed=True, has_update=False)

    assert (_ids(everything), everything.total) == ({market.outdated, market.fresh}, 2)
    assert _ids(not_installed) == {market.fresh}
    assert _ids(with_update) == {market.outdated}
    assert _ids(without_update) == {market.fresh}
    for page in (everything, not_installed, with_update, without_update):
        for item in page.items:
            assert item.status == "listed"
    # installed 和 has_update 同时传时取交集：has_update=true 只会命中已上架的条目。
    assert _ids(installed_with_update) == {market.outdated}
    # has_update=false 排除能升级的那个；下架的条目不能升级，仍在「已安装」的结果里。
    assert _ids(installed_without_update) == {market.delisted, market.removed}


async def test_installed_workspace_sees_non_listed_detail_but_cannot_install(
    pg, installed_market
) -> None:
    market = installed_market
    service = MarketService(pg.db)

    delisted = await service.get_listing_detail(
        pg.installer_ws, pg.installer_id, market.delisted, is_platform_admin=False
    )
    removed = await service.get_listing_detail(
        pg.installer_ws, pg.member_id, market.removed, is_platform_admin=False
    )

    assert (delisted.status, delisted.listing.status) == ("delisted", "delisted")
    assert (removed.status, removed.status_reason) == ("removed", "含违规内容")
    assert (removed.listing.status, removed.listing.status_reason) == ("removed", "含违规内容")
    for detail in (delisted, removed):
        assert detail.current_version is not None
        # 不是 listed 的条目不能安装也不能升级，没有预览，与原来发布方看到的一致。
        assert detail.install_preview is None
        assert detail.listing.installed is True
        assert detail.listing.has_update is False
    assert delisted.current_version.name == f"delisted-{pg.suffix}"

    # 与条目无关的工作区看不到；装出来的 Skill 已删除的工作区不算有效安装，也看不到。
    outsider = await _expect_error(
        pg,
        lambda: service.get_listing_detail(
            pg.other_ws, pg.outsider_id, market.delisted, is_platform_admin=False
        ),
    )
    target_deleted = await _expect_error(
        pg,
        lambda: service.get_listing_detail(
            pg.second_ws, pg.second_installer_id, market.hidden, is_platform_admin=False
        ),
    )
    assert outsider.status_code == 404
    assert target_deleted.status_code == 404

    # 能看到详情，但安装和升级照旧被拒。
    install_delisted = await _expect_error(
        pg, lambda: service.install_listing(pg.installer_ws, pg.installer_id, market.delisted)
    )
    upgrade_delisted = await _expect_error(
        pg, lambda: service.upgrade_listing(pg.installer_ws, pg.installer_id, market.delisted)
    )
    install_removed = await _expect_error(
        pg, lambda: service.install_listing(pg.installer_ws, pg.installer_id, market.removed)
    )
    upgrade_removed = await _expect_error(
        pg, lambda: service.upgrade_listing(pg.installer_ws, pg.installer_id, market.removed)
    )
    for error in (install_delisted, upgrade_delisted, install_removed, upgrade_removed):
        assert error.status_code == 409
        assert error.message == "条目未上架，不能安装或升级"


async def test_installed_listing_over_http(pg, installed_market, api) -> None:
    market = installed_market
    installer = api(pg.installer_id, pg.installer_ws)
    outsider = api(pg.outsider_id, pg.other_ws)

    page = await installer.get(
        "/api/market/listings",
        params={"keyword": pg.suffix, "installed": "true"},
    )
    detail = await installer.get(f"/api/market/listings/{market.removed}")
    hidden = await outsider.get(f"/api/market/listings/{market.removed}")
    install = await installer.post(f"/api/market/listings/{market.removed}/install")
    upgrade = await installer.post(f"/api/market/listings/{market.delisted}/upgrade")

    assert page.status_code == 200, page.text
    statuses: dict[int, tuple[str, str | None]] = {}
    for item in page.json()["data"]["items"]:
        statuses[item["id"]] = (item["status"], item["status_reason"])
    assert statuses == {
        market.outdated: ("listed", None),
        market.delisted: ("delisted", None),
        market.removed: ("removed", "含违规内容"),
    }
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert (data["status"], data["status_reason"]) == ("removed", "含违规内容")
    assert data["listing"]["status"] == "removed"
    assert data["install_preview"] is None
    assert data["current_version"]["name"] == f"removed-{pg.suffix}"
    assert hidden.status_code == 404, hidden.text
    assert install.status_code == 409, install.text
    assert upgrade.status_code == 409, upgrade.text


# ---------- 3. 安装次数只算有效安装 ----------


async def test_install_count_drops_when_target_or_workspace_deleted(pg) -> None:
    suffix = pg.suffix
    skill = await _save_skill(pg, f"counted-{suffix}")
    bundled = await _save_skill(pg, f"counted-bundled-{suffix}")
    agent_id = await _create_agent(pg, f"计数助手{suffix}", [bundled.id])
    skill_listing = await _publish(pg, "skill", skill.id)
    agent_listing = await _publish(pg, "agent", agent_id)
    installer_skill = await _install(pg, skill_listing)
    installer_agent = await _install(pg, agent_listing)
    await _install(pg, skill_listing, workspace_id=pg.second_ws, user_id=pg.second_installer_id)
    await _install(pg, agent_listing, workspace_id=pg.second_ws, user_id=pg.second_installer_id)
    service = MarketService(pg.db)

    async def install_counts() -> dict[int, int]:
        """以与两个安装工作区都无关的工作区浏览；详情里的安装次数必须与列表一致。"""
        page = await _browse(pg, workspace_id=pg.other_ws, user_id=pg.outsider_id)
        counts: dict[int, int] = {}
        for item in page.items:
            counts[item.id] = item.install_count
            detail = await service.get_listing_detail(
                pg.other_ws, pg.outsider_id, item.id, is_platform_admin=False
            )
            assert detail.listing.install_count == item.install_count
        return counts

    assert await install_counts() == {skill_listing: 2, agent_listing: 2}

    # 删掉安装工作区里装出来的 Skill 和 Agent：这两条安装不再算。
    await SkillService(pg.db).delete_skill(
        pg.installer_ws, installer_skill.install.target_skill_id, pg.installer_id
    )
    await AgentService(pg.db).delete_agent(
        pg.installer_ws, installer_agent.install.target_agent_id, pg.installer_id
    )
    assert await install_counts() == {skill_listing: 1, agent_listing: 1}

    # 删掉第二个安装工作区：里面装出来的对象还在，但工作区没了，安装也不再算。
    await WorkspaceService(pg.db).delete_workspace(pg.second_ws, pg.second_installer_id)
    assert await install_counts() == {skill_listing: 0, agent_listing: 0}


# ---------- 4. 从市场装来的内容不能上架 ----------


@pytest.fixture
async def installed_copies(pg):
    """安装工作区装了一个带 Skill 的 Agent 条目和一个 Skill 条目，另有一个自己建的 Skill。"""
    suffix = pg.suffix
    bundled = await _save_skill(pg, f"bundled-{suffix}")
    agent_id = await _create_agent(pg, f"来源助手{suffix}", [bundled.id])
    agent_listing = await _publish(pg, "agent", agent_id)
    solo = await _save_skill(pg, f"solo-{suffix}")
    skill_listing = await _publish(pg, "skill", solo.id)
    agent_install = await _install(pg, agent_listing)
    skill_install = await _install(pg, skill_listing)
    # Agent 自带的 Skill 在安装工作区是新建的，记为托管 Skill。
    managed_skill_ids = agent_install.install.managed_skill_ids
    assert len(managed_skill_ids) == 1
    own_skill = await _save_skill(
        pg, f"own-{suffix}", workspace_id=pg.installer_ws, user_id=pg.installer_id
    )
    return SimpleNamespace(
        agent_listing=agent_listing,
        skill_listing=skill_listing,
        agent_id=agent_install.install.target_agent_id,
        managed_skill_id=managed_skill_ids[0],
        skill_id=skill_install.install.target_skill_id,
        own_skill_id=own_skill.id,
    )


async def _submit_from_installer(env: SimpleNamespace, kind: str, source_id: int):
    return await _submit(
        env, kind, source_id, workspace_id=env.installer_ws, user_id=env.installer_id
    )


async def test_submit_rejects_copies_installed_from_market(pg, installed_copies) -> None:
    copies = installed_copies

    agent_error = await _expect_error(
        pg, lambda: _submit_from_installer(pg, "agent", copies.agent_id)
    )
    managed_error = await _expect_error(
        pg, lambda: _submit_from_installer(pg, "skill", copies.managed_skill_id)
    )
    skill_error = await _expect_error(
        pg, lambda: _submit_from_installer(pg, "skill", copies.skill_id)
    )

    expected = [
        (agent_error, copies.agent_listing),
        (managed_error, copies.agent_listing),
        (skill_error, copies.skill_listing),
    ]
    for error, listing_id in expected:
        assert error.status_code == 409
        assert error.message == "从市场安装的内容不能上架"
        assert error.details == {"listing_id": listing_id}
    # 被拒的提交没有留下条目。
    repo = MarketRepository(pg.db)
    assert await repo.get_listing_by_source_for_update("agent", copies.agent_id) is None
    assert await repo.get_listing_by_source_for_update("skill", copies.managed_skill_id) is None
    assert await repo.get_listing_by_source_for_update("skill", copies.skill_id) is None

    # 自己建的 Skill 照常可以上架。
    own_version = await _submit_from_installer(pg, "skill", copies.own_skill_id)
    assert own_version.status == "pending"


async def test_submit_installed_copy_over_http_is_409(pg, installed_copies, api) -> None:
    client = api(pg.installer_id, pg.installer_ws)

    response = await client.post(
        "/api/market/submissions",
        json={"kind": "agent", "source_id": installed_copies.agent_id, "category": "data"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["data"] is None
    assert response.json()["error"]["message"] == "从市场安装的内容不能上架"


async def _by_target(client: httpx.AsyncClient, kind: str, target_id: int) -> httpx.Response:
    return await client.get(
        "/api/market/installs/by-target",
        params={"kind": kind, "target_id": target_id},
    )


async def test_by_target_hits_installed_copies(pg, installed_copies, api) -> None:
    copies = installed_copies
    # 普通成员也能查：只要求是当前工作区的有效成员。
    client = api(pg.member_id, pg.installer_ws)

    agent_hit = await _by_target(client, "agent", copies.agent_id)
    managed_hit = await _by_target(client, "skill", copies.managed_skill_id)
    skill_hit = await _by_target(client, "skill", copies.skill_id)

    for response in (agent_hit, managed_hit, skill_hit):
        assert response.status_code == 200, response.text
        assert response.json()["error"] is None
    assert agent_hit.json()["data"] == {
        "listing_id": copies.agent_listing,
        "listing_name": f"来源助手{pg.suffix}",
        "listing_status": "listed",
        "installed_version_number": 1,
        "installed_with_agent": False,
    }
    # 随 Agent 一起装来的托管 Skill 指向 Agent 条目，并标明是随 Agent 安装的。
    assert managed_hit.json()["data"] == {
        "listing_id": copies.agent_listing,
        "listing_name": f"来源助手{pg.suffix}",
        "listing_status": "listed",
        "installed_version_number": 1,
        "installed_with_agent": True,
    }
    assert skill_hit.json()["data"] == {
        "listing_id": copies.skill_listing,
        "listing_name": f"solo-{pg.suffix}",
        "listing_status": "listed",
        "installed_version_number": 1,
        "installed_with_agent": False,
    }

    # 条目下架后照样命中，状态跟着变。
    await MarketService(pg.db).delist_listing(pg.publisher_ws, pg.author_id, copies.agent_listing)
    after_delist = await _by_target(client, "agent", copies.agent_id)
    assert after_delist.json()["data"]["listing_status"] == "delisted"


async def test_by_target_misses_and_validates_query(pg, installed_copies, api) -> None:
    copies = installed_copies
    client = api(pg.installer_id, pg.installer_ws)
    other_client = api(pg.outsider_id, pg.other_ws)

    own = await _by_target(client, "skill", copies.own_skill_id)
    # 别的工作区查不到安装工作区的安装记录。
    from_other_workspace = await _by_target(other_client, "agent", copies.agent_id)
    invalid_kind = await _by_target(client, "team", 1)
    zero_target = await _by_target(client, "agent", 0)
    negative_target = await _by_target(client, "skill", -3)
    missing_target = await client.get("/api/market/installs/by-target", params={"kind": "agent"})
    # 装出来的 Agent 删除后，这条安装不再有效，按 Agent 查不再命中。
    await AgentService(pg.db).delete_agent(pg.installer_ws, copies.agent_id, pg.installer_id)
    deleted_agent = await _by_target(client, "agent", copies.agent_id)

    for response in (own, from_other_workspace, deleted_agent):
        assert response.status_code == 200, response.text
        assert response.json()["data"] is None
        assert response.json()["error"] is None
    for response in (invalid_kind, zero_target, negative_target, missing_target):
        assert response.status_code == 422, response.text
