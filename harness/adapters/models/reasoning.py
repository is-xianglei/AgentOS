"""模型端点的思考档位映射表：每个 Effort 在某个工作区端点上的落地方式。

表在构造客户端时一次算好（见 builder.reasoning_levels），客户端每次调用只查表，
不在实例上保存或修改任何思考状态。
"""

from collections.abc import Mapping
from dataclasses import dataclass

from core.errors import AgentException
from harness.app.ports import Effort


@dataclass(frozen=True)
class ThinkingLevel:
    """某个思考强度在端点上的落地方式。

    enabled 为 False 表示关闭思考；value 是上游实际接收的取值（档位名或思考预算 token 数），
    None 表示开启思考但不显式下发档位，由各家客户端按端点配置决定预算或省略该参数。
    """

    enabled: bool
    value: str | int | None = None


@dataclass(frozen=True)
class ReasoningLevels:
    """工作区端点上全部 Effort 的落地方式，构造时已按端点能力收敛完毕。"""

    levels: Mapping[Effort, ThinkingLevel]

    def __post_init__(self) -> None:
        missing = [effort.value for effort in Effort if effort not in self.levels]
        if missing:
            raise AgentException.message("思考档位映射表不完整", {"missing": missing})

    def level(self, effort: Effort | None) -> ThinkingLevel:
        """取档位的落地方式。

        未指定档位时按 fast 处理：工作区端点没有 .env 那样独立的默认思考开关，
        与「未传思考强度即 fast」的既有语义一致。
        """
        return self.levels[effort or Effort.FAST]


def resolve_level(levels: ReasoningLevels | None, effort: Effort | None) -> ThinkingLevel | None:
    """求本次调用的思考落地方式；返回 None 表示沿用 .env 的思考配置。

    levels 为 None 即 .env 端点：它没有档位映射表，指定档位时开启思考并把档位名原样下发，
    fast 关闭思考，未指定档位时交给 .env 的 {provider}_thinking_* 配置。
    """
    if levels is not None:
        return levels.level(effort)
    if effort is None:
        return None
    if effort is Effort.FAST:
        return ThinkingLevel(enabled=False)
    return ThinkingLevel(enabled=True, value=effort.value)
