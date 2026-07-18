from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, with_loader_criteria

from core.config import settings
from db.base import Base

DATABASE_URL = settings.database_url

# Supabase pooler(事务模式)不支持服务端 prepared statements,
# psycopg3 下需禁用 prepare,否则并发会话会报 DuplicatePreparedStatement。
engine = create_async_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args={"prepare_threshold": None},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=AsyncSession,
)


@event.listens_for(Session, "do_orm_execute")
def _filter_soft_deleted(state) -> None:
    """全局软删除读过滤:所有实体 SELECT 自动追加 is_deleted = false。

    挂在 Base 上对每个子类生效,新增模型无需改动、不会漏。
    AsyncSession 底层委托同步 Session,故监听 Session 即可覆盖异步执行。
    需要读到已删除行时(回收站/管理端),给语句或会话传
    execution_options(include_deleted=True) 即可跳过本过滤。
    """
    if (
        not state.is_select
        or state.is_column_load
        or state.is_relationship_load
        or state.execution_options.get("include_deleted", False)
    ):
        return
    state.statement = state.statement.options(
        with_loader_criteria(
            Base,
            lambda cls: cls.is_deleted.is_(False),
            include_aliases=True,
        )
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as db:
        try:
            yield db
        except Exception:
            await db.rollback()
            raise
