"""WIKI-378: provider ingress is bounded by count and bytes."""

from __future__ import annotations

import asyncio

from backend.app.agent_runtime.provider import BoundedProviderEventQueue


def test_put_blocks_at_item_cap_and_resumes_after_get() -> None:
    async def run() -> None:
        queue = BoundedProviderEventQueue(max_items=2, max_bytes=1 << 30)
        await queue.put("a")
        await queue.put("b")
        blocked = asyncio.ensure_future(queue.put("c"))
        await asyncio.sleep(0.01)
        assert not blocked.done()
        assert await queue.get() == "a"
        await asyncio.wait_for(blocked, timeout=2)
        assert len(queue) == 2

    asyncio.run(run())


def test_put_blocks_at_byte_cap_and_resumes_after_get() -> None:
    async def run() -> None:
        queue = BoundedProviderEventQueue(max_items=100, max_bytes=1000)
        await queue.put("a", size=600)
        blocked = asyncio.ensure_future(queue.put("b", size=600))
        await asyncio.sleep(0.01)
        assert not blocked.done()
        assert queue.buffered_bytes == 600
        assert await queue.get() == "a"
        await asyncio.wait_for(blocked, timeout=2)
        assert queue.buffered_bytes == 600

    asyncio.run(run())


def test_oversized_event_is_admitted_when_queue_is_empty() -> None:
    # A single event larger than the byte cap must pass, or the reader
    # would deadlock on it forever.
    async def run() -> None:
        queue = BoundedProviderEventQueue(max_items=100, max_bytes=1000)
        await asyncio.wait_for(queue.put("huge", size=5000), timeout=2)
        assert await queue.get() == "huge"

    asyncio.run(run())


def test_put_forced_bypasses_caps() -> None:
    async def run() -> None:
        queue = BoundedProviderEventQueue(max_items=1, max_bytes=100)
        await queue.put("a", size=100)
        queue.put_forced("terminal", size=100)
        assert len(queue) == 2
        assert await queue.get() == "a"
        assert await queue.get() == "terminal"

    asyncio.run(run())


def test_get_nowait_matches_asyncio_queue_contract() -> None:
    async def run() -> None:
        queue = BoundedProviderEventQueue()
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        else:
            raise AssertionError("expected QueueEmpty")
        await queue.put("a")
        assert queue.get_nowait() == "a"

    asyncio.run(run())
