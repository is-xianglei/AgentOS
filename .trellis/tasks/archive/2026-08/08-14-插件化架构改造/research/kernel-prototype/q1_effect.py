"""Q1 原型:effect 拆除顺序 — 顶层并发、单 effect 内逆序串行、两种错误策略对照。

先说保真基准(实测 vendor/cordis/src/fiber.ts):
  - fiber 级 _unload (676-686):Promise.all 并发,每个顶层 effect 单独 try/catch,
    错误 **logger.error 记日志,不上抛不聚合**(卸载永不失败,状态机继续走)。
  - 单个 effect 内 dispose (427-441):.then() 链逆序串行,某 disposer 失败会
    **跳过该 effect 链上剩余 disposer**(fail-fast),错误冒到 fiber 级被 catch。
本原型两种策略都实现,对照验证。
"""
import asyncio
import inspect
import time
import traceback

log: list[str] = []


class Effect:
    """单个 effect:disposer 逆序串行。policy=failfast 复刻 Cordis;continue_ 为对照。"""

    def __init__(self, name: str, policy: str = "failfast") -> None:
        self.name = name
        self.policy = policy
        self._disposers: list = []

    def defer(self, fn) -> None:
        self._disposers.append(fn)

    async def dispose(self) -> None:
        errors: list[Exception] = []
        pending = self._disposers[::-1]
        self._disposers.clear()
        for fn in pending:
            try:
                r = fn()
                if inspect.isawaitable(r):
                    await r
            except Exception as exc:  # CancelledError(BaseException)有意放行
                exc.add_note(f"disposer={getattr(fn, '__name__', repr(fn))} effect={self.name}")
                if self.policy == "failfast":
                    raise                # Cordis 语义:同 effect 剩余 disposer 跳过
                errors.append(exc)       # 对照策略:继续拆,最后聚合
        if errors:
            raise ExceptionGroup(f"effect {self.name!r} 拆除失败", errors)


async def dispose_fiber(effects: list[Effect], *, raise_group: bool) -> list[BaseException]:
    """顶层并发拆除。raise_group=False 复刻 Cordis(记日志不上抛);True 聚合上抛。"""
    results = await asyncio.gather(*(e.dispose() for e in effects), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors and raise_group:
        raise ExceptionGroup("fiber 拆除失败", errors)
    return errors  # Cordis 路径:调用方(内核)自行 logger.error


def make_disposer(name: str, delay: float, fail: bool = False):
    async def disposer() -> None:
        await asyncio.sleep(delay)
        log.append(name)
        if fail:
            raise RuntimeError(f"{name} 拆除时爆炸")
    disposer.__name__ = name
    return disposer


async def scenario_1_order() -> None:
    log.clear()
    a, b = Effect("A"), Effect("B")
    for n in ("a1", "a2", "a3"):
        a.defer(make_disposer(n, 0.03))
    for n in ("b1", "b2"):
        b.defer(make_disposer(n, 0.03))
    t0 = time.perf_counter()
    errs = await dispose_fiber([a, b], raise_group=False)
    dt = time.perf_counter() - t0
    print(f"[S1] 执行顺序 = {log}")
    print(f"[S1] 总耗时 = {dt:.3f}s(全串行应为 0.15s,顶层并发应约 0.09s)")
    assert not errs
    assert [x for x in log if x.startswith("a")] == ["a3", "a2", "a1"], "effect A 内必须逆序"
    assert [x for x in log if x.startswith("b")] == ["b2", "b1"], "effect B 内必须逆序"
    assert dt < 0.12, "顶层必须并发"
    print("[S1] PASS:单 effect 逆序串行 + 顶层并发\n")


async def scenario_2_failfast_cordis() -> None:
    """Cordis 保真:a2 爆炸 → a1(更早注册的)被跳过;兄弟 effect B 不受影响。"""
    log.clear()
    a = Effect("A", policy="failfast")
    a.defer(make_disposer("a1", 0.01))
    a.defer(make_disposer("a2", 0.01, fail=True))
    a.defer(make_disposer("a3", 0.01))
    b = Effect("B", policy="failfast")
    b.defer(make_disposer("b1", 0.01))
    b.defer(make_disposer("b2", 0.01))
    errs = await dispose_fiber([a, b], raise_group=False)
    print(f"[S2] failfast(Cordis 语义)执行顺序 = {log}")
    assert "a3" in log and "a2" in log and "a1" not in log, "同 effect 后续 disposer 应被跳过"
    assert "b1" in log and "b2" in log, "兄弟 effect 必须不受影响"
    assert len(errs) == 1 and isinstance(errs[0], RuntimeError)
    print(f"[S2] 错误被收集不冒泡(Cordis 是 logger.error): {errs[0]!r}")
    print("[S2] PASS:同 effect fail-fast 跳过剩余;兄弟 effect 全部跑完\n")


async def scenario_3_continue_aggregate() -> None:
    """对照策略:effect 内 continue-on-error + 全量聚合 ExceptionGroup 上抛。"""
    log.clear()
    a = Effect("A", policy="continue_")
    a.defer(make_disposer("a1", 0.01))
    a.defer(make_disposer("a2", 0.01, fail=True))
    a.defer(make_disposer("a3", 0.01))
    b = Effect("B", policy="continue_")
    b.defer(make_disposer("b1", 0.01, fail=True))
    b.defer(make_disposer("b2", 0.01))
    try:
        await dispose_fiber([a, b], raise_group=True)
        raise AssertionError("应抛 ExceptionGroup")
    except ExceptionGroup as eg:
        assert set(log) == {"a1", "a2", "a3", "b1", "b2"}, "continue 策略下全部执行"
        text = "".join(traceback.format_exception(eg))
        assert "disposer=a2 effect=A" in text and "disposer=b1 effect=B" in text
        print(f"[S3] continue_ 策略执行顺序 = {log}")
        print("[S3] 聚合结构:fiber 级 EG → effect 级 EG → 原始异常;add_note 摘录:")
        for line in text.splitlines():
            if "disposer=" in line:
                print("      " + line.strip())
        print("[S3] PASS:continue-on-error + 两层 ExceptionGroup,归属可定位\n")


async def main() -> None:
    await scenario_1_order()
    await scenario_2_failfast_cordis()
    await scenario_3_continue_aggregate()


if __name__ == "__main__":
    asyncio.run(main())
