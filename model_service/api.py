"""模型服务接口，写操作仅限工作区管理员。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import (
    get_current_active_user,
    get_current_admin_user,
    get_current_workspace_id,
    get_db,
)
from core.responses import ApiResponse, ok
from model_service.schemas import (
    ModelPreferencesRequest,
    ModelProbeRequest,
    ModelProviderInput,
    ModelProviderResponse,
    ModelProviderUpdate,
    ModelRoleRequest,
    ModelRoleResponse,
    ModelTestResponse,
)
from model_service.service import ModelService, provider_schemas
from user.models import UserRecord

router = APIRouter()
Database = Annotated[AsyncSession, Depends(get_db)]
Workspace = Annotated[int, Depends(get_current_workspace_id)]
User = Annotated[UserRecord, Depends(get_current_active_user)]
Admin = Annotated[UserRecord, Depends(get_current_admin_user)]


@router.get("/provider-schemas", response_model=ApiResponse[list[dict[str, Any]]])
async def schemas(request: Request, user: User, workspace_id: Workspace):
    return ok(provider_schemas(), request)


@router.get("/providers", response_model=ApiResponse[list[ModelProviderResponse]])
async def providers(request: Request, db: Database, user: Admin, workspace_id: Workspace):
    return ok(await ModelService(db).list_providers(workspace_id), request)


@router.post("/providers", response_model=ApiResponse[ModelProviderResponse])
async def create_provider(
    payload: ModelProviderInput,
    request: Request,
    db: Database,
    user: Admin,
    workspace_id: Workspace,
):
    return ok(await ModelService(db).save_provider(workspace_id, payload), request)


@router.put("/providers/{provider_id}", response_model=ApiResponse[ModelProviderResponse])
async def update_provider(
    provider_id: str,
    payload: ModelProviderUpdate,
    request: Request,
    db: Database,
    user: Admin,
    workspace_id: Workspace,
):
    return ok(await ModelService(db).save_provider(workspace_id, payload, provider_id), request)


@router.delete("/providers/{provider_id}", response_model=ApiResponse[dict[str, bool]])
async def delete_provider(
    provider_id: str,
    request: Request,
    db: Database,
    user: Admin,
    workspace_id: Workspace,
):
    await ModelService(db).delete_provider(workspace_id, provider_id)
    return ok({"deleted": True}, request)


@router.post("/providers/{provider_id}/test", response_model=ApiResponse[ModelTestResponse])
async def test_provider(
    provider_id: str,
    request: Request,
    db: Database,
    user: Admin,
    workspace_id: Workspace,
):
    return ok(await ModelService(db).test_provider(workspace_id, provider_id), request)


@router.post("/providers/detect-context", response_model=ApiResponse[dict[str, Any]])
async def detect_context(
    payload: ModelProbeRequest,
    request: Request,
    db: Database,
    user: Admin,
    workspace_id: Workspace,
):
    return ok(await ModelService(db).detect_context(workspace_id, payload), request)


@router.post("/providers/detect-reasoning", response_model=ApiResponse[dict[str, Any]])
async def detect_reasoning(
    payload: ModelProbeRequest,
    request: Request,
    db: Database,
    user: Admin,
    workspace_id: Workspace,
):
    return ok(await ModelService(db).detect_reasoning(workspace_id, payload), request)


@router.get("/roles", response_model=ApiResponse[list[ModelRoleResponse]])
async def roles(request: Request, db: Database, user: Admin, workspace_id: Workspace):
    return ok(await ModelService(db).list_roles(workspace_id), request)


@router.put("/roles/{role_key}", response_model=ApiResponse[dict[str, bool]])
async def assign_role(
    role_key: str,
    payload: ModelRoleRequest,
    request: Request,
    db: Database,
    user: Admin,
    workspace_id: Workspace,
):
    await ModelService(db).assign_role(workspace_id, role_key, payload.provider_id)
    return ok({"assigned": True}, request)


@router.delete("/roles/{role_key}", response_model=ApiResponse[dict[str, bool]])
async def unassign_role(
    role_key: str,
    request: Request,
    db: Database,
    user: Admin,
    workspace_id: Workspace,
):
    await ModelService(db).assign_role(workspace_id, role_key, None)
    return ok({"assigned": False}, request)


@router.get("/preferences", response_model=ApiResponse[ModelPreferencesRequest])
async def preferences(request: Request, db: Database, user: User, workspace_id: Workspace):
    return ok(await ModelService(db).preferences(workspace_id, user.id), request)


@router.put("/preferences", response_model=ApiResponse[ModelPreferencesRequest])
async def update_preferences(
    payload: ModelPreferencesRequest,
    request: Request,
    db: Database,
    user: User,
    workspace_id: Workspace,
):
    preferences = await ModelService(db).preferences(
        workspace_id,
        user.id,
        payload.default_model_provider_id,
        update=True,
    )
    return ok(preferences, request)


@router.get("/capabilities", response_model=ApiResponse[dict[str, Any]])
async def capabilities(request: Request, db: Database, user: User, workspace_id: Workspace):
    return ok(await ModelService(db).capabilities(workspace_id, user.id), request)
