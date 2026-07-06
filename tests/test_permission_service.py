"""PermissionService 默认策略的纯逻辑测试(不依赖数据库)。

evaluate 的规则命中/优先级依赖 DB,由端到端验证覆盖(见方案 Task 7);
此处只测不需 DB 的默认策略与 behavior 收敛。
"""

from app.services.permission_service import DANGEROUS_TOOLS, PermissionService


def _service() -> PermissionService:
    # default_behavior / _coerce 不触达 DB,可传 None 占位。
    return PermissionService(db=None)  # type: ignore[arg-type]


def test_default_behavior_dangerous_tool_asks():
    svc = _service()
    for tool in DANGEROUS_TOOLS:
        assert svc.default_behavior(tool) == "ask"


def test_default_behavior_safe_tool_allows():
    svc = _service()
    assert svc.default_behavior("TaskCreate") == "allow"
    assert svc.default_behavior("SendMessage") == "allow"


def test_coerce_valid_behavior_passthrough():
    svc = _service()
    assert svc._coerce("allow", "Bash") == "allow"
    assert svc._coerce("ask", "TaskCreate") == "ask"
    assert svc._coerce("deny", "Bash") == "deny"


def test_coerce_invalid_behavior_falls_back_to_default():
    svc = _service()
    # 非法落库值 → 退回默认策略(危险工具 ask,安全工具 allow)。
    assert svc._coerce("garbage", "Bash") == "ask"
    assert svc._coerce("", "TaskCreate") == "allow"
