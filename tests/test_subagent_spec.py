"""SubAgent 规格表测试:类型注册、工具集求交、模型档位与轮次上限。

工具权限的最终集合由 resolve_tools 决定,提示词无权放宽,故这里按"安全求交"的
定义逐条断言:黑名单优先于白名单,全局禁用先于两者。
"""

import pytest

from core.config import settings
from tools.registry import build_tool_registry
from tools.shell_policy import ShellAccess
from tools.subagents.definition import (
    GLOBAL_SUBAGENT_DENIED_TOOLS,
    AgentType,
    SubAgentSpec,
)
from tools.subagents.registry import SUBAGENT_SPECS, get_subagent_spec, routing_catalog


def _names(registry) -> set[str]:
    """从注册表取工具名集合(Anthropic 工具声明的 name 字段)。"""
    return {tool["name"] for tool in registry.to_anthropic_tools()}


class TestSpecTable:
    def test_四种内置类型全部注册(self) -> None:
        assert set(SUBAGENT_SPECS) == set(AgentType)
        assert len(SUBAGENT_SPECS) == 4

    def test_类型名与参考实现严格一致(self) -> None:
        """大小写与连字符都是对外契约的一部分,主代理按这些字面量分派。"""
        assert [t.value for t in AgentType] == [
            "general-purpose",
            "Explore",
            "Plan",
            "verification",
        ]

    def test_每个spec的agent_type与键一致(self) -> None:
        for agent_type, spec in SUBAGENT_SPECS.items():
            assert spec.agent_type is agent_type

    def test_每个spec都有提示词与路由说明(self) -> None:
        for spec in SUBAGENT_SPECS.values():
            assert spec.system_prompt.strip()
            assert spec.when_to_use.strip()

    def test_未知类型取不到spec(self) -> None:
        with pytest.raises(Exception):
            get_subagent_spec("NotAnAgent")  # type: ignore[arg-type]


class TestToolResolution:
    def test_任何子代理都不能再派子代理或组队(self) -> None:
        """递归 spawn 会让资源消耗失控,这条禁令先于各 spec 自身声明。"""
        registry = build_tool_registry()
        for spec in SUBAGENT_SPECS.values():
            names = _names(spec.resolve_tools(registry))
            for denied in GLOBAL_SUBAGENT_DENIED_TOOLS:
                assert denied not in names, f"{spec.agent_type.value} 不应有 {denied}"

    def test_只读类型不含任何写工具(self) -> None:
        registry = build_tool_registry()
        for agent_type in (AgentType.EXPLORE, AgentType.PLAN, AgentType.VERIFICATION):
            names = _names(SUBAGENT_SPECS[agent_type].resolve_tools(registry))
            assert names.isdisjoint({"Write", "Edit", "SkillRun"})
            assert "Read" in names and "Grep" in names

    def test_general_purpose_保留除全局禁用外的全部工具(self) -> None:
        registry = build_tool_registry()
        resolved = _names(SUBAGENT_SPECS[AgentType.GENERAL_PURPOSE].resolve_tools(registry))
        assert resolved == _names(registry) - set(GLOBAL_SUBAGENT_DENIED_TOOLS)

    def test_黑名单优先于白名单(self) -> None:
        """同一工具同时出现在两侧时必须被剔除,否则白名单会成为提权途径。"""
        spec = SubAgentSpec(
            agent_type=AgentType.EXPLORE,
            system_prompt="x",
            allowed_tools=("Read", "Grep", "Write"),
            disallowed_tools=("Write",),
        )
        names = _names(spec.resolve_tools(build_tool_registry()))
        assert "Write" not in names
        assert names == {"Read", "Grep"}

    def test_白名单里写全局禁用工具也无效(self) -> None:
        spec = SubAgentSpec(
            agent_type=AgentType.EXPLORE,
            system_prompt="x",
            allowed_tools=("Read", "Agent", "TeamCreate"),
        )
        names = _names(spec.resolve_tools(build_tool_registry()))
        assert names == {"Read"}


class TestShellAccessTier:
    def test_各类型的shell档位符合职责(self) -> None:
        assert SUBAGENT_SPECS[AgentType.GENERAL_PURPOSE].shell_access is ShellAccess.FULL
        assert SUBAGENT_SPECS[AgentType.EXPLORE].shell_access is ShellAccess.READ_ONLY
        assert SUBAGENT_SPECS[AgentType.PLAN].shell_access is ShellAccess.READ_ONLY
        # verification 要落临时测试脚本,只读档位不够用。
        assert SUBAGENT_SPECS[AgentType.VERIFICATION].shell_access is ShellAccess.TMP_WRITABLE

    def test_只有tmp可写档位才有可写目录(self) -> None:
        assert SUBAGENT_SPECS[AgentType.EXPLORE].shell_tmp_root() is None
        assert SUBAGENT_SPECS[AgentType.PLAN].shell_tmp_root() is None
        assert SUBAGENT_SPECS[AgentType.GENERAL_PURPOSE].shell_tmp_root() is None
        tmp_root = SUBAGENT_SPECS[AgentType.VERIFICATION].shell_tmp_root()
        assert tmp_root is not None
        assert "verification" in tmp_root.name

    def test_各类型的可写目录互不重叠(self) -> None:
        """目录按 agent_type 命名,避免并发运行时相互覆盖测试脚本。"""
        spec = SubAgentSpec(
            agent_type=AgentType.EXPLORE,
            system_prompt="x",
            shell_access=ShellAccess.TMP_WRITABLE,
        )
        assert spec.shell_tmp_root() != SUBAGENT_SPECS[AgentType.VERIFICATION].shell_tmp_root()


class TestModelResolution:
    def test_显式入参优先于spec声明(self) -> None:
        spec = SUBAGENT_SPECS[AgentType.EXPLORE]
        assert spec.resolve_model("claude-opus-5") == "claude-opus-5"

    def test_无入参时用spec声明(self) -> None:
        spec = SubAgentSpec(
            agent_type=AgentType.EXPLORE, system_prompt="x", model="claude-haiku-4-5-20251001"
        )
        assert spec.resolve_model() == "claude-haiku-4-5-20251001"

    def test_都缺失时回落环境兜底(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "subagent_default_model", "claude-sonnet-5")
        spec = SubAgentSpec(agent_type=AgentType.PLAN, system_prompt="x", model=None)
        assert spec.resolve_model() == "claude-sonnet-5"

    def test_环境兜底不覆盖spec的速度档位(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """键名叫 default 就该是兜底:配上它不该把 Explore 的快模型顶掉。"""
        monkeypatch.setattr(settings, "subagent_default_model", "claude-sonnet-5")
        spec = SubAgentSpec(
            agent_type=AgentType.EXPLORE, system_prompt="x", model="claude-haiku-4-5-20251001"
        )
        assert spec.resolve_model() == "claude-haiku-4-5-20251001"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_空值一律归一成None(self, blank: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
        """.env 里留空的键会读成空串,不能当模型名发给 API。"""
        monkeypatch.setattr(settings, "subagent_default_model", blank)
        spec = SubAgentSpec(agent_type=AgentType.PLAN, system_prompt="x", model=blank)
        assert spec.resolve_model(blank) is None


class TestRoundBudget:
    def test_verification轮次远高于检索类(self) -> None:
        """它要跑构建 + 相关测试 + 全量测试 + lint + 对抗探测,6 轮结构上不可能完成。"""
        verification = SUBAGENT_SPECS[AgentType.VERIFICATION].max_rounds
        assert verification >= 20
        assert verification > SUBAGENT_SPECS[AgentType.EXPLORE].max_rounds

    def test_plan轮次高于explore(self) -> None:
        assert (
            SUBAGENT_SPECS[AgentType.PLAN].max_rounds
            > SUBAGENT_SPECS[AgentType.EXPLORE].max_rounds
        )

    def test_轮次来自配置而非硬编码(self) -> None:
        assert SUBAGENT_SPECS[AgentType.EXPLORE].max_rounds == settings.subagent_max_rounds
        assert SUBAGENT_SPECS[AgentType.PLAN].max_rounds == settings.subagent_plan_max_rounds
        assert (
            SUBAGENT_SPECS[AgentType.VERIFICATION].max_rounds
            == settings.subagent_verification_max_rounds
        )

    def test_所有轮次为正(self) -> None:
        for spec in SUBAGENT_SPECS.values():
            assert spec.max_rounds >= 1


class TestMemoryInheritance:
    def test_只读检索类跳过记忆继承(self) -> None:
        """注入用户长期偏好对代码检索无益,还会挤占上下文预算。"""
        assert SUBAGENT_SPECS[AgentType.EXPLORE].omit_inherited_memory is True
        assert SUBAGENT_SPECS[AgentType.PLAN].omit_inherited_memory is True

    def test_通用型继承记忆(self) -> None:
        assert SUBAGENT_SPECS[AgentType.GENERAL_PURPOSE].omit_inherited_memory is False


class TestRoutingCatalog:
    def test_目录含全部类型与适用场景(self) -> None:
        catalog = routing_catalog()
        for agent_type in AgentType:
            assert agent_type.value in catalog
        for spec in SUBAGENT_SPECS.values():
            # when_to_use 首句应出现在目录中,主代理据此选型。
            assert spec.when_to_use.split("。")[0] in catalog

    def test_目录标明可用工具范围(self) -> None:
        catalog = routing_catalog()
        assert "Read" in catalog and "Grep" in catalog

    def test_verification入口标明VERDICT契约(self) -> None:
        assert "VERDICT" in routing_catalog()
