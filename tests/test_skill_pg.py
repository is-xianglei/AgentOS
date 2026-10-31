"""skill 域的真实 PostgreSQL 集成测试:JSONB 往返、延迟加载、启用与复活规则、软删过滤、
唯一约束冲突和按 ID 定位的 HTTP 路由。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库,整个用例包在外层事务里,结束时回滚,不留数据。
库不可达时跳过并给出原因。
"""

import io
import uuid
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException
from skill.models import SkillRecord
from skill.parser import SkillBundle, encode_file
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


async def _save(
    repo: SkillRepository,
    record: SkillRecord | None,
    workspace_id: int,
    user_id: int,
    bundle: SkillBundle,
) -> SkillRecord:
    return await repo.save(
        record,
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


async def _add_owner(session: AsyncSession, workspace_id: int, user_id: int) -> None:
    session.add(
        WorkspaceMemberRecord(
            workspace_id=workspace_id, user_id=user_id, role="owner",
            joined_at=datetime.now(UTC),
        )
    )
    await session.flush()


def _zip_files(files: dict[str, str]) -> dict[str, tuple[str, bytes, str]]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return {"file": ("skill.zip", buffer.getvalue(), "application/zip")}


async def test_extra_files_jsonb_round_trip_and_deferred_listing(pg):
    session, workspace_id, user_id, _ = pg
    repo = SkillRepository(session)
    extra = {
        "references/中文.md": encode_file("# 参考\n".encode("utf-8")),
        "assets/a.bin": encode_file(b"\x00\xff\x01"),
    }
    await _save(repo, None, workspace_id, user_id, _bundle(extra_files=extra))
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
    record = await _save(repo, None, workspace_id, user_id, _bundle())

    record.extra_files["notes.md"] = encode_file(b"later")
    await session.flush()
    session.expunge_all()

    reloaded = await repo.get_by_name(workspace_id, "demo")
    assert reloaded.extra_files == {"notes.md": encode_file(b"later")}


async def test_save_enable_rules_and_soft_delete_revival(pg):
    session, workspace_id, creator_id, other_id = pg
    repo = SkillRepository(session)
    created = await _save(repo, None, workspace_id, creator_id, _bundle())
    assert (created.is_enabled, created.created_by_user_id) == (True, creator_id)

    await repo.set_enabled(created, False)
    updated = await _save(
        repo, created, workspace_id, other_id, _bundle(extra_files={"a.txt": encode_file(b"a")})
    )
    assert (updated.is_enabled, updated.created_by_user_id) == (False, creator_id)

    await repo.soft_delete(updated)
    assert await repo.get_by_name(workspace_id, "demo") is None
    assert await repo.get_by_id(workspace_id, created.id) is None
    assert await repo.list_all(workspace_id) == []
    locked = await repo.lock_by_name(workspace_id, "demo")
    assert locked is created and locked.is_deleted is True

    revived = await _save(repo, locked, workspace_id, other_id, _bundle())
    assert revived.id == created.id
    assert (revived.is_deleted, revived.is_enabled) == (False, True)
    assert revived.created_by_user_id == other_id
    assert revived.extra_files == {}


async def test_disabled_skill_is_filtered_from_runtime_paths(pg):
    session, workspace_id, user_id, _ = pg
    repo = SkillRepository(session)
    enabled = await _save(repo, None, workspace_id, user_id, _bundle("enabled-one"))
    disabled = await _save(repo, None, workspace_id, user_id, _bundle("disabled-one"))
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


async def test_concurrent_same_name_create_maps_unique_violation_to_409(pg, monkeypatch):
    """模拟并发新建:判重时看不到对方刚提交的同名行,写入撞唯一约束后应转成 409。"""
    session, workspace_id, user_id, _ = pg
    await _add_owner(session, workspace_id, user_id)
    original = await _save(SkillRepository(session), None, workspace_id, user_id, _bundle())
    service = SkillService(session)

    async def _miss(workspace_id: int, name: str) -> None:
        return None

    monkeypatch.setattr(service.repo, "lock_by_name", _miss)
    # 唯一约束失败会让事务进入 aborted 状态,包在 SAVEPOINT 内以便外层事务继续断言。
    nested = await session.begin_nested()
    with pytest.raises(AgentException) as exc_info:
        await service.create_from_fields(
            workspace_id, user_id, name="demo", description="d", body="正文",
            version=None, compatibility=None, metadata=None,
        )
    await nested.rollback()

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "skill 名称已存在: demo"
    assert isinstance(exc_info.value.__cause__, IntegrityError)
    assert (await SkillRepository(session).get_by_name(workspace_id, "demo")).id == original.id


async def test_skill_http_api_end_to_end(pg):
    """经 FastAPI 路由走完整链路;依赖覆盖为同一回滚事务内的会话,不写入真实数据。"""
    from api.deps import get_current_active_user, get_current_workspace_id, get_db
    from main import app

    session, workspace_id, user_id, _ = pg
    await _add_owner(session, workspace_id, user_id)
    other_workspace = WorkspaceRecord(
        name=f"skill-other-{uuid.uuid4().hex[:12]}",
        slug=f"skill-other-{uuid.uuid4().hex[:12]}",
        display_name="另一个工作区",
    )
    session.add(other_workspace)
    await session.flush()
    foreign = await _save(
        SkillRepository(session), None, other_workspace.id, user_id, _bundle("foreign")
    )

    async def _db():
        yield session

    app.dependency_overrides.update(
        {
            get_db: _db,
            get_current_active_user: lambda: SimpleNamespace(id=user_id),
            get_current_workspace_id: lambda: workspace_id,
        }
    )
    demo_md = "---\nname: demo\ndescription: d\nversion: 1.0.0\n---\n\n正文\n"
    demo_zip = {"demo/SKILL.md": demo_md, "demo/scripts/run.py": "print(1)\n"}
    other_zip = {"SKILL.md": "---\nname: other\ndescription: d\n---\n\n正文\n"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            uploaded = await client.post("/api/skills/upload", files=_zip_files(demo_zip))
            skill_id = uploaded.json()["data"]["id"]
            base = f"/api/skills/{skill_id}"
            dup_upload = await client.post("/api/skills/upload", files=_zip_files(demo_zip))
            dup_json = await client.post(
                "/api/skills", json={"name": "demo", "description": "d", "body": "b"}
            )
            created = await client.post(
                "/api/skills",
                json={
                    "name": "json-skill", "description": "JSON 新建", "body": "正文",
                    "version": "0.1.0", "metadata": {"作者": "张三"},
                },
            )
            put = await client.put(base, json={"description": "新描述", "body": "新正文"})
            put_detail = (await client.get(base)).json()["data"]
            renamed = await client.put(
                base, json={"name": "other", "description": "d", "body": "b"}
            )
            invalid = await client.put(base, json={"description": "d", "body": " "})
            mismatch = await client.put(f"{base}/bundle", files=_zip_files(other_zip))
            rebundled = await client.put(f"{base}/bundle", files=_zip_files(demo_zip))
            patched = await client.patch(f"{base}/enabled", json={"enabled": False})
            listed = (await client.get("/api/skills")).json()["data"]
            detail = (await client.get(base)).json()["data"]
            exported = await client.get(f"{base}/export")
            by_name = await client.get("/api/skills/demo")
            foreign_base = f"/api/skills/{foreign.id}"
            foreign_hits = [
                await client.get(foreign_base),
                await client.put(foreign_base, json={"description": "d", "body": "b"}),
                await client.put(f"{foreign_base}/bundle", files=_zip_files(demo_zip)),
                await client.patch(f"{foreign_base}/enabled", json={"enabled": False}),
                await client.get(f"{foreign_base}/export"),
            ]
            foreign_deleted = (await client.delete(foreign_base)).json()["data"]
            deleted = (await client.delete(base)).json()["data"]
            missing = await client.get(base)
            revived = await client.post("/api/skills/upload", files=_zip_files(demo_zip))
    finally:
        app.dependency_overrides.clear()

    assert uploaded.status_code == 200
    assert (uploaded.json()["data"]["name"], uploaded.json()["data"]["version"]) == ("demo", "1.0.0")
    for conflict in (dup_upload, dup_json):
        assert conflict.status_code == 409
        assert conflict.json()["error"] == {
            "message": "skill 名称已存在: demo",
            "details": {"name": "demo"},
        }
    assert created.status_code == 200
    assert (created.json()["data"]["name"], created.json()["data"]["version"]) == (
        "json-skill",
        "0.1.0",
    )
    assert put.json()["data"]["description"] == "新描述"
    assert put.json()["data"]["version"] == "1.0.0"
    assert put_detail["frontmatter"]["version"] == "1.0.0"
    assert put_detail["body"] == "新正文"
    assert [r["relative_path"] for r in put_detail["resources"]] == ["scripts/run.py"]
    assert renamed.status_code == 400
    assert renamed.json()["error"]["details"] == {"name": "demo", "requested_name": "other"}
    assert invalid.status_code == 422
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["details"] == {"name": "demo", "bundle_name": "other"}
    assert rebundled.json()["data"]["description"] == "d"
    assert patched.json()["data"]["is_enabled"] is False
    assert {(s["name"], s["is_enabled"]) for s in listed} == {("demo", False), ("json-skill", True)}
    assert detail["body"] == "正文"
    assert exported.headers["content-type"] == "application/zip"
    assert 'filename="demo.zip"' in exported.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(exported.content)) as zf:
        assert sorted(zf.namelist()) == ["demo/SKILL.md", "demo/scripts/run.py"]
    assert by_name.status_code == 422
    for hit in foreign_hits:
        assert hit.status_code == 404
        assert hit.json()["error"]["message"] == f"skill 不存在: {foreign.id}"
    assert foreign_deleted == {"deleted": False}
    assert (await SkillRepository(session).get_by_id(other_workspace.id, foreign.id)) is foreign
    assert deleted == {"deleted": True}
    assert missing.status_code == 404
    assert revived.status_code == 200
    assert (revived.json()["data"]["id"], revived.json()["data"]["is_enabled"]) == (skill_id, True)
