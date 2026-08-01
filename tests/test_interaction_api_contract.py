"""人工交互 API 路由、判别联合与访问隔离契约测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.responses import StreamingResponse

from core.errors import AgentException
# 先初始化 tools 包，保证 main 导入完整路由时模型注册顺序稳定。
from tools.registry import build_tool_registry  # noqa: F401
from interaction import api as interaction_api
from interaction.schemas import UserQuestionResolutionRequest
from main import app


def test_OpenAPI暴露新交互和计划端点并移除旧审批入口() -> None:
    paths = app.openapi()["paths"]

    assert "/api/interactions/sessions/{session_id}/pending" in paths
    assert "/api/interactions/sessions/{session_id}/requests/{request_id}/resolve" in paths
    assert "/api/interactions/sessions/{session_id}/suspensions/{suspension_id}/cancel" in paths
    assert "/api/plans/{session_id}" in paths
    assert "/api/sessions/{session_id}/approvals" not in paths


def test_响应请求使用kind判别四种载荷() -> None:
    operation = app.openapi()["paths"][
        "/api/interactions/sessions/{session_id}/requests/{request_id}/resolve"
    ]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert body_schema["discriminator"]["propertyName"] == "kind"
    assert set(body_schema["discriminator"]["mapping"]) == {
        "tool_approval",
        "user_question",
        "enter_plan_mode",
        "exit_plan_mode",
    }


def test_暂停点响应不暴露内部continuation() -> None:
    schemas = app.openapi()["components"]["schemas"]
    properties = schemas["RuntimeSuspensionResponse"]["properties"]

    assert "continuation_payload" not in properties
    assert "resolution_policy" not in properties


@pytest.mark.anyio
async def test_交互查询拒绝无会话权限的用户(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(
        get_required=AsyncMock(return_value=SimpleNamespace(id=1)),
        check_access=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(interaction_api, "SessionService", lambda db: service)

    with pytest.raises(AgentException) as exc_info:
        await interaction_api._require_session_access(
            SimpleNamespace(),
            session_id=1,
            user_id=9,
            workspace_id=7,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_响应在建立SSE前完成持久化预校验(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = uuid4()
    runtime = SimpleNamespace(prepare_interaction_response=AsyncMock())
    monkeypatch.setattr(interaction_api, "_require_session_access", AsyncMock())
    monkeypatch.setattr(interaction_api, "AgentRuntime", lambda *args, **kwargs: runtime)
    payload = UserQuestionResolutionRequest.model_validate(
        {
            "kind": "user_question",
            "response_payload": {
                "decision": "submit",
                "answers": {"选择哪种方案？": "方案A"},
            },
        }
    )

    response = await interaction_api.resolve_interaction(
        session_id=1,
        request_id=request_id,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=9),
        workspace_id=7,
    )

    assert isinstance(response, StreamingResponse)
    runtime.prepare_interaction_response.assert_awaited_once_with(
        1,
        request_id,
        "user_question",
        {
            "decision": "submit",
            "answers": {"选择哪种方案？": "方案A"},
            "annotations": {},
            "message": None,
        },
    )


@pytest.mark.anyio
async def test_不同响应冲突在建立SSE前保留HTTP409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        prepare_interaction_response=AsyncMock(
            side_effect=AgentException.message(
                "人工交互请求已使用不同响应处理",
                status_code=409,
            )
        )
    )
    monkeypatch.setattr(interaction_api, "_require_session_access", AsyncMock())
    monkeypatch.setattr(interaction_api, "AgentRuntime", lambda *args, **kwargs: runtime)
    payload = UserQuestionResolutionRequest.model_validate(
        {
            "kind": "user_question",
            "response_payload": {
                "decision": "submit",
                "answers": {"选择哪种方案？": "方案B"},
            },
        }
    )

    with pytest.raises(AgentException) as exc_info:
        await interaction_api.resolve_interaction(
            session_id=1,
            request_id=uuid4(),
            payload=payload,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=9),
            workspace_id=7,
        )

    assert exc_info.value.status_code == 409
