"""官方预置：读取代码仓库里的预置目录，在启动时同步进市场。

目录结构（目录名就是条目的 official_key）：
    presets/skills/<skill-name>/SKILL.md   加上其他资源文件；目录名必须等于 SKILL.md 里的 name
    presets/agents/<key>/AGENT.md          YAML frontmatter 加正文，正文就是系统提示词

先把全部预置读完、校验完再写库：预置内容是代码的一部分，任何一处不合法都直接抛错，让启动失败。
同步是 market 域自己的业务，直接使用本域的 MarketRepository；跨域只调用 ToolCatalogService。
这里只 flush 不 commit，由启动流程提交或回滚。
"""

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from market.models import MarketListingRecord
from market.repository import MarketRepository
from market.service import (
    MARKET_CATEGORIES,
    TOOL_DROP_MESSAGES,
    MarketService,
    check_credential_files,
    check_snapshot_size,
    compute_snapshot_hash,
)
from skill.parser import SKILL_MD, SkillBundle, parse_skill_zip
from tools.service import ToolCatalogService

logger = logging.getLogger(__name__)

# 预置内容的默认根目录；测试可以传入临时目录。
PRESETS_DIR = Path(__file__).parent / "presets"
AGENT_MD = "AGENT.md"

# 没写分类时使用的分类。
DEFAULT_CATEGORY = "other"
# official_key 列和 agents.name 列的长度上限。
OFFICIAL_KEY_MAX_LENGTH = 128
AGENT_NAME_MAX_LENGTH = 128

# 预置从代码中删除后，条目改为 removed 时写入的原因。
REMOVED_REASON = "官方预置已从代码中移除"

# AGENT.md 的写法与 SKILL.md 相同：首行为 ---，两个 --- 之间是 YAML，之后是正文。
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


@dataclass
class OfficialPreset:
    """读取并校验过的一个官方预置。snapshot 的格式与社区条目的快照完全一致。"""

    kind: str
    key: str
    category: str
    snapshot: dict[str, Any]


@dataclass
class PresetSyncOutcome:
    """一个官方条目的同步结果。

    action：created 新建，updated 内容变化后新增了版本，restored 从 removed 恢复上架，
    unchanged 没有变化，removed 预置已从代码中删除。
    """

    kind: str
    official_key: str | None
    listing_id: int
    version_number: int | None
    action: str


# ---------- 读取与校验（只读文件，不碰数据库） ----------


def load_presets(root: Path) -> list[OfficialPreset]:
    """读取并校验预置目录，先返回全部 Skill 预置，再返回全部 Agent 预置。"""
    if not root.is_dir():
        raise AgentException.message(f"官方预置根目录不存在：{root}")

    presets: list[OfficialPreset] = []
    # Agent 引用的 Skill 只能来自 presets/skills，内容直接取自这里，按名称索引。
    skill_bundles: dict[str, SkillBundle] = {}
    for skill_dir in _list_preset_dirs(root / "skills"):
        bundle, category = load_skill_preset(skill_dir)
        skill_bundles[bundle.name] = bundle
        snapshot = {"skill": build_skill_snapshot(bundle)}
        _check_snapshot(f"skills/{skill_dir.name}", snapshot)
        presets.append(
            OfficialPreset(kind="skill", key=skill_dir.name, category=category, snapshot=snapshot)
        )

    for agent_dir in _list_preset_dirs(root / "agents"):
        preset = load_agent_preset(agent_dir, skill_bundles)
        _check_snapshot(f"agents/{agent_dir.name}", preset.snapshot)
        presets.append(preset)
    return presets


def load_skill_preset(skill_dir: Path) -> tuple[SkillBundle, str]:
    """读取一个 Skill 预置，返回解析结果和分类。

    目录在内存里打成 zip 交给 skill 域的 parse_skill_zip，和用户上传走同一套解析和校验规则。
    分类取 SKILL.md frontmatter 的 metadata.market_category，不写时为 other。
    """
    where = f"skills/{skill_dir.name}"
    # parse_skill_zip 会把层级最浅的 SKILL.md 当作根，这里要求它直接放在预置目录下。
    if not (skill_dir / SKILL_MD).is_file():
        raise _preset_error(where, "缺少 SKILL.md")
    try:
        bundle = parse_skill_zip(_zip_directory(skill_dir))
    except AgentException as exc:
        raise _preset_error(where, exc.message) from exc
    if bundle.name != skill_dir.name:
        raise _preset_error(
            where,
            f"目录名必须与 SKILL.md 里的 name 一致，当前 name 为 {bundle.name}",
        )

    category = DEFAULT_CATEGORY
    metadata = bundle.parsed.frontmatter.get("metadata")
    if metadata is not None and "market_category" in metadata:
        category = metadata["market_category"]
    if not isinstance(category, str) or category not in MARKET_CATEGORIES:
        raise _preset_error(where, f"metadata.market_category 字段的分类未知：{category}")
    return bundle, category


def load_agent_preset(agent_dir: Path, skill_bundles: dict[str, SkillBundle]) -> OfficialPreset:
    """读取一个 Agent 预置。

    frontmatter 有 name、description、category、tools、skills，正文是系统提示词。
    快照的格式和排序规则与 AgentDefinitionService.build_snapshot 相同：tool_keys 按字母排序，
    skills 按名称排序。这样预置内容没变时哈希也不变，不会每次启动都生成一个新版本。
    """
    key = agent_dir.name
    where = f"agents/{key}"
    if len(key) > OFFICIAL_KEY_MAX_LENGTH:
        raise _preset_error(where, f"目录名超过 {OFFICIAL_KEY_MAX_LENGTH} 个字符")
    agent_md = agent_dir / AGENT_MD
    if not agent_md.is_file():
        raise _preset_error(where, "缺少 AGENT.md")
    try:
        text = agent_md.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _preset_error(where, "AGENT.md 必须是 UTF-8 编码") from exc
    frontmatter, system_prompt = _parse_agent_md(where, text)

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _preset_error(where, "name 字段缺失、为空或不是字符串")
    # 与创建 Agent 接口的校验一致：名称去掉首尾空白，长度不超过 agents.name 列。
    name = name.strip()
    if len(name) > AGENT_NAME_MAX_LENGTH:
        raise _preset_error(where, f"name 字段超过 {AGENT_NAME_MAX_LENGTH} 个字符")

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _preset_error(where, "description 字段缺失、为空或不是字符串")

    category = frontmatter.get("category", DEFAULT_CATEGORY)
    if not isinstance(category, str) or category not in MARKET_CATEGORIES:
        raise _preset_error(where, f"category 字段的分类未知：{category}")

    if not system_prompt:
        raise _preset_error(where, "AGENT.md 的正文为空，正文就是系统提示词")

    tool_keys = _read_name_list(where, frontmatter, "tools")
    skill_names = _read_name_list(where, frontmatter, "skills")

    skills: list[dict[str, Any]] = []
    for skill_name in sorted(set(skill_names)):
        bundle = skill_bundles.get(skill_name)
        if bundle is None:
            raise _preset_error(
                where,
                f"skills 字段引用的 Skill 不在 presets/skills 里：{skill_name}",
            )
        # 每次都生成新的快照字典：官方 Skill 条目和这里内嵌的 Skill 是各自独立的快照。
        skills.append(build_skill_snapshot(bundle))

    snapshot = {
        "agent": {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "tool_keys": sorted(set(tool_keys)),
        },
        "skills": skills,
    }
    return OfficialPreset(kind="agent", key=key, category=category, snapshot=snapshot)


def build_skill_snapshot(bundle: SkillBundle) -> dict[str, Any]:
    """Skill 快照，字段与 SkillService.build_snapshot 一致。"""
    extra_files: dict[str, dict[str, str]] = {}
    for relative_path, entry in bundle.extra_files.items():
        extra_files[relative_path] = dict(entry)
    return {
        "name": bundle.name,
        "content": bundle.content,
        "extra_files": extra_files,
        "skill_hash": bundle.skill_hash,
    }


async def check_preset_tools(
    presets: list[OfficialPreset],
    tool_catalog_service: ToolCatalogService,
) -> None:
    """Agent 预置的 tools 必须都能按自建 Agent 的档位绑定，口径与市场安装时的工具解析一致。"""
    for preset in presets:
        if preset.kind != "agent":
            continue
        resolution = await tool_catalog_service.resolve_tool_keys(
            preset.snapshot["agent"]["tool_keys"],
            profile="user_agent",
        )
        if not resolution.unavailable:
            continue
        problems: list[str] = []
        for tool_key, reason in resolution.unavailable.items():
            problems.append(f"{tool_key}（{TOOL_DROP_MESSAGES[reason]}）")
        raise _preset_error(
            f"agents/{preset.key}",
            f"tools 字段引用了不可用的工具：{'、'.join(problems)}",
        )


# ---------- 同步进市场 ----------


async def sync_official_presets(
    db: AsyncSession,
    root: Path = PRESETS_DIR,
) -> list[PresetSyncOutcome]:
    """把预置目录同步进市场，返回每个官方条目的同步结果。只 flush 不 commit。

    四种情况：代码里有、库里没有就新建；快照哈希不同就新增版本；哈希相同就不动，
    但之前因从代码中删除而 removed 的恢复上架；库里有、代码里已删除的改为 removed，
    已经安装出去的副本不受影响。
    """
    # 1. 先读完、校验完全部预置文件，任何一处不合法都在写库之前失败。
    presets = load_presets(root)

    # 2. 多个进程同时启动时，咨询锁让同步排队执行；后到的进程拿到锁时能看到先到者提交的结果。
    repo = MarketRepository(db)
    await repo.lock_official_sync()

    # 3. 工具要对照库里的工具目录校验，所以启动时排在内置工具目录同步之后。
    await check_preset_tools(presets, ToolCatalogService(db))

    # 4. 逐个同步代码里的预置。
    service = MarketService(db)
    outcomes: list[PresetSyncOutcome] = []
    synced_keys: set[tuple[str, str]] = set()
    for preset in presets:
        outcome = await _sync_preset(repo, service, preset)
        outcomes.append(outcome)
        synced_keys.add((preset.kind, preset.key))

    # 5. 库里有、代码里已删除的官方条目改为 removed；已安装出去的副本不受影响。
    for listing in await repo.list_official_listings():
        if (listing.kind, listing.official_key) in synced_keys:
            continue
        if listing.status == "removed":
            continue
        listing.status = "removed"
        listing.status_reason = REMOVED_REASON
        listing = await repo.update(listing)
        outcomes.append(_outcome(listing, "removed"))

    for outcome in outcomes:
        if outcome.action == "unchanged":
            continue
        logger.info(
            "官方预置已同步",
            extra={
                "kind": outcome.kind,
                "official_key": outcome.official_key,
                "listing_id": outcome.listing_id,
                "version_number": outcome.version_number,
                "action": outcome.action,
            },
        )
    logger.info("官方预置同步完成", extra={"preset_count": len(presets)})
    return outcomes


async def _sync_preset(
    repo: MarketRepository,
    service: MarketService,
    preset: OfficialPreset,
) -> PresetSyncOutcome:
    """同步一个预置：新建、新增版本、从 removed 恢复上架，或者不动。"""
    snapshot_hash = compute_snapshot_hash(preset.snapshot)
    # 已经持有同步咨询锁，这里仍加行锁，是 publish_version 的约定：调用方先锁住条目。
    listing = await repo.get_official_listing_for_update(preset.kind, preset.key)

    # 代码里有、库里没有：新建条目，再建版本 1 并上架。
    if listing is None:
        listing = await repo.create_listing(
            kind=preset.kind,
            status="unlisted",
            is_official=True,
            official_key=preset.key,
            publisher_workspace_id=None,
            source_agent_id=None,
            source_skill_id=None,
            created_by_user_id=None,
        )
        listing = await _publish_new_version(repo, service, listing, preset, snapshot_hash)
        return _outcome(listing, "created")

    current_hash = None
    if listing.current_version_number is not None:
        current_hash = await repo.get_snapshot_hash(listing.id, listing.current_version_number)

    # 内容变了：新增一个版本并设为当前版本；之前被标为 removed 的也随之重新上架。
    if current_hash != snapshot_hash:
        listing = await _publish_new_version(repo, service, listing, preset, snapshot_hash)
        return _outcome(listing, "updated")

    # 内容没变：不建新版本。之前从代码里删过、被标为 removed 的恢复上架。
    if listing.status == "removed":
        listing.status = "listed"
        listing.status_reason = None
        listing = await repo.update(listing)
        return _outcome(listing, "restored")
    return _outcome(listing, "unchanged")


async def _publish_new_version(
    repo: MarketRepository,
    service: MarketService,
    listing: MarketListingRecord,
    preset: OfficialPreset,
    snapshot_hash: str,
) -> MarketListingRecord:
    """新增一个已通过的版本并设为当前版本。官方内容不经审核，提交人和审核人都为空。"""
    version_number = await repo.get_max_version_number(listing.id) + 1
    version = await repo.create_version(
        listing_id=listing.id,
        version_number=version_number,
        status="approved",
        category=preset.category,
        release_note=None,
        snapshot=preset.snapshot,
        snapshot_hash=snapshot_hash,
        submitted_by_user_id=None,
        reviewed_by_user_id=None,
        reviewed_at=None,
    )
    return await service.publish_version(listing, version)


def _outcome(listing: MarketListingRecord, action: str) -> PresetSyncOutcome:
    return PresetSyncOutcome(
        kind=listing.kind,
        official_key=listing.official_key,
        listing_id=listing.id,
        version_number=listing.current_version_number,
        action=action,
    )


# ---------- 内部辅助 ----------


def _preset_error(where: str, message: str) -> AgentException:
    """预置不合法的错误：消息里指明是哪个预置，启动会因此失败。"""
    return AgentException.message(f"官方预置 {where} 不合法：{message}", {"preset": where})


def _list_preset_dirs(parent: Path) -> list[Path]:
    """按名称列出 skills 或 agents 下的预置目录；目录不存在时视为没有预置，隐藏项和普通文件跳过。"""
    if not parent.is_dir():
        return []
    preset_dirs: list[Path] = []
    for entry in sorted(parent.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            preset_dirs.append(entry)
    return preset_dirs


def _zip_directory(directory: Path) -> bytes:
    """把目录下的全部文件按相对路径打进内存里的 zip。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            archive.writestr(path.relative_to(directory).as_posix(), path.read_bytes())
    return buffer.getvalue()


def _check_snapshot(where: str, snapshot: dict[str, Any]) -> None:
    """快照大小和凭据文件按社区上架的同一套规则检查。"""
    try:
        check_snapshot_size(snapshot)
        check_credential_files(snapshot)
    except AgentException as exc:
        raise _preset_error(where, exc.message) from exc


def _parse_agent_md(where: str, text: str) -> tuple[dict[str, Any], str]:
    """把 AGENT.md 拆成 frontmatter 和正文（去掉首尾空白）。"""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise _preset_error(
            where,
            "AGENT.md 缺少 YAML frontmatter（首行须为 ---，且有第二个 --- 分隔）",
        )
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise _preset_error(where, f"AGENT.md 的 frontmatter 不是合法的 YAML：{exc}") from exc
    if not isinstance(frontmatter, dict):
        raise _preset_error(where, "AGENT.md 的 frontmatter 必须是 key: value 结构")
    return frontmatter, match.group(2).strip()


def _read_name_list(where: str, frontmatter: dict[str, Any], field_name: str) -> list[str]:
    """读取 tools、skills 这类字符串列表字段，不写时为空列表。"""
    value = frontmatter.get(field_name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _preset_error(where, f"{field_name} 字段必须是列表")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _preset_error(where, f"{field_name} 字段只能包含非空字符串")
        names.append(item.strip())
    return names
