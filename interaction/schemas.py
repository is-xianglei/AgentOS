from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

InteractionKind = Literal[
    "tool_approval",
    "user_question",
    "enter_plan_mode",
    "exit_plan_mode",
]
InteractionRequestStatus = Literal["pending", "resolved", "cancelled", "expired"]
RuntimeSuspensionStatus = Literal[
    "pending",
    "resuming",
    "resolved",
    "cancelled",
    "failed",
]


class StrictSchema(BaseModel):
    """交互协议默认拒绝未知字段，避免恢复时静默丢失客户端数据。"""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class QuestionOption(StrictSchema):
    label: str = Field(min_length=1, max_length=80, description="选项显示文本")
    description: str = Field(min_length=1, max_length=1000, description="选项说明")
    preview: str | None = Field(
        default=None,
        max_length=100_000,
        description="聚焦选项时展示的可选预览内容",
    )


class AskUserQuestion(StrictSchema):
    question: str = Field(min_length=1, max_length=1000, description="向用户提出的完整问题")
    header: str = Field(min_length=1, max_length=12, description="问题短标签")
    options: list[QuestionOption] = Field(
        min_length=2,
        max_length=4,
        description="可选项，不包含由客户端自动提供的其他选项",
    )
    multi_select: bool = Field(
        default=False,
        alias="multiSelect",
        description="是否允许多选",
    )

    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
        "populate_by_name": True,
    }

    @model_validator(mode="after")
    def validate_question(self) -> AskUserQuestion:
        labels = [option.label for option in self.options]
        if len(labels) != len(set(labels)):
            raise ValueError("同一问题内的选项文本不能重复")
        return self


class AskUserQuestionMetadata(StrictSchema):
    source: str | None = Field(default=None, min_length=1, max_length=120)


class AskUserQuestionRequestPayload(StrictSchema):
    questions: list[AskUserQuestion] = Field(min_length=1, max_length=4)
    metadata: AskUserQuestionMetadata | None = None

    @model_validator(mode="after")
    def validate_questions(self) -> AskUserQuestionRequestPayload:
        texts = [question.question for question in self.questions]
        if len(texts) != len(set(texts)):
            raise ValueError("同一次交互中的问题文本不能重复")

        total_chars = sum(
            len(question.question)
            + len(question.header)
            + sum(
                len(option.label)
                + len(option.description)
                + len(option.preview or "")
                for option in question.options
            )
            for question in self.questions
        )
        if total_chars > 100_000:
            raise ValueError("问题与预览内容总长度不能超过100000字符")
        return self


class QuestionAnnotation(StrictSchema):
    preview: str | None = Field(default=None, max_length=100_000)
    notes: str | None = Field(default=None, max_length=10_000)


class ToolApprovalResponsePayload(StrictSchema):
    decision: Literal["allow_once", "always_allow", "deny"]
    updated_input: dict[str, Any] | None = None
    always_scope: Literal["session", "global"] = "session"
    message: str | None = Field(default=None, min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_decision(self) -> ToolApprovalResponsePayload:
        if self.decision == "deny" and self.updated_input is not None:
            raise ValueError("拒绝工具调用时不能提供 updated_input")
        if self.decision != "always_allow" and self.always_scope != "session":
            raise ValueError("只有 always_allow 可以指定非默认作用域")
        if self.decision != "deny" and self.message is not None:
            raise ValueError("只有 deny 决策可以提供 message")
        return self


class UserQuestionResponsePayload(StrictSchema):
    decision: Literal["submit", "cancel", "discuss", "finish_plan_interview"] = "submit"
    answers: dict[str, str] = Field(
        default_factory=dict,
        description="问题文本到用户答案的映射",
    )
    annotations: dict[str, QuestionAnnotation] = Field(default_factory=dict)
    message: str | None = Field(default=None, min_length=1, max_length=10_000)

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for question, answer in value.items():
            question_text = question.strip()
            answer_text = answer.strip()
            if not question_text or not answer_text:
                raise ValueError("问题文本和答案不能为空")
            if len(question_text) > 1000 or len(answer_text) > 10_000:
                raise ValueError("问题文本或答案长度超出限制")
            if question_text in normalized:
                raise ValueError("答案中不能包含重复问题")
            normalized[question_text] = answer_text
        return normalized

    @model_validator(mode="after")
    def validate_annotations(self) -> UserQuestionResponsePayload:
        if self.decision == "submit" and not self.answers:
            raise ValueError("提交问题响应时必须提供答案")
        if self.decision == "cancel" and (self.answers or self.annotations or self.message):
            raise ValueError("取消问题时不能附带答案、注解或消息")
        if self.decision not in {"discuss", "finish_plan_interview"} and self.message:
            raise ValueError("只有讨论或结束规划访谈时可以提供消息")
        unknown = set(self.annotations) - set(self.answers)
        if unknown:
            raise ValueError("注解只能关联本次已回答的问题")
        return self


class EnterPlanModeResponsePayload(StrictSchema):
    decision: Literal["approve", "reject"]
    feedback: str | None = Field(default=None, min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_feedback(self) -> EnterPlanModeResponsePayload:
        if self.decision != "reject" and self.feedback is not None:
            raise ValueError("只有 reject 决策可以提供 feedback")
        return self


class ExitPlanModeResponsePayload(StrictSchema):
    decision: Literal["approve", "reject"]
    feedback: str | None = Field(default=None, min_length=1, max_length=10_000)
    edited_plan: str | None = Field(default=None, min_length=1, max_length=500_000)


class InteractionRequestCreate(StrictSchema):
    kind: InteractionKind
    request_payload: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: int | None = Field(default=None, ge=1)
    tool_use_id: str | None = Field(default=None, min_length=1, max_length=255)
    schema_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_request_payload(self) -> InteractionRequestCreate:
        if self.kind == "user_question":
            validated = AskUserQuestionRequestPayload.model_validate(self.request_payload)
            self.request_payload = validated.model_dump(mode="json", by_alias=True)
        return self


class InteractionRequestResponse(BaseModel):
    id: UUID
    suspension_id: UUID
    session_id: int
    turn_id: UUID
    tool_call_id: int | None = None
    tool_use_id: str | None = None
    kind: InteractionKind
    schema_version: int
    sequence: int
    status: InteractionRequestStatus
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None = None
    responded_by: int | None = None
    responded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RuntimeSuspensionResponse(BaseModel):
    """客户端可见的暂停点元数据；continuation 与恢复策略仅在服务端使用。"""

    id: UUID
    session_id: int
    turn_id: UUID
    status: RuntimeSuspensionStatus
    version: int
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PendingInteractionResponse(BaseModel):
    suspension: RuntimeSuspensionResponse
    requests: list[InteractionRequestResponse]


class ToolApprovalResolutionRequest(StrictSchema):
    kind: Literal["tool_approval"]
    response_payload: ToolApprovalResponsePayload


class UserQuestionResolutionRequest(StrictSchema):
    kind: Literal["user_question"]
    response_payload: UserQuestionResponsePayload


class EnterPlanModeResolutionRequest(StrictSchema):
    kind: Literal["enter_plan_mode"]
    response_payload: EnterPlanModeResponsePayload


class ExitPlanModeResolutionRequest(StrictSchema):
    kind: Literal["exit_plan_mode"]
    response_payload: ExitPlanModeResponsePayload


InteractionResolutionRequest = Annotated[
    ToolApprovalResolutionRequest
    | UserQuestionResolutionRequest
    | EnterPlanModeResolutionRequest
    | ExitPlanModeResolutionRequest,
    Field(discriminator="kind"),
]


RESPONSE_PAYLOAD_MODELS: dict[InteractionKind, type[BaseModel]] = {
    "tool_approval": ToolApprovalResponsePayload,
    "user_question": UserQuestionResponsePayload,
    "enter_plan_mode": EnterPlanModeResponsePayload,
    "exit_plan_mode": ExitPlanModeResponsePayload,
}
