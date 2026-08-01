from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from department.schemas import (
    DepartmentAssignmentResponse,
    DepartmentCreateRequest,
    DepartmentDeleteResult,
    DepartmentMemberResponse,
    DepartmentResponse,
    DepartmentSetMemberRequest,
    DepartmentTreeResponse,
    DepartmentUpdateRequest,
)
from department.service import DepartmentService, DepartmentTreeNode, DepartmentUpdateFields
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


def _require_current_workspace(workspace_id: int, current_workspace_id: int) -> None:
    if workspace_id != current_workspace_id:
        raise AgentException.message("路径工作区与当前工作区不一致", status_code=403)


def _tree_response(node: DepartmentTreeNode) -> DepartmentTreeResponse:
    department = DepartmentResponse.model_validate(node.department)
    return DepartmentTreeResponse(
        **department.model_dump(),
        children=[_tree_response(child) for child in node.children],
    )


@router.post(
    "/{workspace_id}/departments",
    summary="创建部门",
    response_model=ApiResponse[DepartmentResponse],
)
async def create_department(
    workspace_id: int,
    payload: DepartmentCreateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    department = await DepartmentService(db).create_department(
        workspace_id=workspace_id,
        actor_user_id=current_user.id,
        name=payload.name,
        parent_id=payload.parent_id,
        code=payload.code,
        description=payload.description,
        manager_id=payload.manager_id,
        sort_order=payload.sort_order,
        is_active=payload.is_active,
    )
    return ok(DepartmentResponse.model_validate(department), request)


@router.get(
    "/{workspace_id}/departments",
    summary="查询部门列表",
    response_model=ApiResponse[list[DepartmentResponse]],
)
async def list_departments(
    workspace_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    departments = await DepartmentService(db).list_departments(workspace_id, current_user.id)
    return ok([DepartmentResponse.model_validate(item) for item in departments], request)


@router.get(
    "/{workspace_id}/departments/tree",
    summary="查询部门树",
    response_model=ApiResponse[list[DepartmentTreeResponse]],
)
async def get_department_tree(
    workspace_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    tree = await DepartmentService(db).get_department_tree(workspace_id, current_user.id)
    return ok([_tree_response(node) for node in tree], request)


@router.get(
    "/{workspace_id}/departments/{department_id}",
    summary="查询部门详情",
    response_model=ApiResponse[DepartmentResponse],
)
async def get_department(
    workspace_id: int,
    department_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    department = await DepartmentService(db).get_department(
        workspace_id,
        department_id,
        current_user.id,
    )
    return ok(DepartmentResponse.model_validate(department), request)


@router.patch(
    "/{workspace_id}/departments/{department_id}",
    summary="更新部门",
    response_model=ApiResponse[DepartmentResponse],
)
async def update_department(
    workspace_id: int,
    department_id: int,
    payload: DepartmentUpdateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    updates = cast(
        DepartmentUpdateFields,
        payload.model_dump(exclude_unset=True),
    )
    department = await DepartmentService(db).update_department(
        workspace_id,
        department_id,
        current_user.id,
        updates,
    )
    return ok(DepartmentResponse.model_validate(department), request)


@router.delete(
    "/{workspace_id}/departments/{department_id}",
    summary="删除部门",
    response_model=ApiResponse[DepartmentDeleteResult],
)
async def delete_department(
    workspace_id: int,
    department_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    await DepartmentService(db).delete_department(
        workspace_id,
        department_id,
        current_user.id,
    )
    return ok(DepartmentDeleteResult(deleted=True), request)


@router.get(
    "/{workspace_id}/departments/{department_id}/ancestors",
    summary="查询祖先部门",
    response_model=ApiResponse[list[DepartmentResponse]],
)
async def list_department_ancestors(
    workspace_id: int,
    department_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    departments = await DepartmentService(db).list_ancestors(
        workspace_id,
        department_id,
        current_user.id,
    )
    return ok([DepartmentResponse.model_validate(item) for item in departments], request)


@router.get(
    "/{workspace_id}/departments/{department_id}/descendants",
    summary="查询子孙部门",
    response_model=ApiResponse[list[DepartmentResponse]],
)
async def list_department_descendants(
    workspace_id: int,
    department_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    departments = await DepartmentService(db).list_descendants(
        workspace_id,
        department_id,
        current_user.id,
    )
    return ok([DepartmentResponse.model_validate(item) for item in departments], request)


@router.get(
    "/{workspace_id}/departments/{department_id}/members",
    summary="查询部门成员",
    response_model=ApiResponse[list[DepartmentMemberResponse]],
)
async def list_department_members(
    workspace_id: int,
    department_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
    include_descendants: bool = Query(default=False, description="是否包含子孙部门成员"),
):
    _require_current_workspace(workspace_id, current_workspace_id)
    members = await DepartmentService(db).list_members(
        workspace_id,
        department_id,
        current_user.id,
        include_descendants=include_descendants,
    )
    response = [
        DepartmentMemberResponse(
            id=member.id,
            workspace_id=member.workspace_id,
            user_id=member.user_id,
            username=str(user["username"]),
            email=str(user["email"]),
            full_name=user["full_name"] if isinstance(user["full_name"], str) else None,
            avatar_url=user["avatar_url"] if isinstance(user["avatar_url"], str) else None,
            role=member.role,
            department_id=member.department_id,
            job_title=member.job_title,
            joined_at=member.joined_at,
        )
        for member, user in members
    ]
    return ok(response, request)


@router.post(
    "/{workspace_id}/members/{user_id}/department",
    summary="设置成员部门",
    response_model=ApiResponse[DepartmentAssignmentResponse],
)
async def set_member_department(
    workspace_id: int,
    user_id: int,
    payload: DepartmentSetMemberRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_current_workspace(workspace_id, current_workspace_id)
    member = await DepartmentService(db).set_member_department(
        workspace_id=workspace_id,
        user_id=user_id,
        actor_user_id=current_user.id,
        department_id=payload.department_id,
        job_title=payload.job_title,
    )
    return ok(DepartmentAssignmentResponse.model_validate(member), request)
