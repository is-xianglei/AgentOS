"""Frontmatter 解析与校验(PRD §10)。

无状态纯函数模块:只解析 SKILL.md 文本、校验合法性,不碰 DB、不碰对象存储、不扫目录。
供上传与预检接口复用。
"""

import re
from dataclasses import dataclass
from typing import Any

import yaml

from core.errors import AgentException

# 首行须为 ---,捕获两个 --- 之间的 YAML 与其后的正文。
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)

# name 格式:小写字母/数字/连字符,不以连字符开头结尾、不连续连字符。
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True)
class ParsedSkill:
    """解析结果:frontmatter(dict)+ 正文 body(已 strip)。"""

    frontmatter: dict[str, Any]
    body: str


def parse_skill_md(text: str) -> ParsedSkill:
    """解析 SKILL.md 文本为 frontmatter + body。

    无合法 frontmatter(不以 --- 开头 / 无第二个 --- / YAML 非 dict)→ 抛 SKILL_INVALID。
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise AgentException.message(
            "SKILL.md 缺少合法的 frontmatter(首行须为 ---,且存在第二个 --- 分隔)"
        )
    raw_fm, body = match.group(1), match.group(2)
    try:
        loaded = yaml.safe_load(raw_fm)
    except yaml.YAMLError as exc:
        raise AgentException.message(f"frontmatter YAML 解析失败: {exc}") from exc
    if not isinstance(loaded, dict):
        raise AgentException.message("frontmatter 必须是 YAML 映射(key: value 结构)")
    return ParsedSkill(frontmatter=loaded, body=body.strip())


def validate_frontmatter(fm: dict) -> None:
    """校验 frontmatter 各字段(全部 MUST,PRD §10.2)。

    任一不满足抛 SkillValidationError(即 ValidationAppError, code=SKILL_INVALID),
    错误信息明确指出违反项。未知顶层字段不拒绝,原样保留。
    """
    # name 存在
    name = fm.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        raise AgentException.message("frontmatter 缺少必填字段 name 或其为空")
    # name 格式
    if len(name) > 64:
        raise AgentException.message(f"name 长度超过 64 字符: {len(name)}")
    if not _NAME_RE.match(name):
        raise AgentException.message(
            f"name 格式非法(须匹配 ^[a-z0-9]+(-[a-z0-9]+)*$): {name!r}"
        )
    # description 存在
    description = fm.get("description")
    if (
        not description
        or not isinstance(description, str)
        or not description.strip()
    ):
        raise AgentException.message("frontmatter 缺少必填字段 description 或其为空")
    # description 长度
    if len(description) > 1024:
        raise AgentException.message(f"description 长度超过 1024 字符: {len(description)}")
    # compatibility 长度(若存在)
    compatibility = fm.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str) or len(compatibility) > 500:
            raise AgentException.message("compatibility 若存在须为字符串且 ≤500 字符")
    # metadata 类型(若存在):须为 dict 且值均可表达为 str(宽松:非 str 值转 str)
    metadata = fm.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise AgentException.message("metadata 若存在须为映射(dict)")


def get_allowed_tools(fm: dict) -> list[str] | None:
    """读取 allowed-tools,兼容列表与空格分隔字符串两种写法,统一为 list[str]。

    - 缺失 / None → 返回 None(表示未限制)。
    - list → 逐项转 str 后去除空白项。
    - str → 按空白切分。
    """
    raw = fm.get("allowed-tools")
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part for part in raw.split() if part]
    # 其他类型宽松处理:整体转字符串再切分。
    return [part for part in str(raw).split() if part]
