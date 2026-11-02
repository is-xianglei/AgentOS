"""用户管理鉴权与会话请求契约回归测试。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api import deps
from core.errors import AgentException
from session.schemas import SessionRenameRequest, SessionSendMessageRequest
from user import api as user_api


def _route(path: str, method: str):
    return next(
        route
        for route in user_api.router.routes
        if route.path == path and method in route.methods
    )


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("", "POST"),
        ("", "GET"),
        ("/{user_id}", "GET"),
        ("/{user_id}", "PATCH"),
        ("/{user_id}", "DELETE"),
        ("/{user_id}/change-password", "POST"),
    ],
)
def test_user_id_routes_require_workspace_admin(path: str, method: str) -> None:
    route = _route(path, method)
    assert any(dependency.call is deps.get_current_admin_user for dependency in route.dependant.dependencies)


def test_me_routes_keep_active_user_authentication() -> None:
    for path, method in (("/me", "GET"), ("/me", "PATCH"), ("/me", "DELETE")):
        route = _route(path, method)
        assert any(
            dependency.call is deps.get_current_active_user
            for dependency in route.dependant.dependencies
        )


@pytest.mark.anyio
async def test_admin_dependency_requires_owner_or_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, set[str]]] = []

    class FakeWorkspaceService:
        def __init__(self, db):
            self.db = db

        async def require_workspace_role(self, workspace_id, user_id, roles):
            calls.append((workspace_id, user_id, set(roles)))
            raise AgentException.message("无权执行该工作区操作", status_code=403)

    monkeypatch.setattr(deps, "WorkspaceService", FakeWorkspaceService)
    with pytest.raises(AgentException) as exc_info:
        await deps.get_current_admin_user(SimpleNamespace(id=7), 11, object())

    assert exc_info.value.status_code == 403
    assert calls == [(11, 7, {"owner", "admin"})]


def test_session_requests_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SessionRenameRequest.model_validate({"title": "新标题", "unexpected": True})


def test_session_request_rejects_unsupported_orchestration_plan_id() -> None:
    with pytest.raises(ValidationError, match="orchestration_plan_id"):
        SessionSendMessageRequest.model_validate(
            {"content": "开始执行", "orchestration_plan_id": 123}
        )
