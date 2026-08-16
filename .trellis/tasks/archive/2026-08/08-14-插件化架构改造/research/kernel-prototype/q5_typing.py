"""Q5:ctx.require(Definition) 的类型推导 — mypy / pyright 实测(Python 3.14, PEP 695)。

四个变体:
  v1  require(type[T])+T 为 ABC 服务类     ← design 2.3 的原始写法
  v2  同签名传非 ABC 类(对照)
  v3  字符串重载(软依赖逃生门)
  v4  独立泛型令牌 Definition[T](备选方案)
"""
from abc import ABC, abstractmethod
from typing import Any, overload, reveal_type


class Service:
    pass


class ShellService(Service, ABC):
    """v1:Definition = ABC 服务类(design 2.3 写法)。"""
    SERVICE_NAME = "shell"

    @abstractmethod
    async def start(self, cmd: str) -> str: ...


class PromptService(Service):
    """v2:非 ABC 对照组(方法体 raise NotImplementedError)。"""
    SERVICE_NAME = "prompt"

    def render(self) -> str:
        raise NotImplementedError


class Context:
    @overload
    def require[T: Service](self, definition: type[T]) -> T: ...
    @overload
    def require(self, definition: str) -> Any: ...

    def require(self, definition):  # 运行时按 SERVICE_NAME / 字符串查槽位
        raise NotImplementedError

    def get[T: Service](self, definition: type[T]) -> T | None:
        raise NotImplementedError


# v4:独立泛型令牌
class Definition[T]:
    def __init__(self, name: str) -> None:
        self.name = name


SHELL_DEF: Definition[ShellService] = Definition("shell")


class Context2:
    def require[T](self, definition: Definition[T]) -> T:
        raise NotImplementedError


# 消费方 inject 声明的类型面
inject: list[type[Service] | str] = [ShellService, "tools"]


def use(ctx: Context, ctx2: Context2) -> None:
    a = ctx.require(ShellService)   # v1:ABC → mypy 是否报 type-abstract?
    reveal_type(a)                  # 期望 ShellService
    b = ctx.require(PromptService)  # v2:非 ABC 对照
    reveal_type(b)                  # 期望 PromptService
    c = ctx.require("shell")        # v3:字符串重载
    reveal_type(c)                  # 期望 Any
    d = ctx.get(ShellService)       # Optional 变体
    reveal_type(d)                  # 期望 ShellService | None
    e = ctx2.require(SHELL_DEF)     # v4:令牌
    reveal_type(e)                  # 期望 ShellService


if __name__ == "__main__":
    print("runtime OK: PEP 695 语法在 Python 3.14 可执行")
