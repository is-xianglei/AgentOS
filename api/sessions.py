from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ok
from models.user import UserRecord
from schemas.common import ApiResponse
from schemas.session import (
    SessionApprovalRequest,
    SessionBatchDeleteRequest,
    SessionDeleteResult,
    SessionMessageResponse,
    SessionRenameRequest,
    SessionResponse,
    SessionSendMessageRequest,
)
from services.agent_runtime import AgentRuntime, format_sse
from services.session_service import SessionService

router = APIRouter()


@router.get("", summary="查询会话列表", response_model=ApiResponse[list[SessionResponse]])
async def list_sessions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
):
    service = SessionService(db)
    # 只返回当前用户的会话
    sessions = await service.list_by_user(current_user.id)
    return ok([SessionResponse.model_validate(item) for item in sessions], request)


@router.post("/messages", summary="发送消息")
async def send_message(
    payload: SessionSendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
    workspace_id: int | None = Depends(get_current_workspace_id),
):
    # 如果提供了 session_id，需要验证权限
    if payload.session_id:
        service = SessionService(db)
        session = await service.get_required(payload.session_id)
        if not await service.check_access(session, current_user.id):
            raise AgentException.message("无权限访问该会话")

    # 传递用户信息和工作区信息到 runtime
    runtime = AgentRuntime(db, user_id=current_user.id, workspace_id=workspace_id)

    async def event_stream():
        async for event in runtime.run(payload.session_id, payload.content):
            yield format_sse(event)

    return StreamingResponse(event_stream(), media_type="text/event-stream; charset=utf-8")


@router.post("/{session_id}/approvals", summary="审批工具调用并恢复执行")
async def respond_approval(
    session_id: int,
    payload: SessionApprovalRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
    workspace_id: int | None = Depends(get_current_workspace_id),
):
    # 验证会话权限
    service = SessionService(db)
    session = await service.get_required(session_id)
    if not await service.check_access(session, current_user.id):
        raise AgentException.message("无权限访问该会话")

    runtime = AgentRuntime(db, user_id=current_user.id, workspace_id=workspace_id)

    async def event_stream():
        async for event in runtime.resume(
            session_id,
            payload.request_id,
            payload.decision,
            payload.updated_input,
            payload.always_scope,
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
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
):
    service = SessionService(db)
    # 验证每个会话的权限
    for session_id in payload.ids:
        session = await service.get_required(session_id)
        if not await service.check_access(session, current_user.id):
            raise AgentException.message(f"无权限删除会话 {session_id}")

    deleted = await service.delete_many(payload.ids)
    return ok(SessionDeleteResult(deleted=deleted), request)


@router.get(
    "/{session_id}/messages",
    summary="查询会话消息",
    response_model=ApiResponse[list[SessionMessageResponse]],
)
async def list_messages(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
):
    service = SessionService(db)
    # 验证会话权限
    session = await service.get_required(session_id)
    if not await service.check_access(session, current_user.id):
        raise AgentException.message("无权限访问该会话")

    messages = await service.list_messages(session_id)
    return ok([SessionMessageResponse.model_validate(item) for item in messages], request)


@router.post("/{session_id}/archive", summary="归档会话", response_model=ApiResponse[SessionResponse])
async def archive_session(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
):
    service = SessionService(db)
    # 验证会话权限
    session = await service.get_required(session_id)
    if not await service.check_access(session, current_user.id):
        raise AgentException.message("无权限归档该会话")

    return ok(SessionResponse.model_validate(await service.archive(session_id)), request)


@router.patch("/{session_id}", summary="重命名会话", response_model=ApiResponse[SessionResponse])
async def rename_session(
    session_id: int,
    payload: SessionRenameRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
):
    service = SessionService(db)
    # 验证会话权限
    session = await service.get_required(session_id)
    if not await service.check_access(session, current_user.id):
        raise AgentException.message("无权限修改该会话")

    session = await service.rename(session_id, payload.title)
    return ok(SessionResponse.model_validate(session), request)


@router.delete("/{session_id}", summary="删除会话", response_model=ApiResponse[SessionDeleteResult])
async def delete_session(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
):
    service = SessionService(db)
    # 验证会话权限
    session = await service.get_required(session_id)
    if not await service.check_access(session, current_user.id):
        raise AgentException.message("无权限删除该会话")

    await service.delete(session_id)
    return ok(SessionDeleteResult(deleted=1), request)

