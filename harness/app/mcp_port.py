"""MCP 专属的被驱动端口：对远程 server 的一次性动作、按身份列 server、server 配置的读写。

只用领域值类型，不涉及 SDK 类型，也不认识 mcp_server 域的 ORM。
- McpServerConnector 的实现在 harness/adapters/mcp/connector.py；
- McpServerSource 的实现是 mcp_server/source.py 的 McpServerCatalog，供 McpToolProvider 每回合读取配置与凭证；
- McpServerRegistry 由 mcp_server 域的 McpServerService 结构化满足，供 McpAdminService 编排管理操作；
- McpOAuthStore 的实现是 mcp_server/source.py 的 McpOAuthCatalog，供 OAuth 适配器读写客户端注册与 token，
  每次调用自开短事务，不借用回合或请求的会话；
- OAuthStateStore 存授权进行中的 state，实现在 harness/adapters/mcp/oauth_state.py（Redis 与进程内两种）。
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from harness.domain.mcp import (
    McpProbeResult,
    McpSecret,
    McpServerConfig,
    McpServerEntry,
    McpToolSummary,
)
from harness.domain.mcp_config import (
    McpServerDraft,
    McpServerUpdate,
    McpServerView,
    McpWritableServer,
)
from harness.domain.mcp_oauth import (
    McpOAuthClient,
    McpOAuthGrant,
    McpOAuthPending,
    McpOAuthToken,
    McpTokenRefresh,
    OAuthCallback,
)
from harness.domain.tool import ToolScope


class McpServerConnector(ABC):
    """对远程 server 的一次性动作。连接池与目录缓存不在这里暴露。"""

    @abstractmethod
    async def probe(self, config: McpServerConfig, secret: McpSecret | None) -> McpProbeResult:
        """建连并翻页拉取完整工具清单，整体受探测超时限制；失败不抛异常，结果里带中文错误。

        OAuth server 只验证 PRM 与授权服务器元数据能被发现（不带 token、不拉清单，8.4 节）。
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

    @abstractmethod
    async def begin_oauth(self, config: McpServerConfig, *, workspace_id: int, user_id: int) -> str:
        """授权前半程：发现元数据、校验 issuer、注册客户端（CIMD 优先，其次 DCR），生成 PKCE 与 state，
        把流程状态写进 state 存储，返回带 resource 参数的授权地址。失败抛中文 AgentException。"""

    @abstractmethod
    async def complete_oauth(
        self,
        callback: OAuthCallback,
        *,
        workspace_id: int,
        user_id: int,
        config_for: Callable[[int], Awaitable[McpServerConfig]],
    ) -> McpOAuthGrant:
        """授权后半程：一次性取出 state 并核对发起人就是当前用户，校验 iss，带 code_verifier 与 resource
        换 token。config_for(server_id) 由调用方给出当前用户能授权的 server 配置（看不到即 404）。
        只做网络动作、不写 token：由调用方核对 server 仍可用后写入。"""

    @abstractmethod
    async def refresh_oauth_snapshot(
        self,
        config: McpServerConfig,
        user_id: int,
        token: McpOAuthToken,
    ) -> bool:
        """授权成功后用新 token 拉一次工具清单，与快照不一致时在独立短事务里回写；返回是否写入。

        失败只记日志、返回 False，不影响授权本身。
        """

    @abstractmethod
    async def forget_user(self, server_id: int, user_id: int) -> None:
        """撤销授权后让本进程里这个用户在这个 server 上的连接与 token 缓存失效。"""


class McpOAuthStore(Protocol):
    """OAuth 客户端注册与用户 token 的持久化，供 OAuth 适配器使用；实现每次调用自开短事务并提交。

    不借用回合或请求的会话：刷新换来的新 token 必须落库，不能随回合回滚而丢（旧 refresh_token 可能已失效）。
    """

    async def get_client(self, server_id: int) -> McpOAuthClient | None: ...

    async def save_client(self, server_id: int, client: McpOAuthClient) -> None:
        """写入或替换这个 server 的客户端注册（软删旧行再插新行）。"""
        ...

    async def get_token(self, server_id: int, user_id: int) -> McpOAuthToken | None: ...

    async def refresh_token_locked(
        self,
        server_id: int,
        user_id: int,
        refresh: McpTokenRefresh,
    ) -> McpOAuthToken | None:
        """持行锁刷新：锁内再读一次，已被别处刷新过就直接返回新值，否则调 refresh 并写回；
        没有有效 token 时返回 None。等锁超时、刷新失败都抛异常，调用方据此判为需要重新授权。"""
        ...

    async def invalidate_token(self, server_id: int, user_id: int, access_token: str) -> bool:
        """把 token 标记为失效（软删），下一回合判为需要重新授权；库里已是别的 token 时不动，返回是否软删。"""
        ...

    async def record_tools_snapshot(
        self,
        server_id: int,
        config_rev: int,
        tools: Sequence[McpToolSummary],
    ) -> bool:
        """OAuth server 的快照回写：清单与快照不一致且配置版本未变时写入，返回是否写入。"""
        ...


class OAuthStateStore(ABC):
    """授权进行中的 state 存储：写入带 TTL，取出用一次性读删（GETDEL），多实例共享。"""

    @abstractmethod
    async def put(self, state: str, pending: McpOAuthPending, *, ttl_seconds: int) -> None: ...

    @abstractmethod
    async def take(self, state: str) -> McpOAuthPending | None:
        """一次性取出：取到即删除；不存在或已过期返回 None。"""

    @abstractmethod
    async def aclose(self) -> None:
        """释放连接（lifespan 退出时调用）。"""


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

    async def require_oauth_server(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpServerConfig:
        """当前用户能对它发起授权的 OAuth server：看不到（含别人的私有 server）404，停用 409，非 OAuth 400。"""
        ...

    async def save_oauth_token(
        self,
        workspace_id: int,
        actor_user_id: int,
        grant: McpOAuthGrant,
    ) -> McpServerView:
        """授权完成后写入或替换当前用户的 token，返回带授权状态的视图；server 已不可用或配置版本已变时拒绝。"""
        ...

    async def revoke_oauth_token(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> bool:
        """撤销自己的授权（软删自己的 token），返回是否确有 token 被撤销。"""
        ...
