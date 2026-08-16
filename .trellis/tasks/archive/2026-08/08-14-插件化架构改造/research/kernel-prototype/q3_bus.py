"""Q3 原型:五种事件分发的 asyncio 实现(emit / parallel / serial / bail / waterfall)。"""
import asyncio
import inspect
import traceback


def _name(fn) -> str:
    return getattr(fn, "__qualname__", repr(fn))


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list] = {}
        self._bg_tasks: set[asyncio.Task] = set()  # emit 的异步监听器必须持引用防 GC

    def on(self, event: str, fn) -> None:
        self._listeners.setdefault(event, []).append(fn)

    def _get(self, event: str) -> list:
        return list(self._listeners.get(event, []))

    # 1. emit:同步广播,忽略返回值;监听器异常记日志不冒泡
    def emit(self, event: str, *args) -> None:
        for fn in self._get(event):
            try:
                r = fn(*args)
            except Exception:
                print(f"[emit] 监听器 {_name(fn)} 异常(已吞,仅记日志):")
                traceback.print_exc()
                continue
            if inspect.isawaitable(r):
                t = asyncio.ensure_future(r)
                self._bg_tasks.add(t)
                t.add_done_callback(lambda t, fn=fn: self._on_bg_done(event, fn, t))

    def _on_bg_done(self, event: str, fn, t: asyncio.Task) -> None:
        self._bg_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            print(f"[emit] 异步监听器 {_name(fn)} 后台异常(已吞): {t.exception()!r}")

    # 2. parallel:全部并发跑完(allSettled),失败聚合为 ExceptionGroup
    async def parallel(self, event: str, *args) -> None:
        listeners = self._get(event)

        async def run(fn):
            r = fn(*args)
            if inspect.isawaitable(r):
                await r

        results = await asyncio.gather(*(run(fn) for fn in listeners), return_exceptions=True)
        errors = []
        for fn, res in zip(listeners, results):
            if isinstance(res, BaseException):
                res.add_note(f"listener={_name(fn)} event={event}")
                errors.append(res)
        if errors:
            raise ExceptionGroup(
                f"parallel({event}): {len(errors)}/{len(listeners)} 个监听器失败", errors,
            )

    # 3. serial:依次 await,首个非 None/False 返回值胜出并停止
    async def serial(self, event: str, *args):
        for fn in self._get(event):
            r = fn(*args)
            if inspect.isawaitable(r):
                r = await r
            if r is not None and r is not False:
                return r
        return None

    # 4. bail:serial 的同步版,只许同步监听器
    def bail(self, event: str, *args):
        for fn in self._get(event):
            r = fn(*args)
            if inspect.isawaitable(r):
                r.close()
                raise TypeError(f"bail({event}) 只接受同步监听器,{_name(fn)} 返回了 awaitable")
            if r is not None and r is not False:
                return r
        return None

    # 5. waterfall:环绕中间件,末参 next;不调 next 即短路
    async def waterfall(self, event: str, *args, final):
        listeners = self._get(event)

        async def dispatch(i: int, cur_args: tuple):
            if i == len(listeners):
                r = final(*cur_args)
                return (await r) if inspect.isawaitable(r) else r
            called = False

            async def next_(*new_args):
                nonlocal called
                if called:
                    raise RuntimeError(
                        f"waterfall({event}): {_name(listeners[i])} 重复调用 next()"
                    )
                called = True
                return await dispatch(i + 1, new_args if new_args else cur_args)

            r = listeners[i](*cur_args, next_)
            return (await r) if inspect.isawaitable(r) else r

        return await dispatch(0, args)
