"""prompt 包的组装测试：段序、按需加载与文案去重。"""

from datetime import date

from prompt import (
    PromptContext,
    compose_lead_blocks,
    compose_lead_prompt,
    compose_subagent_prompt,
    compose_teammate_prompt,
)
from prompt.sections import MEMORY_TRUST_RULES

FIXED_DAY = date(2026, 7, 31)


def _ctx(**kwargs) -> PromptContext:
    kwargs.setdefault("cwd", "/srv/agentos")
    kwargs.setdefault("today", FIXED_DAY)
    return PromptContext.create(**kwargs)


def test_段序按易失性单调递增():
    """稳定段必须物理位于易失段之前，否则易失段会击穿其后的 prompt cache。"""
    out = compose_lead_prompt(
        _ctx(
            skills_catalog="- pdf: 处理 PDF",
            session_prompt="本会话只做代码审查。",
            memory_catalog="- [pref](p.md) — 偏好简体中文",
        )
    )
    positions = [
        out.index("你是 AgentOS 的主代理"),
        out.index("## 运行环境"),
        out.index("## Memory 使用规则"),
        out.index("## 可用 skills"),
        out.index("本会话只做代码审查"),
        out.index("## Memory Catalog"),
    ]
    assert positions == sorted(positions)


def test_会话指令位于_memory_catalog_之前():
    """回归：旧实现把 session_prompt 排在 catalog 之后，catalog 一变即失效。"""
    out = compose_lead_prompt(
        _ctx(session_prompt="只读模式。", memory_catalog="- [a](a.md) — x")
    )
    assert out.index("只读模式。") < out.index("## Memory Catalog")


def test_memory_规则在无_memory_时仍加载():
    """规则属全局前缀；随 catalog 条件加载会切出两条不同前缀。"""
    out = compose_lead_prompt(_ctx())
    assert "## Memory 使用规则" in out
    assert "## Memory Catalog" not in out


def test_空输入不产生空段或多余空行():
    out = compose_lead_prompt(_ctx(skills_catalog="  ", session_prompt="", memory_catalog=None))
    assert "## 可用 skills" not in out
    assert "\n\n\n" not in out
    assert out == out.strip()


def test_环境段只含日期不含时刻():
    """时刻会让每次请求前缀不同，直接击穿缓存。"""
    out = compose_lead_prompt(_ctx())
    value = out.split("当前日期:")[1].split("\n")[0].strip()
    assert value == "2026-07-31"


def test_主代理与子代理共用同一份_memory_边界文案():
    """去重回归：两条路径曾各写一份，措辞漂移会造成安全语义差异。"""
    lead = compose_lead_prompt(_ctx(memory_catalog="- [a](a.md) — x"))
    sub = compose_subagent_prompt("你是子代理。", "记忆正文")
    assert MEMORY_TRUST_RULES in lead
    assert MEMORY_TRUST_RULES in sub


def test_子代理无记忆时不加规则():
    assert compose_subagent_prompt("你是子代理。", None) == "你是子代理。"
    assert compose_subagent_prompt("你是子代理。", "") == "你是子代理。"


def test_teammate_身份头前置且保留原_spec():
    base = "你是会话内的子代理。"
    out = compose_teammate_prompt(base, 42, "alice", "reviewer")
    assert out.startswith("你是团队成员 'alice'")
    assert "角色: reviewer" in out
    assert "会话 42" in out
    assert out.endswith(base)


def test_相同输入的_context_相等():
    """frozen dataclass 天然可比较，不需要额外的序列化 cache key。"""
    a = _ctx(session_prompt="x", memory_catalog="y")
    b = _ctx(session_prompt="x", memory_catalog="y")
    assert a == b
    assert a != _ctx(session_prompt="x", memory_catalog="z")


def test_create_默认取进程真实状态():
    ctx = PromptContext.create()
    assert ctx.cwd
    assert ctx.today == date.today()


def test_blocks_有两个断点且落在段末():
    ctx = _ctx(skills_catalog="- pdf: x", session_prompt="只读。", memory_catalog="- [a](a.md) — y")
    blocks = compose_lead_blocks(ctx)
    assert len(blocks) == 2
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in blocks)
    # 断点 1 结束于稳定段（会话指令），断点 2 只含易失的 Catalog
    assert blocks[0]["text"].endswith("只读。")
    assert blocks[1]["text"].startswith("## Memory Catalog")


def test_blocks_无_catalog_时只有一个断点():
    blocks = compose_lead_blocks(_ctx(session_prompt="只读。"))
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_blocks_拼接后与字符串变体逐字节一致():
    """两种形式必须可互换，否则改用 blocks 会静默改变模型看到的内容。"""
    for kwargs in (
        {},
        {"session_prompt": "只读。"},
        {"skills_catalog": "- pdf: x", "memory_catalog": "- [a](a.md) — y"},
        {"skills_catalog": "- pdf: x", "session_prompt": "只读。", "memory_catalog": "- [a](a.md) — y"},
    ):
        ctx = _ctx(**kwargs)
        joined = "\n\n".join(b["text"] for b in compose_lead_blocks(ctx))
        assert joined == compose_lead_prompt(ctx)


def test_断点数不超过上限():
    """API 上限为每请求 4 个 cache_control 断点。"""
    ctx = _ctx(skills_catalog="- a: x", session_prompt="p", memory_catalog="- [m](m.md) — z")
    assert sum("cache_control" in b for b in compose_lead_blocks(ctx)) <= 4
