import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from runtime.agent import AgentRuntime, format_sse
from runtime.workflow import WorkflowRuntime
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
from workflow.service import WorkflowService

logger = logging.getLogger(__name__)

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
    # 链路起点日志：只记提问字符数，正文可能含敏感内容，严禁入日志。
    logger.info(
        "收到用户提问",
        extra={
            "session_id": payload.session_id,
            "agent_id": payload.agent_id,
            "workflow_id": payload.workflow_id,
            "prompt_chars": len(payload.content),
        },
    )
    if payload.agent_id is not None and payload.workflow_id is not None:
        raise AgentException.message("同一次请求不能同时指定Agent和Workflow")
    # 如果提供了 session_id，需要验证权限
    session = None
    if payload.session_id:
        session = await SessionService(db).require_write_access(
            payload.session_id,
            current_user.id,
            workspace_id,
        )
        if payload.agent_id is not None and payload.agent_id != session.agent_id:
            raise AgentException.message("已有会话不能切换Agent", status_code=409)
        # 与 Agent 的规则同构；会话已锁定 Agent 时再带 workflow_id 同样视为切换。
        if payload.workflow_id is not None and payload.workflow_id != session.workflow_id:
            raise AgentException.message("已有会话不能切换Workflow", status_code=409)
    elif payload.workflow_id is not None:
        # 新建会话时前置校验 Workflow 可运行：让 404/409 走普通 JSON 错误响应，而不是进入
        # 流式生产者后只留日志、返回一条空 SSE 流。Service 内的复检会再读一次，属有意重复。
        await WorkflowService(db).require_runnable(payload.workflow_id, workspace_id)

    # 分发判据是「请求体的 workflow_id 或会话已锁定的 workflow_id」：会话锁定后前端不再带
    # workflow_id，只看请求体会让第二轮静默退回普通 Agent 路径且不报错。
    effective_workflow_id = SessionService.resolve_effective_workflow_id(
        payload.workflow_id,
        session,
    )

    # 运行时在进入流式响应前构造：LLM 客户端配置缺失等错误仍走普通 JSON 错误响应。
    if effective_workflow_id is not None:
        workflow_runtime = WorkflowRuntime(db, user_id=current_user.id, workspace_id=workspace_id)
        events = workflow_runtime.run(
            payload.session_id,
            payload.content,
            workflow_id=effective_workflow_id,
        )
    else:
        # 传递用户信息和工作区信息到 runtime
        runtime = AgentRuntime(db, user_id=current_user.id, workspace_id=workspace_id)
        events = runtime.run(
            payload.session_id,
            payload.content,
            agent_id=payload.agent_id,
        )

    async def event_stream():
        async for event in events:
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
