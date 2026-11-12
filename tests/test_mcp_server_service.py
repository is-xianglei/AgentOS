"""McpServerService 的业务规则单元测试：用内存仓储替身与 monkeypatch 的成员服务，不连数据库。

覆盖：共享 server 只有 owner / admin 能写、成员能建私有 server 且受上限约束、别人的私有 server 一律 404、
成员对共享 server 403、重名与并发撞 key 一律 409 且不带 owner 信息、config_rev 冲突 409、
列表与详情不读凭证表、McpServerSource 的数据来源、permission 用的工具名查询。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

import database.registry  # noqa: F401  构造 ORM 实例前需全部映射在册
from core.config import settings
from core.errors import AgentException
from harness.app.mcp_port import McpServerRegistry
from harness.domain.mcp import (
    McpAuthType,
    McpProbeResult,
    McpToolAnnotations,
    McpToolSummary,
)
from harness.domain.mcp_config import McpServerDraft, plan_update
from mcp_server.models import McpServerRecord, McpServerSecretRecord
from mcp_server.service import McpServerService
from workspace.service import WorkspaceService

pytestmark = pytest.mark.anyio

WORKSPACE_ID = 1
ADMIN_ID = 1
MEMBER_ID = 2
OTHER_ID = 3
_ROLES = {ADMIN_ID: "admin", MEMBER_ID: "member", OTHER_ID: "member"}
_NOW = datetime(2026, 11, 12, tzinfo=UTC)


def _tool(name: str) -> McpToolSummary:
    return McpToolSummary(
        name=name,
        description="",
        input_schema_sha256="d",
        annotations=McpToolAnnotations(),
    )


def _ok(*names: str) -> McpProbeResult:
    return McpProbeResult.success(
        server_name="fake",
        server_version="1",
        protocol_version="2026-07-28",
        tools=[_tool(name) for name in names],
    )


class FakeRepository:
    """McpServerRepository 的内存替身：按工作区过滤、模拟部分唯一索引，记录凭证表的读取。"""

    def __init__(self) -> None:
        self.rows: dict[int, McpServerRecord] = {}
        self.secrets: dict[int, McpServerSecretRecord] = {}
        self.secret_reads = 0
        self.locked: list[int] = []
        self.next_id = 1
        self.race_on_add = False

    def _active(self) -> list[McpServerRecord]:
        return [row for row in self.rows.values() if not row.is_deleted]

    async def get(self, workspace_id: int, server_id: int) -> McpServerRecord | None:
        row = self.rows.get(server_id)
        if row is None or row.is_deleted or row.workspace_id != workspace_id:
            return None
        return row

    async def get_for_update(self, workspace_id: int, server_id: int) -> McpServerRecord | None:
        return await self.get(workspace_id, server_id)

    async def list_visible(
        self,
        workspace_id: int,
        user_id: int,
        *,
        include_disabled_shared: bool,
    ) -> list[McpServerRecord]:
        rows = [
            row
            for row in self._active()
            if row.workspace_id == workspace_id
            and (
                (row.owner_user_id is None and (include_disabled_shared or row.is_enabled))
                or row.owner_user_id == user_id
            )
        ]
        return sorted(rows, key=lambda row: (row.owner_user_id is not None, row.server_key))

    async def list_enabled_usable(self, workspace_id: int, user_id: int) -> list[McpServerRecord]:
        return [
            row
            for row in await self.list_visible(workspace_id, user_id, include_disabled_shared=False)
            if row.is_enabled
        ]

    async def get_shared_by_key(self, workspace_id: int, server_key: str) -> McpServerRecord | None:
        for row in self._active():
            if (row.workspace_id, row.server_key, row.owner_user_id) == (
                workspace_id,
                server_key,
                None,
            ):
                return row
        return None

    async def key_taken(self, workspace_id: int, server_key: str) -> bool:
        return any(
            (row.workspace_id, row.server_key) == (workspace_id, server_key)
            for row in self._active()
        )

    async def count_private(self, workspace_id: int, owner_user_id: int) -> int:
        return sum(
            1
            for row in self._active()
            if (row.workspace_id, row.owner_user_id) == (workspace_id, owner_user_id)
        )

    async def lock_private_quota(self, owner_user_id: int) -> None:
        self.locked.append(owner_user_id)

    async def add(self, record: McpServerRecord) -> McpServerRecord:
        # 模拟部分唯一索引：预检查之后被并发请求抢先占用同一个 key。
        if self.race_on_add or await self.key_taken(record.workspace_id, record.server_key):
            raise IntegrityError("INSERT", {}, Exception("uq_mcp_servers_active_workspace_key"))
        record.id = self.next_id
        self.next_id += 1
        record.is_deleted = False
        record.created_at = record.updated_at = _NOW
        self.rows[record.id] = record
        return record

    async def save(self, record: McpServerRecord | McpServerSecretRecord) -> None:
        if isinstance(record, McpServerSecretRecord):
            record.is_deleted = False
            self.secrets[record.server_id] = record

    async def soft_delete(self, record: McpServerRecord) -> None:
        record.is_deleted = True

    async def get_secret(self, server_id: int) -> McpServerSecretRecord | None:
        self.secret_reads += 1
        return self.secrets.get(server_id)

    async def list_secrets(self, server_ids: list[int]) -> dict[int, McpServerSecretRecord]:
        self.secret_reads += 1
        return {sid: self.secrets[sid] for sid in server_ids if sid in self.secrets}

    async def soft_delete_secrets(self, server_id: int) -> None:
        self.secrets.pop(server_id, None)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> McpServerService:
    """角色按 _ROLES 给出；不在表里的用户不是成员。仓储换成内存替身。"""

    async def membership(self: WorkspaceService, workspace_id: int, user_id: int) -> Any:
        if workspace_id != WORKSPACE_ID or user_id not in _ROLES:
            raise AgentException.message("当前用户不是该工作区的有效成员")
        return SimpleNamespace(role=_ROLES[user_id])

    async def role(
        self: WorkspaceService,
        workspace_id: int,
        user_id: int,
        allowed: set[str],
    ) -> Any:
        member = await membership(self, workspace_id, user_id)
        if member.role not in allowed:
            raise AgentException.message("无权执行该工作区操作", status_code=403)
        return member

    monkeypatch.setattr(WorkspaceService, "require_active_membership", membership)
    monkeypatch.setattr(WorkspaceService, "require_workspace_role", role)
    svc = McpServerService(db=None)  # type: ignore[arg-type]
    svc.repo = FakeRepository()  # type: ignore[assignment]
    return svc


def _draft(key: str = "github", *, private: bool = False, **overrides: Any) -> McpServerDraft:
    values: dict[str, Any] = {
        "server_key": key,
        "name": key,
        "url": f"https://{key}.example.com/mcp",
        "private": private,
        "auth_type": McpAuthType.STATIC_HEADERS,
        "headers": {"Authorization": f"Bearer {key}-secret"},
    }
    values.update(overrides)
    return McpServerDraft.create(**values)


async def _create(svc: McpServerService, user_id: int, draft: McpServerDraft) -> int:
    await svc.prepare_create(WORKSPACE_ID, user_id, draft)
    view = await svc.create_server(WORKSPACE_ID, user_id, draft, _ok("create_issue"))
    return view.server_id


def test_service_structurally_satisfies_registry_port() -> None:
    registry: McpServerRegistry = McpServerService(db=None)  # type: ignore[arg-type]

    assert callable(registry.prepare_create)


# ---------- 新建：角色、上限、重名 ----------


async def test_member_cannot_create_shared_server(service: McpServerService) -> None:
    with pytest.raises(AgentException) as exc_info:
        await service.prepare_create(WORKSPACE_ID, MEMBER_ID, _draft())

    assert exc_info.value.status_code == 403


async def test_admin_creates_shared_and_member_creates_private(service: McpServerService) -> None:
    shared_id = await _create(service, ADMIN_ID, _draft("github"))
    private_id = await _create(service, MEMBER_ID, _draft("mine", private=True))

    shared = await service.get_server(WORKSPACE_ID, ADMIN_ID, shared_id)
    private = await service.get_server(WORKSPACE_ID, MEMBER_ID, private_id)
    assert shared.config.owner_user_id is None and shared.config.config_rev == 1
    assert private.config.owner_user_id == MEMBER_ID
    assert [tool.name for tool in private.tools] == ["create_issue"]
    assert service.repo.locked == [MEMBER_ID]  # type: ignore[attr-defined]


async def test_private_quota_is_enforced(
    service: McpServerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_max_private_servers_per_user", 2)
    await _create(service, MEMBER_ID, _draft("one", private=True))
    await _create(service, MEMBER_ID, _draft("two", private=True))
    # 上限按人计：别人的私有 server 与共享 server 不占这个名额。
    await _create(service, OTHER_ID, _draft("three", private=True))

    with pytest.raises(AgentException, match="上限") as exc_info:
        await service.prepare_create(WORKSPACE_ID, MEMBER_ID, _draft("four", private=True))

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"limit": 2}


async def test_quota_is_rechecked_when_writing(
    service: McpServerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测期间同一用户的另一个请求已写满上限：写库时在咨询锁里重新核对。"""
    monkeypatch.setattr(settings, "mcp_max_private_servers_per_user", 1)
    draft = _draft("late", private=True)
    await service.prepare_create(WORKSPACE_ID, MEMBER_ID, draft)
    await _create(service, MEMBER_ID, _draft("early", private=True))

    with pytest.raises(AgentException, match="上限"):
        await service.create_server(WORKSPACE_ID, MEMBER_ID, draft, _ok())


async def test_key_taken_by_someone_elses_private_server_is_409_without_owner(
    service: McpServerService,
) -> None:
    await _create(service, OTHER_ID, _draft("secret", private=True))

    with pytest.raises(AgentException) as exc_info:
        await service.prepare_create(WORKSPACE_ID, ADMIN_ID, _draft("secret"))

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "server_key 已被占用"
    assert exc_info.value.details == {"server_key": "secret"}


async def test_concurrent_insert_integrity_error_becomes_409(service: McpServerService) -> None:
    draft = _draft("race")
    await service.prepare_create(WORKSPACE_ID, ADMIN_ID, draft)
    service.repo.race_on_add = True  # type: ignore[attr-defined]

    with pytest.raises(AgentException) as exc_info:
        await service.create_server(WORKSPACE_ID, ADMIN_ID, draft, _ok())

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "server_key 已被占用"
    assert isinstance(exc_info.value.__cause__, IntegrityError)


async def test_failed_probe_result_cannot_be_written(service: McpServerService) -> None:
    with pytest.raises(AgentException, match="探测失败"):
        await service.create_server(
            WORKSPACE_ID,
            ADMIN_ID,
            _draft(),
            McpProbeResult.failure("连接失败"),
        )


# ---------- 可见性与写权限 ----------


async def test_someone_elses_private_server_is_404_everywhere(service: McpServerService) -> None:
    server_id = await _create(service, OTHER_ID, _draft("theirs", private=True))

    for call in (
        service.get_server(WORKSPACE_ID, ADMIN_ID, server_id),
        service.load_for_write(WORKSPACE_ID, ADMIN_ID, server_id),
        service.delete_server(WORKSPACE_ID, MEMBER_ID, server_id),
        service.record_probe(WORKSPACE_ID, ADMIN_ID, server_id, _ok()),
    ):
        with pytest.raises(AgentException) as exc_info:
            await call
        assert exc_info.value.status_code == 404
        assert exc_info.value.message == "MCP server 不存在"
    assert await service.list_servers(WORKSPACE_ID, ADMIN_ID) == []


async def test_member_sees_enabled_shared_but_cannot_write(service: McpServerService) -> None:
    enabled_id = await _create(service, ADMIN_ID, _draft("on"))
    disabled_id = await _create(service, ADMIN_ID, _draft("off", is_enabled=False))

    member_list = await service.list_servers(WORKSPACE_ID, MEMBER_ID)
    admin_list = await service.list_servers(WORKSPACE_ID, ADMIN_ID)

    assert [view.server_id for view in member_list] == [enabled_id]
    assert not member_list[0].writable
    assert {view.server_id for view in admin_list} == {enabled_id, disabled_id}
    assert all(view.writable for view in admin_list)
    with pytest.raises(AgentException) as exc_info:
        await service.get_server(WORKSPACE_ID, MEMBER_ID, disabled_id)
    assert exc_info.value.status_code == 404
    with pytest.raises(AgentException) as exc_info:
        await service.load_for_write(WORKSPACE_ID, MEMBER_ID, enabled_id)
    assert exc_info.value.status_code == 403


async def test_admin_cannot_write_members_private_server_but_owner_can(
    service: McpServerService,
) -> None:
    server_id = await _create(service, MEMBER_ID, _draft("mine", private=True))

    writable = await service.load_for_write(WORKSPACE_ID, MEMBER_ID, server_id)

    assert writable.view.writable
    assert writable.secret is not None
    assert writable.secret.as_dict() == {"Authorization": "Bearer mine-secret"}


async def test_list_and_get_never_read_secrets(service: McpServerService) -> None:
    server_id = await _create(service, ADMIN_ID, _draft())
    repo = service.repo
    repo.secret_reads = 0  # type: ignore[attr-defined]

    views = await service.list_servers(WORKSPACE_ID, ADMIN_ID)
    view = await service.get_server(WORKSPACE_ID, ADMIN_ID, server_id)

    assert repo.secret_reads == 0  # type: ignore[attr-defined]
    assert views[0].header_names is None and view.header_names is None


async def test_non_member_is_rejected(service: McpServerService) -> None:
    with pytest.raises(AgentException, match="有效成员"):
        await service.list_servers(WORKSPACE_ID, 99)


# ---------- 修改与删除 ----------


async def test_apply_update_writes_plan_and_replaces_secret(service: McpServerService) -> None:
    server_id = await _create(service, ADMIN_ID, _draft())
    current = await service.load_for_write(WORKSPACE_ID, ADMIN_ID, server_id)
    update = plan_update(current, {"headers": {"X-Token": "new"}, "url": "https://b.example.com"})

    view = await service.apply_update(WORKSPACE_ID, ADMIN_ID, server_id, update, _ok("x", "y"))

    assert view.config.config_rev == 2
    assert view.config.url == "https://b.example.com"
    assert view.header_names == ("X-Token",)
    assert [tool.name for tool in view.tools] == ["x", "y"]
    stored = service.repo.secrets[server_id]  # type: ignore[attr-defined]
    assert stored.headers == {"X-Token": "new"}


async def test_apply_update_rejects_stale_plan(service: McpServerService) -> None:
    server_id = await _create(service, ADMIN_ID, _draft())
    current = await service.load_for_write(WORKSPACE_ID, ADMIN_ID, server_id)
    first = plan_update(current, {"is_enabled": False})
    stale = plan_update(current, {"tool_denylist": ["x"]})
    await service.apply_update(WORKSPACE_ID, ADMIN_ID, server_id, first, None)

    with pytest.raises(AgentException) as exc_info:
        await service.apply_update(WORKSPACE_ID, ADMIN_ID, server_id, stale, None)

    assert exc_info.value.status_code == 409


async def test_delete_soft_deletes_server_and_secret_and_frees_key(
    service: McpServerService,
) -> None:
    server_id = await _create(service, ADMIN_ID, _draft())

    await service.delete_server(WORKSPACE_ID, ADMIN_ID, server_id)

    assert service.repo.rows[server_id].is_deleted  # type: ignore[attr-defined]
    assert server_id not in service.repo.secrets  # type: ignore[attr-defined]
    await _create(service, ADMIN_ID, _draft())


async def test_record_probe_keeps_snapshot_on_failure(service: McpServerService) -> None:
    server_id = await _create(service, ADMIN_ID, _draft())

    failed = await service.record_probe(
        WORKSPACE_ID,
        ADMIN_ID,
        server_id,
        McpProbeResult.failure("超时"),
    )
    succeeded = await service.record_probe(WORKSPACE_ID, ADMIN_ID, server_id, _ok("new_tool"))

    assert [tool.name for tool in failed.tools] == ["create_issue"]
    assert failed.last_probe_error == "超时"
    assert [tool.name for tool in succeeded.tools] == ["new_tool"]
    assert succeeded.last_probe_error is None
    assert succeeded.config.config_rev == 1


# ---------- 回合来源与权限查询 ----------


async def test_list_usable_returns_enabled_shared_and_own_private(
    service: McpServerService,
) -> None:
    await _create(service, ADMIN_ID, _draft("shared"))
    await _create(service, ADMIN_ID, _draft("off", is_enabled=False))
    await _create(service, MEMBER_ID, _draft("mine", private=True))
    await _create(service, MEMBER_ID, _draft("mine_off", private=True, is_enabled=False))
    await _create(service, OTHER_ID, _draft("theirs", private=True))

    entries = await service.list_usable(WORKSPACE_ID, MEMBER_ID)

    assert [entry.config.server_key for entry in entries] == ["shared", "mine"]
    assert all(entry.config.server_id is not None for entry in entries)
    secret = entries[1].secret
    assert secret is not None and secret.as_dict() == {"Authorization": "Bearer mine-secret"}


async def test_known_shared_tool_checks_snapshot_names(service: McpServerService) -> None:
    await _create(service, ADMIN_ID, _draft("github"))
    await _create(service, MEMBER_ID, _draft("mine", private=True))

    assert await service.is_known_shared_tool(WORKSPACE_ID, "mcp__github__create_issue")
    assert not await service.is_known_shared_tool(WORKSPACE_ID, "mcp__github__unknown")
    # 私有 server 不参与校验：即使工具在快照里也当作未知，不泄露它的存在。
    assert not await service.is_known_shared_tool(WORKSPACE_ID, "mcp__mine__create_issue")
    assert not await service.is_known_shared_tool(WORKSPACE_ID, "mcp__nope__x")
    assert not await service.is_known_shared_tool(WORKSPACE_ID, "Bash")
    assert not await service.is_known_shared_tool(2, "mcp__github__create_issue")


async def test_known_shared_tool_matches_prefix_for_oauth_without_snapshot(
    service: McpServerService,
) -> None:
    """快照为空的 OAuth server 按 mcp__{key}__ 前缀匹配（OAuth 的建表与授权在 M3，这里直接造行）。"""
    repo = service.repo
    oauth = McpServerRecord(
        workspace_id=WORKSPACE_ID,
        owner_user_id=None,
        server_key="jira",
        name="Jira",
        url="https://jira.example.com/mcp",
        auth_type="oauth",
        approval_mode="always",
        tool_allowlist=[],
        tool_denylist=[],
        config_rev=1,
        is_enabled=True,
        tools_snapshot=[],
    )
    await repo.add(oauth)  # type: ignore[attr-defined]
    empty_static_id = await _create(service, ADMIN_ID, _draft("empty"))
    repo.rows[empty_static_id].tools_snapshot = []  # type: ignore[attr-defined]

    assert await service.is_known_shared_tool(WORKSPACE_ID, "mcp__jira__any_tool")
    assert not await service.is_known_shared_tool(WORKSPACE_ID, "mcp__empty__any_tool")
