"""Skill 工具层(PRD §14):三个工具经 Anthropic 原生 tool-calling 暴露。

- SkillTool(Skill):加载指定 skill 的正文 + 资源清单(排除 SKILL.md)。
- SkillResourceTool(SkillResource):读取单个资源文件(文本内联,二进制只给描述)。
- SkillRunTool(SkillRun):执行 scripts/ 下脚本,返回退出码 + 截断后的 stdout/stderr。

均继承 tools.base.BaseTool,run async 返回 str;经 ctx.db 构造 SkillService(ctx.db);
领域错误在 run 内捕获转可读中文文本,不抛给框架(风格同 tools/builtin/task.py)。
"""

from pydantic import BaseModel, Field

from core.errors import AgentException
from skill.service import SkillService
from tools.base import BaseTool, ToolContext

# 文本类 mime:命中则把资源内容内联返回,否则只给描述(避免把二进制/base64 塞进上下文)。
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/x-python",
    "application/javascript",
    "application/x-sh",
    "application/x-shellscript",
    "application/toml",
}


def _is_text_mime(mime: str) -> bool:
    """判定 mime 是否按文本内联。"""
    if any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return True
    return mime in _TEXT_MIME_EXACT


def _human_size(n: int) -> str:
    """字节数转人类可读(供资源清单展示,如 8.4KB)。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


# ---------------------------------------------------------------------------
# SkillTool:加载正文
# ---------------------------------------------------------------------------
class SkillInput(BaseModel):
    name: str = Field(description="要加载的 skill 名称(取自系统提示中的可用 skill 清单)")


class SkillTool(BaseTool):
    name = "Skill"
    description = (
        "加载指定 skill 的完整说明并注入上下文。"
        "当任务匹配某个可用 skill 时,先调用它取回正文再执行。"
    )
    input_model = SkillInput

    async def run(self, args: SkillInput, ctx: ToolContext) -> str:
        service = SkillService(ctx.db)
        try:
            body = await service.load_body(args.name)
            resources = await service.list_resources(args.name)
        except AgentException:
            # skill 不存在等领域错误统一转可读文本(不抛)。
            return f"错误:未知 skill '{args.name}'。"

        # 资源清单排除 SKILL.md(正文已单独返回)。
        listed = [r for r in resources if r.relative_path != "SKILL.md"]
        lines = [
            f"- {r.relative_path} ({r.mime}, {_human_size(r.size)})" for r in listed
        ]
        resource_block = "\n".join(lines) if lines else "(无附属资源)"
        return (
            f'<skill name="{args.name}">\n'
            f"{body}\n"
            f"</skill>\n"
            f'<skill_resources base="{args.name}">\n'
            f"此 skill 的相对路径以 {args.name}/ 为基准。"
            f"以下资源可用 SkillResource 工具按 relative_path 读取:\n"
            f"{resource_block}\n"
            f"脚本(scripts/)可读取查看;如需运行,使用 SkillRun 工具"
            f"(当前为进程内执行,受超时与输出上限约束)。\n"
            f"</skill_resources>"
        )


# ---------------------------------------------------------------------------
# SkillResourceTool:读资源
# ---------------------------------------------------------------------------
class SkillResourceInput(BaseModel):
    skill_name: str = Field(description="skill 名称")
    relative_path: str = Field(description="资源相对路径,如 references/cli.md")


class SkillResourceTool(BaseTool):
    name = "SkillResource"
    description = "读取指定 skill 的某个资源文件(references/scripts/assets),返回其内容。"
    input_model = SkillResourceInput

    async def run(self, args: SkillResourceInput, ctx: ToolContext) -> str:
        service = SkillService(ctx.db)
        try:
            data, mime = await service.read_resource(args.skill_name, args.relative_path)
        except AgentException as exc:
            return f"错误:{exc.message}"
        if _is_text_mime(mime):
            return data.decode("utf-8", errors="replace")
        # 二进制类不内联,只返回描述(不塞 base64 进上下文)。
        return (
            f"[二进制资源 {args.relative_path},{mime},{len(data)} 字节,暂不支持内联展示]"
        )


# ---------------------------------------------------------------------------
# SkillRunTool:执行脚本
# ---------------------------------------------------------------------------
class SkillRunInput(BaseModel):
    skill_name: str = Field(description="skill 名称")
    relative_path: str = Field(description="脚本相对路径,须在 scripts/ 下")
    args: list[str] | None = Field(default=None, description="传给脚本的命令行参数")
    stdin: str | None = Field(default=None, description="写入脚本标准输入的文本")


class SkillRunTool(BaseTool):
    name = "SkillRun"
    description = (
        "执行指定 skill 的脚本(scripts/ 下),返回退出码与截断后的 stdout/stderr。"
        "进程内执行,受超时与输出上限约束。"
    )
    input_model = SkillRunInput

    async def run(self, args: SkillRunInput, ctx: ToolContext) -> str:
        service = SkillService(ctx.db)
        try:
            result = await service.run_script(
                args.skill_name, args.relative_path, args.args, args.stdin
            )
        except AgentException as exc:
            return f"错误:{exc.message}"
        status = "超时被终止" if result.timed_out else f"退出码 {result.exit_code}"
        parts = [
            f"执行结果:{status}",
        ]
        if result.truncated:
            parts.append("(输出已按上限截断)")
        parts.append(f"--- stdout ---\n{result.stdout}")
        parts.append(f"--- stderr ---\n{result.stderr}")
        return "\n".join(parts)
