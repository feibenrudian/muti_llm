"""UT-36-1/2：SSE 心跳 drain 助手单元测试（纯内存队列，不经 SRS）。"""

import asyncio

from app.gateways.llm_gateway import _END, _drain_with_heartbeat


async def test_heartbeat_idle_ping_and_end() -> None:
    """UT-36-1 心跳：空闲间隔发 ': ping'，_END 正常终止，数据行不丢序。"""
    queue: asyncio.Queue = asyncio.Queue()

    async def feed() -> None:
        await asyncio.sleep(0.25)  # interval=0.1 → 空闲期应触发至少 1 次心跳
        queue.put_nowait("line-1")
        queue.put_nowait("line-2")
        queue.put_nowait(_END)

    feeder = asyncio.create_task(feed())
    got: list[str] = []
    async for item in _drain_with_heartbeat(queue, 0.1):
        got.append(item)
    await feeder

    assert ": ping\n\n" in got
    data = [x for x in got if not x.startswith(": ")]
    assert data == ["line-1", "line-2"]
    # 数据行相对顺序不被心跳打乱
    assert [x for x in got if x in ("line-1", "line-2")] == ["line-1", "line-2"]


async def test_heartbeat_disabled_pure_passthrough() -> None:
    """UT-36-2 心跳关闭（interval=0）：纯透传，无 ping 行，_END 正常终止。"""
    queue: asyncio.Queue = asyncio.Queue()
    for item in ("a", "b", _END):
        queue.put_nowait(item)

    got = [item async for item in _drain_with_heartbeat(queue, 0)]
    assert got == ["a", "b"]
