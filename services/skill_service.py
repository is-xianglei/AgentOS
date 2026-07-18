import hashlib
import io
import mimetypes
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import SKILL_EXEC_INTERPRETERS, settings
from core.errors import AgentException
from core.skill_executor import ExecResult, Executor, get_executor
from core.skill_parser import parse_skill_md, validate_frontmatter
from core.storage import ObjectStorage, get_object_storage, skill_object_key
from models.skill import SkillRecord
from repositories.skill_repo import SkillRepository
from schemas.skill import SkillDetailResponse, SkillResourceItem, SkillValidateResult

# 资源相对路径校验正则(PRD §6.2):字母/数字/点/下划线/连字符/斜杠,另禁 .. 段与绝对路径。
_RESOURCE_PATH_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


@dataclass(frozen=True)
class _ParsedBundle:
    """bundle 解析结果"""
    top: str
    name: str
    description: str
    frontmatter: dict[str, Any]
    version: str | None
    files: dict[str, bytes]  # 相对 skill 根的路径 -> 文件内容(含 SKILL.md)


class SkillService:
    """skill 上传 / 校验 / 读取 / 执行的业务编排层。"""

    def __init__(
        self,
        db: AsyncSession,
        storage: ObjectStorage | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.db = db
        self.repo = SkillRepository(db)
        # 懒加载:仅在真正需要时才落到单例工厂,避免纯解析路径强依赖 MinIO 配置。
        self._storage = storage
        self._executor = executor

    @property
    def storage(self) -> ObjectStorage:
        if self._storage is None:
            self._storage = get_object_storage()
        return self._storage

    @property
    def executor(self) -> Executor:
        if self._executor is None:
            self._executor = get_executor()
        return self._executor


    def _parse_bundle(self, file_bytes: bytes) -> _ParsedBundle:
        """解析并校验 zip bundle,返回结构化结果;任一步不满足抛领域错误。"""
        # 1. 读 zip
        try:
            zf = zipfile.ZipFile(io.BytesIO(file_bytes))
        except zipfile.BadZipFile as exc:
            raise AgentException.message(f"非法的 zip 文件: {exc}") from exc

        with zf:
            infos = zf.infolist()
            names = [info.filename for info in infos]

            # 2. 定位顶层目录:所有成员须共享唯一的顶层目录名。
            top = self._resolve_top_dir(names)

            # 3. zip-slip 防护:拒绝绝对路径、.. 段、越出 top/ 的成员。
            files: dict[str, bytes] = {}
            for info in infos:
                member = info.filename
                # 目录成员(以 / 结尾)跳过,不参与内容与存储。
                if member.endswith("/"):
                    continue
                self._guard_member(member, top)
                rel = member[len(top) + 1 :]  # 去掉 "top/" 前缀
                # 相对路径须过 §6.2 校验(zip-slip 之外再拦一层非法字符)。
                self._validate_relative_path(rel)
                files[rel] = zf.read(info)

        # 4. 定位并解析 SKILL.md
        if "SKILL.md" not in files:
            raise AgentException.message(f"bundle 缺少 {top}/SKILL.md")
        skill_md_text = files["SKILL.md"].decode("utf-8", errors="replace")
        parsed = parse_skill_md(skill_md_text)
        validate_frontmatter(parsed.frontmatter)
        name = str(parsed.frontmatter["name"])
        description = str(parsed.frontmatter["description"])
        version = parsed.frontmatter.get("version")
        version = str(version) if version is not None else None

        # 5. 一致性:顶层目录名须等于 frontmatter.name
        if top != name:
            raise AgentException.message(
                f"bundle 顶层目录名 {top!r} 与 frontmatter.name {name!r} 不一致"
            )

        return _ParsedBundle(
            top=top,
            name=name,
            description=description,
            frontmatter=parsed.frontmatter,
            version=version,
            files=files,
        )

    @staticmethod
    def _resolve_top_dir(names: list[str]) -> str:
        """从 zip 成员名推断唯一顶层目录;无 / 多 / 根级文件均报 SKILL_BUNDLE_INVALID。"""
        tops: set[str] = set()
        for raw in names:
            # 归一化:去掉可能的前导 ./,统一用 / 分隔。
            norm = raw.replace("\\", "/").lstrip("./")
            if not norm or norm == "/":
                continue
            head = norm.split("/", 1)[0]
            # 根级直接是文件(无 / )→ 视为缺少顶层目录。
            if "/" not in norm.rstrip("/"):
                raise AgentException.message(
                    f"bundle 内不允许根级文件 {raw!r};须有且只有一个顶层目录"
                )
            tops.add(head)
        if len(tops) == 0:
            raise AgentException.message("bundle 为空或无顶层目录")
        if len(tops) > 1:
            raise AgentException.message(
                f"bundle 存在多个顶层目录: {sorted(tops)};须有且只有一个"
            )
        return next(iter(tops))

    @staticmethod
    def _guard_member(member: str, top: str) -> None:
        """zip-slip 防护:拒绝绝对路径、.. 段、越出 top/ 前缀的成员。"""
        norm = member.replace("\\", "/")
        if norm.startswith("/"):
            raise AgentException.message(f"bundle 成员为绝对路径,拒绝: {member!r}")
        parts = norm.split("/")
        if ".." in parts:
            raise AgentException.message(f"bundle 成员含 .. 段,拒绝: {member!r}")
        if norm != top and not norm.startswith(top + "/"):
            raise AgentException.message(f"bundle 成员越出顶层目录 {top!r}: {member!r}")

    @staticmethod
    def _validate_relative_path(rel: str) -> None:
        """校验相对 skill 根的资源路径(§6.2):正则 + 禁 .. 段。"""
        if not rel or not _RESOURCE_PATH_RE.match(rel):
            raise AgentException.message(f"非法的资源相对路径: {rel!r}")
        if ".." in rel.split("/"):
            raise AgentException.message(f"资源相对路径含 .. 段: {rel!r}")

    @staticmethod
    def _skill_hash(file_bytes: bytes) -> str:
        """对上传的 zip 包整体算 sha256,作为幂等上传判重键。"""
        return hashlib.sha256(file_bytes).hexdigest()

    async def upload_bundle(self, file_bytes: bytes) -> SkillRecord:
        """上传 / 覆盖更新一个 skill bundle(§13.1 九步,全部按序)。"""
        bundle = self._parse_bundle(file_bytes)  # 第 1-5 步
        skill_hash = self._skill_hash(file_bytes)  # 第 6 步:对 zip 包整体算哈希

        # 幂等短路:同名且 zip 哈希一致 → 直接返回现有记录,不重复写 MinIO、不改 DB。
        existing = await self.repo.get_by_name(bundle.name)
        if existing is not None and existing.skill_hash == skill_hash:
            return existing

        # 第 7 步:先写对象存储
        if hasattr(self.storage, "ensure_bucket"):
            await self.storage.ensure_bucket()
        prefix = f"{bundle.name}/"
        await self.storage.delete_prefix(prefix)  # 清旧文件(覆盖更新去除已删资源)
        for rel, data in bundle.files.items():
            mime = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            await self.storage.put(skill_object_key(bundle.name, rel), data, content_type=mime)

        # 第 8 步:落库 + commit。
        record = await self.repo.upsert(
            name=bundle.name,
            description=bundle.description,
            frontmatter=bundle.frontmatter,
            version=bundle.version,
            skill_hash=skill_hash,
        )
        await self.db.commit()
        # 第 9 步:返回记录。
        return record

    async def validate(self, file_bytes: bytes) -> SkillValidateResult:
        """上传预检,不写存储不落库。"""
        try:
            bundle = self._parse_bundle(file_bytes)
        except AgentException as exc:
            return SkillValidateResult(ok=False, errors=[exc.message])
        return SkillValidateResult(
            ok=True, name=bundle.name, description=bundle.description
        )

    async def get_catalog(self) -> str:
        """渲染供发现用的 catalog 文本;每行 `- {name}: {description}`,空时给占位。"""
        records = await self.repo.list_enabled()
        if not records:
            return "(当前没有可用的 skill)"
        return "\n".join(f"- {r.name}: {r.description}" for r in records)

    async def list_skills(self) -> list[SkillRecord]:
        """列表接口:全部未软删 skill,按 name 排序。"""
        return await self.repo.list_all()

    async def get_detail(self, name: str) -> SkillDetailResponse:
        """详情:组装元数据 + frontmatter + 资源清单(含 SKILL.md)+ 正文。"""
        record = await self.repo.get_by_name(name)
        if record is None:
            raise AgentException.message(f"skill 不存在: {name}")
        body = await self.load_body(name)
        resources = await self.list_resources(name)  # 详情取完整视图,含 SKILL.md

        return SkillDetailResponse(
            id=record.id,
            name=record.name,
            description=record.description,
            version=record.version,
            scope=record.scope,
            created_at=record.created_at,
            updated_at=record.updated_at,
            frontmatter=record.frontmatter or {},
            resources=resources,
            body=body,
        )

    async def load_body(self, name: str) -> str:
        """从 MinIO 取 SKILL.md,剥离 frontmatter 只返回正文(避免把 YAML 头喂给模型)。"""
        raw = await self.storage.get(skill_object_key(name, "SKILL.md"))
        text = raw.decode("utf-8", errors="replace")
        return parse_skill_md(text).body

    async def list_resources(self, name: str) -> list[SkillResourceItem]:
        """经 list_prefix 实时重建资源清单;relative_path 去掉 `{name}/` 前缀,mime 按扩展名猜。"""
        prefix = f"{name}/"
        metas = await self.storage.list_prefix(prefix)
        items: list[SkillResourceItem] = []
        for meta in metas:
            key = meta.key
            rel = key[len(prefix) :] if key.startswith(prefix) else key
            if not rel:
                continue
            mime = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            items.append(
                SkillResourceItem(relative_path=rel, size=meta.size, mime=mime)
            )
        items.sort(key=lambda it: it.relative_path)
        return items

    async def read_resource(self, name: str, relative_path: str) -> tuple[bytes, str]:
        """读取单个资源:校验 rel(§6.2)→ storage.get;不存在抛 SKILL_RESOURCE_NOT_FOUND。

        返回 (内容字节, mime)。
        """
        self._validate_relative_path(relative_path)
        # storage.get 对象不存在时抛 NotFoundError(SKILL_RESOURCE_NOT_FOUND),直接透传。
        data = await self.storage.get(skill_object_key(name, relative_path))
        mime = mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
        return data, mime

    async def run_script(
        self,
        name: str,
        relative_path: str,
        args: list[str] | None = None,
        stdin_text: str | None = None,
    ) -> ExecResult:
        """执行 skill scripts/ 下脚本"""
        # 1. 全局开关。
        if not settings.skill_exec_enabled:
            raise AgentException.message(
                "skill 脚本执行已被禁用(AGENTOS_SKILL_EXEC_ENABLED=false)"
            )
        # 2. 校验 relative_path、必须在 scripts/ 下;扩展名查白名单。
        self._validate_relative_path(relative_path)
        if not relative_path.startswith("scripts/"):
            raise AgentException.message(f"脚本路径必须位于 scripts/ 下: {relative_path!r}")
        _, ext = os.path.splitext(relative_path)
        interpreter = SKILL_EXEC_INTERPRETERS.get(ext)
        if not interpreter:
            raise AgentException.message(
                f"脚本扩展名 {ext!r} 不在解释器白名单内,拒绝执行"
            )

        # 3. 物化:临时工作目录镜像整个 bundle;try/finally 确保清理。
        workdir = tempfile.mkdtemp(prefix="skillrun-")
        try:
            metas = await self.storage.list_prefix(f"{name}/")
            if not metas:
                raise AgentException.message(f"skill 不存在或无内容: {name}")
            prefix = f"{name}/"
            script_abspath: str | None = None
            for meta in metas:
                key = meta.key
                rel = key[len(prefix) :] if key.startswith(prefix) else key
                if not rel:
                    continue
                # 物化路径也过一遍校验,防对象存储侧被污染的 key 造成穿越。
                self._validate_relative_path(rel)
                dest = os.path.join(workdir, name, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                data = await self.storage.get(key)
                with open(dest, "wb") as fp:
                    fp.write(data)
                if rel == relative_path:
                    script_abspath = dest
            if script_abspath is None:
                raise AgentException.message(f"脚本不存在: {name}/{relative_path}")

            # 4. 组 argv + 最小 env 白名单(严禁下发 ANTHROPIC_API_KEY/DATABASE_URL/MINIO_*)。
            argv = list(interpreter) + [script_abspath] + list(args or [])
            env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            }
            materialized_root = os.path.join(workdir, name)

            # 5. 执行:cwd 设为该 skill 物化根,使脚本内相对路径原样解析。
            return await self.executor.run(
                workdir=materialized_root,
                argv=argv,
                stdin=stdin_text.encode("utf-8") if stdin_text is not None else None,
                timeout=settings.skill_exec_timeout,
                env=env,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
