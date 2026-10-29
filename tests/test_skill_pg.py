"""skill 域的真实 PostgreSQL 集成测试:JSONB 往返、延迟加载、启用规则与软删过滤。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库,整个用例包在外层事务里,结束时回滚,不留数据。
库不可达时跳过并给出原因。
"""

import uuid

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException
from skill.parser import SkillBundle, encode_file
from skill.repository import SkillRepository
from skill.service import SkillService
from user.models import UserRecord
from workspace.models import WorkspaceRecord

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
    suffix = uuid.uuid4().hex[:12]
    users = [
        UserRecord(email=f"skill-{i}-{suffix}@test.local", username=f"skill-{i}-{suffix}",
                   password="x")
        for i in range(2)
    ]
    workspace = WorkspaceRecord(name=f"skill-{suffix}", slug=f"skill-{suffix}",
                                display_name="skill 测试")
    session.add_all([*users, workspace])
    await session.flush()
    try:
        yield session, workspace.id, users[0].id, users[1].id
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _upsert(repo: SkillRepository, workspace_id: int, user_id: int, bundle: SkillBundle):
    return await repo.upsert(
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


def _bundle(name: str = "demo", extra_files: dict | None = None) -> SkillBundle:
    content = f"---\nname: {name}\ndescription: 中文描述\nversion: 1.0.0\n---\n\n正文\n"
    return SkillBundle.from_content(content, extra_files or {})


async def test_extra_files_jsonb_round_trip_and_deferred_listing(pg):
    session, workspace_id, user_id, _ = pg
    repo = SkillRepository(session)
    extra = {
        "references/中文.md": encode_file("# 参考\n".encode("utf-8")),
        "assets/a.bin": encode_file(b"\x00\xff\x01"),
    }
    await _upsert(repo, workspace_id, user_id, _bundle(extra_files=extra))
    session.expunge_all()

    (listed,) = await repo.list_all(workspace_id)
    unloaded = sa_inspect(listed).unloaded
    loaded = await repo.get_by_name(workspace_id, "demo")

    assert {"content", "extra_files"} <= unloaded
    assert loaded is listed
    assert loaded.extra_files == extra
    assert loaded.content.startswith("---\nname: demo")


async def test_extra_files_in_place_mutation_is_persisted(pg):
    session, workspace_id, user_id, _ = pg
    repo = SkillRepository(session)
    record = await _upsert(repo, workspace_id, user_id, _bundle())

    record.extra_files["notes.md"] = encode_file(b"later")
    await session.flush()
    session.expunge_all()

    reloaded = await repo.get_by_name(workspace_id, "demo")
    assert reloaded.extra_files == {"notes.md": encode_file(b"later")}


async def test_upsert_enable_rules_and_soft_delete_revival(pg):
    session, workspace_id, creator_id, other_id = pg
    repo = SkillRepository(session)
    created = await _upsert(repo, workspace_id, creator_id, _bundle())
    assert (created.is_enabled, created.created_by_user_id) == (True, creator_id)

    await repo.set_enabled(created, False)
    updated = await _upsert(
        repo, workspace_id, other_id, _bundle(extra_files={"a.txt": encode_file(b"a")})
    )
    assert (updated.is_enabled, updated.created_by_user_id) == (False, creator_id)

    await repo.soft_delete(updated)
    assert await repo.get_by_name(workspace_id, "demo") is None
    assert await repo.list_all(workspace_id) == []

    revived = await _upsert(repo, workspace_id, other_id, _bundle())
    assert revived.id == created.id
    assert (revived.is_deleted, revived.is_enabled) == (False, True)
    assert revived.created_by_user_id == other_id
    assert revived.extra_files == {}


async def test_disabled_skill_is_filtered_from_runtime_paths(pg):
    session, workspace_id, user_id, _ = pg
    repo = SkillRepository(session)
    enabled = await _upsert(repo, workspace_id, user_id, _bundle("enabled-one"))
    disabled = await _upsert(repo, workspace_id, user_id, _bundle("disabled-one"))
    await repo.set_enabled(disabled, False)
    service = SkillService(session)

    assert await service.get_catalog(workspace_id) == "- enabled-one: 中文描述"
    assert await service.get_catalog(workspace_id, [disabled.id]) == "(当前没有可用的 skill)"
    assert await service.get_enabled_names_by_ids(
        workspace_id, [disabled.id, enabled.id]
    ) == ("enabled-one",)
    assert await service.require_bindable_skill_ids(workspace_id, [disabled.id]) == [disabled.id]
    assert {(r.name, r.is_enabled) for r in await service.list_skills(workspace_id)} == {
        ("enabled-one", True),
        ("disabled-one", False),
    }
    with pytest.raises(AgentException) as exc_info:
        await service.load_body(workspace_id, "disabled-one")
    assert exc_info.value.status_code == 404


async def test_skill_http_api_end_to_end(pg):
    """经 FastAPI 路由走完整链路;依赖覆盖为同一回滚事务内的会话,不写入真实数据。"""
    import io
    import zipfile
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import httpx

    from api.deps import get_current_active_user, get_current_workspace_id, get_db
    from main import app
    from workspace.models import WorkspaceMemberRecord

    session, workspace_id, user_id, _ = pg
    session.add(
        WorkspaceMemberRecord(
            workspace_id=workspace_id, user_id=user_id, role="owner",
            joined_at=datetime.now(UTC),
        )
    )
    await session.flush()

    async def _db():
        yield session

    app.dependency_overrides.update(
        {
            get_db: _db,
            get_current_active_user: lambda: SimpleNamespace(id=user_id),
            get_current_workspace_id: lambda: workspace_id,
        }
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("demo/SKILL.md", "---\nname: demo\ndescription: d\n---\n\n正文\n")
        zf.writestr("demo/scripts/run.py", "print(1)\n")
    files = {"file": ("demo.zip", buffer.getvalue(), "application/zip")}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            uploaded = (await client.post("/api/skills", files=files)).json()["data"]
            put = await client.put(
                "/api/skills/demo", json={"description": "新描述", "body": "新正文"}
            )
            patched = await client.patch("/api/skills/demo/enabled", json={"enabled": False})
            listed = (await client.get("/api/skills")).json()["data"]
            detail = (await client.get("/api/skills/demo")).json()["data"]
            exported = await client.get("/api/skills/demo/export")
            invalid = await client.put("/api/skills/demo", json={"description": "d", "body": " "})
            deleted = (await client.delete("/api/skills/demo")).json()["data"]
            missing = await client.get("/api/skills/demo")
    finally:
        app.dependency_overrides.clear()

    assert (uploaded["name"], uploaded["is_enabled"]) == ("demo", True)
    assert put.json()["data"]["description"] == "新描述"
    assert patched.json()["data"]["is_enabled"] is False
    assert [(s["name"], s["is_enabled"]) for s in listed] == [("demo", False)]
    assert detail["body"] == "新正文"
    assert [r["relative_path"] for r in detail["resources"]] == ["scripts/run.py"]
    assert exported.headers["content-type"] == "application/zip"
    assert 'filename="demo.zip"' in exported.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(exported.content)) as zf:
        assert sorted(zf.namelist()) == ["demo/SKILL.md", "demo/scripts/run.py"]
    assert invalid.status_code == 422
    assert deleted == {"deleted": True}
    assert missing.status_code == 404
