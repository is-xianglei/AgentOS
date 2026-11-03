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
    REASONING_KEYS,
    fast_capabilities,
    normalize_effort,
    reasoning_capabilities,
    resolve_thinking_protocol,
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
# 每项依次为：供应商标识、显示名、默认服务地址、是否自动填充地址、是否必须填写密钥。
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
    """返回前端配置表单使用的供应商清单及各自支持的模型用途。"""
    schemas: list[dict[str, Any]] = []
    for key, label, url, autofill, required in _PROVIDERS:
        # Anthropic 走原生客户端，只支持对话；其余都走 OpenAI 兼容协议。
        if key == "anthropic":
            engine = "native"
            supports_types = ["chat"]
        elif key == "audio_compatible":
            engine = "openai"
            supports_types = ["audio"]
        else:
            engine = "openai"
            supports_types = ["chat", "embedding", "reranker", "audio"]
        schemas.append(
            {
                "id": key,
                "label": label,
                "engine": engine,
                "supports_types": supports_types,
                "base_url_template": url,
                "autofill_base_url": autofill,
                "api_key_required": required,
            }
        )
    return schemas


def public_provider(record: ModelProviderRecord) -> ModelProviderResponse:
    """转换为对外响应：密钥只在服务端使用，已配置时一律返回掩码。"""
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
        """按工作区查询供应商；chat=True 时还要求是已启用的对话模型。"""
        record = await self.repo.get_provider(workspace_id, provider_id)
        if record is None or record.is_deleted:
            raise AgentException.message("模型供应商不存在或不属于当前工作区", status_code=404)
        if chat and (not record.is_active or record.provider_type != "chat"):
            raise AgentException.message("请选择已启用的对话模型", status_code=400)
        return record

    async def list_providers(self, workspace_id: int) -> list[ModelProviderResponse]:
        return [public_provider(row) for row in await self.repo.list_providers(workspace_id)]

    def _validate_provider(self, payload: ModelProviderInput, key: str) -> None:
        """保存或探测前的统一校验；key 为最终生效的密钥（可能沿用已保存的值）。"""
        specs = {spec["id"]: spec for spec in provider_schemas()}
        spec = specs.get(payload.provider)
        if spec is None or payload.provider_type not in spec["supports_types"]:
            raise AgentException.message("不支持该供应商协议或用途", status_code=400)
        if not payload.display_name or not payload.model_name:
            raise AgentException.message("显示名称和模型名不能为空", status_code=400)
        if not payload.base_url:
            raise AgentException.message("模型服务地址不能为空", status_code=400)

        # 地址里不允许带凭据或查询参数，避免密钥混进地址后被保存、展示或写入日志。
        url_error = "模型服务地址须为无凭据、查询参数的 HTTP 或 HTTPS 地址"
        try:
            url = httpx.URL(payload.base_url)
            url_is_valid = (
                url.scheme in {"http", "https"}
                and bool(url.host)
                and not url.userinfo
                and not url.query
                and not url.fragment
            )
        except (httpx.InvalidURL, ValueError) as exc:
            raise AgentException.message(url_error, status_code=400) from exc
        if not url_is_valid:
            raise AgentException.message(url_error, status_code=400)

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

            # Anthropic 的思考预算按 token 计，须不少于 1024 且小于单次最大输出。
            max_tokens = settings.llm_default_max_tokens
            if "thinking_budget_tokens" in payload.extra_config:
                budget = payload.extra_config["thinking_budget_tokens"]
                if type(budget) is not int or not 1024 <= budget < max_tokens:
                    raise AgentException.message(
                        "Anthropic 思考预算须为至少1024且小于最大输出限制的整数",
                        status_code=400,
                    )
            # 档位映射成整数时同样是思考预算，适用相同范围；字符串档位名不受此限。
            levels = reasoning_capabilities(payload.extra_config)["reasoning_effort_levels"]
            values = [row["value"] for row in levels]
            for value in values:
                if type(value) is int and not 1024 <= value < max_tokens:
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
        """新建或更新供应商；连接参数变化时先做连通性测试，测试失败不落库。"""
        existing = None
        if provider_id:
            existing = await self.require_provider(workspace_id, provider_id)

        # 部分更新：用已保存的值补齐未提交字段，再按完整输入统一校验。
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

        # 未提交或提交空密钥时沿用已保存的密钥。
        key = ""
        if payload.api_key:
            key = payload.api_key.get_secret_value().strip()
        if not key and existing is not None:
            key = existing.api_key
        self._validate_provider(payload, key)

        # 被模型角色引用的供应商不能停用或改用途，否则角色会指向不可用的模型。
        if existing and (existing.provider_type != payload.provider_type or not payload.is_active):
            role_rows = await self.repo.list_roles(workspace_id)
            roles = [row.role_key for row in role_rows if row.provider_id == provider_id]
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
        # 只有保存后处于启用状态才测试：新建、重新启用，或连接参数有变化。
        if not payload.is_active:
            needs_test = False
        elif existing is None or not existing.is_active:
            needs_test = True
        else:
            needs_test = any(
                getattr(existing, field) != values[field] for field in connection_fields
            )
        # 用临时对象做连通性测试，测试失败时已保存的记录保持不变。
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
        """软删除供应商，仍被模型角色引用时拒绝。"""
        record = await self.require_provider(workspace_id, provider_id)
        role_rows = await self.repo.list_roles(workspace_id)
        roles = [row.role_key for row in role_rows if row.provider_id == provider_id]
        if roles:
            raise AgentException.message(
                "该模型正在被角色引用，请先取消指派",
                {"roles": roles},
                status_code=409,
            )
        await self.repo.delete(record)

    async def ping(self, record: ModelProviderRecord) -> ModelTestResponse:
        """用最小请求测试连通性；任何失败都转换为不含敏感信息的结果，不向上抛出。"""
        started = time.perf_counter()
        try:
            if record.provider_type == "chat":
                await self._probe_chat(record)
            else:
                await self._probe_http(record)
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

    async def _probe_chat(self, record: ModelProviderRecord) -> None:
        """对话模型：发一条极短消息，要求流式输出产出有效的终局结果。"""
        client = self.build_client(record, "fast")
        client.max_tokens = 32
        result = None
        async with (
            asyncio.timeout(30),
            aclosing(client.stream([{"role": "user", "content": "Reply OK"}])) as stream,
        ):
            async for event in stream:
                if event.kind.value == "turn_result":
                    result = event.result
        if result is None or not (result.text or result.tool_uses):
            raise ValueError("模型流未产出有效终局结果")

    async def _probe_http(self, record: ModelProviderRecord) -> None:
        """向量、重排和音频转写模型：直接调用对应接口，并校验返回结构。"""
        headers: dict[str, str] = {}
        if record.api_key:
            headers = {"Authorization": f"Bearer {record.api_key}"}
        async with httpx.AsyncClient(timeout=20) as client:
            if record.provider_type == "audio":
                # 生成 1 秒 16kHz 单声道静音 WAV，只用来确认转写接口可用。
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
            elif record.provider_type == "embedding":
                body = {"model": record.model_name, "input": ["test"]}
                response = await client.post(
                    f"{record.base_url}/embeddings", headers=headers, json=body
                )
            else:
                body = {"model": record.model_name, "query": "test", "documents": ["test"]}
                response = await client.post(
                    f"{record.base_url}/rerank", headers=headers, json=body
                )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict):
            raise ValueError("上游响应须为对象")
        if record.provider_type == "embedding":
            rows = data.get("data")
            vector = None
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                vector = rows[0].get("embedding")
            if not isinstance(vector, list) or not vector:
                raise ValueError("上游未返回有效向量")
            for value in vector:
                if type(value) not in {int, float} or not math.isfinite(value):
                    raise ValueError("上游未返回有效向量")
        elif record.provider_type == "reranker":
            rows = data.get("results")
            if not isinstance(rows, list) or not rows:
                raise ValueError("上游未返回有效重排结果")
            # 只发送了一篇文档，每条结果都必须指向下标 0，且分数是有限数值。
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or type(row.get("index")) is not int
                    or row["index"] != 0
                    or type(row.get("relevance_score")) not in {int, float}
                    or not math.isfinite(row["relevance_score"])
                ):
                    raise ValueError("上游未返回有效重排结果")
        elif not isinstance(data.get("text"), str):
            raise ValueError("上游未返回转写结果")

    async def test_provider(self, workspace_id: int, provider_id: str) -> ModelTestResponse:
        """手动测试已保存的供应商，并记录本次测试时间与结果。"""
        record = await self.require_provider(workspace_id, provider_id)
        result = await self.ping(record)
        record.last_tested_at = datetime.now(UTC)
        if result.success:
            record.last_test_status = "success"
        else:
            record.last_test_status = "failed"
        await self.repo.save(record)
        return result

    async def list_roles(self, workspace_id: int) -> list[dict[str, Any]]:
        """列出全部场景角色及其当前指派；未指派的角色 provider_id 为 None。"""
        role_rows = await self.repo.list_roles(workspace_id)
        provider_rows = await self.repo.list_providers(workspace_id)
        provider_id_by_role = {row.role_key: row.provider_id for row in role_rows}
        provider_by_id = {row.provider_id: row for row in provider_rows}

        result: list[dict[str, Any]] = []
        for key, label in ROLE_LABELS.items():
            provider_id = provider_id_by_role.get(key)
            provider_name = None
            if provider_id in provider_by_id:
                provider_name = provider_by_id[provider_id].display_name
            result.append(
                {
                    "role_key": key,
                    "label": label,
                    "required_type": "chat",
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                }
            )
        return result

    async def assign_role(self, workspace_id: int, role_key: str, provider_id: str | None) -> None:
        """给场景角色指派对话模型；provider_id 为 None 表示取消指派。"""
        if role_key not in ROLE_LABELS:
            raise AgentException.message("模型角色不存在", status_code=404)
        # 唯一约束 (workspace_id, role_key) 包含软删除的行，所以重新指派时复用旧行。
        row = await self.repo.get_role_including_deleted(workspace_id, role_key)
        if provider_id is None:
            if row is not None:
                await self.repo.delete(row)
            return
        await self.require_provider(workspace_id, provider_id, chat=True)
        if row is None:
            row = ModelRoleRecord(workspace_id=workspace_id, role_key=role_key)
        row.provider_id = provider_id
        row.is_deleted = False
        row.deleted_at = None
        await self.repo.save(row)

    async def preferences(
        self,
        workspace_id: int,
        user_id: int,
        provider_id: str | None = None,
        *,
        update: bool = False,
    ) -> dict[str, str | None]:
        """读取（update=True 时先写入）用户在该工作区的默认对话模型。

        偏好按工作区分别保存在用户 preferences 中；update 时 provider_id 为 None 表示清除。
        """
        user = await UserService(self.db).get_user(user_id)
        if update:
            if provider_id is not None:
                await self.require_provider(workspace_id, provider_id, chat=True)
            new_preferences = dict(user.preferences or {})
            defaults = dict(new_preferences.get("model_defaults_by_workspace") or {})
            defaults[str(workspace_id)] = provider_id
            new_preferences["model_defaults_by_workspace"] = defaults
            user = await UserService(self.db).update_user(user_id, preferences=new_preferences)

        preferences = user.preferences or {}
        defaults = preferences.get("model_defaults_by_workspace") or {}
        # 该工作区从未设置过时才回落到旧版全局字段；显式清除（存为 None）不回落。
        value = defaults.get(str(workspace_id), preferences.get("default_model_provider_id"))
        # 偏好指向的模型已删除、停用或改了用途时，视为没有偏好。
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
        """按优先级选择本次使用的对话模型。

        顺序：显式指定 > 用户默认（仅 agent 场景）> 场景角色指派 > agent 角色指派
        > 工作区第一个启用的对话模型。返回 None 表示工作区没有可用配置。
        """
        if provider_id:
            return await self.require_provider(workspace_id, provider_id, chat=True)

        if user_id is not None and scenario == "agent":
            user_preferences = await self.preferences(workspace_id, user_id)
            preference = user_preferences["default_model_provider_id"]
            if preference:
                row = await self.repo.get_provider(workspace_id, preference)
                if row and row.is_active and row.provider_type == "chat":
                    return row

        role_rows = await self.repo.list_roles(workspace_id)
        provider_id_by_role = {row.role_key: row.provider_id for row in role_rows}
        target = provider_id_by_role.get(scenario) or provider_id_by_role.get("agent")
        if target:
            return await self.require_provider(workspace_id, target, chat=True)

        for row in await self.repo.list_providers(workspace_id):
            if row.is_active and row.provider_type == "chat":
                return row
        return None

    async def capabilities(self, workspace_id: int, user_id: int) -> dict[str, Any]:
        """汇总前端模型选择器所需信息；顶层能力字段取当前主模型的配置。"""
        provider_rows = await self.repo.list_providers(workspace_id)
        rows = [row for row in provider_rows if row.is_active and row.provider_type == "chat"]
        main = await self.select_provider(workspace_id, user_id)
        workflow = await self.select_provider(workspace_id, user_id, scenario="workflow")

        models: list[dict[str, Any]] = []
        for row in rows:
            models.append(
                {
                    "provider_id": row.provider_id,
                    "display_name": row.display_name,
                    "model_name": row.model_name,
                    **reasoning_capabilities(row.extra_config),
                    **fast_capabilities(row.provider, row.extra_config),
                }
            )

        # 没有可用主模型时，不承诺思考档位和极速模式。
        if main is not None:
            main_reasoning = reasoning_capabilities(main.extra_config)
            main_fast = fast_capabilities(main.provider, main.extra_config)
        else:
            main_reasoning = reasoning_capabilities({})
            main_fast = {
                "supports_fast": False,
                "turbo_supported": False,
                "fast_behavior": "model_default",
            }

        return {
            "main_provider_id": main.provider_id if main else None,
            "workflow_provider_id": workflow.provider_id if workflow else None,
            "allow_model_switch": True,
            "models": models,
            "modes": MODES,
            **main_reasoning,
            **main_fast,
        }

    @staticmethod
    def build_client(record: ModelProviderRecord, effort: str = "fast") -> BaseLLMClient:
        """按工作区配置构造 LLM 客户端，effort 为请求的思考强度。"""
        from llm.anthropic_client import AnthropicClient
        from llm.openai_client import OpenAIClient

        normalized = normalize_effort(record.extra_config or {}, effort)
        # 把档位名换成上游实际接收的值（档位名或思考预算）；fast 没有对应档位，取 None。
        levels = reasoning_capabilities(record.extra_config or {})["reasoning_effort_levels"]
        wire_value = None
        for item in levels:
            if item["key"] == normalized:
                wire_value = item["value"]
                break
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
            thinking_protocol=resolve_thinking_protocol(record.provider, record.extra_config),
            fast_reasoning_effort=record.extra_config.get("fast_reasoning_effort"),
        )

    async def probe_metadata(self, workspace_id: int, payload: ModelProbeRequest) -> dict[str, Any]:
        """读取上游 /models 中目标模型的元数据，找不到时返回空字典。"""
        key = payload.api_key.get_secret_value().strip()
        # 编辑已有供应商且未重新填写密钥时，沿用已保存的密钥。
        if payload.provider_id:
            record = await self.require_provider(workspace_id, payload.provider_id)
            key = key or record.api_key
        # 先校验地址再带密钥发请求，避免把已保存的密钥发往不合规的地址。
        candidate = ModelProviderInput(
            display_name="能力探测",
            provider=payload.provider,
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            model_name=payload.model_name,
        )
        self._validate_provider(candidate, key)

        base_url = payload.base_url.rstrip("/")
        if payload.provider == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            endpoint = base_url + "/v1/models"
        else:
            headers = {}
            if key:
                headers = {"Authorization": f"Bearer {key}"}
            endpoint = base_url + "/models"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(endpoint, headers=headers)
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, (dict, list)):
                raise ValueError("模型元数据须为对象或列表")
            # 兼容 {"data": [...]}、{"models": [...]} 和直接返回列表三种格式。
            if isinstance(data, dict):
                rows = data.get("data", data.get("models", []))
            else:
                rows = data
            if not isinstance(rows, list):
                raise ValueError("模型元数据列表格式错误")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if (row.get("id") or row.get("name")) == payload.model_name:
                    return row
            return {}
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
        """从模型元数据中识别上下文窗口；各家字段名不同，按顺序取第一个有效值。"""
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
        """从模型元数据中识别支持的思考档位，只保留系统认识的档位名。"""
        metadata = await self.probe_metadata(workspace_id, payload)
        values = metadata.get("supported_reasoning_efforts", metadata.get("reasoning_efforts"))

        levels: list[dict[str, str]] = []
        if isinstance(values, list):
            for key in REASONING_KEYS:
                if key in values:
                    levels.append({"key": key, "value": key})
        if not levels:
            return {
                "levels": [],
                "default": None,
                "source": "unknown",
                "complete": False,
                "numeric_range": None,
            }

        default = metadata.get("default_reasoning_effort")
        if default not in {row["key"] for row in levels}:
            default = levels[0]["key"]
        return {
            "levels": levels,
            "default": default,
            "source": "model_metadata",
            "complete": True,
            "numeric_range": None,
        }
