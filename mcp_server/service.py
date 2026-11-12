"""MCP server 域的业务规则：共享与私有 server 的可见性、写权限、上限、重名、配置版本与快照。

只做持久化与业务规则，不做网络 I/O：探测由应用层 McpAdminService 经 McpServerConnector 完成。
本类结构化满足应用层的 McpServerRegistry 端口；回合读取配置与凭证另见 source.py。
- 共享 server 只有工作区 owner / admin 能写；私有 server 只有 owner 本人能写，普通成员可以建私有 server；
- 别人的私有 server 一律按不存在处理（404），列表里也看不到；
- 凭证表只在需要凭证的方法里读，列表与详情不读它。
"""

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from harness.domain.mcp import (
    ApprovalMode,
    McpAuthType,
    McpProbeResult,
    McpSecret,
    McpServerConfig,
    McpServerEntry,
    McpToolSummary,
    model_tool_names,
    server_key_of,
)
from harness.domain.mcp_config import (
    McpServerDraft,
    McpServerUpdate,
    McpServerView,
    McpWritableServer,
)
from mcp_server.models import McpServerRecord, McpServerSecretRecord
from mcp_server.repository import McpServerRepository
from workspace.service import WorkspaceService

logger = logging.getLogger(__name__)

_ADMIN_ROLES = frozenset({"owner", "admin"})


def _server_not_found() -> AgentException:
    # 别人的私有 server、别的工作区、已删除、成员眼里停用的共享 server 一律同一句话，不泄露存在与否。
    return AgentException.message("MCP server 不存在", status_code=404)


def _key_taken(server_key: str) -> AgentException:
    # 不透露 key 被谁占用：可能是别人的私有 server。
    return AgentException.message(
        "server_key 已被占用",
        {"server_key": server_key},
        status_code=409,
    )


def record_config(record: McpServerRecord) -> McpServerConfig:
    """ORM 行 → 领域配置值。"""
    return McpServerConfig(
        server_id=record.id,
        workspace_id=record.workspace_id,
        owner_user_id=record.owner_user_id,
        server_key=record.server_key,
        url=record.url,
        auth_type=McpAuthType(record.auth_type),
        approval_mode=ApprovalMode(record.approval_mode),
        tool_allowlist=tuple(record.tool_allowlist),
        tool_denylist=tuple(record.tool_denylist),
        call_timeout_seconds=record.call_timeout_seconds,
        config_rev=record.config_rev,
        is_enabled=record.is_enabled,
    )


def record_tools(record: McpServerRecord) -> tuple[McpToolSummary, ...]:
    return tuple(McpToolSummary.from_dict(item) for item in record.tools_snapshot)


def record_secret(record: McpServerSecretRecord | None) -> McpSecret | None:
    if record is None or not record.headers:
        return None
    return McpSecret.from_headers(record.headers)


def _view(
    record: McpServerRecord,
    *,
    writable: bool,
    header_names: Iterable[str] | None = None,
) -> McpServerView:
    return McpServerView(
        config=record_config(record),
        name=record.name,
        description=record.description,
        created_by=record.created_by,
        writable=writable,
        last_probe_at=record.last_probe_at,
        last_probe_error=record.last_probe_error,
        tools=record_tools(record),
        created_at=record.created_at,
        updated_at=record.updated_at,
        header_names=tuple(sorted(header_names)) if header_names is not None else None,
    )


class McpServerService:
    """MCP server 的增删改查与业务规则；持有请求或调用方的数据库会话，只 flush 不提交。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = McpServerRepository(db)
        self.workspace_service = WorkspaceService(db)

    # ---------- 访问规则 ----------

    async def _is_admin(self, workspace_id: int, actor_user_id: int) -> bool:
        """校验活跃成员身份，返回是否是工作区 owner / admin。"""
        member = await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        return member.role in _ADMIN_ROLES

    @staticmethod
    def _visible(record: McpServerRecord, actor_user_id: int, *, is_admin: bool) -> bool:
        if record.owner_user_id is not None:
            return record.owner_user_id == actor_user_id
        return is_admin or record.is_enabled

    @staticmethod
    def _writable(record: McpServerRecord, actor_user_id: int, *, is_admin: bool) -> bool:
        if record.owner_user_id is not None:
            return record.owner_user_id == actor_user_id
        return is_admin

    async def _require_visible(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
        *,
        for_update: bool = False,
    ) -> tuple[McpServerRecord, bool]:
        """取一个对当前用户可见的 server，返回 (记录, 是否管理员)；看不到一律 404。"""
        is_admin = await self._is_admin(workspace_id, actor_user_id)
        if for_update:
            record = await self.repo.get_for_update(workspace_id, server_id)
        else:
            record = await self.repo.get(workspace_id, server_id)
        if record is None or not self._visible(record, actor_user_id, is_admin=is_admin):
            raise _server_not_found()
        return record, is_admin

    async def _require_writable(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpServerRecord:
        """加行锁取一个可写的 server：看不到报 404，看得到但不能改（成员对共享 server）报 403。"""
        record, is_admin = await self._require_visible(
            workspace_id,
            actor_user_id,
            server_id,
            for_update=True,
        )
        if not self._writable(record, actor_user_id, is_admin=is_admin):
            raise AgentException.message("只有工作区管理员可以修改共享 MCP server", status_code=403)
        return record

    # ---------- 读：列表与详情不读凭证表 ----------

    async def list_servers(self, workspace_id: int, actor_user_id: int) -> list[McpServerView]:
        """管理员看全部共享 server，成员只看已启用的，另加本人的私有 server；共享在前。"""
        is_admin = await self._is_admin(workspace_id, actor_user_id)
        records = await self.repo.list_visible(
            workspace_id,
            actor_user_id,
            include_disabled_shared=is_admin,
        )
        return [
            _view(record, writable=self._writable(record, actor_user_id, is_admin=is_admin))
            for record in records
        ]

    async def get_server(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpServerView:
        record, is_admin = await self._require_visible(workspace_id, actor_user_id, server_id)
        return _view(record, writable=self._writable(record, actor_user_id, is_admin=is_admin))

    # ---------- 新建 ----------

    async def prepare_create(
        self,
        workspace_id: int,
        actor_user_id: int,
        draft: McpServerDraft,
    ) -> None:
        """探测前的业务校验：共享 server 要 owner / admin；私有 server 要活跃成员且没超过每人上限；
        server_key 共享和私有一起算，已被占用时只说「已被占用」，不透露是谁。"""
        if draft.private:
            await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
            await self._require_private_quota(workspace_id, actor_user_id)
        else:
            await self.workspace_service.require_workspace_role(
                workspace_id,
                actor_user_id,
                set(_ADMIN_ROLES),
            )
        if await self.repo.key_taken(workspace_id, draft.server_key):
            raise _key_taken(draft.server_key)

    async def _require_private_quota(self, workspace_id: int, owner_user_id: int) -> None:
        limit = settings.mcp_max_private_servers_per_user
        if await self.repo.count_private(workspace_id, owner_user_id) >= limit:
            raise AgentException.message(
                "私有 MCP server 数量已达上限",
                {"limit": limit},
                status_code=409,
            )

    async def create_server(
        self,
        workspace_id: int,
        actor_user_id: int,
        draft: McpServerDraft,
        probe: McpProbeResult,
    ) -> McpServerView:
        """探测成功后写库，连同凭证与工具快照。

        探测可能耗时数十秒，所以这里重新核对角色；私有 server 的上限在按用户的咨询锁里重新核对，
        防止同一用户并发创建越过上限。server_key 的并发竞态由部分唯一索引兜底，转成 409。
        """
        if not probe.ok:
            raise AgentException.message("探测失败的 MCP server 不能写库", status_code=400)
        owner_user_id: int | None = None
        if draft.private:
            await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
            await self.repo.lock_private_quota(actor_user_id)
            await self._require_private_quota(workspace_id, actor_user_id)
            owner_user_id = actor_user_id
        else:
            await self.workspace_service.require_workspace_role(
                workspace_id,
                actor_user_id,
                set(_ADMIN_ROLES),
            )
        record = McpServerRecord(
            workspace_id=workspace_id,
            owner_user_id=owner_user_id,
            server_key=draft.server_key,
            name=draft.name,
            description=draft.description,
            url=draft.url,
            auth_type=draft.auth_type.value,
            approval_mode=draft.approval_mode.value,
            tool_allowlist=list(draft.tool_allowlist),
            tool_denylist=list(draft.tool_denylist),
            call_timeout_seconds=draft.call_timeout_seconds,
            config_rev=1,
            is_enabled=draft.is_enabled,
            last_probe_at=datetime.now(UTC),
            last_probe_error=None,
            tools_snapshot=[tool.to_dict() for tool in probe.tools],
            created_by=actor_user_id,
        )
        try:
            record = await self.repo.add(record)
        except IntegrityError as exc:
            raise _key_taken(draft.server_key) from exc
        headers: dict[str, str] = draft.secret.as_dict() if draft.secret is not None else {}
        if headers:
            await self.repo.save(McpServerSecretRecord(server_id=record.id, headers=headers))
        logger.info(
            "MCP server 已创建，server_key=%s，私有=%s",
            record.server_key,
            draft.private,
            extra=self._log_context(record),
        )
        return _view(record, writable=True, header_names=headers)

    # ---------- 修改、删除与探测结果：都先加行锁，同一 server 的写操作串行 ----------

    async def load_for_write(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpWritableServer:
        """校验写权限并读出配置与凭证；行锁持有到请求事务结束，期间别的写请求排队等待。"""
        record = await self._require_writable(workspace_id, actor_user_id, server_id)
        secret_record = await self.repo.get_secret(record.id)
        secret = record_secret(secret_record)
        header_names = secret.as_dict().keys() if secret is not None else ()
        return McpWritableServer(
            view=_view(record, writable=True, header_names=header_names),
            secret=secret,
        )

    async def apply_update(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
        update: McpServerUpdate,
        probe: McpProbeResult | None,
    ) -> McpServerView:
        """按更新计划写库：配置版本与计划所依据的不符说明期间被改过，报 409 不覆盖；
        带探测结果时同时刷新快照与探测时间。"""
        record = await self._require_writable(workspace_id, actor_user_id, server_id)
        if record.config_rev != update.base_rev:
            raise AgentException.message(
                "MCP server 配置已被修改，请刷新后重试",
                status_code=409,
            )
        config = update.config
        record.name = update.name
        record.description = update.description
        record.url = config.url
        record.auth_type = config.auth_type.value
        record.approval_mode = config.approval_mode.value
        record.tool_allowlist = list(config.tool_allowlist)
        record.tool_denylist = list(config.tool_denylist)
        record.call_timeout_seconds = (
            int(config.call_timeout_seconds) if config.call_timeout_seconds is not None else None
        )
        record.is_enabled = config.is_enabled
        record.config_rev = config.config_rev
        if probe is not None:
            self._apply_probe(record, probe)
        await self.repo.save(record)
        headers = await self._replace_secret(record.id, update)
        logger.info(
            "MCP server 已修改，server_key=%s，config_rev=%s",
            record.server_key,
            record.config_rev,
            extra=self._log_context(record),
        )
        return _view(record, writable=True, header_names=headers)

    async def _replace_secret(self, server_id: int, update: McpServerUpdate) -> list[str]:
        """凭证有变化时软删旧行再写新行，返回更新后的请求头名称；没变化时原样保留。"""
        headers = update.secret.as_dict() if update.secret is not None else {}
        if not update.secret_changed:
            return list(headers)
        await self.repo.soft_delete_secrets(server_id)
        if headers:
            await self.repo.save(McpServerSecretRecord(server_id=server_id, headers=headers))
        return list(headers)

    async def delete_server(self, workspace_id: int, actor_user_id: int, server_id: int) -> None:
        """软删 server 与它的凭证，并经 agent 域解除全部 Agent 对它的绑定，三者同一事务。"""
        # 局部导入打破导入环：agent.service 在模块级导入本模块（绑定时校验 server）。
        from agent.service import AgentDefinitionService

        record = await self._require_writable(workspace_id, actor_user_id, server_id)
        await self.repo.soft_delete_secrets(record.id)
        await self.repo.soft_delete(record)
        await AgentDefinitionService(self.db).unbind_mcp_server(record.id)
        logger.info(
            "MCP server 已删除，server_key=%s",
            record.server_key,
            extra=self._log_context(record),
        )

    async def record_probe(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
        probe: McpProbeResult,
    ) -> McpServerView:
        """记下一次探测的时间与结果；成功时用新清单替换快照，失败时保留旧快照。不改 config_rev。"""
        record = await self._require_writable(workspace_id, actor_user_id, server_id)
        self._apply_probe(record, probe)
        await self.repo.save(record)
        return _view(record, writable=True)

    @staticmethod
    def _apply_probe(record: McpServerRecord, probe: McpProbeResult) -> None:
        record.last_probe_at = datetime.now(UTC)
        if probe.ok:
            record.last_probe_error = None
            record.tools_snapshot = [tool.to_dict() for tool in probe.tools]
        else:
            record.last_probe_error = probe.error

    @staticmethod
    def _log_context(record: McpServerRecord) -> dict[str, int | str | None]:
        return {
            "workspace_id": record.workspace_id,
            "mcp_server_id": record.id,
            "mcp_server_key": record.server_key,
        }

    # ---------- 给回合与别的域用的查询 ----------

    async def list_usable(self, workspace_id: int, user_id: int) -> list[McpServerEntry]:
        """回合可用的 server 配置与凭证：已启用的共享 server，加上 user_id 本人已启用的私有 server。

        McpServerSource 的数据来源；身份由运行时从可信回合身份给出，这里不再校验成员资格。
        """
        records = await self.repo.list_enabled_usable(workspace_id, user_id)
        secrets = await self.repo.list_secrets([record.id for record in records])
        return [
            McpServerEntry(
                config=record_config(record),
                secret=record_secret(secrets.get(record.id)),
            )
            for record in records
        ]

    async def require_usable(
        self,
        workspace_id: int,
        server_ids: Sequence[int],
        *,
        actor_user_id: int,
    ) -> dict[int, str]:
        """校验这些 server 都能绑定到 Agent，返回 server_id → server_key。

        - 查不到（别的工作区、已删除）报 404；别人的私有 server 同样 404，不泄露它的存在；
        - 本人的私有 server 报 400：Agent 是工作区共享资源，别的成员用它时并没有这个 server；
        - 停用的共享 server 报 409。
        调用方（agent 域）已校验过写权限，这里只按 actor_user_id 判断私有 server 是不是本人的。
        """
        normalized = list(dict.fromkeys(server_ids))
        records = {
            record.id: record for record in await self.repo.list_by_ids(workspace_id, normalized)
        }
        missing = [
            server_id
            for server_id in normalized
            if server_id not in records
            or records[server_id].owner_user_id not in (None, actor_user_id)
        ]
        if missing:
            raise AgentException.message(
                "MCP server 不存在",
                {"server_ids": missing},
                status_code=404,
            )
        private = [
            server_id for server_id in normalized if records[server_id].owner_user_id is not None
        ]
        if private:
            raise AgentException.message(
                "私有 MCP server 不能绑定到 Agent，只能绑定工作区共享的 server",
                {"server_ids": private},
                status_code=400,
            )
        disabled = [server_id for server_id in normalized if not records[server_id].is_enabled]
        if disabled:
            raise AgentException.message(
                "MCP server 已停用，不能绑定",
                {"server_ids": disabled},
                status_code=409,
            )
        return {server_id: records[server_id].server_key for server_id in normalized}

    async def enabled_shared_keys(
        self,
        workspace_id: int,
        server_ids: Sequence[int],
    ) -> dict[int, str]:
        """这些 server 里仍可用（本工作区、未删除、已启用的共享 server）的 server_id → server_key。

        给 Agent 的运行配置用：不可用的直接略过、不报错，停用的 server 重新启用后绑定随之恢复。
        """
        records = await self.repo.list_by_ids(workspace_id, list(dict.fromkeys(server_ids)))
        return {
            record.id: record.server_key
            for record in records
            if record.owner_user_id is None and record.is_enabled
        }

    async def is_known_shared_tool(self, workspace_id: int, tool_name: str) -> bool:
        """权限规则的工具名校验（9.1 节）：工具名是否属于本工作区某个共享 server。

        - 有快照的 server：按快照里的原名算出模型名，精确匹配；
        - 快照为空的 OAuth server（拿不到清单）：只要 server_key 对得上，即按 mcp__{key}__ 前缀匹配；
        - 私有 server 不参与：管理员看不到它们，校验结果也不能泄露它们的存在。
        停用的共享 server 仍算已知：规则可以先配好，启用后生效。
        """
        server_key = server_key_of(tool_name)
        if server_key is None:
            return False
        record = await self.repo.get_shared_by_key(workspace_id, server_key)
        if record is None:
            return False
        tools = record_tools(record)
        if not tools:
            return record.auth_type == McpAuthType.OAUTH.value
        naming = model_tool_names(record.server_key, [tool.name for tool in tools])
        return tool_name in naming.names.values()
