"""market 接口的契约测试：审核接口只对平台管理员开放，未上架条目的详情对外部工作区返回 404，
分类接口返回设计里的七个分类，业务错误走统一外壳且状态码正确。

不连接数据库：当前用户和工作区通过依赖覆盖直接给出，需要查库的 Service 方法用 Fake 替换。
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import database.registry  # noqa: F401  构造 ORM 实例前需全部映射在册
from market.models import MarketListingRecord
from market.repository import MarketRepository
from market.service import MarketService
from workspace.service import WorkspaceService

pytestmark = pytest.mark.anyio

USER_ID = 1
WORKSPACE_ID = 2
PUBLISHER_WORKSPACE_ID = 3


def _user(*, is_platform_admin: bool) -> SimpleNamespace:
    return SimpleNamespace(id=USER_ID, is_platform_admin=is_platform_admin)


def _listing(status: str) -> MarketListingRecord:
    now = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    return MarketListingRecord(
        id=9,
        kind="skill",
        is_official=False,
        publisher_workspace_id=PUBLISHER_WORKSPACE_ID,
        status=status,
        current_version_number=None,
        name="data-cleaning",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def client_factory(monkeypatch: pytest.MonkeyPatch):
    """按给定的当前用户构造客户端；Service 里会访问数据库的方法由各用例自行替换。"""
    from api.deps import get_current_active_user, get_current_workspace_id, get_db
    from main import create_app

    # 依赖覆盖后 get_current_workspace_id 不再校验成员，Service 里的成员和角色校验也放行。
    async def allow(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(WorkspaceService, "require_active_membership", allow)
    monkeypatch.setattr(WorkspaceService, "require_workspace_role", allow)

    # Service 和 Repository 构造时只保存会话引用，不会真正访问数据库。
    async def fake_db() -> AsyncIterator[None]:
        yield None

    clients: list[httpx.AsyncClient] = []

    def build(user: SimpleNamespace) -> httpx.AsyncClient:
        app = create_app()
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_current_workspace_id] = lambda: WORKSPACE_ID
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(client)
        return client

    yield build
    for client in clients:
        await client.aclose()


async def test_review_endpoints_reject_normal_user_with_403(client_factory) -> None:
    client = client_factory(_user(is_platform_admin=False))

    responses = [
        await client.get("/api/market/admin/versions"),
        await client.post("/api/market/admin/versions/1/approve"),
        await client.post("/api/market/admin/versions/1/reject", json={"reason": "不合规"}),
        await client.post("/api/market/admin/listings/1/remove", json={"reason": "不合规"}),
    ]

    for response in responses:
        assert response.status_code == 403, response.text
        body = response.json()
        assert body["data"] is None
        assert body["error"]["message"] == "需要平台管理员权限"


async def test_review_list_is_open_to_platform_admin(client_factory, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_list(self: MarketService, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"items": [], "total": 0, "limit": kwargs["limit"], "offset": kwargs["offset"]}

    monkeypatch.setattr(MarketService, "list_versions_for_review", fake_list)
    client = client_factory(_user(is_platform_admin=True))

    response = await client.get("/api/market/admin/versions", params={"limit": 5})
    invalid = await client.get("/api/market/admin/versions", params={"status": "unknown"})
    blank_reason = await client.post("/api/market/admin/versions/1/reject", json={"reason": " "})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["total"] == 0
    assert calls == [{"status": "pending", "limit": 5, "offset": 0}]
    assert invalid.status_code == 422
    assert blank_reason.status_code == 422


@pytest.mark.parametrize("status", ["unlisted", "delisted", "removed"])
async def test_detail_of_unlisted_listing_is_404_for_other_workspace(
    client_factory, monkeypatch, status: str
) -> None:
    async def fake_get_listing(self: MarketRepository, listing_id: int) -> MarketListingRecord:
        return _listing(status)

    monkeypatch.setattr(MarketRepository, "get_listing", fake_get_listing)
    client = client_factory(_user(is_platform_admin=False))

    response = await client.get("/api/market/listings/9")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "市场条目不存在"


async def test_install_of_delisted_listing_is_409(client_factory, monkeypatch) -> None:
    async def fake_get_listing(self: MarketRepository, listing_id: int) -> MarketListingRecord:
        return _listing("delisted")

    monkeypatch.setattr(MarketRepository, "get_listing", fake_get_listing)
    client = client_factory(_user(is_platform_admin=False))

    install = await client.post("/api/market/listings/9/install")
    upgrade = await client.post("/api/market/listings/9/upgrade")

    assert install.status_code == 409, install.text
    assert upgrade.status_code == 409, upgrade.text


async def test_categories_endpoint(client_factory) -> None:
    client = client_factory(_user(is_platform_admin=False))

    response = await client.get("/api/market/categories")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["key"] for item in data] == [
        "general",
        "coding",
        "data",
        "writing",
        "office",
        "education",
        "other",
    ]
    assert data[0] == {"key": "general", "label": "通用"}


async def test_unique_violation_on_submit_becomes_409_without_leaking(
    client_factory, monkeypatch
) -> None:
    """并发首次提交撞上源对象的唯一索引：返回 409 中文业务错误，响应里不出现 SQL 原文。"""
    from sqlalchemy.exc import IntegrityError

    from skill.service import SkillService

    async def fake_build_snapshot(self: SkillService, workspace_id: int, skill_id: int) -> dict:
        content = "---\nname: data-cleaning\ndescription: 清洗\n---\n\n正文\n"
        return {"name": "data-cleaning", "content": content, "extra_files": {}, "skill_hash": "h"}

    async def fake_lock(self: MarketRepository, kind: str, source_id: int) -> None:
        return None

    async def fake_create_listing(self: MarketRepository, **kwargs: Any) -> MarketListingRecord:
        raise IntegrityError(
            "INSERT INTO market_listings SECRET-SQL", {}, Exception("uq_market_listings")
        )

    monkeypatch.setattr(SkillService, "build_snapshot", fake_build_snapshot)
    monkeypatch.setattr(MarketRepository, "get_listing_by_source_for_update", fake_lock)
    monkeypatch.setattr(MarketRepository, "create_listing", fake_create_listing)
    client = client_factory(_user(is_platform_admin=False))

    response = await client.post(
        "/api/market/submissions",
        json={"kind": "skill", "source_id": 5, "category": "data"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["message"] == "该对象正在被其他请求提交上架，请稍后重试"
    assert "SECRET-SQL" not in response.text
    assert "uq_market_listings" not in response.text


async def test_submit_rejects_unknown_category_with_400(client_factory, monkeypatch) -> None:
    from skill.service import SkillService

    async def fake_build_snapshot(self: SkillService, workspace_id: int, skill_id: int) -> dict:
        content = "---\nname: data-cleaning\ndescription: 清洗\n---\n\n正文\n"
        return {"name": "data-cleaning", "content": content, "extra_files": {}, "skill_hash": "h"}

    monkeypatch.setattr(SkillService, "build_snapshot", fake_build_snapshot)
    client = client_factory(_user(is_platform_admin=False))

    response = await client.post(
        "/api/market/submissions",
        json={"kind": "skill", "source_id": 5, "category": "games"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["details"]["category"] == "games"
