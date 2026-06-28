from logging.config import fileConfig
import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

from app.db.base import Base
from app import models  # noqa: F401

load_dotenv()

config = context.config

database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise RuntimeError("未配置 DATABASE_URL，请在 .env 中设置 PostgreSQL 连接地址")
if not database_url.startswith("postgresql"):
    raise RuntimeError("DATABASE_URL 必须使用 PostgreSQL 连接地址")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(database_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
