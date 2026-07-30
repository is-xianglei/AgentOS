from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, json_type


class PermissionRuleRecord(Base):
    __tablename__ = "permission_rules"
    __table_args__ = (
        # 判定按 (scope, session_id, tool_name) 查询;session 级优先于 global 级。
        Index(
            "ix_permission_rules_scope_session_tool",
            "scope",
            "session_id",
            "tool_name",
        ),
        {"comment": "工具权限规则(全局 / 会话双作用域)"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="规则ID")
    scope: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="作用域: global 全局 / session 会话级",
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="所属会话ID(scope=session 时必填, global 时为空)",
    )
    tool_name: Mapped[str] = mapped_column(
        String(120), index=True, comment="规则针对的工具名"
    )
    behavior: Mapped[str] = mapped_column(
        String(16),
        comment="判定行为: allow 放行 / ask 需审批 / deny 拒绝",
    )
    matcher: Mapped[dict[str, Any] | None] = mapped_column(
        json_type(),
        nullable=True,
        comment="细化匹配条件(预留, 空表示按 tool_name 全匹配)",
    )
    source: Mapped[str] = mapped_column(
        String(16),
        default="user",
        comment="规则来源: user 设置页配置 / always_allow 审批时始终允许落库",
    )
