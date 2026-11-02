"""Agent 工具调用预算。

预算是运行时控制面的一部分，和 PermissionService 的单次 allow/ask/deny
分工不同：权限决定某次调用是否有资格执行，预算决定当前执行是否还允许
继续消耗工具调用额度。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallBudget:
    """限制一次运行中的总工具调用数和单工具调用数。"""

    total_limit: int
    per_tool_limit: int
    total_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成可放入 suspension continuation 的 JSON 结构。"""
        return {
            "total_limit": self.total_limit,
            "per_tool_limit": self.per_tool_limit,
            "total_calls": self.total_calls,
            "calls_by_tool": dict(self.calls_by_tool),
        }

    snapshot = to_dict

    @classmethod
    def from_dict(cls, payload: Any, *, default: "ToolCallBudget") -> "ToolCallBudget":
        """从持久化 continuation 恢复预算；旧 continuation 回落到 default。"""
        if not isinstance(payload, dict):
            return default
        try:
            total_limit = int(payload["total_limit"])
            per_tool_limit = int(payload["per_tool_limit"])
            total_calls = int(payload.get("total_calls", 0))
            calls_by_tool_raw = payload.get("calls_by_tool", {})
            if not isinstance(calls_by_tool_raw, dict):
                return default
            calls_by_tool = {
                str(name): int(count)
                for name, count in calls_by_tool_raw.items()
            }
        except (TypeError, ValueError, KeyError):
            return default
        if total_limit < 1 or per_tool_limit < 1 or total_calls < 0:
            return default
        if any(count < 0 for count in calls_by_tool.values()):
            return default
        return cls(
            total_limit=total_limit,
            per_tool_limit=per_tool_limit,
            total_calls=total_calls,
            calls_by_tool=calls_by_tool,
        )

    @classmethod
    def from_snapshot(
        cls, payload: Any, *, default: "ToolCallBudget"
    ) -> "ToolCallBudget":
        return cls.from_dict(payload, default=default)

    @property
    def remaining(self) -> int:
        return max(0, self.total_limit - self.total_calls)

    def try_consume(self, tool_name: str) -> tuple[bool, str | None]:
        """消耗一次额度，返回 (是否允许, 面向模型的错误原因)。"""
        if self.total_calls >= self.total_limit:
            return (
                False,
                f"error: 本次运行的工具调用总额度已用尽（上限 {self.total_limit} 次）。",
            )
        current = self.calls_by_tool.get(tool_name, 0)
        if current >= self.per_tool_limit:
            return (
                False,
                f"error: 工具 {tool_name} 的调用额度已用尽（上限 {self.per_tool_limit} 次）。",
            )
        self.total_calls += 1
        self.calls_by_tool[tool_name] = current + 1
        return True, None

