"""对话模式与思考档位的统一策略。"""

from typing import Any

from core.errors import AgentException

REASONING_KEYS = ("low", "medium", "high", "xhigh", "max")
LEGACY_LEVELS = [{"key": key, "value": key} for key in ("medium", "high", "max")]
MODES = [
    {
        "slug": "standard",
        "name": "标准模式",
        "description": "完整工具能力，按需思考",
        "icon": "message-circle",
        "sort_order": 0,
        "thinking_locked": False,
        "default_thinking_effort": "fast",
    },
    {
        "slug": "turbo",
        "name": "极速模式",
        "description": "快速直接回答，不执行代码或写入文件",
        "icon": "zap",
        "sort_order": 1,
        "thinking_locked": True,
        "default_thinking_effort": "fast",
    },
]
TURBO_MAX_ITERATIONS = 4
# 当前目录没有真实检索工具，Weather 为占位实现；极速仅回答已有上下文。
TURBO_ALLOWED_TOOLS: tuple[str, ...] = ()
TURBO_PROMPT = """你是极速查询助手。优先在约30秒内给出直接可用的准确答案，默认约300字。
最多进行1至2轮必要查询；已有信息足够时立即回答。没有可靠结果时如实说明，禁止编造来源。
当前为极速模式，没有代码执行、文件读写、计划或子智能体工具。不得声称执行了代码、生成了文件或进行了不存在的网络检索。
遇到文档、表格或代码请求，完整提供可复制的正文、Markdown、CSV或代码；需要执行、封装文件时建议切换标准模式。"""


def validate_reasoning_config(extra: dict[str, Any], provider_type: str) -> None:
    """严格保留上游映射类型，避免把数值预算改成字符串。"""
    if "fast_reasoning_effort" in extra and (
        provider_type != "chat" or extra["fast_reasoning_effort"] != "none"
    ):
        raise AgentException.message("快速参数仅支持对话模型的 none 显式映射", status_code=400)
    enabled = extra.get("supports_reasoning_effort", False)
    levels = extra.get("reasoning_effort_levels")
    default = extra.get("default_reasoning_effort")
    if type(enabled) is not bool:
        raise AgentException.message("多档思考开关必须为布尔值", status_code=400)
    if not enabled:
        if levels is not None or default is not None:
            raise AgentException.message("请先开启多档思考强度再配置档位", status_code=400)
        return
    if provider_type != "chat":
        raise AgentException.message("只有对话模型能配置思考强度", status_code=400)
    if "reasoning_effort_levels" not in extra:
        if default is not None and default not in {row["key"] for row in LEGACY_LEVELS}:
            raise AgentException.message("默认思考档位不在兼容档位中", status_code=400)
        return
    if not isinstance(levels, list) or not 1 <= len(levels) <= 5:
        raise AgentException.message("思考档位须为1至5档", status_code=400)
    keys: list[str] = []
    for level in levels:
        if not isinstance(level, dict):
            raise AgentException.message("思考档位格式不正确", status_code=400)
        key, value = level.get("key"), level.get("value")
        valid_value = (type(value) is int and value > 0) or (
            isinstance(value, str)
            and bool(value)
            and value == value.strip()
            and len(value) <= 64
        )
        if key not in REASONING_KEYS or key in keys or not valid_value:
            raise AgentException.message("思考档位或上游映射值无效", status_code=400)
        keys.append(key)
    if default not in keys:
        raise AgentException.message("默认思考档位必须在已配置档位中", status_code=400)


def fast_capabilities(provider: str, extra: dict[str, Any]) -> dict[str, Any]:
    """原生 reasoning_effort 未声明关闭映射时，不承诺极速模式可用。"""
    protocol = extra.get("thinking_protocol") or (
        "deepseek"
        if provider == "deepseek"
        else "openai"
        if provider == "openai"
        else "chat_template"
    )
    disabled = provider == "anthropic" or protocol != "openai" or (
        extra.get("fast_reasoning_effort") == "none"
    )
    return {
        "supports_fast": disabled,
        "turbo_supported": disabled,
        "fast_behavior": "disabled" if disabled else "model_default",
    }


def reasoning_capabilities(extra: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(extra.get("supports_reasoning_effort"))
    levels = extra.get("reasoning_effort_levels", LEGACY_LEVELS) if enabled else []
    default = extra.get("default_reasoning_effort", "medium")
    if levels and default not in {item["key"] for item in levels}:
        default = levels[0]["key"]
    return {
        "supports_reasoning_effort": enabled,
        "reasoning_effort_levels": levels,
        "default_reasoning_effort": default,
        "reasoning_effort_configured": "reasoning_effort_levels" in extra,
    }


def normalize_effort(extra: dict[str, Any], effort: str, mode_slug: str = "standard") -> str:
    if mode_slug == "turbo" or effort == "fast":
        return "fast"
    policy = reasoning_capabilities(extra)
    if not policy["supports_reasoning_effort"]:
        return "medium"
    keys = {level["key"] for level in policy["reasoning_effort_levels"]}
    return effort if effort in keys else policy["default_reasoning_effort"]
