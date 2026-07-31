"""Typed command, event, receipt, and error models."""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4


class CommandError(RuntimeError):
    """Base class for command-log failures."""


class CommandConflict(CommandError):
    """The command is not valid for the current projection."""


class CommandReceiptError(CommandError):
    """A prior command attempt recorded an error receipt."""


def _canonical_command_hash(command: "AgentCommand") -> str:
    payload = dict(command.payload)
    identity_payload = payload.get("command_hash_payload")
    if isinstance(identity_payload, Mapping):
        payload = dict(identity_payload)
    value = json.dumps(
        {
            "method": command.method,
            "agent_id": command.agent_id.upper(),
            "payload": payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentCommand:
    """A typed mutation accepted by the supervisor command queue."""

    method: str
    agent_id: str
    request_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def command_hash(self) -> str:
        return _canonical_command_hash(self)

    @classmethod
    def spawn(
        cls,
        *,
        agent_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> "AgentCommand":
        return cls("run/start", agent_id, request_id, dict(payload))

    @classmethod
    def steer(
        cls,
        *,
        agent_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> "AgentCommand":
        method = str(payload.get("method") or "run/send_now")
        if method not in {"run/send_now", "run/send_on_idle"}:
            raise ValueError("steer method must be run/send_now or run/send_on_idle")
        return cls(method, agent_id, request_id, dict(payload))

    @classmethod
    def archive(
        cls,
        *,
        agent_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> "AgentCommand":
        return cls("run/archive", agent_id, request_id, dict(payload))

    @classmethod
    def replace(
        cls,
        *,
        agent_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> "AgentCommand":
        return cls("run/replace", agent_id, request_id, dict(payload))


@dataclass(frozen=True)
class EventSpec:
    event_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CommandIntent:
    command: AgentCommand
    events: tuple[EventSpec, ...]
    replay: bool = False
    pending: bool = False
    result: Any = None


@dataclass(frozen=True)
class CommandReceipt:
    method: str
    request_id: str
    result: Any = None
    error_type: str | None = None
    error_message: str | None = None
    error_module: str | None = None
    error_qualname: str | None = None
    agent_id: str | None = None
    command_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_type is None


def restore_receipt_error(receipt: CommandReceipt) -> BaseException:
    """Restore the stable domain error recorded by a prior command."""

    module_name = receipt.error_module
    qualname = receipt.error_qualname or receipt.error_type
    if module_name and qualname:
        try:
            value: Any = importlib.import_module(module_name)
            for part in qualname.split("."):
                value = getattr(value, part)
            if isinstance(value, type) and issubclass(value, BaseException):
                return value(receipt.error_message or "command failed")
        except (ImportError, AttributeError, TypeError, ValueError):
            pass
    return CommandReceiptError(receipt.error_message or "command failed")
