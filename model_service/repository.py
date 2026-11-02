"""模型服务数据访问，事务由请求或运行时边界提交。"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model_service.models import ModelProviderRecord, ModelRoleRecord


class ModelServiceRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_providers(self, workspace_id: int) -> list[ModelProviderRecord]:
        statement = (
            select(ModelProviderRecord)
            .where(ModelProviderRecord.workspace_id == workspace_id)
            .order_by(ModelProviderRecord.created_at, ModelProviderRecord.provider_id)
        )
        return list(await self.db.scalars(statement))

    async def get_provider(self, workspace_id: int, provider_id: str) -> ModelProviderRecord | None:
        statement = select(ModelProviderRecord).where(
            ModelProviderRecord.workspace_id == workspace_id,
            ModelProviderRecord.provider_id == provider_id,
        )
        return await self.db.scalar(statement)

    async def list_roles(self, workspace_id: int) -> list[ModelRoleRecord]:
        statement = select(ModelRoleRecord).where(ModelRoleRecord.workspace_id == workspace_id)
        return list(await self.db.scalars(statement))

    async def get_role_including_deleted(
        self,
        workspace_id: int,
        role_key: str,
    ) -> ModelRoleRecord | None:
        statement = (
            select(ModelRoleRecord)
            .where(
                ModelRoleRecord.workspace_id == workspace_id,
                ModelRoleRecord.role_key == role_key,
            )
            .execution_options(include_deleted=True)
        )
        return await self.db.scalar(statement)

    async def save(self, record: ModelProviderRecord | ModelRoleRecord) -> None:
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)

    async def delete(self, record: ModelProviderRecord | ModelRoleRecord) -> None:
        record.is_deleted = True
        record.deleted_at = datetime.now(UTC)
        await self.db.flush()
