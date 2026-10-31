"""run 事件流存储:本地与真实 Redis 两个后端参数化,覆盖顺序、游标、唤醒、超时、过期与裁剪。"""

import asyncio
import json

import pytest

from core.run_events import START_CURSOR, cursor_key

pytestmark = pytest.mark.anyio

_TURN = "turn-store"


def _data(sequence: int) -> str:
    return json.dumps({"type": "text_delta", "sequence": sequence, "text": f"第{sequence}段"})


async def test_read_order_and_cursor(run_store_factory) -> None:
    store = run_store_factory()
    cursors = [await store.append(_TURN, _data(i)) for i in range(1, 6)]

    assert cursors == sorted(cursors, key=cursor_key)
    batch = await store.wait(_TURN, START_CURSOR, timeout_ms=100)
    assert [event["sequence"] for _, event in batch] == [1, 2, 3, 4, 5]
    after = await store.wait(_TURN, cursors[2], timeout_ms=100)
    assert [event["sequence"] for _, event in after] == [4, 5]
    assert await store.first_cursor(_TURN) == cursors[0]
    assert await store.last_cursor(_TURN) == cursors[-1]


async def test_wait_is_woken_by_append(run_store_factory) -> None:
    store = run_store_factory()
    cursor = await store.append(_TURN, _data(1))

    async def _late_append() -> None:
        await asyncio.sleep(0.1)
        await store.append(_TURN, _data(2))

    writer = asyncio.create_task(_late_append())
    started = asyncio.get_running_loop().time()
    batch = await store.wait(_TURN, cursor, timeout_ms=3000)
    elapsed = asyncio.get_running_loop().time() - started
    await writer

    assert [event["sequence"] for _, event in batch] == [2]
    assert elapsed < 2


async def test_wait_times_out_and_missing_key_waits(run_store_factory) -> None:
    store = run_store_factory()

    assert await store.wait("turn-missing", START_CURSOR, timeout_ms=150) == []
    capture = await store.capture("turn-missing")
    assert not capture.exists
    assert capture.tail_cursor == START_CURSOR


async def test_capture_returns_projection_and_entries(run_store_factory) -> None:
    store = run_store_factory()
    await store.append(_TURN, _data(1))
    await store.append(_TURN, _data(2), json.dumps({"last_offset": 2}))
    await store.append(_TURN, _data(3))

    capture = await store.capture(_TURN)

    assert capture.projection == {"last_offset": 2}
    assert [event["sequence"] for _, event in capture.entries] == [1, 2, 3]
    assert capture.exists


async def test_expire_then_persist(run_store_factory) -> None:
    store = run_store_factory()
    await store.append(_TURN, _data(1), json.dumps({"last_offset": 1}))
    await store.append("turn-kept", _data(1))

    await store.expire(_TURN, 0.2)
    await store.expire("turn-kept", 0.2)
    await store.persist("turn-kept")
    await asyncio.sleep(0.4)

    assert not (await store.capture(_TURN)).exists
    assert (await store.capture("turn-kept")).exists


async def test_maxlen_trims_stream_head(run_store_factory) -> None:
    # Redis 用 MAXLEN ~ 近似裁剪(按宏节点整块删除),只保证不少于 maxlen,故写入量远大于节点大小。
    store = run_store_factory(maxlen=10)
    total = 400
    for i in range(1, total + 1):
        await store.append(_TURN, _data(i))

    capture = await store.capture(_TURN)
    sequences = [event["sequence"] for _, event in capture.entries]

    assert sequences[-1] == total
    assert sequences[0] > 1
    assert len(sequences) >= 10
    assert len(sequences) < total
    assert sequences == list(range(sequences[0], total + 1))
