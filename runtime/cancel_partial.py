"""取消收尾:从 turn 渲染状态还原主代理尚未落库的消息,供取消方与执行方在终态事务里补写。

主循环在挂起或完成时才提交,中途取消会回滚本次执行写下的全部 assistant / tool 消息,
而这些内容已经 emit 到事件流、折叠进 TurnState。本模块据此还原,规则:
- 只取最近一次 run_started 之后建块的主代理块,更早的块属于已提交的执行;
- 某个 tool_result 的 sequence 早于后一个块的建块 sequence 时,后一个块属于下一次 LLM 调用,
  据此切分成多条 assistant 消息;
- tool_use 只保留已收到完整入参的块(半截流式的 tool_use 丢弃);没有结果的 tool_use
  补一条 is_error 的「已取消」结果,保证历史里 tool_use 与 tool_result 成对;
- thinking 一律不保存:事件流不携带签名,无签名的思考块对 Anthropic 无法回传;
- 既无文本也无 tool_use 的调用不产出消息。

同进程取消读执行方 RunStreamBus 的内存状态,跨进程取消读事件流快照,两条路径共用本函数。
"""

from __future__ import annotations

from typing import Any

from core.events import ActorRole
from core.run_state import ActorState, Block, TurnState
from llm.types import ToolResultMessage, ToolUse, build_assistant_blocks
from session.service import MessageDraft

CANCELLED_TOOL_OUTPUT = "用户取消了本轮执行，该工具调用未完成，结果未知。"


def extract_unsaved_messages(state: TurnState) -> list[MessageDraft]:
    """返回主代理在最近一次执行中产出、但因取消回滚而未落库的消息,按原顺序排列。"""
    actor = _orchestrator(state)
    if actor is None:
        return []
    start = state.execution_start_offset
    blocks = [
        block
        for block in actor.blocks
        if block.sequence is not None and block.sequence > start and block.index >= 0
    ]
    drafts: list[MessageDraft] = []
    for round_blocks in _split_rounds(blocks):
        drafts.extend(_round_messages(round_blocks))
    return drafts


def _orchestrator(state: TurnState) -> ActorState | None:
    for actor in state.actors:
        if actor.actor.get("role") == ActorRole.ORCHESTRATOR.value:
            return actor
    return None


def _split_rounds(blocks: list[Block]) -> list[list[Block]]:
    """工具结果在整次 LLM 调用结束后才产出,故结果早于某块建块即说明该块属于下一次调用。"""
    rounds: list[list[Block]] = []
    current: list[Block] = []
    for block in blocks:
        if current and _has_result_before(current, block.sequence):
            rounds.append(current)
            current = []
        current.append(block)
    if current:
        rounds.append(current)
    return rounds


def _has_result_before(blocks: list[Block], sequence: int | None) -> bool:
    if sequence is None:
        return False
    return any(
        block.result_sequence is not None and block.result_sequence < sequence
        for block in blocks
    )


def _round_messages(blocks: list[Block]) -> list[MessageDraft]:
    text = "".join(block.text for block in blocks if block.kind == "text")
    tool_blocks = [
        (block, tool_use)
        for block in blocks
        if block.kind == "tool_use" and (tool_use := _complete_tool_use(block)) is not None
    ]
    if not text.strip():
        text = ""
    if not text and not tool_blocks:
        return []
    tool_uses = [tool_use for _, tool_use in tool_blocks]
    drafts = [MessageDraft("assistant", build_assistant_blocks(text, tool_uses))]
    drafts.extend(
        MessageDraft("tool", _tool_result(tool_use, block.result).to_content_dict())
        for block, tool_use in tool_blocks
    )
    return drafts


def _complete_tool_use(block: Block) -> ToolUse | None:
    """tool_use 事件在入参完整后才发出;只见 block_start 的块没有可回放的调用。"""
    tool = block.tool or {}
    tool_id, name = tool.get("id"), tool.get("name")
    if not isinstance(tool_id, str) or not tool_id or not isinstance(name, str) or not name:
        return None
    raw_input = tool.get("input")
    return ToolUse(id=tool_id, name=name, input=raw_input if isinstance(raw_input, dict) else {})


def _tool_result(tool_use: ToolUse, result: dict[str, Any] | None) -> ToolResultMessage:
    if result is None:
        return ToolResultMessage(
            tool_use_id=tool_use.id,
            tool_name=tool_use.name,
            input_args=tool_use.input,
            output=CANCELLED_TOOL_OUTPUT,
            is_error=True,
        )
    return ToolResultMessage(
        tool_use_id=tool_use.id,
        tool_name=tool_use.name,
        input_args=tool_use.input,
        output=str(result.get("output") or ""),
        is_error=bool(result.get("is_error")),
    )
