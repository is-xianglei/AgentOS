from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core import config
from app.core.errors import AppError, app_error_handler, unhandled_error_handler, validation_error_handler
from app.core.logging import request_context_middleware, setup_logging
from app.db.base import Base
from app.db.session import engine
from app import models  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.get("AGENTOS_CREATE_TABLES") == "1":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    yield


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(title="AgentOS API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(request_context_middleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.include_router(api_router)

    return app


app = create_app()

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
