"""market 域 Service 的真实 PostgreSQL 集成测试：上架提交与审核、下架与重新上架、我的上架、
审核列表、浏览与详情（含安装预览）、安装与升级。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库，整个用例包在外层事务里，结束时回滚，不留数据。
预期失败的调用放在 SAVEPOINT 里执行，模拟请求事务的整体回滚，失败后外层事务还能继续使用。
库不可达时跳过并给出原因。
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.models import AgentRecord
from agent.repository import AgentRepository
from agent.service import AgentService
from core.config import settings
from core.errors import AgentException
from market import service as market_service
from market.models import MarketVersionRecord
from market.repository import MarketRepository
from market.service import MarketService, compute_snapshot_hash
from skill.models import SkillRecord
from skill.parser import SkillBundle, encode_file
from skill.repository import SkillRepository
from skill.service import SkillService
from tools.repository import ToolCatalogRepository
from tools.service import ToolCatalogService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

_TOOL_NAMES = ("Read", "Write", "Grep")


async def _enabled_tool_ids(db: AsyncSession) -> dict[str, int]:
    """取测试用内置工具的目录 ID；目录缺行时先同步，改动都随外层事务回滚。"""
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
    users = {}
    for role in ("author", "installer", "member", "admin", "outsider"):
        users[role] = UserRecord(
            email=f"mk-{role}-{suffix}@test.local",
            username=f"mk-{role}-{suffix}",
            password="x",
            is_platform_admin=role == "admin",
        )
    publisher = WorkspaceRecord(
        name=f"mk-pub-{suffix}", slug=f"mk-pub-{suffix}", display_name=f"发布工作区{suffix}"
    )
    installer = WorkspaceRecord(
        name=f"mk-ins-{suffix}", slug=f"mk-ins-{suffix}", display_name=f"安装工作区{suffix}"
    )
    # 与条目无关的工作区：既不发布也不安装。
    other = WorkspaceRecord(
        name=f"mk-oth-{suffix}", slug=f"mk-oth-{suffix}", display_name=f"无关工作区{suffix}"
    )
    session.add_all([*users.values(), publisher, installer, other])
    await session.flush()
    now = datetime.now(UTC)
    memberships = [
        (publisher, users["author"], "owner"),
        (installer, users["installer"], "owner"),
        (installer, users["member"], "member"),
        (installer, users["admin"], "member"),
        (other, users["outsider"], "owner"),
    ]
    for workspace, user, role in memberships:
        session.add(
            WorkspaceMemberRecord(
                workspace_id=workspace.id, user_id=user.id, role=role, joined_at=now
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
            admin_id=users["admin"].id,
            outsider_id=users["outsider"].id,
            publisher_ws=publisher.id,
            installer_ws=installer.id,
            other_ws=other.id,
            tool_ids=await _enabled_tool_ids(session),
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


def _bundle(
    name: str,
    body: str = "正文",
    extra_files: dict[str, dict[str, str]] | None = None,
) -> SkillBundle:
    content = f"---\nname: {name}\ndescription: {name} 的描述\n---\n\n{body}\n"
    if extra_files is None:
        extra_files = {"scripts/run.py": encode_file(b"print('hi')\n")}
    return SkillBundle.from_content(content, extra_files)


def _skill_snapshot(bundle: SkillBundle) -> dict[str, Any]:
    return {
        "name": bundle.name,
        "content": bundle.content,
        "extra_files": bundle.extra_files,
        "skill_hash": bundle.skill_hash,
    }


async def _save_skill(
    env: SimpleNamespace,
    name: str,
    *,
    body: str = "正文",
    extra_files: dict[str, dict[str, str]] | None = None,
    workspace_id: int | None = None,
) -> SkillRecord:
    bundle = _bundle(name, body, extra_files)
    return await SkillRepository(env.db).save(
        None,
        workspace_id=workspace_id or env.publisher_ws,
        name=bundle.name,
        description=bundle.description,
        frontmatter=bundle.parsed.frontmatter,
        version=bundle.version,
        skill_hash=bundle.skill_hash,
        content=bundle.content,
        extra_files=bundle.extra_files,
        actor_user_id=env.author_id,
    )


async def _create_agent(
    env: SimpleNamespace,
    name: str,
    *,
    workspace_id: int | None = None,
    tools: tuple[str, ...] = (),
    skill_ids: tuple[int, ...] = (),
    model_name: str | None = None,
) -> AgentRecord:
    """默认建在发布工作区；建在安装工作区时用那边的 owner 操作。"""
    actor_id = env.author_id
    if workspace_id is None:
        workspace_id = env.publisher_ws
    if workspace_id == env.installer_ws:
        actor_id = env.installer_id
    tool_ids: list[int] = []
    for tool in tools:
        tool_ids.append(env.tool_ids[tool])
    return await AgentService(env.db).create_agent(
        workspace_id=workspace_id,
        actor_user_id=actor_id,
        name=name,
        description=f"{name}的描述",
        system_prompt=f"你是{name}",
        model_name=model_name,
        is_enabled=True,
        tool_ids=tool_ids,
        skill_ids=list(skill_ids),
    )


async def _submit(
    env: SimpleNamespace,
    kind: str,
    source_id: int,
    *,
    category: str = "data",
) -> MarketVersionRecord:
    return await MarketService(env.db).submit(
        env.publisher_ws,
        env.author_id,
        kind=kind,
        source_id=source_id,
        category=category,
        release_note=None,
    )


async def _publish(
    env: SimpleNamespace,
    kind: str,
    source_id: int,
    *,
    category: str = "data",
) -> int:
    """提交并审核通过，返回条目 ID。同一个源对象再调用一次就是发布新版本。"""
    version = await _submit(env, kind, source_id, category=category)
    await MarketService(env.db).approve_version(env.admin_id, version.id, None)
    return version.listing_id


async def _create_official_listing(env: SimpleNamespace, skill_name: str) -> int:
    """直接用 Repository 建一个已上架的官方 Skill 条目，第三阶段的预置同步也是这样建的。"""
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
    snapshot = {"skill": _skill_snapshot(_bundle(skill_name))}
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


async def _expect_error(
    env: SimpleNamespace,
    call: Callable[[], Awaitable[Any]],
) -> AgentException:
    """像请求事务一样执行：失败时回滚到保存点，这次调用写过的数据全部撤销。"""
    with pytest.raises(AgentException) as exc_info:
        async with env.db.begin_nested():
            await call()
    return exc_info.value


async def _installer_detail(env: SimpleNamespace, listing_id: int):
    return await MarketService(env.db).get_listing_detail(
        env.installer_ws, env.installer_id, listing_id, is_platform_admin=False
    )


async def _bound_ids(env: SimpleNamespace, agent_id: int) -> tuple[set[int], set[int]]:
    """返回安装工作区里 Agent 当前绑定的工具 ID 和 Skill ID。"""
    agent = await AgentRepository(env.db).get(env.installer_ws, agent_id)
    assert agent is not None
    tool_ids = {binding.tool_id for binding in agent.tool_bindings}
    skill_ids = {binding.skill_id for binding in agent.skill_bindings}
    return tool_ids, skill_ids


def _outcomes(report) -> dict[str, str]:
    return {skill.name: skill.action for skill in report.skills}


# ---------- 社区上架与审核 ----------


async def test_submit_withdraw_and_approve_flow(pg) -> None:
    name = f"alpha-{pg.suffix}"
    skill = await _save_skill(pg, name)
    service = MarketService(pg.db)
    repo = MarketRepository(pg.db)

    first = await _submit(pg, "skill", skill.id)

    listing = await repo.get_listing(first.listing_id)
    assert listing is not None
    assert listing.status == "unlisted"
    assert listing.current_version_number is None
    assert listing.publisher_workspace_id == pg.publisher_ws
    assert listing.source_skill_id == skill.id
    assert (first.version_number, first.status) == (1, "pending")
    expected = {"skill": await SkillService(pg.db).build_snapshot(pg.publisher_ws, skill.id)}
    assert first.snapshot == expected
    assert first.snapshot_hash == compute_snapshot_hash(expected)

    # 已有待审版本时再次提交被拒。
    duplicate = await _expect_error(pg, lambda: _submit(pg, "skill", skill.id))
    assert duplicate.status_code == 409

    withdrawn = await service.withdraw_version(pg.publisher_ws, pg.author_id, first.id)
    assert withdrawn.status == "withdrawn"
    second = await _submit(pg, "skill", skill.id)
    assert (second.listing_id, second.version_number) == (first.listing_id, 2)

    approved = await service.approve_version(pg.admin_id, second.id, "内容合规")
    assert approved.status == "approved"
    assert approved.reviewed_by_user_id == pg.admin_id
    assert approved.reviewed_at is not None
    assert approved.review_comment == "内容合规"
    listing = await repo.get_listing(first.listing_id)
    assert listing is not None
    assert listing.status == "listed"
    assert listing.current_version_number == 2
    assert (listing.name, listing.summary, listing.category) == (name, f"{name} 的描述", "data")


async def test_same_content_resubmit_and_reject_flow(pg) -> None:
    agent = await _create_agent(pg, f"分析师{pg.suffix}", tools=("Read",))
    listing_id = await _publish(pg, "agent", agent.id)
    service = MarketService(pg.db)

    same = await _expect_error(pg, lambda: _submit(pg, "agent", agent.id))
    assert same.status_code == 409
    assert same.message == "内容与当前上架版本相同"

    await AgentService(pg.db).update_agent(
        pg.publisher_ws, agent.id, pg.author_id, {"system_prompt": "新的提示词"}
    )
    second = await _submit(pg, "agent", agent.id)
    rejected = await service.reject_version(pg.admin_id, second.id, "提示词不完整")
    assert rejected.status == "rejected"
    assert rejected.review_comment == "提示词不完整"
    listing = await MarketRepository(pg.db).get_listing(listing_id)
    assert listing is not None
    assert (listing.status, listing.current_version_number) == ("listed", 1)

    # 已驳回的版本不能再审核，也不能撤回。
    approve_again = await _expect_error(
        pg, lambda: service.approve_version(pg.admin_id, second.id, None)
    )
    withdraw_again = await _expect_error(
        pg, lambda: service.withdraw_version(pg.publisher_ws, pg.author_id, second.id)
    )
    assert approve_again.status_code == 409
    assert withdraw_again.status_code == 409


async def test_submit_failure_paths(pg, monkeypatch) -> None:
    clean = await _save_skill(pg, f"clean-{pg.suffix}")
    leaky = await _save_skill(
        pg,
        f"leaky-{pg.suffix}",
        extra_files={"config/.env": encode_file(b"API_KEY=x\n")},
    )
    other_ws_skill = await _save_skill(pg, f"other-{pg.suffix}", workspace_id=pg.installer_ws)
    service = MarketService(pg.db)

    credential = await _expect_error(pg, lambda: _submit(pg, "skill", leaky.id))
    category = await _expect_error(pg, lambda: _submit(pg, "skill", clean.id, category="games"))
    foreign = await _expect_error(pg, lambda: _submit(pg, "skill", other_ws_skill.id))
    no_access = await _expect_error(
        pg,
        lambda: service.submit(
            pg.installer_ws,
            pg.member_id,
            kind="skill",
            source_id=other_ws_skill.id,
            category="data",
            release_note=None,
        ),
    )
    monkeypatch.setattr(market_service, "MAX_SNAPSHOT_BYTES", 10)
    too_large = await _expect_error(pg, lambda: _submit(pg, "skill", clean.id))

    assert credential.status_code == 400
    assert credential.details == {"paths": [f"leaky-{pg.suffix}/config/.env"]}
    assert category.status_code == 400
    assert foreign.status_code == 404
    assert no_access.status_code == 403
    assert too_large.status_code == 400
    # 失败的提交整体回滚，不留下空条目。
    assert await MarketRepository(pg.db).get_listing_by_source_for_update("skill", clean.id) is None


async def test_removed_listing_accepts_new_version_and_relists_on_approval(pg) -> None:
    skill = await _save_skill(pg, f"gamma-{pg.suffix}")
    listing_id = await _publish(pg, "skill", skill.id)
    service = MarketService(pg.db)

    removed = await service.remove_listing(pg.admin_id, listing_id, "含违规内容")
    assert (removed.status, removed.status_reason) == ("removed", "含违规内容")
    relist = await _expect_error(
        pg, lambda: service.relist_listing(pg.publisher_ws, pg.author_id, listing_id)
    )
    assert relist.status_code == 409

    await SkillService(pg.db).update_from_fields(
        pg.publisher_ws,
        skill.id,
        pg.author_id,
        name=None,
        description="修正后的描述",
        body="修正后的正文",
        version=None,
        compatibility=None,
        metadata=None,
    )
    version = await _submit(pg, "skill", skill.id)
    await service.approve_version(pg.admin_id, version.id, None)

    listing = await MarketRepository(pg.db).get_listing(listing_id)
    assert listing is not None
    assert (listing.status, listing.status_reason) == ("listed", None)
    assert (listing.current_version_number, listing.summary) == (2, "修正后的描述")


async def test_delisted_listing_is_visible_to_installer_but_not_installable(pg) -> None:
    skill = await _save_skill(pg, f"delta-{pg.suffix}")
    listing_id = await _publish(pg, "skill", skill.id)
    service = MarketService(pg.db)
    await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    await SkillService(pg.db).update_from_fields(
        pg.publisher_ws,
        skill.id,
        pg.author_id,
        name=None,
        description="v2",
        body="第二版",
        version=None,
        compatibility=None,
        metadata=None,
    )
    await _publish(pg, "skill", skill.id)

    delisted = await service.delist_listing(pg.publisher_ws, pg.author_id, listing_id)
    assert delisted.status == "delisted"

    # 装过的工作区仍能看到详情，好从「已安装」里点开；但没有安装预览，也不算有更新。
    detail = await _installer_detail(pg, listing_id)
    assert (detail.status, detail.status_reason) == ("delisted", None)
    assert detail.install_preview is None
    assert detail.listing.has_update is False
    # 安装和升级照旧被拒绝。
    install = await _expect_error(
        pg, lambda: service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    )
    upgrade = await _expect_error(
        pg, lambda: service.upgrade_listing(pg.installer_ws, pg.installer_id, listing_id)
    )
    assert install.status_code == 409
    assert upgrade.status_code == 409
    # 与条目无关的工作区按不存在处理。
    unrelated = await _expect_error(
        pg,
        lambda: service.get_listing_detail(
            pg.other_ws, pg.outsider_id, listing_id, is_platform_admin=False
        ),
    )
    assert unrelated.status_code == 404
    browse = await service.list_listings(
        pg.installer_ws,
        pg.installer_id,
        kind=None,
        category=None,
        keyword=pg.suffix,
        limit=20,
        offset=0,
    )
    assert browse.total == 0

    # 发布工作区的成员和平台管理员仍能看到详情，但没有安装预览。
    own = await service.get_listing_detail(
        pg.publisher_ws, pg.author_id, listing_id, is_platform_admin=False
    )
    admin = await service.get_listing_detail(
        pg.installer_ws, pg.admin_id, listing_id, is_platform_admin=True
    )
    assert own.status == "delisted"
    assert own.current_version is not None
    assert own.install_preview is None
    assert admin.status == "delisted"

    relisted = await service.relist_listing(pg.publisher_ws, pg.author_id, listing_id)
    assert relisted.status == "listed"
    assert (await _installer_detail(pg, listing_id)).listing.has_update is True


async def test_delist_and_relist_permission_and_official_rules(pg) -> None:
    skill = await _save_skill(pg, f"epsilon-{pg.suffix}")
    listing_id = await _publish(pg, "skill", skill.id)
    official_id = await _create_official_listing(pg, f"official-{pg.suffix}")
    service = MarketService(pg.db)

    other_ws = await _expect_error(
        pg, lambda: service.delist_listing(pg.installer_ws, pg.installer_id, listing_id)
    )
    official_delist = await _expect_error(
        pg, lambda: service.delist_listing(pg.publisher_ws, pg.author_id, official_id)
    )
    official_relist = await _expect_error(
        pg, lambda: service.relist_listing(pg.publisher_ws, pg.author_id, official_id)
    )
    official_remove = await _expect_error(
        pg, lambda: service.remove_listing(pg.admin_id, official_id, "测试")
    )
    not_delisted = await _expect_error(
        pg, lambda: service.relist_listing(pg.publisher_ws, pg.author_id, listing_id)
    )

    assert other_ws.status_code == 404
    assert official_delist.status_code == 409
    assert official_relist.status_code == 409
    assert official_remove.status_code == 409
    assert official_remove.message == "官方内容请从代码中移除"
    assert not_delisted.status_code == 409


async def test_my_listings_and_review_queue(pg) -> None:
    listed = await _save_skill(pg, f"zeta-{pg.suffix}")
    pending = await _save_skill(pg, f"eta-{pg.suffix}")
    listing_id = await _publish(pg, "skill", listed.id)
    rejected = await _submit(pg, "skill", pending.id)
    await MarketService(pg.db).reject_version(pg.admin_id, rejected.id, "描述不清")
    waiting = await _submit(pg, "skill", pending.id)
    service = MarketService(pg.db)

    mine = await service.list_my_listings(pg.publisher_ws, pg.author_id)
    by_listing = {item.listing.id: item for item in mine}
    assert set(by_listing) == {listing_id, rejected.listing_id}
    versions = by_listing[rejected.listing_id].versions
    assert [(v.version_number, v.status) for v in versions] == [(2, "pending"), (1, "rejected")]
    assert versions[1].review_comment == "描述不清"

    queue = await service.list_versions_for_review(status="pending", limit=100, offset=0)
    queued = [item for item in queue.items if item.version.id == waiting.id]
    assert len(queued) == 1
    assert queued[0].version.snapshot["skill"]["name"] == f"eta-{pg.suffix}"
    assert queued[0].listing.id == rejected.listing_id
    assert queued[0].publisher_name == f"发布工作区{pg.suffix}"

    member_mine = await _expect_error(
        pg, lambda: service.list_my_listings(pg.installer_ws, pg.member_id)
    )
    assert member_mine.status_code == 403


# ---------- 浏览 ----------


async def test_browse_lists_only_listed_with_install_state(pg) -> None:
    skill = await _save_skill(pg, f"theta-{pg.suffix}")
    agent = await _create_agent(pg, f"浏览助手{pg.suffix}")
    hidden = await _save_skill(pg, f"iota-{pg.suffix}")
    skill_listing = await _publish(pg, "skill", skill.id, category="coding")
    agent_listing = await _publish(pg, "agent", agent.id, category="office")
    await _submit(pg, "skill", hidden.id)
    official_id = await _create_official_listing(pg, f"official-{pg.suffix}")
    service = MarketService(pg.db)
    await service.install_listing(pg.installer_ws, pg.installer_id, skill_listing)

    async def browse(**filters: Any):
        query: dict[str, Any] = {"kind": None, "category": None, "keyword": pg.suffix}
        query.update(filters)
        return await service.list_listings(
            pg.installer_ws, pg.installer_id, limit=20, offset=0, **query
        )

    page = await browse()
    ids = [item.id for item in page.items]
    assert page.total == 3
    # 官方的在前，其余按更新时间倒序；待审的不出现。
    assert ids == [official_id, agent_listing, skill_listing]
    items = {item.id: item for item in page.items}
    assert items[official_id].publisher_name == "官方"
    assert items[agent_listing].publisher_name == f"发布工作区{pg.suffix}"
    assert items[skill_listing].installed is True
    assert items[skill_listing].installed_version_number == 1
    assert items[skill_listing].install_count == 1
    assert items[skill_listing].has_update is False
    assert items[agent_listing].installed is False
    assert items[agent_listing].install_count == 0

    assert [item.id for item in (await browse(kind="agent")).items] == [agent_listing]
    assert [item.id for item in (await browse(category="coding")).items] == [skill_listing]
    # keyword 同时匹配 summary。
    assert (await browse(keyword=f"浏览助手{pg.suffix}的描述")).total == 1
    second_page = await service.list_listings(
        pg.installer_ws,
        pg.installer_id,
        kind=None,
        category=None,
        keyword=pg.suffix,
        limit=1,
        offset=1,
    )
    assert [item.id for item in second_page.items] == [agent_listing]


async def test_install_record_with_deleted_target_counts_as_not_installed(pg) -> None:
    skill = await _save_skill(pg, f"kappa-{pg.suffix}")
    listing_id = await _publish(pg, "skill", skill.id)
    service = MarketService(pg.db)
    first = await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)

    again = await _expect_error(
        pg, lambda: service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    )
    assert again.status_code == 409
    assert again.message == "已安装，请使用升级"

    await SkillService(pg.db).delete_skill(
        pg.installer_ws, first.install.target_skill_id, pg.installer_id
    )
    detail = await _installer_detail(pg, listing_id)
    assert detail.listing.installed is False
    assert detail.install_preview is not None
    assert detail.install_preview.mode == "install"
    assert _outcomes(detail.install_preview.report) == {f"kappa-{pg.suffix}": "restore"}

    # 重新安装复用原来那条安装记录，软删的 Skill 原地恢复。
    second = await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    assert second.install.id == first.install.id
    assert second.install.target_skill_id == first.install.target_skill_id
    assert _outcomes(second.report) == {f"kappa-{pg.suffix}": "restore"}


# ---------- 安装 ----------


async def test_skill_listing_rejects_same_name_even_with_same_content(pg) -> None:
    name = f"lambda-{pg.suffix}"
    skill = await _save_skill(pg, name)
    listing_id = await _publish(pg, "skill", skill.id)
    await _save_skill(pg, name, workspace_id=pg.installer_ws)
    service = MarketService(pg.db)

    detail = await _installer_detail(pg, listing_id)
    error = await _expect_error(
        pg, lambda: service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    )

    assert detail.install_preview is not None
    assert detail.install_preview.can_proceed is False
    assert _outcomes(detail.install_preview.report) == {name: "conflict"}
    assert error.status_code == 409
    assert name in error.message


async def test_agent_install_covers_create_restore_reuse_and_drops_tools(pg) -> None:
    created = f"created-{pg.suffix}"
    restored = f"restored-{pg.suffix}"
    reused = f"reused-{pg.suffix}"
    skills = []
    for name in (created, restored, reused):
        skills.append(await _save_skill(pg, name))
    agent = await _create_agent(
        pg,
        f"数据分析师{pg.suffix}",
        tools=("Read", "Grep", "Write"),
        skill_ids=tuple(skill.id for skill in skills),
        model_name="author-model",
    )
    listing_id = await _publish(pg, "agent", agent.id)

    # 安装方工作区：一个同名软删的 Skill（将被恢复），一个同名同内容的 Skill（将被复用）。
    deleted = await _save_skill(pg, restored, body="旧正文", workspace_id=pg.installer_ws)
    await SkillRepository(pg.db).soft_delete(deleted)
    local_reused = await _save_skill(pg, reused, workspace_id=pg.installer_ws)
    write = await ToolCatalogService(pg.db).get_required(pg.tool_ids["Write"])
    write.is_enabled = False
    await pg.db.flush()

    detail = await _installer_detail(pg, listing_id)
    preview = detail.install_preview
    assert preview is not None
    assert (preview.mode, preview.can_proceed, preview.agent_name_conflict) == (
        "install",
        True,
        False,
    )
    assert _outcomes(preview.report) == {created: "create", restored: "restore", reused: "reuse"}

    result = await MarketService(pg.db).install_listing(
        pg.installer_ws, pg.installer_id, listing_id
    )

    report = result.report
    # 预览看到的就是实际发生的。
    assert _outcomes(report) == _outcomes(preview.report)
    assert sorted(report.bound_tool_keys) == ["Grep", "Read"]
    assert [(tool.tool_key, tool.reason) for tool in report.dropped_tools] == [
        ("Write", "disabled")
    ]
    assert report.dropped_tools == preview.report.dropped_tools
    skill_ids = {skill.name: skill.skill_id for skill in report.skills}
    assert skill_ids[restored] == deleted.id
    assert skill_ids[reused] == local_reused.id
    # 托管 Skill 只有这次新建或恢复的。
    assert sorted(result.install.managed_skill_ids) == sorted([skill_ids[created], deleted.id])
    installed = await AgentRepository(pg.db).get(pg.installer_ws, result.install.target_agent_id)
    assert installed is not None
    assert (installed.name, installed.model_name, installed.is_enabled) == (
        f"数据分析师{pg.suffix}",
        None,
        True,
    )
    tool_ids, bound_skill_ids = await _bound_ids(pg, installed.id)
    assert tool_ids == {pg.tool_ids["Read"], pg.tool_ids["Grep"]}
    assert bound_skill_ids == set(skill_ids.values())


async def test_agent_install_rejects_skill_conflict_and_same_name_agent(pg) -> None:
    fresh = f"fresh-{pg.suffix}"
    clash = f"clash-{pg.suffix}"
    fresh_skill = await _save_skill(pg, fresh)
    clash_skill = await _save_skill(pg, clash)
    agent_name = f"冲突助手{pg.suffix}"
    agent = await _create_agent(pg, agent_name, skill_ids=(fresh_skill.id, clash_skill.id))
    listing_id = await _publish(pg, "agent", agent.id)
    await _save_skill(pg, clash, body="本地改过的正文", workspace_id=pg.installer_ws)
    service = MarketService(pg.db)

    preview = (await _installer_detail(pg, listing_id)).install_preview
    conflict = await _expect_error(
        pg, lambda: service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    )

    assert preview is not None and preview.can_proceed is False
    assert _outcomes(preview.report) == {fresh: "create", clash: "conflict"}
    assert conflict.status_code == 409
    assert clash in conflict.message
    # 整个安装被拒，排在前面的 Skill 也没有建出来。
    assert await SkillRepository(pg.db).get_by_name(pg.installer_ws, fresh) is None
    assert await AgentService(pg.db).is_name_taken(pg.installer_ws, agent_name) is False

    # 同名 Agent 被拒。
    await SkillService(pg.db).delete_skill(
        pg.installer_ws,
        (await SkillRepository(pg.db).get_by_name(pg.installer_ws, clash)).id,
        pg.installer_id,
    )
    await _create_agent(pg, agent_name, workspace_id=pg.installer_ws)
    preview = (await _installer_detail(pg, listing_id)).install_preview
    same_name = await _expect_error(
        pg, lambda: service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    )
    assert preview is not None and preview.agent_name_conflict is True
    assert same_name.status_code == 409
    assert agent_name in same_name.message


async def test_agent_install_rolls_back_when_failing_midway(pg, monkeypatch) -> None:
    name = f"rollback-{pg.suffix}"
    skill = await _save_skill(pg, name)
    agent = await _create_agent(pg, f"回滚助手{pg.suffix}", skill_ids=(skill.id,))
    listing_id = await _publish(pg, "agent", agent.id)

    async def failing_create_agent(self, **kwargs: Any) -> AgentRecord:
        raise AgentException.message("模拟创建 Agent 失败", status_code=409)

    # Skill 已经新建之后，创建 Agent 这一步失败。
    monkeypatch.setattr(AgentService, "create_agent", failing_create_agent)
    error = await _expect_error(
        pg,
        lambda: MarketService(pg.db).install_listing(pg.installer_ws, pg.installer_id, listing_id),
    )

    assert error.message == "模拟创建 Agent 失败"
    assert await SkillRepository(pg.db).get_by_name(pg.installer_ws, name) is None
    assert await MarketRepository(pg.db).get_install(listing_id, pg.installer_ws) is None


async def test_install_requires_write_role(pg) -> None:
    skill = await _save_skill(pg, f"mu-{pg.suffix}")
    listing_id = await _publish(pg, "skill", skill.id)

    error = await _expect_error(
        pg,
        lambda: MarketService(pg.db).install_listing(pg.installer_ws, pg.member_id, listing_id),
    )

    assert error.status_code == 403


# ---------- 升级 ----------


async def _edit_skill(env: SimpleNamespace, skill_id: int, body: str) -> None:
    await SkillService(env.db).update_from_fields(
        env.publisher_ws,
        skill_id,
        env.author_id,
        name=None,
        description="新描述",
        body=body,
        version=None,
        compatibility=None,
        metadata=None,
    )


async def test_agent_upgrade_overwrites_managed_unbinds_removed_and_keeps_local(pg) -> None:
    kept = f"kept-{pg.suffix}"
    dropped = f"dropped-{pg.suffix}"
    added = f"added-{pg.suffix}"
    kept_skill = await _save_skill(pg, kept)
    dropped_skill = await _save_skill(pg, dropped)
    added_skill = await _save_skill(pg, added)
    agent = await _create_agent(
        pg, f"升级助手{pg.suffix}", tools=("Read",), skill_ids=(kept_skill.id, dropped_skill.id)
    )
    listing_id = await _publish(pg, "agent", agent.id)
    service = MarketService(pg.db)
    installed = await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    installed_ids = {skill.name: skill.skill_id for skill in installed.report.skills}
    target_id = installed.install.target_agent_id

    # 安装方修改本地设置；另有一个 Agent 也绑定了托管的 kept。
    local_name = f"我的助手{pg.suffix}"
    await AgentService(pg.db).update_agent(
        pg.installer_ws,
        target_id,
        pg.installer_id,
        {"name": local_name, "is_enabled": False, "model_name": "local-model"},
    )
    other = await _create_agent(
        pg,
        f"旁观者{pg.suffix}",
        workspace_id=pg.installer_ws,
        skill_ids=(installed_ids[kept],),
    )

    # 作者发布第二版：改 kept 的内容，去掉 dropped，加上 added，换工具和提示词。
    await _edit_skill(pg, kept_skill.id, "第二版正文")
    author_agents = AgentService(pg.db)
    await author_agents.replace_skill_bindings(
        pg.publisher_ws, agent.id, pg.author_id, [kept_skill.id, added_skill.id]
    )
    await author_agents.replace_tool_bindings(
        pg.publisher_ws, agent.id, pg.author_id, [pg.tool_ids["Grep"]]
    )
    await author_agents.update_agent(
        pg.publisher_ws,
        agent.id,
        pg.author_id,
        {"system_prompt": "第二版提示词", "description": "第二版描述"},
    )
    await _publish(pg, "agent", agent.id)

    preview = (await _installer_detail(pg, listing_id)).install_preview
    assert preview is not None
    assert (preview.mode, preview.installed_version_number, preview.target_version_number) == (
        "upgrade",
        1,
        2,
    )
    expected = {kept: "overwrite", added: "create", dropped: "unbind"}
    assert _outcomes(preview.report) == expected

    result = await service.upgrade_listing(pg.installer_ws, pg.installer_id, listing_id)

    assert _outcomes(result.report) == expected
    added_id = {skill.name: skill.skill_id for skill in result.report.skills}[added]
    assert result.install.installed_version_number == 2
    assert sorted(result.install.managed_skill_ids) == sorted([installed_ids[kept], added_id])
    upgraded = await AgentRepository(pg.db).get(pg.installer_ws, target_id)
    assert upgraded is not None
    assert (upgraded.name, upgraded.is_enabled, upgraded.model_name) == (
        local_name,
        False,
        "local-model",
    )
    assert (upgraded.system_prompt, upgraded.description) == ("第二版提示词", "第二版描述")
    tool_ids, skill_ids = await _bound_ids(pg, target_id)
    assert tool_ids == {pg.tool_ids["Grep"]}
    assert skill_ids == {installed_ids[kept], added_id}
    # 托管 Skill 原地覆盖，绑定它的其他 Agent 也看到新内容；被移除的只解绑、不删除。
    kept_record = await SkillRepository(pg.db).get_by_id(pg.installer_ws, installed_ids[kept])
    assert kept_record is not None and "第二版正文" in kept_record.content
    assert (await _bound_ids(pg, other.id))[1] == {installed_ids[kept]}
    assert await SkillRepository(pg.db).get_by_id(pg.installer_ws, installed_ids[dropped])


async def test_skill_upgrade_overwrites_content(pg) -> None:
    name = f"nu-{pg.suffix}"
    skill = await _save_skill(pg, name)
    listing_id = await _publish(pg, "skill", skill.id)
    service = MarketService(pg.db)
    installed = await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    await _edit_skill(pg, skill.id, "第二版正文")
    await _publish(pg, "skill", skill.id)

    result = await service.upgrade_listing(pg.installer_ws, pg.installer_id, listing_id)

    assert result.install.installed_version_number == 2
    assert _outcomes(result.report) == {name: "overwrite"}
    record = await SkillRepository(pg.db).get_by_id(
        pg.installer_ws, installed.install.target_skill_id
    )
    assert record is not None and "第二版正文" in record.content


async def test_upgrade_failure_paths(pg) -> None:
    clash = f"xi-{pg.suffix}"
    agent = await _create_agent(pg, f"受阻助手{pg.suffix}")
    listing_id = await _publish(pg, "agent", agent.id)
    service = MarketService(pg.db)

    not_installed = await _expect_error(
        pg, lambda: service.upgrade_listing(pg.installer_ws, pg.installer_id, listing_id)
    )
    await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    latest = await _expect_error(
        pg, lambda: service.upgrade_listing(pg.installer_ws, pg.installer_id, listing_id)
    )
    assert not_installed.status_code == 409
    assert latest.status_code == 409
    assert latest.message == "已是最新版本"

    # 新版本带了一个 Skill，安装方本地已有同名但内容不同的：拒绝整个升级，什么都不改。
    clash_skill = await _save_skill(pg, clash)
    await AgentService(pg.db).replace_skill_bindings(
        pg.publisher_ws, agent.id, pg.author_id, [clash_skill.id]
    )
    await _publish(pg, "agent", agent.id)
    await _save_skill(pg, clash, body="本地版本", workspace_id=pg.installer_ws)
    conflict = await _expect_error(
        pg, lambda: service.upgrade_listing(pg.installer_ws, pg.installer_id, listing_id)
    )
    assert conflict.status_code == 409
    assert clash in conflict.message
    install = await MarketRepository(pg.db).get_install(listing_id, pg.installer_ws)
    assert install is not None and install.installed_version_number == 1
