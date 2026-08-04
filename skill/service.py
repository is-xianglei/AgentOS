import hashlib
import io
import mimetypes
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import SKILL_EXEC_INTERPRETERS, settings
from core.errors import AgentException
from core.executor import ExecResult, Executor, get_executor
from core.storage import ObjectMeta, ObjectStorage, get_object_storage, skill_object_key
from skill.parser import parse_skill_md, validate_frontmatter
from skill.models import SkillRecord
from skill.repository import SkillRepository
from skill.schemas import SkillDetailResponse, SkillResourceItem, SkillValidateResult
from workspace.service import WorkspaceService


@dataclass(frozen=True)
class _ParsedBundle:
    """bundle 解析结果"""
    root_prefix: str  # skill 根在 zip 内的前缀,如 "foo/" 或 ""(SKILL.md 在根级)
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

            # 2. 定位 SKILL.md 所在层级,以其目录作为 skill 根前缀。
            #    宽松策略:不强制顶层目录结构,SKILL.md 在根级或任意单层目录内均可。
            root_prefix = self._resolve_root_prefix(
                [info.filename for info in infos if not info.filename.endswith("/")]
            )

            # 3. 收集 root_prefix 下的成员;穿越防护保留,root 外与 macOS 垃圾直接忽略。
            files: dict[str, bytes] = {}
            for info in infos:
                member = info.filename
                # 目录成员(以 / 结尾)跳过,不参与内容与存储。
                if member.endswith("/"):
                    continue
                if self._is_ignored_member(member):
                    continue
                norm = member.replace("\\", "/")
                # root 前缀之外的成员忽略(不报错),使"多顶层目录"这类 bundle 也能用。
                if root_prefix and not norm.startswith(root_prefix):
                    continue
                rel = norm[len(root_prefix):]
                # 穿越防护:绝对路径 / .. 段仍拒绝。
                self._validate_relative_path(rel)
                files[rel] = zf.read(info)

        # 4. 解析 SKILL.md(定位阶段已保证其存在)。
        skill_md_text = files["SKILL.md"].decode("utf-8", errors="replace")
        parsed = parse_skill_md(skill_md_text)
        validate_frontmatter(parsed.frontmatter)
        name = str(parsed.frontmatter["name"])
        description = str(parsed.frontmatter["description"])
        version = parsed.frontmatter.get("version")
        version = str(version) if version is not None else None

        return _ParsedBundle(
            root_prefix=root_prefix,
            name=name,
            description=description,
            frontmatter=parsed.frontmatter,
            version=version,
            files=files,
        )

    @staticmethod
    def _is_ignored_member(member: str) -> bool:
        """忽略打包工具产生的噪声:macOS 的 __MACOSX/ 与 .DS_Store、以 ._ 开头的 AppleDouble。"""
        norm = member.replace("\\", "/")
        parts = norm.split("/")
        if "__MACOSX" in parts:
            return True
        base = parts[-1]
        return base == ".DS_Store" or base.startswith("._")

    @classmethod
    def _resolve_root_prefix(cls, names: list[str]) -> str:
        """定位 SKILL.md 所在层级,返回其目录前缀(含末尾 /,根级则为 "")。

        宽松策略(PRD §5.3):不再强制"唯一顶层目录/禁根级文件/目录名==name"。
        只要能找到 SKILL.md 即可;存在多个时取路径最浅(层级最少)的那个作为根。
        找不到 → 报错。忽略 macOS 噪声成员后再判定。
        """
        candidates: list[str] = []
        for raw in names:
            if cls._is_ignored_member(raw):
                continue
            norm = raw.replace("\\", "/")
            if norm.split("/")[-1] == "SKILL.md":
                candidates.append(norm)
        if not candidates:
            raise AgentException.message("bundle 缺少 SKILL.md(根级或任一目录内均可)")
        # 取层级最浅的 SKILL.md(斜杠最少),其所在目录即 skill 根。
        chosen = min(candidates, key=lambda p: (p.count("/"), len(p)))
        head, _, _ = chosen.rpartition("/")
        return head + "/" if head else ""

    @staticmethod
    def _validate_relative_path(rel: str) -> None:
        """校验相对 skill 根的资源路径:不再限制字符集,仅保留路径穿越防护。

        安全兜底(不放宽):拒绝空路径、绝对路径与 .. 段,防 zip-slip / 对象存储 key 污染
        导致的目录穿越。先把反斜杠归一化为 /,避免 Windows 风格 `..\\` 绕过 .. 检查。
        """
        if not rel:
            raise AgentException.message(f"非法的资源相对路径: {rel!r}")
        norm = rel.replace("\\", "/")
        if norm.startswith("/"):
            raise AgentException.message(f"资源相对路径不允许绝对路径: {rel!r}")
        if ".." in norm.split("/"):
            raise AgentException.message(f"资源相对路径含 .. 段: {rel!r}")

    @staticmethod
    def _skill_hash(file_bytes: bytes) -> str:
        """对上传的 zip 包整体算 sha256,作为幂等上传判重键。"""
        return hashlib.sha256(file_bytes).hexdigest()

    async def upload_bundle(
        self,
        file_bytes: bytes,
        workspace_id: int,
        actor_user_id: int,
    ) -> SkillRecord:
        """上传 / 覆盖更新一个 skill bundle"""
        await WorkspaceService(self.db).require_workspace_role(
            workspace_id,
            actor_user_id,
            {"owner", "admin"},
        )
        bundle = self._parse_bundle(file_bytes)

        skill_hash = self._skill_hash(file_bytes)

        existing = await self.repo.get_by_name(workspace_id, bundle.name)

        if existing is not None and existing.skill_hash == skill_hash:
            return existing

        if hasattr(self.storage, "ensure_bucket"):
            await self.storage.ensure_bucket()

        prefix = self._storage_prefix(workspace_id, bundle.name)
        await self.storage.delete_prefix(prefix)

        for rel, data in bundle.files.items():
            mime = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            await self.storage.put(
                skill_object_key(workspace_id, bundle.name, rel),
                data,
                content_type=mime,
            )

        record = await self.repo.upsert(
            workspace_id=workspace_id,
            name=bundle.name,
            description=bundle.description,
            frontmatter=bundle.frontmatter,
            version=bundle.version,
            skill_hash=skill_hash,
        )
        # 提交交给请求边界（get_db）统一处理
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

    async def get_catalog(
        self,
        workspace_id: int,
        skill_ids: list[int] | None = None,
    ) -> str:
        """渲染供发现用的 catalog 文本;每行 `- {name}: {description}`,空时给占位。"""
        records = await self.repo.list_enabled(workspace_id, skill_ids)
        if not records:
            return "(当前没有可用的 skill)"
        return "\n".join(f"- {r.name}: {r.description}" for r in records)

    async def list_skills(self, workspace_id: int) -> list[SkillRecord]:
        """列表接口:全部未软删 skill,按 name 排序。"""
        return await self.repo.list_all(workspace_id)

    async def get_detail(self, workspace_id: int, name: str) -> SkillDetailResponse:
        """详情:组装元数据 + frontmatter + 资源清单(含 SKILL.md)+ 正文。"""
        record = await self.repo.get_by_name(workspace_id, name)
        if record is None:
            raise AgentException.message(f"skill 不存在: {name}")
        body = await self.load_body(workspace_id, name)
        resources = await self.list_resources(
            workspace_id,
            name,
        )  # 详情取完整视图,含 SKILL.md

        return SkillDetailResponse(
            id=record.id,
            workspace_id=record.workspace_id,
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

    async def load_body(self, workspace_id: int, name: str) -> str:
        """从 MinIO 取 SKILL.md,剥离 frontmatter 只返回正文(避免把 YAML 头喂给模型)。"""
        await self._require_skill(workspace_id, name)
        prefix, _ = await self._list_storage_objects(workspace_id, name)
        raw = await self.storage.get(f"{prefix}SKILL.md")
        text = raw.decode("utf-8", errors="replace")
        return parse_skill_md(text).body

    async def list_resources(self, workspace_id: int, name: str) -> list[SkillResourceItem]:
        """经 list_prefix 实时重建资源清单;relative_path 去掉 `{name}/` 前缀,mime 按扩展名猜。"""
        await self._require_skill(workspace_id, name)
        prefix, metas = await self._list_storage_objects(workspace_id, name)
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

    async def read_resource(
        self,
        workspace_id: int,
        name: str,
        relative_path: str,
    ) -> tuple[bytes, str]:
        """读取单个资源:校验 rel(§6.2)→ storage.get;不存在抛 SKILL_RESOURCE_NOT_FOUND。

        返回 (内容字节, mime)。
        """
        self._validate_relative_path(relative_path)
        await self._require_skill(workspace_id, name)
        prefix, _ = await self._list_storage_objects(workspace_id, name)
        # storage.get 对象不存在时抛 NotFoundError(SKILL_RESOURCE_NOT_FOUND),直接透传。
        data = await self.storage.get(f"{prefix}{relative_path}")
        mime = mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
        return data, mime

    async def run_script(
        self,
        workspace_id: int,
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
            await self._require_skill(workspace_id, name)
            prefix, metas = await self._list_storage_objects(workspace_id, name)
            if not metas:
                raise AgentException.message(f"skill 不存在或无内容: {name}")
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

    async def require_bindable_skill_ids(
        self,
        workspace_id: int,
        skill_ids: list[int],
    ) -> list[int]:
        """校验Skill均属于当前工作区且可用，并返回去重后的ID。"""
        normalized = list(dict.fromkeys(skill_ids))
        records = await self.repo.list_by_ids(workspace_id, normalized)
        existing_ids = {record.id for record in records}
        missing = [skill_id for skill_id in normalized if skill_id not in existing_ids]
        if missing:
            raise AgentException.message(
                "绑定的Skill不存在",
                {"skill_ids": missing},
                status_code=404,
            )
        return normalized

    async def get_names_by_ids(
        self,
        workspace_id: int,
        skill_ids: list[int],
    ) -> tuple[str, ...]:
        normalized = await self.require_bindable_skill_ids(workspace_id, skill_ids)
        records = await self.repo.list_by_ids(workspace_id, normalized)
        names_by_id = {record.id: record.name for record in records}
        return tuple(names_by_id[skill_id] for skill_id in normalized)

    async def delete_skill(
        self,
        workspace_id: int,
        name: str,
        actor_user_id: int,
    ) -> bool:
        await WorkspaceService(self.db).require_workspace_role(
            workspace_id,
            actor_user_id,
            {"owner", "admin"},
        )
        record = await self.repo.get_by_name(workspace_id, name)
        if record is None:
            return False
        deleted = await self.repo.soft_delete(workspace_id, name)
        if deleted:
            from agent.service import AgentService

            await AgentService(self.db).remove_skill_bindings(workspace_id, record.id)
        return deleted

    async def _require_skill(self, workspace_id: int, name: str) -> SkillRecord:
        record = await self.repo.get_by_name(workspace_id, name)
        if record is None:
            raise AgentException.message(f"skill 不存在: {name}", status_code=404)
        return record

    @staticmethod
    def _storage_prefix(workspace_id: int, name: str) -> str:
        return f"{workspace_id}/{name}/"

    async def _list_storage_objects(
        self,
        workspace_id: int,
        name: str,
    ) -> tuple[str, list[ObjectMeta]]:
        """优先读取工作区前缀，兼容迁移前遗留的全局对象路径。"""
        prefix = self._storage_prefix(workspace_id, name)
        metas = await self.storage.list_prefix(prefix)
        if metas:
            return prefix, metas
        legacy_prefix = f"{name}/"
        return legacy_prefix, await self.storage.list_prefix(legacy_prefix)
