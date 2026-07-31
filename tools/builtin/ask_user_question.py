from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.errors import AgentException
from tools.base import BaseTool, ToolContext


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1,
        description="展示给用户的简短选项文本，建议1-5个词",
    )
    description: str = Field(min_length=1, description="选项含义、影响或取舍说明")
    preview: str | None = Field(
        default=None,
        description="聚焦选项时展示的可选Markdown预览",
    )


class UserQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    question: str = Field(min_length=1, description="完整、明确的问题文本")
    header: str = Field(min_length=1, max_length=12, description="最多12字符的短标签")
    options: list[QuestionOption] = Field(
        min_length=2,
        max_length=4,
        description="2-4个候选选项，不要添加Other选项",
    )
    multi_select: bool = Field(
        default=False,
        alias="multiSelect",
        description="是否允许选择多个选项",
    )

    @model_validator(mode="after")
    def validate_unique_options(self) -> "UserQuestion":
        labels = [option.label for option in self.options]
        if len(labels) != len(set(labels)):
            raise ValueError("同一问题内的选项标签必须唯一")
        return self


class AskUserQuestionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, description="可选的问题来源标识")


class AskUserQuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[UserQuestion] = Field(
        min_length=1,
        max_length=4,
        description="一次询问的1-4个问题",
    )
    metadata: AskUserQuestionMetadata | None = Field(
        default=None,
        description="可选的追踪元数据，不向用户展示",
    )

    @model_validator(mode="after")
    def validate_unique_questions(self) -> "AskUserQuestionInput":
        texts = [question.question for question in self.questions]
        if len(texts) != len(set(texts)):
            raise ValueError("同一次询问的问题文本必须唯一")
        return self

    def request_payload(self) -> dict[str, Any]:
        """只序列化模型提出的问题，答案必须来自独立交互响应。"""
        return self.model_dump(by_alias=True, exclude_none=True)


class AskUserQuestionTool(BaseTool):
    name = "AskUserQuestion"
    description = (
        "Ask the user 1-4 multiple-choice questions to clarify requirements, preferences, "
        "trade-offs, or decisions. Users may also provide a custom answer. In plan mode, "
        "use ExitPlanMode rather than this tool to request plan approval."
    )
    input_model = AskUserQuestionInput

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        raise AgentException.message("AskUserQuestion必须由主运行时的人工交互流程处理")
