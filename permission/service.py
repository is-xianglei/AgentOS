from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from permission.models import PermissionRuleRecord
from permission.repository import PermissionRepository
from tools.policy import TOOL_POLICIES, get_tool_policy

Scope = Literal["global", "session"]

Behavior = Literal["allow", "ask", "deny"]

# 默认策略:未被任何规则命中时的兜底判定(参考 s03_permission 的"危险工具需审批"精神)。
# 危险工具(可执行任意命令 / 有副作用)默认 ask;其余工具默认 allow。
# SkillRun:进程内执行 skill 脚本,等同在主机跑任意代码(PRD §3.5 / §17),与 Bash 同级需审批。
# Write/Edit:写入/修改文件有副作用,需审批。
# Skill / SkillResource / Read / Glob / Grep 为只读能力,默认放行(未知工具默认 allow),不纳入本集合。
# 审批默认值由工具能力策略派生，避免 PermissionService 与 ToolPolicy 各维护一份
# 危险工具名单。新增工具只需在 ToolPolicy 声明 requires_approval=True。
DANGEROUS_TOOLS: frozenset[str] = frozenset(
    name for name, policy in TOOL_POLICIES.items() if policy.requires_approval
)
DEFAULT_ALLOW: Behavior = "allow"
DEFAULT_ASK: Behavior = "ask"

# 内置定向默认策略:(agent_type, tool_name) -> behavior。
# verification 子代理必须真正跑测试命令才能给出 VERDICT,而子代理没有审批挂起通道,
# 若沿用 Bash 默认 ask 会直接坍缩成 deny,导致该 agent 结构上无法完成职责。
# 其命令另受 shell_policy 的 TMP_WRITABLE 档位约束(禁装包、禁 git 写、仅 tmp 可写),
# 故此处放行的是"受限的 Bash",不是完整 shell。用户可用同 agent_type 的显式规则覆盖。
BUILTIN_AGENT_DEFAULTS: dict[tuple[str, str], Behavior] = {
    ("verification", "Bash"): "allow",
}


class PermissionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = PermissionRepository(db)

    def default_behavior(self, tool_name: str, agent_type: str | None = None) -> Behavior:
        """无规则命中时的默认判定;内置定向策略优先于按工具的粗粒度默认值。"""
        if agent_type is not None:
            builtin = BUILTIN_AGENT_DEFAULTS.get((agent_type, tool_name))
            if builtin is not None:
                return builtin
        return DEFAULT_ASK if get_tool_policy(tool_name).requires_approval else DEFAULT_ALLOW

    async def evaluate(
        self,
        session_id: int,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        agent_type: str | None = None,
        workspace_id: int | None = None,
    ) -> Behavior:
        """判定某工具调用应 allow / ask / deny。

        优先级:会话级规则 > 当前工作区全局规则 > 内置定向默认 > 按工具默认。
        同一作用域内 matcher.agent_type 命中的定向规则优先于未限定 agent_type 的通用规则;
        agent_type=None 表示主代理,只会命中通用规则。

        workspace_id 是 global 规则的租户边界；未提供时不会读取任何 global 规则。
        """
        session_rule = await self.repo.find_match(
            "session",
            session_id,
            tool_name,
            agent_type,
            workspace_id,
        )
        if session_rule is not None:
            return self._coerce(session_rule.behavior, tool_name, agent_type)
        global_rule = await self.repo.find_match(
            "global",
            None,
            tool_name,
            agent_type,
            workspace_id,
        )
        if global_rule is not None:
            return self._coerce(global_rule.behavior, tool_name, agent_type)
        return self.default_behavior(tool_name, agent_type)

    async def add_always_allow(
        self,
        session_id: int,
        tool_name: str,
        scope: Literal["session", "global"] = "session",
        workspace_id: int | None = None,
    ) -> PermissionRuleRecord:
        """审批时"始终允许":落一条 behavior=allow 规则(默认会话级)。"""
        target_session = session_id if scope == "session" else None
        return await self.repo.upsert(
            scope=scope,
            session_id=target_session,
            tool_name=tool_name,
            behavior="allow",
            source="always_allow",
            workspace_id=workspace_id,
        )

    async def list_rules(
        self,
        scope: str | None = None,
        session_id: int | None = None,
        workspace_id: int | None = None,
    ) -> list[PermissionRuleRecord]:
        """按可选 scope / session_id 过滤列出规则(REST 只读,不涉及事务)。"""
        return await self.repo.list(
            scope=scope,
            session_id=session_id,
            workspace_id=workspace_id,
        )

    async def get_required(self, rule_id: int) -> PermissionRuleRecord:
        """读取规则；不存在或已软删除时统一返回业务错误。"""
        rule = await self.repo.get(rule_id)
        if rule is None or rule.is_deleted:
            raise AgentException.message("权限规则不存在", status_code=404)
        return rule

    async def upsert_rule(
        self,
        scope: Scope,
        session_id: int | None,
        tool_name: str,
        behavior: Behavior,
        matcher: dict[str, Any] | None = None,
        workspace_id: int | None = None,
    ) -> PermissionRuleRecord:
        """用户手动新增或更新一条规则,并提交事务。"""
        target_session = session_id if scope == "session" else None
        rule: PermissionRuleRecord = await self.repo.upsert(
            scope=scope,
            session_id=target_session,
            tool_name=tool_name,
            behavior=behavior,
            source="user",
            matcher=matcher,
            workspace_id=workspace_id,
        )
        return rule

    async def upsert_agent_rule(
        self,
        agent_type: str,
        tool_name: str,
        behavior: Behavior,
        scope: Scope = "global",
        session_id: int | None = None,
        workspace_id: int | None = None,
    ) -> PermissionRuleRecord:
        """为特定子代理类型落一条定向规则,覆盖 BUILTIN_AGENT_DEFAULTS。"""
        target_session = session_id if scope == "session" else None
        return await self.repo.upsert(
            scope=scope,
            session_id=target_session,
            tool_name=tool_name,
            behavior=behavior,
            source="user",
            matcher={"agent_type": agent_type},
            workspace_id=workspace_id,
        )

    async def delete_rule(self, rule_id: int) -> int:
        """删除一条规则;规则不存在时抛 AgentException。提交交给请求边界统一处理。"""
        deleted: int = await self.repo.delete(rule_id)
        if deleted == 0:
            raise AgentException.message("权限规则不存在")
        return deleted

    def _coerce(
        self, behavior: str, tool_name: str, agent_type: str | None = None
    ) -> Behavior:
        """把落库的 behavior 收敛到合法取值;非法值退回默认策略。"""
        if behavior in ("allow", "ask", "deny"):
            return behavior  # type: ignore[return-value]
        return self.default_behavior(tool_name, agent_type)
