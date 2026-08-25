from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .types import LifecycleState, ProviderKind, RunRecord, utc_now

INGRESS_MAX_ITEMS = 10_000
INGRESS_MAX_BYTES = 64 * 1024 * 1024
INGRESS_DEFAULT_EVENT_SIZE = 256


class BoundedProviderEventQueue:
    """Provider ingress with count and byte caps (WIKI-378).

    A full queue blocks ``put`` — backpressure reaches the provider pipe
    instead of growing supervisor memory. ``put_forced`` bypasses the caps:
    terminal markers (process exit, stream end) are emitted from reader
    shutdown paths that must never park (the 2026-08-24 archive-stall class).
    """

    def __init__(
        self,
        *,
        max_items: int = INGRESS_MAX_ITEMS,
        max_bytes: int = INGRESS_MAX_BYTES,
    ) -> None:
        self._items: deque[tuple[Any, int]] = deque()
        self._bytes = 0
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def buffered_bytes(self) -> int:
        return self._bytes

    def _full_for(self, size: int) -> bool:
        if not self._items:
            return False
        return (
            len(self._items) >= self._max_items
            or self._bytes + size > self._max_bytes
        )

    def _append(self, item: Any, size: int) -> None:
        self._items.append((item, size))
        self._bytes += size
        self._not_empty.set()

    async def put(self, item: Any, *, size: int = INGRESS_DEFAULT_EVENT_SIZE) -> None:
        while self._full_for(size):
            self._not_full.clear()
            await self._not_full.wait()
        self._append(item, size)

    def put_forced(
        self, item: Any, *, size: int = INGRESS_DEFAULT_EVENT_SIZE
    ) -> None:
        self._append(item, size)

    def _pop(self) -> Any:
        item, size = self._items.popleft()
        self._bytes -= size
        if not self._items:
            self._not_empty.clear()
        if not self._full_for(INGRESS_DEFAULT_EVENT_SIZE):
            self._not_full.set()
        return item

    async def get(self) -> Any:
        while not self._items:
            self._not_empty.clear()
            await self._not_empty.wait()
        return self._pop()

    def get_nowait(self) -> Any:
        if not self._items:
            raise asyncio.QueueEmpty
        return self._pop()


class ProviderError(RuntimeError):
    pass


class ProviderBusy(ProviderError):
    pass


class ProviderProcessError(ProviderError):
    pass


class ProviderProtocolError(ProviderError):
    pass


@dataclass(frozen=True)
class StartRequest:
    prompt: str
    model: str
    effort: str | None
    worktree: str
    run_id: str
    agent_id: str


@dataclass(frozen=True)
class ProviderEvent:
    provider: ProviderKind
    payload: dict[str, Any]
    direction: str = "provider"
    generation: int = 1
    received_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class AdapterStatus:
    state: LifecycleState
    session_id: str | None
    pid: int | None
    generation: int = 1
    active_turn_id: str | None = None
    transcript_path: str | None = None
    detail: str | None = None
    core_phase: str | None = None


class ProviderAdapter(ABC):
    """Provider-neutral control surface owned by the durable supervisor."""

    provider: ProviderKind

    def set_process_created_callback(self, callback: Callable[[int], None]) -> None:
        """Install a callback for the first durable process identity boundary."""

        del callback

    @abstractmethod
    async def start(self, request: StartRequest) -> AdapterStatus:
        raise NotImplementedError

    @abstractmethod
    async def resume(self, session_id: str) -> AdapterStatus:
        raise NotImplementedError

    @abstractmethod
    async def send_now(self, message: str) -> AdapterStatus:
        raise NotImplementedError

    @abstractmethod
    async def send_on_idle(self, message: str) -> AdapterStatus:
        raise NotImplementedError

    @abstractmethod
    async def interrupt(self) -> AdapterStatus:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> AdapterStatus:
        raise NotImplementedError

    @abstractmethod
    async def replace(
        self,
        new_prompt: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> AdapterStatus:
        """End the current provider session and return the replacement status.

        Replacement-session events must not be exposed through `events()`
        until this await returns; the supervisor rekeys the stream to the new
        stable run id at that boundary.
        """

        raise NotImplementedError

    def prepare_replacement(self, record: RunRecord) -> None:
        """Rebind per-run provider configuration before a replacement starts.

        Fixture and third-party adapters without per-run configuration can keep
        the default no-op. Built-in adapters use this to rotate Wiki runtime and
        MCP identity before the replacement prompt can invoke tools.
        """

        del record

    @abstractmethod
    async def status(self) -> AdapterStatus:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self) -> AdapterStatus:
        """Return lock-free in-memory status for the event persistence path."""

        raise NotImplementedError

    @abstractmethod
    def events(self) -> AsyncIterator[ProviderEvent]:
        raise NotImplementedError

    async def drain_events(self) -> list[ProviderEvent]:
        """Return events buffered after the provider has stopped."""

        return []

    @abstractmethod
    async def archive(self) -> AdapterStatus:
        raise NotImplementedError

    async def respond(
        self, request_id: str | int, response: dict[str, Any]
    ) -> AdapterStatus:
        """Answer a provider approval/question request when the protocol supports it."""

        raise NotImplementedError(
            f"{self.provider.value} adapter does not support responses"
        )

    async def close(self) -> None:
        """Release local transport resources without changing durable run intent."""

        await self.stop()
