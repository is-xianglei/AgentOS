"""OpenAI 客户端实现。"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from openai import NOT_GIVEN, AsyncOpenAI
from openai.types.chat import ChatCompletionFunctionToolParam, ChatCompletionMessageParam
from pydantic import BaseModel

from core.config import settings
from core.errors import AgentException
from core.events import BlockType, ToolInfo, Usage
from llm.base import BaseLLMClient
from llm.types import (
    LLMEventKind,
    LLMStreamEvent,
    LLMTurnResult,
    PromptBlock,
    TextBlock,
    ToolResultMessage,
    ToolSpec,
    ToolUse,
    ToolUseBlock,
    parse_assistant_blocks,
    parse_tool_result,
    parse_user_text,
)

logger = logging.getLogger(__name__)


# OpenAI 的 role="tool" 消息没有 is_error 字段(Anthropic 的 tool_result 有)。
# 该信号不能静默丢弃——否则模型分不清工具返回的是结果还是失败原因,故编成
# 内容前缀显式下发。前缀刻意用方括号中文标记而非裸 "error:",避免与工具自身
# 输出的 "error: ..." 文本(如 subagent 的权限拒绝语)混淆。
_TOOL_ERROR_PREFIX = "[工具执行失败] "


def _encode_tool_calls(blocks: list[ToolUseBlock]) -> list[dict[str, Any]]:
    """把中立工具调用块编成 OpenAI 的 tool_calls 数组。

    OpenAI 的入参是 JSON 字符串(Anthropic 是原生对象),故此处需序列化。
    """
    return [
        {
            "id": block.id,
            "type": "function",
            "function": {
                "name": block.name,
                "arguments": json.dumps(block.input, ensure_ascii=False),
            },
        }
        for block in blocks
    ]


def _encode_tool_result(tool_result: ToolResultMessage) -> dict[str, Any]:
    """把中立工具结果编成 OpenAI 独立的 role="tool" 消息。

    与 Anthropic 的结构性差异:OpenAI 每个工具结果是一条独立消息并以
    tool_call_id 与调用配对,不合并、也不塞进 user 消息。
    """
    output = tool_result.output
    return {
        "role": "tool",
        "tool_call_id": tool_result.tool_use_id,
        "content": f"{_TOOL_ERROR_PREFIX}{output}" if tool_result.is_error else output,
    }


def _map_usage(usage: dict[str, Any]) -> Usage:
    """把 OpenAI 的 usage 字段映射成中立 Usage。

    cache_creation_input_tokens 恒 0:OpenAI 无显式缓存写入概念,
    Usage 该字段本就缺省 0。缓存命中读取在 prompt_tokens_details.cached_tokens。
    """
    details = usage.get("prompt_tokens_details") or {}
    return Usage(
        input_tokens=int(usage.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage.get("completion_tokens", 0) or 0),
        cache_read_input_tokens=int(details.get("cached_tokens", 0) or 0),
    )


@dataclass
class _PendingToolCall:
    """适配过程中正在累积的工具调用状态。"""

    ref: int
    tool_id: str
    tool_name: str
    args_buffer: str = ""


class _OpenAIChunkAdapter:
    """把 OpenAI 裸 chunk 适配成中立 LLMStreamEvent 序列(模块私有)。

    生命周期为单次 stream() 调用:流开始时创建,结束即弃。
    OpenAI 没有块索引概念,block_ref 由本类自建:文本块恒占 0
    (首个非空 delta.content 时开块),工具块占 1 + tool_call.index;
    block_ref 只需单次调用内唯一,turn 级全局 index 由 TurnEventAssembler 分配。

    收束点是 finish_reason 非空的那个 chunk(OpenAI 无显式 block_stop 事件):
    文本块补 BLOCK_STOP,各工具块入参一次性解析并产出 TOOL_USE——工具块不发
    BLOCK_STOP,由装配器统一补发,provider 侧不参与协议决策。终局四字段全部
    自累积,不依赖 get_final_completion():后者在 finish_reason 为 length /
    content_filter 时抛异常取代返回值,已生成内容会随之丢失。

    THINKING_DELTA 恒不产出:Chat Completions 的 ChoiceDelta 没有 reasoning
    字段,协议本身不回传思考内容——这是协议事实而非实现缺失,不要"补齐"。
    """

    # 文本块的固定 block_ref;不产出 BLOCK_START 就不会有空块,恒占 0 是安全的。
    _TEXT_REF = 0

    def __init__(self) -> None:
        self._text_open = False
        self._text_parts: list[str] = []
        # 键为 OpenAI 的 tool_call.index(与 block_ref 差 1,见 _TEXT_REF 注释)。
        self._tool_calls: dict[int, _PendingToolCall] = {}
        self._usage: Usage | None = None
        self._stop_reason: str | None = None
        self._finished = False
        self._tool_uses: list[ToolUse] = []

    def adapt(self, chunk: dict[str, Any]) -> list[LLMStreamEvent]:
        """适配单个裸 chunk 为零或多个中立事件。

        usage 只在末 chunk 出现(choices 为空、需显式开启 include_usage),
        故与 choices 分支独立记录;role-only 首 chunk、空 delta 等噪声不产出事件。
        """
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = _map_usage(usage)
        choices = chunk.get("choices") or []
        if not choices:
            return []
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        events: list[LLMStreamEvent] = []
        events.extend(self._on_text_delta(delta.get("content")))
        events.extend(self._on_tool_calls(delta.get("tool_calls") or []))
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            events.extend(self._on_finish(finish_reason))
        return events

    def finalize(self) -> list[LLMStreamEvent]:
        """流耗尽后产出 TURN_RESULT 终结事件。

        终局必须晚于收束产出:usage 在 finish_reason 之后的末 chunk 才到达,
        在收束点直接产出 TURN_RESULT 会恒丢用量。未见 finish_reason 即流意外
        中断:不产出 TURN_RESULT,由装配器抛「LLM 流未产出终局结果」,
        沿用现有失败语义,不新增分支。
        """
        if not self._finished:
            return []
        result = LLMTurnResult(
            text="".join(self._text_parts),
            tool_uses=self._tool_uses,
            usage=self._usage,
            stop_reason=self._stop_reason,
        )
        return [LLMStreamEvent(kind=LLMEventKind.TURN_RESULT, result=result)]

    def _on_text_delta(self, content: Any) -> list[LLMStreamEvent]:
        """处理文本增量:首个非空增量开块,空串与非字符串视为噪声。"""
        if not isinstance(content, str) or not content:
            return []
        events: list[LLMStreamEvent] = []
        if not self._text_open:
            self._text_open = True
            events.append(
                LLMStreamEvent(
                    kind=LLMEventKind.BLOCK_START,
                    block_ref=self._TEXT_REF,
                    block_type=BlockType.TEXT,
                )
            )
        self._text_parts.append(content)
        events.append(
            LLMStreamEvent(kind=LLMEventKind.TEXT_DELTA, block_ref=self._TEXT_REF, text=content)
        )
        return events

    def _on_tool_calls(self, tool_calls: list[Any]) -> list[LLMStreamEvent]:
        """处理工具调用增量:首个增量开块并登记 id/name,后续只累积入参字符串。"""
        events: list[LLMStreamEvent] = []
        for raw in tool_calls:
            if not isinstance(raw, dict) or not isinstance(raw.get("index"), int):
                raise AgentException.message(
                    "工具调用增量缺少 index,无法与已开块配对",
                    {"tool_call": str(raw)[:200]},
                )
            function = raw.get("function") or {}
            pending = self._tool_calls.get(raw["index"])
            if pending is None:
                tool_id = raw.get("id") or ""
                tool_name = function.get("name") or ""
                if not tool_id or not tool_name:
                    # 无静默兜底:缺 id 会让后续 tool_result 无法配对,缺 name 无法路由工具。
                    raise AgentException.message(
                        "工具调用首个增量缺少 id 或 name",
                        {"tool_name": tool_name, "tool_call_id": tool_id},
                    )
                pending = _PendingToolCall(
                    ref=1 + raw["index"], tool_id=tool_id, tool_name=tool_name
                )
                self._tool_calls[raw["index"]] = pending
                events.append(
                    LLMStreamEvent(
                        kind=LLMEventKind.BLOCK_START,
                        block_ref=pending.ref,
                        block_type=BlockType.TOOL_USE,
                    )
                )
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                pending.args_buffer += arguments
        return events

    def _on_finish(self, finish_reason: str) -> list[LLMStreamEvent]:
        """收束:文本块补 BLOCK_STOP,各工具块入参一次性解析产出 TOOL_USE。

        stop_reason 原样带出不归一成 Anthropic 词汇表:该字段无枚举约束,
        强行归一会丢失 content_filter 这类各家独有的语义,由上层判读。
        """
        self._finished = True
        self._stop_reason = finish_reason
        events: list[LLMStreamEvent] = []
        if self._text_open:
            self._text_open = False
            events.append(LLMStreamEvent(kind=LLMEventKind.BLOCK_STOP, block_ref=self._TEXT_REF))
        for tc_index in sorted(self._tool_calls):
            pending = self._tool_calls[tc_index]
            tool_input = self._parse_tool_input(pending)
            self._tool_uses.append(
                ToolUse(id=pending.tool_id, name=pending.tool_name, input=tool_input)
            )
            events.append(
                LLMStreamEvent(
                    kind=LLMEventKind.TOOL_USE,
                    block_ref=pending.ref,
                    tool=ToolInfo(id=pending.tool_id, name=pending.tool_name, input=tool_input),
                )
            )
        return events

    @staticmethod
    def _parse_tool_input(pending: _PendingToolCall) -> dict[str, Any]:
        """解析累积完整的工具入参 JSON;空串视为无参数调用。"""
        try:
            return json.loads(pending.args_buffer) if pending.args_buffer else {}
        except json.JSONDecodeError as exc:
            # 无静默兜底:兜成 {} 会让工具带空参执行,失败原因在链路上彻底消失。
            raise AgentException.message(
                "工具调用入参 JSON 解析失败",
                {"tool_name": pending.tool_name, "tool_call_id": pending.tool_id},
            ) from exc


class OpenAIClient(BaseLLMClient):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float | None = None,
    ):
        self.model = model
        self.api_key = api_key or settings.openai_api_key
        self.base_url = base_url or settings.openai_base_url
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def provider(self) -> str:
        return "openai"

    @staticmethod
    def _encode_system(system_prompt: str | list[PromptBlock] | None) -> str:
        """把中立 system 描述编成 OpenAI 的纯文本 system 消息内容。

        OpenAI 无 prompt cache 断点概念,cache_breakpoint 标志直接忽略,
        多块按块间空行拼接成纯文本(与 Anthropic 侧块拼接语义一致)。
        """
        if system_prompt is None:
            return ""
        if isinstance(system_prompt, str):
            return system_prompt
        return "\n\n".join(block.text for block in system_prompt)

    @staticmethod
    def _encode_tools(tools: list[ToolSpec] | None) -> list[ChatCompletionFunctionToolParam]:
        """把中立工具描述编成 OpenAI 嵌套的 function tool 形状。"""
        return [
            ChatCompletionFunctionToolParam(
                type="function",
                function={
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            )
            for spec in (tools or [])
        ]

    @staticmethod
    def _encode_messages(messages: list[dict[str, Any]]) -> list[ChatCompletionMessageParam]:
        """把中立消息编成 OpenAI wire 形状(出站翻译,Client 私有)。

        三类中立角色的映射:
        - user:纯文本,原样成为 user 消息;
        - assistant:文本进 content,工具调用块进 tool_calls 数组(与 Anthropic
          「工具调用是 content 里的一个块」形状不同);仅有工具调用而无文本时
          content 置 None(OpenAI 允许 null,且不能省略该键);
        - tool:每个工具结果一条独立的 role="tool" 消息,以 tool_call_id 与调用
          配对——**不像** Anthropic 那样合并成块塞进 user 消息。

        多个文本块拼接成单段:OpenAI 的 assistant content 是单一字符串,
        无多块概念(build_assistant_blocks 本就最多产一个文本块)。
        ToolResultMessage 的 tool_name / input_args 是 AgentOS 自有排障字段,
        OpenAI 协议无对应位置,不发给模型。
        """
        encoded: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "user":
                encoded.append({"role": "user", "content": parse_user_text(content)})
            elif role == "assistant":
                blocks = parse_assistant_blocks(content)
                text = "".join(block.text for block in blocks if isinstance(block, TextBlock))
                tool_calls = _encode_tool_calls(
                    [block for block in blocks if isinstance(block, ToolUseBlock)]
                )
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": text if text or not tool_calls else None,
                }
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                encoded.append(assistant)
            elif role == "tool":
                encoded.append(_encode_tool_result(parse_tool_result(content)))
            else:
                raise AgentException.message("未知的消息角色", {"role": role})
        # 接口层用 dict[str, Any] 承载动态 JSON,此处收窄为 SDK 的 TypedDict。
        return cast("list[ChatCompletionMessageParam]", encoded)

    async def complete_structured[T: BaseModel](
        self,
        messages: list[dict[str, Any]],
        output_format: type[T],
        *,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int = 256,
        timeout: float | None = None,
    ) -> T:
        """使用 OpenAI Structured Outputs API。"""
        effective_timeout = timeout if timeout is not None else self.timeout
        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.max_retries,
            # 显式 None 会被 SDK 视为「禁用超时」,转 NOT_GIVEN 保留 SDK 默认超时。
            timeout=effective_timeout if effective_timeout is not None else NOT_GIVEN,
        )

        # 构造消息（OpenAI 要求 system 是首条消息）
        openai_messages: list[dict[str, Any]] = []
        if system_prompt:
            openai_messages.append({"role": "system", "content": system_prompt})
        openai_messages.extend(messages)

        # 调用 parse() 方法（Structured Outputs）
        response = await client.chat.completions.parse(
            model=model or self.model,
            # 接口层统一用 dict[str, Any] 承载动态 JSON，此处收窄为 SDK 的 TypedDict。
            messages=cast("list[ChatCompletionMessageParam]", openai_messages),
            response_format=output_format,
            max_completion_tokens=max_tokens,
            temperature=0,
        )

        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError(
                f"结构化输出缺失，finish_reason={response.choices[0].finish_reason}"
            )
        return parsed

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | list[PromptBlock] | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """流式调用,产出中立 LLMStreamEvent。

        走 create(stream=True) 裸 chunk 路径而非 .stream() helper:后者对每个
        工具强制 strict=True 校验(strict 模式倒逼改全部工具 schema,且与
        Anthropic 侧不对称),且其 get_final_completion() 在 finish_reason 为
        length / content_filter 时抛异常取代返回值。裸 chunk 经模块私有
        _OpenAIChunkAdapter 消化,原始格式不外露;流以恰好一个 TURN_RESULT
        收尾(含终局文本、工具调用与用量)。

        与 Anthropic 侧的结构差异:system 不是顶层参数,包成 messages 首条
        role="system" 消息(空串时不插入);终局由适配器自累积产出。
        """
        client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            max_retries=self.max_retries,
            # 客户端级 timeout 显式传 None 会被 SDK 视为「禁用超时」,
            # 此处转成 NOT_GIVEN 以保留 SDK 的默认超时计算。
            timeout=self.timeout if self.timeout is not None else NOT_GIVEN,
        )

        openai_messages: list[dict[str, Any]] = []
        system_text = self._encode_system(system_prompt)
        if system_text:
            openai_messages.append({"role": "system", "content": system_text})
        openai_messages.extend(self._encode_messages(messages))

        request_kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": openai_messages,
            "max_completion_tokens": settings.llm_default_max_tokens,
            "stream": True,
            # 不显式开启则 usage 恒为 None(SDK 不会自动注入该选项)。
            "stream_options": {"include_usage": True},
        }
        # 空 tools 不传该参数:部分兼容网关对空 tools: [] 报 400。
        if tools:
            request_kwargs["tools"] = self._encode_tools(tools)

        # 适配器生命周期即单次调用:流开始创建,结束即弃。
        adapter = _OpenAIChunkAdapter()
        # async with 保证消费方提前中断(用户取消、Agent 提前收束)时底层 HTTP 连接
        # 被显式关闭而非留给 GC;否则解释器回收异步生成器时会抛
        # "generator didn't stop after athrow()"。Anthropic 侧由 messages.stream()
        # 的上下文管理器承担同一职责。
        async with await client.chat.completions.create(**request_kwargs) as raw_stream:
            async for chunk in raw_stream:
                chunk_dict = (
                    chunk.model_dump(mode="json", warnings=False)
                    if hasattr(chunk, "model_dump")
                    else cast("dict[str, Any]", chunk)
                )
                for neutral_event in adapter.adapt(chunk_dict):
                    yield neutral_event
        for neutral_event in adapter.finalize():
            yield neutral_event
