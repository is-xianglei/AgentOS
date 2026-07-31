"""verification 触发阈值测试。

阈值必须是运行时判定而非提示词自律,所以这里断言的是:什么样的回合会被判定
需要验收、什么不会、以及提示不会重复注入导致回合停不下来。
"""

from typing import Any

import pytest

import database.registry  # noqa: F401
from core.events import Actor, ActorRole
from hooks import HookContext, HookEvent
from hooks.builtin import verification_trigger
from tools.subagents.trigger import FILE_COUNT_THRESHOLD, assess, compose_hint


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _write(path: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"t-{path}", "name": "Write", "input": {"file_path": path}}


def _edit(path: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"e-{path}", "name": "Edit", "input": {"file_path": path}}


def _read(path: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"r-{path}", "name": "Read", "input": {"file_path": path}}


def _agent(subagent_type: str) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": f"a-{subagent_type}",
        "name": "Agent",
        "input": {"subagent_type": subagent_type, "prompt": "x"},
    }


def _ctx(*blocks: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "assistant", "content": list(blocks)}]


class TestFileCountThreshold:
    def test_无改动不触发(self):
        assert not assess(_ctx(_read("a.py"), _read("b.py"))).should_verify

    def test_空上下文不触发(self):
        assert not assess(None).should_verify
        assert not assess([]).should_verify

    def test_单文件改动不触发(self):
        # README 类改动既不够数量也不命中路径特征。
        assert not assess(_ctx(_write("README.md"))).should_verify

    def test_两个无关文件不触发(self):
        result = assess(_ctx(_write("a.md"), _write("b.md")))
        assert not result.should_verify
        assert len(result.changed_files) == 2

    def test_达到文件数阈值即触发(self):
        blocks = [_write(f"doc{i}.md") for i in range(FILE_COUNT_THRESHOLD)]
        result = assess(_ctx(*blocks))
        assert result.should_verify
        assert any("文件数" in reason for reason in result.reasons)

    def test_同一文件多次编辑只算一处(self):
        # 反复改一个文件不应凑成阈值,否则迭代式编辑会一直被提示。
        blocks = [_edit("a.md") for _ in range(5)]
        result = assess(_ctx(*blocks))
        assert result.changed_files == ("a.md",)
        assert not result.should_verify

    def test_只读工具不计入(self):
        blocks = [_read(f"x{i}.py") for i in range(6)]
        assert assess(_ctx(*blocks)).changed_files == ()


class TestPathBasedTriggers:
    """路径特征命中即触发,不再看文件数:一处后端改动也可能是破坏性的。"""

    def test_单个service变更即触发(self):
        result = assess(_ctx(_edit("memory/service.py")))
        assert result.should_verify
        assert "包含后端 / API 变更" in result.reasons

    def test_单个api目录变更即触发(self):
        assert assess(_ctx(_edit("api/router.py"))).should_verify

    def test_repository与models也算后端(self):
        assert assess(_ctx(_edit("skill/repository.py"))).should_verify
        assert assess(_ctx(_edit("task/models.py"))).should_verify

    def test_迁移变更算基础设施(self):
        result = assess(_ctx(_write("alembic/versions/0008_x.py")))
        assert result.should_verify
        assert "包含基础设施变更" in result.reasons

    def test_依赖清单变更算基础设施(self):
        assert assess(_ctx(_edit("pyproject.toml"))).should_verify
        assert assess(_ctx(_edit("uv.lock"))).should_verify

    def test_路径匹配忽略大小写(self):
        assert assess(_ctx(_write("Dockerfile"))).should_verify

    def test_绝对路径同样命中(self):
        assert assess(_ctx(_edit("/repo/AgentOS/auth/service.py"))).should_verify

    def test_多个原因会一起给出(self):
        result = assess(
            _ctx(_edit("api/deps.py"), _edit("alembic/env.py"), _write("database/engine.py"))
        )
        assert len(result.reasons) == 3


class TestIdempotence:
    """已验收过就不再提示,否则回合永远结束不了。"""

    def test_已派发verification后不再触发(self):
        result = assess(_ctx(_edit("api/router.py"), _agent("verification")))
        assert not result.should_verify
        # 阈值本身仍然成立,只是不再需要提示。
        assert result.reasons

    def test_派发其他类型不算已验收(self):
        assert assess(_ctx(_edit("api/router.py"), _agent("Explore"))).should_verify

    def test_general_purpose不顶替验收(self):
        assert assess(_ctx(_edit("api/router.py"), _agent("general-purpose"))).should_verify


class TestRobustness:
    """上下文形态不规整时不能抛异常,阈值判定不该拖垮回合收尾。"""

    def test_content为字符串时跳过(self):
        assert not assess([{"role": "assistant", "content": "纯文本"}]).should_verify

    def test_content块缺少input时跳过(self):
        blocks = [{"type": "tool_use", "id": "1", "name": "Write"}]
        assert assess(_ctx(*blocks)).changed_files == ()

    def test_input非字典时跳过(self):
        blocks = [{"type": "tool_use", "id": "1", "name": "Write", "input": "oops"}]
        assert assess(_ctx(*blocks)).changed_files == ()

    def test_路径为空串时跳过(self):
        blocks = [{"type": "tool_use", "id": "1", "name": "Write", "input": {"file_path": "  "}}]
        assert assess(_ctx(*blocks)).changed_files == ()

    def test_notebook路径字段也识别(self):
        blocks = [
            {
                "type": "tool_use",
                "id": "1",
                "name": "NotebookEdit",
                "input": {"notebook_path": "api/x.ipynb"},
            }
        ]
        assert assess(_ctx(*blocks)).changed_files == ("api/x.ipynb",)


class TestHintText:
    def test_提示包含变更清单与调用方式(self):
        assessment = assess(_ctx(_edit("api/router.py"), _edit("auth/service.py")))
        hint = compose_hint(assessment)
        assert "api/router.py" in hint
        assert "auth/service.py" in hint
        assert "verification" in hint

    def test_提示给出正当跳过路径(self):
        # 没有退出条件的强制续跑会变成死循环,提示里必须允许说明理由后结束。
        hint = compose_hint(assess(_ctx(_edit("api/router.py"))))
        assert "无需验收" in hint

    def test_提示写明触发原因(self):
        hint = compose_hint(assess(_ctx(_write("alembic/versions/0009_y.py"))))
        assert "基础设施" in hint


def _hook_ctx(role: ActorRole, messages: list[dict[str, Any]] | None) -> HookContext:
    return HookContext(
        event=HookEvent.STOP,
        session_id=1,
        actor=Actor(role=role, name="orchestrator"),
        messages=messages,
    )


@pytest.mark.anyio
class TestStopHook:
    """hook 层:只对主代理生效,且达标时返回 continuation 强制续跑。"""

    async def test_主代理达标时强制续跑(self):
        outcome = await verification_trigger(
            _hook_ctx(ActorRole.ORCHESTRATOR, _ctx(_edit("api/router.py")))
        )
        assert outcome is not None
        assert "verification" in outcome.continuation

    async def test_主代理未达标时正常结束(self):
        outcome = await verification_trigger(
            _hook_ctx(ActorRole.ORCHESTRATOR, _ctx(_write("notes.md")))
        )
        assert outcome is None

    async def test_子代理不注入提示(self):
        # 子代理拿不到 Agent 工具,提示它去派发只会让它反复撞墙。
        outcome = await verification_trigger(
            _hook_ctx(ActorRole.SUBAGENT, _ctx(_edit("api/router.py")))
        )
        assert outcome is None

    async def test_团队成员不注入提示(self):
        outcome = await verification_trigger(
            _hook_ctx(ActorRole.TEAMMATE, _ctx(_edit("api/router.py")))
        )
        assert outcome is None

    async def test_已验收后不再续跑(self):
        outcome = await verification_trigger(
            _hook_ctx(
                ActorRole.ORCHESTRATOR,
                _ctx(_edit("api/router.py"), _agent("verification")),
            )
        )
        assert outcome is None

    async def test_无消息上下文时不报错(self):
        assert await verification_trigger(_hook_ctx(ActorRole.ORCHESTRATOR, None)) is None
