from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.event_bus import StreamBus
from core.events import ORCHESTRATOR_ACTOR, Actor
from hooks import HookContext, HookEvent, get_hook_registry
from tools.models import ToolCallRecord
from tools.repository import ToolRepository
from tools.base import ToolContext
from tools.registry import ToolRegistry


class ToolService:
    def __init__(self, db: AsyncSession, registry: ToolRegistry):
        """初始化工具执行器依赖。"""
        self.db = db
        self.registry = registry
        self.tool_repo = ToolRepository(db)

    async def run(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        message_id: int | None = None,
        bus: StreamBus | None = None,
        record: ToolCallRecord | None = None,
        actor: Actor | None = None,
        turn_id: UUID | None = None,
        user_id: int | None = None,
        workspace_id: int | None = None,
    ) -> str:
        """执行工具并记录调用状态。

        这里是三类 actor(orchestrator / subagent / teammate)工具执行的唯一交汇点,
        因此把 PreToolUse / PostToolUse 两个 hook 挂在此处,一处覆盖全部产出者。

        record 非空时复用既有记录(如审批通过后复用之前的 awaiting 记录),
        回到 running 并以最新 input_args 覆盖(可能被审批时的 updated_input 修改过);
        否则新建一条 running 记录。
        """
        actor = actor or ORCHESTRATOR_ACTOR
        hooks = get_hook_registry()

        # PreToolUse:可拦截(block)或改写入参(updated_input)。permission 已在
        # 更高层(orchestrator _execute_tool_uses)裁决过,此处 hook 只能更严,不能提权。
        pre_outcomes = await hooks.trigger(
            HookContext(
                event=HookEvent.PRE_TOOL_USE,
                session_id=session_id,
                actor=actor,
                turn_id=turn_id,
                tool_name=tool_name,
                tool_input=input_args,
            )
        )
        for outcome in pre_outcomes:
            if outcome.updated_input is not None:
                input_args = outcome.updated_input
        blocked = next((o.block for o in pre_outcomes if o.block is not None), None)

        if record is None:
            record = await self.tool_repo.start(
                session_id, tool_name, input_args, message_id=message_id
            )
        else:
            record.input_args = input_args
            record.status = "running"
            await self.db.flush()

        if blocked is not None:
            # 被 PreToolUse hook 拦截:不执行工具,把拦截理由作为结果回给模型。
            await self.tool_repo.fail(record, blocked)
            await self.db.flush()
            return blocked

        try:
            tool = self.registry.get(tool_name)
            ctx = ToolContext(
                session_id=session_id,
                db=self.db,
                bus=bus,
                turn_id=turn_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            # 开始执行工具
            output = await tool.run_with_dict(input_args, ctx)
        except Exception as exc:
            await self.tool_repo.fail(record, str(exc))
            await self.db.flush()
            # 让"工具失败告警" hook 能观测到;触发后原样上抛。
            await hooks.trigger(
                HookContext(
                    event=HookEvent.POST_TOOL_USE,
                    session_id=session_id,
                    actor=actor,
                    turn_id=turn_id,
                    tool_name=tool_name,
                    tool_input=input_args,
                    tool_output=str(exc),
                    is_error=True,
                )
            )
            raise
        await self.tool_repo.succeed(record, output)
        await self.db.flush()

        # PostToolUse:观察副作用 / 检查输出(返回值不改写结果,只做观测与副作用)。
        await hooks.trigger(
            HookContext(
                event=HookEvent.POST_TOOL_USE,
                session_id=session_id,
                actor=actor,
                turn_id=turn_id,
                tool_name=tool_name,
                tool_input=input_args,
                tool_output=output,
            )
        )
        return output
