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
from database.base import Base
from database.engine import AsyncSessionLocal, engine
from hooks.builtin import register_builtin_hooks
from tools.service import ToolCatalogService


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    yield


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
