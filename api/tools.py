from anthropic.types import ToolParam
from fastapi import APIRouter, Request

from core.responses import ApiResponse, ok
from tools.registry import build_tool_registry

router = APIRouter()


@router.get("", summary="查询可用工具", response_model=ApiResponse[list[ToolParam]])
def list_tools(request: Request):
    return ok(build_tool_registry().to_anthropic_tools(), request)
