"""按工作区模型配置（ModelProviderRecord）构造模型客户端与解析结果。

档位收敛（normalize_effort）在这里对每个 Effort 预先算好，写成客户端的档位映射表：
收敛规则属于模型服务域（model_service.policy），与端点配置一起在构造时一次落定，
客户端每次调用只查表，看不到 extra_config，也就无从绕过收敛。
"""

from typing import Any

from harness.adapters.models.anthropic_client import AnthropicClient
from harness.adapters.models.openai_client import OpenAIClient
from harness.adapters.models.reasoning import ReasoningLevels, ThinkingLevel
from harness.app.ports import Effort, ModelSettings, ResolvedModel
from model_service.models import ModelProviderRecord
from model_service.policy import (
    normalize_effort,
    reasoning_capabilities,
    resolve_thinking_protocol,
)

# 工作区端点统一的 SDK 重试与超时：模型服务配置里没有这两项，沿用既有取值。
_WORKSPACE_MAX_RETRIES = 1
_WORKSPACE_TIMEOUT_SECONDS = 30.0


def reasoning_levels(extra_config: dict[str, Any]) -> ReasoningLevels:
    """把端点的档位配置展开成全部 Effort 的落地方式。

    每个档位先按 normalize_effort 收敛到端点支持的档位（不支持时回落模型默认档位），
    再换成上游实际接收的取值；未配置档位映射的端点开启思考但不显式下发档位。
    """
    configured = reasoning_capabilities(extra_config)["reasoning_effort_levels"]
    levels: dict[Effort, ThinkingLevel] = {}
    for effort in Effort:
        normalized = normalize_effort(extra_config, effort.value)
        if normalized == Effort.FAST:
            levels[effort] = ThinkingLevel(enabled=False)
            continue
        # 取第一个匹配的档位：与保存时的去重校验一致，旧数据重复时也不改变取值。
        wire_value = next(
            (item["value"] for item in configured if item["key"] == normalized),
            None,
        )
        levels[effort] = ThinkingLevel(enabled=True, value=wire_value)
    return ReasoningLevels(levels)


def build_model_client(record: ModelProviderRecord) -> AnthropicClient | OpenAIClient:
    """按工作区模型配置构造客户端：Anthropic 走原生协议，其余供应商走 OpenAI 兼容协议。"""
    extra = record.extra_config or {}
    if record.provider == "anthropic":
        return AnthropicClient(
            model=record.model_name,
            api_key=record.api_key or "no-key",
            base_url=record.base_url,
            max_retries=_WORKSPACE_MAX_RETRIES,
            timeout=_WORKSPACE_TIMEOUT_SECONDS,
            thinking_budget_tokens=extra.get("thinking_budget_tokens"),
            reasoning_levels=reasoning_levels(extra),
        )
    return OpenAIClient(
        model=record.model_name,
        api_key=record.api_key or "no-key",
        base_url=record.base_url,
        max_retries=_WORKSPACE_MAX_RETRIES,
        timeout=_WORKSPACE_TIMEOUT_SECONDS,
        thinking_protocol=resolve_thinking_protocol(record.provider, extra),
        fast_reasoning_effort=extra.get("fast_reasoning_effort"),
        reasoning_levels=reasoning_levels(extra),
    )


def build_resolved_model(record: ModelProviderRecord, effort: Effort | None) -> ResolvedModel:
    """构造客户端并带上该端点的默认调用参数：模型名取配置，思考强度按调用方请求。

    effort 为 None 时按 fast 处理（见 ReasoningLevels.level），工作区端点没有独立的默认思考开关。
    """
    context_length = (record.extra_config or {}).get("context_length")
    # type() 判断排除 bool 等 int 子类，与保存时的校验口径一致。
    if type(context_length) is not int:
        context_length = None
    return ResolvedModel(
        client=build_model_client(record),
        settings=ModelSettings(model_id=record.model_name, effort=effort),
        provider_record_id=record.provider_id,
        context_length=context_length,
    )
