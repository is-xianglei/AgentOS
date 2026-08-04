"""子代理输出协议测试:VERDICT 判定、证据校验、关键文件清单与 Agent 工具路由。

verification 的报告是主代理的决策输入,因此"看起来通过"必须无法冒充"验证通过":
缺 VERDICT、无命令证据的 PASS 都要在返回边界降级,而不是靠主代理自觉复核。
"""

import json

import pytest

import database.registry  # noqa: F401
from tools.builtin.agent import AgentTool, AgentToolInput
from tools.subagents.definition import AgentType
from tools.subagents.prompts import PLAN_CRITICAL_FILES_HEADING
from tools.subagents.registry import (
    count_command_evidence,
    parse_plan_critical_files,
    parse_verdict,
    validate_verification_report,
)

_EVIDENCE = """执行的命令:
```
uv run pytest -q
```
原始输出:
```
17 passed
```
"""


class TestVerdictParsing:
    @pytest.mark.parametrize("value", ["PASS", "FAIL", "PARTIAL"])
    def test_识别三种判定(self, value: str) -> None:
        assert parse_verdict(f"报告正文\n\nVERDICT: {value}") == value

    def test_容忍加粗与尾随句号(self) -> None:
        """模型常把末行写成 **VERDICT: PASS**,不该因此解析失败。"""
        assert parse_verdict("**VERDICT: PASS**") == "PASS"
        assert parse_verdict("VERDICT: FAIL.") == "FAIL"

    def test_行内提及不算判定(self) -> None:
        """否则报告里讨论"VERDICT: PASS 的条件"就会被误读成通过。"""
        assert parse_verdict("如果测试都过了,才能给出 VERDICT: PASS 这样的结论") is None

    def test_取最后一条判定(self) -> None:
        """报告可能在中间复述格式要求,最终判定以末次出现为准。"""
        report = "示例格式:\nVERDICT: PASS\n\n实际结论:\nVERDICT: FAIL"
        assert parse_verdict(report) == "FAIL"

    def test_缺失判定返回None(self) -> None:
        assert parse_verdict("一切正常,代码质量很好") is None

    def test_非法判定值不识别(self) -> None:
        assert parse_verdict("VERDICT: MAYBE") is None
        assert parse_verdict("VERDICT: pass") is None


class TestCommandEvidence:
    def test_统计命令证据块(self) -> None:
        assert count_command_evidence(_EVIDENCE) == 1
        assert count_command_evidence(_EVIDENCE * 3) == 3

    def test_无证据时为零(self) -> None:
        assert count_command_evidence("我审阅了代码,实现是正确的") == 0

    def test_只写命令不带代码块不算证据(self) -> None:
        """要求原始输出是为了防止编造命令结果。"""
        assert count_command_evidence("执行的命令: uv run pytest -q") == 0


class TestReportValidation:
    def test_带证据的PASS原样保留(self) -> None:
        verdict, note = validate_verification_report(f"{_EVIDENCE}\n\nVERDICT: PASS")
        assert verdict == "PASS"
        assert note is None

    def test_无证据的PASS降级为PARTIAL(self) -> None:
        """这是核心断言:仅凭读代码得出的"通过"不能当验证结果用。"""
        verdict, note = validate_verification_report("代码看起来没问题\n\nVERDICT: PASS")
        assert verdict == "PARTIAL"
        assert note is not None and "证据" in note

    def test_缺失VERDICT按PARTIAL处理(self) -> None:
        verdict, note = validate_verification_report(f"{_EVIDENCE}\n\n实现基本没问题")
        assert verdict == "PARTIAL"
        assert note is not None

    def test_FAIL无证据不升级也不降级(self) -> None:
        """FAIL 本身是保守结论,无需证据支撑即可采信。"""
        verdict, note = validate_verification_report("接口 500 了\n\nVERDICT: FAIL")
        assert verdict == "FAIL"
        assert note is None

    def test_空报告按PARTIAL处理(self) -> None:
        verdict, _ = validate_verification_report("")
        assert verdict == "PARTIAL"


class TestPlanCriticalFiles:
    def test_解析关键文件清单(self) -> None:
        report = f"""## 方案
先改 Service 再补迁移。

{PLAN_CRITICAL_FILES_HEADING}
- `tools/service.py` — 加档位透传
- `runtime/subagent.py` — 接权限裁决
"""
        files = parse_plan_critical_files(report)
        assert files == ["tools/service.py", "runtime/subagent.py"]

    def test_无清单时返回空(self) -> None:
        assert parse_plan_critical_files("## 方案\n直接改就行") == []

    def test_只取清单小节内的条目(self) -> None:
        """清单前的正文里也会提到文件路径,不能一并收进来。"""
        report = f"""正文提到 `should/not/appear.py` 这个文件。

{PLAN_CRITICAL_FILES_HEADING}
- `real/target.py` — 主改动点
"""
        assert parse_plan_critical_files(report) == ["real/target.py"]

    def test_无反引号时取首个片段(self) -> None:
        """模型不一定按格式加反引号,退化格式也要能取到路径。"""
        report = f"{PLAN_CRITICAL_FILES_HEADING}\n- tools/service.py: 加档位透传\n"
        assert parse_plan_critical_files(report) == ["tools/service.py"]

    def test_遇到下一小节即停止(self) -> None:
        report = f"""{PLAN_CRITICAL_FILES_HEADING}
- `a.py` — 改这里

## 风险
- `b.py` 不是关键文件
"""
        assert parse_plan_critical_files(report) == ["a.py"]


class TestAgentToolRouting:
    def test_默认类型为general_purpose(self) -> None:
        """未指定时不该报错或选到只读类型,通用型是安全默认。"""
        assert AgentToolInput(prompt="干点活").agent_type is AgentType.GENERAL_PURPOSE

    def test_描述里含全部类型的选型依据(self) -> None:
        description = AgentTool().description
        for agent_type in AgentType:
            assert agent_type.value in description

    def test_描述说明子代理的能力边界(self) -> None:
        """主代理需要知道哪些事不能派给子代理,否则会派了又失败。"""
        description = AgentTool().description
        assert "无法再派生子代理" in description
        assert "审批" in description

    def test_verification入参含完整输入契约(self) -> None:
        fields = AgentToolInput.model_fields
        for name in ("original_request", "changed_files", "implementation_notes", "known_risks"):
            assert name in fields

    def test_verification组装结构化输入(self) -> None:
        args = AgentToolInput(
            agent_type=AgentType.VERIFICATION,
            prompt="验收本次改动",
            original_request="加登录接口",
            changed_files=["auth/api.py", "auth/service.py"],
            implementation_notes="复用现有 AuthService",
            known_risks="并发登录未测",
        )
        composed = AgentTool._compose_prompt(args)
        assert "验收本次改动" in composed
        assert "加登录接口" in composed
        assert "auth/api.py" in composed and "auth/service.py" in composed
        assert "复用现有 AuthService" in composed
        assert "并发登录未测" in composed

    def test_缺失字段显式标注而非静默省略(self) -> None:
        """让 verification 能看出是调用方没给,而不是误以为无风险。"""
        args = AgentToolInput(agent_type=AgentType.VERIFICATION, prompt="验收")
        composed = AgentTool._compose_prompt(args)
        assert composed.count("(调用方未提供)") == 5

    def test_其他类型不做组装(self) -> None:
        for agent_type in (AgentType.GENERAL_PURPOSE, AgentType.EXPLORE, AgentType.PLAN):
            args = AgentToolInput(agent_type=agent_type, prompt="原始任务")
            assert AgentTool._compose_prompt(args) == "原始任务"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Ctx:
    """ToolContext 的最小替身,run() 只用到这几个字段。"""

    session_id = 1
    bus = None
    turn_id = None
    user_id = None
    workspace_id = None
    allowed_tool_names = frozenset({"Agent", "Read"})
    allowed_skill_names = frozenset({"workspace-skill"})


def _tool_with_report(report: str) -> AgentTool:
    """把 _dispatch 换成直接返回给定报告,隔离掉真实子代理运行。"""
    tool = AgentTool()

    async def fake_dispatch(*args: object, **kwargs: object) -> str:
        return report

    tool._dispatch = fake_dispatch  # type: ignore[method-assign]
    return tool


@pytest.mark.anyio
class TestAgentToolReturnBoundary:
    async def test_父Agent能力边界传给一次性子代理(self) -> None:
        seen: list[tuple[object, ...]] = []
        tool = AgentTool()

        async def fake_dispatch(*args: object) -> str:
            seen.append(args)
            return "完成"

        tool._dispatch = fake_dispatch  # type: ignore[method-assign]

        await tool.run(AgentToolInput(prompt="查一下"), _Ctx())

        assert seen[0][-2] == _Ctx.allowed_tool_names
        assert seen[0][-1] == _Ctx.allowed_skill_names

    async def test_verification结果带机器可读判定(self) -> None:
        """主代理不必自己解析报告文本,也就无法把 FAIL 读成 PASS。"""
        tool = _tool_with_report(f"{_EVIDENCE}\n\nVERDICT: PASS")
        result = json.loads(
            await tool.run(
                AgentToolInput(agent_type=AgentType.VERIFICATION, prompt="验收"), _Ctx()
            )
        )
        assert result["verdict"] == "PASS"
        assert result["verdict_note"] is None
        assert result["agent"] == "verification"

    async def test_无证据的PASS在返回边界被降级(self) -> None:
        tool = _tool_with_report("我读了代码,没问题\n\nVERDICT: PASS")
        result = json.loads(
            await tool.run(
                AgentToolInput(agent_type=AgentType.VERIFICATION, prompt="验收"), _Ctx()
            )
        )
        assert result["verdict"] == "PARTIAL"
        assert result["verdict_note"]

    async def test_FAIL如实返回(self) -> None:
        tool = _tool_with_report("测试失败\n\nVERDICT: FAIL")
        result = json.loads(
            await tool.run(
                AgentToolInput(agent_type=AgentType.VERIFICATION, prompt="验收"), _Ctx()
            )
        )
        assert result["verdict"] == "FAIL"

    async def test_非verification类型不带判定字段值(self) -> None:
        """只有验收类型才有 VERDICT 语义,别的类型硬塞会误导主代理。"""
        tool = _tool_with_report("查到了实现在 tools/service.py:22")
        result = json.loads(await tool.run(AgentToolInput(prompt="查一下"), _Ctx()))
        assert result["verdict"] is None
        assert result["verdict_note"] is None
        assert result["report"]

    async def test_报告原文一并返回(self) -> None:
        """降级只改判定字段,不能删掉报告正文,否则主代理无从判断原因。"""
        tool = _tool_with_report("看起来没问题\n\nVERDICT: PASS")
        result = json.loads(
            await tool.run(
                AgentToolInput(agent_type=AgentType.VERIFICATION, prompt="验收"), _Ctx()
            )
        )
        assert "看起来没问题" in result["report"]
