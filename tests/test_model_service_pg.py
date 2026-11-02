"""真实 PostgreSQL 验证工作区隔离、JSONB、软删除及 HTTP 契约。"""
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401
from core.config import settings
from core.errors import AgentException
from model_service.schemas import ModelProviderInput, ModelProviderUpdate, ModelTestResponse
from model_service.service import ModelService
from user.models import UserRecord
from workspace.models import WorkspaceRecord

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pg():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        if not await session.scalar(text("SELECT to_regclass('model_providers')")):
            pytest.skip("需要已升级0027的隔离测试schema")
        suffix = uuid4().hex[:12]
        user = UserRecord(email=f"model-{suffix}@test.local", username=f"model-{suffix}", password="x")
        workspaces = [WorkspaceRecord(name=f"model-{i}-{suffix}", slug=f"model-{i}-{suffix}", display_name="模型测试") for i in range(2)]
        session.add_all([user, *workspaces])
        await session.flush()
        yield session, user, workspaces
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


def model_input(**overrides):
    values = dict(display_name="测试模型", provider="openai_compatible", model_name="test", base_url="https://test.local/v1", api_key="private-test-key")
    return ModelProviderInput(**(values | overrides))


async def test_model_jsonb_roles_soft_delete_and_workspace_isolation(pg, monkeypatch):
    db, user, workspaces = pg
    service = ModelService(db)

    async def ping(record):
        return ModelTestResponse(success=True, latency_ms=1)

    monkeypatch.setattr(service, "ping", ping)
    extra = {"supports_reasoning_effort": True, "reasoning_effort_levels": [{"key": "high", "value": 8192}], "default_reasoning_effort": "high"}
    created = await service.save_provider(workspaces[0].id, model_input(extra_config=extra))
    assert created.api_key == "********"
    record = await service.require_provider(workspaces[0].id, created.provider_id)
    record.extra_config["context_length"] = 65536
    await db.flush()
    await db.refresh(record)
    assert record.extra_config["reasoning_effort_levels"][0]["value"] == 8192
    assert record.extra_config["context_length"] == 65536
    assert await service.list_providers(workspaces[1].id) == []
    with pytest.raises(AgentException) as error:
        await service.require_provider(workspaces[1].id, created.provider_id)
    assert error.value.status_code == 404
    await service.assign_role(workspaces[0].id, "agent", created.provider_id)
    with pytest.raises(AgentException):
        await service.delete_provider(workspaces[0].id, created.provider_id)
    await service.assign_role(workspaces[0].id, "agent", None)
    await service.assign_role(workspaces[0].id, "agent", created.provider_id)
    assert len(await service.repo.list_roles(workspaces[0].id)) == 1
    await service.assign_role(workspaces[0].id, "agent", None)
    await service.delete_provider(workspaces[0].id, created.provider_id)
    assert await service.list_providers(workspaces[0].id) == []


async def test_personal_defaults_are_scoped_by_workspace(pg, monkeypatch):
    db, user, workspaces = pg
    service = ModelService(db)

    async def ping(record):
        return ModelTestResponse(success=True, latency_ms=1)

    monkeypatch.setattr(service, "ping", ping)
    providers = [await service.save_provider(workspace.id, model_input()) for workspace in workspaces]
    for workspace, provider in zip(workspaces, providers, strict=True):
        await service.preferences(workspace.id, user.id, provider.provider_id, update=True)
    for workspace, provider in zip(workspaces, providers, strict=True):
        assert (await service.preferences(workspace.id, user.id))["default_model_provider_id"] == provider.provider_id
    with pytest.raises(AgentException):
        await service.preferences(workspaces[1].id, user.id, providers[0].provider_id, update=True)
    await service.save_provider(workspaces[0].id, ModelProviderUpdate(is_active=False), providers[0].provider_id)
    assert (await service.preferences(workspaces[0].id, user.id))["default_model_provider_id"] is None
    assert (await service.preferences(workspaces[1].id, user.id))["default_model_provider_id"] == providers[1].provider_id


async def test_http_provider_update_and_validation_do_not_leak_secrets(pg, monkeypatch):
    from api.deps import get_current_active_user, get_current_admin_user, get_current_workspace_id, get_db
    from main import create_app

    db, user, workspaces = pg

    async def ping(self, record):
        return ModelTestResponse(success=True, latency_ms=1)

    monkeypatch.setattr(ModelService, "ping", ping)
    app = create_app()

    async def database():
        yield db

    app.dependency_overrides.update({
        get_db: database, get_current_active_user: lambda: user,
        get_current_admin_user: lambda: user,
        get_current_workspace_id: lambda: workspaces[0].id,
    })
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/models/providers", json=model_input().model_dump(mode="json") | {"api_key": "private-test-key"})
        assert response.status_code == 200, response.text
        assert "private-test-key" not in response.text
        provider_id = response.json()["data"]["provider_id"]
        response = await client.put(f"/api/models/providers/{provider_id}", json={"display_name": "修改名称"})
        assert response.status_code == 200
        record = await ModelService(db).require_provider(workspaces[0].id, provider_id)
        assert record.api_key == "private-test-key" and record.display_name == "修改名称"
        response = await client.get("/api/models/capabilities")
        assert response.status_code == 200
        assert "private-test-key" not in response.text
        assert response.json()["data"]["models"][0]["provider_id"] == provider_id
        response = await client.put(f"/api/models/providers/{provider_id}", json={"display_name": None, "api_key": "private-test-key"})
        assert response.status_code == 422
        assert "private-test-key" not in response.text
