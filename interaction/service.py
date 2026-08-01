import json
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from interaction.models import (
    InteractionRequestRecord,
    RuntimeSuspensionRecord,
)
from interaction.repository import InteractionRepository
from interaction.schemas import (
    AskUserQuestionRequestPayload,
    InteractionRequestCreate,
    PendingInteractionResponse,
    RESPONSE_PAYLOAD_MODELS,
)
from session.service import SessionService


class InteractionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = InteractionRepository(db)
        self.session_service = SessionService(db)

    async def create_suspension_with_requests(
        self,
        session_id: int,
        turn_id: UUID,
        continuation_payload: dict[str, Any],
        resolution_policy: dict[str, Any],
        requests: list[InteractionRequestCreate],
    ) -> tuple[RuntimeSuspensionRecord, list[InteractionRequestRecord]]:
        """原子创建暂停点及其全部交互请求，提交由调用方事务边界负责。"""
        if len(requests) != 1:
            raise AgentException.message("顺序运行暂停点每次必须且只能创建一个交互请求")

        session = await self.session_service.get_required(session_id)
        turn = await self.session_service.get_turn_required(turn_id)
        if turn.session_id != session.id:
            raise AgentException.message("交互轮次不属于指定会话")

        existing = await self.repo.get_active_suspension_for_update(session_id)
        if existing is not None:
            raise AgentException.message(
                "会话已有未完成的人工交互",
                {"suspension_id": str(existing.id), "status": existing.status},
            )

        tool_use_ids = [request.tool_use_id for request in requests if request.tool_use_id]
        if len(tool_use_ids) != len(set(tool_use_ids)):
            raise AgentException.message("同一暂停点中的 tool_use_id 不能重复")

        suspension = await self.repo.create_suspension(
            session_id,
            turn_id,
            dict(continuation_payload),
            dict(resolution_policy),
        )
        records: list[InteractionRequestRecord] = []
        for sequence, request in enumerate(requests, start=1):
            records.append(
                await self.repo.create_request(
                    suspension,
                    kind=request.kind,
                    request_payload=dict(request.request_payload),
                    tool_call_id=request.tool_call_id,
                    tool_use_id=request.tool_use_id,
                    schema_version=request.schema_version,
                    sequence=sequence,
                )
            )
        return suspension, records

    async def create_suspension_with_request(
        self,
        session_id: int,
        turn_id: UUID,
        continuation_payload: dict[str, Any],
        resolution_policy: dict[str, Any],
        request: InteractionRequestCreate,
    ) -> tuple[RuntimeSuspensionRecord, InteractionRequestRecord]:
        suspension, records = await self.create_suspension_with_requests(
            session_id,
            turn_id,
            continuation_payload,
            resolution_policy,
            [request],
        )
        return suspension, records[0]

    async def get_pending(
        self, session_id: int
    ) -> tuple[RuntimeSuspensionRecord, list[InteractionRequestRecord]] | None:
        """查询会话当前可响应的暂停点和待处理请求。"""
        await self.session_service.get_required(session_id)
        suspension = await self.repo.get_pending_by_session(session_id)
        if suspension is None:
            return None
        requests = (
            [request for request in suspension.requests if request.status == "pending"]
            if suspension.status == "pending"
            else list(suspension.requests)
        )
        return suspension, requests

    async def get_pending_response(self, session_id: int) -> PendingInteractionResponse | None:
        pending = await self.get_pending(session_id)
        if pending is None:
            return None
        suspension, requests = pending
        return PendingInteractionResponse(
            suspension=suspension,
            requests=requests,
        )

    async def get_request_required(
        self,
        request_id: UUID,
        *,
        session_id: int | None = None,
    ) -> InteractionRequestRecord:
        request = await self.repo.get_request(request_id)
        if request is None or (session_id is not None and request.session_id != session_id):
            raise AgentException.message("人工交互请求不存在")
        return request

    async def get_suspension_required(
        self,
        suspension_id: UUID,
        *,
        session_id: int | None = None,
    ) -> RuntimeSuspensionRecord:
        suspension = await self.repo.get_suspension(suspension_id)
        if suspension is None or (
            session_id is not None and suspension.session_id != session_id
        ):
            raise AgentException.message("运行暂停点不存在")
        return suspension

    async def lock_suspension_required(
        self,
        suspension_id: UUID,
        *,
        session_id: int | None = None,
    ) -> RuntimeSuspensionRecord:
        """锁定并刷新暂停点，保证同一 continuation 只有一个恢复者应用。"""
        suspension = await self.repo.get_suspension_for_update(suspension_id)
        if suspension is None or (
            session_id is not None and suspension.session_id != session_id
        ):
            raise AgentException.message("运行暂停点不存在")
        return suspension

    async def continue_suspension_with_request(
        self,
        suspension_id: UUID,
        continuation_payload: dict[str, Any],
        request: InteractionRequestCreate,
    ) -> tuple[RuntimeSuspensionRecord, InteractionRequestRecord]:
        """在同一 continuation 中追加下一项顺序交互。"""
        suspension = await self.repo.get_suspension_for_update(suspension_id)
        if suspension is None:
            raise AgentException.message("运行暂停点不存在")
        if suspension.status != "resuming":
            raise AgentException.message(
                "只有恢复中的暂停点可以追加交互请求",
                {"status": suspension.status},
            )
        if await self.repo.count_pending_requests(suspension.id) != 0:
            raise AgentException.message("运行暂停点仍有未处理的交互请求")

        existing_requests = await self.repo.list_requests(
            suspension.id,
            for_update=True,
        )
        next_sequence = len(existing_requests) + 1
        await self.repo.update_continuation(suspension, dict(continuation_payload))
        record = await self.repo.create_request(
            suspension,
            kind=request.kind,
            request_payload=dict(request.request_payload),
            tool_call_id=request.tool_call_id,
            tool_use_id=request.tool_use_id,
            schema_version=request.schema_version,
            sequence=next_sequence,
        )
        await self.repo.update_suspension_status(suspension, "pending")
        return suspension, record

    async def resolve_request(
        self,
        request_id: UUID,
        response_payload: dict[str, Any],
        responded_by: int | None,
        *,
        session_id: int | None = None,
        expected_kind: str | None = None,
    ) -> tuple[InteractionRequestRecord, RuntimeSuspensionRecord]:
        """校验并裁决一个请求；最后一个请求完成后暂停点进入 resuming。"""
        current = await self.repo.get_request(request_id)
        if current is None or (session_id is not None and current.session_id != session_id):
            raise AgentException.message("人工交互请求不存在")
        suspension = await self.repo.get_suspension_for_update(current.suspension_id)
        if suspension is None:
            raise AgentException.message("人工交互请求对应的运行暂停点不存在")
        if expected_kind is not None and current.kind != expected_kind:
            raise AgentException.message(
                "人工交互响应类型与待处理请求不匹配",
                {"expected_kind": current.kind, "actual_kind": expected_kind},
            )
        normalized = self._validate_response_payload(current, response_payload)
        if current.status == "resolved":
            if current.response_payload != normalized:
                raise AgentException.message(
                    "人工交互请求已使用不同响应处理",
                    status_code=409,
                )
            return current, suspension
        if suspension.status != "pending" or current.status != "pending":
            raise AgentException.message(
                "人工交互请求已处理或已失效",
                {"request_status": current.status, "suspension_status": suspension.status},
            )
        request = await self.repo.lock_pending_request(request_id, session_id)
        if request is None:
            raise AgentException.message("人工交互请求已处理或已失效")
        await self.repo.resolve_request(request, normalized, responded_by)

        if await self.repo.count_pending_requests(suspension.id) == 0:
            await self.repo.update_suspension_status(suspension, "resuming")
        return request, suspension

    async def cancel_suspension(
        self,
        suspension_id: UUID,
        responded_by: int | None = None,
        *,
        session_id: int | None = None,
    ) -> tuple[RuntimeSuspensionRecord, list[InteractionRequestRecord]]:
        """取消暂停点，并把其中尚未回答的请求一并取消。"""
        suspension = await self.repo.get_suspension_for_update(suspension_id)
        if suspension is None:
            raise AgentException.message("运行暂停点不存在")
        if session_id is not None and suspension.session_id != session_id:
            raise AgentException.message("运行暂停点不属于指定会话")
        if suspension.status not in {"pending", "resuming"}:
            raise AgentException.message("运行暂停点已进入终态")

        requests = await self.repo.list_requests(
            suspension.id,
            for_update=True,
        )
        for request in requests:
            if request.status == "pending":
                await self.repo.cancel_request(request, responded_by)
        suspension = await self.repo.update_suspension_status(
            suspension,
            "cancelled",
            terminal=True,
        )
        return suspension, requests

    async def mark_suspension_resolved(
        self, suspension_id: UUID
    ) -> RuntimeSuspensionRecord:
        """运行时成功恢复并持久化 continuation 后，将暂停点置为 resolved。"""
        suspension = await self.repo.get_suspension_for_update(suspension_id)
        if suspension is None:
            raise AgentException.message("运行暂停点不存在")
        if suspension.status != "resuming":
            raise AgentException.message(
                "只有恢复中的暂停点可以标记为已解决",
                {"status": suspension.status},
            )
        return await self.repo.update_suspension_status(
            suspension,
            "resolved",
            terminal=True,
        )

    def _validate_response_payload(
        self,
        request: InteractionRequestRecord,
        response_payload: dict[str, Any],
    ) -> dict[str, Any]:
        model_type = RESPONSE_PAYLOAD_MODELS.get(request.kind)  # type: ignore[arg-type]
        if model_type is None:
            raise AgentException.message("不支持的人工交互类型")
        try:
            response = model_type.model_validate(response_payload)
        except ValidationError as exc:
            errors = json.loads(json.dumps(exc.errors(), default=str))
            raise AgentException.message("人工交互响应格式无效", {"errors": errors}) from exc

        normalized = response.model_dump(mode="json", by_alias=True)
        if request.kind == "user_question":
            try:
                question_request = AskUserQuestionRequestPayload.model_validate(
                    request.request_payload
                )
            except ValidationError as exc:
                errors = json.loads(json.dumps(exc.errors(), default=str))
                raise AgentException.message(
                    "已保存的问题请求格式无效",
                    {"errors": errors},
                ) from exc
            expected = {question.question for question in question_request.questions}
            actual = set(normalized["answers"])
            decision = normalized["decision"]
            if decision == "submit" and actual != expected:
                raise AgentException.message(
                    "用户答案必须完整对应本次全部问题",
                    {
                        "missing": sorted(expected - actual),
                        "unexpected": sorted(actual - expected),
                    },
                )
            if decision != "submit" and not actual.issubset(expected):
                raise AgentException.message(
                    "用户答案包含不属于本次交互的问题",
                    {"unexpected": sorted(actual - expected)},
                )
        return normalized
