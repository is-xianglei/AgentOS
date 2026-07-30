"""complete_structured_by_tool 的传输层测试：请求形状、回灌重试与失败路径。"""

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from llm.client import _STRUCTURED_TOOL_NAME, LLMClient


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    memory_key: str
    type: str


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    memories: list[Candidate]


def _message(content: list[dict], model: str, stop_reason: str = "tool_use") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "content": content,
    }


def _tool_use(payload: dict, block_id: str = "toolu_1") -> dict:
    return {
        "type": "tool_use",
        "id": block_id,
        "name": _STRUCTURED_TOOL_NAME,
        "input": payload,
    }


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """拦在 httpx 传输层，记录每次请求体，避免真实网络调用。"""
    seen: list[dict] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return handler(len(seen) - 1, body)

    real_init = LLMClient.__init__

    def patched_init(self) -> None:
        real_init(self)
        self.model = self.model or "claude-sonnet-4-6"
        self.api_key = self.api_key or "test-key"

    monkeypatch.setattr(LLMClient, "__init__", patched_init)
    monkeypatch.setattr(
        "llm.client.AsyncAnthropic",
        lambda **kwargs: __import__("anthropic").AsyncAnthropic(
            base_url=kwargs.get("base_url") or "https://example.invalid",
            api_key=kwargs.get("api_key") or "test-key",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)),
        ),
    )
    return seen


def test_首轮成功并正确构造请求(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"memories": [{"memory_key": "reply-in-chinese", "type": "feedback"}]}
    seen = _patch_transport(
        monkeypatch,
        lambda index, body: httpx.Response(
            200,
            json=_message(
                [
                    {"type": "text", "text": "\n<thinking>思考内容</thinking>"},
                    _tool_use(payload),
                ],
                body["model"],
            ),
        ),
    )

    result = asyncio.run(
        LLMClient().complete_structured_by_tool(
            [{"role": "user", "content": "提取"}],
            Result,
            system_prompt="你是提取器",
            max_tokens=1024,
        )
    )

    assert isinstance(result, Result)
    assert result.memories[0].memory_key == "reply-in-chinese"
    assert len(seen) == 1
    request = seen[0]
    # 关键：不再发送 output_config，改用 tools + tool_choice + strict。
    assert "output_config" not in request
    assert request["tool_choice"] == {"type": "tool", "name": _STRUCTURED_TOOL_NAME}
    assert request["tools"][0]["strict"] is True
    assert request["tools"][0]["input_schema"]["properties"]["memories"]
    assert request["temperature"] == 0


def test_校验失败回灌一次后成功(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {"memories": [{"memory_key": "k", "memory_type": "feedback"}]}
    good = {"memories": [{"memory_key": "k", "type": "feedback"}]}

    def handler(index: int, body: dict) -> httpx.Response:
        payload = bad if index == 0 else good
        return httpx.Response(
            200, json=_message([_tool_use(payload, f"toolu_{index}")], body["model"])
        )

    seen = _patch_transport(monkeypatch, handler)

    result = asyncio.run(
        LLMClient().complete_structured_by_tool([{"role": "user", "content": "提取"}], Result)
    )

    assert result.memories[0].type == "feedback"
    assert len(seen) == 2
    # 第二轮必须回放 assistant 的 tool_use 轮，再以 tool_result 承载错误。
    retry_messages = seen[1]["messages"]
    assert retry_messages[1]["role"] == "assistant"
    assert retry_messages[1]["content"][0]["type"] == "tool_use"
    result_block = retry_messages[2]["content"][0]
    assert retry_messages[2]["role"] == "user"
    assert result_block["type"] == "tool_result"
    assert result_block["is_error"] is True
    assert result_block["tool_use_id"] == "toolu_0"
    assert "memory_type" in result_block["content"]


def test_重试仍失败抛出校验错误(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {"memories": [{"memory_key": "k", "memory_type": "feedback"}]}
    seen = _patch_transport(
        monkeypatch,
        lambda index, body: httpx.Response(
            200, json=_message([_tool_use(bad, f"toolu_{index}")], body["model"])
        ),
    )

    with pytest.raises(ValidationError):
        asyncio.run(
            LLMClient().complete_structured_by_tool([{"role": "user", "content": "提取"}], Result)
        )

    # 只重试一次，总共两次请求。
    assert len(seen) == 2


def test_缺少工具块抛出值错误(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(
        monkeypatch,
        lambda index, body: httpx.Response(
            200,
            json=_message(
                [{"type": "text", "text": "被截断"}], body["model"], stop_reason="max_tokens"
            ),
        ),
    )

    with pytest.raises(ValueError, match="结构化输出缺失"):
        asyncio.run(
            LLMClient().complete_structured_by_tool(
                [{"role": "user", "content": "提取"}], Result, max_tokens=1
            )
        )
