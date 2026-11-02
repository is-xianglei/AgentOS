"""运行时模型解析入口，数据库角色优先，兼容既有环境配置。"""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from llm import BaseLLMClient, create_llm_client
from model_service.models import ModelProviderRecord
from model_service.service import ModelService


async def resolve_runtime_client(
    db: AsyncSession, workspace_id: int | None, user_id: int | None,
    scenario: str, *, provider_id: str | None = None, effort: str | None = None,
) -> tuple[BaseLLMClient, ModelProviderRecord | None]:
    if workspace_id is not None:
        service = ModelService(db)
        provider = await service.select_provider(workspace_id, user_id, provider_id, scenario)
        if provider is not None:
            return service.build_client(provider, effort or "fast"), provider
    client = create_llm_client(scenario=scenario)
    if effort is not None:
        client.thinking_enabled = effort != "fast"
        if effort == "fast" and getattr(client, "provider", None) == "openai":
            # 旧环境没有声明兼容协议或none映射，仅保留模型默认行为。
            client.thinking_protocol = "openai"
        if effort != "fast":
            client.reasoning_effort = effort
    return client, None


async def session_client(
    db: AsyncSession, session: Any, scenario: str,
) -> tuple[BaseLLMClient, ModelProviderRecord | None]:
    return await resolve_runtime_client(
        db, session.workspace_id, session.user_id, scenario,
        provider_id=(getattr(session, "model_provider_id", None) if scenario == "agent" or (getattr(session, "extra", None) or {}).get("model_provider_override") else None),
        effort=getattr(session, "thinking_effort", "fast"),
    )


async def frozen_job_client(
    db: AsyncSession, workspace_id: int, user_id: int, scenario: str,
    *, model: str | None, provider_id: str | None,
) -> BaseLLMClient:
    """旧任务仅存环境模型名，不能迁往后来新增的数据库端点。"""
    if model and not provider_id:
        return create_llm_client(scenario=scenario)
    client, _ = await resolve_runtime_client(
        db, workspace_id, user_id, scenario, provider_id=provider_id,
    )
    return client
