"""Memory Phase 3 提取输入、校验和候选分类单元测试。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from core.config import settings
from models.session import SessionMessage
from repositories.session_repo import SessionRepository
from services.memory_extraction_service import (
    ExtractedMemory,
    ExtractionMessage,
    MemoryExtractionResult,
    MemoryExtractionService,
)
from services.memory_service import MEMORY_BODY_MAX_BYTES


def _candidate_payload(**overrides) -> dict:
    payload = {
        "memory_key": "user-preference-tabs",
        "type": "user",
        "name": "缩进偏好",
        "description": "用户偏好使用制表符缩进",
        "body": "用户编写代码时偏好使用制表符，而不是空格。",
        "source_message_ids": [1],
    }
    payload.update(overrides)
    return payload


def _candidate(**overrides) -> ExtractedMemory:
    return ExtractedMemory.model_validate(_candidate_payload(**overrides))


def _message(message_id: int, role: str, content) -> SessionMessage:
    return SessionMessage(
        id=message_id,
        session_id=9,
        role=role,
        content=content,
        token_estimate=0,
    )


class _FakeApplyJobRepo:
    """按 apply 所需的最小契约返回持有 Lease 的提取 Job。"""

    def __init__(self, job) -> None:
        self.job = job

    async def require_owned_lease(self, job_id, worker_id):
        assert job_id == self.job.id
        return self.job


class _FakeApplySessionRepo:
    def __init__(self, session, turn, messages) -> None:
        self.session = session
        self.turn = turn
        self.messages = messages

    async def get(self, session_id):
        assert session_id == self.session.id
        return self.session

    async def get_turn(self, turn_id):
        assert turn_id == self.turn.id
        return self.turn

    async def list_messages_for_extraction(self, session_id, **kwargs):
        assert session_id == self.session.id
        return list(self.messages)


class _FakeApplyMemoryRepo:
    """记录写路径调用，模拟 supersede 所需的最小 Repository 行为。"""

    def __init__(self, space, items_by_key: dict) -> None:
        self.space = space
        self.items_by_key = items_by_key
        self.next_item_id = 200
        self.state_changes: list[tuple[int, str, int | None]] = []
        self.created_items: list = []
        self.revision_item_ids: list[int] = []
        self.catalog_increments = 0

    async def get_space(self, workspace_id, user_id, *, for_update):
        assert for_update is True
        return self.space

    async def count_active_items(self, workspace_id, user_id, space_id):
        return len([item for item in self.items_by_key.values() if item.status == "active"])

    async def get_active_item_by_key(self, workspace_id, user_id, memory_key, *, for_update):
        assert for_update is True
        item = self.items_by_key.get(memory_key)
        if item is not None and item.status != "active":
            return None
        return item

    async def create_item(self, workspace_id, user_id, *, space, memory_key, **kwargs):
        self.next_item_id += 1
        item = SimpleNamespace(
            id=self.next_item_id,
            memory_key=memory_key,
            memory_type=kwargs["memory_type"],
            name=kwargs["name"],
            description=kwargs["description"],
            body=kwargs["body"],
            status="active",
            superseded_by_id=None,
            version=1,
        )
        self.created_items.append(item)
        return item

    async def update_item(self, workspace_id, user_id, space, item, **kwargs):
        item.memory_type = kwargs["memory_type"]
        item.name = kwargs["name"]
        item.description = kwargs["description"]
        item.body = kwargs["body"]
        item.version += 1
        return item

    async def update_item_state(
        self,
        workspace_id,
        user_id,
        space,
        item,
        *,
        status,
        superseded_by_id,
    ):
        # 与真实 Repository 相同的不变量：superseded 状态必须且只能指定替代 Memory。
        assert (status == "superseded") == (superseded_by_id is not None)
        item.status = status
        item.superseded_by_id = superseded_by_id
        item.version += 1
        self.state_changes.append((item.id, status, superseded_by_id))
        return item

    async def create_revision(self, workspace_id, user_id, space, item, **kwargs):
        self.revision_item_ids.append(item.id)
        return SimpleNamespace(id=300 + len(self.revision_item_ids), memory_id=item.id)

    async def get_current_revision(self, workspace_id, user_id, item):
        return SimpleNamespace(id=399, memory_id=item.id)

    async def create_source_if_absent(self, workspace_id, user_id, **kwargs):
        return None

    async def increment_catalog_version(self, workspace_id, user_id, space):
        self.catalog_increments += 1
        space.catalog_version += 1
        return space.catalog_version


def _apply_service_with_existing_item(existing_body: str):
    """构造带一条既有 active Memory 的 apply 测试环境。"""
    turn_id = uuid4()
    job = SimpleNamespace(
        id=uuid4(),
        job_type="extract",
        session_id=9,
        turn_id=turn_id,
        space_id=17,
        payload={"mode": "active"},
    )
    session = SimpleNamespace(id=9, user_id=3, workspace_id=5)
    turn = SimpleNamespace(
        id=turn_id,
        session_id=9,
        user_id=3,
        workspace_id=5,
        status="completed",
        started_message_id=1,
        completed_message_id=2,
    )
    user_message = _message(1, "user", "以后请改用四个空格缩进")
    assistant_message = _message(2, "assistant", "好的，已记住该偏好")
    user_message.turn_id = turn_id
    assistant_message.turn_id = turn_id
    space = SimpleNamespace(id=17, workspace_id=5, user_id=3, catalog_version=7)
    existing_item = SimpleNamespace(
        id=101,
        memory_key="user-preference-tabs",
        memory_type="user",
        name="缩进偏好",
        description="用户偏好使用制表符缩进",
        body=existing_body,
        status="active",
        superseded_by_id=None,
        version=1,
    )
    service = object.__new__(MemoryExtractionService)
    service.db = SimpleNamespace()
    service.llm = SimpleNamespace()
    service.job_repo = _FakeApplyJobRepo(job)
    service.session_repo = _FakeApplySessionRepo(
        session,
        turn,
        [user_message, assistant_message],
    )
    service.memory_repo = _FakeApplyMemoryRepo(
        space,
        {existing_item.memory_key: existing_item},
    )
    return service, job, existing_item


def test_apply_supersedes_old_item_when_model_marks_intent() -> None:
    service, job, existing_item = _apply_service_with_existing_item("用户偏好使用制表符缩进。")
    result = MemoryExtractionResult.model_validate(
        {
            "memories": [
                _candidate_payload(
                    body="用户偏好使用四个空格缩进。",
                    supersedes_existing=True,
                )
            ]
        }
    )

    applied = asyncio.run(service.apply(job.id, "test-worker", result))

    new_item = service.memory_repo.created_items[0]
    assert applied.created_ids == (new_item.id,)
    assert applied.superseded_ids == (existing_item.id,)
    assert applied.rejected_count == 0
    assert existing_item.status == "superseded"
    assert existing_item.superseded_by_id == new_item.id
    assert new_item.body == "用户偏好使用四个空格缩进。"
    # 新旧两个 Item 各追加一条审计 Revision。
    assert sorted(service.memory_repo.revision_item_ids) == sorted(
        [new_item.id, existing_item.id]
    )
    # supersede 计入目录变更，catalog_version 只递增一次。
    assert service.memory_repo.catalog_increments == 1
    assert applied.catalog_version == 8


def test_apply_rejects_contradiction_without_supersede_mark() -> None:
    service, job, existing_item = _apply_service_with_existing_item("用户偏好使用制表符缩进。")
    result = MemoryExtractionResult.model_validate(
        {"memories": [_candidate_payload(body="用户偏好使用四个空格缩进。")]}
    )

    applied = asyncio.run(service.apply(job.id, "test-worker", result))

    assert applied.created_ids == ()
    assert applied.superseded_ids == ()
    assert applied.rejected_count == 1
    assert applied.rejected_candidates == (
        {"memory_key": "user-preference-tabs", "reason": "conflict"},
    )
    assert existing_item.status == "active"
    assert existing_item.superseded_by_id is None
    assert service.memory_repo.catalog_increments == 0
    assert applied.catalog_version == 7
    # payload 中只保留 memory_key 和原因枚举等引用信息，不含 Memory 正文。
    payload = applied.to_payload()
    assert payload["rejected_candidates"] == [
        {"memory_key": "user-preference-tabs", "reason": "conflict"}
    ]
    assert "四个空格" not in str(payload)


def test_apply_supersede_and_create_share_single_catalog_increment() -> None:
    service, job, existing_item = _apply_service_with_existing_item("用户偏好使用制表符缩进。")
    result = MemoryExtractionResult.model_validate(
        {
            "memories": [
                _candidate_payload(
                    body="用户偏好使用四个空格缩进。",
                    supersedes_existing=True,
                ),
                _candidate_payload(
                    memory_key="project-language-python",
                    type="project",
                    name="项目语言",
                    description="项目主要使用Python",
                    body="项目主要语言是Python 3.14。",
                    source_message_ids=[2],
                ),
            ]
        }
    )

    applied = asyncio.run(service.apply(job.id, "test-worker", result))

    assert len(applied.created_ids) == 2
    assert applied.superseded_ids == (existing_item.id,)
    assert service.memory_repo.catalog_increments == 1
    assert applied.catalog_version == 8


def test_extractor_result_declares_and_truncates_to_configured_max_items() -> None:
    limit = settings.memory_extractor_max_items
    schema = MemoryExtractionResult.model_json_schema()
    assert schema["properties"]["memories"]["maxItems"] == limit

    candidates = [
        _candidate_payload(
            memory_key=f"user-preference-{index}",
            source_message_ids=[index + 1],
        )
        for index in range(limit + 2)
    ]

    result = MemoryExtractionResult.model_validate({"memories": candidates})

    # 超量的合法输出被截断到配置上限，而不是判为非法结构导致Job直接dead。
    assert len(result.memories) == limit
    assert [candidate.memory_key for candidate in result.memories] == [
        f"user-preference-{index}" for index in range(limit)
    ]


@pytest.mark.parametrize(
    "case",
    [
        "top_extra",
        "candidate_extra",
        "string_source_id",
        "duplicate_source",
        "oversized_utf8_body",
    ],
)
def test_extractor_schema_rejects_non_strict_candidates(case: str) -> None:
    payload = {"memories": [_candidate_payload()]}
    if case == "top_extra":
        payload["unexpected"] = True
    elif case == "candidate_extra":
        payload["memories"][0]["unexpected"] = True
    elif case == "string_source_id":
        payload["memories"][0]["source_message_ids"] = ["1"]
    elif case == "duplicate_source":
        payload["memories"][0]["source_message_ids"] = [1, 1]
    else:
        payload["memories"][0]["body"] = "记" * (MEMORY_BODY_MAX_BYTES // 3 + 1)

    with pytest.raises(ValidationError):
        MemoryExtractionResult.model_validate(payload)


def test_extractor_schema_counts_body_by_utf8_bytes() -> None:
    body_at_limit = "记" * (MEMORY_BODY_MAX_BYTES // 3) + "a"

    result = MemoryExtractionResult.model_validate(
        {"memories": [_candidate_payload(body=body_at_limit)]}
    )

    assert result.memories[0].body == body_at_limit
    assert len(body_at_limit.encode("utf-8")) == MEMORY_BODY_MAX_BYTES


def test_extractor_schema_rejects_duplicate_memory_keys() -> None:
    first = _candidate_payload(source_message_ids=[1])
    second = deepcopy(first)
    second["source_message_ids"] = [2]

    with pytest.raises(ValidationError, match="重复memory_key"):
        MemoryExtractionResult.model_validate({"memories": [first, second]})


def test_assistant_projection_keeps_only_text_blocks() -> None:
    message = _message(
        1,
        "assistant",
        [
            {"type": "text", "text": "公开文本一"},
            {
                "type": "tool_use",
                "name": "shell",
                "input": {"password": "never-project-this"},
            },
            {"type": "thinking", "text": "隐藏推理"},
            {"type": "text", "text": "公开文本二"},
        ],
    )

    projected = MemoryExtractionService._project_message(message)

    assert projected == ExtractionMessage(1, "assistant", "公开文本一\n公开文本二")
    assert "password" not in projected.content
    assert "隐藏推理" not in projected.content


def test_tool_projection_only_keeps_name_status_and_bounded_summary() -> None:
    output = "不应保留的开头" + "x" * 600 + "保留的结尾"
    message = _message(
        2,
        "tool",
        {
            "tool_name": "shell",
            "is_error": True,
            "input_args": {"token": "never-project-this"},
            "output": output,
        },
    )

    projected = MemoryExtractionService._project_message(message)
    summary = projected.content.partition("summary=")[2]

    assert projected.role == "tool"
    assert projected.content.startswith("tool=shell; status=error; summary=")
    assert len(summary) == 500
    assert summary.startswith("[前文已截断]")
    assert summary.endswith("保留的结尾")
    assert "input_args" not in projected.content
    assert "never-project-this" not in projected.content


class _ExtractionWindowDb:
    def __init__(self, recent: list[SessionMessage], required: list[SessionMessage]) -> None:
        self._results = iter((recent, required))
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return next(self._results)


def test_ten_message_window_always_keeps_turn_boundaries() -> None:
    messages = {
        message_id: _message(message_id, "user", str(message_id)) for message_id in range(1, 15)
    }
    db = _ExtractionWindowDb(
        [messages[message_id] for message_id in range(13, 5, -1)],
        [messages[1], messages[14]],
    )
    repo = SessionRepository(db)

    selected = asyncio.run(
        repo.list_messages_for_extraction(
            9,
            through_message_id=14,
            required_message_ids=(1, 14),
            limit=10,
        )
    )

    assert [message.id for message in selected] == [1, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    recent_sql = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LIMIT 8" in recent_sql
    assert "session_messages.id NOT IN (1, 14)" in recent_sql


def test_character_budget_still_keeps_turn_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_extractor_max_chars", 8)
    messages = tuple(
        ExtractionMessage(message_id, "user", f"message-{message_id}" * 3)
        for message_id in range(1, 11)
    )

    selected = MemoryExtractionService._limit_message_chars(messages, {1, 10})

    assert [message.id for message in selected] == [1, 10]
    assert sum(len(message.content) for message in selected) <= 8


@pytest.mark.parametrize(
    "sensitive_text",
    [
        "password=abcdefgh",
        "Token: abcdefghijklmnop",
        "-----BEGIN PRIVATE KEY-----",
        "Cookie: session=abcdefgh",
        "eyJabcdefghijk.abcdefghijkl.abcdefghijkl",
        "我的密码是 hunter2abc",
        "登录口令：p@ssw0rd88",
        "数据库密钥为 abcd1234efgh",
        "服务器私钥是 MIIEvQIBADAN",
        "访问令牌: ya29.a0AfH6S",
        "我的令牌为 t0ken-value-1",
        "身份证号是 110101199001011234",
        "银行卡号：6222020200112233445",
        "手机验证码是 845721",
    ],
)
def test_sensitive_credentials_are_rejected(sensitive_text: str) -> None:
    candidate = _candidate(body=f"请记住以下内容：{sensitive_text}")

    assert MemoryExtractionService._contains_sensitive_candidate(candidate) is True
    assert MemoryExtractionService._safe_source_excerpt(candidate.body) is None


@pytest.mark.parametrize(
    "safe_text",
    [
        "用户偏好使用制表符缩进",
        "用户要求所有密码都存放在公司统一的保险库中管理",
        "项目要求验证码输入框放在表单底部",
    ],
)
def test_safe_preference_is_not_treated_as_sensitive(safe_text: str) -> None:
    candidate = _candidate(body=safe_text)

    assert MemoryExtractionService._contains_sensitive_candidate(candidate) is False


def test_candidate_classification_is_pure_and_complete() -> None:
    current = SimpleNamespace(
        memory_type="user",
        name="缩进偏好",
        description="用户偏好使用制表符缩进",
        body="用户偏好使用制表符。",
    )
    exact = _candidate(body=current.body)
    richer = _candidate(body="用户偏好使用制表符。该偏好适用于所有Python项目。")
    conflicting_type = _candidate(type="project", body=current.body)
    conflicting_fact = _candidate(body="用户偏好使用四个空格缩进。")
    explicit_supersede = _candidate(
        body="用户偏好使用四个空格缩进。",
        supersedes_existing=True,
    )
    supersede_wrong_type = _candidate(
        type="project",
        body="用户偏好使用四个空格缩进。",
        supersedes_existing=True,
    )

    classify = MemoryExtractionService._classify_candidate
    assert classify(None, exact) == "create"
    assert classify(current, exact) == "noop"
    assert classify(current, richer) == "update"
    assert classify(current, conflicting_type) == "conflict"
    # 正文矛盾但模型未标记推翻意图时保守拒绝，只有明确标记才允许 supersede。
    assert classify(current, conflicting_fact) == "conflict"
    assert classify(current, explicit_supersede) == "supersede"
    # 类型不一致的矛盾即使标记了推翻也不放行。
    assert classify(current, supersede_wrong_type) == "conflict"
