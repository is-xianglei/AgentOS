from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import config

DATABASE_URL = config.DATABASE_URL

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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as db:
        try:
            yield db
        except Exception:
            await db.rollback()
            raise
