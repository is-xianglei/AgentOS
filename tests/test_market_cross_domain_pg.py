"""市场后续要用的跨域公开方法的真实 PostgreSQL 集成测试：
tools 的 tool_key 与 ID 互转、workspace 批量展示名、skill 的快照与安装判定、
agent 的快照、同名判断、存在性判断和升级覆盖。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库，整个用例包在外层事务里，结束时回滚，不留数据。
库不可达时跳过并给出原因。
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.models import AgentRecord
from agent.service import AgentDefinitionService
from core.config import settings
from core.errors import AgentException
from skill.models import SkillRecord
from skill.parser import SkillBundle, encode_file
from skill.repository import SkillRepository
from skill.service import SkillInstallDecision, SkillService
from tools.models import ToolRecord
from tools.repository import ToolCatalogRepository
from tools.service import ToolCatalogService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord
from workspace.service import WorkspaceService

pytestmark = pytest.mark.anyio

# 内置工具的 tool_key 与 runtime_name 相同；SkillRun 不允许 user_agent 绑定。
_TOOL_NAMES = ("Read", "Write", "Grep", "SkillRun")


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
    suffix = uuid.uuid4().hex[:12]
    owner = UserRecord(
        email=f"market-owner-{suffix}@test.local", username=f"market-owner-{suffix}", password="x"
    )
    member = UserRecord(
        email=f"market-member-{suffix}@test.local", username=f"market-member-{suffix}", password="x"
    )
    workspaces = [
        WorkspaceRecord(
            name=f"market-{i}-{suffix}", slug=f"market-{i}-{suffix}", display_name=f"市场测试{i}"
        )
        for i in range(2)
    ]
    session.add_all([owner, member, *workspaces])
    await session.flush()
    now = datetime.now(UTC)
    session.add_all(
        [
            WorkspaceMemberRecord(
                workspace_id=workspaces[0].id, user_id=owner.id, role="owner", joined_at=now
            ),
            WorkspaceMemberRecord(
                workspace_id=workspaces[0].id, user_id=member.id, role="member", joined_at=now
            ),
            WorkspaceMemberRecord(
                workspace_id=workspaces[1].id, user_id=owner.id, role="owner", joined_at=now
            ),
        ]
    )
    await session.flush()
    try:
        yield SimpleNamespace(
            db=session,
            owner_id=owner.id,
            member_id=member.id,
            workspace_id=workspaces[0].id,
            other_workspace_id=workspaces[1].id,
            tool_ids=await _enabled_tool_ids(session),
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


def _bundle(name: str, body: str = "正文") -> SkillBundle:
    content = f"---\nname: {name}\ndescription: {name} 的描述\n---\n\n{body}\n"
    extra_files = {"scripts/run.py": encode_file(b"print('hi')\n")}
    return SkillBundle.from_content(content, extra_files)


def _snapshot(bundle: SkillBundle) -> dict[str, Any]:
    return {
        "name": bundle.name,
        "content": bundle.content,
        "extra_files": bundle.extra_files,
        "skill_hash": bundle.skill_hash,
    }


async def _save_skill(
    env: SimpleNamespace,
    bundle: SkillBundle,
    *,
    workspace_id: int | None = None,
    enabled: bool = True,
) -> SkillRecord:
    repo = SkillRepository(env.db)
    record = await repo.save(
        None,
        workspace_id=workspace_id or env.workspace_id,
        name=bundle.name,
        description=bundle.description,
        frontmatter=bundle.parsed.frontmatter,
        version=bundle.version,
        skill_hash=bundle.skill_hash,
        content=bundle.content,
        extra_files=bundle.extra_files,
        actor_user_id=env.owner_id,
    )
    if not enabled:
        await repo.set_enabled(record, False)
    return record


async def _create_agent(
    env: SimpleNamespace,
    name: str,
    *,
    workspace_id: int | None = None,
    tools: tuple[str, ...] = (),
    skill_ids: tuple[int, ...] = (),
    model_name: str | None = None,
    is_enabled: bool = True,
) -> AgentRecord:
    tool_ids: list[int] = []
    for tool in tools:
        tool_ids.append(env.tool_ids[tool])
    return await AgentDefinitionService(env.db).create_agent(
        workspace_id=workspace_id or env.workspace_id,
        actor_user_id=env.owner_id,
        name=name,
        description=f"{name}的描述",
        system_prompt=f"你是{name}",
        model_name=model_name,
        is_enabled=is_enabled,
        tool_ids=tool_ids,
        skill_ids=list(skill_ids),
    )


def _count_skill_saves(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """记录 SkillRepository.save 的调用，用来断言哪些路径真的写了库。"""
    saved_names: list[str] = []
    original_save = SkillRepository.save

    async def counting_save(self: SkillRepository, record: Any, **kwargs: Any) -> SkillRecord:
        saved_names.append(kwargs["name"])
        return await original_save(self, record, **kwargs)

    monkeypatch.setattr(SkillRepository, "save", counting_save)
    return saved_names


# ---------- tools：tool_key 与工具 ID 互转 ----------


async def test_resolve_tool_keys_gives_reason_for_each_unavailable_tool(pg) -> None:
    catalog = ToolCatalogService(pg.db)
    write = await catalog.get_required(pg.tool_ids["Write"])
    write.is_enabled = False
    # 目录里有、代码注册表里没有实现的工具，与 require_bindable_tool_ids 一样视为不可用。
    legacy_key = f"legacy-{uuid.uuid4().hex[:8]}"
    pg.db.add(
        ToolRecord(
            tool_key=legacy_key,
            name=legacy_key,
            description="已下线的工具",
            runtime_name=legacy_key,
            input_schema={},
            implementation_config={},
            is_enabled=True,
        )
    )
    await pg.db.flush()

    resolution = await catalog.resolve_tool_keys(
        ["Read", "Grep", "Write", "SkillRun", legacy_key, "NoSuchTool", "Read"],
        profile="user_agent",
    )

    assert resolution.tool_ids == {"Read": pg.tool_ids["Read"], "Grep": pg.tool_ids["Grep"]}
    assert resolution.unavailable == {
        "Write": "disabled",
        "SkillRun": "not_bindable",
        legacy_key: "not_found",
        "NoSuchTool": "not_found",
    }


async def test_resolve_tool_keys_agrees_with_require_bindable_tool_ids(pg) -> None:
    """可绑定的工具 require_bindable_tool_ids 放行，不可用的它逐个拒绝，两边口径一致。"""
    catalog = ToolCatalogService(pg.db)
    write = await catalog.get_required(pg.tool_ids["Write"])
    write.is_enabled = False
    await pg.db.flush()

    resolution = await catalog.resolve_tool_keys(
        ["Read", "Write", "SkillRun"],
        profile="user_agent",
    )

    bindable = await catalog.require_bindable_tool_ids(
        list(resolution.tool_ids.values()),
        profile="user_agent",
    )
    assert bindable == [pg.tool_ids["Read"]]
    assert set(resolution.unavailable) == {"Write", "SkillRun"}
    for tool_key in resolution.unavailable:
        with pytest.raises(AgentException):
            await catalog.require_bindable_tool_ids([pg.tool_ids[tool_key]], profile="user_agent")


async def test_get_tool_keys_by_ids_includes_disabled_and_skips_missing(pg) -> None:
    catalog = ToolCatalogService(pg.db)
    write = await catalog.get_required(pg.tool_ids["Write"])
    write.is_enabled = False
    await pg.db.flush()

    tool_keys = await catalog.get_tool_keys_by_ids(
        [pg.tool_ids["Read"], pg.tool_ids["Write"], -1, pg.tool_ids["Read"]]
    )

    assert tool_keys == {pg.tool_ids["Read"]: "Read", pg.tool_ids["Write"]: "Write"}


# ---------- workspace：批量展示名 ----------


async def test_get_display_names_by_ids_skips_deleted_and_missing(pg) -> None:
    suffix = uuid.uuid4().hex[:12]
    deleted = WorkspaceRecord(
        name=f"market-deleted-{suffix}",
        slug=f"market-deleted-{suffix}",
        display_name="已删除",
        is_deleted=True,
    )
    pg.db.add(deleted)
    await pg.db.flush()

    names = await WorkspaceService(pg.db).get_display_names_by_ids(
        [pg.workspace_id, pg.other_workspace_id, deleted.id, -1, pg.workspace_id]
    )

    assert names == {pg.workspace_id: "市场测试0", pg.other_workspace_id: "市场测试1"}


# ---------- skill：快照、安装判定与执行、覆盖、存在性 ----------


async def test_build_skill_snapshot_has_bundle_fields_even_when_disabled(pg) -> None:
    bundle = _bundle("data-cleaning")
    record = await _save_skill(pg, bundle, enabled=False)

    snapshot = await SkillService(pg.db).build_snapshot(pg.workspace_id, record.id)

    assert snapshot == _snapshot(bundle)


async def test_build_skill_snapshot_rejects_other_workspace(pg) -> None:
    record = await _save_skill(pg, _bundle("outsider"), workspace_id=pg.other_workspace_id)

    with pytest.raises(AgentException) as exc_info:
        await SkillService(pg.db).build_snapshot(pg.workspace_id, record.id)

    assert exc_info.value.status_code == 404


async def test_preview_install_covers_create_restore_reuse_conflict(pg) -> None:
    service = SkillService(pg.db)
    repo = SkillRepository(pg.db)
    restored = await _save_skill(pg, _bundle("restored"))
    await repo.soft_delete(restored)
    reused = await _save_skill(pg, _bundle("reused"))
    conflicted = await _save_skill(pg, _bundle("conflicted", body="本地改过的正文"))

    create = await service.preview_install_from_snapshot(
        pg.workspace_id, _snapshot(_bundle("brand-new"))
    )
    restore = await service.preview_install_from_snapshot(
        pg.workspace_id, _snapshot(_bundle("restored"))
    )
    reuse = await service.preview_install_from_snapshot(
        pg.workspace_id, _snapshot(_bundle("reused"))
    )
    conflict = await service.preview_install_from_snapshot(
        pg.workspace_id, _snapshot(_bundle("conflicted"))
    )

    assert create == SkillInstallDecision(name="brand-new", action="create", skill_id=None)
    assert restore == SkillInstallDecision(name="restored", action="restore", skill_id=restored.id)
    assert reuse == SkillInstallDecision(name="reused", action="reuse", skill_id=reused.id)
    assert conflict == SkillInstallDecision(
        name="conflicted", action="conflict", skill_id=conflicted.id
    )
    # 预览只读：要新建的没有落库，软删的仍是软删。
    assert await repo.get_by_name(pg.workspace_id, "brand-new") is None
    assert await repo.get_by_name(pg.workspace_id, "restored") is None


async def test_install_from_snapshot_creates_restores_and_reuses(pg, monkeypatch) -> None:
    service = SkillService(pg.db)
    repo = SkillRepository(pg.db)
    restored = await _save_skill(pg, _bundle("restored", body="删除前的旧正文"))
    await repo.soft_delete(restored)
    reused = await _save_skill(pg, _bundle("reused"))
    saved_names = _count_skill_saves(monkeypatch)

    create = await service.install_from_snapshot(
        pg.workspace_id, pg.owner_id, _snapshot(_bundle("brand-new"))
    )
    restore = await service.install_from_snapshot(
        pg.workspace_id, pg.owner_id, _snapshot(_bundle("restored"))
    )
    reuse = await service.install_from_snapshot(
        pg.workspace_id, pg.owner_id, _snapshot(_bundle("reused"))
    )

    created = await repo.get_by_name(pg.workspace_id, "brand-new")
    assert created is not None and created.is_enabled
    assert create == SkillInstallDecision(name="brand-new", action="create", skill_id=created.id)
    # 恢复沿用原 ID，内容换成快照里的。
    revived = await repo.get_by_id(pg.workspace_id, restored.id)
    assert restore == SkillInstallDecision(name="restored", action="restore", skill_id=restored.id)
    assert revived is not None
    assert revived.skill_hash == _bundle("restored").skill_hash
    # 复用不写库。
    assert reuse == SkillInstallDecision(name="reused", action="reuse", skill_id=reused.id)
    assert saved_names == ["brand-new", "restored"]


async def test_install_from_snapshot_rejects_conflict_without_writing(pg, monkeypatch) -> None:
    local = await _save_skill(pg, _bundle("conflicted", body="本地改过的正文"))
    saved_names = _count_skill_saves(monkeypatch)

    with pytest.raises(AgentException) as exc_info:
        await SkillService(pg.db).install_from_snapshot(
            pg.workspace_id, pg.owner_id, _snapshot(_bundle("conflicted"))
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"name": "conflicted"}
    assert saved_names == []
    current = await SkillRepository(pg.db).get_by_name(pg.workspace_id, "conflicted")
    assert current is not None and current.skill_hash == local.skill_hash


async def test_install_from_snapshot_requires_write_access(pg) -> None:
    with pytest.raises(AgentException) as exc_info:
        await SkillService(pg.db).install_from_snapshot(
            pg.workspace_id, pg.member_id, _snapshot(_bundle("brand-new"))
        )

    assert exc_info.value.status_code == 403


async def test_snapshot_with_mismatched_hash_is_rejected(pg) -> None:
    snapshot = _snapshot(_bundle("tampered"))
    snapshot["skill_hash"] = "0" * 64

    with pytest.raises(AgentException) as exc_info:
        await SkillService(pg.db).preview_install_from_snapshot(pg.workspace_id, snapshot)

    assert exc_info.value.status_code == 400


async def test_overwrite_from_snapshot_replaces_content_and_is_idempotent(pg, monkeypatch) -> None:
    service = SkillService(pg.db)
    record = await _save_skill(pg, _bundle("managed", body="第一版"))
    new_bundle = _bundle("managed", body="第二版")

    updated = await service.overwrite_from_snapshot(
        pg.workspace_id, record.id, pg.owner_id, _snapshot(new_bundle)
    )
    saved_names = _count_skill_saves(monkeypatch)
    again = await service.overwrite_from_snapshot(
        pg.workspace_id, record.id, pg.owner_id, _snapshot(new_bundle)
    )

    assert updated.id == record.id
    assert updated.skill_hash == new_bundle.skill_hash
    assert "第二版" in updated.content
    # 内容哈希没变时不写库。
    assert again.id == record.id
    assert saved_names == []


async def test_overwrite_from_snapshot_failure_paths(pg) -> None:
    service = SkillService(pg.db)
    record = await _save_skill(pg, _bundle("managed"))

    with pytest.raises(AgentException) as name_mismatch:
        await service.overwrite_from_snapshot(
            pg.workspace_id, record.id, pg.owner_id, _snapshot(_bundle("other-name"))
        )
    with pytest.raises(AgentException) as missing:
        await service.overwrite_from_snapshot(
            pg.workspace_id, -1, pg.owner_id, _snapshot(_bundle("managed"))
        )
    with pytest.raises(AgentException) as no_access:
        await service.overwrite_from_snapshot(
            pg.workspace_id, record.id, pg.member_id, _snapshot(_bundle("managed"))
        )

    assert name_mismatch.value.status_code == 400
    assert missing.value.status_code == 404
    assert no_access.value.status_code == 403


async def test_skill_filter_existing_ids(pg) -> None:
    live = await _save_skill(pg, _bundle("live"))
    disabled = await _save_skill(pg, _bundle("disabled"), enabled=False)
    deleted = await _save_skill(pg, _bundle("deleted"))
    await SkillRepository(pg.db).soft_delete(deleted)
    outsider = await _save_skill(pg, _bundle("outsider"), workspace_id=pg.other_workspace_id)

    existing = await SkillService(pg.db).filter_existing_ids(
        pg.workspace_id, [live.id, disabled.id, deleted.id, outsider.id, -1]
    )

    assert existing == {live.id, disabled.id}


# ---------- agent：快照、同名、存在性、升级覆盖 ----------


async def test_build_agent_snapshot(pg) -> None:
    beta = await _save_skill(pg, _bundle("beta"))
    alpha = await _save_skill(pg, _bundle("alpha"), enabled=False)
    agent = await _create_agent(
        pg,
        "数据分析师",
        tools=("Write", "Read"),
        skill_ids=(beta.id, alpha.id),
        model_name="author-model",
    )

    snapshot = await AgentDefinitionService(pg.db).build_snapshot(pg.workspace_id, agent.id)

    # 不带 model_name；已禁用的 Skill 也在内；两个列表都排好序。
    assert snapshot == {
        "agent": {
            "name": "数据分析师",
            "description": "数据分析师的描述",
            "system_prompt": "你是数据分析师",
            "tool_keys": ["Read", "Write"],
        },
        "skills": [_snapshot(_bundle("alpha")), _snapshot(_bundle("beta"))],
    }


async def test_build_agent_snapshot_rejects_other_workspace(pg) -> None:
    outsider = await _create_agent(pg, "别家", workspace_id=pg.other_workspace_id)

    with pytest.raises(AgentException) as exc_info:
        await AgentDefinitionService(pg.db).build_snapshot(pg.workspace_id, outsider.id)

    assert exc_info.value.status_code == 404


async def test_is_name_taken_counts_disabled_but_not_deleted(pg) -> None:
    service = AgentDefinitionService(pg.db)
    await _create_agent(pg, "在用")
    await _create_agent(pg, "已停用", is_enabled=False)
    deleted = await _create_agent(pg, "已删除")
    await service.delete_agent(pg.workspace_id, deleted.id, pg.owner_id)
    await _create_agent(pg, "别家", workspace_id=pg.other_workspace_id)

    assert await service.is_name_taken(pg.workspace_id, "在用") is True
    assert await service.is_name_taken(pg.workspace_id, "已停用") is True
    assert await service.is_name_taken(pg.workspace_id, "已删除") is False
    assert await service.is_name_taken(pg.workspace_id, "别家") is False


async def test_agent_filter_existing_ids(pg) -> None:
    service = AgentDefinitionService(pg.db)
    live = await _create_agent(pg, "在用")
    disabled = await _create_agent(pg, "已停用", is_enabled=False)
    deleted = await _create_agent(pg, "已删除")
    await service.delete_agent(pg.workspace_id, deleted.id, pg.owner_id)
    outsider = await _create_agent(pg, "别家", workspace_id=pg.other_workspace_id)

    existing = await service.filter_existing_ids(
        pg.workspace_id, [live.id, disabled.id, deleted.id, outsider.id, -1]
    )

    assert existing == {live.id, disabled.id}


async def test_overwrite_for_upgrade_keeps_local_settings(pg) -> None:
    old_skill = await _save_skill(pg, _bundle("old-skill"))
    new_skill = await _save_skill(pg, _bundle("new-skill"))
    agent = await _create_agent(
        pg,
        "本地名称",
        tools=("Read",),
        skill_ids=(old_skill.id,),
        model_name="local-model",
        is_enabled=False,
    )

    upgraded = await AgentDefinitionService(pg.db).overwrite_for_upgrade(
        pg.workspace_id,
        agent.id,
        pg.owner_id,
        description="新描述",
        system_prompt="新提示词",
        tool_ids=[pg.tool_ids["Grep"], pg.tool_ids["Write"]],
        skill_ids=[new_skill.id],
    )

    assert upgraded.name == "本地名称"
    assert upgraded.is_enabled is False
    assert upgraded.model_name == "local-model"
    assert upgraded.description == "新描述"
    assert upgraded.system_prompt == "新提示词"
    bound_tool_ids = {binding.tool_id for binding in upgraded.tool_bindings}
    bound_skill_ids = {binding.skill_id for binding in upgraded.skill_bindings}
    assert bound_tool_ids == {pg.tool_ids["Grep"], pg.tool_ids["Write"]}
    assert bound_skill_ids == {new_skill.id}


async def test_overwrite_for_upgrade_failure_paths(pg) -> None:
    service = AgentDefinitionService(pg.db)
    agent = await _create_agent(pg, "被升级")

    with pytest.raises(AgentException) as no_access:
        await service.overwrite_for_upgrade(
            pg.workspace_id, agent.id, pg.member_id,
            description="新描述", system_prompt="新提示词", tool_ids=[], skill_ids=[],
        )
    with pytest.raises(AgentException) as missing:
        await service.overwrite_for_upgrade(
            pg.workspace_id, -1, pg.owner_id,
            description="新描述", system_prompt="新提示词", tool_ids=[], skill_ids=[],
        )
    with pytest.raises(AgentException) as forbidden_tool:
        await service.overwrite_for_upgrade(
            pg.workspace_id, agent.id, pg.owner_id,
            description="新描述", system_prompt="新提示词",
            tool_ids=[pg.tool_ids["SkillRun"]], skill_ids=[],
        )

    assert no_access.value.status_code == 403
    assert missing.value.status_code == 404
    assert forbidden_tool.value.status_code == 403
