from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.responses import ok
from app.schemas.common import ApiResponse
from app.schemas.session import (
    SessionMessageResponse,
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

