"""提示词组装层：决定向模型发什么，不负责怎么发（传输在 llm/）。"""

from prompt.compose import (
    compose_lead_blocks,
    compose_lead_prompt,
    compose_subagent_prompt,
    compose_teammate_prompt,
)
from prompt.context import PromptContext

__all__ = [
    "PromptContext",
    "compose_lead_blocks",
    "compose_lead_prompt",
    "compose_subagent_prompt",
    "compose_teammate_prompt",
]
