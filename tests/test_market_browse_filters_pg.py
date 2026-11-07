"""市场浏览的安装状态筛选、按源对象查条目的真实 PostgreSQL 集成测试。

浏览：installed、has_update、is_official 三个筛选，多条件组合，筛选后的 total 和分页，
以及「安装出来的 Agent 或 Skill 已删除时视为未安装」。
按源对象查条目：找到时与「我的上架」单个条目一致、找不到返回 null、非 owner/admin 返回 403、
查不到别的工作区的条目、kind 非法返回 422。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库，整个用例包在外层事务里，结束时回滚，不留数据。
测试数据都带唯一后缀，浏览时用后缀作关键词，避开库里已有的条目。库不可达时跳过并给出原因。
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.service import AgentDefinitionService
from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.config import settings
from market.repository import MarketRepository
from market.schemas import MarketBrowsePage
from market.service import MarketService, compute_snapshot_hash
from skill.models import SkillRecord
from skill.parser import SkillBundle
from skill.repository import SkillRepository
from skill.service import SkillService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

# 用户键 -> (所在工作区键, 角色)；平台管理员不属于任何工作区，只负责审核。
_MEMBERSHIPS = {
    "author": ("publisher", "owner"),
    "publisher_admin": ("publisher", "admin"),
    "installer": ("installer", "owner"),
    "member": ("installer", "member"),
    "outsider": ("other", "owner"),
}


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
            email=f"mkf-{user_key}-{suffix}@test.local",
            username=f"mkf-{user_key}-{suffix}",
            password="x",
            is_platform_admin=user_key == "platform_admin",
        )
    workspaces: dict[str, WorkspaceRecord] = {}
    for workspace_key in ("publisher", "installer", "other"):
        workspaces[workspace_key] = WorkspaceRecord(
            name=f"mkf-{workspace_key}-{suffix}",
            slug=f"mkf-{workspace_key}-{suffix}",
            display_name=f"筛选测试{workspace_key}{suffix}",
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
            publisher_admin_id=users["publisher_admin"].id,
            installer_id=users["installer"].id,
            member_id=users["member"].id,
            outsider_id=users["outsider"].id,
            platform_admin_id=users["platform_admin"].id,
            publisher_ws=workspaces["publisher"].id,
            installer_ws=workspaces["installer"].id,
            other_ws=workspaces["other"].id,
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _save_skill(env: SimpleNamespace, name: str) -> SkillRecord:
    """在发布工作区直接建一个 Skill 作为上架的源对象。"""
    content = f"---\nname: {name}\ndescription: {name} 的描述\n---\n\n正文\n"
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


async def _create_agent(env: SimpleNamespace, name: str) -> int:
    """在发布工作区建一个不绑工具和 Skill 的 Agent，返回 Agent ID。"""
    agent = await AgentDefinitionService(env.db).create_agent(
        workspace_id=env.publisher_ws,
        actor_user_id=env.author_id,
        name=name,
        description=f"{name}的描述",
        system_prompt=f"你是{name}",
        model_name=None,
        is_enabled=True,
        tool_ids=[],
        skill_ids=[],
    )
    return agent.id


async def _publish(env: SimpleNamespace, kind: str, source_id: int) -> int:
    """提交并审核通过，返回条目 ID。同一个源对象再调用一次就是发布新版本。"""
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


async def _create_official_listing(env: SimpleNamespace, skill_name: str) -> int:
    """直接用 Repository 建一个已上架的官方 Skill 条目，与预置同步的建法相同。"""
    repo = MarketRepository(env.db)
    listing = await repo.create_listing(
        kind="skill",
        status="unlisted",
        is_official=True,
        official_key=skill_name,
        publisher_workspace_id=None,
        source_agent_id=None,
        source_skill_id=None,
        created_by_user_id=None,
    )
    content = f"---\nname: {skill_name}\ndescription: {skill_name} 的描述\n---\n\n正文\n"
    bundle = SkillBundle.from_content(content, {})
    snapshot = {
        "skill": {
            "name": bundle.name,
            "content": bundle.content,
            "extra_files": bundle.extra_files,
            "skill_hash": bundle.skill_hash,
        }
    }
    version = await repo.create_version(
        listing_id=listing.id,
        version_number=1,
        status="approved",
        category="general",
        release_note=None,
        snapshot=snapshot,
        snapshot_hash=compute_snapshot_hash(snapshot),
        submitted_by_user_id=None,
        reviewed_by_user_id=None,
        reviewed_at=None,
    )
    await MarketService(env.db).publish_version(listing, version)
    return listing.id


@pytest.fixture
async def market(pg):
    """准备八个条目，覆盖安装工作区里的各种安装状态；返回的键名就是各条目的预期状态。"""
    service = MarketService(pg.db)
    suffix = pg.suffix

    async def install(listing_id: int):
        return await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)

    # 官方条目：一个装了最新版本，一个没装。
    official_installed = await _create_official_listing(pg, f"off-inst-{suffix}")
    official_fresh = await _create_official_listing(pg, f"off-fresh-{suffix}")
    await install(official_installed)

    # 社区 Skill：装了 v1 之后作者发布了 v2，已安装且有更新。
    outdated_skill = await _save_skill(pg, f"outdated-{suffix}")
    outdated = await _publish(pg, "skill", outdated_skill.id)
    await install(outdated)
    await _publish_new_skill_version(pg, outdated_skill.id)

    # 社区 Agent：装的就是最新版本。
    agent_source_id = await _create_agent(pg, f"筛选助手{suffix}")
    agent_current = await _publish(pg, "agent", agent_source_id)
    await install(agent_current)

    # 社区 Skill：从没装过。
    fresh_skill = await _save_skill(pg, f"fresh-{suffix}")
    fresh = await _publish(pg, "skill", fresh_skill.id)

    # 社区 Skill：装了 v1、作者又发布了 v2，然后装出来的 Skill 被删掉：视为未安装，也不算有更新。
    gone_skill = await _save_skill(pg, f"gone-{suffix}")
    skill_target_deleted = await _publish(pg, "skill", gone_skill.id)
    gone_skill_install = await install(skill_target_deleted)
    await _publish_new_skill_version(pg, gone_skill.id)
    await SkillService(pg.db).delete_skill(
        pg.installer_ws, gone_skill_install.install.target_skill_id, pg.installer_id
    )

    # 社区 Agent：装出来的 Agent 被删掉，视为未安装。
    gone_agent_source_id = await _create_agent(pg, f"已删助手{suffix}")
    agent_target_deleted = await _publish(pg, "agent", gone_agent_source_id)
    gone_agent_install = await install(agent_target_deleted)
    await AgentDefinitionService(pg.db).delete_agent(
        pg.installer_ws, gone_agent_install.install.target_agent_id, pg.installer_id
    )

    # 社区 Skill：装过之后被作者下架。只有 installed=true 时出现，其他筛选下都不出现。
    delisted_skill = await _save_skill(pg, f"delisted-{suffix}")
    delisted = await _publish(pg, "skill", delisted_skill.id)
    await install(delisted)
    await service.delist_listing(pg.publisher_ws, pg.author_id, delisted)

    return SimpleNamespace(
        official_installed=official_installed,
        official_fresh=official_fresh,
        outdated=outdated,
        outdated_source_id=outdated_skill.id,
        agent_current=agent_current,
        agent_source_id=agent_source_id,
        fresh=fresh,
        skill_target_deleted=skill_target_deleted,
        agent_target_deleted=agent_target_deleted,
        delisted=delisted,
    )


async def _browse(
    env: SimpleNamespace,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    **filters: Any,
) -> MarketBrowsePage:
    """默认以安装工作区的 owner 浏览；关键词固定为本用例的后缀，只看得到本用例建的条目。"""
    if workspace_id is None:
        workspace_id = env.installer_ws
        user_id = env.installer_id
    query: dict[str, Any] = {"kind": None, "category": None, "keyword": env.suffix}
    query.update(filters)
    return await MarketService(env.db).list_listings(
        workspace_id, user_id, limit=limit, offset=offset, **query
    )


def _ids(page: MarketBrowsePage) -> set[int]:
    ids: set[int] = set()
    for item in page.items:
        ids.add(item.id)
    return ids


def _all_listed(market: SimpleNamespace) -> set[int]:
    """本用例全部 listed 的条目；下架的那个不在其中。"""
    return {
        market.official_installed,
        market.official_fresh,
        market.outdated,
        market.agent_current,
        market.fresh,
        market.skill_target_deleted,
        market.agent_target_deleted,
    }


# ---------- 浏览筛选 ----------


async def test_installed_filter_counts_only_active_installs(pg, market) -> None:
    everything = await _browse(pg)
    installed = await _browse(pg, installed=True)
    not_installed = await _browse(pg, installed=False)

    # 不带新筛选时与原来一致：只有 listed 的条目，下架的不出现。
    assert (_ids(everything), everything.total) == (_all_listed(market), 7)
    # installed=true 还会带上装过、已被下架的条目，并给出状态和下架原因。
    assert _ids(installed) == {
        market.official_installed,
        market.outdated,
        market.agent_current,
        market.delisted,
    }
    assert installed.total == 4
    for item in installed.items:
        if item.id == market.delisted:
            assert (item.status, item.status_reason) == ("delisted", None)
        else:
            assert (item.status, item.status_reason) == ("listed", None)
    # 安装出来的 Skill 或 Agent 被删掉的，算作未安装。
    assert _ids(not_installed) == {
        market.official_fresh,
        market.fresh,
        market.skill_target_deleted,
        market.agent_target_deleted,
    }
    assert not_installed.total == 4
    # 列表项里的 installed 字段与筛选口径一致。
    for item in installed.items:
        assert item.installed is True
    for item in not_installed.items:
        assert item.installed is False
        assert item.has_update is False


async def test_has_update_filter(pg, market) -> None:
    with_update = await _browse(pg, has_update=True)
    without_update = await _browse(pg, has_update=False)

    # 装出来的 Skill 已删除的那个条目也有 v2，但不算已安装，所以不算有更新。
    assert (_ids(with_update), with_update.total) == ({market.outdated}, 1)
    assert with_update.items[0].installed_version_number == 1
    assert with_update.items[0].current_version_number == 2
    assert with_update.items[0].has_update is True
    assert _ids(without_update) == _all_listed(market) - {market.outdated}
    assert without_update.total == 6
    for item in without_update.items:
        assert item.has_update is False


async def test_is_official_filter(pg, market) -> None:
    official = await _browse(pg, is_official=True)
    community = await _browse(pg, is_official=False)

    official_ids = {market.official_installed, market.official_fresh}
    assert (_ids(official), official.total) == (official_ids, 2)
    assert _ids(community) == _all_listed(market) - official_ids
    assert community.total == 5


async def test_filters_combine_with_and(pg, market) -> None:
    installed_community = await _browse(pg, installed=True, is_official=False)
    installed_without_update = await _browse(pg, installed=True, has_update=False)
    impossible = await _browse(pg, installed=False, has_update=True)
    installed_skills = await _browse(pg, installed=True, kind="skill")
    official_with_update = await _browse(pg, has_update=True, is_official=True)
    deleted_agents = await _browse(pg, installed=False, is_official=False, kind="agent")
    community_without_update = await _browse(pg, has_update=False, is_official=False)

    # 带 installed=true 的组合会包含已下架的条目。
    assert _ids(installed_community) == {market.outdated, market.agent_current, market.delisted}
    assert installed_community.total == 3
    assert _ids(installed_without_update) == {
        market.official_installed,
        market.agent_current,
        market.delisted,
    }
    assert installed_without_update.total == 3
    assert (impossible.items, impossible.total) == ([], 0)
    assert _ids(installed_skills) == {market.official_installed, market.outdated, market.delisted}
    assert installed_skills.total == 3
    # 其他组合仍只返回 listed 的条目。
    assert (official_with_update.items, official_with_update.total) == ([], 0)
    assert (_ids(deleted_agents), deleted_agents.total) == ({market.agent_target_deleted}, 1)
    # 下架的条目是社区条目、也没有更新，这一组里只有状态能把它排除。
    assert _ids(community_without_update) == {
        market.agent_current,
        market.fresh,
        market.skill_target_deleted,
        market.agent_target_deleted,
    }
    assert community_without_update.total == 4


async def test_filtered_total_and_pagination(pg, market) -> None:
    full = await _browse(pg, installed=False)
    first_page = await _browse(pg, installed=False, limit=3, offset=0)
    second_page = await _browse(pg, installed=False, limit=3, offset=3)
    beyond = await _browse(pg, installed=False, limit=3, offset=6)

    full_order = [item.id for item in full.items]
    paged_order = [item.id for item in first_page.items + second_page.items]
    # total 是筛选之后的总数，每一页都一样；分页拼起来就是完整列表，顺序不变。
    assert (first_page.total, second_page.total, beyond.total) == (4, 4, 4)
    assert (len(first_page.items), len(second_page.items), beyond.items) == (3, 1, [])
    assert paged_order == full_order
    # 排序规则不变：官方的在前。
    assert full_order[0] == market.official_fresh


async def test_workspace_without_installs(pg, market) -> None:
    """工作区一条安装记录都没有时，IN 空集合恒假、NOT IN 空集合恒真。"""

    async def browse_other(**filters: Any) -> MarketBrowsePage:
        return await _browse(pg, workspace_id=pg.other_ws, user_id=pg.outsider_id, **filters)

    installed = await browse_other(installed=True)
    not_installed = await browse_other(installed=False)
    with_update = await browse_other(has_update=True)
    without_update = await browse_other(has_update=False)

    assert (installed.items, installed.total) == ([], 0)
    assert (_ids(not_installed), not_installed.total) == (_all_listed(market), 7)
    assert (with_update.items, with_update.total) == ([], 0)
    assert (_ids(without_update), without_update.total) == (_all_listed(market), 7)


# ---------- 通过 HTTP 调用 ----------


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


async def test_browse_filters_over_http(pg, market, api) -> None:
    client = api(pg.installer_id, pg.installer_ws)

    response = await client.get(
        "/api/market/listings",
        params={"keyword": pg.suffix, "installed": "true", "is_official": "false"},
    )
    invalid = await client.get("/api/market/listings", params={"has_update": "maybe"})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 3
    items_by_id: dict[int, dict[str, Any]] = {}
    for item in data["items"]:
        items_by_id[item["id"]] = item
    assert set(items_by_id) == {market.outdated, market.agent_current, market.delisted}
    # 已下架的条目带上状态和下架原因，前端据此显示「已下架」。
    delisted_item = items_by_id[market.delisted]
    assert (delisted_item["status"], delisted_item["status_reason"]) == ("delisted", None)
    assert items_by_id[market.outdated]["status"] == "listed"
    # 原有字段不变，新增 status 和 status_reason。
    assert set(data["items"][0]) == {
        "id",
        "kind",
        "name",
        "summary",
        "category",
        "is_official",
        "status",
        "status_reason",
        "current_version_number",
        "publisher_name",
        "install_count",
        "installed",
        "installed_version_number",
        "has_update",
        "updated_at",
    }
    assert invalid.status_code == 422


# ---------- 按源对象查条目 ----------


async def _by_source(client: httpx.AsyncClient, kind: str, source_id: int) -> httpx.Response:
    return await client.get(
        "/api/market/mine/by-source",
        params={"kind": kind, "source_id": source_id},
    )


async def test_by_source_returns_same_item_as_mine(pg, market, api) -> None:
    # 再准备一个只有驳回版本的条目：还没上架，但已经有条目，前端应显示「提交新版本」。
    rejected_skill = await _save_skill(pg, f"rejected-{pg.suffix}")
    rejected_version = await MarketService(pg.db).submit(
        pg.publisher_ws,
        pg.author_id,
        kind="skill",
        source_id=rejected_skill.id,
        category="data",
        release_note=None,
    )
    await MarketService(pg.db).reject_version(pg.platform_admin_id, rejected_version.id, "描述不清")
    client = api(pg.author_id, pg.publisher_ws)

    mine = await client.get("/api/market/mine")
    agent_item = await _by_source(client, "agent", market.agent_source_id)
    outdated_item = await _by_source(client, "skill", market.outdated_source_id)
    rejected_item = await _by_source(client, "skill", rejected_skill.id)

    assert mine.status_code == 200, mine.text
    mine_by_listing_id: dict[int, dict[str, Any]] = {}
    for item in mine.json()["data"]:
        mine_by_listing_id[item["listing"]["id"]] = item
    for response in (agent_item, outdated_item, rejected_item):
        assert response.status_code == 200, response.text
        assert response.json()["error"] is None
    # 结构和内容与「我的上架」里的单个条目完全相同。
    assert agent_item.json()["data"] == mine_by_listing_id[market.agent_current]
    outdated_data = outdated_item.json()["data"]
    assert outdated_data == mine_by_listing_id[market.outdated]
    outdated_versions = []
    for version in outdated_data["versions"]:
        outdated_versions.append((version["version_number"], version["status"]))
    assert outdated_versions == [(2, "approved"), (1, "approved")]
    rejected_data = rejected_item.json()["data"]
    assert rejected_data == mine_by_listing_id[rejected_version.listing_id]
    assert rejected_data["listing"]["status"] == "unlisted"
    assert len(rejected_data["versions"]) == 1
    assert rejected_data["versions"][0]["status"] == "rejected"
    assert rejected_data["versions"][0]["review_comment"] == "描述不清"


async def test_by_source_allows_workspace_admin_and_returns_null_when_missing(
    pg, market, api
) -> None:
    never_submitted = await _save_skill(pg, f"never-{pg.suffix}")
    client = api(pg.publisher_admin_id, pg.publisher_ws)

    found = await _by_source(client, "skill", market.outdated_source_id)
    missing = await _by_source(client, "skill", never_submitted.id)

    # 工作区 admin 与 owner 一样可以查。
    assert found.status_code == 200, found.text
    assert found.json()["data"]["listing"]["id"] == market.outdated
    # 没有条目是正常情况：200，data 为 null，不是 404。
    assert missing.status_code == 200, missing.text
    assert missing.json()["data"] is None
    assert missing.json()["error"] is None


async def test_by_source_does_not_see_other_workspace_listing(pg, market, api) -> None:
    # 安装工作区的 owner 拿发布工作区的源对象来查：条目的发布方不是自己，查不到。
    installer_client = api(pg.installer_id, pg.installer_ws)
    publisher_client = api(pg.author_id, pg.publisher_ws)

    from_other_workspace = await _by_source(installer_client, "skill", market.outdated_source_id)
    from_publisher = await _by_source(publisher_client, "skill", market.outdated_source_id)

    assert from_other_workspace.status_code == 200, from_other_workspace.text
    assert from_other_workspace.json()["data"] is None
    assert from_publisher.json()["data"]["listing"]["id"] == market.outdated


async def test_by_source_requires_owner_or_admin(pg, api) -> None:
    client = api(pg.member_id, pg.installer_ws)

    response = await _by_source(client, "skill", 1)

    assert response.status_code == 403, response.text
    assert response.json()["data"] is None
    assert response.json()["error"]["message"] == "无权执行该工作区操作"


async def test_by_source_validates_query(pg, api) -> None:
    client = api(pg.author_id, pg.publisher_ws)

    invalid_kind = await _by_source(client, "team", 1)
    missing_source = await client.get("/api/market/mine/by-source", params={"kind": "agent"})
    zero_source = await _by_source(client, "agent", 0)

    assert invalid_kind.status_code == 422
    assert missing_source.status_code == 422
    assert zero_source.status_code == 422
