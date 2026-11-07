"""market 域持久化：只操作条目、版本、安装记录三张表。

写操作只 add / flush / refresh，不 commit，事务由请求边界（get_db）统一提交或回滚。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from database.base import Base
from market.models import MarketInstallRecord, MarketListingRecord, MarketVersionRecord

# 官方预置同步的咨询锁，即 pg_advisory_xact_lock(int4, int4) 的两个键：
# 第一个键是命名空间（ASCII "MRKT"），第二个键固定为 1。
# 与首个用户判定（"USER"）、部门树（"DEPT"）的命名空间不同，互不阻塞。
OFFICIAL_SYNC_LOCK_NAMESPACE = 0x4D524B54
OFFICIAL_SYNC_LOCK_ID = 1


class MarketRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def update[RecordT: Base](self, record: RecordT) -> RecordT:
        """把调用方改过的字段写入库，并读回 updated_at 等由库生成的值。"""
        await self.db.flush()
        await self.db.refresh(record)
        return record

    # ---------- 条目 ----------

    async def create_listing(
        self,
        *,
        kind: str,
        status: str,
        is_official: bool,
        official_key: str | None,
        publisher_workspace_id: int | None,
        source_agent_id: int | None,
        source_skill_id: int | None,
        created_by_user_id: int | None,
    ) -> MarketListingRecord:
        record = MarketListingRecord(
            kind=kind,
            status=status,
            is_official=is_official,
            official_key=official_key,
            publisher_workspace_id=publisher_workspace_id,
            source_agent_id=source_agent_id,
            source_skill_id=source_skill_id,
            created_by_user_id=created_by_user_id,
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def get_listing(self, listing_id: int) -> MarketListingRecord | None:
        stmt = select(MarketListingRecord).where(MarketListingRecord.id == listing_id)
        return (await self.db.scalars(stmt)).first()

    async def get_listing_for_update(self, listing_id: int) -> MarketListingRecord | None:
        """加行锁读取条目；populate_existing 保证等到锁之后读到的是库里的最新状态。"""
        stmt = (
            select(MarketListingRecord)
            .where(MarketListingRecord.id == listing_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def get_listing_by_source_for_update(
        self,
        kind: str,
        source_id: int,
    ) -> MarketListingRecord | None:
        """按源 Agent 或源 Skill 加行锁读取条目；条目还不存在时锁不住任何行。"""
        if kind == "agent":
            condition = MarketListingRecord.source_agent_id == source_id
        else:
            condition = MarketListingRecord.source_skill_id == source_id
        stmt = (
            select(MarketListingRecord)
            .where(condition)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def lock_official_sync(self) -> None:
        """加事务级咨询锁，让多个进程同时启动时的官方预置同步排队执行；锁在事务结束时自动释放。"""
        await self.db.execute(
            select(func.pg_advisory_xact_lock(OFFICIAL_SYNC_LOCK_NAMESPACE, OFFICIAL_SYNC_LOCK_ID))
        )

    async def get_official_listing_for_update(
        self,
        kind: str,
        official_key: str,
    ) -> MarketListingRecord | None:
        """按 (kind, official_key) 加行锁读取官方条目；populate_existing 保证读到库里的最新状态。"""
        stmt = (
            select(MarketListingRecord)
            .where(
                MarketListingRecord.kind == kind,
                MarketListingRecord.official_key == official_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def list_official_listings(self) -> list[MarketListingRecord]:
        """列出全部官方条目，按 ID 排序。"""
        stmt = (
            select(MarketListingRecord)
            .where(MarketListingRecord.is_official.is_(True))
            .order_by(MarketListingRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_listings_by_ids(self, listing_ids: list[int]) -> list[MarketListingRecord]:
        if not listing_ids:
            return []
        stmt = select(MarketListingRecord).where(MarketListingRecord.id.in_(listing_ids))
        return list(await self.db.scalars(stmt))

    async def list_listings_by_publisher(self, workspace_id: int) -> list[MarketListingRecord]:
        """列出工作区发布的全部条目，新建的在前。"""
        stmt = (
            select(MarketListingRecord)
            .where(MarketListingRecord.publisher_workspace_id == workspace_id)
            .order_by(MarketListingRecord.id.desc())
        )
        return list(await self.db.scalars(stmt))

    async def get_listing_by_publisher_and_source(
        self,
        workspace_id: int,
        kind: str,
        source_id: int,
    ) -> MarketListingRecord | None:
        """按发布工作区和源 Agent 或源 Skill 取条目；源对象的部分唯一索引保证最多一行。"""
        if kind == "agent":
            source_condition = MarketListingRecord.source_agent_id == source_id
        else:
            source_condition = MarketListingRecord.source_skill_id == source_id
        stmt = select(MarketListingRecord).where(
            MarketListingRecord.publisher_workspace_id == workspace_id,
            source_condition,
        )
        return (await self.db.scalars(stmt)).first()

    async def list_for_browse(
        self,
        *,
        kind: str | None,
        category: str | None,
        keyword: str | None,
        is_official: bool | None,
        installed: bool | None,
        installed_listing_ids: list[int],
        has_update: bool | None,
        updatable_listing_ids: list[int],
        limit: int,
        offset: int,
    ) -> tuple[list[MarketListingRecord], int]:
        """分页列出浏览页的条目，官方的在前，其余按更新时间倒序。

        installed=true 时返回 installed_listing_ids 里的全部条目，不论状态：条目下架后，
        装过它的工作区仍要能在「已安装」里看到它。其他筛选组合只返回已上架的条目。
        安装状态涉及 agent、skill 两个域的表，这里不拼它们的 SQL：
        由 Service 先算好「有效安装的条目 ID」和「有更新的条目 ID」，这里只做 IN / NOT IN。
        所有条件之间是 AND，总数和分页都按同一组条件在库里完成。
        """
        filters: list[Any] = [MarketListingRecord.is_deleted.is_(False)]
        if installed is not True:
            filters.append(MarketListingRecord.status == "listed")
        if kind is not None:
            filters.append(MarketListingRecord.kind == kind)
        if category is not None:
            filters.append(MarketListingRecord.category == category)
        if keyword:
            pattern = f"%{keyword.strip()}%"
            filters.append(
                or_(
                    MarketListingRecord.name.ilike(pattern),
                    MarketListingRecord.summary.ilike(pattern),
                )
            )
        if is_official is not None:
            filters.append(MarketListingRecord.is_official.is_(is_official))
        # 集合为空时，SQLAlchemy 把 IN 渲染成恒假、把 NOT IN 渲染成恒真，结果仍然正确。
        if installed is True:
            filters.append(MarketListingRecord.id.in_(installed_listing_ids))
        elif installed is False:
            filters.append(MarketListingRecord.id.not_in(installed_listing_ids))
        # 有更新的条目只会是已上架的（非 listed 不能升级），所以 has_update=true 也只命中 listed。
        if has_update is True:
            filters.append(MarketListingRecord.id.in_(updatable_listing_ids))
        elif has_update is False:
            filters.append(MarketListingRecord.id.not_in(updatable_listing_ids))
        total = await self.db.scalar(select(func.count(MarketListingRecord.id)).where(*filters))
        stmt = (
            select(MarketListingRecord)
            .where(*filters)
            .order_by(
                MarketListingRecord.is_official.desc(),
                MarketListingRecord.updated_at.desc(),
                MarketListingRecord.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(await self.db.scalars(stmt)), int(total or 0)

    # ---------- 版本 ----------

    async def create_version(
        self,
        *,
        listing_id: int,
        version_number: int,
        status: str,
        category: str,
        release_note: str | None,
        snapshot: dict[str, Any],
        snapshot_hash: str,
        submitted_by_user_id: int | None,
        reviewed_by_user_id: int | None,
        reviewed_at: datetime | None,
    ) -> MarketVersionRecord:
        record = MarketVersionRecord(
            listing_id=listing_id,
            version_number=version_number,
            status=status,
            category=category,
            release_note=release_note,
            snapshot=snapshot,
            snapshot_hash=snapshot_hash,
            submitted_by_user_id=submitted_by_user_id,
            reviewed_by_user_id=reviewed_by_user_id,
            reviewed_at=reviewed_at,
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def get_version(self, version_id: int) -> MarketVersionRecord | None:
        # populate_existing：审核和撤回在锁住条目之后会再读一次版本，要读到库里的最新状态。
        stmt = (
            select(MarketVersionRecord)
            .where(MarketVersionRecord.id == version_id)
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def get_version_by_number(
        self,
        listing_id: int,
        version_number: int,
    ) -> MarketVersionRecord | None:
        stmt = select(MarketVersionRecord).where(
            MarketVersionRecord.listing_id == listing_id,
            MarketVersionRecord.version_number == version_number,
        )
        return (await self.db.scalars(stmt)).first()

    async def get_snapshot_hash(self, listing_id: int, version_number: int) -> str | None:
        """只取版本的快照哈希，不加载可能很大的快照本身。"""
        stmt = select(MarketVersionRecord.snapshot_hash).where(
            MarketVersionRecord.listing_id == listing_id,
            MarketVersionRecord.version_number == version_number,
            MarketVersionRecord.is_deleted.is_(False),
        )
        return await self.db.scalar(stmt)

    async def has_pending_version(self, listing_id: int) -> bool:
        stmt = select(MarketVersionRecord.id).where(
            MarketVersionRecord.listing_id == listing_id,
            MarketVersionRecord.status == "pending",
            MarketVersionRecord.is_deleted.is_(False),
        )
        pending_id = await self.db.scalar(stmt.limit(1))
        return pending_id is not None

    async def get_max_version_number(self, listing_id: int) -> int:
        """条目现有的最大版本号，没有版本时为 0。

        软删除的版本也算：(listing_id, version_number) 的唯一约束不区分是否删除。
        """
        stmt = (
            select(func.max(MarketVersionRecord.version_number))
            .where(MarketVersionRecord.listing_id == listing_id)
            .execution_options(include_deleted=True)
        )
        max_number = await self.db.scalar(stmt)
        if max_number is None:
            return 0
        return max_number

    async def list_versions_by_listing_ids(
        self,
        listing_ids: list[int],
    ) -> list[MarketVersionRecord]:
        """列出这些条目的全部版本，版本号大的在前；不加载快照，快照可能很大。"""
        if not listing_ids:
            return []
        stmt = (
            select(MarketVersionRecord)
            .options(defer(MarketVersionRecord.snapshot))
            .where(MarketVersionRecord.listing_id.in_(listing_ids))
            .order_by(MarketVersionRecord.listing_id, MarketVersionRecord.version_number.desc())
        )
        return list(await self.db.scalars(stmt))

    async def list_versions_by_status(
        self,
        status: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[MarketVersionRecord], int]:
        """按审核状态分页列出版本；待审的先提交先审，其他状态最近的在前。"""
        filters = [
            MarketVersionRecord.status == status,
            MarketVersionRecord.is_deleted.is_(False),
        ]
        total = await self.db.scalar(select(func.count(MarketVersionRecord.id)).where(*filters))
        if status == "pending":
            order = MarketVersionRecord.id.asc()
        else:
            order = MarketVersionRecord.id.desc()
        stmt = (
            select(MarketVersionRecord).where(*filters).order_by(order).limit(limit).offset(offset)
        )
        return list(await self.db.scalars(stmt)), int(total or 0)

    # ---------- 安装记录 ----------

    async def create_install(
        self,
        *,
        listing_id: int,
        workspace_id: int,
        installed_version_number: int,
        target_agent_id: int | None,
        target_skill_id: int | None,
        managed_skill_ids: list[int],
        installed_by_user_id: int,
    ) -> MarketInstallRecord:
        record = MarketInstallRecord(
            listing_id=listing_id,
            workspace_id=workspace_id,
            installed_version_number=installed_version_number,
            target_agent_id=target_agent_id,
            target_skill_id=target_skill_id,
            managed_skill_ids=managed_skill_ids,
            installed_by_user_id=installed_by_user_id,
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def get_install(self, listing_id: int, workspace_id: int) -> MarketInstallRecord | None:
        """取工作区在这个条目上的安装记录；部分唯一索引保证未删除的最多一行。"""
        stmt = select(MarketInstallRecord).where(
            MarketInstallRecord.listing_id == listing_id,
            MarketInstallRecord.workspace_id == workspace_id,
        )
        return (await self.db.scalars(stmt)).first()

    async def get_install_for_update(
        self,
        listing_id: int,
        workspace_id: int,
    ) -> MarketInstallRecord | None:
        stmt = (
            select(MarketInstallRecord)
            .where(
                MarketInstallRecord.listing_id == listing_id,
                MarketInstallRecord.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def list_installs_linked_to_skill_for_update(
        self,
        workspace_id: int,
        skill_id: int,
    ) -> list[MarketInstallRecord]:
        """加行锁读取工作区里指向这个 Skill 的全部安装记录，已软删除的也算。

        指向包括 target_skill_id 等于它，或 managed_skill_ids 含有它（JSONB 的 @> 包含判断）。
        按 ID 顺序加锁，多个请求同时锁多行时顺序一致；
        populate_existing 保证等到锁之后读到的是库里的最新值。
        """
        stmt = (
            select(MarketInstallRecord)
            .where(
                MarketInstallRecord.workspace_id == workspace_id,
                or_(
                    MarketInstallRecord.target_skill_id == skill_id,
                    MarketInstallRecord.managed_skill_ids.contains([skill_id]),
                ),
            )
            .order_by(MarketInstallRecord.id)
            .with_for_update()
            .execution_options(include_deleted=True, populate_existing=True)
        )
        return list(await self.db.scalars(stmt))

    async def list_installs_of_workspace(
        self,
        workspace_id: int,
        listing_ids: list[int],
    ) -> list[MarketInstallRecord]:
        if not listing_ids:
            return []
        stmt = select(MarketInstallRecord).where(
            MarketInstallRecord.workspace_id == workspace_id,
            MarketInstallRecord.listing_id.in_(listing_ids),
        )
        return list(await self.db.scalars(stmt))

    async def list_all_installs_of_workspace(self, workspace_id: int) -> list[MarketInstallRecord]:
        """列出工作区的全部安装记录，按 ID 排序；每个条目最多一条，数量不大。"""
        stmt = (
            select(MarketInstallRecord)
            .where(MarketInstallRecord.workspace_id == workspace_id)
            .order_by(MarketInstallRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_install_targets_by_listing_ids(
        self,
        listing_ids: list[int],
    ) -> list[Row[tuple[int, int, int | None, int | None]]]:
        """列出这些条目在所有工作区的未软删除安装记录，供统计有效安装数。

        只取 listing_id、workspace_id、target_agent_id、target_skill_id 四列，
        不建 ORM 对象，避免和会话里已加载的完整安装记录互相影响。
        """
        if not listing_ids:
            return []
        stmt = select(
            MarketInstallRecord.listing_id,
            MarketInstallRecord.workspace_id,
            MarketInstallRecord.target_agent_id,
            MarketInstallRecord.target_skill_id,
        ).where(
            MarketInstallRecord.listing_id.in_(listing_ids),
            MarketInstallRecord.is_deleted.is_(False),
        )
        result = await self.db.execute(stmt)
        return list(result.all())
