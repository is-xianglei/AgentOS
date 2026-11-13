"""MCP server 域的数据访问：mcp_servers、mcp_server_secrets 与 OAuth 的两张表，事务由调用方边界提交。"""

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.models import (
    McpOAuthClientRecord,
    McpOAuthTokenRecord,
    McpServerRecord,
    McpServerSecretRecord,
)

# 私有 server 上限的咨询锁命名空间，即 pg_advisory_xact_lock(int4, int4) 的第一个键（ASCII "MCPS"），
# 第二个键是用户 ID；与 "USER"、"DEPT" 命名空间互不阻塞。
PRIVATE_QUOTA_LOCK_NAMESPACE = 0x4D435053


class McpServerRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _in_workspace(workspace_id: int) -> Select[tuple[McpServerRecord]]:
        return select(McpServerRecord).where(McpServerRecord.workspace_id == workspace_id)

    async def get(self, workspace_id: int, server_id: int) -> McpServerRecord | None:
        stmt = self._in_workspace(workspace_id).where(McpServerRecord.id == server_id)
        return await self.db.scalar(stmt)

    async def get_for_update(self, workspace_id: int, server_id: int) -> McpServerRecord | None:
        """加行锁读：更新、删除与写探测结果之间串行，配置版本按锁内读到的值核对。"""
        stmt = (
            self._in_workspace(workspace_id)
            .where(McpServerRecord.id == server_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.db.scalar(stmt)

    async def list_visible(
        self,
        workspace_id: int,
        user_id: int,
        *,
        include_disabled_shared: bool,
    ) -> list[McpServerRecord]:
        """共享 server（按需排除停用的）加上 user_id 本人的私有 server，私有在后、各自按 key 排序。"""
        shared = McpServerRecord.owner_user_id.is_(None)
        if not include_disabled_shared:
            shared = shared & McpServerRecord.is_enabled.is_(True)
        stmt = (
            self._in_workspace(workspace_id)
            .where(or_(shared, McpServerRecord.owner_user_id == user_id))
            .order_by(McpServerRecord.owner_user_id.is_not(None), McpServerRecord.server_key)
        )
        return list(await self.db.scalars(stmt))

    async def list_enabled_usable(self, workspace_id: int, user_id: int) -> list[McpServerRecord]:
        """回合可用的 server：已启用的共享 server，加上 user_id 本人已启用的私有 server。"""
        stmt = (
            self._in_workspace(workspace_id)
            .where(
                McpServerRecord.is_enabled.is_(True),
                or_(
                    McpServerRecord.owner_user_id.is_(None),
                    McpServerRecord.owner_user_id == user_id,
                ),
            )
            .order_by(McpServerRecord.server_key)
        )
        return list(await self.db.scalars(stmt))

    async def list_by_ids(
        self,
        workspace_id: int,
        server_ids: Sequence[int],
    ) -> list[McpServerRecord]:
        """本工作区内这些 ID 对应的有效 server（共享与私有都在内），按 ID 排序。"""
        if not server_ids:
            return []
        stmt = (
            self._in_workspace(workspace_id)
            .where(McpServerRecord.id.in_(list(server_ids)))
            .order_by(McpServerRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def key_taken(self, workspace_id: int, server_key: str) -> bool:
        """server_key 是否已被本工作区任一有效 server 占用，共享和私有一起算。"""
        stmt = (
            select(McpServerRecord.id)
            .where(
                McpServerRecord.workspace_id == workspace_id,
                McpServerRecord.server_key == server_key,
            )
            .limit(1)
        )
        return (await self.db.scalar(stmt)) is not None

    async def count_private(self, workspace_id: int, owner_user_id: int) -> int:
        stmt = select(func.count(McpServerRecord.id)).where(
            McpServerRecord.workspace_id == workspace_id,
            McpServerRecord.owner_user_id == owner_user_id,
        )
        return int(await self.db.scalar(stmt) or 0)

    async def add(self, record: McpServerRecord) -> McpServerRecord:
        """插入一条 server；撞部分唯一索引时 flush 抛 IntegrityError，由 Service 转成 409。"""
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def save(
        self,
        record: (
            McpServerRecord | McpServerSecretRecord | McpOAuthClientRecord | McpOAuthTokenRecord
        ),
    ) -> None:
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)

    async def soft_delete(self, record: McpServerRecord) -> None:
        record.is_deleted = True
        record.deleted_at = datetime.now(UTC)
        await self.db.flush()

    # ---------- 凭证：只有需要凭证的方法才调用，列表与详情不读这张表 ----------

    async def get_secret(self, server_id: int) -> McpServerSecretRecord | None:
        stmt = select(McpServerSecretRecord).where(McpServerSecretRecord.server_id == server_id)
        return await self.db.scalar(stmt)

    async def list_secrets(self, server_ids: Sequence[int]) -> dict[int, McpServerSecretRecord]:
        if not server_ids:
            return {}
        stmt = select(McpServerSecretRecord).where(
            McpServerSecretRecord.server_id.in_(list(server_ids)),
        )
        return {row.server_id: row for row in await self.db.scalars(stmt)}

    async def soft_delete_secrets(self, server_id: int) -> None:
        """软删这个 server 的有效凭证行；没有凭证时什么也不做。"""
        await self.db.execute(
            update(McpServerSecretRecord)
            .where(
                McpServerSecretRecord.server_id == server_id,
                McpServerSecretRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=datetime.now(UTC)),
        )

    async def get_shared_by_key(self, workspace_id: int, server_key: str) -> McpServerRecord | None:
        stmt = self._in_workspace(workspace_id).where(
            McpServerRecord.owner_user_id.is_(None),
            McpServerRecord.server_key == server_key,
        )
        return await self.db.scalar(stmt)

    async def lock_private_quota(self, owner_user_id: int) -> None:
        """按用户加事务级咨询锁，让同一用户并发创建私有 server 时串行核对上限；提交或回滚时释放。"""
        await self.db.execute(
            select(func.pg_advisory_xact_lock(PRIVATE_QUOTA_LOCK_NAMESPACE, owner_user_id)),
        )

    # ---------- OAuth：客户端注册与用户 token，只有建连、刷新与授权流程才读 ----------

    async def get_oauth_client(self, server_id: int) -> McpOAuthClientRecord | None:
        stmt = select(McpOAuthClientRecord).where(McpOAuthClientRecord.server_id == server_id)
        return await self.db.scalar(stmt)

    async def soft_delete_oauth_clients(self, server_id: int) -> None:
        await self.db.execute(
            update(McpOAuthClientRecord)
            .where(
                McpOAuthClientRecord.server_id == server_id,
                McpOAuthClientRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=datetime.now(UTC)),
        )

    async def get_oauth_token(
        self,
        server_id: int,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> McpOAuthTokenRecord | None:
        """读一个用户的有效 token；for_update 时加行锁，持锁到事务结束，同一 token 的刷新因此串行。"""
        stmt = select(McpOAuthTokenRecord).where(
            McpOAuthTokenRecord.server_id == server_id,
            McpOAuthTokenRecord.user_id == user_id,
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return await self.db.scalar(stmt)

    async def list_oauth_tokens(
        self,
        server_ids: Sequence[int],
        user_id: int,
    ) -> dict[int, McpOAuthTokenRecord]:
        if not server_ids:
            return {}
        stmt = select(McpOAuthTokenRecord).where(
            McpOAuthTokenRecord.server_id.in_(list(server_ids)),
            McpOAuthTokenRecord.user_id == user_id,
        )
        return {row.server_id: row for row in await self.db.scalars(stmt)}

    async def soft_delete_oauth_tokens(self, server_id: int, user_id: int | None = None) -> int:
        """软删有效 token：给了 user_id 只删这个用户的，否则删这个 server 的全部；返回软删的行数。"""
        stmt = update(McpOAuthTokenRecord).where(
            McpOAuthTokenRecord.server_id == server_id,
            McpOAuthTokenRecord.is_deleted.is_(False),
        )
        if user_id is not None:
            stmt = stmt.where(McpOAuthTokenRecord.user_id == user_id)
        result = await self.db.execute(stmt.values(is_deleted=True, deleted_at=datetime.now(UTC)))
        return int(result.rowcount or 0)

    async def set_lock_timeout(self, seconds: int) -> None:
        """只对当前事务生效的锁等待上限；等锁超时报 LockNotAvailable（SQLSTATE 55P03）。"""
        # SET LOCAL 不接受绑定参数，这里只拼接整数秒，不拼接任何外部输入。
        await self.db.execute(text(f"SET LOCAL lock_timeout = '{int(seconds)}s'"))

    async def get_by_id_for_update(
        self,
        server_id: int,
        *,
        skip_locked: bool = False,
    ) -> McpServerRecord | None:
        """按 ID 加行锁读（不限定工作区）：OAuth 快照回写由适配器按 server_id 调用，身份来自可信回合。

        skip_locked 时行已被别的事务锁住就返回 None，不等待。
        """
        stmt = (
            select(McpServerRecord)
            .where(McpServerRecord.id == server_id)
            .with_for_update(skip_locked=skip_locked)
            .execution_options(populate_existing=True)
        )
        return await self.db.scalar(stmt)
