"""ModelResolver 的实现：工作区模型配置优先，没有可用配置时回落 .env 的全局 provider。

会话、子代理、Workflow 与 Memory 任务的模型选择规则都收在这里。.env 路径不再改客户端属性，
思考强度只放进返回的 ModelSettings，由客户端按调用翻译成各家参数。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from harness.adapters.models.builder import build_resolved_model
from harness.adapters.models.factory import create_model_client
from harness.app.ports import Effort, ModelSettings, ResolvedModel, SessionModelOptions
from model_service.service import ModelService


def _env_resolved(scenario: str, effort: Effort | None) -> ResolvedModel:
    """按 .env 的全局 provider 与场景模型建客户端；模型名取工厂按场景解析出的结果。"""
    client = create_model_client(scenario=scenario)
    return ResolvedModel(client=client, settings=ModelSettings(model_id=client.model, effort=effort))


class DatabaseModelResolver:
    """按数据库会话解析模型；每个请求或后台任务的数据库会话各建一个实例。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve(
        self,
        workspace_id: int | None,
        user_id: int | None,
        scenario: str,
        *,
        provider_id: str | None = None,
        effort: Effort | None = None,
    ) -> ResolvedModel:
        """优先用工作区的模型服务配置；没有可用配置时回落到 .env 的全局 provider。

        工作区端点未指定思考强度时按 fast 处理；.env 端点未指定时沿用 {provider}_thinking_*。
        """
        if workspace_id is not None:
            record = await ModelService(self.db).select_provider(
                workspace_id, user_id, provider_id, scenario
            )
            if record is not None:
                return build_resolved_model(record, effort)
        return _env_resolved(scenario, effort)

    async def for_session(self, session: SessionModelOptions, scenario: str) -> ResolvedModel:
        """按会话保存的模型选项解析。

        agent 场景始终使用会话上的供应商；其余场景只有用户显式指定过供应商时才沿用，
        否则交给工作区为该场景配置的角色。
        """
        provider_id = None
        if scenario == "agent":
            provider_id = getattr(session, "model_provider_id", None)
        else:
            session_extra: dict[str, Any] = getattr(session, "extra", None) or {}
            if session_extra.get("model_provider_override"):
                provider_id = getattr(session, "model_provider_id", None)
        return await self.resolve(
            session.workspace_id,
            session.user_id,
            scenario,
            provider_id=provider_id,
            effort=Effort.parse(getattr(session, "thinking_effort", Effort.FAST)),
        )

    async def for_frozen_job(
        self,
        workspace_id: int,
        user_id: int,
        scenario: str,
        *,
        model: str | None,
        provider_id: str | None,
    ) -> ResolvedModel:
        """按后台任务入队时冻结的模型解析。

        旧任务只存了 .env 模型名、没有 provider，不能迁往后来新增的数据库端点；
        冻结的模型名由调用方写进 ModelSettings.model_id，这里只负责选端点。
        """
        if model and not provider_id:
            return _env_resolved(scenario, None)
        return await self.resolve(workspace_id, user_id, scenario, provider_id=provider_id)
