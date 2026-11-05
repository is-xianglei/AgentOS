"""market 域业务：社区上架与审核、市场浏览、安装与升级。

安装和升级共用一套只读判定（_build_plan）：详情页的预览直接展示判定结果；
真正执行时先用同一个判定拒绝冲突，再按判定结果写库，保证预览看到的就是实际会发生的。
所有写操作都在请求事务（get_db）里完成，本层和 Repository 都不 commit，任何一步失败整体回滚。
"""

import fnmatch
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.service import AgentService
from core.errors import AgentException
from market.models import MarketInstallRecord, MarketListingRecord, MarketVersionRecord
from market.repository import MarketRepository
from market.schemas import (
    MarketBrowseItem,
    MarketBrowsePage,
    MarketDroppedTool,
    MarketInstallPreview,
    MarketInstallReport,
    MarketInstallResponse,
    MarketInstallResult,
    MarketListingDetailResponse,
    MarketListingResponse,
    MarketMyListingItem,
    MarketReviewItem,
    MarketReviewPage,
    MarketSkillOutcome,
    MarketVersionDetailResponse,
    MarketVersionResponse,
)
from skill.parser import parse_skill_md
from skill.service import SkillService
from tools.service import ToolCatalogService
from workspace.service import WorkspaceService

logger = logging.getLogger(__name__)

# 分类写在代码里：key 存库，中文名只用于展示，字典顺序就是展示顺序。
MARKET_CATEGORIES: dict[str, str] = {
    "general": "通用",
    "coding": "编程开发",
    "data": "数据分析",
    "writing": "写作创作",
    "office": "办公效率",
    "education": "学习教育",
    "other": "其他",
}

# 快照序列化后的大小上限，超过就不允许上架。
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024

# 疑似凭据的文件名：Skill 附属文件命中任意一个就拒绝上架，防止把密钥公开到市场。
# 只看路径最后一段的文件名，不区分大小写。
CREDENTIAL_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "secrets.json",
    "credentials.json",
    "*.pem",
    "id_rsa",
    "id_ed25519",
)

# 官方条目没有发布工作区，发布方固定显示这个名字。
OFFICIAL_PUBLISHER_NAME = "官方"

# 工具被丢弃时写进报告的中文说明，键与 ToolCatalogService.resolve_tool_keys 给出的原因一致。
TOOL_DROP_MESSAGES = {
    "not_found": "工具不存在",
    "disabled": "工具已停用",
    "not_bindable": "自建 Agent 不允许绑定该工具",
}

# 上架、下架、安装、升级的权限口径与 Agent、Skill 的写权限一致。
_WRITE_ROLES = {"owner", "admin"}


# ---------- 快照与分类的校验（纯函数） ----------


def dump_snapshot(snapshot: dict[str, Any]) -> bytes:
    """把快照规范化为 JSON 字节：键排序、紧凑分隔符、中文不转义。大小和哈希都按它计算。"""
    text = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def compute_snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(dump_snapshot(snapshot)).hexdigest()


def check_snapshot_size(snapshot: dict[str, Any]) -> None:
    size = len(dump_snapshot(snapshot))
    if size > MAX_SNAPSHOT_BYTES:
        raise AgentException.message(
            f"快照大小超过上限 32MB（当前 {size} 字节）",
            {"size": size, "max_size": MAX_SNAPSHOT_BYTES},
            status_code=400,
        )


def is_credential_file(path: str) -> bool:
    file_name = path.rsplit("/", 1)[-1].lower()
    for pattern in CREDENTIAL_FILE_PATTERNS:
        if fnmatch.fnmatchcase(file_name, pattern):
            return True
    return False


def check_credential_files(snapshot: dict[str, Any]) -> None:
    """快照里任一 Skill 带有疑似凭据的文件就拒绝，错误里列出全部命中的文件路径。"""
    # Skill 条目的快照只有一个 skill，Agent 条目的快照带一组 skills。
    skills: list[dict[str, Any]] = []
    if "skill" in snapshot:
        skills.append(snapshot["skill"])
    else:
        skills.extend(snapshot["skills"])

    hit_paths: list[str] = []
    for skill in skills:
        for path in skill["extra_files"]:
            if is_credential_file(path):
                hit_paths.append(f"{skill['name']}/{path}")
    if hit_paths:
        raise AgentException.message(
            f"Skill 中含有疑似凭据的文件，不能上架：{'、'.join(hit_paths)}",
            {"paths": hit_paths},
            status_code=400,
        )


def check_category(category: str) -> None:
    if category not in MARKET_CATEGORIES:
        raise AgentException.message(
            f"未知的分类: {category}",
            {"category": category, "allowed": list(MARKET_CATEGORIES)},
            status_code=400,
        )


def snapshot_display_fields(kind: str, snapshot: dict[str, Any]) -> tuple[str, str | None]:
    """从快照取条目展示用的名称和简介：Agent 取 description，Skill 取 frontmatter 的 description。"""
    if kind == "agent":
        agent = snapshot["agent"]
        return agent["name"], agent["description"]
    skill = snapshot["skill"]
    frontmatter = parse_skill_md(skill["content"]).frontmatter
    return skill["name"], frontmatter["description"]


# ---------- 状态流转的合法性检查（纯函数，非法时报 409） ----------


def check_version_pending(version: MarketVersionRecord) -> None:
    """撤回、通过、驳回都只能针对待审版本。"""
    if version.status != "pending":
        raise AgentException.message(
            "该版本不是待审核状态",
            {"status": version.status},
            status_code=409,
        )


def check_can_delist(listing: MarketListingRecord) -> None:
    if listing.is_official:
        raise AgentException.message("官方内容不支持下架", status_code=409)
    if listing.status != "listed":
        raise AgentException.message(
            "只有已上架的条目才能下架",
            {"status": listing.status},
            status_code=409,
        )


def check_can_relist(listing: MarketListingRecord) -> None:
    if listing.is_official:
        raise AgentException.message("官方内容不支持重新上架", status_code=409)
    if listing.status == "removed":
        raise AgentException.message(
            "条目已被平台下架，请提交新版本，审核通过后会重新上架",
            {"status": listing.status},
            status_code=409,
        )
    if listing.status != "delisted":
        raise AgentException.message(
            "只有作者下架的条目才能重新上架",
            {"status": listing.status},
            status_code=409,
        )


def check_can_remove(listing: MarketListingRecord) -> None:
    if listing.is_official:
        raise AgentException.message("官方内容请从代码中移除", status_code=409)
    if listing.status != "listed" and listing.status != "delisted":
        raise AgentException.message(
            "只有已上架或作者下架的条目才能强制下架",
            {"status": listing.status},
            status_code=409,
        )


def check_listing_installable(listing: MarketListingRecord) -> None:
    """下架后不只是从列表隐藏，安装和升级也一并拒绝。"""
    if listing.status != "listed":
        raise AgentException.message(
            "条目未上架，不能安装或升级",
            {"status": listing.status},
            status_code=409,
        )


# ---------- 安装与升级的判定结果 ----------


@dataclass
class SkillStep:
    """安装或升级时对一个 Skill 的处理。action 的取值见 schemas.SkillOutcomeAction。"""

    name: str
    action: str
    skill_id: int | None
    # 要写入的 Skill 快照；只解绑的托管 Skill 没有。
    snapshot: dict[str, Any] | None = None


@dataclass
class InstallPlan:
    """安装或升级会发生什么。blockers 非空时不能执行，执行时报第一条原因。"""

    mode: str
    target_version_number: int
    installed_version_number: int | None = None
    blockers: list[str] = field(default_factory=list)
    agent_name_conflict: bool = False
    tool_ids: list[int] = field(default_factory=list)
    bound_tool_keys: list[str] = field(default_factory=list)
    dropped_tools: dict[str, str] = field(default_factory=dict)
    skill_steps: list[SkillStep] = field(default_factory=list)


class MarketService:
    """市场的业务入口。跨域只调用 agent、skill、tools、workspace 各域 Service 的公开方法。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = MarketRepository(db)
        self.workspace_service = WorkspaceService(db)
        self.agent_service = AgentService(db)
        self.skill_service = SkillService(db)
        self.tool_catalog_service = ToolCatalogService(db)

    # ---------- 社区上架 ----------

    async def submit(
        self,
        workspace_id: int,
        actor_user_id: int,
        *,
        kind: str,
        source_id: int,
        category: str,
        release_note: str | None,
    ) -> MarketVersionRecord:
        """提交新条目或新版本，生成一个待审版本。

        快照在这一刻生成：审核人看到的就是最终上架的内容，作者之后再改源对象不影响这个版本。
        """
        await self._require_write_access(workspace_id, actor_user_id)

        # 1. 生成快照；源对象不属于当前工作区或已删除时，由对应的域返回 404。
        if kind == "agent":
            snapshot = await self.agent_service.build_snapshot(workspace_id, source_id)
        else:
            skill_snapshot = await self.skill_service.build_snapshot(workspace_id, source_id)
            snapshot = {"skill": skill_snapshot}

        # 2. 依次校验快照大小、凭据文件和分类。
        check_snapshot_size(snapshot)
        check_credential_files(snapshot)
        check_category(category)
        snapshot_hash = compute_snapshot_hash(snapshot)

        # 3. 锁住源对象对应的条目，同一条目的提交、撤回和审核由这把行锁串行化。
        listing = await self.repo.get_listing_by_source_for_update(kind, source_id)
        if listing is None:
            listing = await self._create_community_listing(
                workspace_id, actor_user_id, kind, source_id
            )

        # 4. 每个条目最多只有一个待审版本。
        if await self.repo.has_pending_version(listing.id):
            raise _pending_version_exists()

        # 5. 内容与当前上架版本完全相同，没有必要再审一次。
        if listing.current_version_number is not None:
            current_hash = await self.repo.get_snapshot_hash(
                listing.id, listing.current_version_number
            )
            if current_hash == snapshot_hash:
                raise AgentException.message("内容与当前上架版本相同", status_code=409)

        # 6. 新建待审版本，版本号取现有最大版本号加 1。
        # 行锁之外还有「每个条目最多一个待审版本」的部分唯一索引兜底，撞上时同样按 409 处理。
        version_number = await self.repo.get_max_version_number(listing.id) + 1
        try:
            version = await self.repo.create_version(
                listing_id=listing.id,
                version_number=version_number,
                status="pending",
                category=category,
                release_note=release_note,
                snapshot=snapshot,
                snapshot_hash=snapshot_hash,
                submitted_by_user_id=actor_user_id,
                reviewed_by_user_id=None,
                reviewed_at=None,
            )
        except IntegrityError as exc:
            raise _pending_version_exists() from exc

        logger.info(
            "市场版本已提交审核",
            extra={
                "listing_id": listing.id,
                "version_id": version.id,
                "version_number": version.version_number,
                "workspace_id": workspace_id,
                "user_id": actor_user_id,
            },
        )
        return version

    async def withdraw_version(
        self,
        workspace_id: int,
        actor_user_id: int,
        version_id: int,
    ) -> MarketVersionRecord:
        """撤回本工作区的待审版本。"""
        await self._require_write_access(workspace_id, actor_user_id)
        listing, version = await self._lock_listing_of_version(version_id)
        if listing.publisher_workspace_id != workspace_id:
            raise _version_not_found()
        check_version_pending(version)
        version.status = "withdrawn"
        return await self.repo.update(version)

    async def delist_listing(
        self,
        workspace_id: int,
        actor_user_id: int,
        listing_id: int,
    ) -> MarketListingRecord:
        """作者下架：已上架的条目改为 delisted，之后详情、安装和升级都会被拒绝。"""
        await self._require_write_access(workspace_id, actor_user_id)
        listing = await self._lock_listing_for_author(workspace_id, listing_id)
        check_can_delist(listing)
        listing.status = "delisted"
        listing.status_reason = None
        return await self.repo.update(listing)

    async def relist_listing(
        self,
        workspace_id: int,
        actor_user_id: int,
        listing_id: int,
    ) -> MarketListingRecord:
        """作者重新上架：只能从 delisted 恢复；被平台下架的只能提交新版本重新审核。"""
        await self._require_write_access(workspace_id, actor_user_id)
        listing = await self._lock_listing_for_author(workspace_id, listing_id)
        check_can_relist(listing)
        listing.status = "listed"
        listing.status_reason = None
        return await self.repo.update(listing)

    async def list_my_listings(
        self,
        workspace_id: int,
        actor_user_id: int,
    ) -> list[MarketMyListingItem]:
        """当前工作区发布的全部条目，每个条目附带全部版本（含审核意见和驳回理由）。"""
        await self._require_write_access(workspace_id, actor_user_id)
        listings = await self.repo.list_listings_by_publisher(workspace_id)
        listing_ids: list[int] = []
        for listing in listings:
            listing_ids.append(listing.id)
        versions = await self.repo.list_versions_by_listing_ids(listing_ids)

        versions_by_listing: dict[int, list[MarketVersionResponse]] = {}
        for version in versions:
            if version.listing_id not in versions_by_listing:
                versions_by_listing[version.listing_id] = []
            versions_by_listing[version.listing_id].append(
                MarketVersionResponse.model_validate(version)
            )

        items: list[MarketMyListingItem] = []
        for listing in listings:
            items.append(
                MarketMyListingItem(
                    listing=MarketListingResponse.model_validate(listing),
                    versions=versions_by_listing.get(listing.id, []),
                )
            )
        return items

    # ---------- 平台管理员审核（权限由 API 层的 get_current_platform_admin 把关） ----------

    async def list_versions_for_review(
        self,
        *,
        status: str,
        limit: int,
        offset: int,
    ) -> MarketReviewPage:
        """按审核状态分页列出版本，带快照全文、所属条目和发布工作区名称。"""
        versions, total = await self.repo.list_versions_by_status(
            status, limit=limit, offset=offset
        )
        listing_ids: list[int] = []
        for version in versions:
            listing_ids.append(version.listing_id)
        listings = await self.repo.list_listings_by_ids(listing_ids)
        publisher_names = await self._publisher_names(listings)
        listings_by_id: dict[int, MarketListingRecord] = {}
        for listing in listings:
            listings_by_id[listing.id] = listing

        items: list[MarketReviewItem] = []
        for version in versions:
            listing = listings_by_id[version.listing_id]
            items.append(
                MarketReviewItem(
                    version=MarketVersionDetailResponse.model_validate(version),
                    listing=MarketListingResponse.model_validate(listing),
                    publisher_name=publisher_names[listing.id],
                )
            )
        return MarketReviewPage(items=items, total=total, limit=limit, offset=offset)

    async def approve_version(
        self,
        reviewer_user_id: int,
        version_id: int,
        comment: str | None,
    ) -> MarketVersionRecord:
        """审核通过：版本成为条目的当前版本，条目上架（原来是未上架、作者下架还是平台下架都一样）。"""
        listing, version = await self._lock_listing_of_version(version_id)
        check_version_pending(version)
        version.status = "approved"
        version.reviewed_by_user_id = reviewer_user_id
        version.reviewed_at = datetime.now(UTC)
        version.review_comment = comment
        version = await self.repo.update(version)
        await self.publish_version(listing, version)
        logger.info(
            "市场版本审核通过",
            extra={
                "listing_id": listing.id,
                "version_id": version.id,
                "reviewer_user_id": reviewer_user_id,
            },
        )
        return version

    async def reject_version(
        self,
        reviewer_user_id: int,
        version_id: int,
        reason: str,
    ) -> MarketVersionRecord:
        """驳回待审版本，理由写入审核意见；条目状态不变。"""
        _listing, version = await self._lock_listing_of_version(version_id)
        check_version_pending(version)
        version.status = "rejected"
        version.reviewed_by_user_id = reviewer_user_id
        version.reviewed_at = datetime.now(UTC)
        version.review_comment = reason
        version = await self.repo.update(version)
        logger.info(
            "市场版本已驳回",
            extra={"version_id": version.id, "reviewer_user_id": reviewer_user_id},
        )
        return version

    async def remove_listing(
        self,
        reviewer_user_id: int,
        listing_id: int,
        reason: str,
    ) -> MarketListingRecord:
        """平台强制下架：条目改为 removed，作者不能自行重新上架，只能提交新版本重新审核。"""
        listing = await self.repo.get_listing_for_update(listing_id)
        if listing is None:
            raise _listing_not_found()
        check_can_remove(listing)
        listing.status = "removed"
        listing.status_reason = reason
        listing = await self.repo.update(listing)
        logger.info(
            "市场条目已被平台下架",
            extra={"listing_id": listing.id, "reviewer_user_id": reviewer_user_id},
        )
        return listing

    async def publish_version(
        self,
        listing: MarketListingRecord,
        version: MarketVersionRecord,
    ) -> MarketListingRecord:
        """把一个已通过的版本设为条目的当前版本，同步展示字段，并把条目改为已上架。

        审核通过和官方预置同步都用它；调用方负责先锁住条目。
        """
        name, summary = snapshot_display_fields(listing.kind, version.snapshot)
        listing.current_version_number = version.version_number
        listing.name = name
        listing.summary = summary
        listing.category = version.category
        listing.status = "listed"
        listing.status_reason = None
        return await self.repo.update(listing)

    # ---------- 浏览 ----------

    async def list_listings(
        self,
        workspace_id: int,
        actor_user_id: int,
        *,
        kind: str | None,
        category: str | None,
        keyword: str | None,
        limit: int,
        offset: int,
    ) -> MarketBrowsePage:
        """分页列出已上架的条目，并带上当前工作区的安装状态。"""
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        listings, total = await self.repo.list_listed(
            kind=kind,
            category=category,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )
        items = await self._build_browse_items(workspace_id, listings)
        return MarketBrowsePage(items=items, total=total, limit=limit, offset=offset)

    async def get_listing_detail(
        self,
        workspace_id: int,
        actor_user_id: int,
        listing_id: int,
        *,
        is_platform_admin: bool,
    ) -> MarketListingDetailResponse:
        """条目详情：当前版本的完整快照，加上当前工作区的安装或升级预览。"""
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        listing = await self.repo.get_listing(listing_id)
        if listing is None:
            raise _listing_not_found()
        # 不是 listed 的条目只有发布工作区的成员和平台管理员能看，其他人按不存在处理。
        if listing.status != "listed":
            is_publisher = listing.publisher_workspace_id == workspace_id
            if not is_publisher and not is_platform_admin:
                raise _listing_not_found()

        browse_items = await self._build_browse_items(workspace_id, [listing])
        detail = MarketListingDetailResponse(
            listing=browse_items[0],
            status=listing.status,
            status_reason=listing.status_reason,
        )
        # 还没有审核通过的版本时，没有快照可看。
        if listing.current_version_number is None:
            return detail
        version = await self.repo.get_version_by_number(listing.id, listing.current_version_number)
        if version is None:
            return detail
        detail.current_version = MarketVersionDetailResponse.model_validate(version)

        # 不是 listed 的条目不能安装也不能升级，没有预览。
        if listing.status != "listed":
            return detail
        install = await self.repo.get_install(listing.id, workspace_id)
        active_install = await self._get_active_install(workspace_id, install)
        plan = await self._build_plan(
            workspace_id, actor_user_id, listing, version, active_install
        )
        detail.install_preview = _plan_preview(plan)
        return detail

    # ---------- 安装与升级 ----------

    async def install_listing(
        self,
        workspace_id: int,
        actor_user_id: int,
        listing_id: int,
    ) -> MarketInstallResult:
        """把条目的当前版本安装到当前工作区。"""
        await self._require_write_access(workspace_id, actor_user_id)
        listing, version = await self._require_installable(listing_id)

        # 锁住本工作区在这个条目上的安装记录，串行化同一工作区对同一条目的并发安装和升级。
        install = await self.repo.get_install_for_update(listing.id, workspace_id)
        active_install = await self._get_active_install(workspace_id, install)
        if active_install is not None:
            raise AgentException.message("已安装，请使用升级", status_code=409)

        plan = await self._build_plan(workspace_id, actor_user_id, listing, version, None)
        if plan.blockers:
            raise AgentException.message(
                plan.blockers[0],
                {"blockers": plan.blockers},
                status_code=409,
            )

        target_agent_id = None
        target_skill_id = None
        managed_skill_ids: list[int] = []
        if listing.kind == "skill":
            target_skill_id = await self._install_skill(workspace_id, actor_user_id, plan)
        else:
            # 原来装出来的 Agent 已被删除、复用旧记录重装时，旧记录里的托管 Skill 要传下去。
            previous_managed_ids: list[int] = []
            if install is not None:
                previous_managed_ids = list(install.managed_skill_ids)
            target_agent_id, managed_skill_ids = await self._install_agent(
                workspace_id, actor_user_id, version.snapshot, plan, previous_managed_ids
            )

        if install is None:
            # 并发安装时安装记录可能被抢先建好，由部分唯一索引兜底并转成 409。
            try:
                install = await self.repo.create_install(
                    listing_id=listing.id,
                    workspace_id=workspace_id,
                    installed_version_number=version.version_number,
                    target_agent_id=target_agent_id,
                    target_skill_id=target_skill_id,
                    managed_skill_ids=managed_skill_ids,
                    installed_by_user_id=actor_user_id,
                )
            except IntegrityError as exc:
                raise AgentException.message("已安装，请使用升级", status_code=409) from exc
        else:
            # 原来装出来的对象已被删除，这条记录视为未安装，复用这一行重新记录。
            install.installed_version_number = version.version_number
            install.target_agent_id = target_agent_id
            install.target_skill_id = target_skill_id
            install.managed_skill_ids = managed_skill_ids
            install.installed_by_user_id = actor_user_id
            install = await self.repo.update(install)

        logger.info(
            "市场条目安装完成",
            extra={
                "listing_id": listing.id,
                "install_id": install.id,
                "workspace_id": workspace_id,
            },
        )
        return MarketInstallResult(
            install=MarketInstallResponse.model_validate(install),
            report=_plan_report(plan),
        )

    async def upgrade_listing(
        self,
        workspace_id: int,
        actor_user_id: int,
        listing_id: int,
    ) -> MarketInstallResult:
        """把已安装的条目原地升级到当前版本：直接覆盖，不检测本地改动。"""
        await self._require_write_access(workspace_id, actor_user_id)
        listing, version = await self._require_installable(listing_id)

        install = await self.repo.get_install_for_update(listing.id, workspace_id)
        active_install = await self._get_active_install(workspace_id, install)
        if active_install is None:
            raise AgentException.message("尚未安装该条目，请先安装", status_code=409)

        plan = await self._build_plan(
            workspace_id, actor_user_id, listing, version, active_install
        )
        if plan.blockers:
            raise AgentException.message(
                plan.blockers[0],
                {"blockers": plan.blockers},
                status_code=409,
            )

        managed_skill_ids: list[int] = []
        if listing.kind == "skill":
            # Skill 名称创建后不能改，同一个条目的 Skill 名称不会变，直接覆盖内容。
            step = plan.skill_steps[0]
            await self.skill_service.overwrite_from_snapshot(
                workspace_id, step.skill_id, actor_user_id, step.snapshot
            )
        else:
            managed_skill_ids = await self._upgrade_agent(
                workspace_id, actor_user_id, active_install.target_agent_id, version.snapshot, plan
            )

        active_install.installed_version_number = version.version_number
        active_install.managed_skill_ids = managed_skill_ids
        install = await self.repo.update(active_install)
        logger.info(
            "市场条目升级完成",
            extra={
                "listing_id": listing.id,
                "install_id": install.id,
                "workspace_id": workspace_id,
            },
        )
        return MarketInstallResult(
            install=MarketInstallResponse.model_validate(install),
            report=_plan_report(plan),
        )

    async def _build_plan(
        self,
        workspace_id: int,
        actor_user_id: int,
        listing: MarketListingRecord,
        version: MarketVersionRecord,
        active_install: MarketInstallRecord | None,
    ) -> InstallPlan:
        """判定安装或升级会发生什么，只读不写库。

        详情页的预览和真正的安装、升级都调用它，保证预览看到的就是实际会发生的。
        active_install 为空表示本工作区没有有效安装，按安装判定；否则按升级判定。
        """
        plan = InstallPlan(mode="install", target_version_number=version.version_number)
        if active_install is not None:
            plan.mode = "upgrade"
            plan.installed_version_number = active_install.installed_version_number
            if active_install.installed_version_number >= version.version_number:
                plan.blockers.append("已是最新版本")
                return plan

        if listing.kind == "skill":
            await self._plan_skill_listing(workspace_id, version.snapshot, active_install, plan)
        else:
            await self._plan_agent_listing(
                workspace_id, actor_user_id, version.snapshot, active_install, plan
            )
        return plan

    async def _plan_skill_listing(
        self,
        workspace_id: int,
        snapshot: dict[str, Any],
        active_install: MarketInstallRecord | None,
        plan: InstallPlan,
    ) -> None:
        skill_snapshot = snapshot["skill"]
        # 升级：用新快照覆盖已安装的 Skill。
        if active_install is not None:
            plan.skill_steps.append(
                SkillStep(
                    name=skill_snapshot["name"],
                    action="overwrite",
                    skill_id=active_install.target_skill_id,
                    snapshot=skill_snapshot,
                )
            )
            return

        # 安装：直接安装 Skill 条目时，本工作区只要有同名未删除的 Skill 就拒绝，内容相同也不复用。
        decision = await self.skill_service.preview_install_from_snapshot(
            workspace_id, skill_snapshot
        )
        action = decision.action
        if action == "reuse" or action == "conflict":
            action = "conflict"
            plan.blockers.append(f"本工作区已有同名 Skill: {decision.name}")
        plan.skill_steps.append(
            SkillStep(
                name=decision.name,
                action=action,
                skill_id=decision.skill_id,
                snapshot=skill_snapshot,
            )
        )

    async def _plan_agent_listing(
        self,
        workspace_id: int,
        actor_user_id: int,
        snapshot: dict[str, Any],
        active_install: MarketInstallRecord | None,
        plan: InstallPlan,
    ) -> None:
        agent_snapshot = snapshot["agent"]

        # 1. 安装时 Agent 不能同名；升级保留本地名称，不用检查。
        if active_install is None:
            agent_name = agent_snapshot["name"]
            if await self.agent_service.is_name_taken(workspace_id, agent_name):
                plan.agent_name_conflict = True
                plan.blockers.append(f"本工作区已有同名 Agent: {agent_name}")

        # 2. 工具按 tool_key 解析，不可用的丢弃并记下原因，口径与绑定校验一致。
        resolution = await self.tool_catalog_service.resolve_tool_keys(
            agent_snapshot["tool_keys"],
            profile="user_agent",
        )
        for tool_key, tool_id in resolution.tool_ids.items():
            plan.bound_tool_keys.append(tool_key)
            plan.tool_ids.append(tool_id)
        for tool_key, reason in resolution.unavailable.items():
            plan.dropped_tools[tool_key] = reason

        # 3. Skill：升级时，托管 Skill 里仍存在的那些直接用新内容覆盖；
        #    其余按安装规则新建、恢复，或在内容相同时复用；同名但内容不同就拒绝整个安装或升级。
        live_managed_ids: set[int] = set()
        if active_install is not None:
            live_managed_ids = await self.skill_service.filter_existing_ids(
                workspace_id, list(active_install.managed_skill_ids)
            )
        overwritten_ids: set[int] = set()
        for skill_snapshot in snapshot["skills"]:
            decision = await self.skill_service.preview_install_from_snapshot(
                workspace_id, skill_snapshot
            )
            action = decision.action
            # 同名记录就是仍存在的托管 Skill（判定结果只会是复用或冲突），升级时直接覆盖。
            if decision.skill_id is not None and decision.skill_id in live_managed_ids:
                action = "overwrite"
                overwritten_ids.add(decision.skill_id)
            if action == "conflict":
                plan.blockers.append(f"本工作区已有同名但内容不同的 Skill: {decision.name}")
            plan.skill_steps.append(
                SkillStep(
                    name=decision.name,
                    action=action,
                    skill_id=decision.skill_id,
                    snapshot=skill_snapshot,
                )
            )

        # 4. 升级会整体替换 Agent 的 Skill 绑定，不在新绑定里的 Skill 都会被解绑。
        #    只解绑、不删除，因为可能还有其他 Agent 在用。报告要列全：新版本不再包含的托管 Skill，
        #    以及 Agent 当前绑定里本地额外加的、上次复用来的非托管 Skill。
        unbound_ids = live_managed_ids - overwritten_ids
        if active_install is not None:
            new_skill_ids: set[int] = set()
            for step in plan.skill_steps:
                if step.skill_id is not None:
                    new_skill_ids.add(step.skill_id)
            agent = await self.agent_service.get_agent(
                workspace_id, active_install.target_agent_id, actor_user_id
            )
            for binding in agent.skill_bindings:
                if binding.skill_id not in new_skill_ids:
                    unbound_ids.add(binding.skill_id)
        for skill_id in sorted(unbound_ids):
            unbound_snapshot = await self.skill_service.build_snapshot(workspace_id, skill_id)
            plan.skill_steps.append(
                SkillStep(name=unbound_snapshot["name"], action="unbind", skill_id=skill_id)
            )

    async def _install_skill(
        self,
        workspace_id: int,
        actor_user_id: int,
        plan: InstallPlan,
    ) -> int:
        """安装 Skill 条目，返回装出来的 Skill ID。"""
        step = plan.skill_steps[0]
        # skill 域在行锁内按同一个判定函数重新判定，冲突时直接报 409。
        decision = await self.skill_service.install_from_snapshot(
            workspace_id, actor_user_id, step.snapshot
        )
        # 预览之后有并发请求建了同名同内容的 Skill：Skill 条目不复用同名 Skill，照样拒绝。
        if decision.action == "reuse":
            raise AgentException.message(
                f"本工作区已有同名 Skill: {decision.name}",
                {"name": decision.name},
                status_code=409,
            )
        step.action = decision.action
        step.skill_id = decision.skill_id
        return decision.skill_id

    async def _install_agent(
        self,
        workspace_id: int,
        actor_user_id: int,
        snapshot: dict[str, Any],
        plan: InstallPlan,
        previous_managed_ids: list[int],
    ) -> tuple[int, list[int]]:
        """安装 Agent 条目：先处理自带的 Skill，再创建 Agent。

        返回 Agent ID 和托管 Skill ID（升级时只覆盖这些）：这次新建或恢复的 Skill，
        加上复用旧安装记录重装时、原本就托管且这次被原样复用的 Skill。
        """
        skill_ids: list[int] = []
        managed_skill_ids: list[int] = []
        for step in plan.skill_steps:
            # skill 域在行锁内按同一个判定函数重新判定，冲突时直接报 409。
            decision = await self.skill_service.install_from_snapshot(
                workspace_id, actor_user_id, step.snapshot
            )
            step.action = decision.action
            step.skill_id = decision.skill_id
            skill_ids.append(decision.skill_id)
            if decision.action == "create" or decision.action == "restore":
                managed_skill_ids.append(decision.skill_id)
            elif decision.action == "reuse" and decision.skill_id in previous_managed_ids:
                # Agent 删除后托管 Skill 还留着，重装时内容相同被复用。它仍归市场托管，
                # 否则下次升级会把它当成本地 Skill，内容一变就因冲突被拒。
                managed_skill_ids.append(decision.skill_id)

        # 复用 create_agent 的全部校验；模型留空使用工作区默认模型，作者的模型在别的工作区多半不存在。
        agent_snapshot = snapshot["agent"]
        agent = await self.agent_service.create_agent(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            name=agent_snapshot["name"],
            description=agent_snapshot["description"],
            system_prompt=agent_snapshot["system_prompt"],
            model_name=None,
            is_enabled=True,
            tool_ids=plan.tool_ids,
            skill_ids=skill_ids,
        )
        return agent.id, managed_skill_ids

    async def _upgrade_agent(
        self,
        workspace_id: int,
        actor_user_id: int,
        agent_id: int | None,
        snapshot: dict[str, Any],
        plan: InstallPlan,
    ) -> list[int]:
        """升级 Agent 条目，返回升级后的托管 Skill ID。

        覆盖描述、系统提示词、工具绑定和 Skill 绑定；名称、启用状态和模型保留本地设置。
        """
        skill_ids: list[int] = []
        managed_skill_ids: list[int] = []
        for step in plan.skill_steps:
            # 不再出现在新绑定列表里就等于解绑，Skill 本身不删除。
            if step.action == "unbind":
                continue
            # 托管 Skill 直接覆盖；绑定它的其他 Agent 也会受影响，这是原地升级的预期行为。
            if step.action == "overwrite":
                await self.skill_service.overwrite_from_snapshot(
                    workspace_id, step.skill_id, actor_user_id, step.snapshot
                )
                skill_ids.append(step.skill_id)
                managed_skill_ids.append(step.skill_id)
                continue
            decision = await self.skill_service.install_from_snapshot(
                workspace_id, actor_user_id, step.snapshot
            )
            step.action = decision.action
            step.skill_id = decision.skill_id
            skill_ids.append(decision.skill_id)
            if decision.action == "create" or decision.action == "restore":
                managed_skill_ids.append(decision.skill_id)

        agent_snapshot = snapshot["agent"]
        await self.agent_service.overwrite_for_upgrade(
            workspace_id,
            agent_id,
            actor_user_id,
            description=agent_snapshot["description"],
            system_prompt=agent_snapshot["system_prompt"],
            tool_ids=plan.tool_ids,
            skill_ids=skill_ids,
        )
        return managed_skill_ids

    # ---------- 内部辅助 ----------

    async def _require_write_access(self, workspace_id: int, actor_user_id: int) -> None:
        await self.workspace_service.require_workspace_role(
            workspace_id,
            actor_user_id,
            _WRITE_ROLES,
        )

    async def _create_community_listing(
        self,
        workspace_id: int,
        actor_user_id: int,
        kind: str,
        source_id: int,
    ) -> MarketListingRecord:
        """源对象第一次提交时新建条目，状态为还没有通过版本的 unlisted。"""
        source_agent_id = None
        source_skill_id = None
        if kind == "agent":
            source_agent_id = source_id
        else:
            source_skill_id = source_id
        # 条目还不存在时行锁锁不住任何行，并发的首次提交由源对象的部分唯一索引兜底，后到者转成 409。
        try:
            return await self.repo.create_listing(
                kind=kind,
                status="unlisted",
                is_official=False,
                official_key=None,
                publisher_workspace_id=workspace_id,
                source_agent_id=source_agent_id,
                source_skill_id=source_skill_id,
                created_by_user_id=actor_user_id,
            )
        except IntegrityError as exc:
            raise AgentException.message(
                "该对象正在被其他请求提交上架，请稍后重试",
                status_code=409,
            ) from exc

    async def _lock_listing_of_version(
        self,
        version_id: int,
    ) -> tuple[MarketListingRecord, MarketVersionRecord]:
        """锁住版本所属的条目，再重新读一次版本。

        撤回、通过、驳回都要先拿这把锁，防止同一条目的版本被同时处理；
        等锁期间版本可能已被别的请求处理，所以拿到锁之后要重新读版本状态。
        """
        version = await self.repo.get_version(version_id)
        if version is None:
            raise _version_not_found()
        listing = await self.repo.get_listing_for_update(version.listing_id)
        if listing is None:
            raise _version_not_found()
        version = await self.repo.get_version(version_id)
        if version is None:
            raise _version_not_found()
        return listing, version

    async def _lock_listing_for_author(
        self,
        workspace_id: int,
        listing_id: int,
    ) -> MarketListingRecord:
        """作者下架、重新上架前锁住条目；别的工作区的社区条目按不存在处理。"""
        listing = await self.repo.get_listing_for_update(listing_id)
        if listing is None:
            raise _listing_not_found()
        # 官方条目没有发布工作区，留给后面的状态检查返回 409「不支持」。
        if not listing.is_official and listing.publisher_workspace_id != workspace_id:
            raise _listing_not_found()
        return listing

    async def _require_installable(
        self,
        listing_id: int,
    ) -> tuple[MarketListingRecord, MarketVersionRecord]:
        """取可以安装或升级的条目和它的当前版本。"""
        listing = await self.repo.get_listing(listing_id)
        if listing is None:
            raise _listing_not_found()
        check_listing_installable(listing)
        version = None
        if listing.current_version_number is not None:
            version = await self.repo.get_version_by_number(
                listing.id, listing.current_version_number
            )
        # listed 的条目一定有当前版本，走到这里说明数据不一致。
        if version is None:
            raise AgentException.message("条目的当前版本不存在", status_code=409)
        return listing, version

    async def _get_active_install(
        self,
        workspace_id: int,
        install: MarketInstallRecord | None,
    ) -> MarketInstallRecord | None:
        """安装出来的对象还在时返回这条安装记录，否则视为未安装，返回 None。"""
        if install is None:
            return None
        active_installs = await self._filter_active_installs(workspace_id, [install])
        return active_installs.get(install.listing_id)

    async def _filter_active_installs(
        self,
        workspace_id: int,
        installs: list[MarketInstallRecord],
    ) -> dict[int, MarketInstallRecord]:
        """只保留安装对象仍存在的安装记录，按条目 ID 索引。

        Agent 和 Skill 都是软删除，不会触发外键的 SET NULL，所以要请 agent 域、skill 域确认目标还在。
        """
        agent_ids: list[int] = []
        skill_ids: list[int] = []
        for install in installs:
            if install.target_agent_id is not None:
                agent_ids.append(install.target_agent_id)
            if install.target_skill_id is not None:
                skill_ids.append(install.target_skill_id)
        existing_agent_ids = await self.agent_service.filter_existing_ids(workspace_id, agent_ids)
        existing_skill_ids = await self.skill_service.filter_existing_ids(workspace_id, skill_ids)

        active_installs: dict[int, MarketInstallRecord] = {}
        for install in installs:
            target_exists = (
                install.target_agent_id in existing_agent_ids
                or install.target_skill_id in existing_skill_ids
            )
            if target_exists:
                active_installs[install.listing_id] = install
        return active_installs

    async def _publisher_names(
        self,
        listings: list[MarketListingRecord],
    ) -> dict[int, str | None]:
        """按条目 ID 给出发布方名称：官方条目固定为「官方」，社区条目取发布工作区的展示名。"""
        workspace_ids: list[int] = []
        for listing in listings:
            if listing.publisher_workspace_id is not None:
                workspace_ids.append(listing.publisher_workspace_id)
        display_names = await self.workspace_service.get_display_names_by_ids(workspace_ids)

        names: dict[int, str | None] = {}
        for listing in listings:
            if listing.is_official:
                names[listing.id] = OFFICIAL_PUBLISHER_NAME
            elif listing.publisher_workspace_id is not None:
                # 发布工作区已删除时取不到展示名，显示为空。
                names[listing.id] = display_names.get(listing.publisher_workspace_id)
            else:
                names[listing.id] = None
        return names

    async def _build_browse_items(
        self,
        workspace_id: int,
        listings: list[MarketListingRecord],
    ) -> list[MarketBrowseItem]:
        """给条目补上发布方名称、安装次数，以及当前工作区的安装状态。"""
        listing_ids: list[int] = []
        for listing in listings:
            listing_ids.append(listing.id)
        install_counts = await self.repo.count_installs_by_listing_ids(listing_ids)
        workspace_installs = await self.repo.list_installs_of_workspace(workspace_id, listing_ids)
        active_installs = await self._filter_active_installs(workspace_id, workspace_installs)
        publisher_names = await self._publisher_names(listings)

        items: list[MarketBrowseItem] = []
        for listing in listings:
            install = active_installs.get(listing.id)
            installed_version_number = None
            has_update = False
            if install is not None:
                installed_version_number = install.installed_version_number
                current_version_number = listing.current_version_number
                if current_version_number is not None:
                    has_update = installed_version_number < current_version_number
            items.append(
                MarketBrowseItem(
                    id=listing.id,
                    kind=listing.kind,
                    name=listing.name,
                    summary=listing.summary,
                    category=listing.category,
                    is_official=listing.is_official,
                    current_version_number=listing.current_version_number,
                    publisher_name=publisher_names[listing.id],
                    install_count=install_counts.get(listing.id, 0),
                    installed=install is not None,
                    installed_version_number=installed_version_number,
                    has_update=has_update,
                    updated_at=listing.updated_at,
                )
            )
        return items


def _listing_not_found() -> AgentException:
    return AgentException.message("市场条目不存在", status_code=404)


def _version_not_found() -> AgentException:
    return AgentException.message("版本不存在", status_code=404)


def _pending_version_exists() -> AgentException:
    return AgentException.message(
        "该条目已有待审核的版本，请等待审核结果或先撤回",
        status_code=409,
    )


def _plan_report(plan: InstallPlan) -> MarketInstallReport:
    """把判定结果转成报告；安装和升级执行后，报告里是实际的处理结果。"""
    dropped_tools: list[MarketDroppedTool] = []
    for tool_key, reason in plan.dropped_tools.items():
        dropped_tools.append(
            MarketDroppedTool(tool_key=tool_key, reason=reason, message=TOOL_DROP_MESSAGES[reason])
        )
    skills: list[MarketSkillOutcome] = []
    for step in plan.skill_steps:
        skills.append(
            MarketSkillOutcome(name=step.name, action=step.action, skill_id=step.skill_id)
        )
    return MarketInstallReport(
        bound_tool_keys=list(plan.bound_tool_keys),
        dropped_tools=dropped_tools,
        skills=skills,
    )


def _plan_preview(plan: InstallPlan) -> MarketInstallPreview:
    return MarketInstallPreview(
        mode=plan.mode,
        installed_version_number=plan.installed_version_number,
        target_version_number=plan.target_version_number,
        can_proceed=len(plan.blockers) == 0,
        blockers=list(plan.blockers),
        agent_name_conflict=plan.agent_name_conflict,
        report=_plan_report(plan),
    )
