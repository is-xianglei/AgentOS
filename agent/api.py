from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models import AgentRecord
from agent.schemas import (
    AgentCreateRequest,
    AgentDeleteResult,
    AgentListResponse,
    AgentResponse,
    AgentSkillBindingsRequest,
    AgentToolBindingsRequest,
    AgentUpdateRequest,
)
from agent.service import AgentService, AgentUpdateFields
from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.responses import ApiResponse, ok
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


def _agent_response(record: AgentRecord) -> AgentResponse:
    response = AgentResponse.model_validate(record)
    return response.model_copy(
        update={
            "tool_ids": sorted(binding.tool_id for binding in record.tool_bindings),
            "skill_ids": sorted(binding.skill_id for binding in record.skill_bindings),
        }
    )


@router.post(
    "",
    summary="创建Agent",
    response_model=ApiResponse[AgentResponse],
)
async def create_agent(
    payload: AgentCreateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    record = await AgentService(db).create_agent(
        workspace_id=workspace_id,
        actor_user_id=current_user.id,
        **payload.model_dump(),
    )
    return ok(_agent_response(record), request)


@router.get(
    "",
    summary="查询Agent列表",
    response_model=ApiResponse[AgentListResponse],
)
async def list_agents(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    keyword: str | None = Query(default=None, max_length=128),
    is_enabled: bool | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    page = await AgentService(db).list_agents(
        workspace_id,
        current_user.id,
        keyword=keyword,
        is_enabled=is_enabled,
        limit=limit,
        offset=offset,
    )
    return ok(
        AgentListResponse(
            items=[_agent_response(record) for record in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        ),
        request,
    )


@router.get(
    "/{agent_id}",
    summary="查询Agent详情",
    response_model=ApiResponse[AgentResponse],
)
async def get_agent(
    agent_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    record = await AgentService(db).get_agent(workspace_id, agent_id, current_user.id)
    return ok(_agent_response(record), request)


@router.patch(
    "/{agent_id}",
    summary="更新Agent",
    response_model=ApiResponse[AgentResponse],
)
async def update_agent(
    agent_id: int,
    payload: AgentUpdateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    updates = cast(AgentUpdateFields, payload.model_dump(exclude_unset=True))
    record = await AgentService(db).update_agent(
        workspace_id,
        agent_id,
        current_user.id,
        updates,
    )
    return ok(_agent_response(record), request)


@router.delete(
    "/{agent_id}",
    summary="删除Agent",
    response_model=ApiResponse[AgentDeleteResult],
)
async def delete_agent(
    agent_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    await AgentService(db).delete_agent(workspace_id, agent_id, current_user.id)
    return ok(AgentDeleteResult(deleted=True), request)


@router.put(
    "/{agent_id}/tools",
    summary="全量替换Agent工具",
    response_model=ApiResponse[AgentResponse],
)
async def replace_agent_tools(
    agent_id: int,
    payload: AgentToolBindingsRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    record = await AgentService(db).replace_tool_bindings(
        workspace_id,
        agent_id,
        current_user.id,
        payload.tool_ids,
    )
    return ok(_agent_response(record), request)


@router.put(
    "/{agent_id}/skills",
    summary="全量替换Agent Skill",
    response_model=ApiResponse[AgentResponse],
)
async def replace_agent_skills(
    agent_id: int,
    payload: AgentSkillBindingsRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    record = await AgentService(db).replace_skill_bindings(
        workspace_id,
        agent_id,
        current_user.id,
        payload.skill_ids,
    )
    return ok(_agent_response(record), request)
