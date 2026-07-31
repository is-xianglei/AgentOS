import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from database.engine import AsyncSessionLocal
from memory.models import (
    MemoryItemRecord,
    MemoryRevisionRecord,
    MemorySourceRecord,
    MemorySpaceRecord,
)
from memory.repository import (
    MemoryCatalogEntry,
    MemoryCatalogSnapshot,
    MemoryExportBundle,
    MemoryPhysicalDeleteCounts,
    MemoryRepository,
)
from workspace.service import WorkspaceService

MEMORY_BODY_MAX_BYTES = 16 * 1024
MEMORY_CATALOG_MAX_BYTES = 25 * 1024
MEMORY_CATALOG_MAX_ITEMS = 200
MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference"})
_MEMORY_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_PURGE_CONFIRMATION_TTL_SECONDS = 5 * 60
_PURGE_TOKEN_VERSION = 1


@dataclass(frozen=True)
class MemoryPage:
    """Memory 分页查询结果。"""

    items: tuple[MemoryItemRecord, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class MemoryDetail:
    """Memory 详情及其审计信息。"""

    item: MemoryItemRecord
    catalog_version: int
    revisions: tuple[MemoryRevisionRecord, ...]
    sources: tuple[MemorySourceRecord, ...]


@dataclass(frozen=True)
class MemoryDeleteResult:
    """Memory 软删除结果。"""

    memory_id: int
    version: int
    catalog_version: int


@dataclass(frozen=True)
class RenderedMemoryCatalog:
    """从数据库投影确定性生成的 Catalog。"""

    snapshot: MemoryCatalogSnapshot
    content: str
    byte_count: int


@dataclass(frozen=True)
class MemoryExportResult:
    """直接从 PostgreSQL 生成的结构化数据导出。"""

    exported_at: datetime
    bundle: MemoryExportBundle


@dataclass(frozen=True)
class MemoryPurgeConfirmation:
    """绑定租户、Space 和 Catalog 版本的短时确认。"""

    confirmation_token: str
    expected_catalog_version: int
    expires_at: datetime


class MemoryExportRunner:
    """在独立 REPEATABLE READ 快照中生成用户可见导出。"""

    def __init__(self, *, session_factory=AsyncSessionLocal) -> None:
        self.session_factory = session_factory

    async def export(self, workspace_id: int, user_id: int) -> MemoryExportResult:
        async with self.session_factory() as db:
            try:
                connection = await db.connection(
                    execution_options={"isolation_level": "REPEATABLE READ"}
                )
                isolation_level = (await connection.get_isolation_level()).upper()
                if isolation_level != "REPEATABLE READ":
                    raise RuntimeError("Memory导出必须运行在REPEATABLE READ事务")
                await WorkspaceService(db).require_active_member(workspace_id, user_id)
                result = await MemoryService(db).export_memories(workspace_id, user_id)
                # 导出是纯读取，回滚即可释放快照并避免误提交未来新增的副作用。
                await db.rollback()
                return result
            except Exception:
                await db.rollback()
                raise


class MemoryService:
    def __init__(self, db: AsyncSession):
        self.repo = MemoryRepository(db)

    async def list_memories(
        self,
        workspace_id: int,
        user_id: int,
        *,
        status: str | None,
        memory_type: str | None,
        limit: int,
        offset: int,
    ) -> MemoryPage:
        """分页列出当前用户在工作区内的 Memory。"""
        items, total = await self.repo.list_items(
            workspace_id,
            user_id,
            status=status,
            memory_type=memory_type,
            limit=limit,
            offset=offset,
        )
        return MemoryPage(tuple(items), total, limit, offset)

    async def search_memories(
        self,
        workspace_id: int,
        user_id: int,
        *,
        query: str,
        memory_type: str | None,
        limit: int,
    ) -> tuple[MemoryItemRecord, ...]:
        """搜索当前 Space 的 active Memory。"""
        items = await self.repo.search_items(
            workspace_id,
            user_id,
            query=query,
            memory_type=memory_type,
            limit=limit,
        )
        return tuple(items)

    async def get_memory(
        self,
        workspace_id: int,
        user_id: int,
        memory_id: int,
    ) -> MemoryDetail:
        """读取 Memory 正文、修订和来源。"""
        item = await self.repo.get_item(workspace_id, user_id, memory_id)
        if item is None:
            raise AgentException.message("Memory 不存在", status_code=404)
        return await self._build_detail(workspace_id, user_id, item)

    async def create_memory(
        self,
        workspace_id: int,
        user_id: int,
        *,
        memory_key: str,
        memory_type: str,
        name: str,
        description: str,
        body: str,
    ) -> MemoryDetail:
        """显式创建 Memory，并在同一事务写入 Revision、Source 和 Catalog 版本。"""
        self._validate_memory(memory_key, memory_type, name, description, body)

        space = await self.repo.get_or_create_space_for_update(workspace_id, user_id)
        existing = await self.repo.get_active_item_by_key(workspace_id, user_id, memory_key)
        if existing is not None:
            raise AgentException.message(
                "memory_key 已存在",
                {"memory_key": memory_key},
                status_code=409,
            )
        if (
            await self.repo.count_active_items(workspace_id, user_id, space.id)
            >= MEMORY_CATALOG_MAX_ITEMS
        ):
            raise AgentException.message(
                f"active Memory 数量不能超过 {MEMORY_CATALOG_MAX_ITEMS} 条",
                status_code=409,
            )

        item = await self.repo.create_item(
            workspace_id,
            user_id,
            space=space,
            memory_key=memory_key,
            memory_type=memory_type,
            name=name,
            description=description,
            body=body,
            source_kind="explicit",
        )
        revision = await self.repo.create_revision(
            workspace_id,
            user_id,
            space,
            item,
            actor_type="user",
            actor_id=str(user_id),
        )
        source = await self.repo.create_source(
            workspace_id,
            user_id,
            space=space,
            item=item,
            revision=revision,
            source_kind="explicit",
        )
        await self._validate_current_catalog(workspace_id, user_id)
        catalog_version = await self.repo.increment_catalog_version(workspace_id, user_id, space)
        return MemoryDetail(item, catalog_version, (revision,), (source,))

    async def update_memory(
        self,
        workspace_id: int,
        user_id: int,
        memory_id: int,
        *,
        expected_version: int,
        memory_type: str | None = None,
        name: str | None = None,
        description: str | None = None,
        body: str | None = None,
    ) -> MemoryDetail:
        """按乐观锁版本更新 Memory；无有效变化时不产生新修订。"""
        space, item = await self._get_locked_space_and_item(
            workspace_id,
            user_id,
            memory_id,
        )
        self._validate_expected_version(item, expected_version)

        next_memory_type = memory_type if memory_type is not None else item.memory_type
        next_name = name if name is not None else item.name
        next_description = description if description is not None else item.description
        next_body = body if body is not None else item.body
        self._validate_memory(
            item.memory_key,
            next_memory_type,
            next_name,
            next_description,
            next_body,
        )
        if (
            next_memory_type == item.memory_type
            and next_name == item.name
            and next_description == item.description
            and next_body == item.body
        ):
            return await self._build_detail(workspace_id, user_id, item, space=space)

        item = await self.repo.update_item(
            workspace_id,
            user_id,
            space,
            item,
            memory_type=next_memory_type,
            name=next_name,
            description=next_description,
            body=next_body,
        )
        revision = await self.repo.create_revision(
            workspace_id,
            user_id,
            space,
            item,
            actor_type="user",
            actor_id=str(user_id),
        )
        await self.repo.create_source(
            workspace_id,
            user_id,
            space=space,
            item=item,
            revision=revision,
            source_kind="explicit",
        )
        await self._validate_current_catalog(workspace_id, user_id)
        catalog_version = await self.repo.increment_catalog_version(workspace_id, user_id, space)
        revisions = await self.repo.list_revisions(workspace_id, user_id, item.id)
        sources = await self.repo.list_sources(workspace_id, user_id, item.id)
        return MemoryDetail(item, catalog_version, tuple(revisions), tuple(sources))

    async def delete_memory(
        self,
        workspace_id: int,
        user_id: int,
        memory_id: int,
        *,
        expected_version: int,
    ) -> MemoryDeleteResult:
        """按乐观锁版本软删除 Memory，并保留删除修订和来源。"""
        space, item = await self._get_locked_space_and_item(
            workspace_id,
            user_id,
            memory_id,
        )
        self._validate_expected_version(item, expected_version)
        await self.repo.soft_delete_item(workspace_id, user_id, space, item)
        revision = await self.repo.create_revision(
            workspace_id,
            user_id,
            space,
            item,
            actor_type="user",
            actor_id=str(user_id),
        )
        await self.repo.create_source(
            workspace_id,
            user_id,
            space=space,
            item=item,
            revision=revision,
            source_kind="explicit",
        )
        await self._validate_current_catalog(workspace_id, user_id)
        catalog_version = await self.repo.increment_catalog_version(workspace_id, user_id, space)
        return MemoryDeleteResult(item.id, item.version, catalog_version)

    async def get_rendered_catalog(
        self,
        workspace_id: int,
        user_id: int,
    ) -> RenderedMemoryCatalog:
        """读取并确定性渲染 Catalog。"""
        snapshot = await self.repo.get_catalog_snapshot(
            workspace_id,
            user_id,
            limit=MEMORY_CATALOG_MAX_ITEMS + 1,
        )
        content = self._render_catalog(snapshot.entries)
        self._validate_catalog_budget(snapshot.entries, content)
        return RenderedMemoryCatalog(snapshot, content, len(content.encode("utf-8")))

    async def export_memories(
        self,
        workspace_id: int,
        user_id: int,
    ) -> MemoryExportResult:
        """锁定 Space 并重验版本，拒绝返回跨 Catalog 的拼接视图。"""
        space = await self.repo.get_space(workspace_id, user_id, for_update=True)
        base_catalog_version = space.catalog_version if space is not None else None
        bundle = await self.repo.get_export_bundle(
            workspace_id,
            user_id,
            space=space,
        )
        if space is not None:
            current_catalog_version = await self.repo.get_catalog_version(
                workspace_id,
                user_id,
                space.id,
            )
            if current_catalog_version != base_catalog_version:
                raise AgentException.message(
                    "Memory 导出期间数据发生变化，请重试",
                    {
                        "base_catalog_version": base_catalog_version,
                        "current_catalog_version": current_catalog_version,
                    },
                    status_code=409,
                )
        return MemoryExportResult(datetime.now(UTC), bundle)

    async def issue_purge_confirmation(
        self,
        workspace_id: int,
        user_id: int,
    ) -> MemoryPurgeConfirmation:
        """签发不可跨用户、工作区、Space 或 Catalog 使用的删除确认。"""
        space = await self.repo.get_space(workspace_id, user_id)
        if space is None:
            raise AgentException.message("Memory 空间不存在", status_code=404)
        expires_at = datetime.now(UTC) + timedelta(seconds=_PURGE_CONFIRMATION_TTL_SECONDS)
        claims = {
            "v": _PURGE_TOKEN_VERSION,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "space_id": space.id,
            "catalog_version": space.catalog_version,
            "expires_at": int(expires_at.timestamp()),
        }
        return MemoryPurgeConfirmation(
            confirmation_token=self._sign_purge_claims(claims),
            expected_catalog_version=space.catalog_version,
            expires_at=expires_at,
        )

    async def purge_memories(
        self,
        workspace_id: int,
        user_id: int,
        *,
        confirmation_token: str,
        expected_catalog_version: int,
    ) -> MemoryPhysicalDeleteCounts:
        """校验双重确认后物理删除当前租户完整 Memory Space。"""
        space = await self.repo.get_space(workspace_id, user_id, for_update=True)
        if space is None:
            raise AgentException.message("Memory 空间不存在", status_code=404)
        if space.catalog_version != expected_catalog_version:
            raise AgentException.message(
                "Memory Catalog 版本冲突，请重新确认后再删除",
                {
                    "expected_catalog_version": expected_catalog_version,
                    "current_catalog_version": space.catalog_version,
                },
                status_code=409,
            )
        claims = self._verify_purge_token(confirmation_token)
        expected_claims = {
            "v": _PURGE_TOKEN_VERSION,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "space_id": space.id,
            "catalog_version": expected_catalog_version,
        }
        if any(claims.get(key) != value for key, value in expected_claims.items()):
            raise AgentException.message("彻底删除确认令牌无效", status_code=409)
        return await self.repo.purge_space(workspace_id, user_id, space)

    async def _get_locked_space_and_item(
        self,
        workspace_id: int,
        user_id: int,
        memory_id: int,
    ) -> tuple[MemorySpaceRecord, MemoryItemRecord]:
        """统一锁顺序，降低不同写操作之间的死锁风险。"""
        space = await self.repo.get_space(workspace_id, user_id, for_update=True)
        if space is None:
            raise AgentException.message("Memory 不存在", status_code=404)
        item = await self.repo.get_item(
            workspace_id,
            user_id,
            memory_id,
            for_update=True,
        )
        if item is None:
            raise AgentException.message("Memory 不存在", status_code=404)
        return space, item

    async def _build_detail(
        self,
        workspace_id: int,
        user_id: int,
        item: MemoryItemRecord,
        *,
        space: MemorySpaceRecord | None = None,
    ) -> MemoryDetail:
        revisions = await self.repo.list_revisions(workspace_id, user_id, item.id)
        sources = await self.repo.list_sources(workspace_id, user_id, item.id)
        current_space = space or await self.repo.get_space(workspace_id, user_id)
        catalog_version = current_space.catalog_version if current_space is not None else 0
        return MemoryDetail(item, catalog_version, tuple(revisions), tuple(sources))

    async def _validate_current_catalog(self, workspace_id: int, user_id: int) -> None:
        snapshot = await self.repo.get_catalog_snapshot(
            workspace_id,
            user_id,
            limit=MEMORY_CATALOG_MAX_ITEMS + 1,
        )
        content = self._render_catalog(snapshot.entries)
        self._validate_catalog_budget(snapshot.entries, content)

    @staticmethod
    def _render_catalog(entries: tuple[MemoryCatalogEntry, ...]) -> str:
        """按 memory_key、ID 的查询顺序生成一行一条的数据库版 MEMORY.md。
        标签使用 memory_key（受正则约束，仅含小写字母、数字、连字符和下划线），无需转义。
        """
        return "\n".join(
            f"- [{entry.memory_key}]"
            f"(memory:{entry.id}@{entry.version}) - "
            f"{MemoryService._escape_description(entry.description)}"
            for entry in entries
        )

    @staticmethod
    def _escape_description(value: str) -> str:
        return value.replace("\r", " ").replace("\n", " ")

    @staticmethod
    def _validate_catalog_budget(
        entries: tuple[MemoryCatalogEntry, ...],
        content: str,
    ) -> None:
        if len(entries) > MEMORY_CATALOG_MAX_ITEMS:
            raise AgentException.message(
                f"active Memory 数量不能超过 {MEMORY_CATALOG_MAX_ITEMS} 条",
                status_code=409,
            )
        byte_count = len(content.encode("utf-8"))
        if byte_count > MEMORY_CATALOG_MAX_BYTES:
            raise AgentException.message(
                f"Memory Catalog 不能超过 {MEMORY_CATALOG_MAX_BYTES} 字节",
                {"byte_count": byte_count},
                status_code=409,
            )

    @staticmethod
    def _validate_memory(
        memory_key: str,
        memory_type: str,
        name: str,
        description: str,
        body: str,
    ) -> None:
        if len(memory_key) > 160 or not _MEMORY_KEY_PATTERN.fullmatch(memory_key):
            raise AgentException.message(
                "memory_key 必须由小写字母、数字、连字符或下划线组成，最多 160 字符"
            )
        if memory_type not in MEMORY_TYPES:
            raise AgentException.message("Memory 类型无效")
        MemoryService._validate_single_line(name, "Memory 标题", 200)
        MemoryService._validate_single_line(description, "Memory 摘要", 500)
        if not body.strip():
            raise AgentException.message("Memory 正文不能为空")
        body_bytes = len(body.encode("utf-8"))
        if body_bytes > MEMORY_BODY_MAX_BYTES:
            raise AgentException.message(
                f"Memory 正文不能超过 {MEMORY_BODY_MAX_BYTES} 字节",
                {"byte_count": body_bytes},
            )

    @staticmethod
    def _validate_single_line(value: str, label: str, max_length: int) -> None:
        if not value.strip() or len(value) > max_length:
            raise AgentException.message(f"{label}长度必须在 1 到 {max_length} 字符之间")
        if "\n" in value or "\r" in value:
            raise AgentException.message(f"{label}不能包含换行符")

    @staticmethod
    def _validate_expected_version(item: MemoryItemRecord, expected_version: int) -> None:
        if item.version != expected_version:
            raise AgentException.message(
                "Memory 版本冲突，请刷新后重试",
                {"expected_version": expected_version, "current_version": item.version},
                status_code=409,
            )

    @staticmethod
    def _sign_purge_claims(claims: dict[str, int]) -> str:
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(
            settings.jwt_secret_key.encode("utf-8"),
            encoded_payload,
            hashlib.sha256,
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
        return f"{encoded_payload.decode('ascii')}.{encoded_signature.decode('ascii')}"

    @staticmethod
    def _verify_purge_token(token: str) -> dict[str, int]:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            payload_bytes = encoded_payload.encode("ascii")
            supplied_signature = MemoryService._decode_urlsafe_base64(encoded_signature)
            expected_signature = hmac.new(
                settings.jwt_secret_key.encode("utf-8"),
                payload_bytes,
                hashlib.sha256,
            ).digest()
            if not secrets.compare_digest(supplied_signature, expected_signature):
                raise ValueError("签名不匹配")
            decoded = MemoryService._decode_urlsafe_base64(encoded_payload)
            claims = json.loads(decoded.decode("utf-8"))
            required = {
                "v",
                "workspace_id",
                "user_id",
                "space_id",
                "catalog_version",
                "expires_at",
            }
            if not isinstance(claims, dict) or set(claims) != required:
                raise ValueError("声明字段不完整")
            if any(not isinstance(claims[key], int) for key in required):
                raise ValueError("声明类型无效")
            if claims["expires_at"] < int(datetime.now(UTC).timestamp()):
                raise ValueError("确认令牌已过期")
            return claims
        except (UnicodeError, ValueError, TypeError, binascii.Error) as exc:
            raise AgentException.message("彻底删除确认令牌无效", status_code=409) from exc

    @staticmethod
    def _decode_urlsafe_base64(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
