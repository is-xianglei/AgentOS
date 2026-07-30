from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from team.models import TeamMemberRecord, TeamMessageRecord, TeamRecord


class TeamRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_members(self, session_id: int) -> list[TeamMemberRecord]:
        stmt = (
            select(TeamMemberRecord)
            .where(TeamMemberRecord.session_id == session_id)
            .order_by(TeamMemberRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def get_member(self, session_id: int, name: str) -> TeamMemberRecord | None:
        stmt = select(TeamMemberRecord).where(
            TeamMemberRecord.session_id == session_id,
            TeamMemberRecord.name == name,
        )
        return (await self.db.scalars(stmt)).first()

    async def create_member(self, session_id: int, name: str, role: str) -> TeamMemberRecord:
        member = TeamMemberRecord(
            session_id=session_id,
            name=name,
            role=role,
            status="idle",
        )
        self.db.add(member)
        await self.db.flush()
        await self.db.refresh(member)
        return member

    async def update_member_status(
        self,
        member: TeamMemberRecord,
        status: str,
    ) -> TeamMemberRecord:
        member.status = status
        await self.db.flush()
        await self.db.refresh(member)
        return member

    async def list_messages(self, session_id: int) -> list[TeamMessageRecord]:
        stmt = (
            select(TeamMessageRecord)
            .where(TeamMessageRecord.session_id == session_id)
            .order_by(TeamMessageRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def send_message(
        self,
        session_id: int,
        sender: str,
        recipient: str,
        content: str,
        message_type: str = "direct",
        consumer_instance_id: str | None = None,
    ) -> TeamMessageRecord:
        message = TeamMessageRecord(
            session_id=session_id,
            sender=sender,
            recipient=recipient,
            message_type=message_type,
            content=content,
            consumer_instance_id=consumer_instance_id,
        )
        self.db.add(message)
        await self.db.flush()
        await self.db.refresh(message)
        return message

    async def unread_messages(
        self,
        session_id: int,
        recipient: str,
        consumer_instance_id: str | None = None,
    ) -> list[TeamMessageRecord]:
        conditions = [
            TeamMessageRecord.session_id == session_id,
            TeamMessageRecord.recipient == recipient,
            TeamMessageRecord.read_at.is_(None),
        ]
        if consumer_instance_id is None:
            conditions.append(TeamMessageRecord.consumer_instance_id.is_(None))
        else:
            conditions.append(
                or_(
                    TeamMessageRecord.consumer_instance_id.is_(None),
                    TeamMessageRecord.consumer_instance_id == consumer_instance_id,
                )
            )
        stmt = (
            select(TeamMessageRecord)
            .where(*conditions)
            .order_by(TeamMessageRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def mark_read(self, messages: list[TeamMessageRecord]) -> None:
        now = datetime.now(timezone.utc)
        for message in messages:
            message.read_at = now
        await self.db.flush()

    # ----- 团队(会话 1:1) -----

    async def get_team(self, session_id: int) -> TeamRecord | None:
        stmt = select(TeamRecord).where(TeamRecord.session_id == session_id)
        return (await self.db.scalars(stmt)).first()

    async def create_team(
        self,
        session_id: int,
        team_name: str = "default",
        creator_type: str = "agent",
        creator_id: str | None = None,
    ) -> TeamRecord:
        team = TeamRecord(
            session_id=session_id,
            team_name=team_name,
            creator_type=creator_type,
            creator_id=creator_id,
        )
        self.db.add(team)
        await self.db.flush()
        await self.db.refresh(team)
        return team

    async def get_or_create_team(
        self,
        session_id: int,
        team_name: str = "default",
        creator_type: str = "agent",
        creator_id: str | None = None,
    ) -> TeamRecord:
        team = await self.get_team(session_id)
        if team is not None:
            return team
        return await self.create_team(
            session_id=session_id,
            team_name=team_name,
            creator_type=creator_type,
            creator_id=creator_id,
        )

    # ----- 成员运行时状态 -----

    async def update_member_history(
        self,
        member: TeamMemberRecord,
        history_json: str | None,
    ) -> TeamMemberRecord:
        member.history = history_json
        await self.db.flush()
        await self.db.refresh(member)
        return member

    async def update_member_runtime(
        self,
        member: TeamMemberRecord,
        status: str | None = None,
        agent_type: str | None = None,
        prompt: str | None = None,
        team_id: int | None = None,
    ) -> TeamMemberRecord:
        if status is not None:
            member.status = status
        if agent_type is not None:
            member.agent_type = agent_type
        if prompt is not None:
            member.prompt = prompt
        if team_id is not None:
            member.team_id = team_id
        await self.db.flush()
        await self.db.refresh(member)
        return member
