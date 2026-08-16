"""Q3 场景验证(一):emit / parallel / serial / bail。"""
import asyncio
import traceback

from q3_bus import EventBus

record: list[str] = []


async def t_emit() -> None:
    bus = EventBus()
    order: list[str] = []

    def sync_fail(x):
        order.append("sync_fail")
        raise RuntimeError("同步监听器挂了")

    async def async_late(x):
        order.append("async_late")

    def sync_ok(x):
        order.append("sync_ok")

    for fn in (sync_fail, async_late, sync_ok):
        bus.on("e", fn)
    print("[emit] 下面这段 traceback 是故意打的日志,不是测试失败 ——")
    bus.emit("e", 1)
    assert order == ["sync_fail", "sync_ok"], "emit 返回时异步监听器还没跑"
    await asyncio.sleep(0)
    assert "async_late" in order
    print("[emit] PASS:同步异常吞掉不打断后续;异步监听器 fire-and-forget 延后一拍执行\n")


async def t_parallel() -> None:
    bus = EventBus()

    async def fail_fast(x):
        raise RuntimeError("fail_fast 挂了")

    async def ok_slow(x):
        await asyncio.sleep(0.03)
        record.append("ok_slow 完成")

    async def fail_late(x):
        await asyncio.sleep(0.01)
        raise ValueError("fail_late 挂了")

    for fn in (fail_fast, ok_slow, fail_late):
        bus.on("e", fn)
    record.clear()
    try:
        await bus.parallel("e", 1)
        raise AssertionError("应抛 ExceptionGroup")
    except ExceptionGroup as eg:
        text = "".join(traceback.format_exception(eg))
        assert "ok_slow 完成" in record, "allSettled:失败不得打断慢监听器"
        assert ".fail_fast event=e" in text and ".fail_late event=e" in text  # add_note 带 qualname
        assert 'raise RuntimeError("fail_fast' in text, "原始 traceback 帧必须还在"
        print("[parallel] PASS:allSettled + ExceptionGroup + add_note 定位 + 原始帧齐全")
        for ln in text.splitlines():
            if "listener=" in ln:
                print("   |", ln.strip())
        print()


async def t_serial_bail() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def l_none(x):
        calls.append("none")

    def l_false(x):
        calls.append("false")
        return False

    async def l_zero(x):
        calls.append("zero")
        return 0

    async def l_never(x):
        calls.append("never")

    for fn in (l_none, l_false, l_zero, l_never):
        bus.on("s", fn)
    r = await bus.serial("s", 1)
    assert r == 0 and r is not False and calls == ["none", "false", "zero"]

    bus2 = EventBus()
    bus2.on("b", lambda x: None)
    bus2.on("b", lambda x: "hit")
    bus2.on("b", lambda x: "unreached")
    assert bus2.bail("b", 1) == "hit"

    async def async_listener(x):
        return 1

    bus3 = EventBus()
    bus3.on("b", async_listener)
    try:
        bus3.bail("b", 1)
        raise AssertionError
    except TypeError as e:
        assert "只接受同步监听器" in str(e)
    print("[serial/bail] PASS:首个非 None/False 胜出(0/'' 也算胜出);bail 拒绝异步监听器\n")


async def main() -> None:
    await t_emit()
    await t_parallel()
    await t_serial_bail()


asyncio.run(main())
