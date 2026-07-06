from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permission import PermissionRuleRecord
from app.repositories.permission_repo import PermissionRepository

Behavior = Literal["allow", "ask", "deny"]

# 默认策略:未被任何规则命中时的兜底判定(参考 s03_permission 的"危险工具需审批"精神)。
# 危险工具(可执行任意命令 / 有副作用)默认 ask;其余工具默认 allow。
# 写文件类工具当前未内置,后续接入时在此登记。
DANGEROUS_TOOLS: frozenset[str] = frozenset({"Bash"})
DEFAULT_ALLOW: Behavior = "allow"
DEFAULT_ASK: Behavior = "ask"


class PermissionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = PermissionRepository(db)

    def default_behavior(self, tool_name: str) -> Behavior:
        """无规则命中时的默认判定。"""
        return DEFAULT_ASK if tool_name in DANGEROUS_TOOLS else DEFAULT_ALLOW

    async def evaluate(
        self,
        session_id: int,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
    ) -> Behavior:
        """判定某工具调用应 allow / ask / deny。

        优先级:会话级规则 > 全局规则 > 默认策略。matcher 本期只按 tool_name 全匹配。
        """
        session_rule = await self.repo.find_match("session", session_id, tool_name)
        if session_rule is not None:
            return self._coerce(session_rule.behavior, tool_name)
        global_rule = await self.repo.find_match("global", None, tool_name)
        if global_rule is not None:
            return self._coerce(global_rule.behavior, tool_name)
        return self.default_behavior(tool_name)

    async def add_always_allow(
        self,
        session_id: int,
        tool_name: str,
        scope: Literal["session", "global"] = "session",
    ) -> PermissionRuleRecord:
        """审批时"始终允许":落一条 behavior=allow 规则(默认会话级)。"""
        target_session = session_id if scope == "session" else None
        return await self.repo.upsert(
            scope=scope,
            session_id=target_session,
            tool_name=tool_name,
            behavior="allow",
            source="always_allow",
        )

    def _coerce(self, behavior: str, tool_name: str) -> Behavior:
        """把落库的 behavior 收敛到合法取值;非法值退回默认策略。"""
        if behavior in ("allow", "ask", "deny"):
            return behavior  # type: ignore[return-value]
        return self.default_behavior(tool_name)
