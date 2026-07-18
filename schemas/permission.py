from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PermissionRuleCreateRequest(BaseModel):
    scope: Literal["global", "session"] = Field(description="作用域: global 全局 / session 会话级")
    session_id: int | None = Field(default=None, description="会话ID(scope=session 时必填)")
    tool_name: str = Field(min_length=1, max_length=120, description="规则针对的工具名")
    behavior: Literal["allow", "ask", "deny"] = Field(description="判定行为")
    matcher: dict[str, Any] | None = Field(default=None, description="细化匹配条件(预留)")

    @model_validator(mode="after")
    def _check_session(self) -> "PermissionRuleCreateRequest":
        if self.scope == "session" and self.session_id is None:
            raise ValueError("scope=session 时必须提供 session_id")
        if self.scope == "global":
            # 全局规则不绑定会话,忽略传入的 session_id。
            self.session_id = None
        return self


class PermissionRuleResponse(BaseModel):
    id: int = Field(description="规则ID")
    scope: str = Field(description="作用域")
    session_id: int | None = Field(default=None, description="所属会话ID")
    tool_name: str = Field(description="工具名")
    behavior: str = Field(description="判定行为")
    matcher: dict[str, Any] | None = Field(default=None, description="细化匹配条件")
    source: str = Field(description="规则来源: user / always_allow")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class PermissionRuleDeleteResult(BaseModel):
    deleted: int = Field(description="实际删除的规则数量")
