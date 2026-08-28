import logging
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import Request, Response

from core.config import settings
from core.errors import AgentException

# 合法级别白名单:作为配置契约显式列出,不接受 WARN/FATAL 别名与 NOTSET。
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# LogRecord 内置属性集:探测法生成,自动覆盖 Python 版本差异(如 3.12+ 新增的 taskName);
# asctime/message 是 format() 过程中动态挂上的属性,需显式补入排除集。
_BUILTIN_FIELDS = set(
    logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__
) | {"asctime", "message"}

# 这些第三方 SDK 在 DEBUG 档会把完整请求体(system prompt 与对话正文)打进日志,
# 违反「日志不记录 Prompt 正文」的规范,故其级别设 INFO 下限,不随根级别下探。
# httpx2/httpcore2 是本项目实际使用的分发名,httpx/httpcore 一并覆盖以防依赖切换。
_THIRD_PARTY_FLOOR_LOGGERS = (
    "openai",
    "anthropic",
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
)


class _KeyValueFormatter(logging.Formatter):
    """把 extra 传入的结构化字段以 key=value 追加到消息行尾。

    只覆盖 formatMessage() 而非 format():format() 先生成消息行再拼 traceback,
    在其中追加字段会把 key=value 拼到堆栈末行,破坏异常日志形态。
    """

    def formatMessage(self, record: logging.LogRecord) -> str:
        message = super().formatMessage(record)
        pairs = " ".join(
            f"{key}={value}"
            for key, value in record.__dict__.items()
            if key not in _BUILTIN_FIELDS
        )
        return f"{message} {pairs}" if pairs else message


def setup_logging() -> None:
    """装配根日志:级别读 AGENTOS_LOG_LEVEL,extra 字段经 KV Formatter 渲染可见。

    先清空根 handler 再挂显式 StreamHandler,避免 uvicorn 预装 handler 重复输出;
    Worker 进程复用本函数,与主进程日志格式保持一致。级别非法时抛异常,不静默回落。
    """
    level_name = settings.log_level.strip().upper()
    if level_name not in _VALID_LOG_LEVELS:
        raise AgentException.message(
            "日志级别配置非法,AGENTOS_LOG_LEVEL 仅支持 DEBUG/INFO/WARNING/ERROR/CRITICAL",
            {"log_level": settings.log_level},
        )
    handler = logging.StreamHandler()
    handler.setFormatter(_KeyValueFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(level_name)
    # 取根级别与 INFO 的较高者:根降到 DEBUG 时第三方仍停在 INFO,
    # 根高于 INFO 时同步抬高,避免把它们变得比根更吵。
    floor = max(logging.getLevelNamesMapping()[level_name], logging.INFO)
    for name in _THIRD_PARTY_FLOOR_LOGGERS:
        logging.getLogger(name).setLevel(floor)


async def request_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = str(uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logging.getLogger("agentos.request").info(
            "请求处理完成",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
    response.headers["X-Request-ID"] = request_id
    return response
