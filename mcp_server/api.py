"""MCP server 管理接口（前缀 /api/mcp）：只做鉴权、Schema 转换与响应包装。

角色、可见性、上限与重名等业务规则在 McpServerService，「校验 → 探测 → 写库 → 失效」的编排在
应用层 McpAdminService，经 api/deps.py 的 get_mcp_admin_service 取得，路由里不 new 适配器。
别人的私有 server 一律按不存在处理（404）。OAuth 的三个接口在 M3、Agent 绑定接口在 M2 由 agent 域提供。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from api.deps import get_mcp_admin_service
from core.responses import ApiResponse, ok
from harness.app.mcp_admin import McpAdminService
from mcp_server.schemas import (
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
