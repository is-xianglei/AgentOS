from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, DateTime, and_, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from interaction.models import InteractionRequestRecord, RuntimeSuspensionRecord
from memory.models import TurnMemoryContextRecord
from permission.models import PermissionRuleRecord
from plan.models import SessionPlanRecord
from session.models import SessionMessage, SessionRecord, SessionSnapshot, SessionTurnRecord
from task.models import TaskRecord
from team.models import (
    SubAgentRunRecord,
    TeamMemberRecord,
    TeamMessageRecord,
    TeamRecord,
)
from tools.models import ToolCallRecord

# 会话软删除时需一并标记的子表(均以 session_id 关联)。
# 物理删除靠外键 ON DELETE CASCADE 清理,软删除只改标记,故须在此显式登记。
_SESSION_CHILD_MODELS = (
    SessionMessage,
    SessionSnapshot,
    SessionTurnRecord,
    ToolCallRecord,
    TaskRecord,
    TeamRecord,
    TeamMemberRecord,
    TeamMessageRecord,
    SubAgentRunRecord,
    PermissionRuleRecord,
    RuntimeSuspensionRecord,
    InteractionRequestRecord,
    SessionPlanRecord,
)


def _db_clock() -> ColumnElement[datetime]:
    """数据库墙钟:用 clock_timestamp() 而非 now(),后者是事务开始时间,长事务里会停滞。"""
    return func.clock_timestamp(type_=DateTime(timezone=True))


def _lease_active() -> ColumnElement[bool]:
    """租约有效:持有者非空且未过期,由数据库时钟判断,避免各进程时钟漂移。"""
    return and_(
        SessionTurnRecord.lease_owner.is_not(None),
        SessionTurnRecord.lease_expires_at > _db_clock(),
    )


class SessionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        title: str,
        model_name: str | None,
        system_prompt: str | None,
        metadata: dict[str, Any],
        user_id: int | None = None,
        workspace_id: int | None = None,
        agent_id: int | None = None,
        workflow_id: int | None = None,
    ) -> SessionRecord:
        session = SessionRecord(
            title=title,
            status="idle",
            model_name=model_name,
            system_prompt=system_prompt,
            extra=metadata,
            user_id=user_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
        )
        self.db.add(session)
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def list(self) -> list[SessionRecord]:
        stmt = select(SessionRecord).order_by(
            desc(SessionRecord.last_active_at), desc(SessionRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_by_user(self, user_id: int) -> list[SessionRecord]:
        """查询指定用户的会话列表。"""
        stmt = (
            select(SessionRecord)
            .where(SessionRecord.user_id == user_id)
            .order_by(desc(SessionRecord.last_active_at), desc(SessionRecord.id))
        )
        return list(await self.db.scalars(stmt))

    async def list_by_user_and_workspace(
        self,
        user_id: int,
        workspace_id: int,
    ) -> list[SessionRecord]:
        """查询当前工作区内的用户会话。"""
        stmt = (
            select(SessionRecord)
            .where(
                SessionRecord.user_id == user_id,
                SessionRecord.workspace_id == workspace_id,
            )
            .order_by(desc(SessionRecord.last_active_at), desc(SessionRecord.id))
        )
        return list(await self.db.scalars(stmt))

    async def list_accessible(
        self,
        user_id: int,
        workspace_id: int,
        *,
        department_scope_ids: list[int],
        group_ids: list[int],
    ) -> list[SessionRecord]:
        """按调用方已解析的组织范围查询当前用户可读会话。"""
        access_conditions = [
            SessionRecord.user_id == user_id,
            SessionRecord.visibility.in_(("workspace", "public")),
            SessionRecord.shared_with.contains([user_id]),
        ]
        access_conditions.extend(
            SessionRecord.shared_with_departments.contains([department_id])
            for department_id in department_scope_ids
        )
        access_conditions.extend(
            SessionRecord.shared_with_groups.contains([group_id]) for group_id in group_ids
        )
        stmt = (
            select(SessionRecord)
            .where(
                SessionRecord.workspace_id == workspace_id,
                SessionRecord.user_id.is_not(None),
                or_(*access_conditions),
            )
            .order_by(desc(SessionRecord.last_active_at), desc(SessionRecord.id))
        )
        return list(await self.db.scalars(stmt))

    async def get(self, session_id: int) -> SessionRecord | None:
        return await self.db.get(SessionRecord, session_id)

    async def update_sharing(
        self,
        session: SessionRecord,
        *,
        visibility: str,
        user_ids: list[int],
        department_ids: list[int],
        group_ids: list[int],
    ) -> SessionRecord:
        """覆盖会话共享范围，提交由请求事务边界负责。"""
        session.visibility = visibility
        session.shared_with = user_ids
        session.shared_with_departments = department_ids
        session.shared_with_groups = group_ids
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def delete(self, session: SessionRecord) -> None:
        """软删除单个会话:标记会话本身并级联标记其所有子表记录。"""
        await self._soft_delete_ids([session.id])

    async def delete_by_ids(self, ids: list[int]) -> int:
        """按 id 批量软删除会话,返回实际标记的会话行数。级联同上。"""
        if not ids:
            return 0
        return await self._soft_delete_ids(ids)

    async def _soft_delete_ids(self, ids: list[int]) -> int:
        """把给定会话及其全部子表记录标记为已删除(is_deleted / deleted_at)。

        子表以 session_id 关联,逐表 bulk UPDATE;已删除的行跳过,避免覆盖首次删除时间。
        返回实际新标记的会话行数(已处于删除态的不计入)。
        """
        now = datetime.now(UTC)
        turn_ids = select(SessionTurnRecord.id).where(SessionTurnRecord.session_id.in_(ids))
        await self.db.execute(
            update(TurnMemoryContextRecord)
            .where(
                TurnMemoryContextRecord.turn_id.in_(turn_ids),
                TurnMemoryContextRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        for model in _SESSION_CHILD_MODELS:
            await self.db.execute(
                update(model)
                .where(model.session_id.in_(ids), model.is_deleted.is_(False))
                .values(is_deleted=True, deleted_at=now)
            )
        result = await self.db.execute(
            update(SessionRecord)
            .where(SessionRecord.id.in_(ids), SessionRecord.is_deleted.is_(False))
            .values(is_deleted=True, deleted_at=now)
        )
        await self.db.flush()
        return result.rowcount or 0

    async def get_for_update(self, session_id: int) -> SessionRecord | None:
        stmt = (
            select(SessionRecord)
            .where(SessionRecord.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def update_status(self, session: SessionRecord, status: str) -> SessionRecord:
        session.status = status
        session.last_active_at = datetime.now(UTC)
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def add_message(
        self,
        session_id: int,
        role: str,
        content: Any,
        token_estimate: int,
        turn_id: UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> SessionMessage:
        message = SessionMessage(
            session_id=session_id,
            role=role,
            content=content,
            token_estimate=token_estimate,
            turn_id=turn_id,
            provider=provider,
            model=model,
        )
        self.db.add(message)
        await self.db.flush()
        await self.db.refresh(message)
        return message

    async def list_messages(self, session_id: int) -> list[SessionMessage]:
        stmt = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_messages_range(
        self,
        session_id: int,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
        turn_id: UUID | None = None,
    ) -> list[SessionMessage]:
        """按消息水位或轮次查询消息，边界均为开区间。"""
        stmt = select(SessionMessage).where(SessionMessage.session_id == session_id)
        if after_message_id is not None:
            stmt = stmt.where(SessionMessage.id > after_message_id)
        if before_message_id is not None:
            stmt = stmt.where(SessionMessage.id < before_message_id)
        if turn_id is not None:
            stmt = stmt.where(SessionMessage.turn_id == turn_id)
        stmt = stmt.order_by(SessionMessage.id)
        return list(await self.db.scalars(stmt))

    async def list_messages_for_extraction(
        self,
        session_id: int,
        *,
        through_message_id: int,
        required_message_ids: tuple[int, int],
        limit: int,
    ) -> list[SessionMessage]:
        """读取提取窗口，并固定保留当前 Turn 的起止消息。"""
        if limit < len(set(required_message_ids)):
            raise ValueError("提取消息上限不能小于必选消息数量")

        remaining = limit - len(set(required_message_ids))
        recent_stmt = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.id <= through_message_id,
                SessionMessage.id.not_in(required_message_ids),
            )
            .order_by(SessionMessage.id.desc())
            .limit(remaining)
        )
        required_stmt = select(SessionMessage).where(
            SessionMessage.session_id == session_id,
            SessionMessage.id.in_(required_message_ids),
            SessionMessage.id <= through_message_id,
        )
        recent = list(await self.db.scalars(recent_stmt)) if remaining else []
        required = list(await self.db.scalars(required_stmt))
        by_id = {message.id: message for message in [*recent, *required]}
        return [by_id[message_id] for message_id in sorted(by_id)]

    async def bind_message_to_turn(
        self,
        message: SessionMessage,
        turn_id: UUID,
    ) -> SessionMessage:
        """在 Turn 创建后补齐首条消息的轮次外键。"""
        message.turn_id = turn_id
        await self.db.flush()
        await self.db.refresh(message)
        return message

    async def create_turn(
        self,
        session_id: int,
        user_id: int,
        workspace_id: int,
        started_message_id: int,
    ) -> SessionTurnRecord:
        """创建运行中的交互轮次。"""
        turn = SessionTurnRecord(
            session_id=session_id,
            user_id=user_id,
            workspace_id=workspace_id,
            status="running",
            started_message_id=started_message_id,
        )
        self.db.add(turn)
        await self.db.flush()
        await self.db.refresh(turn)
        return turn

    async def get_turn(self, turn_id: UUID) -> SessionTurnRecord | None:
        """按 ID 查询交互轮次。"""
        return await self.db.get(SessionTurnRecord, turn_id)

    async def update_turn_status(
        self,
        turn: SessionTurnRecord,
        status: str,
        completed_message_id: int | None = None,
    ) -> SessionTurnRecord:
        """更新轮次状态，并维护完成字段的不变量。"""
        turn.status = status
        if status == "completed":
            turn.completed_message_id = completed_message_id
            turn.completed_at = datetime.now(UTC)
        else:
            turn.completed_message_id = None
            turn.completed_at = None
        await self.db.flush()
        await self.db.refresh(turn)
        return turn

    async def add_snapshot(
        self,
        session_id: int,
        messages: list[Any],
        summary: str,
        through_message_id: int,
        snapshot_type: str = "compact",
    ) -> SessionSnapshot:
        snapshot = SessionSnapshot(
            session_id=session_id,
            snapshot_type=snapshot_type,
            messages=messages,
            summary=summary,
            through_message_id=through_message_id,
        )
        self.db.add(snapshot)
        await self.db.flush()
        await self.db.refresh(snapshot)
        return snapshot

    async def latest_snapshot(self, session_id: int) -> SessionSnapshot | None:
        stmt = (
            select(SessionSnapshot)
            .where(
                SessionSnapshot.session_id == session_id,
                SessionSnapshot.through_message_id.is_not(None),
            )
            .order_by(desc(SessionSnapshot.id))
            .limit(1)
        )
        return (await self.db.scalars(stmt)).first()

    # ---- 执行租约(run 即 turn) ----
    # 批量 UPDATE 不同步 identity map:调用方需要新值时用 populate_existing 重新读取。

    async def claim_turn_lease(
        self,
        turn_id: UUID,
        owner: str,
        lease_seconds: float,
    ) -> int | None:
        """无人持有或已过期时认领租约,返回 last_event_offset;未认领到返回 None。"""
        now = _db_clock()
        stmt = (
            update(SessionTurnRecord)
            .where(
                SessionTurnRecord.id == turn_id,
                or_(
                    SessionTurnRecord.lease_owner.is_(None),
                    SessionTurnRecord.lease_expires_at < now,
                ),
            )
            .values(lease_owner=owner, lease_expires_at=now + timedelta(seconds=lease_seconds))
            .returning(SessionTurnRecord.last_event_offset)
            .execution_options(synchronize_session=False)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def renew_turn_lease(self, turn_id: UUID, owner: str, lease_seconds: float) -> bool:
        """仅当租约仍由 owner 持有时续约。"""
        stmt = (
            update(SessionTurnRecord)
            .where(SessionTurnRecord.id == turn_id, SessionTurnRecord.lease_owner == owner)
            .values(lease_expires_at=_db_clock() + timedelta(seconds=lease_seconds))
            .execution_options(synchronize_session=False)
        )
        return ((await self.db.execute(stmt)).rowcount or 0) > 0

    async def reserve_turn_offsets(self, turn_id: UUID, owner: str, count: int) -> int | None:
        """在租约内预留 count 个连续 offset,返回首个;租约不属于 owner 时返回 None。"""
        stmt = (
            update(SessionTurnRecord)
            .where(SessionTurnRecord.id == turn_id, SessionTurnRecord.lease_owner == owner)
            .values(last_event_offset=SessionTurnRecord.last_event_offset + count)
            .returning(SessionTurnRecord.last_event_offset)
            .execution_options(synchronize_session=False)
        )
        last = (await self.db.execute(stmt)).scalar_one_or_none()
        return None if last is None else last - count + 1

    async def get_turn_lease_owner(self, turn_id: UUID) -> str | None:
        stmt = select(SessionTurnRecord.lease_owner).where(SessionTurnRecord.id == turn_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_turn_for_update(self, turn_id: UUID) -> SessionTurnRecord | None:
        stmt = (
            select(SessionTurnRecord)
            .where(SessionTurnRecord.id == turn_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def get_turn_lease_state(
        self,
        turn_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[SessionTurnRecord, bool] | None:
        """返回轮次及其租约是否有效;for_update 时锁定轮次行。"""
        stmt = (
            select(SessionTurnRecord, _lease_active())
            .where(SessionTurnRecord.id == turn_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update(of=SessionTurnRecord)
        row = (await self.db.execute(stmt)).first()
        return None if row is None else (row[0], bool(row[1]))

    async def clear_turn_lease(
        self,
        turn_id: UUID,
        *,
        owner: str | None = None,
        reserve_offset: bool = False,
    ) -> int | None:
        """清除租约;reserve_offset 时同语句为 run_end 分配一个 offset。

        owner 非空时仅清除该持有者的租约。返回清除后的 last_event_offset,未命中返回 None。
        """
        conditions = [SessionTurnRecord.id == turn_id]
        if owner is not None:
            conditions.append(SessionTurnRecord.lease_owner == owner)
        values: dict[str, Any] = {"lease_owner": None, "lease_expires_at": None}
        if reserve_offset:
            values["last_event_offset"] = SessionTurnRecord.last_event_offset + 1
        stmt = (
            update(SessionTurnRecord)
            .where(*conditions)
            .values(**values)
            .returning(SessionTurnRecord.last_event_offset)
            .execution_options(synchronize_session=False)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_reapable_turn_ids(self, *, include_unleased_running: bool) -> list[UUID]:
        """租约已过期的轮次;启动时另含 running 且无租约的迁移前遗留轮次。"""
        expired = and_(
            SessionTurnRecord.lease_owner.is_not(None),
            SessionTurnRecord.lease_expires_at <= _db_clock(),
        )
        condition = expired
        if include_unleased_running:
            condition = or_(
                expired,
                and_(
                    SessionTurnRecord.lease_owner.is_(None),
                    SessionTurnRecord.status == "running",
                ),
            )
        stmt = select(SessionTurnRecord.id).where(condition)
        return list((await self.db.execute(stmt)).scalars())

    async def get_latest_turn_lease_state(
        self,
        session_id: int,
    ) -> tuple[SessionTurnRecord, bool] | None:
        """会话最新一轮及其租约是否有效;按起始消息 ID 排序,同一事务内也有确定顺序。"""
        stmt = (
            select(SessionTurnRecord, _lease_active())
            .where(SessionTurnRecord.session_id == session_id)
            .order_by(desc(SessionTurnRecord.started_message_id))
            .limit(1)
            .execution_options(populate_existing=True)
        )
        row = (await self.db.execute(stmt)).first()
        return None if row is None else (row[0], bool(row[1]))
