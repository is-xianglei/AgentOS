"""第二阶段两处遗留问题修正的真实 PostgreSQL 集成测试：
1. 已安装的 Agent 被删除后重装，复用到的旧托管 Skill 仍算托管，之后升级直接覆盖而不是冲突；
2. 升级的预览和报告列出升级后会被解绑的全部 Skill，包括本地额外加的和复用来的非托管 Skill。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库，整个用例包在外层事务里，结束时回滚，不留数据。
库不可达时跳过并给出原因。
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.models import AgentRecord
from agent.repository import AgentRepository
from agent.service import AgentService
from core.config import settings
from market.service import MarketService
from skill.models import SkillRecord
from skill.parser import SkillBundle
from skill.repository import SkillRepository
from skill.service import SkillService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio


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
    for role in ("author", "installer", "admin"):
        users[role] = UserRecord(
            email=f"mkfix-{role}-{suffix}@test.local",
            username=f"mkfix-{role}-{suffix}",
            password="x",
            is_platform_admin=role == "admin",
        )
    publisher = WorkspaceRecord(
        name=f"mkfix-pub-{suffix}", slug=f"mkfix-pub-{suffix}", display_name=f"发布{suffix}"
    )
    installer = WorkspaceRecord(
        name=f"mkfix-ins-{suffix}", slug=f"mkfix-ins-{suffix}", display_name=f"安装{suffix}"
    )
    session.add_all([*users.values(), publisher, installer])
    await session.flush()
    now = datetime.now(UTC)
    for workspace, user in ((publisher, users["author"]), (installer, users["installer"])):
        session.add(
            WorkspaceMemberRecord(
                workspace_id=workspace.id, user_id=user.id, role="owner", joined_at=now
            )
        )
    await session.flush()
    try:
        yield SimpleNamespace(
            db=session,
            suffix=suffix,
            author_id=users["author"].id,
            installer_id=users["installer"].id,
            admin_id=users["admin"].id,
            publisher_ws=publisher.id,
            installer_ws=installer.id,
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _save_skill(
    env: SimpleNamespace,
    name: str,
    *,
    body: str = "正文",
    workspace_id: int | None = None,
) -> SkillRecord:
    """直接落一个 Skill；默认在发布工作区，同名同正文的两份内容哈希相同。"""
    content = f"---\nname: {name}\ndescription: {name} 的描述\n---\n\n{body}\n"
    bundle = SkillBundle.from_content(content, {})
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


async def _create_author_agent(
    env: SimpleNamespace,
    name: str,
    skill_ids: list[int],
) -> AgentRecord:
    return await AgentService(env.db).create_agent(
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


async def _publish(env: SimpleNamespace, agent_id: int) -> int:
    """作者提交 Agent 并由平台管理员审核通过，返回条目 ID。"""
    service = MarketService(env.db)
    version = await service.submit(
        env.publisher_ws,
        env.author_id,
        kind="agent",
        source_id=agent_id,
        category="office",
        release_note=None,
    )
    await service.approve_version(env.admin_id, version.id, None)
    return version.listing_id


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


async def _preview(env: SimpleNamespace, listing_id: int):
    detail = await MarketService(env.db).get_listing_detail(
        env.installer_ws, env.installer_id, listing_id, is_platform_admin=False
    )
    assert detail.install_preview is not None
    return detail.install_preview


async def _bound_skill_ids(env: SimpleNamespace, agent_id: int) -> set[int]:
    agent = await AgentRepository(env.db).get(env.installer_ws, agent_id)
    assert agent is not None
    return {binding.skill_id for binding in agent.skill_bindings}


def _outcomes(report) -> dict[str, str]:
    return {skill.name: skill.action for skill in report.skills}


def _skill_ids(report) -> dict[str, int | None]:
    return {skill.name: skill.skill_id for skill in report.skills}


async def test_reinstall_keeps_reused_managed_skill_and_upgrade_overwrites(pg) -> None:
    managed = f"managed-{pg.suffix}"
    local = f"local-{pg.suffix}"
    managed_skill = await _save_skill(pg, managed)
    local_skill = await _save_skill(pg, local)
    agent = await _create_author_agent(
        pg, f"纪要助手{pg.suffix}", [managed_skill.id, local_skill.id]
    )
    listing_id = await _publish(pg, agent.id)
    # 安装方本地原本就有一份同内容的 local：第一次安装时被复用，不归市场托管。
    own_local = await _save_skill(pg, local, workspace_id=pg.installer_ws)
    service = MarketService(pg.db)

    first = await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)

    assert _outcomes(first.report) == {managed: "create", local: "reuse"}
    managed_id = _skill_ids(first.report)[managed]
    assert first.install.managed_skill_ids == [managed_id]

    # 删除装出来的 Agent 就相当于卸载；托管 Skill 还留在工作区里。
    await AgentService(pg.db).delete_agent(
        pg.installer_ws, first.install.target_agent_id, pg.installer_id
    )
    preview = await _preview(pg, listing_id)
    assert (preview.mode, preview.can_proceed) == ("install", True)
    assert _outcomes(preview.report) == {managed: "reuse", local: "reuse"}

    second = await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)

    # 复用原来那条安装记录；原来托管的 Skill 被原样复用，仍算托管，本地原有的 local 仍不算。
    assert second.install.id == first.install.id
    assert _outcomes(second.report) == _outcomes(preview.report)
    assert second.install.managed_skill_ids == [managed_id]
    assert _skill_ids(second.report)[local] == own_local.id

    # 作者改了 managed 的内容并发布第二版：升级直接覆盖，不会因为「同名但内容不同」被拒。
    await _edit_skill(pg, managed_skill.id, "第二版正文")
    await _publish(pg, agent.id)
    preview = await _preview(pg, listing_id)
    assert (preview.mode, preview.can_proceed) == ("upgrade", True)
    assert _outcomes(preview.report) == {managed: "overwrite", local: "reuse"}

    result = await service.upgrade_listing(pg.installer_ws, pg.installer_id, listing_id)

    assert _outcomes(result.report) == _outcomes(preview.report)
    assert result.install.installed_version_number == 2
    assert result.install.managed_skill_ids == [managed_id]
    record = await SkillRepository(pg.db).get_by_id(pg.installer_ws, managed_id)
    assert record is not None and "第二版正文" in record.content


async def test_upgrade_report_lists_every_skill_that_gets_unbound(pg) -> None:
    kept = f"kept-{pg.suffix}"
    dropped = f"dropped-{pg.suffix}"
    reused = f"reused-{pg.suffix}"
    shared = f"shared-{pg.suffix}"
    author_skills: dict[str, SkillRecord] = {}
    for name in (kept, dropped, reused, shared):
        author_skills[name] = await _save_skill(pg, name)
    agent = await _create_author_agent(
        pg, f"升级助手{pg.suffix}", [skill.id for skill in author_skills.values()]
    )
    listing_id = await _publish(pg, agent.id)
    # 安装方本地已有同内容的 reused 和 shared，安装时被复用，不归市场托管。
    local_reused = await _save_skill(pg, reused, workspace_id=pg.installer_ws)
    local_shared = await _save_skill(pg, shared, workspace_id=pg.installer_ws)
    service = MarketService(pg.db)
    installed = await service.install_listing(pg.installer_ws, pg.installer_id, listing_id)
    installed_ids = _skill_ids(installed.report)
    target_id = installed.install.target_agent_id

    # 安装方给装出来的 Agent 额外绑定一个本地 Skill。
    extra = f"extra-{pg.suffix}"
    extra_skill = await _save_skill(pg, extra, workspace_id=pg.installer_ws)
    await AgentService(pg.db).replace_skill_bindings(
        pg.installer_ws,
        target_id,
        pg.installer_id,
        [*installed_ids.values(), extra_skill.id],
    )

    # 作者发布第二版：只保留 kept 和 shared。
    await AgentService(pg.db).replace_skill_bindings(
        pg.publisher_ws,
        agent.id,
        pg.author_id,
        [author_skills[kept].id, author_skills[shared].id],
    )
    await _publish(pg, agent.id)

    expected = {
        kept: "overwrite",
        shared: "reuse",
        dropped: "unbind",
        reused: "unbind",
        extra: "unbind",
    }
    preview = await _preview(pg, listing_id)
    assert preview.can_proceed is True
    assert _outcomes(preview.report) == expected

    result = await service.upgrade_listing(pg.installer_ws, pg.installer_id, listing_id)

    # 预览看到的就是实际发生的；报告里的解绑项与升级后的实际绑定一致。
    assert _outcomes(result.report) == expected
    assert _skill_ids(result.report) == _skill_ids(preview.report)
    report_ids = _skill_ids(result.report)
    assert report_ids[reused] == local_reused.id
    assert report_ids[extra] == extra_skill.id
    assert report_ids[shared] == local_shared.id
    assert await _bound_skill_ids(pg, target_id) == {installed_ids[kept], local_shared.id}
    # 行为不变：托管列表只剩 kept，被解绑的 Skill 都还在。
    assert result.install.managed_skill_ids == [installed_ids[kept]]
    existing = await SkillService(pg.db).filter_existing_ids(
        pg.installer_ws, [installed_ids[dropped], local_reused.id, extra_skill.id]
    )
    assert existing == {installed_ids[dropped], local_reused.id, extra_skill.id}
