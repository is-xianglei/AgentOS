"""模型服务业务规则与工作区隔离。"""

import asyncio
import io
import logging
import math
import time
import wave
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from llm.base import BaseLLMClient
from llm.errors import error_type_name
from model_service.models import ModelProviderRecord, ModelRoleRecord
from model_service.policy import (
    MODES,
    fast_capabilities,
    normalize_effort,
    reasoning_capabilities,
    validate_reasoning_config,
)
from model_service.repository import ModelServiceRepository
from model_service.schemas import (
    ModelProbeRequest,
    ModelProviderInput,
    ModelProviderResponse,
    ModelProviderUpdate,
    ModelTestResponse,
)
from user.service import UserService

ROLE_LABELS = {
    "agent": "主智能体",
    "subagent": "子智能体",
    "subagent_fast": "子智能体（快速）",
    "compact": "上下文压缩",
    "memory_selector": "记忆检索",
    "memory_extractor": "记忆提取",
    "memory_dream": "记忆整理",
    "workflow": "工作流",
}
logger = logging.getLogger(__name__)
# 只声明项目现有适配器实际支持的协议，避免原生或云平台入口保存后才发现不能运行。
_PROVIDERS = [
    ("openai_compatible", "OpenAI 兼容", "https://api.openai.com/v1", False, True),
    ("openai", "OpenAI", "https://api.openai.com/v1", False, True),
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1", True, True),
    ("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", True, True),
    ("minimax", "MiniMax", "https://api.minimaxi.com/v1", True, True),
    ("siliconflow", "硅基流动", "https://api.siliconflow.cn/v1", True, True),
    ("moonshot", "Kimi / Moonshot", "https://api.moonshot.cn/v1", True, True),
    ("qwen_compat", "通义千问（兼容模式）", "https://dashscope.aliyuncs.com/compatible-mode/v1", True, True),
    ("anthropic", "Anthropic 原生", "https://api.anthropic.com", True, True),
    ("audio_compatible", "音频转写（OpenAI 兼容）", "", False, False),
]


def provider_schemas() -> list[dict[str, Any]]:
    return [
        {
            "id": key,
            "label": label,
            "engine": "native" if key == "anthropic" else "openai",
            "supports_types": (
                ["chat"]
                if key == "anthropic"
                else (
                    ["audio"]
                    if key == "audio_compatible"
                    else ["chat", "embedding", "reranker", "audio"]
                )
            ),
            "base_url_template": url,
            "autofill_base_url": autofill,
            "api_key_required": required,
        }
        for key, label, url, autofill, required in _PROVIDERS
    ]


def public_provider(record: ModelProviderRecord) -> ModelProviderResponse:
    data = {
        key: getattr(record, key)
        for key in ModelProviderResponse.model_fields
        if key != "api_key"
    }
    data["api_key"] = "********" if record.api_key else ""
    return ModelProviderResponse.model_validate(data)


class ModelService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ModelServiceRepository(db)

    async def require_provider(
        self,
        workspace_id: int,
        provider_id: str,
        *,
        chat: bool = False,
    ) -> ModelProviderRecord:
        record = await self.repo.get_provider(workspace_id, provider_id)
        if record is None or record.is_deleted:
            raise AgentException.message("模型供应商不存在或不属于当前工作区", status_code=404)
        if chat and (not record.is_active or record.provider_type != "chat"):
            raise AgentException.message("请选择已启用的对话模型", status_code=400)
        return record

    async def list_providers(self, workspace_id: int) -> list[ModelProviderResponse]:
        return [public_provider(row) for row in await self.repo.list_providers(workspace_id)]

    def _validate_provider(self, payload: ModelProviderInput, key: str) -> None:
        specs = {spec["id"]: spec for spec in provider_schemas()}
        spec = specs.get(payload.provider)
        if spec is None or payload.provider_type not in spec["supports_types"]:
            raise AgentException.message("不支持该供应商协议或用途", status_code=400)
        if not payload.display_name or not payload.model_name:
            raise AgentException.message("显示名称和模型名不能为空", status_code=400)
        if not payload.base_url:
            raise AgentException.message("模型服务地址不能为空", status_code=400)
        try:
            url = httpx.URL(payload.base_url)
            if payload.base_url and (
                url.scheme not in {"http", "https"}
                or not url.host
                or url.userinfo
                or url.query
                or url.fragment
            ):
                raise ValueError("invalid URL")
        except (httpx.InvalidURL, ValueError) as exc:
            raise AgentException.message(
                "模型服务地址须为无凭据、查询参数的 HTTP 或 HTTPS 地址", status_code=400
            ) from exc
        if spec["api_key_required"] and not key:
            raise AgentException.message("请填写模型服务密钥", status_code=400)
        if key.startswith("****"):
            raise AgentException.message("密钥掩码不能用作模型服务密钥", status_code=400)
        if payload.extra_config.get("api_protocol") not in {None, "chat_completions"}:
            raise AgentException.message(
                "当前支持 Chat Completions 或 Anthropic 原生消息协议", status_code=400
            )
        validate_reasoning_config(payload.extra_config, payload.provider_type)
        if payload.extra_config.get("thinking_protocol") not in {
            None, "openai", "deepseek", "chat_template",
        }:
            raise AgentException.message("不支持该思考参数协议", status_code=400)
        if payload.provider == "anthropic":
            from core.config import settings

            if "thinking_budget_tokens" in payload.extra_config:
                budget = payload.extra_config["thinking_budget_tokens"]
                if type(budget) is not int or not 1024 <= budget < settings.llm_default_max_tokens:
                    raise AgentException.message(
                        "Anthropic 思考预算须为至少1024且小于最大输出限制的整数",
                        status_code=400,
                    )
            values = [
                row["value"]
                for row in reasoning_capabilities(payload.extra_config)["reasoning_effort_levels"]
            ]
            if any(
                type(value) is int and not 1024 <= value < settings.llm_default_max_tokens
                for value in values
            ):
                raise AgentException.message(
                    "Anthropic 思考预算须至少为1024且小于最大输出限制",
                    status_code=400,
                )
        context = payload.extra_config.get("context_length")
        if context is not None and (type(context) is not int or context <= 0):
            raise AgentException.message("上下文窗口必须为正整数", status_code=400)

    async def save_provider(
        self,
        workspace_id: int,
        payload: ModelProviderInput | ModelProviderUpdate,
        provider_id: str | None = None,
    ) -> ModelProviderResponse:
        existing = await self.require_provider(workspace_id, provider_id) if provider_id else None
        if isinstance(payload, ModelProviderUpdate):
            if existing is None:
                raise AgentException.message("更新模型服务需要供应商 ID", status_code=400)
            values = {
                name: getattr(existing, name)
                for name in ModelProviderInput.model_fields
                if name != "api_key"
            }
            values.update(payload.model_dump(exclude_unset=True))
            payload = ModelProviderInput.model_validate(values)
        key = payload.api_key.get_secret_value().strip() if payload.api_key else ""
        key = key or (existing.api_key if existing else "")
        self._validate_provider(payload, key)
        if existing and (existing.provider_type != payload.provider_type or not payload.is_active):
            roles = [
                row.role_key
                for row in await self.repo.list_roles(workspace_id)
                if row.provider_id == provider_id
            ]
            if roles:
                raise AgentException.message(
                    "请先解除模型角色指派，再停用或修改用途",
                    {"roles": roles},
                    status_code=409,
                )
        values = payload.model_dump(exclude={"api_key"})
        values["base_url"] = values["base_url"].rstrip("/")
        values["api_key"] = key
        connection_fields = (
            "provider",
            "provider_type",
            "base_url",
            "api_key",
            "model_name",
            "extra_config",
        )
        needs_test = payload.is_active and (
            existing is None
            or not existing.is_active
            or any(getattr(existing, field) != values[field] for field in connection_fields)
        )
        candidate = ModelProviderRecord(workspace_id=workspace_id, **values)
        if needs_test:
            result = await self.ping(candidate)
            if not result.success:
                raise AgentException.message(
                    "模型服务连通性校验失败", {"error": result.error}, status_code=400
                )
            values["last_tested_at"] = datetime.now(UTC)
            values["last_test_status"] = "success"
        record = existing or candidate
        for name, value in values.items():
            setattr(record, name, value)
        await self.repo.save(record)
        return public_provider(record)

    async def delete_provider(self, workspace_id: int, provider_id: str) -> None:
        record = await self.require_provider(workspace_id, provider_id)
        roles = [
            row.role_key
            for row in await self.repo.list_roles(workspace_id)
            if row.provider_id == provider_id
        ]
        if roles:
            raise AgentException.message("该模型正在被角色引用，请先取消指派", {"roles": roles}, status_code=409)
        await self.repo.delete(record)

    async def ping(self, record: ModelProviderRecord) -> ModelTestResponse:
        started = time.perf_counter()
        try:
            if record.provider_type == "chat":
                client = self.build_client(record, "fast")
                client.max_tokens = 32
                result = None
                async with asyncio.timeout(30):
                    async with aclosing(
                        client.stream([{"role": "user", "content": "Reply OK"}])
                    ) as stream:
                        async for event in stream:
                            if event.kind.value == "turn_result":
                                result = event.result
                if result is None or not (result.text or result.tool_uses):
                    raise ValueError("模型流未产出有效终局结果")
            else:
                headers = {"Authorization": f"Bearer {record.api_key}"} if record.api_key else {}
                async with httpx.AsyncClient(timeout=20) as client:
                    if record.provider_type == "audio":
                        sample = io.BytesIO()
                        with wave.open(sample, "wb") as audio:
                            audio.setnchannels(1)
                            audio.setsampwidth(2)
                            audio.setframerate(16000)
                            audio.writeframes(bytes(32000))
                        response = await client.post(
                            f"{record.base_url}/audio/transcriptions",
                            headers=headers,
                            data={"model": record.model_name},
                            files={"file": ("probe.wav", sample.getvalue(), "audio/wav")},
                        )
                    else:
                        endpoint = "embeddings" if record.provider_type == "embedding" else "rerank"
                        body = (
                            {"model": record.model_name, "input": ["test"]}
                            if endpoint == "embeddings"
                            else {
                                "model": record.model_name,
                                "query": "test",
                                "documents": ["test"],
                            }
                        )
                        response = await client.post(
                            f"{record.base_url}/{endpoint}", headers=headers, json=body
                        )
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("上游响应须为对象")
                    if record.provider_type == "embedding":
                        rows = data.get("data")
                        vector = (
                            rows[0].get("embedding")
                            if isinstance(rows, list) and rows and isinstance(rows[0], dict)
                            else None
                        )
                        if not isinstance(vector, list) or not vector or any(
                            type(value) not in {int, float} or not math.isfinite(value)
                            for value in vector
                        ):
                            raise ValueError("上游未返回有效向量")
                    elif record.provider_type == "reranker":
                        rows = data.get("results")
                        if not isinstance(rows, list) or not rows or any(
                            not isinstance(row, dict)
                            or type(row.get("index")) is not int
                            or row["index"] != 0
                            or type(row.get("relevance_score")) not in {int, float}
                            or not math.isfinite(row["relevance_score"])
                            for row in rows
                        ):
                            raise ValueError("上游未返回有效重排结果")
                    elif not isinstance(data.get("text"), str):
                        raise ValueError("上游未返回转写结果")
            return ModelTestResponse(
                success=True, latency_ms=round((time.perf_counter() - started) * 1000)
            )
        except Exception as exc:
            # 上游异常可能包含密钥或连接串，只向客户端暴露分类后的安全信息。
            status = getattr(exc, "status_code", None)
            # 中立 LLM 异常取原始 SDK 类名,保持对外文案与日志字段口径不变。
            exception_type = error_type_name(exc)
            error = f"上游 HTTP {status}" if status else f"无法连接模型服务（{exception_type}）"
            logger.warning(
                "模型服务测试失败",
                extra={"exception_type": exception_type, "upstream_status": status},
            )
            return ModelTestResponse(
                success=False,
                latency_ms=round((time.perf_counter() - started) * 1000),
                error=error,
            )

    async def test_provider(self, workspace_id: int, provider_id: str) -> ModelTestResponse:
        record = await self.require_provider(workspace_id, provider_id)
        result = await self.ping(record)
        record.last_tested_at, record.last_test_status = (
            datetime.now(UTC), "success" if result.success else "failed"
        )
        await self.repo.save(record)
        return result

    async def list_roles(self, workspace_id: int) -> list[dict[str, Any]]:
        roles = {row.role_key: row.provider_id for row in await self.repo.list_roles(workspace_id)}
        providers = {row.provider_id: row for row in await self.repo.list_providers(workspace_id)}
        return [
            {
                "role_key": key,
                "label": label,
                "required_type": "chat",
                "provider_id": roles.get(key),
                "provider_name": (
                    providers[roles[key]].display_name if roles.get(key) in providers else None
                ),
            }
            for key, label in ROLE_LABELS.items()
        ]

    async def assign_role(self, workspace_id: int, role_key: str, provider_id: str | None) -> None:
        if role_key not in ROLE_LABELS:
            raise AgentException.message("模型角色不存在", status_code=404)
        row = await self.repo.get_role_including_deleted(workspace_id, role_key)
        if provider_id is None:
            if row is not None:
                await self.repo.delete(row)
            return
        await self.require_provider(workspace_id, provider_id, chat=True)
        row = row or ModelRoleRecord(workspace_id=workspace_id, role_key=role_key)
        row.provider_id = provider_id
        row.is_deleted, row.deleted_at = False, None
        await self.repo.save(row)

    async def preferences(
        self,
        workspace_id: int,
        user_id: int,
        provider_id: str | None = None,
        *,
        update: bool = False,
    ) -> dict[str, str | None]:
        user = await UserService(self.db).get_user(user_id)
        if update:
            if provider_id is not None:
                await self.require_provider(workspace_id, provider_id, chat=True)
            preferences = dict(user.preferences or {})
            defaults = dict(preferences.get("model_defaults_by_workspace") or {})
            defaults[str(workspace_id)] = provider_id
            preferences["model_defaults_by_workspace"] = defaults
            user = await UserService(self.db).update_user(user_id, preferences=preferences)
        preferences = user.preferences or {}
        defaults = preferences.get("model_defaults_by_workspace") or {}
        value = defaults.get(str(workspace_id), preferences.get("default_model_provider_id"))
        if value:
            record = await self.repo.get_provider(workspace_id, value)
            if record is None or not record.is_active or record.provider_type != "chat":
                value = None
        return {"default_model_provider_id": value}

    async def select_provider(
        self,
        workspace_id: int,
        user_id: int | None,
        provider_id: str | None = None,
        scenario: str = "agent",
    ) -> ModelProviderRecord | None:
        if provider_id:
            return await self.require_provider(workspace_id, provider_id, chat=True)
        if user_id is not None and scenario == "agent":
            preference = (await self.preferences(workspace_id, user_id))[
                "default_model_provider_id"
            ]
            if preference:
                row = await self.repo.get_provider(workspace_id, preference)
                if row and row.is_active and row.provider_type == "chat":
                    return row
        roles = {row.role_key: row.provider_id for row in await self.repo.list_roles(workspace_id)}
        target = roles.get(scenario) or roles.get("agent")
        if target:
            return await self.require_provider(workspace_id, target, chat=True)
        return next(
            (
                row
                for row in await self.repo.list_providers(workspace_id)
                if row.is_active and row.provider_type == "chat"
            ),
            None,
        )

    async def capabilities(self, workspace_id: int, user_id: int) -> dict[str, Any]:
        rows = [
            row
            for row in await self.repo.list_providers(workspace_id)
            if row.is_active and row.provider_type == "chat"
        ]
        main = await self.select_provider(workspace_id, user_id)
        workflow = await self.select_provider(workspace_id, user_id, scenario="workflow")
        models = [
            {
                "provider_id": row.provider_id,
                "display_name": row.display_name,
                "model_name": row.model_name,
                **reasoning_capabilities(row.extra_config),
                **fast_capabilities(row.provider, row.extra_config),
            }
            for row in rows
        ]
        fast = (
            fast_capabilities(main.provider, main.extra_config)
            if main
            else {
                "supports_fast": False,
                "turbo_supported": False,
                "fast_behavior": "model_default",
            }
        )
        return {
            "main_provider_id": main.provider_id if main else None,
            "workflow_provider_id": workflow.provider_id if workflow else None,
            "allow_model_switch": True,
            "models": models,
            "modes": MODES,
            **reasoning_capabilities(main.extra_config if main else {}),
            **fast,
        }

    @staticmethod
    def build_client(record: ModelProviderRecord, effort: str = "fast") -> BaseLLMClient:
        from llm.anthropic_client import AnthropicClient
        from llm.openai_client import OpenAIClient

        normalized = normalize_effort(record.extra_config or {}, effort)
        levels = reasoning_capabilities(record.extra_config or {})["reasoning_effort_levels"]
        wire_value = next((item["value"] for item in levels if item["key"] == normalized), None)
        kwargs = {
            "model": record.model_name,
            "api_key": record.api_key or "no-key",
            "base_url": record.base_url,
            "max_retries": 1,
            "timeout": 30.0,
        }
        if record.provider == "anthropic":
            return AnthropicClient(
                **kwargs,
                thinking_enabled=normalized != "fast",
                reasoning_effort=wire_value,
                thinking_budget_tokens=record.extra_config.get("thinking_budget_tokens"),
            )
        return OpenAIClient(
            **kwargs,
            thinking_enabled=normalized != "fast",
            reasoning_effort=wire_value,
            thinking_protocol=record.extra_config.get("thinking_protocol")
            or (
                "deepseek"
                if record.provider == "deepseek"
                else "openai"
                if record.provider == "openai"
                else "chat_template"
            ),
            fast_reasoning_effort=record.extra_config.get("fast_reasoning_effort"),
        )

    async def probe_metadata(self, workspace_id: int, payload: ModelProbeRequest) -> dict[str, Any]:
        key = payload.api_key.get_secret_value().strip()
        if payload.provider_id:
            record = await self.require_provider(workspace_id, payload.provider_id)
            key = key or record.api_key
        candidate = ModelProviderInput(
            display_name="能力探测",
            provider=payload.provider,
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            model_name=payload.model_name,
        )
        self._validate_provider(candidate, key)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if payload.provider == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        endpoint = payload.base_url.rstrip("/") + (
            "/v1/models" if payload.provider == "anthropic" else "/models"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(endpoint, headers=headers)
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, (dict, list)):
                raise ValueError("模型元数据须为对象或列表")
            rows = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError("模型元数据列表格式错误")
            return next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and (row.get("id") or row.get("name")) == payload.model_name
                ),
                {},
            )
        except httpx.HTTPStatusError as exc:
            raise AgentException.message(
                "模型元数据探测失败",
                {"upstream_status": exc.response.status_code},
                status_code=400,
            ) from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AgentException.message(
                "无法读取模型元数据，请检查服务地址和密钥", status_code=400
            ) from exc

    async def detect_context(self, workspace_id: int, payload: ModelProbeRequest) -> dict[str, Any]:
        metadata = await self.probe_metadata(workspace_id, payload)
        for key in ("context_length", "max_model_len", "context_window", "max_context_length"):
            value = metadata.get(key)
            if type(value) is int and value > 0:
                return {
                    "context_length": value,
                    "source": "models_endpoint",
                    "source_label": "模型元数据",
                    "confidence": "high",
                    "notes": [],
                }
        return {
            "context_length": 0,
            "source": "none",
            "source_label": "未知",
            "confidence": "none",
            "notes": ["上游未声明该模型的上下文窗口，请查阅供应商文档后手动填写。"],
        }

    async def detect_reasoning(
        self, workspace_id: int, payload: ModelProbeRequest
    ) -> dict[str, Any]:
        metadata = await self.probe_metadata(workspace_id, payload)
        values = metadata.get("supported_reasoning_efforts", metadata.get("reasoning_efforts"))
        from model_service.policy import REASONING_KEYS

        levels = [
            {"key": key, "value": key}
            for key in REASONING_KEYS
            if isinstance(values, list) and key in values
        ]
        default = metadata.get("default_reasoning_effort")
        if levels and default not in {row["key"] for row in levels}:
            default = levels[0]["key"]
        return {
            "levels": levels,
            "default": default if levels else None,
            "source": "model_metadata" if levels else "unknown",
            "complete": bool(levels),
            "numeric_range": None,
        }
