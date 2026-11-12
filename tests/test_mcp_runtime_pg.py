"""MCP 运行时接线（M1c）的真实 PostgreSQL 集成测试。

在一个外层事务里跑、结束时整体回滚（同 test_mcp_server_pg.py）。覆盖：
- tool_calls.source 列（迁移 0032）：MCP 工具建出记录后写入来源，内置工具为 NULL；
  isError 的结果记为 failed 并写 error_message；
- MCP 工具审批时选 always_scope=global：经真实 WorkspaceService 校验角色，成员 403，管理员放行。
库不可达或未升级到 0032 时跳过。
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException
from harness.app.tool_set import ToolSet
from harness.bootstrap import default_harness
from harness.domain.tool import ToolOutput
from runtime.interaction_flow import InteractionFlow
from session.models import SessionRecord
from tests.fixtures.scoped_tools import FakeScopedTool
from tools.models import ToolCallRecord
from tools.service import ToolService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pg() -> AsyncIterator[SimpleNamespace]:
    """外层事务里的工作区：admin 与 member 两个成员和 member 的一个会话；用例结束整体回滚。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        column = await session.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'tool_calls' AND column_name = 'source'"
            )
        )
        if not column:
            pytest.skip("需要已升级到 0032_tool_calls_source 的数据库")
        suffix = uuid4().hex[:12]
        admin, member = (
            UserRecord(
                email=f"mcpr-{name}-{suffix}@test.local",
                username=f"mcpr-{name}-{suffix}",
                password="x",
            )
            for name in ("admin", "member")
        )
        workspace = WorkspaceRecord(
            name=f"mcpr-{suffix}", slug=f"mcpr-{suffix}", display_name="MCP 运行时测试"
        )
        session.add_all([admin, member, workspace])
        await session.flush()
        now = datetime.now(UTC)
        session.add_all(
            [
                WorkspaceMemberRecord(
                    workspace_id=workspace.id, user_id=admin.id, role="admin", joined_at=now
                ),
                WorkspaceMemberRecord(
                    workspace_id=workspace.id, user_id=member.id, role="member", joined_at=now
                ),
            ]
        )
        chat = SessionRecord(title="t", status="idle", user_id=member.id, workspace_id=workspace.id)
        session.add(chat)
        await session.flush()
        yield SimpleNamespace(
            db=session,
            workspace_id=workspace.id,
            admin=admin.id,
            member=member.id,
            session_id=chat.id,
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _hooks(*_: Any) -> list[Any]:
    return []


async def test_tool_calls_source_and_error_persist(pg, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import service as tool_service_module

    monkeypatch.setattr(
        tool_service_module, "get_hook_registry", lambda: SimpleNamespace(trigger=_hooks)
    )
    failing = FakeScopedTool("mcp__github__fail", output=ToolOutput.error("远程报错"))
    ok = FakeScopedTool("mcp__github__ok", source="mcp:github")
    failing.source = "mcp:github"
    platform = default_harness().tools
    policies = ToolSet((*platform.tools, failing, ok))
    service = ToolService(pg.db, policies, policies)

    await service.start_call(pg.session_id, "Read", {"file_path": "/a"})
    await service.run(pg.session_id, failing.name, {"query": "x"}, profile="lead")
    await service.run(pg.session_id, ok.name, {"query": "y"}, profile="lead")
    await pg.db.flush()
    pg.db.expire_all()

    rows = (
        await pg.db.scalars(
            select(ToolCallRecord)
            .where(ToolCallRecord.session_id == pg.session_id)
            .order_by(ToolCallRecord.id)
        )
    ).all()
    assert [(row.tool_name, row.source, row.status) for row in rows] == [
        ("Read", None, "running"),
        (failing.name, "mcp:github", "failed"),
        (ok.name, "mcp:github", "succeeded"),
    ]
    assert rows[1].error_message == "远程报错"
    assert rows[1].tool_id is None
    # 按来源加状态就能统计每个 server 的调用量与失败率。
    counts = (
        await pg.db.execute(
            text(
                "SELECT status, count(*) FROM tool_calls "
                "WHERE session_id = :sid AND source = 'mcp:github' GROUP BY status ORDER BY status"
            ),
            {"sid": pg.session_id},
        )
    ).all()
    assert [tuple(row) for row in counts] == [("failed", 1), ("succeeded", 1)]


def _flow(db: AsyncSession, *, user_id: int, workspace_id: int) -> InteractionFlow:
    """只用到 _normalize_response 的交互流程：数据库会话与可信身份是真的，其余协作者不会被调用。"""
    unused: Any = None
    return InteractionFlow(
        db=db,
        configuration=unused,
        executor=unused,
        sessions=unused,
        interaction_service=unused,
        plan_service=unused,
        permission_service=unused,
        tool_service=unused,
        bus=unused,
        user_id=user_id,
        workspace_id=workspace_id,
    )


def _approval_request(source: str | None) -> Any:
    tool: dict[str, Any] = {"id": "call-1", "name": "mcp__github__delete_repo", "input": {}}
    if source is not None:
        tool["source"] = source
    return SimpleNamespace(request_payload={"tool": tool, "can_update_input": source is None})


_GLOBAL_ALLOW = {"decision": "always_allow", "always_scope": "global"}


async def test_mcp_global_always_allow_checks_real_workspace_role(pg) -> None:
    request = _approval_request("mcp:github")
    member = _flow(pg.db, user_id=pg.member, workspace_id=pg.workspace_id)
    admin = _flow(pg.db, user_id=pg.admin, workspace_id=pg.workspace_id)

    with pytest.raises(AgentException) as exc_info:
        await member._normalize_response(request, "tool_approval", _GLOBAL_ALLOW)
    allowed = await admin._normalize_response(request, "tool_approval", _GLOBAL_ALLOW)
    # 会话级的「始终允许」不受限制；内置工具的全局规则保持现状（不查角色）。
    session_scope = await member._normalize_response(
        request, "tool_approval", {"decision": "always_allow"}
    )
    builtin = await member._normalize_response(
        _approval_request(None), "tool_approval", _GLOBAL_ALLOW
    )

    assert exc_info.value.status_code == 403
    assert allowed == _GLOBAL_ALLOW
    assert session_scope == {"decision": "always_allow"}
    assert builtin == _GLOBAL_ALLOW
