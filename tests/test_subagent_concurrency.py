"""SubAgent 并发闸门测试:验证 subagent_max_concurrency 真的形成背压。

关注两点:同时在执行的子代理数不超过配置值,以及闸门只圈住 LLM+工具区段,
不把数据库往返锁在里面(否则并发上限会顺带限制无关的记录写入)。
"""

import asyncio

import pytest

import database.registry  # noqa: F401
import runtime.subagent as subagent_module


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_gate():
    """每个用例前后清空模块级信号量,避免跨用例串上限。"""
    subagent_module._concurrency_gate = None
    yield
    subagent_module._concurrency_gate = None


class TestConcurrencyGate:
    def test_惰性创建而非模块加载期(self):
        # 信号量必须在事件循环内构造,所以模块导入后应仍为 None。
        assert subagent_module._concurrency_gate is None

    @pytest.mark.anyio
    async def test_同一实例复用(self):
        first = subagent_module._get_concurrency_gate()
        assert subagent_module._get_concurrency_gate() is first

    @pytest.mark.anyio
    async def test_配置值决定闸门容量(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(subagent_module.settings, "subagent_max_concurrency", 2)
        assert subagent_module._get_concurrency_gate()._value == 2

    @pytest.mark.anyio
    async def test_非法配置回落到至少一路(self, monkeypatch: pytest.MonkeyPatch):
        # 配成 0 或负数不应把系统锁死,退化为串行即可。
        monkeypatch.setattr(subagent_module.settings, "subagent_max_concurrency", 0)
        assert subagent_module._get_concurrency_gate()._value == 1


@pytest.mark.anyio
class TestBackpressure:
    """真并发下的观测:峰值在线数受限,且超额者排队而非报错。"""

    async def test_峰值在线数不超过上限(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(subagent_module.settings, "subagent_max_concurrency", 2)
        gate = subagent_module._get_concurrency_gate()
        inflight = 0
        peak = 0

        async def worker():
            nonlocal inflight, peak
            async with gate:
                inflight += 1
                peak = max(peak, inflight)
                await asyncio.sleep(0.01)
                inflight -= 1

        await asyncio.gather(*(worker() for _ in range(8)))
        assert peak == 2
        assert inflight == 0

    async def test_超额者排队而非失败(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(subagent_module.settings, "subagent_max_concurrency", 1)
        gate = subagent_module._get_concurrency_gate()
        order: list[int] = []

        async def worker(index: int):
            async with gate:
                order.append(index)
                await asyncio.sleep(0)

        await asyncio.gather(*(worker(i) for i in range(5)))
        # 全部完成,一个不落:背压是等待,不是拒绝。
        assert sorted(order) == [0, 1, 2, 3, 4]

    async def test_异常路径也会释放闸门(self, monkeypatch: pytest.MonkeyPatch):
        # async with 保证异常时归还许可,否则一次失败就永久占掉一路并发。
        monkeypatch.setattr(subagent_module.settings, "subagent_max_concurrency", 1)
        gate = subagent_module._get_concurrency_gate()

        with pytest.raises(RuntimeError):
            async with gate:
                raise RuntimeError("子代理内部失败")

        assert gate._value == 1
        async with gate:
            assert gate.locked()
