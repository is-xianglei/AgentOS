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
    """请求级事务边界:一次 HTTP 请求 = 一个事务。

    service / repository 层只做 add / flush,绝不 commit;请求处理成功后由本依赖
    统一 commit,任何异常则整体 rollback。这样跨 service 的多步写入(如注册时建
    user + 建 workspace + 建 member)要么全部成功,要么全部回滚,不留孤儿数据。

    注:流式 Agent 执行(agent_runtime / subagent_runner)在后台任务里自行按步
    commit 做增量持久化,其 session 用完后此处的收尾 commit 已无未提交变更,是安全的空操作。
    """
    async with AsyncSessionLocal() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
