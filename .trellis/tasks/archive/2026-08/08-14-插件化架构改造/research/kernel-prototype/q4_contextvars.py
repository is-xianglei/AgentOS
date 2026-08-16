"""Q4-b:contextvars 替代显式 ctx —— 哪些路径天然正确,哪些边界仍要手工接力。"""
import asyncio
from contextvars import ContextVar

current_ctx: ContextVar["Context"] = ContextVar("current_ctx")


class Context:
    def __init__(self, name: str) -> None:
        self.name = name
        self.disposers: list = []

    def effect(self, install):
        d = install()
        self.disposers.append(d)
        return d

    def unload(self) -> None:
        for d in reversed(self.disposers):
            d()
        self.disposers.clear()


class ToolRegistry:
    """注册 API 不再收 caller 参数:归属自动取自 current_ctx。"""

    def __init__(self, own_ctx: Context) -> None:
        self.ctx = own_ctx
        self.tools: dict[str, str] = {}

    def register(self, name: str) -> None:
        caller = current_ctx.get()   # 忘绑 ctx 时这里抛 LookupError → 失败很响
        def install():
            self.tools[name] = caller.name
            return lambda: self.tools.pop(name, None)
        caller.effect(install)


async def run_plugin(ctx: Context, apply) -> None:
    """内核在调 apply 前压入插件自己的 ctx —— getTraceable 的 contextvars 版。"""
    token = current_ctx.set(ctx)
    try:
        await apply()
    finally:
        current_ctx.reset(token)


class MiniBus:
    def __init__(self) -> None:
        self.naive: list = []
        self.bound: list = []   # (listener, 注册时捕获的 owner ctx)

    def on_naive(self, fn) -> None:
        self.naive.append(fn)

    def on_bound(self, fn) -> None:
        self.bound.append((fn, current_ctx.get()))

    def emit(self) -> None:
        for fn in self.naive:
            fn()                                  # 在发射方的上下文里跑
        for fn, owner in self.bound:
            token = current_ctx.set(owner)        # 分发前重绑监听器归属(内核内部一次 set/reset)
            try:
                fn()
            finally:
                current_ctx.reset(token)


async def main() -> None:
    provider = Context("provider")
    registry = ToolRegistry(provider)

    # (a) 直接调用:consumer.apply 里调 registry.register,无需传 ctx
    consumer = Context("tool-bash")
    async def apply_a():
        registry.register("bash")                 # ← 比显式版少一个 ctx 参数
    await run_plugin(consumer, apply_a)
    assert registry.tools == {"bash": "tool-bash"}
    consumer.unload()
    assert registry.tools == {}
    print("(a) PASS 直接调用:归属自动正确,调用点最顺手")

    # (b) apply 里 spawn 的后台任务,apply 返回、token 已 reset 之后才注册
    consumer_b = Context("tool-late")
    task_box: list[asyncio.Task] = []
    async def apply_b():
        async def later():
            await asyncio.sleep(0.01)             # 此刻 run_plugin 早已 reset token
            registry.register("late")
        task_box.append(asyncio.create_task(later()))
    await run_plugin(consumer_b, apply_b)
    await task_box[0]
    assert registry.tools.get("late") == "tool-late", registry.tools
    print("(b) PASS 后台任务:create_task 拷贝上下文,token reset 后归属仍正确")

    # (c) 事件监听器:分发发生在发射方上下文里 → 必须由总线重绑
    bus = MiniBus()
    owner = Context("listener-owner")
    async def apply_c():
        bus.on_naive(lambda: registry.register("via-naive"))
        bus.on_bound(lambda: registry.register("via-bound"))
    await run_plugin(owner, apply_c)
    emitter = Context("emitter")
    async def apply_emit():
        bus.emit()
    await run_plugin(emitter, apply_emit)
    assert registry.tools["via-naive"] == "emitter", "naive:归属错挂到发射方!"
    assert registry.tools["via-bound"] == "listener-owner"
    print("(c) 事件分发:naive 归属=emitter(错);总线注册时捕获 owner、分发时重绑 → 正确")

    # (d) 队列边界:worker 常驻在 provider 上下文里,contextvar 在这里断链
    q: asyncio.Queue = asyncio.Queue()
    async def worker():
        while True:
            item = await q.get()
            if item is None:
                return
            name, captured = item
            if captured is None:
                registry.register(name)           # 归属 = worker 的上下文(provider)
            else:
                token = current_ctx.set(captured) # 入队时捕获、出队时恢复 → 手工接力
                try:
                    registry.register(name)
                finally:
                    current_ctx.reset(token)
    worker_task: list[asyncio.Task] = []
    async def apply_provider():
        worker_task.append(asyncio.create_task(worker()))
    await run_plugin(provider, apply_provider)
    consumer_d = Context("tool-queued")
    async def apply_d():
        await q.put(("q1", None))                 # 忘了接力
        await q.put(("q2", current_ctx.get()))    # 正确接力
    await run_plugin(consumer_d, apply_d)
    await q.put(None)
    await worker_task[0]
    assert registry.tools["q1"] == "provider", "断链:归属错挂到 provider"
    assert registry.tools["q2"] == "tool-queued"
    print("(d) 队列边界:contextvar 天然断链(q1 挂给 provider);须入队捕获+出队恢复(q2)")
    print("    —— 显式 ctx 在此处同样要把 ctx 塞进队列消息,两种方案负担相同")

    # (e) 忘绑护栏:完全在插件生命周期之外裸调注册 API
    try:
        registry.register("outside")
        raise AssertionError
    except LookupError:
        print("(e) PASS 生命周期外裸调 → LookupError,失败很响(显式版此处是 TypeError 缺参)")


asyncio.run(main())
