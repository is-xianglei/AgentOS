from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from llm.types import ToolResultMessage
from models.session import SessionMessage, SessionRecord, SessionSnapshot
from repositories.session_repo import SessionRepository


class SessionService:
    def __init__(self, db: AsyncSession):
        """初始化会话服务依赖。"""
        self.db = db
        self.repo = SessionRepository(db)

    async def create(
        self,
        title: str | None = None,
        model_name: str | None = None,
        system_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        """创建会话并提交事务。"""
        session: SessionRecord = await self.repo.create(
            title=title or "新会话",
            model_name=model_name,
            system_prompt=system_prompt,
            metadata=metadata or {},
        )
        await self.db.commit()
        return session

    async def create_from_first_message(self, content: str) -> SessionRecord:
        """根据首条用户消息创建会话。"""
        title = self._summarize_title(content)
        return await self.repo.create(
            title=title,
            model_name=None,
            system_prompt=None,
            metadata={},
        )

    async def list(self) -> list[SessionRecord]:
        """查询全部会话列表。"""
        return await self.repo.list()

    async def get_required(self, session_id: int) -> SessionRecord:
        """查询会话，不存在时抛出业务错误。"""
        session: SessionRecord = await self.repo.get(session_id)
        if session is None:
            raise AgentException.message("会话不存在")
        return session

    async def archive(self, session_id: int) -> SessionRecord:
        """归档指定会话并提交事务。"""
        session: SessionRecord = await self.get_required(session_id)
        await self.repo.update_status(session, "archived")
        await self.db.commit()
        return session

    async def rename(self, session_id: int, title: str) -> SessionRecord:
        """重命名会话标题并提交事务。"""
        session: SessionRecord = await self.get_required(session_id)
        session.title = title
        await self.db.flush()
        await self.db.refresh(session)
        await self.db.commit()
        return session

    async def delete(self, session_id: int) -> None:
        """软删除单个会话(级联标记消息/快照/任务等子表)并提交事务。"""
        session: SessionRecord = await self.get_required(session_id)
        await self.repo.delete(session)
        await self.db.commit()

    async def delete_many(self, ids: list[int]) -> int:
        """批量软删除会话,返回实际标记数量并提交事务。级联同上。"""
        deleted: int = await self.repo.delete_by_ids(ids)
        await self.db.commit()
        return deleted

    async def ensure_runnable(self, session_id: int) -> SessionRecord:
        """校验会话当前是否允许继续执行。"""
        session: SessionRecord = await self.repo.get_for_update(session_id)
        if session is None:
            raise AgentException.message("会话不存在")
        if session.status not in {"idle", "created", "failed"}:
            raise AgentException.message("会话当前不可执行")
        return session

    async def ensure_resumable(self, session_id: int) -> SessionRecord:
        """校验会话当前是否处于可从审批挂起点恢复的状态。

        只允许 awaiting_approval 态被 /approvals 端点驱动;与 ensure_runnable 的
        白名单隔离,避免普通 send_message 误入恢复流程,也避免 /approvals 驱动一个
        并未挂起的会话。
        """
        session: SessionRecord = await self.repo.get_for_update(session_id)
        if session is None:
            raise AgentException.message("会话不存在")
        if session.status != "awaiting_approval":
            raise AgentException.message("会话当前没有待审批的请求")
        return session

    async def mark_running(self, session_id: int) -> SessionRecord:
        """将可执行会话标记为运行中。"""
        session: SessionRecord = await self.ensure_runnable(session_id)
        await self.repo.update_status(session, "running")
        return session

    async def mark_finished(self, session: SessionRecord, status: str) -> SessionRecord:
        """会话执行结束(idle/failed)统一切状态并提交。"""
        await self.repo.update_status(session, status)
        await self.db.commit()
        return session

    async def set_pending_approval(self, session: SessionRecord, pending: dict[str, Any]) -> None:
        """把会话置为待审批态并写入待批指针(session.extra.pending_approval)。

        pending 结构见方案:request_id / assistant_message_id / actor / pending[] /
        done_tool_use_ids。extra 是 MutableDict,直接改并 update_status 落库。
        """
        extra = dict(session.extra or {})
        extra["pending_approval"] = pending
        session.extra = extra
        await self.repo.update_status(session, "awaiting_approval")

    def get_pending_approval(self, session: SessionRecord) -> dict[str, Any] | None:
        """读取当前待批指针(无则 None)。"""
        return (session.extra or {}).get("pending_approval")

    async def clear_pending_approval(self, session: SessionRecord) -> None:
        """清除待批指针(恢复流程裁决完全部待批项后调用)。"""
        extra = dict(session.extra or {})
        extra.pop("pending_approval", None)
        session.extra = extra
        await self.db.flush()

    async def prepare_for_message(self, session_id: int | None, content: str) -> SessionRecord:
        """获取或创建本次消息所属会话，并标记为运行中。"""
        if session_id is None:
            session: SessionRecord = await self.create_from_first_message(content)
            await self.repo.update_status(session, "running")
            return session
        return await self.mark_running(session_id)

    async def add_message(self, session_id: int, role: str, content: Any) -> SessionMessage:
        """保存会话消息并估算 token 数。"""
        token_estimate = len(str(content)) // 4
        return await self.repo.add_message(session_id, role, content, token_estimate)

    async def list_messages(self, session_id: int) -> list[SessionMessage]:
        """查询指定会话的消息历史。"""
        await self.get_required(session_id)
        return await self.repo.list_messages(session_id)

    async def load_context(self, session_id: int) -> list[dict[str, Any]]:
        """加载可恢复的会话上下文。"""
        await self.get_required(session_id)
        snapshot: SessionSnapshot = await self.repo.latest_snapshot(session_id)
        if snapshot is not None:
            return list(snapshot.messages)
        context: list[dict[str, Any]] = []
        for message in await self.repo.list_messages(session_id):
            if message.role in {"user", "assistant"}:
                context.append({"role": message.role, "content": message.content})
            elif message.role == "tool":
                tool_result = ToolResultMessage.from_content_dict(message.content)
                block = {
                    "type": "tool_result",
                    "tool_use_id": tool_result.tool_use_id,
                    "content": tool_result.output,
                }
                if context and context[-1].get("role") == "user" and isinstance(context[-1].get("content"), list) and context[-1]["content"] and context[-1]["content"][0].get("type") == "tool_result":
                    context[-1]["content"].append(block)
                else:
                    context.append({"role": "user", "content": [block]})
        return context

    def _summarize_title(self, content: str) -> str:
        """从首条消息生成一个简短会话标题。"""
        normalized = " ".join(content.strip().split())
        if not normalized:
            return "新会话"
        if len(normalized) <= 24:
            return normalized
        return f"{normalized[:24].rstrip()}..."
