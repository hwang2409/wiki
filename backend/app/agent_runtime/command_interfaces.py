"""Typed boundaries between command decisions, persistence, effects, and execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .command_log import AgentCommand, CommandIntent


class CommandDecider(Protocol):
    """Pure command-to-event decision boundary."""

    def __call__(
        self, command: AgentCommand, state: Mapping[str, Any]
    ) -> tuple[Any, ...]: ...


class CommandPersistence(Protocol):
    """SQLite receipt and intent boundary used by the queue reactor."""

    def pending(self) -> list[AgentCommand]: ...

    def append_intent(
        self, command: AgentCommand, state: Mapping[str, Any]
    ) -> CommandIntent: ...

    def begin_effect(self, command: AgentCommand) -> None: ...

    def complete(self, command: AgentCommand, result: Any, state: Mapping[str, Any]) -> Any: ...

    def fail(
        self, command: AgentCommand, exc: BaseException, state: Mapping[str, Any]
    ) -> None: ...

    def forget(self, method: str, request_id: str) -> None: ...


class EffectOutbox(Protocol):
    """Durable provider-effect checkpoint boundary."""

    def update_steer_effect(
        self,
        method: str,
        request_id: str,
        status: str,
        result: Any | None = None,
    ) -> None: ...

    def update_replace_effect(
        self,
        method: str,
        request_id: str,
        status: str,
        result: Any | None = None,
    ) -> None: ...


CommandExecutor = Callable[[], Awaitable[Any]]
