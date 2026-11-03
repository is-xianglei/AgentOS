"""模型服务的公开请求与响应契约。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

ProviderType = Literal["chat", "embedding", "reranker", "audio"]
ReasoningKey = Literal["low", "medium", "high", "xhigh", "max"]
ThinkingEffort = Literal["fast", "low", "medium", "high", "xhigh", "max"]
ModeSlug = Literal["standard", "turbo"]


class ModelProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=100)
    provider_type: ProviderType = "chat"
    provider: str = Field(min_length=1, max_length=40)
    base_url: str = ""
    api_key: SecretStr | None = None
    model_name: str = Field(min_length=1, max_length=200)
    extra_config: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True

    @field_validator("display_name", "model_name", "provider", "base_url", mode="before")
    @classmethod
    def trim_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ModelProviderUpdate(BaseModel):
    """更新允许省略字段；显式空密钥保留已有值，其余字段不能为 null。"""

    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    provider_type: ProviderType | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=40)
    base_url: str | None = None
    api_key: SecretStr | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    extra_config: dict[str, Any] | None = None
    is_active: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        for field, item in value.items():
            if field != "api_key" and item is None:
                raise ValueError("更新字段不能为 null；留空密钥表示保留已有密钥")
        return value


class ModelProviderResponse(BaseModel):
    model_config = {"from_attributes": True}
    provider_id: str
    display_name: str
    provider_type: ProviderType
    provider: str
    base_url: str
    api_key: str
    model_name: str
    extra_config: dict[str, Any]
    is_active: bool
    last_tested_at: datetime | None = None
    last_test_status: Literal["success", "failed"] | None = None


class ModelProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    provider_type: ProviderType = "chat"
    base_url: str = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    model_name: str
    provider_id: str | None = None


class ModelTestResponse(BaseModel):
    success: bool
    latency_ms: int
    error: str | None = None


class ModelRoleRequest(BaseModel):
    provider_id: str


class ModelRoleResponse(BaseModel):
    role_key: str
    label: str
    required_type: ProviderType = "chat"
    provider_id: str | None = None
    provider_name: str | None = None


class ModelPreferencesRequest(BaseModel):
    default_model_provider_id: str | None = None
