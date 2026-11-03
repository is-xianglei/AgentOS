"""运行时模型解析入口，数据库角色优先，兼容既有环境配置。"""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from llm import BaseLLMClient, create_llm_client
from model_service.models import ModelProviderRecord
from model_service.service import ModelService


async def resolve_runtime_client(
    db: AsyncSession,
    workspace_id: int | None,
    user_id: int | None,
    scenario: str,
    *,
    provider_id: str | None = None,
    effort: str | None = None,
) -> tuple[BaseLLMClient, ModelProviderRecord | None]:
    """优先使用工作区的模型服务配置；没有可用配置时回落到 .env 的全局 provider。

    返回的 provider 为 None 表示走了 .env 回落路径。
    """
    if workspace_id is not None:
        service = ModelService(db)
        provider = await service.select_provider(workspace_id, user_id, provider_id, scenario)
        if provider is not None:
            return service.build_client(provider, effort or "fast"), provider

    client = create_llm_client(scenario=scenario)
    if effort is None:
        return client, None

    # .env 客户端没有思考强度配置，按会话的 effort 就地调整。
    client.thinking_enabled = effort != "fast"
    if effort == "fast" and getattr(client, "provider", None) == "openai":
        # 旧环境没有声明兼容协议或none映射，仅保留模型默认行为。
        client.thinking_protocol = "openai"
    if effort != "fast":
        client.reasoning_effort = effort
    return client, None


async def session_client(
    db: AsyncSession,
    session: Any,
    scenario: str,
) -> tuple[BaseLLMClient, ModelProviderRecord | None]:
    """按会话保存的模型选项解析客户端。"""
    # agent 场景始终使用会话上的供应商；其余场景只有用户显式指定过供应商时才沿用，
    # 否则交给工作区为该场景配置的角色。
    provider_id = None
    if scenario == "agent":
        provider_id = getattr(session, "model_provider_id", None)
    else:
        session_extra = getattr(session, "extra", None) or {}
        if session_extra.get("model_provider_override"):
            provider_id = getattr(session, "model_provider_id", None)

    return await resolve_runtime_client(
        db,
        session.workspace_id,
        session.user_id,
        scenario,
        provider_id=provider_id,
        effort=getattr(session, "thinking_effort", "fast"),
    )


async def frozen_job_client(
    db: AsyncSession,
    workspace_id: int,
    user_id: int,
    scenario: str,
    *,
    model: str | None,
    provider_id: str | None,
) -> BaseLLMClient:
    """旧任务仅存环境模型名，不能迁往后来新增的数据库端点。"""
    if model and not provider_id:
        return create_llm_client(scenario=scenario)
    client, _ = await resolve_runtime_client(
        db,
        workspace_id,
        user_id,
        scenario,
        provider_id=provider_id,
    )
    return client
