"""工具集：聚合多个工具来源，负责重名校验、稳定顺序、按名查询与按档位裁剪。

ToolSet 取代原 ToolRegistry。工具顺序就是发给模型的工具顺序，提示缓存依赖它保持稳定：
多个来源按传入顺序拼接，来源内部保持各自 ``list_tools()`` 的顺序，裁剪后保持原有相对顺序。
"""

from collections.abc import Iterable
from typing import Any

from core.errors import AgentException
from harness.app.ports import ToolProvider
from harness.domain.context import ToolSpec
from harness.domain.tool import Tool, ToolPolicy, ToolProfile, unknown_tool_policy


class ToolSet:
    """一组名称唯一、顺序稳定的工具；所有裁剪操作都返回新实例，不改原实例。"""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            # 不同来源的工具共用一个命名空间（MCP 工具以 mcp__server__tool 命名），
            # 重名时无法确定模型调用的是哪一个，直接报错而不是静默覆盖。
            if tool.name in self._tools:
                raise AgentException.message("工具名称重复", {"tool_name": tool.name})
            self._tools[tool.name] = tool

    @classmethod
    def from_providers(cls, *providers: ToolProvider) -> ToolSet:
        """按来源的传入顺序聚合全部工具。"""
        tools: list[Tool] = []
        for provider in providers:
            tools.extend(provider.list_tools())
        return cls(tools)

    def to_specs(self) -> list[ToolSpec]:
        return [tool.to_spec() for tool in self._tools.values()]

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise AgentException.message("工具不存在")
        return tool

    def policy_for(self, name: str) -> ToolPolicy:
        """按名查工具策略；不在本工具集里的工具任何档位都不可绑定、不可调用。"""
        tool = self._tools.get(name)
        if tool is None:
            return unknown_tool_policy(name)
        return tool.policy

    def validate_input(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        """按工具参数 schema 校验并规范化输入，供执行和人工恢复共用。"""
        return self.get(name).normalize_arguments(data)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    @property
    def tools(self) -> tuple[Tool, ...]:
        """返回稳定快照，供数据库目录同步使用。"""
        return tuple(self._tools.values())

    def without(self, *names: str) -> ToolSet:
        excluded = set(names)
        return ToolSet(tool for name, tool in self._tools.items() if name not in excluded)

    def only(self, *names: str) -> ToolSet:
        included = set(names)
        return ToolSet(tool for name, tool in self._tools.items() if name in included)

    def with_tools(self, *tools: Tool) -> ToolSet:
        """在现有工具集基础上追加 / 覆盖若干工具,返回新工具集(不改原实例)。

        用于 teammate 场景:把被黑名单剔除的 Team 以受限实例补回。同名工具以新传入的为准,
        并留在原来的位置。
        """
        merged = dict(self._tools)
        for tool in tools:
            merged[tool.name] = tool
        return ToolSet(merged.values())

    def callable_names(
        self,
        tool_names: Iterable[str],
        profile: ToolProfile,
        *,
        strict: bool = False,
    ) -> tuple[str, ...]:
        """按调用策略筛出 profile 可以调用的工具名，保持输入顺序并去重。

        strict=True 时只要有被拒绝的工具（含本工具集里没有的工具）就报错，
        避免静默丢失配置导致模型以为自己拥有某个工具。
        """
        resolved: list[str] = []
        denied: list[str] = []
        for name in dict.fromkeys(tool_names):
            if self.policy_for(name).can_call(profile):
                resolved.append(name)
            else:
                denied.append(name)
        if strict and denied:
            raise ValueError(f"工具能力策略拒绝 profile={profile}: {', '.join(denied)}")
        return tuple(resolved)

    def bindable_names(
        self,
        tool_names: Iterable[str],
        profile: ToolProfile,
        *,
        strict: bool = False,
    ) -> tuple[str, ...]:
        """按绑定策略筛出可写入 Agent 配置的工具名，保持输入顺序并去重。

        ``callable_profiles`` 描述运行时是否可以使用能力；``bindable_profiles``
        描述用户或父节点是否可以把能力写入配置。两者必须分开，否则
        ``SkillRun`` 这类由已授权 Skill 自动注入的工具会被误认为普通用户可绑定。
        """
        resolved: list[str] = []
        denied: list[str] = []
        for name in dict.fromkeys(tool_names):
            if self.policy_for(name).can_bind(profile):
                resolved.append(name)
            else:
                denied.append(name)
        if strict and denied:
            raise ValueError(f"工具绑定策略拒绝 profile={profile}: {', '.join(denied)}")
        return tuple(resolved)

    def denied_names(self, tool_names: Iterable[str], profile: ToolProfile) -> tuple[str, ...]:
        """返回 profile 不能调用的工具名，保持输入顺序并去重。"""
        return tuple(
            name
            for name in dict.fromkeys(tool_names)
            if not self.policy_for(name).can_call(profile)
        )
