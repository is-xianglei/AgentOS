import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from anthropic import NOT_GIVEN, AsyncAnthropic, NotGiven
from anthropic.types import MessageParam, TextBlockParam, ToolChoiceToolParam, ToolParam
from pydantic import BaseModel, ValidationError

from core.config import settings
from core.errors import AgentException
from core.events import BlockType, ToolInfo, Usage
from llm.base import BaseLLMClient
from llm.types import (
    AssistantBlock,
    LLMEventKind,
    LLMStreamEvent,
    LLMTurnResult,
    PromptBlock,
    TextBlock,
    ThinkingBlock,
    ThinkingSegment,
    ToolResultMessage,
    ToolSpec,
    ToolUse,
    parse_assistant_blocks,
    parse_tool_result,
    parse_user_text,
)

logger = logging.getLogger(__name__)

# 承载 schema 的工具名，语义中立，避免模型将其误解为业务动作。
_STRUCTURED_TOOL_NAME = "emit_structured_output"

# Anthropic 对思考预算的硬下限,低于此值 API 直接报错。
_MIN_THINKING_BUDGET = 1024


def _thinking_param() -> dict[str, Any] | NotGiven:
    """按配置产出 messages.stream() 的 thinking 参数,关闭时返回 NOT_GIVEN。

    显式传 None 会被 SDK 当作有效取值下发,故沿用超时处的 NOT_GIVEN 写法。
    预算越界当场抛中文异常而不夹取:静默夹取会让实际思考深度与配置不符,
    成本与效果都对不上账。
    """
    if not settings.anthropic_thinking_enabled:
        return NOT_GIVEN
    budget = settings.anthropic_thinking_budget_tokens
    if not _MIN_THINKING_BUDGET <= budget < settings.llm_default_max_tokens:
        raise AgentException.message(
            "思考预算配置越界",
            {
                "budget_tokens": budget,
                "min": _MIN_THINKING_BUDGET,
                "max_tokens": settings.llm_default_max_tokens,
            },
        )
    return {"type": "enabled", "budget_tokens": budget}

# redacted_thinking 归到 THINKING:它是被加密的思考块,对外协议语义与思考一致,
# 只是无可读文本。漏登记会走静默兜底当成文本块,终局提取时被丢掉,
# 回传给 Anthropic 时思考块缺失直接 400。
_BLOCK_TYPE_MAP = {
    "text": BlockType.TEXT,
    "thinking": BlockType.THINKING,
    "redacted_thinking": BlockType.THINKING,
    "tool_use": BlockType.TOOL_USE,
}


def _chunk_type(chunk: dict[str, Any]) -> str | None:
    """读取 Anthropic 原始事件的顶层类型。"""
    value = chunk.get("type")
    return value if isinstance(value, str) else None


def _chunk_text(chunk: dict[str, Any]) -> str:
    """从原始 content_block_delta 中取出文本增量,非文本事件返回空串。"""
    delta = chunk.get("delta")
    if (
        isinstance(delta, dict)
        and delta.get("type") == "text_delta"
        and isinstance(delta.get("text"), str)
    ):
        return delta["text"]
    return ""


def _extract_tool_uses(content: list[dict[str, Any]] | None) -> list[ToolUse]:
    """从完整消息内容中提取 tool_use 块,返回类型化的 ToolUse 列表。"""
    return [
        ToolUse(id=block["id"], name=block["name"], input=block.get("input") or {})
        for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]


def _extract_thinking(content: list[dict[str, Any]] | None) -> list[ThinkingSegment]:
    """从完整消息内容中按序提取思考段(含 redacted_thinking)。

    保序是硬约束:Anthropic 要求带签名的思考块原样回传,不得修改、重排或过滤,
    同一回合的多段思考签名互不相同,合并成一段即签名失效。
    """
    segments: list[ThinkingSegment] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking":
            segments.append(
                ThinkingSegment(
                    text=block.get("thinking", "") or "",
                    signature=block.get("signature") or None,
                )
            )
        elif block.get("type") == "redacted_thinking":
            segments.append(ThinkingSegment(redacted_data=block.get("data") or None))
    return segments


def _text_from_content(content: list[dict[str, Any]] | None) -> str:
    """从完整消息内容中拼接文本块。"""
    return "".join(
        block.get("text", "")
        for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _encode_assistant_block(block: AssistantBlock) -> dict[str, Any] | None:
    """把 assistant 中立块编成 Anthropic wire 块,不可回传的思考块返回 None。

    无签名的思考块必须跳过:签名由 Anthropic 签发且不可伪造,回传缺签名的
    思考块会被判为篡改。切换 provider 后库里会留下 OpenAI 侧产出的无签名思考,
    此处丢弃即可——它们只用于展示,不参与 Anthropic 的思考完整性校验。
    """
    if isinstance(block, ThinkingBlock):
        if block.redacted_data:
            return {"type": "redacted_thinking", "data": block.redacted_data}
        if not block.signature:
            return None
        return {"type": "thinking", "thinking": block.text, "signature": block.signature}
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    return {
        "type": "tool_use",
        "id": block.id,
        "name": block.name,
        "input": block.input,
    }


def _encode_tool_result(tool_result: ToolResultMessage) -> dict[str, Any]:
    """把中立工具结果编成 Anthropic 的 tool_result 块。

    is_error 仅在为真时下发:Anthropic 该字段缺省即 false,恒发会让 wire 变噪。
    """
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_result.tool_use_id,
        "content": tool_result.output,
    }
    if tool_result.is_error:
        block["is_error"] = True
    return block


def _is_tool_result_carrier(message: dict[str, Any]) -> bool:
    """判断某条已编码消息是否为可继续追加 tool_result 的 user 消息。"""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and isinstance(content[0], dict)
        and content[0].get("type") == "tool_result"
    )


@dataclass
class _OpenBlock:
    """适配过程中正在累积的内容块状态。"""

    index: int
    block_type: BlockType
    tool_id: str = ""
    tool_name: str = ""
    json_buffer: str = ""


class _AnthropicChunkAdapter:
    """把 Anthropic 原始 chunk 适配成中立 LLMStreamEvent 序列(模块私有)。

    生命周期为单次 stream() 调用:流开始时创建,结束即弃。
    block_ref 直接沿用 Anthropic 原始 index(调用内本就唯一),
    跨调用全局化由 runtime 侧 TurnEventAssembler 负责。
    tool_use 的入参 JSON 在 content_block_stop 时累积完整后一次性产出;
    合成的 message_final 转为 TURN_RESULT 终结事件。
    """

    def __init__(self) -> None:
        self._open: dict[int, _OpenBlock] = {}

    def adapt(self, chunk: dict[str, Any]) -> list[LLMStreamEvent]:
        """适配单个原始 chunk 为零或多个中立事件。"""
        ctype = _chunk_type(chunk)
        if ctype == "content_block_start":
            return self._on_block_start(chunk)
        if ctype == "content_block_delta":
            return self._on_block_delta(chunk)
        if ctype == "content_block_stop":
            return self._on_block_stop(chunk)
        if ctype == "message_final":
            return self._on_message_final(chunk)
        # ping / message_start / message_delta / signature 等噪声不产出事件。
        return []

    def _on_block_start(self, chunk: dict[str, Any]) -> list[LLMStreamEvent]:
        index = int(chunk.get("index", 0))
        cb = chunk.get("content_block") or {}
        raw_type = cb.get("type", "text")
        block_type = _BLOCK_TYPE_MAP.get(raw_type)
        if block_type is None:
            # 无静默兜底:未知块兜成 TEXT 会让内容以错误类型流向前端,
            # 且终局提取按 wire type 过滤时被整块丢掉,故障要在此当场暴露。
            raise AgentException.message(
                "未知的 Anthropic 内容块类型", {"block_type": raw_type}
            )
        self._open[index] = _OpenBlock(
            index=index,
            block_type=block_type,
            tool_id=cb.get("id", "") or "",
            tool_name=cb.get("name", "") or "",
        )
        return [
            LLMStreamEvent(kind=LLMEventKind.BLOCK_START, block_ref=index, block_type=block_type)
        ]

    def _on_block_delta(self, chunk: dict[str, Any]) -> list[LLMStreamEvent]:
        index = int(chunk.get("index", 0))
        delta = chunk.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            return [
                LLMStreamEvent(
                    kind=LLMEventKind.TEXT_DELTA, block_ref=index, text=_chunk_text(chunk)
                )
            ]
        if dtype == "thinking_delta":
            return [
                LLMStreamEvent(
                    kind=LLMEventKind.THINKING_DELTA,
                    block_ref=index,
                    text=delta.get("thinking", ""),
                )
            ]
        if dtype == "input_json_delta":
            # 累积 tool_use 的入参 JSON,等 block_stop 一次性产出。
            block = self._open.get(index)
            if block is not None:
                block.json_buffer += delta.get("partial_json", "")
            return []
        # signature_delta 等:噪声,不产出。
        return []

    def _on_block_stop(self, chunk: dict[str, Any]) -> list[LLMStreamEvent]:
        index = int(chunk.get("index", 0))
        block = self._open.pop(index, None)
        if block is None:
            # 从没开过的块:原样透传 stop,由装配器判定丢弃并留日志。
            return [LLMStreamEvent(kind=LLMEventKind.BLOCK_STOP, block_ref=index)]
        if block.block_type is BlockType.TOOL_USE:
            # tool_use 块:入参累积完毕,一次性产出完整 tool_use 事件;
            # 协议层的 block_stop 配对由装配器补发,provider 侧不参与协议决策。
            try:
                tool_input = json.loads(block.json_buffer) if block.json_buffer else {}
            except json.JSONDecodeError as exc:
                # 无静默兜底:兜成 {} 会让工具带空参执行,失败原因在链路上彻底消失。
                raise AgentException.message(
                    "工具调用入参 JSON 解析失败",
                    {"tool_name": block.tool_name, "tool_use_id": block.tool_id},
                ) from exc
            tool = ToolInfo(id=block.tool_id, name=block.tool_name, input=tool_input)
            return [LLMStreamEvent(kind=LLMEventKind.TOOL_USE, block_ref=index, tool=tool)]
        return [LLMStreamEvent(kind=LLMEventKind.BLOCK_STOP, block_ref=index)]

    def _on_message_final(self, chunk: dict[str, Any]) -> list[LLMStreamEvent]:
        """把合成的 message_final 组装成 TURN_RESULT 终结事件。"""
        usage_dict = chunk.get("usage")
        usage: Usage | None = None
        if isinstance(usage_dict, dict):
            usage = Usage(
                input_tokens=int(usage_dict.get("input_tokens", 0) or 0),
                output_tokens=int(usage_dict.get("output_tokens", 0) or 0),
                cache_creation_input_tokens=int(
                    usage_dict.get("cache_creation_input_tokens", 0) or 0
                ),
                cache_read_input_tokens=int(usage_dict.get("cache_read_input_tokens", 0) or 0),
            )
        content = chunk.get("content") or []
        result = LLMTurnResult(
            text=_text_from_content(content),
            tool_uses=_extract_tool_uses(content),
            thinking=_extract_thinking(content),
            usage=usage,
            stop_reason=chunk.get("stop_reason"),
        )
        return [LLMStreamEvent(kind=LLMEventKind.TURN_RESULT, result=result)]


class AnthropicClient(BaseLLMClient):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float | None = None,
    ):
        self.model = model
        self.api_key = api_key or settings.anthropic_api_key
        self.base_url = base_url or settings.anthropic_base_url
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def provider(self) -> str:
        return "anthropic"

    @staticmethod
    def _encode_system(
        system_prompt: str | list[PromptBlock] | None,
    ) -> str | list[TextBlockParam]:
        """把中立 system 描述编成 Anthropic SDK 形状。

        cache_breakpoint=True 落成 cache_control={"type": "ephemeral"}，默认 5m TTL
        （turn 内迭代是密集调用，5m 足够，避免 1h 的 2 倍写溢价）。渲染顺序为
        tools → system → messages，故断点落在稳定段末尾时会连 tools 数组一起缓存。
        str 形态（子代理整段纯文本）在此归一化，原样透传。
        """
        if system_prompt is None:
            return ""
        if isinstance(system_prompt, str):
            return system_prompt
        blocks: list[TextBlockParam] = []
        for block in system_prompt:
            encoded: TextBlockParam = {"type": "text", "text": block.text}
            if block.cache_breakpoint:
                encoded["cache_control"] = {"type": "ephemeral"}
            blocks.append(encoded)
        return blocks

    @staticmethod
    def _encode_tools(tools: list[ToolSpec] | None) -> list[ToolParam]:
        """把中立工具描述编成 Anthropic 平铺的 ToolParam 形状。"""
        return [
            ToolParam(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
            )
            for spec in (tools or [])
        ]

    @staticmethod
    def _encode_messages(messages: list[dict[str, Any]]) -> list[MessageParam]:
        """把中立消息编成 Anthropic wire 形状(出站翻译,Client 私有)。

        三类中立角色的映射:
        - user:纯文本,原样成为 user 消息;
        - assistant:中立块数组编成 text / tool_use 块;空块数组落成空串
          (Anthropic 拒绝空 content 数组);
        - tool:编成 tool_result 块塞进 role="user" 消息——Anthropic 没有 tool 角色。
          连续的工具结果必须合并进同一条 user 消息,否则会出现连续 user 消息而
          违反角色交替约束(该合并逻辑原在 session/service.py,随出站翻译一并迁入)。

        ToolResultMessage 的 tool_name / input_args 是 AgentOS 自有的排障字段,
        Anthropic 协议无对应位置,不发给模型。
        """
        encoded: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "user":
                encoded.append({"role": "user", "content": parse_user_text(content)})
            elif role == "assistant":
                # 原样遍历不重排:思考块的落库序即 Anthropic 要求的回传序。
                blocks = [
                    encoded_block
                    for block in parse_assistant_blocks(content)
                    if (encoded_block := _encode_assistant_block(block)) is not None
                ]
                encoded.append({"role": "assistant", "content": blocks or ""})
            elif role == "tool":
                block = _encode_tool_result(parse_tool_result(content))
                if encoded and _is_tool_result_carrier(encoded[-1]):
                    encoded[-1]["content"].append(block)
                else:
                    encoded.append({"role": "user", "content": [block]})
            else:
                raise AgentException.message("未知的消息角色", {"role": role})
        # 接口层用 dict[str, Any] 承载动态 JSON,此处收窄为 SDK 的 TypedDict。
        return cast("list[MessageParam]", encoded)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | list[PromptBlock] | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """流式调用,产出中立 LLMStreamEvent。system_prompt 传块列表时可携带 cache 断点。

        中立入参经私有 _encode_system / _encode_tools 编成 SDK 形状;
        SDK 原始事件在内部经 _AnthropicChunkAdapter 消化,不外露;
        流末尾以恰好一个 TURN_RESULT 事件收尾(含终局文本、工具调用与用量)。
        model 留空时回落构造时由工厂按场景解析的模型,供 SubAgent 按类型指定
        档位(如 Explore 走速度优先档)。别名与云厂商区域前缀原样透传,
        不做改写,避免继承时丢前缀。

        扩展思考由 anthropic_thinking_enabled 控制,默认关。
        """

        client = AsyncAnthropic(
            base_url=self.base_url,
            api_key=self.api_key,
            max_retries=self.max_retries,
            # 客户端级 timeout 显式传 None 会被 SDK 视为「禁用超时」,
            # 此处转成 NOT_GIVEN 以保留 SDK 的默认超时计算。
            timeout=self.timeout if self.timeout is not None else NOT_GIVEN,
        )

        # 适配器生命周期即单次调用:流开始创建,结束即弃。
        adapter = _AnthropicChunkAdapter()
        async with client.messages.stream(
            max_tokens=settings.llm_default_max_tokens,
            thinking=_thinking_param(),
            model=model or self.model,
            system=self._encode_system(system_prompt),
            messages=self._encode_messages(messages),
            tools=self._encode_tools(tools),
        ) as stream:
            async for event in stream:
                chunk = (
                    event.model_dump(mode="json", warnings=False)
                    if hasattr(event, "model_dump")
                    else {"type": str(event)}
                )
                for neutral_event in adapter.adapt(chunk):
                    yield neutral_event

            final = await stream.get_final_message()

            usage = None
            if hasattr(final, "usage") and final.usage is not None:
                usage = final.usage.model_dump(mode="json", warnings=False)
                self._log_cache_usage(usage)
            for neutral_event in adapter.adapt(
                {
                    "type": "message_final",
                    "stop_reason": final.stop_reason,
                    "usage": usage,
                    "content": [
                        block.model_dump(mode="json", warnings=False) for block in final.content
                    ],
                }
            ):
                yield neutral_event

    @staticmethod
    def _log_cache_usage(usage: dict[str, Any]) -> None:
        """记录 prompt cache 命中情况。

        写入按 1.25 倍计费、读取按 0.1 倍，故 write 常态不为 0 说明前缀在被反复
        击穿（多为易失内容排在了稳定内容之前）。低于模型最小可缓存长度时不报错，
        只是两项都为 0。
        """
        write = usage.get("cache_creation_input_tokens") or 0
        read = usage.get("cache_read_input_tokens") or 0
        if not write and not read:
            return
        logger.info(
            "prompt cache 使用情况: 写入=%d 读取=%d 未缓存=%d",
            write,
            read,
            usage.get("input_tokens") or 0,
            extra={"cache_write": write, "cache_read": read},
        )

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
        """用 tool use 强制结构化输出，校验失败时回灌错误重试一次。

        相比 complete_structured：结果落在独立的 tool_use block，模型的思考与寒暄留在
        text block，二者互不干扰，因此不依赖端点是否实现 output_config。tool use 自
        2024 年 GA，第三方中转普遍支持。校验由 Pydantic 在客户端完成。
        """
        client = AsyncAnthropic(
            base_url=self.base_url,
            api_key=self.api_key,
            max_retries=self.max_retries,
            # 同 stream():None 转 NOT_GIVEN,保留 SDK 默认超时而非禁用超时。
            timeout=self.timeout if self.timeout is not None else NOT_GIVEN,
        )
        # Anthropic 的 input_schema 接受标准 JSON Schema，含 $defs/$ref 的嵌套模型可直接使用。
        tool: ToolParam = {
            "name": _STRUCTURED_TOOL_NAME,
            "description": "提交本次任务的结构化结果，所有字段必须严格符合 input_schema。",
            "input_schema": output_format.model_json_schema(),
            # 要求服务端将工具入参也纳入约束解码；不支持该字段的端点会忽略它，
            # 降级为普通 tool use 而不会报错。
            "strict": True,
        }
        conversation = list(messages)
        last_error: ValidationError | None = None

        # 首轮 + 一次回灌重试：把 ValidationError 原文交回模型，让它自行修正字段。
        for attempt in range(2):
            response = await client.messages.create(
                max_tokens=max_tokens,
                model=model or self.model,
                system=system_prompt or "",
                messages=cast("list[MessageParam]", conversation),
                tools=[tool],
                # 指定工具名即强制调用，模型无法改调其他工具或只返回文本。
                tool_choice=ToolChoiceToolParam(type="tool", name=_STRUCTURED_TOOL_NAME),
                temperature=0,
                # 显式 None 会被 SDK 视为"已给定"从而禁用超时，
                # 此处转成 NOT_GIVEN 以保留非流式超时的自动计算。
                timeout=timeout if timeout is not None else NOT_GIVEN,
            )
            tool_block = next(
                (
                    block
                    for block in response.content
                    if block.type == "tool_use" and block.name == _STRUCTURED_TOOL_NAME
                ),
                None,
            )
            if tool_block is None:
                # 无 tool_use block（如被 max_tokens 截断或模型拒答），与校验失败同归 ValueError。
                raise ValueError(f"结构化输出缺失，stop_reason={response.stop_reason}")

            try:
                # block.input 已由 SDK 反序列化为 dict，无需再次 json.loads。
                return output_format.model_validate(tool_block.input)
            except ValidationError as exc:
                last_error = exc
                if attempt == 1:
                    break
                logger.warning(
                    "结构化输出校验失败，回灌错误重试，模型=%s 目标类型=%s 错误数=%d",
                    model or self.model,
                    output_format.__name__,
                    exc.error_count(),
                )
                # 必须回放 assistant 的 tool_use 轮，再以 tool_result 承载错误，
                # 否则会出现连续 user 消息而违反角色交替约束。
                conversation = [
                    *conversation,
                    {
                        "role": "assistant",
                        "content": [tool_block.model_dump(mode="json", warnings=False)],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_block.id,
                                "is_error": True,
                                "content": (
                                    "参数未通过 schema 校验，请修正后重新调用该工具。"
                                    f"错误详情：{exc}"
                                ),
                            }
                        ],
                    },
                ]

        # 重试后仍不合法，抛出最后一次 ValidationError 以保留字段级错误信息。
        raise last_error  # type: ignore[misc]
