import logging
from collections.abc import AsyncIterator
from typing import Any
from warnings import deprecated

from anthropic import NOT_GIVEN, AsyncAnthropic
from anthropic.types import TextBlockParam, ToolParam
from pydantic import BaseModel, ValidationError

from core.config import settings
from llm.types import LLMRawChunk

logger = logging.getLogger(__name__)

# 承载 schema 的工具名，语义中立，避免模型将其误解为业务动作。
_STRUCTURED_TOOL_NAME = "emit_structured_output"


class LLMClient:
    def __init__(self):
        self.model = settings.anthropic_model
        self.api_key = settings.anthropic_api_key
        self.base_url = settings.anthropic_base_url

    async def stream(
        self,
        messages: list[dict],
        system_prompt: str | list[TextBlockParam] | None = None,
        tools: list[ToolParam] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMRawChunk]:
        """流式调用。system_prompt 传 TextBlockParam 列表时可携带 cache 断点。

        model 留空时回落主会话模型,供 SubAgent 按类型指定档位(如 Explore 走
        速度优先档)。别名与云厂商区域前缀原样透传,不做改写,避免继承时丢前缀。
        """

        client = AsyncAnthropic(base_url=self.base_url, api_key=self.api_key)

        async with client.messages.stream(
            max_tokens=131072,
            model=model or self.model,
            system=system_prompt or "",
            messages=messages,
            tools=tools or [],
        ) as stream:
            async for event in stream:
                yield (
                    event.model_dump(mode="json", warnings=False)
                    if hasattr(event, "model_dump")
                    else {"type": str(event)}
                )

            final = await stream.get_final_message()

            usage = None
            if hasattr(final, "usage") and final.usage is not None:
                usage = final.usage.model_dump(mode="json", warnings=False)
                self._log_cache_usage(usage)
            yield {
                "type": "message_final",
                "stop_reason": final.stop_reason,
                "usage": usage,
                "content": [
                    block.model_dump(mode="json", warnings=False)
                    for block in final.content
                ],
            }

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

    @deprecated(
        "依赖服务端 structured outputs，第三方中转端点普遍不实现 output_config 且静默丢弃该字段，"
        "导致模型退回自由文本、SDK 对整段 text 做 validate_json 必然失败。"
        "请改用 complete_structured_by_tool。"
    )
    async def _complete_structured[T: BaseModel](
        self,
        messages: list[dict[str, Any]],
        output_format: type[T],
        *, # * 是仅限关键字参数（keyword-only）的分隔符：它自己不接收任何值，只标记"从这里往后的参数，调用时必须写参数名"。
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int = 256,
        timeout: float | None = None,
    ) -> T:
        """已废弃：走服务端 structured outputs，仅在端点真实支持 output_config 时可用。

        废弃原因：该实现把 schema 约束下放给服务端做约束解码，SDK 侧只对返回的
        text block 整段执行 validate_json，且不校验服务端是否真的生效。第三方中转
        （如 new-api/one-api 系）会在反序列化阶段静默丢弃 output_config，模型于是按
        普通对话作答，返回带 <thinking> 与 markdown 围栏的文本，解析必然在首字符失败。
        官方 API、Bedrock、Vertex 等真实实现该能力的端点仍可正常使用，故保留不删。
        """
        client = AsyncAnthropic(base_url=self.base_url, api_key=self.api_key)
        response = await client.messages.parse(
            max_tokens=max_tokens,
            model=model or self.model,
            system=system_prompt or "",
            messages=messages,
            output_format=output_format,
            temperature=0,
            # 显式 None 会被 SDK 视为"已给定"从而禁用超时，
            # 此处转成 NOT_GIVEN 以保留非流式超时的自动计算。
            timeout=timeout if timeout is not None else NOT_GIVEN,
        )
        parsed = response.parsed_output
        if parsed is None:
            # 无 text block（如被 max_tokens 截断或模型拒答），与校验失败同归 ValueError。
            raise ValueError(f"结构化输出缺失，stop_reason={response.stop_reason}")
        return parsed

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
        client = AsyncAnthropic(base_url=self.base_url, api_key=self.api_key)
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
                messages=conversation,
                tools=[tool],
                # 指定工具名即强制调用，模型无法改调其他工具或只返回文本。
                tool_choice={"type": "tool", "name": _STRUCTURED_TOOL_NAME},
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
