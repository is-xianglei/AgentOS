"""子代理运行记录接口的序列化契约：agent_id 须随响应返回，未关联 Agent 时为 null。

只验证 API 层把 ORM 记录转换成响应的结果，TeamService 查询用 Fake 替换，不连接数据库。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import database.registry  # noqa: F401  构造 ORM 实例前需全部映射在册
from team.models import SubAgentRunRecord
from team.service import TeamService

pytestmark = pytest.mark.anyio

SESSION_ID = 10
USER_ID = 1
WORKSPACE_ID = 2


def _build_run(run_id: int, member_name: str, agent_id: int | None) -> SubAgentRunRecord:
    """构造一条已结束的运行记录；id 和时间字段在内存对象上不会自动生成，需显式给出。"""
    return SubAgentRunRecord(
        id=run_id,
        session_id=SESSION_ID,
        member_name=member_name,
        agent_id=agent_id,
        status="succeeded",
        prompt="审阅方案",
        report="审阅完成",
        error_message=None,
        started_at=datetime(2026, 11, 4, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 11, 4, 10, 5, tzinfo=UTC),
    )


async def _get_subagent_runs(
    monkeypatch: pytest.MonkeyPatch,
    runs: list[SubAgentRunRecord],
) -> list[dict[str, Any]]:
    """请求子代理运行记录接口，返回响应外壳中的 data 列表。"""
    from api.deps import get_current_active_user, get_current_workspace_id, get_db
    from main import create_app

    async def fake_list_subagent_runs_for_user(
        self: TeamService,
        session_id: int,
        user_id: int,
        workspace_id: int,
    ) -> list[SubAgentRunRecord]:
        return runs

    monkeypatch.setattr(
        TeamService,
        "list_subagent_runs_for_user",
        fake_list_subagent_runs_for_user,
    )

    # 查询已被 Fake 替换，TeamService 构造时只保存会话引用，不会真正访问数据库。
    async def fake_db():
        yield None

    app = create_app()
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(id=USER_ID)
    app.dependency_overrides[get_current_workspace_id] = lambda: WORKSPACE_ID

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/teams/subagent-runs",
            params={"session_id": SESSION_ID},
        )

    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_subagent_run_returns_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """委派自建 Agent 或 Workflow 成员的运行记录，接口原样返回 agent_id。"""
    runs = [_build_run(run_id=1, member_name="delegate-审稿人", agent_id=7)]

    data = await _get_subagent_runs(monkeypatch, runs)

    assert len(data) == 1
    assert data[0]["id"] == 1
    assert data[0]["member_name"] == "delegate-审稿人"
    assert data[0]["agent_id"] == 7


async def test_subagent_run_without_agent_returns_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """内置子代理的运行记录没有关联 Agent，agent_id 键仍然存在且值为 null。"""
    runs = [_build_run(run_id=2, member_name="Explore", agent_id=None)]

    data = await _get_subagent_runs(monkeypatch, runs)

    assert len(data) == 1
    assert data[0]["id"] == 2
    assert data[0]["member_name"] == "Explore"
    assert "agent_id" in data[0]
    assert data[0]["agent_id"] is None
