from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.responses import ok
from app.schemas.common import ApiResponse
from app.schemas.session import (
    SessionApprovalRequest,
    SessionBatchDeleteRequest,
    SessionDeleteResult,
    SessionMessageResponse,
    SessionRenameRequest,
    SessionResponse,
    SessionSendMessageRequest,
)
from app.services.agent_runtime import AgentRuntime, format_sse
from app.services.session_service import SessionService

router = APIRouter()


@router.get("", summary="查询会话列表", response_model=ApiResponse[list[SessionResponse]])
async def list_sessions(request: Request, db: AsyncSession = Depends(get_db)):
    service = SessionService(db)
    return ok([SessionResponse.model_validate(item) for item in await service.list()], request)


@router.post("/messages", summary="发送消息")
async def send_message(payload: SessionSendMessageRequest,db: AsyncSession = Depends(get_db)):
    runtime = AgentRuntime(db)

    async def event_stream():
        async for event in runtime.run(payload.session_id, payload.content):
            yield format_sse(event)

    return StreamingResponse(event_stream(), media_type="text/event-stream; charset=utf-8")


@router.post("/{session_id}/approvals", summary="审批工具调用并恢复执行")
async def respond_approval(
    session_id: int,
    payload: SessionApprovalRequest,
    db: AsyncSession = Depends(get_db),
):

    runtime = AgentRuntime(db)

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
    payload: SessionBatchDeleteRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    service = SessionService(db)
    deleted = await service.delete_many(payload.ids)
    return ok(SessionDeleteResult(deleted=deleted), request)


@router.get(
    "/{session_id}/messages",
    summary="查询会话消息",
    response_model=ApiResponse[list[SessionMessageResponse]],
)
async def list_messages(session_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = SessionService(db)
    messages = await service.list_messages(session_id)
    return ok([SessionMessageResponse.model_validate(item) for item in messages], request)


@router.post("/{session_id}/archive", summary="归档会话", response_model=ApiResponse[SessionResponse])
async def archive_session(session_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = SessionService(db)
    return ok(SessionResponse.model_validate(await service.archive(session_id)), request)


@router.patch("/{session_id}", summary="重命名会话", response_model=ApiResponse[SessionResponse])
async def rename_session(
    session_id: int,
    payload: SessionRenameRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    service = SessionService(db)
    session = await service.rename(session_id, payload.title)
    return ok(SessionResponse.model_validate(session), request)


@router.delete("/{session_id}", summary="删除会话", response_model=ApiResponse[SessionDeleteResult])
async def delete_session(session_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = SessionService(db)
    await service.delete(session_id)
    return ok(SessionDeleteResult(deleted=1), request)

