"""工具网关执行身份的不可变边界测试。"""

from pathlib import Path

import pytest

from tools.context import ToolExecutionContext
from tools.gateway import ToolGateway
from tools.shell_policy import ShellAccess


def test_execution_context_freezes_resource_boundaries(tmp_path: Path) -> None:
    context = ToolExecutionContext(
        profile="user_agent",
        principal_name="agent:7",
        agent_id=7,
        allowed_skill_names={"approved"},
        shell_access=ShellAccess.READ_ONLY,
        shell_tmp_root=tmp_path,
    )

    assert context.allowed_skill_names == frozenset({"approved"})
    assert context.shell_tmp_root == tmp_path
    with pytest.raises(AttributeError):
        context.agent_id = 8  # type: ignore[misc]


def test_gateway_exposes_the_same_frozen_context() -> None:
    gateway = ToolGateway.create(
        None,
        "user_agent",
        principal_name="agent:7",
        agent_id=7,
        requested_names=["Read"],
        allowed_skill_names=frozenset({"approved"}),
    )

    assert gateway.context.profile == "user_agent"
    assert gateway.context.principal_name == "agent:7"
    assert gateway.context.agent_id == 7
    assert gateway.context.allowed_skill_names == frozenset({"approved"})
    assert gateway.context is not None
