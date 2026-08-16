"""Q3 场景验证(二):waterfall 的短路、异常回卷、防呆。"""
import asyncio

from q3_bus import EventBus

events: list[str] = []


async def mw_outer(req, next_):
    events.append("outer:pre")
    try:
        res = await next_(req + ":outer改写")
        return res + "+outer后处理"
    finally:
        events.append("outer:finally")


async def mw_veto(req, next_):
    if "禁" in req:
        events.append("veto:短路")
        return "被否决"
    return await next_(req)


async def final(req):
    events.append(f"final({req})")
    return f"结果[{req}]"


async def main() -> None:
    bus = EventBus()
    bus.on("w", mw_outer)
    bus.on("w", mw_veto)

    # (1) 全通:改写的参数一路传到 final,结果被外层再加工
    r = await bus.waterfall("w", "请求", final=final)
    assert r == "结果[请求:outer改写]+outer后处理", r
    print("[waterfall-全通] PASS:next(new_args) 传参改写 + 返回值环绕加工")

    # (2) 短路:veto 不调 next → final 不执行;外层的后处理仍作用在否决值上
    events.clear()
    r = await bus.waterfall("w", "禁词", final=final)
    assert r == "被否决+outer后处理", r
    assert not any(e.startswith("final") for e in events), "短路后 final 不得执行"
    assert "outer:finally" in events
    print("[waterfall-短路] PASS:不调 next 即短路;外层中间件照常收尾并可加工否决值")

    # (3) 中途异常:boom 抛错 → final 与后续层不执行;已执行的外层经 try/finally 收尾
    async def mw_boom(req, next_):
        raise RuntimeError("中间件爆炸")

    bus2 = EventBus()
    events.clear()
    bus2.on("w", mw_outer)
    bus2.on("w", mw_boom)
    bus2.on("w", mw_veto)
    try:
        await bus2.waterfall("w", "x", final=final)
        raise AssertionError
    except RuntimeError:
        assert "outer:pre" in events and "outer:finally" in events
        assert "veto:短路" not in events and not any("final" in e for e in events)
    print("[waterfall-异常] PASS:异常沿链上抛;已执行的外层 try/finally 收尾;后续层跳过")

    # (3b) 外层可捕获内层异常并降级 —— 中间件补偿模式
    async def mw_rescue(req, next_):
        try:
            return await next_()
        except RuntimeError:
            return "降级默认值"

    bus3 = EventBus()
    bus3.on("w", mw_rescue)
    bus3.on("w", mw_boom)
    r = await bus3.waterfall("w", "x", final=final)
    assert r == "降级默认值"
    print("[waterfall-补偿] PASS:外层 try/except 环绕 next() 可捕获内层异常并降级")

    # (4) 防呆:同一中间件重复调用 next()
    async def mw_double(req, next_):
        await next_()
        return await next_()

    bus4 = EventBus()
    bus4.on("w", mw_double)
    try:
        await bus4.waterfall("w", "x", final=final)
        raise AssertionError
    except RuntimeError as e:
        assert "重复调用 next" in str(e)
    print("[waterfall-防呆] PASS:重复调用 next() 被拦截(RuntimeError)")


asyncio.run(main())
