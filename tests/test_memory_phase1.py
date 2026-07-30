"""Memory Phase 1 的服务与 Schema 回归测试。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.errors import AgentException
from repositories.memory_repo import MemoryCatalogEntry, MemoryCatalogSnapshot
from schemas.memory import MemoryCreateRequest, MemoryUpdateRequest
from services.memory_service import MemoryService


class FakeMemoryRepository:
    """保留关键状态和调用次数的内存替身，不模拟 PostgreSQL 行锁。"""

    def __init__(self):
        self.space = SimpleNamespace(
            id=10,
            workspace_id=20,
            user_id=30,
            catalog_version=0,
            is_deleted=False,
        )
        self.items = {}
        self.revisions = {}
        self.sources = {}
        self.create_revision_count = 0
        self.create_source_count = 0
        self.increment_count = 0
        self.next_item_id = 1
        self.next_revision_id = 1
        self.next_source_id = 1

    def _in_scope(self, workspace_id, user_id):
        return self.space.workspace_id == workspace_id and self.space.user_id == user_id

    async def get_space(self, workspace_id, user_id, *, for_update=False):
        return self.space if self._in_scope(workspace_id, user_id) else None

    async def get_or_create_space_for_update(self, workspace_id, user_id):
        assert self._in_scope(workspace_id, user_id)
        return self.space

    async def list_items(
        self,
        workspace_id,
        user_id,
        *,
        status,
        memory_type,
        limit,
        offset,
    ):
        assert self._in_scope(workspace_id, user_id)
        items = [item for item in self.items.values() if not item.is_deleted]
        if status is not None:
            items = [item for item in items if item.status == status]
        if memory_type is not None:
            items = [item for item in items if item.memory_type == memory_type]
        return items[offset : offset + limit], len(items)

    async def search_items(
        self,
        workspace_id,
        user_id,
        *,
        query,
        memory_type,
        limit,
    ):
        assert self._in_scope(workspace_id, user_id)
        query = query.lower()
        items = [
            item
            for item in self.items.values()
            if not item.is_deleted
            and item.status == "active"
            and query in f"{item.memory_key} {item.name} {item.description}".lower()
        ]
        if memory_type is not None:
            items = [item for item in items if item.memory_type == memory_type]
        return items[:limit]

    async def get_item(self, workspace_id, user_id, memory_id, *, for_update=False):
        if not self._in_scope(workspace_id, user_id):
            return None
        item = self.items.get(memory_id)
        return item if item is not None and not item.is_deleted else None

    async def get_active_item_by_key(self, workspace_id, user_id, memory_key):
        if not self._in_scope(workspace_id, user_id):
            return None
        return next(
            (
                item
                for item in self.items.values()
                if item.memory_key == memory_key and item.status == "active" and not item.is_deleted
            ),
            None,
        )

    async def count_active_items(self, workspace_id, user_id, space_id):
        assert self._in_scope(workspace_id, user_id)
        assert space_id == self.space.id
        return sum(item.status == "active" and not item.is_deleted for item in self.items.values())

    async def create_item(self, workspace_id, user_id, *, space, **values):
        assert self._in_scope(workspace_id, user_id)
        assert space is self.space
        now = datetime.now(UTC)
        item = SimpleNamespace(
            id=self.next_item_id,
            space_id=space.id,
            status="active",
            version=1,
            superseded_by_id=None,
            last_used_at=None,
            use_count=0,
            is_deleted=False,
            deleted_at=None,
            created_at=now,
            updated_at=now,
            **values,
        )
        self.next_item_id += 1
        self.items[item.id] = item
        return item

    async def update_item(
        self,
        workspace_id,
        user_id,
        space,
        item,
        *,
        memory_type,
        name,
        description,
        body,
    ):
        assert self._in_scope(workspace_id, user_id)
        assert space is self.space
        item.memory_type = memory_type
        item.name = name
        item.description = description
        item.body = body
        item.version += 1
        item.updated_at = datetime.now(UTC)
        return item

    async def soft_delete_item(self, workspace_id, user_id, space, item):
        assert self._in_scope(workspace_id, user_id)
        assert space is self.space
        item.status = "archived"
        item.version += 1
        item.is_deleted = True
        item.deleted_at = datetime.now(UTC)

    async def create_revision(
        self,
        workspace_id,
        user_id,
        space,
        item,
        *,
        actor_type,
        actor_id,
        run_id=None,
    ):
        assert self._in_scope(workspace_id, user_id)
        assert space is self.space
        revision = SimpleNamespace(
            id=self.next_revision_id,
            memory_id=item.id,
            revision=item.version,
            memory_type=item.memory_type,
            name=item.name,
            description=item.description,
            body=item.body,
            actor_type=actor_type,
            actor_id=actor_id,
            created_at=datetime.now(UTC),
        )
        self.next_revision_id += 1
        self.create_revision_count += 1
        self.revisions.setdefault(item.id, []).insert(0, revision)
        return revision

    async def create_source(
        self,
        workspace_id,
        user_id,
        *,
        space,
        item,
        revision,
        source_kind,
        **values,
    ):
        assert self._in_scope(workspace_id, user_id)
        assert space is self.space
        source = SimpleNamespace(
            id=self.next_source_id,
            memory_id=item.id,
            revision_id=revision.id,
            source_kind=source_kind,
            created_at=datetime.now(UTC),
            **values,
        )
        self.next_source_id += 1
        self.create_source_count += 1
        self.sources.setdefault(item.id, []).insert(0, source)
        return source

    async def list_revisions(self, workspace_id, user_id, memory_id):
        assert self._in_scope(workspace_id, user_id)
        return self.revisions.get(memory_id, [])

    async def list_sources(self, workspace_id, user_id, memory_id):
        assert self._in_scope(workspace_id, user_id)
        return self.sources.get(memory_id, [])

    async def increment_catalog_version(self, workspace_id, user_id, space):
        assert self._in_scope(workspace_id, user_id)
        space.catalog_version += 1
        self.increment_count += 1
        return space.catalog_version

    async def get_catalog_snapshot(self, workspace_id, user_id, *, limit):
        if not self._in_scope(workspace_id, user_id):
            return MemoryCatalogSnapshot(None, 0, ())
        items = sorted(
            (
                item
                for item in self.items.values()
                if item.status == "active" and not item.is_deleted
            ),
            key=lambda item: (item.memory_key, item.id),
        )[:limit]
        entries = tuple(
            MemoryCatalogEntry(
                id=item.id,
                version=item.version,
                memory_key=item.memory_key,
                memory_type=item.memory_type,
                name=item.name,
                description=item.description,
            )
            for item in items
        )
        return MemoryCatalogSnapshot(self.space.id, self.space.catalog_version, entries)


def _service():
    service = MemoryService(SimpleNamespace())
    service.repo = FakeMemoryRepository()
    return service


def _create(service):
    return asyncio.run(
        service.create_memory(
            20,
            30,
            memory_key="user-preference-tabs",
            memory_type="user",
            name="user-preference-tabs",
            description="User prefers tabs for indentation",
            body="Always use tabs.",
        )
    )


def test_memory_schema_checks_utf8_bytes_and_partial_update():
    with pytest.raises(ValidationError, match="16384"):
        MemoryCreateRequest(
            memory_key="large-body",
            memory_type="reference",
            name="large-body",
            description="正文过大",
            body="中" * 6000,
        )
    with pytest.raises(ValidationError, match="至少提供一个"):
        MemoryUpdateRequest(version=1)
    with pytest.raises(ValidationError, match="不能为 null"):
        MemoryUpdateRequest(version=1, name=None)


def test_create_memory_writes_revision_source_and_catalog_once():
    service = _service()
    detail = _create(service)

    assert detail.item.version == 1
    assert detail.catalog_version == 1
    assert len(detail.revisions) == 1
    assert len(detail.sources) == 1
    assert service.repo.create_revision_count == 1
    assert service.repo.create_source_count == 1
    assert service.repo.increment_count == 1

    catalog = asyncio.run(service.get_rendered_catalog(20, 30))
    assert catalog.snapshot.catalog_version == 1
    assert catalog.content == (
        "- [user-preference-tabs](memory:1@1) - User prefers tabs for indentation"
    )
    assert catalog.byte_count == len(catalog.content.encode("utf-8"))


def test_noop_update_does_not_advance_versions():
    service = _service()
    detail = _create(service)

    same = asyncio.run(
        service.update_memory(
            20,
            30,
            detail.item.id,
            expected_version=1,
            name=detail.item.name,
        )
    )

    assert same.item.version == 1
    assert same.catalog_version == 1
    assert service.repo.create_revision_count == 1
    assert service.repo.create_source_count == 1
    assert service.repo.increment_count == 1


def test_update_conflict_returns_409_without_side_effects():
    service = _service()
    detail = _create(service)

    with pytest.raises(AgentException, match="版本冲突") as caught:
        asyncio.run(
            service.update_memory(
                20,
                30,
                detail.item.id,
                expected_version=99,
                body="new body",
            )
        )

    assert caught.value.status_code == 409
    assert caught.value.details == {"expected_version": 99, "current_version": 1}
    assert detail.item.version == 1
    assert service.repo.create_revision_count == 1
    assert service.repo.increment_count == 1


def test_effective_update_and_delete_each_advance_catalog_once():
    service = _service()
    created = _create(service)

    updated = asyncio.run(
        service.update_memory(
            20,
            30,
            created.item.id,
            expected_version=1,
            body="Always use tabs in Python files.",
        )
    )
    assert updated.item.version == 2
    assert updated.catalog_version == 2
    assert [revision.revision for revision in updated.revisions] == [2, 1]

    deleted = asyncio.run(
        service.delete_memory(
            20,
            30,
            created.item.id,
            expected_version=2,
        )
    )
    assert deleted.version == 3
    assert deleted.catalog_version == 3
    assert service.repo.create_revision_count == 3
    assert service.repo.create_source_count == 3
    assert service.repo.increment_count == 3
    assert asyncio.run(service.get_rendered_catalog(20, 30)).content == ""


def test_duplicate_key_and_cross_scope_access_are_rejected():
    service = _service()
    created = _create(service)

    with pytest.raises(AgentException, match="memory_key 已存在") as caught:
        _create(service)
    assert caught.value.status_code == 409

    with pytest.raises(AgentException, match="不存在"):
        asyncio.run(service.get_memory(999, 30, created.item.id))
    with pytest.raises(AgentException, match="不存在"):
        asyncio.run(service.get_memory(20, 999, created.item.id))
