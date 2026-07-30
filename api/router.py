from fastapi import APIRouter

from api import (
    auth,
    health,
    memories,
    skills,
    tasks,
    teams,
    tools,
    users,
)
from permission import api as permission_api
from session import api as session_api
from workspace import api as workspace_api

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(memories.router, prefix="/memories", tags=["memories"])
api_router.include_router(session_api.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(tools.router, prefix="/tools", tags=["tools"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(teams.router, prefix="/teams", tags=["teams"])
api_router.include_router(permission_api.router, prefix="/permissions", tags=["permissions"])
api_router.include_router(skills.router, prefix="/skills", tags=["skills"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(workspace_api.router, prefix="/workspaces", tags=["workspaces"])
