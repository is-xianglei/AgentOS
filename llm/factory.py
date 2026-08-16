"""LLM 客户端工厂:按全局 provider 与场景档位创建客户端。

模型解析在本层完成(场景覆盖 > provider 主模型,2 层回落),客户端构造时
收到的已是解析好的模型,不再自行读配置。provider 未配置、取值不支持、
api_key 缺失、模型解析不出时一律抛中文 AgentException,不做静默兜底。
"""

from core.config import settings
from core.errors import AgentException
from llm.anthropic_client import AnthropicClient
from llm.base import BaseLLMClient
from llm.openai_client import OpenAIClient

_SUPPORTED_PROVIDERS = ("anthropic", "openai")

# 已注册场景,与 Settings 中 {provider}_{scenario}_model 字段一一对应;
# 场景拼错属编码错误,直接抛错而非静默回落主模型。
_KNOWN_SCENARIOS = frozenset(
    {
        "agent",
        "subagent",
        "subagent_fast",
        "compact",
        "memory_selector",
        "memory_extractor",
        "memory_dream",
    }
)


def _blank_to_none(value: str | None) -> str | None:
    """把 None / 空串 / 纯空白统一归一成 None(.env 留空的键会读成空串)。"""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _require_provider() -> str:
    """读取并校验全局 provider,未配置或不支持时抛中文错误。"""
    provider = _blank_to_none(settings.llm_provider)
    if not provider:
        raise AgentException.message(
            "未配置 LLM provider，请在 .env 中设置 AGENTOS_LLM_PROVIDER"
            "（可选值：anthropic / openai）"
        )
    if provider not in _SUPPORTED_PROVIDERS:
        raise AgentException.message(
            f"不支持的 LLM provider：{provider}，可选值：anthropic / openai"
        )
    return provider


def _resolve_model(provider: str, scenario: str | None) -> str:
    """按「场景覆盖 > provider 主模型」解析模型,两级都为空则抛中文错误。

    场景覆盖未配置时回落主模型是既定语义(覆盖本就是可选项);
    主模型也未配置属必填项缺失,直接抛错,不存在硬编码模型兜底。
    """
    if scenario is not None:
        if scenario not in _KNOWN_SCENARIOS:
            raise AgentException.message(
                f"未知的 LLM 场景：{scenario}，"
                f"可选值：{'、'.join(sorted(_KNOWN_SCENARIOS))}"
            )
        scenario_model = _blank_to_none(getattr(settings, f"{provider}_{scenario}_model"))
        if scenario_model:
            return scenario_model

    model = _blank_to_none(getattr(settings, f"{provider}_model"))
    if not model:
        raise AgentException.message(
            f"未配置 {provider} 的模型，请设置 AGENTOS_{provider.upper()}_MODEL，"
            f"或为该场景设置 AGENTOS_{provider.upper()}_<SCENARIO>_MODEL"
        )
    return model


def resolve_scenario_model(scenario: str | None = None) -> str:
    """解析当前 provider 下指定场景最终生效的模型名。

    供只需模型名而不新建客户端的调用方使用(如 SubAgent 速度档的逐调用覆盖、
    Memory Job 入队时冻结模型)。校验规则与 create_llm_client 一致。
    """
    return _resolve_model(_require_provider(), scenario)


def create_llm_client(scenario: str | None = None) -> BaseLLMClient:
    """创建 LLM 客户端,根据全局 provider 和场景选择模型。

    Args:
        scenario: 场景标识(agent/subagent/subagent_fast/compact/
                  memory_selector/memory_extractor/memory_dream),
                  None 表示直接使用 provider 主模型。

    Returns:
        BaseLLMClient 实例(AnthropicClient 或 OpenAIClient)。
    """
    provider = _require_provider()
    model = _resolve_model(provider, scenario)

    if provider == "anthropic":
        api_key = _blank_to_none(settings.anthropic_api_key)
        if not api_key:
            raise AgentException.message(
                "LLM provider 为 anthropic，但未配置 AGENTOS_ANTHROPIC_API_KEY"
            )
        return AnthropicClient(
            model=model,
            api_key=api_key,
            base_url=settings.anthropic_base_url,
            max_retries=settings.anthropic_max_retries,
            timeout=settings.anthropic_timeout_seconds,
        )

    api_key = _blank_to_none(settings.openai_api_key)
    if not api_key:
        raise AgentException.message(
            "LLM provider 为 openai，但未配置 AGENTOS_OPENAI_API_KEY"
        )
    return OpenAIClient(
        model=model,
        api_key=api_key,
        base_url=settings.openai_base_url,
        max_retries=settings.openai_max_retries,
        timeout=settings.openai_timeout_seconds,
    )
