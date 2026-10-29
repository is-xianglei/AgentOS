"""SKILL.md 与 zip bundle 的解析、校验和序列化。

无状态纯函数模块:不碰 DB、不扫磁盘。上传、预检、JSON 写入和导出共用同一套规则。
"""

import base64
import hashlib
import io
import json
import mimetypes
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any

import yaml

from core.errors import AgentException

SKILL_MD = "SKILL.md"
ENCODING_UTF8 = "utf-8"
ENCODING_BASE64 = "base64"

# 名称规则对齐 Agent Skills 规范:小写字母数字,以单个连字符分段;长度与 DB 列 String(64) 对齐。
NAME_MAX_LENGTH = 64
DESCRIPTION_MAX_LENGTH = 1024
COMPATIBILITY_MAX_LENGTH = 500
VERSION_MAX_LENGTH = 32
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# 解压上限按实际读出的字节累计,防 zip 炸弹伪造 header 中的 file_size。
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_FILES = 500
_READ_CHUNK_BYTES = 64 * 1024

# 打包工具噪声:任一路径段命中即忽略整个成员。
_IGNORED_SEGMENTS = frozenset({"__MACOSX", "__pycache__", ".git"})

# 首行须为 ---,捕获两个 --- 之间的 YAML 与其后的正文。
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _invalid(message: str) -> AgentException:
    return AgentException.message(message, status_code=400)


@dataclass(frozen=True)
class ParsedSkill:
    """SKILL.md 解析结果:frontmatter(JSON 安全的 dict)+ 正文 body(已 strip)。"""

    frontmatter: dict[str, Any]
    body: str


@dataclass(frozen=True)
class SkillBundle:
    """规范化的 skill 包:SKILL.md 原文 + 附属文件,落库与导出都以它为准。

    经 from_content() 构造即保证 SKILL.md 已通过校验。
    extra_files 形如 {相对路径: {"encoding": "utf-8" | "base64", "content": str}},不含 SKILL.md。
    """

    content: str
    parsed: ParsedSkill
    extra_files: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_content(
        cls,
        content: str,
        extra_files: dict[str, dict[str, str]] | None = None,
    ) -> SkillBundle:
        if "\x00" in content:
            raise _invalid("SKILL.md 不允许包含 NUL 字符")
        parsed = parse_skill_md(content)
        validate_frontmatter(parsed.frontmatter)
        return cls(content=content, parsed=parsed, extra_files=dict(extra_files or {}))

    @property
    def name(self) -> str:
        return self.parsed.frontmatter["name"]

    @property
    def description(self) -> str:
        return self.parsed.frontmatter["description"]

    @property
    def version(self) -> str | None:
        version = self.parsed.frontmatter.get("version")
        return str(version) if version is not None else None

    @property
    def skill_hash(self) -> str:
        return compute_skill_hash(self.content, self.extra_files)


def parse_skill_md(text: str) -> ParsedSkill:
    """解析 SKILL.md 文本为 frontmatter + body;无合法 frontmatter 或 YAML 非映射时抛错。"""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise _invalid("SKILL.md 缺少合法的 frontmatter(首行须为 ---,且存在第二个 --- 分隔)")
    raw_fm, body = match.group(1), match.group(2)
    try:
        loaded = yaml.safe_load(raw_fm)
    except yaml.YAMLError as exc:
        raise _invalid(f"frontmatter YAML 解析失败: {exc}") from exc
    if not isinstance(loaded, dict):
        raise _invalid("frontmatter 必须是 YAML 映射(key: value 结构)")
    return ParsedSkill(frontmatter=_to_json_safe(loaded), body=body.strip())


def _to_json_safe(data: dict[Any, Any]) -> dict[str, Any]:
    """把 YAML 解析出的日期等非 JSON 类型转为字符串,保证可写入 JSONB 且哈希稳定。"""
    try:
        return json.loads(json.dumps(data, ensure_ascii=False, default=str))
    except (TypeError, ValueError) as exc:
        raise _invalid(f"frontmatter 含无法序列化的结构: {exc}") from exc


def validate_frontmatter(fm: dict[str, Any]) -> None:
    """校验 frontmatter 必填与长度规则;未知顶层字段不拒绝,原样保留。"""
    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        # YAML 1.1 会把 off/no/123 这类裸值解析成非字符串,同样落到这里。
        raise _invalid("frontmatter 的 name 缺失、为空或不是字符串(off、no 等裸值需加引号)")
    if len(name) > NAME_MAX_LENGTH:
        raise _invalid(f"name 长度超过 {NAME_MAX_LENGTH} 字符: {len(name)}")
    if not _NAME_RE.fullmatch(name):
        raise _invalid(f"name 只能由小写字母、数字和单个连字符组成,且不能以连字符开头或结尾: {name!r}")
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _invalid("frontmatter 缺少必填字段 description 或其为空")
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise _invalid(f"description 长度超过 {DESCRIPTION_MAX_LENGTH} 字符: {len(description)}")
    compatibility = fm.get("compatibility")
    if compatibility is not None and (
        not isinstance(compatibility, str) or len(compatibility) > COMPATIBILITY_MAX_LENGTH
    ):
        raise _invalid(f"compatibility 若存在须为字符串且 ≤{COMPATIBILITY_MAX_LENGTH} 字符")
    metadata = fm.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise _invalid("metadata 若存在须为映射(dict)")
    version = fm.get("version")
    # 与 DB 列 String(32) 对齐,避免超长值落库时才以数据库错误暴露。
    if version is not None and len(str(version)) > VERSION_MAX_LENGTH:
        raise _invalid(f"version 长度超过 {VERSION_MAX_LENGTH} 字符")


def build_skill_md(frontmatter: dict[str, Any], body: str) -> str:
    """把 JSON 字段拼成 SKILL.md 原文。

    只做序列化;调用方须经 SkillBundle.from_content() 回解校验,保证与上传共用一套规则。
    """
    dumped = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"---\n{dumped}---\n\n{body.strip()}\n"


def compute_skill_hash(content: str, extra_files: dict[str, dict[str, str]]) -> str:
    """对规范化 bundle 求 sha256:同内容不同时间打包得到相同哈希,作为幂等写入判重键。"""
    canonical = json.dumps(
        {"content": content, "extra_files": extra_files},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_file(data: bytes) -> dict[str, str]:
    """能按 UTF-8 解码且不含 NUL 的存为文本,否则存 base64(PostgreSQL JSONB 不接受 NUL)。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and "\x00" not in text:
        return {"encoding": ENCODING_UTF8, "content": text}
    return {"encoding": ENCODING_BASE64, "content": base64.b64encode(data).decode("ascii")}


def decode_file(entry: dict[str, str]) -> bytes:
    if entry["encoding"] == ENCODING_BASE64:
        return base64.b64decode(entry["content"])
    return entry["content"].encode("utf-8")


def guess_mime(relative_path: str) -> str:
    return mimetypes.guess_type(relative_path)[0] or "application/octet-stream"


def normalize_relative_path(raw: str) -> str:
    """校验并规范化相对 skill 根的路径;拒绝空路径、绝对路径、盘符、NUL 和 .. 段。

    先把反斜杠归一化为 /,避免 Windows 风格的 `..\\` 绕过 .. 检查;空段与 . 段直接折叠。
    """
    if not raw or "\x00" in raw:
        raise _invalid(f"非法的资源相对路径: {raw!r}")
    norm = raw.replace("\\", "/")
    if norm.startswith("/") or _WINDOWS_DRIVE_RE.match(norm):
        raise _invalid(f"资源相对路径不允许绝对路径或盘符: {raw!r}")
    parts = [part for part in norm.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise _invalid(f"资源相对路径含 .. 段: {raw!r}")
    if not parts:
        raise _invalid(f"非法的资源相对路径: {raw!r}")
    return "/".join(parts)


def parse_skill_zip(file_bytes: bytes) -> SkillBundle:
    """解析 zip bundle:层级最浅的 SKILL.md 所在目录为 skill 根,根外成员与打包噪声忽略。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise _invalid(f"非法的 zip 文件: {exc}") from exc

    with zf:
        members: list[tuple[str, zipfile.ZipInfo]] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            # 先规范化再判断忽略与根前缀,保证 root 外的穿越路径同样被拒绝。
            path = normalize_relative_path(info.filename)
            if not _is_ignored(path):
                members.append((path, info))
        root_prefix = _resolve_root_prefix([path for path, _ in members])

        files: dict[str, bytes] = {}
        total_bytes = 0
        for path, info in members:
            if not path.startswith(root_prefix):
                continue
            rel = path[len(root_prefix):]
            if rel in files:
                raise _invalid(f"bundle 内存在重复路径: {rel!r}")
            if len(files) >= MAX_BUNDLE_FILES:
                raise _invalid(f"bundle 文件数超过上限 {MAX_BUNDLE_FILES}")
            data = _read_member(zf, info, MAX_UNCOMPRESSED_BYTES - total_bytes)
            total_bytes += len(data)
            files[rel] = data

    _reject_file_dir_conflicts(files)
    content = _decode_skill_md(files.pop(SKILL_MD))
    extra_files = {rel: encode_file(data) for rel, data in sorted(files.items())}
    return SkillBundle.from_content(content, extra_files)


def build_skill_zip(bundle: SkillBundle) -> bytes:
    """导出为 zip,目录结构 {name}/SKILL.md + 附属文件,可直接重新上传。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{bundle.name}/{SKILL_MD}", bundle.content.encode("utf-8"))
        for rel, entry in sorted(bundle.extra_files.items()):
            zf.writestr(f"{bundle.name}/{rel}", decode_file(entry))
    return buffer.getvalue()


def _is_ignored(path: str) -> bool:
    parts = path.split("/")
    if _IGNORED_SEGMENTS.intersection(parts):
        return True
    base = parts[-1]
    return base == ".DS_Store" or base.startswith("._")


def _resolve_root_prefix(paths: list[str]) -> str:
    """返回层级最浅的 SKILL.md 所在目录前缀(含末尾 /,根级为 "");找不到时报错。"""
    candidates = [path for path in paths if path.rsplit("/", 1)[-1] == SKILL_MD]
    if not candidates:
        raise _invalid("bundle 缺少 SKILL.md(根级或任一目录内均可)")
    chosen = min(candidates, key=lambda path: (path.count("/"), len(path)))
    head, _, _ = chosen.rpartition("/")
    return f"{head}/" if head else ""


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, remaining: int) -> bytes:
    """分块读取单个成员,累计超出剩余额度立即中止,不信任 header 声明的大小。"""
    chunks: list[bytes] = []
    size = 0
    try:
        with zf.open(info) as stream:
            while chunk := stream.read(_READ_CHUNK_BYTES):
                size += len(chunk)
                if size > remaining:
                    raise _invalid(f"bundle 解压后总大小超过上限 {MAX_UNCOMPRESSED_BYTES} 字节")
                chunks.append(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as exc:
        # RuntimeError 来自加密成员,NotImplementedError 来自不支持的压缩算法。
        raise _invalid(f"无法读取 zip 成员 {info.filename!r}: {exc}") from exc
    return b"".join(chunks)


def _reject_file_dir_conflicts(files: dict[str, bytes]) -> None:
    """同一路径既是文件又是目录时无法还原到磁盘,导出和脚本执行都会失败,提前拒绝。"""
    for rel in files:
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in files:
                raise _invalid(f"bundle 内路径冲突: {parent!r} 既是文件又是目录")


def _decode_skill_md(data: bytes) -> str:
    """严格按 UTF-8 解码 SKILL.md(容忍 BOM),不做替换字符兜底。"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _invalid("SKILL.md 必须是 UTF-8 编码") from exc
