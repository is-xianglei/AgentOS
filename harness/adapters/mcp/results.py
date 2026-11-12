"""CallToolResult → ToolOutput：内容块转文本、截断，isError 原样映射为 ToolOutput.error。

非文本内容不发给模型（图片进模型属于后续阶段），只留占位文本；未知内容类型用 model_dump_json()，
不用 str()，后者会输出 Python repr。
"""

import base64
import binascii
import json

from mcp_types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    ContentBlock,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)

from harness.domain.tool import ToolOutput

_BLOCK_SEPARATOR = "\n\n"


def _approx_size(data: str) -> str:
    """base64 数据解码后的大致大小，用于占位文本；数据损坏时按编码长度估算。"""
    try:
        size = len(base64.b64decode(data, validate=False))
    except (binascii.Error, ValueError):
        size = len(data) * 3 // 4
    if size < 1024:
        return f"{size} B"
    return f"{max(size // 1024, 1)} KB"


def _media_placeholder(kind: str, mime_type: str, data: str) -> str:
    return f"[{kind} {mime_type}，约 {_approx_size(data)}，未发送给模型]"


def _block_text(block: ContentBlock) -> str:
    match block:
        case TextContent():
            return block.text
        case ImageContent():
            return _media_placeholder("图片", block.mime_type, block.data)
        case AudioContent():
            return _media_placeholder("音频", block.mime_type, block.data)
        case ResourceLink():
            return f"[资源链接 {block.name}: {block.uri}]"
        case EmbeddedResource(resource=TextResourceContents() as resource):
            return resource.text
        case EmbeddedResource(resource=BlobResourceContents() as resource):
            mime_type = resource.mime_type or "application/octet-stream"
            return _media_placeholder(f"资源 {resource.uri}", mime_type, resource.blob)
        case _:
            return block.model_dump_json(by_alias=True, exclude_none=True)


def truncate_middle(text: str, max_chars: int) -> str:
    """超过上限时保留开头和结尾、截掉中间，并注明截掉的字符数；结果不超过 max_chars。"""
    if len(text) <= max_chars:
        return text
    marker_template = "\n\n…[已截断 {} 字符]…\n\n"
    # 截掉的字符数取决于标记长度，按最长的数字位数预留，保证总长不超过上限。
    marker_room = len(marker_template.format(len(text)))
    keep = max(max_chars - marker_room, 0)
    head = keep - keep // 2
    tail = keep // 2
    removed = len(text) - head - tail
    tail_text = text[len(text) - tail :] if tail else ""
    return f"{text[:head]}{marker_template.format(removed)}{tail_text}"


def result_text(result: CallToolResult) -> str:
    """把各内容块转成文本并用空行连接；content 里没有文本块时才用 structuredContent 补成文本。"""
    parts = [_block_text(block) for block in result.content]
    has_text = any(isinstance(block, TextContent) for block in result.content)
    if not has_text and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return _BLOCK_SEPARATOR.join(parts)


def to_tool_output(result: CallToolResult, *, max_chars: int) -> ToolOutput:
    """CallToolResult → ToolOutput：isError 为真时文本仍交给模型，标记为失败。"""
    text = truncate_middle(result_text(result), max_chars)
    if result.is_error:
        return ToolOutput.error(text)
    return ToolOutput.ok(text)
