"""Orchestrator checkpoint 存储实现。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.models import OrchestrationCheckpointRecord
from runtime.orchestrator import OrchestrationCheckpoint


class CheckpointConflictError(RuntimeError):
    """保存旧版本快照时抛出，调用方应重新加载后决定是否恢复。"""


class CheckpointStore(Protocol):
    async def save(self, checkpoint: OrchestrationCheckpoint) -> None: ...

    async def load(self, run_id: UUID) -> OrchestrationCheckpoint | None: ...

    async def delete(self, run_id: UUID) -> None: ...


class InMemoryCheckpointStore:
    """测试和单进程开发用存储；接口与数据库实现一致。"""

    def __init__(self) -> None:
        self._items: dict[UUID, OrchestrationCheckpoint] = {}
        self._lock = asyncio.Lock()

    async def save(self, checkpoint: OrchestrationCheckpoint) -> None:
        async with self._lock:
            previous = self._items.get(checkpoint.run_id)
            if previous is not None:
                if checkpoint.revision < previous.revision:
                    raise CheckpointConflictError(
                        f"检查点版本过旧: run_id={checkpoint.run_id} "
                        f"revision={checkpoint.revision} < {previous.revision}"
                    )
                if checkpoint.revision == previous.revision:
                    if checkpoint.to_dict() == previous.to_dict():
                        return
                    raise CheckpointConflictError(
                        f"检查点版本冲突: run_id={checkpoint.run_id} "
                        f"revision={checkpoint.revision}"
                    )
            self._items[checkpoint.run_id] = OrchestrationCheckpoint.from_dict(
                checkpoint.to_dict()
            )

    async def load(self, run_id: UUID) -> OrchestrationCheckpoint | None:
        async with self._lock:
            checkpoint = self._items.get(run_id)
            if checkpoint is None:
                return None
            # 返回副本，调用方修改不会影响存储中的快照。
            return OrchestrationCheckpoint.from_dict(checkpoint.to_dict())

    async def delete(self, run_id: UUID) -> None:
        async with self._lock:
            self._items.pop(run_id, None)


class DatabaseCheckpointStore:
    """PostgreSQL 检查点存储，使用行锁和 revision 做乐观并发控制。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def save(self, checkpoint: OrchestrationCheckpoint) -> None:
        result = await self.db.execute(
            select(OrchestrationCheckpointRecord)
            .where(OrchestrationCheckpointRecord.run_id == checkpoint.run_id)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = OrchestrationCheckpointRecord(
                run_id=checkpoint.run_id,
                session_id=checkpoint.session_id,
                turn_id=checkpoint.turn_id,
                cursor=checkpoint.cursor,
                state=deepcopy(checkpoint.state),
                revision=checkpoint.revision,
            )
            self.db.add(row)
            await self.db.flush()
            return

        if checkpoint.revision < row.revision:
            raise CheckpointConflictError(
                f"检查点版本过旧: run_id={checkpoint.run_id} "
                f"revision={checkpoint.revision} < {row.revision}"
            )
        # 同版本重复保存：内容一致视为幂等重放直接返回，内容不同说明有并发写入。
        if checkpoint.revision == row.revision:
            current = OrchestrationCheckpoint(
                run_id=row.run_id,
                session_id=row.session_id,
                turn_id=row.turn_id,
                cursor=row.cursor,
                state=deepcopy(row.state),
                revision=row.revision,
            )
            if current.to_dict() == checkpoint.to_dict():
                return
            raise CheckpointConflictError(
                f"检查点版本冲突: run_id={checkpoint.run_id} "
                f"revision={checkpoint.revision}"
            )

        row.session_id = checkpoint.session_id
        row.turn_id = checkpoint.turn_id
        row.cursor = checkpoint.cursor
        row.state = deepcopy(checkpoint.state)
        row.revision = checkpoint.revision
        await self.db.flush()

    async def load(self, run_id: UUID) -> OrchestrationCheckpoint | None:
        row = await self.db.get(OrchestrationCheckpointRecord, run_id)
        if row is None:
            return None
        return OrchestrationCheckpoint(
            run_id=row.run_id,
            session_id=row.session_id,
            turn_id=row.turn_id,
            cursor=row.cursor,
            state=deepcopy(row.state),
            revision=row.revision,
        )

    async def delete(self, run_id: UUID) -> None:
        row = await self.db.get(OrchestrationCheckpointRecord, run_id)
        if row is not None:
            await self.db.delete(row)
            await self.db.flush()
