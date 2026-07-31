"""子代理权限裁决测试:agent_type 维度匹配、内置定向默认与 ask 坍缩。

子代理执行工具前必须经 PermissionService 裁决,否则它就是绕过审批的提权路径。
本文件用 Fake 仓储覆盖判定矩阵,不触真实 SQL;matcher 的 JSONB 落库另由集成测试覆盖。
"""

from typing import Any

import pytest

import database.registry  # noqa: F401
from permission.models import PermissionRuleRecord
from permission.repository import matcher_agent_type
from permission.service import BUILTIN_AGENT_DEFAULTS, PermissionService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _rule(
    rule_id: int,
    scope: str,
    tool_name: str,
    behavior: str,
    agent_type: str | None = None,
    session_id: int | None = None,
) -> PermissionRuleRecord:
    """构造一条内存中的规则记录,不入库。"""
    rule = PermissionRuleRecord()
    rule.id = rule_id
    rule.scope = scope
    rule.session_id = session_id
    rule.tool_name = tool_name
    rule.behavior = behavior
    rule.matcher = {"agent_type": agent_type} if agent_type else None
    return rule


class _FakeRepo:
    """只按 (scope, session_id, tool_name) 返回预置规则,匹配取舍交给被测代码。"""

    def __init__(self, rules: list[PermissionRuleRecord]):
        self.rules = rules

    async def find_candidates(
        self, scope: str, session_id: int | None, tool_name: str
    ) -> list[PermissionRuleRecord]:
        matched = [
            r
            for r in self.rules
            if r.scope == scope and r.session_id == session_id and r.tool_name == tool_name
        ]
        return sorted(matched, key=lambda r: r.id, reverse=True)


def _service(rules: list[PermissionRuleRecord]) -> PermissionService:
    """构造只替换仓储的 PermissionService,保留真实匹配与判定逻辑。"""
    from permission.repository import PermissionRepository

    service = PermissionService.__new__(PermissionService)
    repo = PermissionRepository.__new__(PermissionRepository)
    fake = _FakeRepo(rules)
    # 只替换取数,匹配优先级仍走真实的 find_match/find_exact。
    repo.find_candidates = fake.find_candidates  # type: ignore[method-assign]
    service.repo = repo
    service.db = None  # type: ignore[assignment]
    return service


class TestMatcherExtraction:
    @pytest.mark.parametrize(
        ("matcher", "expected"),
        [
            (None, None),
            ({}, None),
            ({"agent_type": "verification"}, "verification"),
            ({"agent_type": ""}, None),
            ({"agent_type": 123}, None),
            ({"other": "x"}, None),
        ],
    )
    def test_取值容错(self, matcher: dict[str, Any] | None, expected: str | None) -> None:
        """matcher 是自由 JSONB,脏数据不能让判定崩掉或误命中。"""
        assert matcher_agent_type(matcher) == expected


class TestBuiltinDefaults:
    def test_verification可用Bash(self) -> None:
        """它必须真跑测试才能给判定;子代理无审批通道,ask 会直接坍缩成拒绝。"""
        service = _service([])
        assert service.default_behavior("Bash", "verification") == "allow"

    def test_主代理的Bash仍需审批(self) -> None:
        service = _service([])
        assert service.default_behavior("Bash", None) == "ask"

    def test_其他子代理的Bash仍需审批(self) -> None:
        """定向放行只给 verification,不能顺带放开所有子代理。"""
        service = _service([])
        for agent_type in ("Explore", "Plan", "general-purpose"):
            assert service.default_behavior("Bash", agent_type) == "ask"

    def test_verification的写工具未被放行(self) -> None:
        """放行的只是受限 Bash,不是整套危险工具。"""
        service = _service([])
        for tool in ("Write", "Edit", "SkillRun"):
            assert service.default_behavior(tool, "verification") == "ask"

    def test_只读工具默认放行(self) -> None:
        service = _service([])
        for tool in ("Read", "Glob", "Grep"):
            assert service.default_behavior(tool, "verification") == "allow"
            assert service.default_behavior(tool, None) == "allow"

    def test_内置定向表只含预期条目(self) -> None:
        """这张表是提权面,新增条目应当是显式决定而非顺手加。"""
        assert BUILTIN_AGENT_DEFAULTS == {("verification", "Bash"): "allow"}


@pytest.mark.anyio
class TestAgentTypeMatching:
    async def test_定向规则优先于通用规则(self) -> None:
        rules = [
            _rule(1, "global", "Bash", "deny"),
            _rule(2, "global", "Bash", "allow", agent_type="verification"),
        ]
        service = _service(rules)
        assert await service.evaluate(1, "Bash", agent_type="verification") == "allow"

    async def test_定向规则不影响其他类型(self) -> None:
        rules = [
            _rule(1, "global", "Bash", "deny"),
            _rule(2, "global", "Bash", "allow", agent_type="verification"),
        ]
        service = _service(rules)
        assert await service.evaluate(1, "Bash", agent_type="Explore") == "deny"

    async def test_主代理不会误命中子代理的定向放行(self) -> None:
        """这是核心隔离断言:给 verification 开的口子不能泄给主代理。"""
        rules = [_rule(1, "global", "Bash", "allow", agent_type="verification")]
        service = _service(rules)
        assert await service.evaluate(1, "Bash", agent_type=None) == "ask"

    async def test_定向规则可收紧内置默认(self) -> None:
        """用户显式 deny 应压过内置的 allow。"""
        rules = [_rule(1, "global", "Bash", "deny", agent_type="verification")]
        service = _service(rules)
        assert await service.evaluate(1, "Bash", agent_type="verification") == "deny"

    async def test_会话级优先于全局(self) -> None:
        rules = [
            _rule(1, "global", "Bash", "allow", agent_type="verification"),
            _rule(2, "session", "Bash", "deny", agent_type="verification", session_id=7),
        ]
        service = _service(rules)
        assert await service.evaluate(7, "Bash", agent_type="verification") == "deny"
        # 换个会话则回落全局规则。
        assert await service.evaluate(9, "Bash", agent_type="verification") == "allow"

    async def test_同作用域内取最新一条(self) -> None:
        rules = [
            _rule(1, "global", "Read", "deny"),
            _rule(5, "global", "Read", "allow"),
        ]
        service = _service(rules)
        assert await service.evaluate(1, "Read") == "allow"

    async def test_非法behavior回落默认策略(self) -> None:
        rules = [_rule(1, "global", "Bash", "whatever")]
        service = _service(rules)
        assert await service.evaluate(1, "Bash", agent_type="verification") == "allow"
        assert await service.evaluate(1, "Bash", agent_type=None) == "ask"

    async def test_无规则时回落默认(self) -> None:
        service = _service([])
        assert await service.evaluate(1, "Bash", agent_type=None) == "ask"
        assert await service.evaluate(1, "Read", agent_type=None) == "allow"
