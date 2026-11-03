from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from permission.schemas import (
    PermissionRuleCreateRequest,
    PermissionRuleDeleteResult,
    PermissionRuleResponse,
)
from permission.service import PermissionService
from session.service import SessionService
from tools.policy import get_tool_policy
from user.models import UserRecord
from workspace.service import WorkspaceService

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


def _validate_tool_name(tool_name: str) -> None:
    """权限规则只能引用已声明能力策略的运行时工具。"""
    if get_tool_policy(tool_name).category == "unknown":
        raise AgentException.message(
            "工具未注册，不能配置权限规则",
            {"tool_name": tool_name},
            status_code=400,
        )


async def _require_global_admin(
    db: AsyncSession,
    user_id: int,
    workspace_id: int,
) -> None:
    """全局规则会影响所有会话，只允许当前工作区管理员维护。"""
    await WorkspaceService(db).require_workspace_role(
        workspace_id,
        user_id,
        {"owner", "admin"},
    )


async def _require_session_owner(
    db: AsyncSession,
    session_id: int,
    user_id: int,
    workspace_id: int,
) -> None:
    """会话级规则只允许会话创建者维护，避免共享读取者修改审批策略。"""
    await SessionService(db).require_write_access(session_id, user_id, workspace_id)


async def _authorize_scope(
    db: AsyncSession,
    *,
    scope: str,
    session_id: int | None,
    user_id: int,
    workspace_id: int,
) -> int | None:
    """按作用域校验参数与操作者权限，返回规则应绑定的 session_id（global 为 None）。"""
    if scope == "global":
        if session_id is not None:
            raise AgentException.message("scope=global 时不能提供 session_id")
        await _require_global_admin(db, user_id, workspace_id)
        return None
    if scope == "session":
        if session_id is None:
            raise AgentException.message("scope=session 时必须提供 session_id")
        await _require_session_owner(db, session_id, user_id, workspace_id)
        return session_id
    raise AgentException.message("权限规则作用域无效")


@router.get(
    "",
    summary="查询权限规则",
    response_model=ApiResponse[list[PermissionRuleResponse]],
)
async def list_rules(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    scope: str | None = Query(default=None, description="按作用域过滤: global / session"),
    session_id: int | None = Query(default=None, description="按会话ID过滤"),
):
    # 不允许使用未指定会话的查询列出全部 session 规则，避免跨会话泄漏。
    if scope is None:
        await _require_global_admin(db, current_user.id, workspace_id)
        if session_id is not None:
            raise AgentException.message("未指定 scope 时不能提供 session_id")
    else:
        await _authorize_scope(
            db,
            scope=scope,
            session_id=session_id,
            user_id=current_user.id,
            workspace_id=workspace_id,
        )
    service = PermissionService(db)
    rules = await service.list_rules(
        scope=scope,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    return ok([PermissionRuleResponse.model_validate(rule) for rule in rules], request)


@router.post(
    "",
    summary="新增或更新权限规则",
    response_model=ApiResponse[PermissionRuleResponse],
)
async def upsert_rule(
    payload: PermissionRuleCreateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    _validate_tool_name(payload.tool_name)
    await _authorize_scope(
        db,
        scope=payload.scope,
        session_id=payload.session_id,
        user_id=current_user.id,
        workspace_id=workspace_id,
    )
    service = PermissionService(db)
    rule = await service.upsert_rule(
        scope=payload.scope,
        session_id=payload.session_id,
        tool_name=payload.tool_name,
        behavior=payload.behavior,
        matcher=payload.matcher,
        workspace_id=workspace_id,
    )
    return ok(PermissionRuleResponse.model_validate(rule), request)


@router.delete(
    "/{rule_id}",
    summary="删除权限规则",
    response_model=ApiResponse[PermissionRuleDeleteResult],
)
async def delete_rule(
    rule_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    service = PermissionService(db)
    rule = await service.get_required(rule_id)
    # 其他工作区的规则按不存在处理（404），不暴露规则是否存在；
    # 同工作区内 global 规则要求管理员，session 规则要求会话创建者。
    if rule.scope == "global":
        if rule.workspace_id != workspace_id:
            raise AgentException.message("权限规则不存在", status_code=404)
        await _require_global_admin(db, current_user.id, workspace_id)
    elif rule.scope == "session":
        if rule.session_id is None:
            raise AgentException.message("会话级权限规则缺少会话ID")
        if rule.workspace_id != workspace_id:
            raise AgentException.message("权限规则不存在", status_code=404)
        await _require_session_owner(
            db,
            rule.session_id,
            current_user.id,
            workspace_id,
        )
    else:
        raise AgentException.message("权限规则作用域无效")
    deleted = await service.delete_rule(rule_id)
    return ok(PermissionRuleDeleteResult(deleted=deleted), request)
