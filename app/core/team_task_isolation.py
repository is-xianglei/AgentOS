from contextvars import ContextVar
from uuid import uuid4

from app.core import config

_PROCESS_INSTANCE_ID = f"process-{uuid4().hex}"
_TRUE_VALUES = {"1", "true", "yes", "on"}

# 每个子代理协程独立的隔离 id。asyncio.create_task/gather 会自动拷贝创建时刻的
# context,因此在协程入口 set 的值不会回写父 context,天然实现协程级隔离。
_instance_id_var: ContextVar[str | None] = ContextVar("team_task_instance_id", default=None)


def team_task_debug_isolation_enabled() -> bool:
    value = config.get("AGENTOS_TEAM_TASK_DEBUG_ISOLATION", "") or ""
    return value.strip().lower() in _TRUE_VALUES


def set_team_task_instance_id(instance_id: str):
    """在当前协程上下文设置隔离 id,返回 Token 供 reset。"""
    return _instance_id_var.set(instance_id)


def reset_team_task_instance_id(token) -> None:
    """还原隔离 id 上下文。"""
    _instance_id_var.reset(token)


def new_team_task_instance_id() -> str:
    """生成一个唯一的子代理运行隔离 id。"""
    return f"run-{uuid4().hex}"


def current_team_task_instance_id() -> str:
    # 优先取协程上下文设置的值,其次环境变量,最后进程级常量。
    ctx_value = _instance_id_var.get()
    if ctx_value:
        return ctx_value
    return (config.get("AGENTOS_INSTANCE_ID", "") or "").strip() or _PROCESS_INSTANCE_ID


def outgoing_team_task_consumer_instance_id(message_type: str) -> str | None:
    if message_type != "task" or not team_task_debug_isolation_enabled():
        return None
    return current_team_task_instance_id()
