"""提示词组装的输入状态。

PromptContext 汇总组装 system prompt 所需的全部运行态。段是否加载取决于这里的
真实状态（catalog 是否为空、会话是否带指令），不靠在消息里搜关键词。
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class PromptContext:
    """主 Agent system prompt 的组装输入。

    frozen 使其可直接用 == 比较，无需额外的序列化 cache key。
    字段按易失性从低到高声明，与 compose_lead_prompt 的拼接顺序一致。
    """

    cwd: str
    today: date
    skills_catalog: str | None = None
    session_prompt: str | None = None
    memory_catalog: str | None = None

    @classmethod
    def create(
        cls,
        *,
        skills_catalog: str | None = None,
        session_prompt: str | None = None,
        memory_catalog: str | None = None,
        cwd: str | None = None,
        today: date | None = None,
    ) -> "PromptContext":
        """从当前运行态构造；cwd 与 today 默认取进程真实状态。"""
        return cls(
            cwd=cwd if cwd is not None else str(Path.cwd()),
            today=today if today is not None else date.today(),
            skills_catalog=skills_catalog,
            session_prompt=session_prompt,
            memory_catalog=memory_catalog,
        )
