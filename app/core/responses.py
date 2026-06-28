from typing import Any

from fastapi import Request


def ok(data: Any, request: Request) -> dict[str, Any]:
    return {
        "data": data,
        "error": None,
        "request_id": getattr(request.state, "request_id", None),
    }
