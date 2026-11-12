"""MCP 专属的被驱动端口：对远程 server 的一次性动作（探测、缓存失效），以及按身份列 server。

只用领域值类型，不涉及 SDK 类型。McpServerConnector 的实现在 harness/adapters/mcp/connector.py；
McpServerSource 由 mcp_server 域实现（M1b），供 McpToolProvider 每回合读取配置与凭证。
OAuth 的两个半程（begin_oauth、complete_oauth）在 M3 加入；server 配置读写端口
McpServerRegistry 在 M1b 随 mcp_server 域一起加入。
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Protocol

from harness.domain.mcp import McpProbeResult, McpSecret, McpServerConfig, McpServerEntry
from harness.domain.tool import ToolScope


class McpServerConnector(ABC):
    """对远程 server 的一次性动作。连接池与目录缓存不在这里暴露。"""

    @abstractmethod
    async def probe(self, config: McpServerConfig, secret: McpSecret | None) -> McpProbeResult:
        """建连并翻页拉取完整工具清单，整体受探测超时限制；失败不抛异常，结果里带中文错误。

        地址不合法（SSRF 校验不通过）属于调用方输入错误，抛中文 AgentException。
        """

    @abstractmethod
    async def invalidate(self, server_id: int) -> None:
        """配置变更后让本进程里这个 server 的连接和目录缓存失效；其他实例靠 config_rev 变化自然失效。"""


class McpServerSource(Protocol):
    """按身份列出可用 server 配置与凭证的窄接口，M1b 由 mcp_server 域实现。

    返回 scope 所在工作区已启用的共享 server，加上 scope.user_id 本人的私有 server；
    server_id 一律非空。实现自己开短数据库会话，读完即关，不占用回合的事务。
    """

    async def list_servers(self, scope: ToolScope) -> Sequence[McpServerEntry]: ...
