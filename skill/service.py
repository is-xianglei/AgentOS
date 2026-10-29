import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import SKILL_EXEC_INTERPRETERS, settings
from core.errors import AgentException
from core.executor import ExecResult, Executor, get_executor
from skill.models import SkillRecord
from skill.parser import (
    ENCODING_UTF8,
    SKILL_MD,
    SkillBundle,
    build_skill_md,
    build_skill_zip,
    decode_file,
    guess_mime,
    normalize_relative_path,
    parse_skill_md,
    parse_skill_zip,
)
from skill.repository import SkillRepository
from skill.schemas import SkillDetailResponse, SkillResourceItem, SkillValidateResult
from workspace.service import WorkspaceService

# JSON 写入时由请求体全量覆盖的 frontmatter 键;其余键(如 license、allowed-tools)更新时保留。
_MANAGED_FRONTMATTER_KEYS = ("name", "description", "version", "compatibility", "metadata")
_WRITE_ROLES = {"owner", "admin"}


@dataclass(frozen=True)
class SkillResource:
    """单个资源的读取结果;text 为 None 表示二进制,只给描述不内联。"""

    relative_path: str
    mime: str
    size: int
    text: str | None


class SkillService:
    """skill 上传 / JSON 写入 / 启停 / 读取 / 导出 / 执行的业务编排层。"""

    def __init__(self, db: AsyncSession, executor: Executor | None = None) -> None:
        self.db = db
        self.repo = SkillRepository(db)
        self._executor = executor

    @property
    def executor(self) -> Executor:
        if self._executor is None:
            self._executor = get_executor()
        return self._executor

    async def upload_bundle(
        self,
        file_bytes: bytes,
        workspace_id: int,
        actor_user_id: int,
    ) -> SkillRecord:
        """上传 zip 创建或覆盖更新 skill;同名且内容哈希一致时直接返回已有记录。"""
        await self._require_write_access(workspace_id, actor_user_id)
        bundle = parse_skill_zip(file_bytes)
        existing = await self.repo.get_by_name(workspace_id, bundle.name)
        return await self._save_bundle(workspace_id, actor_user_id, bundle, existing)

    async def upsert_from_fields(
        self,
        workspace_id: int,
        actor_user_id: int,
        name: str,
        *,
        description: str,
        body: str,
        version: str | None,
        compatibility: str | None,
        metadata: dict[str, Any] | None,
    ) -> SkillRecord:
        """JSON 创建或更新:服务端拼 SKILL.md;更新时保留已有附属文件和非托管 frontmatter 键。"""
        await self._require_write_access(workspace_id, actor_user_id)
        existing = await self.repo.get_by_name(workspace_id, name)
        frontmatter: dict[str, Any] = {}
        if existing is not None:
            frontmatter.update(
                (key, value)
                for key, value in existing.frontmatter.items()
                if key not in _MANAGED_FRONTMATTER_KEYS
            )
        frontmatter.update(
            (key, value)
            for key, value in {
                "name": name,
                "description": description,
                "version": version,
                "compatibility": compatibility,
                "metadata": metadata,
            }.items()
            if value is not None
        )
        content = build_skill_md(frontmatter, body)
        extra_files = dict(existing.extra_files) if existing is not None else {}
        bundle = SkillBundle.from_content(content, extra_files)
        return await self._save_bundle(workspace_id, actor_user_id, bundle, existing)

    async def _save_bundle(
        self,
        workspace_id: int,
        actor_user_id: int,
        bundle: SkillBundle,
        existing: SkillRecord | None,
    ) -> SkillRecord:
        skill_hash = bundle.skill_hash
        if existing is not None and existing.skill_hash == skill_hash:
            return existing
        # 提交交给请求边界（get_db）统一处理
        return await self.repo.upsert(
            workspace_id=workspace_id,
            name=bundle.name,
            description=bundle.description,
            frontmatter=bundle.parsed.frontmatter,
            version=bundle.version,
            skill_hash=skill_hash,
            content=bundle.content,
            extra_files=bundle.extra_files,
            actor_user_id=actor_user_id,
        )

    async def validate(self, file_bytes: bytes) -> SkillValidateResult:
        """上传预检,不落库。"""
        try:
            bundle = parse_skill_zip(file_bytes)
        except AgentException as exc:
            return SkillValidateResult(ok=False, errors=[exc.message])
        return SkillValidateResult(ok=True, name=bundle.name, description=bundle.description)

    async def set_enabled(
        self,
        workspace_id: int,
        name: str,
        actor_user_id: int,
        enabled: bool,
    ) -> SkillRecord:
        """启用或禁用;agent 绑定保留,禁用期间由运行时过滤,重新启用后原绑定直接生效。"""
        await self._require_write_access(workspace_id, actor_user_id)
        record = await self._require_skill(workspace_id, name)
        if record.is_enabled == enabled:
            return record
        return await self.repo.set_enabled(record, enabled)

    async def get_catalog(
        self,
        workspace_id: int,
        skill_ids: list[int] | None = None,
    ) -> str:
        """渲染供发现用的 catalog 文本,只列已启用 skill;每行 `- {name}: {description}`。"""
        records = await self.repo.list_enabled(workspace_id, skill_ids)
        if not records:
            return "(当前没有可用的 skill)"
        return "\n".join(f"- {r.name}: {r.description}" for r in records)

    async def list_skills(self, workspace_id: int) -> list[SkillRecord]:
        """列表接口:全部未软删 skill(含禁用),按创建时间倒序。"""
        return await self.repo.list_all(workspace_id)

    async def get_detail(self, workspace_id: int, name: str) -> SkillDetailResponse:
        """详情:元数据 + frontmatter + 附属文件清单 + 正文;禁用的 skill 同样可查看。"""
        record = await self._require_skill(workspace_id, name)
        return SkillDetailResponse(
            id=record.id,
            workspace_id=record.workspace_id,
            name=record.name,
            description=record.description,
            version=record.version,
            scope=record.scope,
            is_enabled=record.is_enabled,
            created_at=record.created_at,
            updated_at=record.updated_at,
            frontmatter=record.frontmatter or {},
            resources=_resource_items(record.extra_files),
            body=parse_skill_md(record.content).body,
        )

    async def export_bundle(self, workspace_id: int, name: str) -> bytes:
        """导出 zip,结构与上传一致,可直接重新上传。"""
        record = await self._require_skill(workspace_id, name)
        bundle = SkillBundle.from_content(record.content, record.extra_files)
        return build_skill_zip(bundle)

    async def load_body(self, workspace_id: int, name: str) -> str:
        """取 SKILL.md 正文,剥离 frontmatter(避免把 YAML 头喂给模型)。"""
        record = await self._require_enabled_skill(workspace_id, name)
        return parse_skill_md(record.content).body

    async def list_resources(self, workspace_id: int, name: str) -> list[SkillResourceItem]:
        record = await self._require_enabled_skill(workspace_id, name)
        return _resource_items(record.extra_files)

    async def read_resource(
        self,
        workspace_id: int,
        name: str,
        relative_path: str,
    ) -> SkillResource:
        """读取单个资源;SKILL.md 返回原文,base64 存储的资源只返回描述。"""
        rel = normalize_relative_path(relative_path)
        record = await self._require_enabled_skill(workspace_id, name)
        if rel == SKILL_MD:
            text = record.content
            return SkillResource(rel, guess_mime(rel), len(text.encode("utf-8")), text)
        entry = record.extra_files.get(rel)
        if entry is None:
            raise AgentException.message(f"资源不存在: {name}/{rel}", status_code=404)
        if entry["encoding"] == ENCODING_UTF8:
            text = entry["content"]
            return SkillResource(rel, guess_mime(rel), len(text.encode("utf-8")), text)
        return SkillResource(rel, guess_mime(rel), len(decode_file(entry)), None)

    async def run_script(
        self,
        workspace_id: int,
        name: str,
        relative_path: str,
        args: list[str] | None = None,
        stdin_text: str | None = None,
    ) -> ExecResult:
        """把整个 bundle 物化到临时目录后执行 scripts/ 下脚本,cwd 为 skill 根。"""
        if not settings.skill_exec_enabled:
            raise AgentException.message(
                "skill 脚本执行已被禁用(AGENTOS_SKILL_EXEC_ENABLED=false)"
            )
        rel = normalize_relative_path(relative_path)
        if not rel.startswith("scripts/"):
            raise AgentException.message(f"脚本路径必须位于 scripts/ 下: {relative_path!r}")
        _, ext = os.path.splitext(rel)
        interpreter = SKILL_EXEC_INTERPRETERS.get(ext)
        if not interpreter:
            raise AgentException.message(f"脚本扩展名 {ext!r} 不在解释器白名单内,拒绝执行")
        record = await self._require_enabled_skill(workspace_id, name)
        if rel not in record.extra_files:
            raise AgentException.message(f"脚本不存在: {name}/{rel}", status_code=404)

        workdir = tempfile.mkdtemp(prefix="skillrun-")
        try:
            skill_root = await asyncio.to_thread(
                _materialize,
                Path(workdir).resolve(),
                record.name,
                record.content,
                record.extra_files,
            )
            # 最小 env 白名单:严禁下发 API Key、数据库连接串等敏感变量。
            env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            }
            return await self.executor.run(
                workdir=str(skill_root),
                argv=[*interpreter, str(skill_root / rel), *(args or [])],
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
        """校验Skill均属于当前工作区且未删除，返回去重后的ID；允许绑定已禁用的Skill。"""
        normalized = list(dict.fromkeys(skill_ids))
        await self._require_records_by_ids(workspace_id, normalized)
        return normalized

    async def get_enabled_names_by_ids(
        self,
        workspace_id: int,
        skill_ids: list[int],
    ) -> tuple[str, ...]:
        """按绑定顺序返回已启用Skill的名称；禁用的被过滤，保证运行时可调用集合不含禁用Skill。"""
        normalized = list(dict.fromkeys(skill_ids))
        records = await self._require_records_by_ids(workspace_id, normalized)
        enabled = {record.id: record.name for record in records if record.is_enabled}
        return tuple(enabled[skill_id] for skill_id in normalized if skill_id in enabled)

    async def delete_skill(
        self,
        workspace_id: int,
        name: str,
        actor_user_id: int,
    ) -> bool:
        """软删除并清理 agent 绑定;记录不存在时返回 False。"""
        await self._require_write_access(workspace_id, actor_user_id)
        record = await self.repo.get_by_name(workspace_id, name)
        if record is None:
            return False
        await self.repo.soft_delete(record)
        # 局部导入：真实双向 Service 依赖，改顶层仍成环 agent.service -> skill.service，待依赖倒置任务处理。
        from agent.service import AgentService

        await AgentService(self.db).remove_skill_bindings(workspace_id, record.id)
        return True

    async def _require_records_by_ids(
        self,
        workspace_id: int,
        skill_ids: list[int],
    ) -> list[SkillRecord]:
        records = await self.repo.list_by_ids(workspace_id, skill_ids)
        existing_ids = {record.id for record in records}
        missing = [skill_id for skill_id in skill_ids if skill_id not in existing_ids]
        if missing:
            raise AgentException.message(
                "绑定的Skill不存在",
                {"skill_ids": missing},
                status_code=404,
            )
        return records

    async def _require_skill(self, workspace_id: int, name: str) -> SkillRecord:
        record = await self.repo.get_by_name(workspace_id, name)
        if record is None:
            raise AgentException.message(f"skill 不存在: {name}", status_code=404)
        return record

    async def _require_enabled_skill(self, workspace_id: int, name: str) -> SkillRecord:
        """运行时入口:禁用与不存在对模型同样不可见,统一报同一条错误。"""
        record = await self.repo.get_by_name(workspace_id, name)
        if record is None or not record.is_enabled:
            raise AgentException.message(f"skill 不存在或已禁用: {name}", status_code=404)
        return record

    async def _require_write_access(self, workspace_id: int, actor_user_id: int) -> None:
        await WorkspaceService(self.db).require_workspace_role(
            workspace_id,
            actor_user_id,
            _WRITE_ROLES,
        )


def _resource_items(extra_files: dict[str, dict[str, str]]) -> list[SkillResourceItem]:
    return [
        SkillResourceItem(
            relative_path=rel,
            size=len(decode_file(entry)),
            mime=guess_mime(rel),
            encoding=entry["encoding"],
        )
        for rel, entry in sorted(extra_files.items())
    ]


def _materialize(
    workdir: Path,
    name: str,
    content: str,
    extra_files: dict[str, dict[str, str]],
) -> Path:
    """把 bundle 解码写入 workdir/{name}/,返回 skill 根;写盘前逐个校验路径不越出根目录。"""
    root = (workdir / normalize_relative_path(name)).resolve()
    if not root.is_relative_to(workdir):
        raise AgentException.message(f"skill 名称非法: {name!r}")
    files: dict[str, bytes] = {SKILL_MD: content.encode("utf-8")}
    files.update((rel, decode_file(entry)) for rel, entry in extra_files.items())
    for rel, data in files.items():
        dest = (root / normalize_relative_path(rel)).resolve()
        if not dest.is_relative_to(root):
            raise AgentException.message(f"资源路径越出 skill 根目录: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return root
