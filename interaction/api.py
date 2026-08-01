from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from interaction.schemas import (
    InteractionResolutionRequest,
    PendingInteractionResponse,
    RuntimeSuspensionResponse,
)
from interaction.service import InteractionService
from runtime.agent import AgentRuntime, format_sse
from session.service import SessionService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


async def _require_session_access(
    db: AsyncSession,
    session_id: int,
    user_id: int,
    workspace_id: int,
) -> None:
    service = SessionService(db)
    session = await service.get_required(session_id)
    if not await service.check_access(session, user_id, workspace_id):
        raise AgentException.message("无权限访问该会话", status_code=403)


@router.get(
    "/sessions/{session_id}/pending",
    summary="查询会话待处理人工交互",
    response_model=ApiResponse[PendingInteractionResponse | None],
)
async def get_pending_interaction(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    await _require_session_access(db, session_id, current_user.id, workspace_id)
    pending = await InteractionService(db).get_pending_response(session_id)
    return ok(pending, request)


@router.post(
    "/sessions/{session_id}/requests/{request_id}/resolve",
    summary="响应人工交互并恢复Agent执行",
)
async def resolve_interaction(
    session_id: int,
    request_id: UUID,
    payload: InteractionResolutionRequest,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    await _require_session_access(db, session_id, current_user.id, workspace_id)
    runtime = AgentRuntime(
        db,
        user_id=current_user.id,
        workspace_id=workspace_id,
    )
    response_payload = payload.response_payload.model_dump(
        mode="json",
        by_alias=True,
    )
    # 在返回 StreamingResponse 前完成校验与响应落库，使 409 等业务错误保持
    # 普通 HTTP 语义；提交后即使 SSE 未建立，也可用相同响应继续恢复。
    await runtime.prepare_interaction_response(
        session_id,
        request_id,
        payload.kind,
        response_payload,
    )

    async def event_stream():
        async for event in runtime.resume(
            session_id,
            request_id,
            payload.kind,
            response_payload,
            response_prepared=True,
        ):
            yield format_sse(event)

    return StreamingResponse(event_stream(), media_type="text/event-stream; charset=utf-8")


@router.post(
    "/sessions/{session_id}/suspensions/{suspension_id}/cancel",
    summary="取消运行暂停点",
    response_model=ApiResponse[RuntimeSuspensionResponse],
)
async def cancel_interaction(
    session_id: int,
    suspension_id: UUID,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    await _require_session_access(db, session_id, current_user.id, workspace_id)
    suspension = await AgentRuntime(
        db,
        user_id=current_user.id,
        workspace_id=workspace_id,
    ).cancel_interaction(
        session_id,
        suspension_id,
    )
    return ok(RuntimeSuspensionResponse.model_validate(suspension), request)
