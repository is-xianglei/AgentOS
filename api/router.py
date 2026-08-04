from fastapi import APIRouter

from agent import api as agent_api
from auth import api as auth_api
from department import api as department_api
from group import api as group_api
from interaction import api as interaction_api
from memory import api as memory_api
from permission import api as permission_api
from plan import api as plan_api
from session import api as session_api
from skill import api as skill_api
from task import api as task_api
from team import api as team_api
from tools import api as tools_api
from user import api as user_api
from workspace import api as workspace_api

api_router = APIRouter(prefix="/api")
api_router.include_router(agent_api.router, prefix="/agents", tags=["agents"])
api_router.include_router(auth_api.router, prefix="/auth", tags=["auth"])
api_router.include_router(
    department_api.router,
    prefix="/workspaces",
    tags=["departments"],
)
api_router.include_router(group_api.router, tags=["groups"])
api_router.include_router(interaction_api.router, prefix="/interactions", tags=["interactions"])
api_router.include_router(memory_api.router, prefix="/memories", tags=["memories"])
api_router.include_router(plan_api.router, prefix="/plans", tags=["plans"])
api_router.include_router(session_api.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(tools_api.router, prefix="/tools", tags=["tools"])
api_router.include_router(task_api.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(team_api.router, prefix="/teams", tags=["teams"])
api_router.include_router(permission_api.router, prefix="/permissions", tags=["permissions"])
api_router.include_router(skill_api.router, prefix="/skills", tags=["skills"])
api_router.include_router(user_api.router, prefix="/users", tags=["users"])
api_router.include_router(workspace_api.router, prefix="/workspaces", tags=["workspaces"])
