from fastapi import APIRouter

from api import health
from auth import api as auth_api
from memory import api as memory_api
from permission import api as permission_api
from session import api as session_api
from skill import api as skill_api
from task import api as task_api
from team import api as team_api
from tools import api as tools_api
from user import api as user_api
from workspace import api as workspace_api

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth_api.router, prefix="/auth", tags=["auth"])
api_router.include_router(memory_api.router, prefix="/memories", tags=["memories"])
api_router.include_router(session_api.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(tools_api.router, prefix="/tools", tags=["tools"])
api_router.include_router(task_api.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(team_api.router, prefix="/teams", tags=["teams"])
api_router.include_router(permission_api.router, prefix="/permissions", tags=["permissions"])
api_router.include_router(skill_api.router, prefix="/skills", tags=["skills"])
api_router.include_router(user_api.router, prefix="/users", tags=["users"])
api_router.include_router(workspace_api.router, prefix="/workspaces", tags=["workspaces"])
