from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.llm.types import ToolResultMessage
from app.repositories.session_repo import SessionRepository


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
    ):
        """创建会话并提交事务。"""
        session = await self.repo.create(
            title=title or "新会话",
            model_name=model_name,
            system_prompt=system_prompt,
            metadata=metadata or {},
        )
        await self.db.commit()
        return session

    async def create_from_first_message(self, content: str):
        """根据首条用户消息创建会话。"""
        title = self._summarize_title(content)
        return await self.repo.create(
            title=title,
            model_name=None,
            system_prompt=None,
            metadata={},
        )

    async def list(self):
        """查询全部会话列表。"""
        return await self.repo.list()

    async def get_required(self, session_id: int):
        """查询会话，不存在时抛出业务错误。"""
        session = await self.repo.get(session_id)
        if session is None:
            raise NotFoundError("SESSION_NOT_FOUND", "会话不存在")
        return session

    async def archive(self, session_id: int):
        """归档指定会话并提交事务。"""
        session = await self.get_required(session_id)
        await self.repo.update_status(session, "archived")
        await self.db.commit()
        return session

    async def ensure_runnable(self, session_id: int):
        """校验会话当前是否允许继续执行。"""
        session = await self.repo.get_for_update(session_id)
        if session is None:
            raise NotFoundError("SESSION_NOT_FOUND", "会话不存在")
        if session.status not in {"idle", "created", "failed"}:
            raise ConflictError("SESSION_NOT_IDLE", "会话当前不可执行")
        return session

    async def mark_running(self, session_id: int):
        """将可执行会话标记为运行中。"""
        session = await self.ensure_runnable(session_id)
        await self.repo.update_status(session, "running")
        return session

    async def mark_finished(self, session, status: str):
        """会话执行结束(idle/failed)统一切状态并提交。"""
        await self.repo.update_status(session, status)
        await self.db.commit()
        return session

    async def prepare_for_message(self, session_id: int | None, content: str):
        """获取或创建本次消息所属会话，并标记为运行中。"""
        if session_id is None:
            session = await self.create_from_first_message(content)
            await self.repo.update_status(session, "running")
            return session
        return await self.mark_running(session_id)

    async def add_message(self, session_id: int, role: str, content: Any):
        """保存会话消息并估算 token 数。"""
        token_estimate = len(str(content)) // 4
        return await self.repo.add_message(session_id, role, content, token_estimate)

    async def list_messages(self, session_id: int):
        """查询指定会话的消息历史。"""
        await self.get_required(session_id)
        return await self.repo.list_messages(session_id)

    async def load_context(self, session_id: int) -> list[dict[str, Any]]:
        """加载可恢复的会话上下文。"""
        await self.get_required(session_id)
        snapshot = await self.repo.latest_snapshot(session_id)
        if snapshot is not None:
            return list(snapshot.messages)
        context: list[dict[str, Any]] = []
        for message in await self.repo.list_messages(session_id):
            if message.role in {"user", "assistant"}:
                context.append({"role": message.role, "content": message.content})
            elif message.role == "tool":
                tool_result = ToolResultMessage.from_content_dict(message.content)
                context.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_result.tool_use_id,
                                "content": tool_result.output,
                            }
                        ],
                    }
                )
        return context

    def _summarize_title(self, content: str) -> str:
        """从首条消息生成一个简短会话标题。"""
        normalized = " ".join(content.strip().split())
        if not normalized:
            return "新会话"
        if len(normalized) <= 24:
            return normalized
        return f"{normalized[:24].rstrip()}..."
