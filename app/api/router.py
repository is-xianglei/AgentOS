from fastapi import APIRouter

from app.api import health, sessions, tasks, teams, tools

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router, tags=["health"])
api_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(tools.router, prefix="/tools", tags=["tools"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(teams.router, prefix="/teams", tags=["teams"])
