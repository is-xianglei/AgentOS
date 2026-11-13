"""MCP server 管理接口（前缀 /api/mcp）：只做鉴权、Schema 转换与响应包装。

角色、可见性、上限与重名等业务规则在 McpServerService，「校验 → 探测 → 写库 → 失效」的编排在
应用层 McpAdminService，经 api/deps.py 的 get_mcp_admin_service 取得，路由里不 new 适配器。
别人的私有 server 一律按不存在处理（404）。Agent 绑定接口由 agent 域提供。

OAuth：授权回跳到前端路由 {AGENTOS_PUBLIC_BASE_URL}/mcp/oauth/callback，前端带 Bearer 调完成接口；
CIMD 文档接口不鉴权（授权服务器来取），内容只有公开的客户端元数据。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.deps import get_mcp_admin_service
from core.config import settings
from core.errors import AgentException
from core.responses import ApiResponse, ok
from harness.app.mcp_admin import McpAdminService
from harness.domain.mcp_oauth import oauth_client_metadata_document
from mcp_server.schemas import (
    McpOAuthAuthorizeResponse,
    McpOAuthCompleteRequest,
    McpOAuthRevokeResponse,
    McpProbeResponse,
    McpServerCreateRequest,
    McpServerCreateResponse,
    McpServerResponse,
    McpServerUpdateRequest,
    McpToolDiffResponse,
    McpToolResponse,
    McpToolSummaryResponse,
)

router = APIRouter()
AdminService = Annotated[McpAdminService, Depends(get_mcp_admin_service)]


@router.get("/servers", response_model=ApiResponse[list[McpServerResponse]])
async def list_servers(request: Request, service: AdminService):
    servers = await service.list_servers()
    return ok([McpServerResponse.from_view(server) for server in servers], request)


@router.post("/servers", response_model=ApiResponse[McpServerCreateResponse])
async def create_server(payload: McpServerCreateRequest, request: Request, service: AdminService):
    server = await service.create_server(payload.to_draft())
    response = McpServerCreateResponse(
        server=McpServerResponse.from_view(server),
        tools=[McpToolSummaryResponse.from_summary(tool) for tool in server.tools],
    )
    return ok(response, request)


@router.get("/servers/{server_id}", response_model=ApiResponse[McpServerResponse])
async def get_server(server_id: int, request: Request, service: AdminService):
    return ok(McpServerResponse.from_view(await service.get_server(server_id)), request)


@router.put("/servers/{server_id}", response_model=ApiResponse[McpServerResponse])
async def update_server(
    server_id: int,
    payload: McpServerUpdateRequest,
    request: Request,
    service: AdminService,
):
    server = await service.update_server(server_id, payload.to_changes())
    return ok(McpServerResponse.from_view(server), request)


@router.delete("/servers/{server_id}", response_model=ApiResponse[dict[str, bool]])
async def delete_server(server_id: int, request: Request, service: AdminService):
    await service.delete_server(server_id)
    return ok({"deleted": True}, request)


@router.post("/servers/{server_id}/probe", response_model=ApiResponse[McpProbeResponse])
async def probe_server(server_id: int, request: Request, service: AdminService):
    result = await service.refresh_tools(server_id)
    response = McpProbeResponse(
        ok=result.error is None,
        error=result.error,
        server=McpServerResponse.from_view(result.server),
        diff=McpToolDiffResponse.from_diff(result.diff),
    )
    return ok(response, request)


@router.get("/servers/{server_id}/tools", response_model=ApiResponse[list[McpToolResponse]])
async def list_tools(server_id: int, request: Request, service: AdminService):
    listings = await service.list_tools(server_id)
    return ok([McpToolResponse.from_listing(listing) for listing in listings], request)


# ---------- OAuth ----------


@router.post(
    "/servers/{server_id}/oauth/authorize",
    response_model=ApiResponse[McpOAuthAuthorizeResponse],
)
async def authorize_oauth(server_id: int, request: Request, service: AdminService):
    """授权前半程：返回授权地址；私有 server 只有 owner 本人能授权，别人 404。"""
    url = await service.begin_oauth(server_id)
    return ok(McpOAuthAuthorizeResponse(authorization_url=url), request)


@router.post("/oauth/complete", response_model=ApiResponse[McpServerResponse])
async def complete_oauth(
    payload: McpOAuthCompleteRequest,
    request: Request,
    service: AdminService,
):
    """授权后半程：state 只能用一次，且必须是当前登录用户发起的；成功返回带授权状态的 server。"""
    server = await service.complete_oauth(payload.to_callback())
    return ok(McpServerResponse.from_view(server), request)


@router.delete("/servers/{server_id}/oauth", response_model=ApiResponse[McpOAuthRevokeResponse])
async def revoke_oauth(server_id: int, request: Request, service: AdminService):
    """撤销自己的授权（软删自己的 token），之后可以重新授权。"""
    revoked = await service.revoke_oauth(server_id)
    return ok(McpOAuthRevokeResponse(revoked=revoked), request)


@router.get("/oauth/client-metadata.json", include_in_schema=False)
async def oauth_client_metadata() -> JSONResponse:
    """CIMD（OAuth Client ID Metadata Document）：授权服务器按 client_id 地址来取，不带 Bearer。

    内容只有公开的客户端元数据（名称、回跳地址），不含任何凭证；未配置对外地址时 404。
    """
    if not settings.public_base_url:
        raise AgentException.message("未配置 AGENTOS_PUBLIC_BASE_URL", status_code=404)
    return JSONResponse(oauth_client_metadata_document(settings.public_base_url))
