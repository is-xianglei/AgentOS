"""Q2 原型:epoch 变更合并 — 「一轮内 provider 换代 3 次」应只触发 1 次重载。

对比四种策略:
  immediate       notify 内立即应用(无合并基线,对应 Cordis 去掉微任务合并)
  call_soon       scheduled 标志 + loop.call_soon 排 flush(design 2.4 方案)
  task_per_notify 每次 notify 直接 create_task(无 scheduled 标志)
  event_worker    asyncio.Event + 常驻 worker 循环
"""
import asyncio


class Provider:
    _seq = 0

    def __init__(self) -> None:
        Provider._seq += 1
        self.uid = Provider._seq


class Registry:
    def __init__(self) -> None:
        self.store: dict[str, Provider] = {}
        self.consumers: list["Consumer"] = []

    def set(self, name: str, p: Provider) -> None:
        self.store[name] = p
        for c in self.consumers:
            c.notify()

    def delete(self, name: str) -> None:
        self.store.pop(name, None)
        for c in self.consumers:
            c.notify()


class Consumer:
    RELOAD_COST = 0.01  # 模拟一次 unload+load 的耗时

    def __init__(self, reg: Registry, inject: list[str], strategy: str) -> None:
        self.reg, self.inject, self.strategy = reg, inject, strategy
        self.wakeups = 0   # flush/对账被唤醒次数
        self.reloads = 0   # 实际发生的重载(启停)次数
        self.active_epoch: str | None = None
        self._scheduled = False
        self._dirty = False
        self._reconciling = False
        reg.consumers.append(self)
        if strategy == "event_worker":
            self._ev = asyncio.Event()
            self._worker = asyncio.create_task(self._worker_loop())

    def compute_epoch(self) -> str | None:
        parts = []
        for name in self.inject:
            p = self.reg.store.get(name)
            if p is None:
                return None
            parts.append(str(p.uid))
        return ":".join(parts)

    def notify(self) -> None:
        loop = asyncio.get_running_loop()
        if self.strategy == "immediate":
            self.wakeups += 1
            if (target := self.compute_epoch()) != self.active_epoch:
                self.reloads += 1          # 无延迟模型:每次变化都立刻启停
                self.active_epoch = target
        elif self.strategy == "call_soon":
            if self._scheduled:
                return                     # 本轮已排过 flush → 合并点
            self._scheduled = True
            loop.call_soon(self._flush)
        elif self.strategy == "task_per_notify":
            loop.create_task(self._task_flush())
        else:
            self._ev.set()

    def _flush(self) -> None:
        self._scheduled = False
        self.wakeups += 1
        self._dirty = True
        if not self._reconciling:
            asyncio.get_running_loop().create_task(self._reconcile())

    async def _task_flush(self) -> None:
        self.wakeups += 1
        self._dirty = True
        if not self._reconciling:
            await self._reconcile()

    async def _reconcile(self) -> None:
        # 单飞 + 脏位:重载期间来的变更由 while 下一圈吸收,不丢更新
        self._reconciling = True
        try:
            while self._dirty:
                self._dirty = False
                if (target := self.compute_epoch()) != self.active_epoch:
                    self.reloads += 1
                    await asyncio.sleep(self.RELOAD_COST)
                    self.active_epoch = target
        finally:
            self._reconciling = False

    async def _worker_loop(self) -> None:
        while True:
            await self._ev.wait()
            self._ev.clear()
            self.wakeups += 1
            if (target := self.compute_epoch()) != self.active_epoch:
                self.reloads += 1
                await asyncio.sleep(self.RELOAD_COST)
                self.active_epoch = target

    def stop(self) -> None:
        if self.strategy == "event_worker":
            self._worker.cancel()


async def run_scenario(strategy: str, scenario: str) -> tuple[int, int, str]:
    reg = Registry()
    c = Consumer(reg, ["svc"], strategy)
    p0 = Provider()
    reg.set("svc", p0)
    await asyncio.sleep(0.05)      # 初始激活收敛
    c.wakeups = c.reloads = 0      # 只统计场景内动作
    if scenario == "burst3":       # 同一任务步内同步连换 3 次
        reg.set("svc", Provider()); reg.set("svc", Provider()); reg.set("svc", Provider())
    elif scenario == "spread3":    # 换 3 次,之间各让出一次事件循环
        reg.set("svc", Provider()); await asyncio.sleep(0)
        reg.set("svc", Provider()); await asyncio.sleep(0)
        reg.set("svc", Provider())
    elif scenario == "flap":       # 同一步内拔掉又原样插回(净变化为零)
        reg.delete("svc"); reg.set("svc", p0)
    await asyncio.sleep(0.1)       # 等全部重载收敛
    ok = "终态一致" if c.active_epoch == c.compute_epoch() else "!!丢更新!!"
    c.stop()
    return c.wakeups, c.reloads, ok


async def main() -> None:
    for scenario, note in [
        ("burst3", "一轮内连换 3 次 provider(断言:应只重载 1 次)"),
        ("spread3", "3 次换代之间各隔一次 await(跨轮次,不承诺合并)"),
        ("flap", "同轮内拔掉又原样插回(净变化为零,应 0 次重载)"),
    ]:
        print(f"—— 场景 {scenario}:{note}")
        for s in ("immediate", "call_soon", "task_per_notify", "event_worker"):
            w, r, ok = await run_scenario(s, scenario)
            print(f"   {s:16s} 唤醒={w}  重载={r}  {ok}")


if __name__ == "__main__":
    asyncio.run(main())
