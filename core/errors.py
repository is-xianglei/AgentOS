"""业务异常与错误响应外壳:所有错误响应统一为 {data, error, request_id}。

本模块被领域层、应用层广泛导入，加载期不依赖 FastAPI；HTTP 异常处理器在 core/error_handlers.py。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 仅作类型标注：加载期不导入 FastAPI。
    from fastapi import Request


class AgentException(Exception):
    """业务异常，携带可读消息、可选详情和 HTTP 状态码。"""

    status_code = 500

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        # 实例属性 message 会遮蔽同名类方法:实例上取到的是消息文本,类上仍是构造入口。
        self.message = message
        self.details = details or {}
        self.status_code = status_code or type(self).status_code
        super().__init__(message)

    @classmethod
    def message(
        cls,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        status_code: int | None = None,
    ) -> AgentException:
        """业务异常唯一入口。"""
        return cls(message, details, status_code)


class LeaseLostError(AgentException):
    """执行租约已不属于当前持有者(被用户取消、被回收或续约失败)。

    持有者据此停止写入并静默收尾:终态与 run_end 由清除租约的一方负责。
    """

    status_code = 409


def error_payload(
    request: Request,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造错误响应外壳;request_id 由请求上下文中间件写入 request.state。"""
    return {
        "data": None,
        "error": {
            "message": message,
            "details": details or {},
        },
        "request_id": getattr(request.state, "request_id", None),
    }

