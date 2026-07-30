import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from anthropic import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from llm.client import LLMClient
from models.memory import MemoryItemRecord
from models.session import SessionMessage, SessionRecord, SessionTurnRecord
from repositories.memory_job_repo import MemoryJobRepository
from repositories.memory_repo import MemoryCatalogEntry, MemoryRepository
from repositories.session_repo import SessionRepository
from services.memory_service import MEMORY_BODY_MAX_BYTES, MEMORY_CATALOG_MAX_ITEMS

_MEMORY_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
# 英文凭据模式与中文敏感表述并列维护；中文模式匹配“密码是/为/：值”这类口语化泄露。
_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=-]{12,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"\b(?:password|passwd|secret|token|api[_-]?key|cookie|session[_-]?id)\b"
        r"\s*(?:=|:)\s*[\"']?[A-Za-z0-9+/_.=-]{8,}",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:Cookie|Set-Cookie)\s*:\s*[^\r\n]{8,}", re.IGNORECASE),
    re.compile(
        r"(?:密码|口令|密钥|私钥|令牌|访问令牌|身份证号?|银行卡号?|手机验证码|验证码)"
        r"\s*(?:是|为|[:：=])\s*[\"'“”]?\S{4,}",
    ),
)

logger = logging.getLogger(__name__)


class ExtractedMemory(BaseModel):
    """Extractor 返回的一条严格 Memory 候选。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    memory_key: str = Field(min_length=1, max_length=160)
    type: Literal["user", "feedback", "project", "reference"]
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)
    source_message_ids: list[int] = Field(min_length=1, max_length=10)
    supersedes_existing: bool = Field(
        default=False,
        description=(
            "仅当本条新事实明确推翻 Catalog 中同 memory_key 的旧事实时设为 true；"
            "对旧事实的补充、细化或不确定是否矛盾时必须保持 false。"
        ),
    )

    @field_validator("memory_key")
    @classmethod
    def validate_memory_key(cls, value: str) -> str:
        if not _MEMORY_KEY_PATTERN.fullmatch(value):
            raise ValueError("memory_key格式无效")
        return value

    @field_validator("name", "description")
    @classmethod
    def validate_single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("标题和摘要必须为单行文本")
        return value

    @field_validator("body")
    @classmethod
    def validate_body_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MEMORY_BODY_MAX_BYTES:
            raise ValueError("Memory正文超过UTF-8字节限制")
        return value

    @field_validator("source_message_ids")
    @classmethod
    def validate_source_message_ids(cls, value: list[int]) -> list[int]:
        if any(message_id <= 0 for message_id in value):
            raise ValueError("来源消息ID必须为正整数")
        if len(value) != len(set(value)):
            raise ValueError("来源消息ID不能重复")
        return value


class MemoryExtractionResult(BaseModel):
    """Extractor 顶层严格输出。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    # 通过 maxItems 向 LLM 声明与运行时校验一致的真实上限；不用 Field(max_length) 硬约束，
    # 因为它会把超量的正常模型输出判成非法结构，导致 Job 非重试直接 dead。
    memories: list[ExtractedMemory] = Field(
        default_factory=list,
        json_schema_extra={"maxItems": settings.memory_extractor_max_items},
    )

    @model_validator(mode="after")
    def validate_unique_keys(self) -> MemoryExtractionResult:
        keys = [candidate.memory_key for candidate in self.memories]
        if len(keys) != len(set(keys)):
            raise ValueError("同一提取结果不能包含重复memory_key")
        # 超量候选截断到配置上限并保留告警，而不是判为 invalid_extractor_output。
        limit = settings.memory_extractor_max_items
        if len(self.memories) > limit:
            logger.warning(
                "提取候选数量超过配置上限，已截断多余候选",
                extra={"candidate_count": len(self.memories), "limit": limit},
            )
            self.memories = self.memories[:limit]
        return self


@dataclass(frozen=True)
class ExtractionMessage:
    """发给 Extractor 的脱敏消息投影。"""

    id: int
    role: str
    content: str

    def to_dict(self) -> dict[str, int | str]:
        return {"id": self.id, "role": self.role, "content": self.content}


@dataclass(frozen=True)
class PreparedMemoryExtraction:
    """关闭读取事务后仍可安全使用的提取输入。"""

    job_id: UUID
    space_id: int
    session_id: int
    turn_id: UUID
    user_id: int
    workspace_id: int
    model: str
    prompt_version: str
    mode: str
    catalog_version: int
    catalog_entries: tuple[MemoryCatalogEntry, ...]
    messages: tuple[ExtractionMessage, ...]


@dataclass(frozen=True)
class MemoryApplyResult:
    """一次提取 Apply 的非正文结果摘要。

    rejected_candidates 只保留 memory_key 和原因枚举等引用信息，
    供人工排查规则 6 的保守拒绝，禁止携带 Memory 正文或对话内容。
    """

    created_ids: tuple[int, ...]
    updated_ids: tuple[int, ...]
    unchanged_ids: tuple[int, ...]
    rejected_count: int
    catalog_version: int
    mode: str = "active"
    superseded_ids: tuple[int, ...] = ()
    rejected_candidates: tuple[dict[str, str], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "created_memory_ids": list(self.created_ids),
            "updated_memory_ids": list(self.updated_ids),
            "unchanged_memory_ids": list(self.unchanged_ids),
            "superseded_memory_ids": list(self.superseded_ids),
            "rejected_count": self.rejected_count,
            "rejected_candidates": [dict(entry) for entry in self.rejected_candidates],
            "catalog_version": self.catalog_version,
        }


class MemoryExtractionFailure(Exception):
    """带重试语义的提取失败。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _classify_extractor_error(exc: Exception) -> tuple[str, bool]:
    """把 Extractor 调用异常归一为 (失败码, 是否可重试)。

    失败码会写入 Job 记录用于排障，故按传输不可用、HTTP 状态、输出不合规三类区分；
    429 与 5xx 属于服务端瞬时问题可重试，输出不合规重试无意义。
    """
    match exc:
        case APITimeoutError() | APIConnectionError() | RateLimitError() | TimeoutError():
            return "extractor_unavailable", True
        case APIStatusError():
            retryable = exc.status_code == 429 or exc.status_code >= 500
            return f"extractor_http_{exc.status_code}", retryable
        case ValidationError() | ValueError():
            return "invalid_extractor_output", False
        case _:
            return "extractor_error", False


class MemoryExtractionService:
    """构建原始输入、调用 Extractor，并应用严格校验后的候选。"""

    def __init__(self, db: AsyncSession, llm: LLMClient | None = None):
        self.db = db
        self.llm = llm or LLMClient()
        self.job_repo = MemoryJobRepository(db)
        self.memory_repo = MemoryRepository(db)
        self.session_repo = SessionRepository(db)

    async def prepare(self, job_id: UUID, worker_id: str) -> PreparedMemoryExtraction:
        """从原始消息和当前 Catalog 构建不含 ORM 对象的提取输入。"""
        job = await self.job_repo.get_owned_lease(job_id, worker_id, for_update=False)
        if job is None:
            raise MemoryExtractionFailure("lease_lost", "Memory Job Lease无效", retryable=True)
        if job.job_type != "extract" or job.session_id is None or job.turn_id is None:
            raise MemoryExtractionFailure("invalid_job", "提取Job引用不完整", retryable=False)

        session = await self.session_repo.get(job.session_id)
        turn = await self.session_repo.get_turn(job.turn_id)
        if session is None or turn is None:
            raise MemoryExtractionFailure("source_missing", "提取来源已不存在", retryable=False)
        self._validate_job_scope(job.space_id, session, turn)

        messages = await self.session_repo.list_messages_for_extraction(
            session.id,
            through_message_id=turn.completed_message_id,
            required_message_ids=(turn.started_message_id, turn.completed_message_id),
            limit=settings.memory_extractor_max_messages,
        )
        required_ids = {turn.started_message_id, turn.completed_message_id}
        if not required_ids.issubset({message.id for message in messages}):
            raise MemoryExtractionFailure(
                "source_boundary_missing",
                "提取窗口缺少当前轮次起止消息",
                retryable=False,
            )
        for message in messages:
            if message.session_id != session.id or message.id > turn.completed_message_id:
                raise MemoryExtractionFailure(
                    "source_scope_invalid", "来源消息越界", retryable=False
                )
        boundary_messages = {
            message.id: message for message in messages if message.id in required_ids
        }
        if (
            any(message.turn_id != turn.id for message in boundary_messages.values())
            or boundary_messages[turn.started_message_id].role != "user"
            or boundary_messages[turn.completed_message_id].role != "assistant"
        ):
            raise MemoryExtractionFailure(
                "source_turn_invalid", "轮次边界消息归属无效", retryable=False
            )

        projected = tuple(self._project_message(message) for message in messages)
        projected = self._limit_message_chars(projected, required_ids)
        catalog = await self.memory_repo.get_catalog_snapshot(
            turn.workspace_id,
            turn.user_id,
            limit=MEMORY_CATALOG_MAX_ITEMS + 1,
        )
        if catalog.space_id != job.space_id:
            raise MemoryExtractionFailure("space_scope_invalid", "Job目标空间无效", retryable=False)
        if len(catalog.entries) > MEMORY_CATALOG_MAX_ITEMS:
            raise MemoryExtractionFailure("catalog_full", "Memory Catalog已满", retryable=False)

        model = job.model or settings.memory_extractor_model or settings.anthropic_model
        if not model:
            raise MemoryExtractionFailure("model_missing", "未配置Memory提取模型", retryable=False)
        mode = (job.payload or {}).get("mode", "active")
        if mode not in {"active", "shadow"}:
            raise MemoryExtractionFailure(
                "invalid_mode",
                "提取Job执行模式无效",
                retryable=False,
            )
        return PreparedMemoryExtraction(
            job_id=job.id,
            space_id=job.space_id,
            session_id=session.id,
            turn_id=turn.id,
            user_id=turn.user_id,
            workspace_id=turn.workspace_id,
            model=model,
            prompt_version=job.prompt_version or settings.memory_extractor_prompt_version,
            mode=mode,
            catalog_version=catalog.catalog_version,
            catalog_entries=catalog.entries,
            messages=projected,
        )

    async def extract(self, prepared: PreparedMemoryExtraction) -> MemoryExtractionResult:
        """在数据库事务外调用结构化输出模型。"""
        return await self.extract_with_llm(prepared, self.llm)

    @classmethod
    async def extract_with_llm(
        cls,
        prepared: PreparedMemoryExtraction,
        llm: LLMClient,
    ) -> MemoryExtractionResult:
        """不持有数据库 Session，调用模型并执行严格二次校验。"""
        payload = {
            "messages": [message.to_dict() for message in prepared.messages],
            "memory_catalog": [
                {
                    "memory_key": entry.memory_key,
                    "type": entry.memory_type,
                    "name": entry.name,
                    "description": entry.description,
                }
                for entry in prepared.catalog_entries
            ],
        }
        try:
            return await llm.complete_structured(
                [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                MemoryExtractionResult,
                system_prompt=cls._system_prompt(prepared.prompt_version),
                model=prepared.model,
                max_tokens=4096,
                timeout=settings.memory_extractor_timeout_seconds,
            )
        except Exception as exc:
            code, retryable = _classify_extractor_error(exc)
            raise MemoryExtractionFailure(code, type(exc).__name__, retryable=retryable) from exc

    async def apply(
        self,
        job_id: UUID,
        worker_id: str,
        result: MemoryExtractionResult,
    ) -> MemoryApplyResult:
        """重新校验 Lease 和来源后，在一个短事务内幂等应用整批候选。"""
        job = await self.job_repo.require_owned_lease(job_id, worker_id)
        if job.job_type != "extract" or job.session_id is None or job.turn_id is None:
            raise MemoryExtractionFailure("invalid_job", "提取Job引用不完整", retryable=False)

        session = await self.session_repo.get(job.session_id)
        turn = await self.session_repo.get_turn(job.turn_id)
        if session is None or turn is None:
            raise MemoryExtractionFailure("source_missing", "提取来源已不存在", retryable=False)
        self._validate_job_scope(job.space_id, session, turn)
        messages = await self.session_repo.list_messages_for_extraction(
            session.id,
            through_message_id=turn.completed_message_id,
            required_message_ids=(turn.started_message_id, turn.completed_message_id),
            limit=settings.memory_extractor_max_messages,
        )
        required_ids = {turn.started_message_id, turn.completed_message_id}
        raw_messages_by_id = {message.id: message for message in messages}
        if not required_ids.issubset(raw_messages_by_id):
            raise MemoryExtractionFailure(
                "source_boundary_missing",
                "提取窗口缺少当前轮次起止消息",
                retryable=False,
            )
        if (
            any(raw_messages_by_id[message_id].turn_id != turn.id for message_id in required_ids)
            or raw_messages_by_id[turn.started_message_id].role != "user"
            or raw_messages_by_id[turn.completed_message_id].role != "assistant"
        ):
            raise MemoryExtractionFailure(
                "source_turn_invalid",
                "轮次边界消息归属无效",
                retryable=False,
            )
        projected = tuple(self._project_message(message) for message in messages)
        projected = self._limit_message_chars(
            projected,
            required_ids,
        )
        messages_by_id = {message.id: message for message in projected}

        accepted: list[ExtractedMemory] = []
        rejected_candidates: list[dict[str, str]] = []
        for candidate in result.memories:
            if not set(candidate.source_message_ids).issubset(messages_by_id):
                rejected_candidates.append(
                    {"memory_key": candidate.memory_key, "reason": "source_out_of_scope"}
                )
                continue
            if self._contains_sensitive_candidate(candidate):
                rejected_candidates.append(
                    {"memory_key": candidate.memory_key, "reason": "sensitive_content"}
                )
                continue
            accepted.append(candidate)

        # 锁顺序固定为 Space→Item，与用户 CRUD 一致，避免交叉死锁。
        space = await self.memory_repo.get_space(
            turn.workspace_id,
            turn.user_id,
            for_update=True,
        )
        if space is None or space.id != job.space_id:
            raise MemoryExtractionFailure("space_scope_invalid", "Job目标空间无效", retryable=False)

        active_count = await self.memory_repo.count_active_items(
            turn.workspace_id,
            turn.user_id,
            space.id,
        )
        created_ids: list[int] = []
        updated_ids: list[int] = []
        unchanged_ids: list[int] = []
        superseded_ids: list[int] = []
        catalog_changed = False

        for candidate in accepted:
            item = await self.memory_repo.get_active_item_by_key(
                turn.workspace_id,
                turn.user_id,
                candidate.memory_key,
                for_update=True,
            )
            action = self._classify_candidate(item, candidate)
            if action == "conflict":
                rejected_candidates.append(
                    {"memory_key": candidate.memory_key, "reason": "conflict"}
                )
                continue
            if action == "create":
                if active_count >= MEMORY_CATALOG_MAX_ITEMS:
                    rejected_candidates.append(
                        {"memory_key": candidate.memory_key, "reason": "catalog_full"}
                    )
                    continue
                item = await self.memory_repo.create_item(
                    turn.workspace_id,
                    turn.user_id,
                    space=space,
                    memory_key=candidate.memory_key,
                    memory_type=candidate.type,
                    name=candidate.name,
                    description=candidate.description,
                    body=candidate.body,
                    source_kind="extracted",
                )
                revision = await self.memory_repo.create_revision(
                    turn.workspace_id,
                    turn.user_id,
                    space,
                    item,
                    actor_type="extractor",
                    actor_id=worker_id,
                    run_id=job.id,
                )
                created_ids.append(item.id)
                active_count += 1
                catalog_changed = True
            elif action == "supersede":
                # 规则 5：新事实明确推翻旧事实。active memory_key 存在部分唯一索引，
                # 必须先把旧项暂置 archived 释放唯一键，再创建新项并回填 superseded
                # 指向；三步同处一个短事务且沿用 Space→Item 锁顺序，外部不可见中间态。
                old_item = item
                await self.memory_repo.update_item_state(
                    turn.workspace_id,
                    turn.user_id,
                    space,
                    old_item,
                    status="archived",
                    superseded_by_id=None,
                )
                item = await self.memory_repo.create_item(
                    turn.workspace_id,
                    turn.user_id,
                    space=space,
                    memory_key=candidate.memory_key,
                    memory_type=candidate.type,
                    name=candidate.name,
                    description=candidate.description,
                    body=candidate.body,
                    source_kind="extracted",
                )
                revision = await self.memory_repo.create_revision(
                    turn.workspace_id,
                    turn.user_id,
                    space,
                    item,
                    actor_type="extractor",
                    actor_id=worker_id,
                    run_id=job.id,
                )
                await self.memory_repo.update_item_state(
                    turn.workspace_id,
                    turn.user_id,
                    space,
                    old_item,
                    status="superseded",
                    superseded_by_id=item.id,
                )
                # 与 Dream 状态迁移一致，为旧项追加审计 Revision 记录推翻动作。
                await self.memory_repo.create_revision(
                    turn.workspace_id,
                    turn.user_id,
                    space,
                    old_item,
                    actor_type="extractor",
                    actor_id=worker_id,
                    run_id=job.id,
                )
                created_ids.append(item.id)
                superseded_ids.append(old_item.id)
                catalog_changed = True
            elif action == "update":
                item = await self.memory_repo.update_item(
                    turn.workspace_id,
                    turn.user_id,
                    space,
                    item,
                    memory_type=candidate.type,
                    name=candidate.name,
                    description=candidate.description,
                    body=candidate.body,
                )
                revision = await self.memory_repo.create_revision(
                    turn.workspace_id,
                    turn.user_id,
                    space,
                    item,
                    actor_type="extractor",
                    actor_id=worker_id,
                    run_id=job.id,
                )
                updated_ids.append(item.id)
                catalog_changed = True
            else:
                revision = await self.memory_repo.get_current_revision(
                    turn.workspace_id,
                    turn.user_id,
                    item,
                )
                if revision is None:
                    raise MemoryExtractionFailure(
                        "revision_missing",
                        "Memory当前修订不存在",
                        retryable=False,
                    )
                unchanged_ids.append(item.id)

            for source_message_id in candidate.source_message_ids:
                source_message = messages_by_id[source_message_id]
                await self.memory_repo.create_source_if_absent(
                    turn.workspace_id,
                    turn.user_id,
                    space=space,
                    item=item,
                    revision=revision,
                    source_kind="extracted",
                    session_id=session.id,
                    turn_id=turn.id,
                    message_id=source_message_id,
                    source_excerpt=self._safe_source_excerpt(source_message.content),
                )

        if catalog_changed:
            catalog_version = await self.memory_repo.increment_catalog_version(
                turn.workspace_id,
                turn.user_id,
                space,
            )
        else:
            catalog_version = space.catalog_version
        return MemoryApplyResult(
            created_ids=tuple(created_ids),
            updated_ids=tuple(updated_ids),
            unchanged_ids=tuple(unchanged_ids),
            rejected_count=len(rejected_candidates),
            catalog_version=catalog_version,
            superseded_ids=tuple(superseded_ids),
            rejected_candidates=tuple(rejected_candidates),
        )

    @staticmethod
    def _validate_job_scope(
        space_id: int,
        session: SessionRecord,
        turn: SessionTurnRecord,
    ) -> None:
        if (
            turn.status != "completed"
            or turn.completed_message_id is None
            or turn.session_id != session.id
            or session.user_id != turn.user_id
            or session.workspace_id != turn.workspace_id
        ):
            raise MemoryExtractionFailure(
                "source_scope_invalid",
                "Job、会话和轮次归属不一致",
                retryable=False,
            )
        if space_id <= 0:
            raise MemoryExtractionFailure("space_scope_invalid", "Job目标空间无效", retryable=False)

    @staticmethod
    def _project_message(message: SessionMessage) -> ExtractionMessage:
        if message.role == "user":
            content = message.content if isinstance(message.content, str) else ""
            return ExtractionMessage(message.id, "user", content)
        if message.role == "assistant":
            text_parts = []
            if isinstance(message.content, list):
                text_parts = [
                    str(block.get("text", ""))
                    for block in message.content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
            elif isinstance(message.content, str):
                text_parts = [message.content]
            return ExtractionMessage(message.id, "assistant", "\n".join(text_parts))
        if message.role == "tool" and isinstance(message.content, dict):
            tool_name = str(message.content.get("tool_name") or "unknown")[:120]
            status = "error" if bool(message.content.get("is_error", False)) else "success"
            output = MemoryExtractionService._collapse_text(
                str(message.content.get("output") or "")
            )
            summary = MemoryExtractionService._truncate_text(output, 500)
            return ExtractionMessage(
                message.id,
                "tool",
                f"tool={tool_name}; status={status}; summary={summary}",
            )
        return ExtractionMessage(message.id, message.role, "")

    @staticmethod
    def _limit_message_chars(
        messages: tuple[ExtractionMessage, ...],
        required_ids: set[int],
    ) -> tuple[ExtractionMessage, ...]:
        limit = settings.memory_extractor_max_chars
        if limit <= 0:
            return tuple(message for message in messages if message.id in required_ids)

        required = [message for message in messages if message.id in required_ids]
        optional = [message for message in messages if message.id not in required_ids]
        required_budget = max(1, limit // max(1, len(required)))
        selected: dict[int, ExtractionMessage] = {
            message.id: ExtractionMessage(
                message.id,
                message.role,
                MemoryExtractionService._truncate_text(message.content, required_budget),
            )
            for message in required
        }
        used = sum(len(message.content) for message in selected.values())
        for message in reversed(optional):
            remaining = limit - used
            if remaining <= 0:
                break
            if len(message.content) > remaining:
                continue
            selected[message.id] = message
            used += len(message.content)
        return tuple(selected[message_id] for message_id in sorted(selected))

    @staticmethod
    def _classify_candidate(
        item: MemoryItemRecord | None,
        candidate: ExtractedMemory,
    ) -> str:
        if item is None:
            return "create"
        current = (item.memory_type, item.name, item.description, item.body)
        proposed = (candidate.type, candidate.name, candidate.description, candidate.body)
        if current == proposed:
            return "noop"
        if item.memory_type != candidate.type:
            return "conflict"

        current_body = MemoryExtractionService._normalize_comparable(item.body)
        proposed_body = MemoryExtractionService._normalize_comparable(candidate.body)
        if current_body == proposed_body or current_body in proposed_body:
            return "update"
        if proposed_body in current_body:
            return "noop"
        # 规则 5 与规则 6 的边界：正文矛盾时只有模型明确标记推翻意图才允许 supersede，
        # 未标记的矛盾一律保守判 conflict，避免模糊输出覆盖用户数据。
        if candidate.supersedes_existing:
            return "supersede"
        return "conflict"

    @staticmethod
    def _contains_sensitive_candidate(candidate: ExtractedMemory) -> bool:
        return MemoryExtractionService._contains_sensitive_text(
            f"{candidate.memory_key}\n{candidate.name}\n{candidate.description}\n{candidate.body}"
        )

    @staticmethod
    def _contains_sensitive_text(value: str) -> bool:
        return any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS) or any(
            MemoryExtractionService._looks_high_entropy_credential(token)
            for token in re.findall(r"[A-Za-z0-9+/=_-]{32,}", value)
        )

    @staticmethod
    def _looks_high_entropy_credential(value: str) -> bool:
        character_classes = sum(
            (
                any(char.islower() for char in value),
                any(char.isupper() for char in value),
                any(char.isdigit() for char in value),
                any(char in "+/=_-" for char in value),
            )
        )
        if character_classes < 4:
            return False
        frequencies = {char: value.count(char) / len(value) for char in set(value)}
        entropy = -sum(probability * math.log2(probability) for probability in frequencies.values())
        return entropy >= 4.2

    @staticmethod
    def _safe_source_excerpt(value: str) -> str | None:
        collapsed = MemoryExtractionService._collapse_text(value)
        if not collapsed or MemoryExtractionService._contains_sensitive_text(collapsed):
            return None
        return MemoryExtractionService._truncate_text(collapsed, 500)

    @staticmethod
    def _collapse_text(value: str) -> str:
        return " ".join(value.split())

    @staticmethod
    def _truncate_text(value: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        if len(value) <= max_chars:
            return value
        marker = "[前文已截断]"
        if max_chars <= len(marker):
            return value[-max_chars:]
        return marker + value[-(max_chars - len(marker)) :]

    @staticmethod
    def _normalize_comparable(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    @staticmethod
    def _system_prompt(prompt_version: str) -> str:
        return f"""你是长期 Memory 提取器，提示词版本 {prompt_version}。
只提取跨轮次仍有价值、用户明确要求记住或稳定可复用的信息。
四种类型固定为 user、feedback、project、reference。memory_key 必须稳定、简短，
仅由小写字母、数字、连字符或下划线组成。对照现有 Catalog：相同主题必须复用
既有 memory_key；无新信息时不要输出候选。当新事实明确推翻 Catalog 中同
memory_key 的旧事实时，将 supersedes_existing 设为 true；仅是补充细节或
无法确定是否矛盾时保持 false。不要保存密码、Token、API Key、私钥、
Cookie、身份凭据、工具大输出、临时任务状态或召回到本轮的历史 Memory。
source_message_ids 只能引用输入中真实支持该事实的消息 ID。不确定时返回空 memories。"""
