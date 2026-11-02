"""工具目录对前端公开的可绑定身份必须与运行时策略一致。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import Request

from tools import api
from tools.policy import get_tool_policy
from tools.schemas import ToolDetailResponse, ToolResponse

pytestmark = pytest.mark.anyio


def _record(runtime_name: str = "Read", *, is_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        tool_key=f"builtin.{runtime_name}",
        name=runtime_name,
        description="测试工具",
        tool_type="builtin",
        runtime_name=runtime_name,
        is_system=True,
        is_enabled=is_enabled,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        updated_at=datetime(2026, 11, 1, tzinfo=UTC),
        input_schema={},
        implementation_config={},
    )


@pytest.mark.parametrize("runtime_name", ["Read", "AskUserQuestion", "Bash", "SkillRun", "Unknown"])
def test_catalog_metadata_comes_from_binding_policy(runtime_name: str) -> None:
    response = ToolResponse.model_validate(_record(runtime_name))
    profiles = response.model_dump()["bindable_profiles"]

    assert profiles == sorted(get_tool_policy(runtime_name).bindable_profiles)
    assert ("user_agent" in profiles) == (runtime_name == "Read")


def test_disabled_tool_preserves_policy_metadata() -> None:
    response = ToolResponse.model_validate(_record(is_enabled=False))

    assert response.is_enabled is False
    assert "user_agent" in response.bindable_profiles


def test_detail_inherits_binding_metadata() -> None:
    response = ToolDetailResponse.model_validate(_record("AskUserQuestion"))

    assert response.model_dump()["bindable_profiles"] == ["lead", "orchestrator"]
    assert response.input_schema == {}


async def test_list_endpoint_serializes_binding_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_tools(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(items=[_record()], total=1, limit=50, offset=0)

    monkeypatch.setattr(api, "ToolCatalogService", lambda _db: SimpleNamespace(list_tools=list_tools))
    response = await api.list_tools(
        request=Request({"type": "http", "headers": []}),
        db=None,
        current_user=None,
        workspace_id=1,
        keyword=None,
        is_enabled=True,
        limit=50,
        offset=0,
    )

    assert "user_agent" in response["data"].model_dump()["items"][0]["bindable_profiles"]
    assert response["error"] is None


async def test_detail_endpoint_serializes_binding_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_required(tool_id: int) -> SimpleNamespace:
        assert tool_id == 1
        return _record("AskUserQuestion")

    monkeypatch.setattr(
        api, "ToolCatalogService", lambda _db: SimpleNamespace(get_required=get_required)
    )
    response = await api.get_tool(
        tool_id=1,
        request=Request({"type": "http", "headers": []}),
        db=None,
        current_user=None,
        workspace_id=1,
    )

    assert response["data"].model_dump()["bindable_profiles"] == ["lead", "orchestrator"]
