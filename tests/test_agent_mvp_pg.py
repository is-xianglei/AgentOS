import io
import zipfile
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models import AgentSkillRecord
from agent.service import AgentService
from core.errors import AgentException
from core.storage import ObjectMeta
from database.engine import engine
from runtime.agent import AgentRuntime
from session.service import SessionService
from skill.service import SkillService
from tools.models import ToolCallRecord
from tools.service import ToolCatalogService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> Any:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


class _MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def ensure_bucket(self) -> None:
        return None

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        del content_type
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise AgentException.message(f"对象不存在: {key}", status_code=404)
        return self.objects[key]

    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        return [
            ObjectMeta(key=key, size=len(data))
            for key, data in self.objects.items()
            if key.startswith(prefix)
        ]

    async def delete_prefix(self, prefix: str) -> None:
        for key in [key for key in self.objects if key.startswith(prefix)]:
            del self.objects[key]

    async def exists(self, key: str) -> bool:
        return key in self.objects


def _bundle(name: str, body: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: 工作区隔离测试\n---\n\n{body}\n",
        )
    return stream.getvalue()


async def _workspace_owner(db: AsyncSession, label: str) -> tuple[UserRecord, WorkspaceRecord]:
    suffix = uuid4().hex
    user = UserRecord(
        email=f"{label}-{suffix}@example.invalid",
        username=f"{label}-{suffix[:12]}",
        password="test-only-hash",
    )
    workspace = WorkspaceRecord(
        name=f"{label}-{suffix}",
        slug=f"{label}-{suffix}",
        display_name=label,
    )
    db.add_all([user, workspace])
    await db.flush()
    db.add(
        WorkspaceMemberRecord(
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
            joined_at=datetime.now(UTC),
        )
    )
    await db.flush()
    return user, workspace


@pytest.mark.anyio
async def test_同名Skill按工作区隔离且覆盖保持ID(db: AsyncSession) -> None:
    owner_a, workspace_a = await _workspace_owner(db, "skill-a")
    owner_b, workspace_b = await _workspace_owner(db, "skill-b")
    storage = _MemoryStorage()
    service = SkillService(db, storage=storage)

    first_a = await service.upload_bundle(
        _bundle("shared-skill", "工作区A第一版"),
        workspace_a.id,
        owner_a.id,
    )
    first_b = await service.upload_bundle(
        _bundle("shared-skill", "工作区B内容"),
        workspace_b.id,
        owner_b.id,
    )
    replaced_a = await service.upload_bundle(
        _bundle("shared-skill", "工作区A覆盖内容"),
        workspace_a.id,
        owner_a.id,
    )

    assert first_a.id != first_b.id
    assert replaced_a.id == first_a.id
    assert await service.load_body(workspace_a.id, "shared-skill") == "工作区A覆盖内容"
    assert await service.load_body(workspace_b.id, "shared-skill") == "工作区B内容"
    assert f"{workspace_a.id}/shared-skill/SKILL.md" in storage.objects
    assert f"{workspace_b.id}/shared-skill/SKILL.md" in storage.objects


@pytest.mark.anyio
async def test_Agent绑定隔离运行配置和调用追踪(db: AsyncSession) -> None:
    owner_a, workspace_a = await _workspace_owner(db, "agent-a")
    owner_b, workspace_b = await _workspace_owner(db, "agent-b")
    storage = _MemoryStorage()
    skill_service = SkillService(db, storage=storage)
    skill_a = await skill_service.upload_bundle(
        _bundle("private-skill", "仅工作区A可见"),
        workspace_a.id,
        owner_a.id,
    )
    skill_b = await skill_service.upload_bundle(
        _bundle("private-skill", "仅工作区B可见"),
        workspace_b.id,
        owner_b.id,
    )
    tool_service = ToolCatalogService(db)
    await tool_service.sync_builtins()
    echo = await tool_service.repo.get_by_runtime_name("echo")
    assert echo is not None

    agents = AgentService(db)
    agent = await agents.create_agent(
        workspace_id=workspace_a.id,
        actor_user_id=owner_a.id,
        name="受限助手",
        description=None,
        system_prompt="只使用绑定能力",
        model_name="test-model",
        is_enabled=True,
        tool_ids=[echo.id],
        skill_ids=[skill_a.id],
    )
    with pytest.raises(AgentException) as exc_info:
        await agents.replace_skill_bindings(
            workspace_a.id,
            agent.id,
            owner_a.id,
            [skill_b.id],
        )
    assert exc_info.value.status_code == 404

    sessions = SessionService(db)
    session = await sessions.prepare_for_message(
        None,
        "开始测试",
        owner_a.id,
        workspace_a.id,
        agent.id,
    )
    runtime = AgentRuntime(db, user_id=owner_a.id, workspace_id=workspace_a.id)
    await runtime._configure_agent(session)

    assert runtime.active_agent_prompt == "只使用绑定能力"
    assert runtime.active_model_name == "test-model"
    assert runtime.allowed_skill_names == frozenset({"private-skill"})
    assert runtime.tool_registry.names == {"echo", "Skill", "SkillResource", "SkillRun"}

    output = await runtime.tool_service.run(
        session.id,
        "echo",
        {"text": "ok"},
        user_id=owner_a.id,
        workspace_id=workspace_a.id,
        agent_id=agent.id,
        allowed_skill_names=runtime.allowed_skill_names,
    )
    call = (
        await db.scalars(
            select(ToolCallRecord)
            .where(ToolCallRecord.session_id == session.id)
            .order_by(ToolCallRecord.id.desc())
        )
    ).first()

    assert output == "ok"
    assert call is not None
    assert call.tool_id == echo.id
    assert call.agent_id == agent.id

    other = await agents.create_agent(
        workspace_id=workspace_a.id,
        actor_user_id=owner_a.id,
        name="另一个助手",
        description=None,
        system_prompt="另一个提示词",
        model_name=None,
        is_enabled=True,
        tool_ids=[],
        skill_ids=[],
    )
    with pytest.raises(AgentException) as switch_error:
        await sessions.prepare_for_message(
            session.id,
            "尝试切换",
            owner_a.id,
            workspace_a.id,
            other.id,
        )
    assert switch_error.value.status_code == 409

    assert await skill_service.delete_skill(
        workspace_a.id,
        "private-skill",
        owner_a.id,
    )
    binding = (
        await db.scalars(
            select(AgentSkillRecord)
            .where(
                AgentSkillRecord.agent_id == agent.id,
                AgentSkillRecord.skill_id == skill_a.id,
            )
            .execution_options(include_deleted=True)
        )
    ).first()
    assert binding is not None
    assert binding.is_deleted is True


@pytest.mark.anyio
async def test_Agent替换绑定后返回当前有效集合(db: AsyncSession) -> None:
    owner, workspace = await _workspace_owner(db, "replace-bindings")
    storage = _MemoryStorage()
    skill_service = SkillService(db, storage=storage)
    first_skill = await skill_service.upload_bundle(
        _bundle("first-skill", "第一个Skill"),
        workspace.id,
        owner.id,
    )
    second_skill = await skill_service.upload_bundle(
        _bundle("second-skill", "第二个Skill"),
        workspace.id,
        owner.id,
    )
    tool_service = ToolCatalogService(db)
    await tool_service.sync_builtins()
    echo = await tool_service.repo.get_by_runtime_name("echo")
    weather = await tool_service.repo.get_by_runtime_name("Weather")
    assert echo is not None
    assert weather is not None

    service = AgentService(db)
    agent = await service.create_agent(
        workspace_id=workspace.id,
        actor_user_id=owner.id,
        name="替换绑定助手",
        description=None,
        system_prompt="验证绑定刷新",
        model_name=None,
        is_enabled=True,
        tool_ids=[echo.id],
        skill_ids=[first_skill.id],
    )

    agent = await service.replace_tool_bindings(
        workspace.id,
        agent.id,
        owner.id,
        [weather.id],
    )
    assert [binding.tool_id for binding in agent.tool_bindings] == [weather.id]

    agent = await service.replace_skill_bindings(
        workspace.id,
        agent.id,
        owner.id,
        [second_skill.id],
    )
    assert [binding.skill_id for binding in agent.skill_bindings] == [second_skill.id]


@pytest.mark.anyio
async def test_新会话未选择Agent时保留默认运行时(db: AsyncSession) -> None:
    owner, workspace = await _workspace_owner(db, "default-agent")
    session = await SessionService(db).prepare_for_message(
        None,
        "使用默认Agent",
        owner.id,
        workspace.id,
    )
    runtime = AgentRuntime(db, user_id=owner.id, workspace_id=workspace.id)
    await runtime._configure_agent(session)

    assert session.agent_id is None
    assert runtime.active_agent_id is None
    assert runtime.allowed_skill_names is None
    assert {"echo", "Bash", "Skill"}.issubset(runtime.tool_registry.names)
