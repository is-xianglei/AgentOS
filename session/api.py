from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from runtime.agent import AgentRuntime, format_sse
from session.schemas import (
    SessionBatchDeleteRequest,
    SessionDeleteResult,
    SessionMessageResponse,
    SessionRenameRequest,
    SessionResponse,
    SessionSendMessageRequest,
    SessionShareRequest,
    SessionShareResponse,
)
from session.service import SessionService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


@router.get("", summary="查询会话列表", response_model=ApiResponse[list[SessionResponse]])
async def list_sessions(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SessionService(db)
    sessions = await service.list_accessible(current_user.id, workspace_id)
    return ok([SessionResponse.model_validate(item) for item in sessions], request)


@router.post("/messages", summary="发送消息")
async def send_message(
    payload: SessionSendMessageRequest,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    # 如果提供了 session_id，需要验证权限
    if payload.session_id:
        session = await SessionService(db).require_write_access(
            payload.session_id,
            current_user.id,
            workspace_id,
        )
        if payload.agent_id is not None and payload.agent_id != session.agent_id:
            raise AgentException.message("已有会话不能切换Agent", status_code=409)

    # 传递用户信息和工作区信息到 runtime
    runtime = AgentRuntime(db, user_id=current_user.id, workspace_id=workspace_id)

    async def event_stream():
        async for event in runtime.run(
            payload.session_id,
            payload.content,
            agent_id=payload.agent_id,
        ):
            yield format_sse(event)

    return StreamingResponse(event_stream(), media_type="text/event-stream; charset=utf-8")


@router.post(
    "/batch-delete",
    summary="批量删除会话",
    response_model=ApiResponse[SessionDeleteResult],
)
async def batch_delete_sessions(
    payload: SessionBatchDeleteRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    deleted = await SessionService(db).delete_many(
        payload.ids,
        current_user.id,
        workspace_id,
    )
    return ok(SessionDeleteResult(deleted=deleted), request)


@router.get(
    "/{session_id}/messages",
    summary="查询会话消息",
    response_model=ApiResponse[list[SessionMessageResponse]],
)
async def list_messages(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = SessionService(db)
    await service.require_read_access(session_id, current_user.id, workspace_id)
    messages = await service.list_messages(session_id)
    return ok([SessionMessageResponse.model_validate(item) for item in messages], request)


@router.post(
    "/{session_id}/archive", summary="归档会话", response_model=ApiResponse[SessionResponse]
)
async def archive_session(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    session = await SessionService(db).archive(session_id, current_user.id, workspace_id)
    return ok(SessionResponse.model_validate(session), request)


@router.patch("/{session_id}", summary="重命名会话", response_model=ApiResponse[SessionResponse])
async def rename_session(
    session_id: int,
    payload: SessionRenameRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    session = await SessionService(db).rename(
        session_id,
        payload.title,
        current_user.id,
        workspace_id,
    )
    return ok(SessionResponse.model_validate(session), request)


@router.delete("/{session_id}", summary="删除会话", response_model=ApiResponse[SessionDeleteResult])
async def delete_session(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    await SessionService(db).delete(session_id, current_user.id, workspace_id)
    return ok(SessionDeleteResult(deleted=1), request)


@router.post(
    "/{session_id}/share",
    summary="覆盖会话共享范围",
    response_model=ApiResponse[SessionShareResponse],
)
async def share_session(
    session_id: int,
    payload: SessionShareRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    session = await SessionService(db).share(
        session_id,
        current_user.id,
        workspace_id,
        visibility=payload.visibility,
        user_ids=payload.share_with.users,
        department_ids=payload.share_with.departments,
        group_ids=payload.share_with.groups,
    )
    return ok(
        SessionShareResponse(
            session_id=session.id,
            visibility=session.visibility,
            users=session.shared_with,
            departments=session.shared_with_departments,
            groups=session.shared_with_groups,
        ),
        request,
    )
