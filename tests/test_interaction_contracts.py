"""人工交互 Schema、主运行时工具集与子代理隔离契约测试。"""

from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from core.errors import AgentException
from interaction.models import InteractionRequestRecord
from interaction.schemas import (
    AskUserQuestionRequestPayload,
    UserQuestionResponsePayload,
)
# 工具包须先完成初始化，避免 interaction.service 经 session/task 触发循环导入。
from tools.registry import build_tool_registry
from tools.subagents.definition import (
    GLOBAL_SUBAGENT_DENIED_TOOLS,
    AgentType,
    SubAgentSpec,
)
from tools.subagents.registry import SUBAGENT_SPECS
from interaction.service import InteractionService
from runtime.agent import PLAN_MODE_ALLOWED_TOOLS, AgentRuntime


INTERACTION_AND_PLAN_TOOLS = frozenset(
    {
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        "WritePlan",
    }
)


def _question(
    index: int = 0,
    *,
    option_count: int = 2,
    multi_select: bool = False,
) -> dict[str, object]:
    return {
        "question": f"问题 {index}？",
        "header": f"问题{index}",
        "options": [
            {
                "label": f"选项 {index}-{option_index}",
                "description": f"选项 {index}-{option_index} 的说明",
            }
            for option_index in range(option_count)
        ],
        "multiSelect": multi_select,
    }


def _request_payload(
    question_count: int = 2,
    *,
    option_count: int = 2,
) -> dict[str, object]:
    return {
        "questions": [
            _question(index, option_count=option_count) for index in range(question_count)
        ]
    }


def _persisted_question_request(
    payload: dict[str, object],
) -> InteractionRequestRecord:
    return cast(
        InteractionRequestRecord,
        SimpleNamespace(kind="user_question", request_payload=payload),
    )


class TestAskUserQuestionRequest:
    @pytest.mark.parametrize("question_count", [1, 2, 3, 4])
    def test_支持一至四个问题(self, question_count: int) -> None:
        payload = AskUserQuestionRequestPayload.model_validate(
            _request_payload(question_count)
        )

        assert len(payload.questions) == question_count

    @pytest.mark.parametrize("question_count", [0, 5])
    def test_拒绝零个或五个问题(self, question_count: int) -> None:
        with pytest.raises(ValidationError):
            AskUserQuestionRequestPayload.model_validate(
                _request_payload(question_count)
            )

    @pytest.mark.parametrize("option_count", [2, 3, 4])
    def test_每题支持二至四个选项(self, option_count: int) -> None:
        payload = AskUserQuestionRequestPayload.model_validate(
            _request_payload(1, option_count=option_count)
        )

        assert len(payload.questions[0].options) == option_count

    @pytest.mark.parametrize("option_count", [1, 5])
    def test_拒绝每题一个或五个选项(self, option_count: int) -> None:
        with pytest.raises(ValidationError):
            AskUserQuestionRequestPayload.model_validate(
                _request_payload(1, option_count=option_count)
            )

    def test_拒绝重复问题文本(self) -> None:
        questions = [_question(0), _question(1)]
        questions[1]["question"] = "  问题 0？  "

        with pytest.raises(ValidationError, match="问题文本不能重复"):
            AskUserQuestionRequestPayload.model_validate({"questions": questions})

    def test_拒绝同一问题内的重复选项标签(self) -> None:
        question = _question()
        options = cast(list[dict[str, object]], question["options"])
        options[1]["label"] = "  选项 0-0  "

        with pytest.raises(ValidationError, match="选项文本不能重复"):
            AskUserQuestionRequestPayload.model_validate({"questions": [question]})

    def test_multi_select别名可输入并按别名输出(self) -> None:
        request = _request_payload(1)
        question = cast(list[dict[str, object]], request["questions"])[0]
        question["multiSelect"] = True

        payload = AskUserQuestionRequestPayload.model_validate(request)
        dumped = payload.model_dump(mode="json", by_alias=True)
        normalized = build_tool_registry().validate_input("AskUserQuestion", request)

        assert payload.questions[0].multi_select is True
        assert dumped["questions"][0]["multiSelect"] is True
        assert "multi_select" not in dumped["questions"][0]
        assert normalized["questions"][0]["multiSelect"] is True


class TestAskUserQuestionResponse:
    def test_submit必须完整回答全部问题并保留注解(self) -> None:
        request_payload = AskUserQuestionRequestPayload.model_validate(
            _request_payload(2)
        ).model_dump(mode="json", by_alias=True)
        request = _persisted_question_request(request_payload)
        service = object.__new__(InteractionService)

        normalized = service._validate_response_payload(
            request,
            {
                "decision": "submit",
                "answers": {
                    "问题 0？": "选项 0-0",
                    "问题 1？": "自定义答案",
                },
                "annotations": {
                    "问题 0？": {
                        "preview": "预览内容",
                        "notes": "用户补充说明",
                    }
                },
            },
        )

        assert normalized["answers"] == {
            "问题 0？": "选项 0-0",
            "问题 1？": "自定义答案",
        }
        assert normalized["annotations"]["问题 0？"] == {
            "preview": "预览内容",
            "notes": "用户补充说明",
        }

    @pytest.mark.parametrize(
        "answers",
        [
            {"问题 0？": "选项 0-0"},
            {
                "问题 0？": "选项 0-0",
                "问题 1？": "选项 1-0",
                "额外问题？": "额外答案",
            },
        ],
    )
    def test_submit拒绝缺失或额外答案(self, answers: dict[str, str]) -> None:
        request_payload = AskUserQuestionRequestPayload.model_validate(
            _request_payload(2)
        ).model_dump(mode="json", by_alias=True)
        request = _persisted_question_request(request_payload)
        service = object.__new__(InteractionService)

        with pytest.raises(AgentException, match="必须完整对应"):
            service._validate_response_payload(
                request,
                {"decision": "submit", "answers": answers},
            )

    def test_submit拒绝空答案集合(self) -> None:
        with pytest.raises(ValidationError, match="必须提供答案"):
            UserQuestionResponsePayload.model_validate(
                {"decision": "submit", "answers": {}}
            )

    def test_annotations只能关联已回答问题(self) -> None:
        with pytest.raises(ValidationError, match="注解只能关联"):
            UserQuestionResponsePayload.model_validate(
                {
                    "decision": "submit",
                    "answers": {"问题 0？": "选项 0-0"},
                    "annotations": {"问题 1？": {"notes": "额外注解"}},
                }
            )


class TestMainRuntimeToolSets:
    @staticmethod
    def _runtime() -> AgentRuntime:
        runtime = object.__new__(AgentRuntime)
        runtime.tool_registry = build_tool_registry()
        return runtime

    def test_完整主注册表包含全部交互与计划工具(self) -> None:
        assert INTERACTION_AND_PLAN_TOOLS <= build_tool_registry().names

    def test_正常模式开放提问和进入计划模式(self) -> None:
        runtime = self._runtime()
        normal = runtime._registry_for_mode(plan_mode=False)

        assert normal.names == runtime.tool_registry.names - {
            "ExitPlanMode",
            "WritePlan",
        }
        assert {"AskUserQuestion", "EnterPlanMode"} <= normal.names

    def test_plan_mode严格使用只读与计划工具集合(self) -> None:
        runtime = self._runtime()
        plan_mode = runtime._registry_for_mode(plan_mode=True)

        assert plan_mode.names == PLAN_MODE_ALLOWED_TOOLS
        assert {"AskUserQuestion", "ExitPlanMode", "WritePlan"} <= plan_mode.names
        assert "EnterPlanMode" not in plan_mode.names
        assert plan_mode.names.isdisjoint({"Bash", "Write", "Edit", "SkillRun"})


class TestSubAgentInteractionIsolation:
    def test_四个交互与计划工具属于全局禁用项(self) -> None:
        assert INTERACTION_AND_PLAN_TOOLS <= set(GLOBAL_SUBAGENT_DENIED_TOOLS)

    @pytest.mark.parametrize(
        "spec",
        list(SUBAGENT_SPECS.values()),
        ids=lambda spec: spec.agent_type.value,
    )
    def test_所有内置子代理都无法获得交互与计划工具(
        self,
        spec: SubAgentSpec,
    ) -> None:
        names = spec.resolve_tools(build_tool_registry()).names

        assert names.isdisjoint(INTERACTION_AND_PLAN_TOOLS)

    def test_子代理白名单也不能重新放开交互与计划工具(self) -> None:
        spec = SubAgentSpec(
            agent_type=AgentType.EXPLORE,
            system_prompt="测试",
            allowed_tools=("Read", *sorted(INTERACTION_AND_PLAN_TOOLS)),
        )

        assert spec.resolve_tools(build_tool_registry()).names == {"Read"}
