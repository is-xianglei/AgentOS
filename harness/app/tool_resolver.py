"""回合工具解析：合并平台工具与作用域工具，产出本回合的策略全集、作用域工具和故障清单。

只管解析，不管裁剪：按档位、模式和绑定选可见集仍在回合配置里做。各作用域来源并发解析，
总时长受 total_timeout 限制；单个来源失败或超时只记一条故障，回合照常进行。
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from core.errors import AgentException
from harness.app.ports import ScopedToolProvider, ScopedTools, ToolSourceFault
from harness.app.tool_set import ToolSet
from harness.domain.tool import Tool, ToolScope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedScope:
    """一个身份在本回合解析出的工具，同一回合的主执行和子执行共用。

    - policies：策略全集，平台工具在前、作用域工具在后，不按档位和绑定裁剪；
    - scoped：本身份的全部作用域工具，按工具名排序；
    - faults：本回合不可用的来源。
    """

    policies: ToolSet
    scoped: tuple[Tool, ...]
    faults: tuple[ToolSourceFault, ...] = ()

    @classmethod
    def platform_only(cls, platform: ToolSet) -> ResolvedScope:
        """没有作用域来源时的结果：策略全集就是平台工具集本身。"""
        return cls(policies=platform, scoped=())


class TurnToolResolver:
    """每回合解析一次作用域工具；由组装根构造，只注入主 Agent 运行时。"""

    def __init__(
        self,
        platform: ToolSet,
        sources: Sequence[ScopedToolProvider],
        *,
        total_timeout: float,
    ) -> None:
        self._platform = platform
        self._sources = tuple(sources)
        self._total_timeout = total_timeout

    async def resolve(self, scope: ToolScope) -> ResolvedScope:
        """并发解析全部来源并合并；作用域工具与平台工具重名属于编码错误，直接抛中文异常。"""
        if not self._sources:
            return ResolvedScope.platform_only(self._platform)
        results = await self._list_all(scope)
        tools: list[Tool] = []
        faults: list[ToolSourceFault] = []
        for result in results:
            tools.extend(result.tools)
            faults.extend(result.faults)
        # 工具名排序保证 tools 逐字节稳定，提示缓存依赖这一点。
        scoped = tuple(sorted(tools, key=lambda tool: tool.name))
        seen: set[str] = set()
        for tool in scoped:
            if tool.name in self._platform.names:
                raise AgentException.message("作用域工具与平台工具重名", {"tool_name": tool.name})
            if tool.name in seen:
                raise AgentException.message("作用域工具重名", {"tool_name": tool.name})
            seen.add(tool.name)
        return ResolvedScope(
            policies=ToolSet((*self._platform.tools, *scoped)),
            scoped=scoped,
            faults=tuple(faults),
        )

    async def _list_all(self, scope: ToolScope) -> list[ScopedTools]:
        """按来源顺序返回各来源的结果；失败或超出总时长的来源换成只含一条故障的结果。"""
        tasks = [asyncio.create_task(source.list_tools(scope)) for source in self._sources]
        try:
            await asyncio.wait(tasks, timeout=self._total_timeout)
        finally:
            # 超时或回合被取消时，未完成的来源一律取消并等它们收尾，不留后台任务。
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        results: list[ScopedTools] = []
        for source, task in zip(self._sources, tasks, strict=True):
            results.append(self._result_of(source, task, scope))
        return results

    def _result_of(
        self,
        source: ScopedToolProvider,
        task: asyncio.Task[ScopedTools],
        scope: ToolScope,
    ) -> ScopedTools:
        context = {
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
            "tool_source": source.name,
        }
        if task.cancelled():
            logger.warning(
                "作用域工具来源解析超时，本回合缺少它的工具，来源=%s",
                source.name,
                extra=context,
            )
            fault = ToolSourceFault(
                source=source.name,
                server_id=None,
                kind="timeout",
                message="工具来源解析超时，本回合暂时无法使用它的工具",
            )
            return ScopedTools(tools=(), faults=(fault,))
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "作用域工具来源解析失败，本回合缺少它的工具，来源=%s，错误类型=%s",
                source.name,
                type(exc).__name__,
                exc_info=exc,
                extra=context,
            )
            fault = ToolSourceFault(
                source=source.name,
                server_id=None,
                kind="unavailable",
                message="工具来源暂时不可用，本回合无法使用它的工具",
            )
            return ScopedTools(tools=(), faults=(fault,))
        return task.result()
