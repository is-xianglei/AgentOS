import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from api.router import api_router
from core.config import settings
from core.errors import (
    AgentException,
    agent_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from core.logging import request_context_middleware, setup_logging
from core.run_events import check_run_event_store, close_run_event_store
from database.base import Base
from database.engine import AsyncSessionLocal, engine
from hooks.builtin import register_builtin_hooks
from llm import create_llm_client
from runtime.execution import shutdown_executions
from runtime.recovery import reap_orphan_turns, run_reaper_loop
from tools.service import ToolCatalogService


@asynccontextmanager
async def lifespan(app: FastAPI):
    # LLM 配置启动自检:provider/api_key/模型缺失在启动时立刻暴露,
    # 而非拖到首次对话或首个 Memory 作业(工厂本身仍是权威关卡)。
    create_llm_client()
    if settings.create_tables:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        try:
            await ToolCatalogService(db).sync_builtins()
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    # run 事件流自检:Redis 缺配置或不可达时启动即失败,不静默降级到本地后端。
    await check_run_event_store()
    # 启动时回收一次:含已过期租约,以及迁移前遗留的 running 且无租约的轮次。
    await reap_orphan_turns(include_unleased_running=True)
    reaper = asyncio.create_task(run_reaper_loop())
    try:
        yield
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        # 取消本进程执行,让它们走取消路径收尾并清除租约,再关闭连接池。
        await shutdown_executions()
        await close_run_event_store()


def create_app() -> FastAPI:
    setup_logging()
    register_builtin_hooks()
    app = FastAPI(title="AgentOS API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(request_context_middleware)
    app.add_exception_handler(AgentException, agent_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.include_router(api_router)
    return app


import uvicorn

if __name__ == "__main__":
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)
else:
    app = create_app()
