"""MCP 专属的被驱动端口：对远程 server 的一次性动作、按身份列 server、server 配置的读写。

只用领域值类型，不涉及 SDK 类型，也不认识 mcp_server 域的 ORM。
- McpServerConnector 的实现在 harness/adapters/mcp/connector.py；
- McpServerSource 的实现是 mcp_server/source.py 的 McpServerCatalog，供 McpToolProvider 每回合读取配置与凭证；
- McpServerRegistry 由 mcp_server 域的 McpServerService 结构化满足，供 McpAdminService 编排管理操作。
OAuth 的两个半程（begin_oauth、complete_oauth）在 M3 加入。
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Protocol

from harness.domain.mcp import McpProbeResult, McpSecret, McpServerConfig, McpServerEntry
from harness.domain.mcp_config import (
    McpServerDraft,
    McpServerUpdate,
    McpServerView,
    McpWritableServer,
)
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

    @abstractmethod
    async def drop_cached_tools(self, server_id: int) -> None:
        """清掉本进程里这个 server 的工具清单缓存（全部授权分区），连接与 config_rev 不动。

        管理员手动刷新探测成功后调用，下一回合重新拉取清单；其他实例仍按缓存 TTL 过期。
        """


class McpServerSource(Protocol):
    """按身份列出可用 server 配置与凭证的窄接口，M1b 由 mcp_server 域实现。

    返回 scope 所在工作区已启用的共享 server，加上 scope.user_id 本人的私有 server；
    server_id 一律非空。实现自己开短数据库会话，读完即关，不占用回合的事务。
    """

    async def list_servers(self, scope: ToolScope) -> Sequence[McpServerEntry]: ...


class McpServerRegistry(Protocol):
    """server 配置的读写：角色与可见性校验、重名检查、持久化与探测结果，不做网络 I/O。

    mcp_server 域的 McpServerService 结构化满足它（同 SessionService 满足 SessionStore），不写转发器；
    应用层测试换成内存实现。actor_user_id 是当前登录用户；看不到的 server（别人的私有 server、
    别的工作区、已删除、普通成员眼里停用的共享 server）一律报 404，不区分原因。
    """

    async def list_servers(self, workspace_id: int, actor_user_id: int) -> list[McpServerView]:
        """管理员看全部共享 server，成员只看已启用的，另加本人的私有 server；不读凭证。"""
        ...

    async def get_server(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpServerView:
        """读一个可见的 server，不读凭证。"""
        ...

    async def prepare_create(
        self,
        workspace_id: int,
        actor_user_id: int,
        draft: McpServerDraft,
    ) -> None:
        """探测前的业务校验：角色、私有 server 上限、server_key 是否已被占用（不透露被谁占用）。"""
        ...

    async def create_server(
        self,
        workspace_id: int,
        actor_user_id: int,
        draft: McpServerDraft,
        probe: McpProbeResult,
    ) -> McpServerView:
        """探测成功后写库，连同凭证与工具快照；并发撞 key 时报 409「server_key 已被占用」。"""
        ...

    async def load_for_write(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpWritableServer:
        """校验写权限并读出配置与凭证，供更新、重新探测使用；看不到报 404，看得到但无权修改报 403。"""
        ...

    async def apply_update(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
        update: McpServerUpdate,
        probe: McpProbeResult | None,
    ) -> McpServerView:
        """按更新计划写库；计划之后配置被别人改过（config_rev 不符）报 409，不覆盖对方的改动。"""
        ...

    async def delete_server(self, workspace_id: int, actor_user_id: int, server_id: int) -> None:
        """软删 server 与它的凭证。"""
        ...

    async def record_probe(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
        probe: McpProbeResult,
    ) -> McpServerView:
        """记下一次探测的时间与结果；成功时用新清单替换工具快照，不改 config_rev。"""
        ...
