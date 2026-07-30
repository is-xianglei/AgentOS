from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from core.responses import ApiResponse, ok
from permission.schemas import (
    PermissionRuleCreateRequest,
    PermissionRuleDeleteResult,
    PermissionRuleResponse,
)
from permission.service import PermissionService

router = APIRouter()


@router.get(
    "",
    summary="查询权限规则",
    response_model=ApiResponse[list[PermissionRuleResponse]],
)
async def list_rules(
    request: Request,
    scope: str | None = Query(default=None, description="按作用域过滤: global / session"),
    session_id: int | None = Query(default=None, description="按会话ID过滤"),
    db: AsyncSession = Depends(get_db),
):
    service = PermissionService(db)
    rules = await service.list_rules(scope=scope, session_id=session_id)
    return ok([PermissionRuleResponse.model_validate(r) for r in rules], request)


@router.post(
    "",
    summary="新增或更新权限规则",
    response_model=ApiResponse[PermissionRuleResponse],
)
async def upsert_rule(
    payload: PermissionRuleCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    service = PermissionService(db)
    rule = await service.upsert_rule(
        scope=payload.scope,
        session_id=payload.session_id,
        tool_name=payload.tool_name,
        behavior=payload.behavior,
        matcher=payload.matcher,
    )
    return ok(PermissionRuleResponse.model_validate(rule), request)


@router.delete(
    "/{rule_id}",
    summary="删除权限规则",
    response_model=ApiResponse[PermissionRuleDeleteResult],
)
async def delete_rule(rule_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = PermissionService(db)
    deleted = await service.delete_rule(rule_id)
    return ok(PermissionRuleDeleteResult(deleted=deleted), request)
