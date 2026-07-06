from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import NotFoundError
from app.core.responses import ok
from app.repositories.permission_repo import PermissionRepository
from app.schemas.common import ApiResponse
from app.schemas.permission import (
    PermissionRuleCreateRequest,
    PermissionRuleDeleteResult,
    PermissionRuleResponse,
)

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
    rules = await PermissionRepository(db).list(scope=scope, session_id=session_id)
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
    repo = PermissionRepository(db)
    rule = await repo.upsert(
        scope=payload.scope,
        session_id=payload.session_id,
        tool_name=payload.tool_name,
        behavior=payload.behavior,
        source="user",
        matcher=payload.matcher,
    )
    await db.commit()
    return ok(PermissionRuleResponse.model_validate(rule), request)


@router.delete(
    "/{rule_id}",
    summary="删除权限规则",
    response_model=ApiResponse[PermissionRuleDeleteResult],
)
async def delete_rule(rule_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    repo = PermissionRepository(db)
    deleted = await repo.delete(rule_id)
    if deleted == 0:
        raise NotFoundError("PERMISSION_RULE_NOT_FOUND", "权限规则不存在")
    await db.commit()
    return ok(PermissionRuleDeleteResult(deleted=deleted), request)
