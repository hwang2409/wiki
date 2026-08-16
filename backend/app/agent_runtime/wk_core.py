"""Provider-neutral contracts for the opt-in Wiki worker harness.

This module deliberately has no provider SDK imports.  Provider lanes and the
supervisor will use these contracts in later WIKI-289 changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol, cast

from .wk_feature import WkKind, wk_lane_for_kind


WK_EVENT_SCHEMA = "wiki.wk.event.v0"
MAX_EVENT_TEXT = 8_192
MAX_EVENT_ITEMS = 128
REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"


class WkEventPhase(str, Enum):
    RUN = "run"
    TURN = "turn"
    ASSISTANT = "assistant"
    TOOL = "tool"
    STATUS = "status"
    COMPACTION = "compaction"
    ARCHIVE = "archive"


class WkDisposition(str, Enum):
    RENDERED = "rendered"
    SUMMARIZED = "summarized"
    INTENTIONALLY_IGNORED = "intentionally_ignored"
    UNKNOWN = "unknown"


class WkMutationClass(str, Enum):
    NONE = "none"
    FILE = "file"
    GIT = "git"
    PROCESS = "process"
    UNKNOWN = "unknown"


class WkIntegrityError(RuntimeError):
    """The harness cannot prove a requested integrity transition."""


@dataclass(frozen=True)
class WkRunMetadata:
    """Additive metadata for the two distinct public wk execution kinds."""

    kind: WkKind

    def __post_init__(self) -> None:
        if wk_lane_for_kind(self.kind) is None:
            raise ValueError(f"unsupported wk kind: {self.kind!r}")

    @property
    def lane(self) -> str:
        lane = wk_lane_for_kind(self.kind)
        if lane is None:
            raise ValueError(f"unsupported wk kind: {self.kind!r}")
        return lane

    @property
    def provider(self) -> str:
        return self.lane

    @classmethod
    def from_kind(cls, kind: str) -> WkRunMetadata:
        lane = wk_lane_for_kind(kind)
        if lane is None:
            raise ValueError(f"unsupported wk kind: {kind!r}")
        return cls(cast(WkKind, kind))


_SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|"
    r"password|private[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)$",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:bearer|basic)\s+)[^\s,;]+|\b(?:sk|rk)-[A-Za-z0-9_-]+"
)


def _redact_text(value: str, *, max_length: int) -> str:
    redacted = _SECRET_VALUE.sub(lambda match: f"{match.group(1) or ''}{REDACTED}", value)
    if len(redacted) <= max_length:
        return redacted
    return redacted[:max_length] + TRUNCATED


def redact_payload(value: object, *, max_length: int = MAX_EVENT_TEXT) -> object:
    """Redact secret-looking values and bound text before event publication."""

    if isinstance(value, str):
        return _redact_text(value, max_length=max_length)
    if isinstance(value, Mapping):
        items = list(value.items())[:MAX_EVENT_ITEMS]
        result: dict[str, object] = {}
        for key, child in items:
            name = str(key)
            result[name] = REDACTED if _SECRET_KEY.search(name) else redact_payload(
                child, max_length=max_length
            )
        if len(value) > MAX_EVENT_ITEMS:
            result[TRUNCATED] = f"{len(value) - MAX_EVENT_ITEMS} items omitted"
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = [redact_payload(item, max_length=max_length) for item in value[:MAX_EVENT_ITEMS]]
        if len(value) > MAX_EVENT_ITEMS:
            items.append(f"{len(value) - MAX_EVENT_ITEMS} items omitted")
        return items
    if isinstance(value, (bytes, bytearray)):
        return _redact_text(value.decode("utf-8", errors="replace"), max_length=max_length)
    return value


def _hash_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class WkEventEnvelope:
    schema: str
    source_event_id: str
    source_seq: int
    run_id: str
    agent_id: str
    kind: str
    phase: WkEventPhase
    provider: str
    lane: WkKind
    disposition: WkDisposition
    ts: str
    parent_source_event_id: str | None
    payload: Mapping[str, object]
    integrity: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", WkEventPhase(self.phase))
        object.__setattr__(self, "disposition", WkDisposition(self.disposition))
        if self.schema != WK_EVENT_SCHEMA:
            raise ValueError(f"unsupported wk event schema: {self.schema!r}")
        if self.source_seq < 0:
            raise ValueError("source_seq must be non-negative")
        if self.provider not in {"claude", "codex"}:
            raise ValueError("provider must be claude or codex")
        if wk_lane_for_kind(self.lane) != self.provider:
            raise ValueError("event lane does not match provider")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "source_event_id": self.source_event_id,
            "source_seq": self.source_seq,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "kind": self.kind,
            "phase": self.phase.value,
            "provider": self.provider,
            "lane": self.lane,
            "disposition": self.disposition.value,
            "ts": self.ts,
            "parent_source_event_id": self.parent_source_event_id,
            "payload": cast(dict[str, object], redact_payload(self.payload)),
        }
        if self.integrity is not None:
            value["integrity"] = cast(dict[str, object], redact_payload(self.integrity))
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WkEventEnvelope:
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise TypeError("wk event payload must be a mapping")
        integrity = value.get("integrity")
        if integrity is not None and not isinstance(integrity, Mapping):
            raise TypeError("wk event integrity must be a mapping")
        return cls(
            schema=str(value["schema"]),
            source_event_id=str(value["source_event_id"]),
            source_seq=int(value["source_seq"]),
            run_id=str(value["run_id"]),
            agent_id=str(value["agent_id"]),
            kind=str(value["kind"]),
            phase=WkEventPhase(str(value["phase"])),
            provider=str(value["provider"]),
            lane=cast(WkKind, str(value["lane"])),
            disposition=WkDisposition(str(value["disposition"])),
            ts=str(value["ts"]),
            parent_source_event_id=(
                str(value["parent_source_event_id"])
                if value.get("parent_source_event_id") is not None
                else None
            ),
            payload=cast(Mapping[str, object], payload),
            integrity=cast(Mapping[str, object], integrity) if integrity is not None else None,
        )


class WkEventSink(Protocol):
    def publish(self, event: WkEventEnvelope) -> WkEventEnvelope:
        """Persist one already-validated event."""


class WkEventSequencer:
    """Single-writer event sink that assigns deterministic per-run sequences."""

    def __init__(self, *, start_seq: int = 1, event_id_factory: Callable[[], str] | None = None):
        if start_seq < 1:
            raise ValueError("start_seq must be positive")
        self._next_seq = start_seq
        self._event_id_factory = event_id_factory or (lambda: str(uuid.uuid4()))
        self.events: list[WkEventEnvelope] = []

    @classmethod
    def from_events(
        cls,
        events: Sequence[WkEventEnvelope],
        *,
        event_id_factory: Callable[[], str] | None = None,
    ) -> WkEventSequencer:
        ordered = sorted(events, key=lambda event: event.source_seq)
        if ordered and [event.source_seq for event in ordered] != list(
            range(ordered[0].source_seq, ordered[-1].source_seq + 1)
        ):
            raise ValueError("wk event sequence has a gap")
        sequencer = cls(
            start_seq=(ordered[-1].source_seq + 1) if ordered else 1,
            event_id_factory=event_id_factory,
        )
        sequencer.events.extend(ordered)
        return sequencer

    def emit(
        self,
        *,
        run_id: str,
        agent_id: str,
        kind: str,
        phase: WkEventPhase,
        provider: str,
        lane: WkKind,
        disposition: WkDisposition,
        ts: str,
        payload: Mapping[str, object],
        parent_source_event_id: str | None = None,
        integrity: Mapping[str, object] | None = None,
    ) -> WkEventEnvelope:
        event = WkEventEnvelope(
            schema=WK_EVENT_SCHEMA,
            source_event_id=self._event_id_factory(),
            source_seq=self._next_seq,
            run_id=run_id,
            agent_id=agent_id,
            kind=kind,
            phase=phase,
            provider=provider,
            lane=lane,
            disposition=disposition,
            ts=ts,
            parent_source_event_id=parent_source_event_id,
            payload=payload,
            integrity=integrity,
        )
        self._next_seq += 1
        self.events.append(event)
        return event

    def publish(self, event: WkEventEnvelope) -> WkEventEnvelope:
        """Assign the next sequence and publish a draft with sequence zero."""

        if event.source_seq != 0:
            raise ValueError("event sink owns source_seq assignment")
        assigned = replace(event, source_seq=self._next_seq)
        self._next_seq += 1
        self.events.append(assigned)
        return assigned


@dataclass(frozen=True)
class WkToolRequest:
    call_id: str
    name: str
    arguments: Mapping[str, object]
    mutation: WkMutationClass = WkMutationClass.NONE


@dataclass(frozen=True)
class WkToolResult:
    """Typed tool output.  ``exit_code`` is the real process exit code."""

    success: bool
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    error_class: str | None = None
    error_detail: str | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    mutation: WkMutationClass = WkMutationClass.NONE
    mutation_receipt: Mapping[str, object] | None = None
    output_artifact: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout": _redact_text(self.stdout, max_length=MAX_EVENT_TEXT),
            "stderr": _redact_text(self.stderr, max_length=MAX_EVENT_TEXT),
            "error_class": self.error_class,
            "error_detail": (
                _redact_text(self.error_detail, max_length=MAX_EVENT_TEXT)
                if self.error_detail is not None
                else None
            ),
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "mutation": self.mutation.value,
            "mutation_receipt": (
                cast(dict[str, object], redact_payload(self.mutation_receipt))
                if self.mutation_receipt is not None
                else None
            ),
            "output_artifact": self.output_artifact,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WkToolResult:
        exit_code = value.get("exit_code")
        if exit_code is not None and not isinstance(exit_code, int):
            raise TypeError("exit_code must be an integer or null")
        return cls(
            success=bool(value["success"]),
            exit_code=exit_code,
            stdout=str(value.get("stdout") or ""),
            stderr=str(value.get("stderr") or ""),
            error_class=(str(value["error_class"]) if value.get("error_class") else None),
            error_detail=(str(value["error_detail"]) if value.get("error_detail") else None),
            timed_out=bool(value.get("timed_out", False)),
            duration_ms=(int(value["duration_ms"]) if value.get("duration_ms") is not None else None),
            mutation=WkMutationClass(str(value.get("mutation", WkMutationClass.NONE.value))),
            mutation_receipt=(
                cast(Mapping[str, object], value["mutation_receipt"])
                if isinstance(value.get("mutation_receipt"), Mapping)
                else None
            ),
            output_artifact=(str(value["output_artifact"]) if value.get("output_artifact") else None),
        )


ToolResult = WkToolResult


class WkTool(Protocol):
    name: str

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        """Validate and normalize model-supplied arguments."""

    def execute(self, request: WkToolRequest) -> Awaitable[WkToolResult]:
        """Execute a validated call under harness ownership."""


WK_TOOL_NAMES = frozenset(
    {"wk.read", "wk.write", "wk.edit", "wk.bash", "wk.gate", "wk.status", "wk.archive"}
)


class WkToolRegistry:
    """Validate and dispatch only Wiki-owned typed tools."""

    def __init__(self, tools: Sequence[WkTool] = ()):
        self._tools: dict[str, WkTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: WkTool) -> None:
        if tool.name not in WK_TOOL_NAMES:
            raise ValueError(f"unsupported wk tool: {tool.name!r}")
        if tool.name in self._tools:
            raise ValueError(f"wk tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    @property
    def names(self) -> frozenset[str]:
        """Return the registered Wiki tool names."""

        return frozenset(self._tools)

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        tool = self._tools.get(request.name)
        if tool is None:
            raise KeyError(f"wk tool is not registered: {request.name!r}")
        arguments = tool.validate(request.arguments)
        if not isinstance(arguments, Mapping):
            raise TypeError("wk tool validation must return a mapping")
        return await tool.execute(replace(request, arguments=arguments))


class WkProvider(Protocol):
    async def start(self, prompt: str) -> None: ...

    async def resume(self, session_id: str) -> None: ...

    async def send(self, message: str) -> None: ...

    async def interrupt(self) -> None: ...

    async def replace(self, prompt: str) -> None: ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[Mapping[str, object]]: ...


class WkSession(Protocol):
    def append(self, entry: Mapping[str, object]) -> None: ...


class WkRunControl(Protocol):
    async def send_now(self, message: str) -> None: ...

    async def send_on_idle(self, message: str) -> None: ...

    async def interrupt(self) -> None: ...

    async def archive(self) -> None: ...


WK_STATUS_STATES = frozenset({"working", "merge-ready", "blocked"})


class _LoopAuthority:
    __slots__ = ()


class WkStatusWriter(Protocol):
    def write(
        self,
        *,
        state: str,
        pr: str | None,
        step: str,
        blocker: str | None,
        authority: _LoopAuthority,
    ) -> int: ...


class _AtomicStatusWriter:
    def __init__(self, path: Path, authority: _LoopAuthority):
        self.path = path
        self._authority = authority
        self._write_seq = 0
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and isinstance(value.get("status_write_seq"), int):
            self._write_seq = max(0, value["status_write_seq"])

    def write(
        self,
        *,
        state: str,
        pr: str | None,
        step: str,
        blocker: str | None,
        authority: _LoopAuthority,
    ) -> int:
        if authority is not self._authority:
            raise PermissionError("status writes belong to the wk loop")
        if state not in WK_STATUS_STATES:
            raise ValueError(f"unsupported status state: {state!r}")
        if not step or "\n" in step or "\r" in step:
            raise ValueError("status step must be one non-empty line")
        if blocker is not None and ("\n" in blocker or "\r" in blocker):
            raise ValueError("status blocker must be one line")
        self._write_seq += 1
        value = {
            "state": state,
            "pr": pr,
            "step": step,
            "blocker": blocker,
            "status_write_seq": self._write_seq,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return self._write_seq


class WkLoop:
    """Small loop-owned boundary for status publication.

    Provider lanes receive no status writer.  They can only ask the loop to
    perform a transition through this method.
    """

    def __init__(self, *, status_path: Path):
        self._authority = _LoopAuthority()
        self._status_writer = _AtomicStatusWriter(status_path, self._authority)
        self._integrity_ledger: object | None = None
        self._integrity_role = "review"

    @property
    def status_path(self) -> Path:
        """Return the loop-owned status path for adjacent durable state."""

        return self._status_writer.path

    @property
    def status_write_seq(self) -> int:
        return self._status_writer._write_seq

    def bind_integrity(self, ledger: object, *, role: str = "review") -> None:
        """Bind the loop to the ledger that proves merge-ready transitions."""

        self._integrity_ledger = ledger
        self._integrity_role = role

    def write_status(
        self,
        *,
        state: str,
        pr: str | None,
        step: str,
        blocker: str | None,
        status_call_id: str | None = None,
    ) -> int:
        if state == "merge-ready":
            ledger = self._integrity_ledger
            if ledger is not None:
                checker = getattr(ledger, "check_merge_ready", None)
                if checker is None:
                    raise WkIntegrityError("wk ledger cannot prove merge-ready")
                allowed, reason = checker(
                    ignore_call_id=status_call_id,
                    role=self._integrity_role,
                )
                if not allowed:
                    raise WkIntegrityError(reason)
        return self._status_writer.write(
            state=state,
            pr=pr,
            step=step,
            blocker=blocker,
            authority=self._authority,
        )


def mutation_input_hash(request: WkToolRequest) -> str:
    """Return the stable hash recorded with a tool-started event."""

    return _hash_json(
        {
            "call_id": request.call_id,
            "name": request.name,
            "arguments": request.arguments,
            "mutation": request.mutation.value,
        }
    )


__all__ = [
    "EventSequencer",
    "ToolResult",
    "WK_EVENT_SCHEMA",
    "WkDisposition",
    "WkEventEnvelope",
    "WkEventPhase",
    "WkEventSequencer",
    "WkEventSink",
    "WkIntegrityError",
    "WkLoop",
    "WkMutationClass",
    "WkProvider",
    "WkRunControl",
    "WkRunMetadata",
    "WkSession",
    "WkStatusWriter",
    "WkTool",
    "WkToolRegistry",
    "WkToolRequest",
    "WkToolResult",
    "WK_TOOL_NAMES",
    "mutation_input_hash",
    "redact_payload",
]


EventSequencer = WkEventSequencer
