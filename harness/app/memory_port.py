"""应用层端口：记忆 Provider。

主循环只认识「召回」和「回合结束写入」两个动作，记忆怎么存、怎么检索由实现决定。
内置实现包装 memory 域（harness.adapters.memory.builtin_provider），以后接外部记忆框架时
新增一个实现即可，不改主循环。端口与模型端口互不相关，单独成模块，不并入 ports.py。

事务约定：调用方持有数据库会话并负责提交。recall、on_turn_completed 在调用方的事务里执行、
从不提交；after_commit 在调用方提交之后执行，自己管理事务。
"""

from abc import ABC, abstractmethod

from harness.domain.memory import MemoryRecall, MemoryWriteTicket, TurnRef


class MemoryProvider(ABC):
    """记忆来源：为回合召回记忆，回合完成时写入记忆。

    调用方负责运行模式的取舍（如极速模式不召回、不写入），实现只管记忆本身。
    """

    @abstractmethod
    async def recall(self, turn: TurnRef) -> MemoryRecall | None:
        """为主 Agent 的本轮召回记忆；同一回合多次调用返回相同结果（首次召回即冻结）。

        在调用方的事务里执行，可以写入（内置实现冻结召回结果、回写使用热度），但不提交；
        返回非 None 时由调用方提交，子代理在独立的数据库会话里经 inherited 读取，提交后才可见。
        返回 None 表示本轮没有记忆（灰度未命中、功能关闭等），调用方无需提交。
        召回失败直接抛出，调用方按本轮失败处理。
        """

    @abstractmethod
    async def inherited(self, turn: TurnRef) -> MemoryRecall | None:
        """子代理继承父回合已冻结的召回结果；turn 指父回合，会话取子代理所在的会话。

        只读，在调用方的会话里执行、不写入；父回合没有冻结结果时返回 None，绝不触发新的召回。
        """

    @abstractmethod
    async def on_turn_completed(self, turn: TurnRef) -> MemoryWriteTicket | None:
        """回合完成时写入记忆（内置实现为登记提取任务），在调用方提交回合终态的同一事务里、提交之前调用。

        写入失败不得影响回合终态：实现自行隔离失败（内置实现用 SAVEPOINT 包住入队，失败只回滚
        SAVEPOINT 并记日志），除取消外不向上抛出，也不提交事务。返回的回执由调用方在提交之后
        交给 after_commit；返回 None 表示本轮没有写入。
        """

    @abstractmethod
    async def after_commit(self, ticket: MemoryWriteTicket) -> None:
        """调用方提交回合终态之后执行写入的后续动作（内置实现为就地运行提取任务）。

        不使用调用方的事务，需要数据库时自行开启；失败只记日志，除取消外不向上抛出。
        """
