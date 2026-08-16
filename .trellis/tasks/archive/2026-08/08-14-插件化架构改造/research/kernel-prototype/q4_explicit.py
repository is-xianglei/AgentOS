"""Q4-a:显式 ctx 人机工程学 — 正确用法、两类误用、_own/_caller 辅助的实际拦截力。"""


class Fiber:
    def __init__(self, name: str) -> None:
        self.name = name
        self.disposers: list = []


class Context:
    def __init__(self, name: str) -> None:
        self.fiber = Fiber(name)

    def effect(self, install):
        disposer = install()
        self.fiber.disposers.append(disposer)
        return disposer

    def unload(self) -> None:
        for d in reversed(self.fiber.disposers):
            d()
        self.fiber.disposers.clear()


class Service:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def _own_effect(self, install):
        """副作用归属服务提供方自己(随 provider 卸载而撤销)。"""
        return self.ctx.effect(install)

    def _caller_effect(self, caller: Context, install):
        """副作用归属调用方(随 caller 卸载而撤销)—— 注册类 API 的正确姿势。"""
        return caller.effect(install)


class ToolRegistry(Service):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.tools: dict[str, str] = {}

    def register(self, caller: Context, name: str):
        def install():
            self.tools[name] = caller.fiber.name
            return lambda: self.tools.pop(name, None)
        return self._caller_effect(caller, install)

    def register_buggy(self, name: str):
        """误用①:服务作者忘收 caller,顺手用了 self.ctx。"""
        def install():
            self.tools[name] = self.ctx.fiber.name
            return lambda: self.tools.pop(name, None)
        return self._own_effect(install)


def main() -> None:
    provider_ctx = Context("provider")
    registry = ToolRegistry(provider_ctx)

    # —— 正确用法:消费方插件的 apply 里,签名里的 ctx 要再传一次 ——
    consumer_ctx = Context("tool-bash")
    registry.register(consumer_ctx, "bash")   # 实际代码形如:ctx.tools.register(ctx, tool)
    assert registry.tools == {"bash": "tool-bash"}
    consumer_ctx.unload()
    assert registry.tools == {}, "卸载消费方即撤销注册"
    print("[正确] register(ctx, tool):卸载 consumer → 注册撤销")

    # —— 误用①:服务作者错用 self.ctx —— 泄漏到 provider 卸载才消失
    consumer2 = Context("tool-grep")
    registry.register_buggy("grep")           # consumer2 的 apply 里调的,但归属挂错
    consumer2.unload()
    assert "grep" in registry.tools, "误用①:consumer 卸载后注册仍在 → 泄漏"
    provider_ctx.unload()
    assert "grep" not in registry.tools
    print("[误用①] 服务作者错用 self.ctx:consumer 卸载不撤销,静默泄漏,运行时无报错")

    # —— 误用②:调用方把服务自己的 ctx 递回去(self.ctx 就在手边,最顺手的错误)——
    provider2 = Context("provider2")
    registry2 = ToolRegistry(provider2)
    consumer3 = Context("tool-ls")
    registry2.register(registry2.ctx, "ls")   # 应传 consumer3 的 ctx,却传了服务自己的
    consumer3.unload()
    assert "ls" in registry2.tools, "误用②同样静默泄漏"
    print("[误用②] 调用方递错 ctx(传成服务自己的):同样静默泄漏,运行时无报错")

    # 结论:_own_effect/_caller_effect 只改善「读代码时能看出来」;
    # 两类误用运行时都不报错 —— 纯纪律,结构上挡不住。
    print("[结论] 辅助方法=命名卫生;真正兜底要靠 A1⑤ 式计数断言测试 + 静态检查首参约定")


main()
