from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .types import LifecycleState, ProviderKind, utc_now


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


class ProviderAdapter(ABC):
    """Provider-neutral control surface owned by the durable supervisor."""

    provider: ProviderKind

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
    async def replace(self, new_prompt: str, model: str | None = None) -> AdapterStatus:
        """End the current provider session and return the replacement status.

        Replacement-session events must not be exposed through `events()`
        until this await returns; the supervisor rekeys the stream to the new
        stable run id at that boundary.
        """

        raise NotImplementedError

    @abstractmethod
    async def status(self) -> AdapterStatus:
        raise NotImplementedError

    @abstractmethod
    def events(self) -> AsyncIterator[ProviderEvent]:
        raise NotImplementedError

    @abstractmethod
    async def archive(self) -> AdapterStatus:
        raise NotImplementedError

    async def respond(self, request_id: str, response: dict[str, Any]) -> AdapterStatus:
        """Answer a provider approval/question request when the protocol supports it."""

        raise NotImplementedError(f"{self.provider.value} adapter does not support responses")
