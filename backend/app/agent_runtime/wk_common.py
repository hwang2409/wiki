"""Provider-neutral state and tool plumbing for wk lanes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .wk_core import (
    WkDisposition,
    WkEventEnvelope,
    WkEventPhase,
    WkIntegrityError,
    WkLoop,
    WkMutationClass,
    WkToolRegistry,
    WkToolRequest,
    WkToolResult,
    mutation_input_hash,
    redact_payload,
)


class WkLedgerError(RuntimeError):
    """The harness cannot prove a complete tool operation."""


class WkSessionTree:
    """Append-only parent-linked session entries."""

    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []
        self.current_id: str | None = None

    def append(self, entry: Mapping[str, object]) -> str:
        entry_id = str(entry.get("id") or uuid4())
        value = dict(entry)
        value["id"] = entry_id
        value["parent_id"] = value.get("parent_id", self.current_id)
        self.entries.append(value)
        self.current_id = entry_id
        return entry_id

    def record_event(self, event: WkEventEnvelope) -> str:
        return self.append(
            {
                "kind": event.kind,
                "phase": event.phase.value,
                "source_event_id": event.source_event_id,
                "payload": dict(redact_payload(event.payload)),
            }
        )

    def restore(self, events: Sequence[WkEventEnvelope]) -> None:
        for event in sorted(events, key=lambda item: item.source_seq):
            self.append(
                {
                    "id": event.source_event_id,
                    "kind": event.kind,
                    "phase": event.phase.value,
                    "source_event_id": event.source_event_id,
                    "parent_id": event.parent_source_event_id,
                    "payload": dict(redact_payload(event.payload)),
                }
            )


def _hash_result(result: WkToolResult) -> str:
    value = {
        "success": result.success,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "error_class": result.error_class,
        "error_detail": result.error_detail,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "mutation": result.mutation.value,
        "mutation_receipt": result.mutation_receipt,
        "output_artifact": result.output_artifact,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class _LedgerOperation:
    request: WkToolRequest
    started: WkEventEnvelope
    result: WkToolResult | None = None


class WkToolLedger:
    """Record and reconcile typed calls for any wk provider."""

    def __init__(self, translator: Any):
        self.translator = translator
        self.operations: dict[str, _LedgerOperation] = {}
        self._transport_pending: set[str] = set()

    @property
    def events(self) -> list[WkEventEnvelope]:
        return self.translator.sequencer.events

    def record_started(self, request: WkToolRequest) -> WkEventEnvelope:
        if request.call_id in self.operations:
            raise WkLedgerError(f"duplicate tool call: {request.call_id}")
        event = self.translator.sequencer.emit(
            run_id=self.translator.run_id,
            agent_id=self.translator.agent_id,
            kind="tool.started",
            phase=WkEventPhase.TOOL,
            provider=self.translator.provider,
            lane=self.translator.lane,
            disposition=WkDisposition.RENDERED,
            ts=self.translator.timestamp(),
            payload={
                "call_id": request.call_id,
                "name": request.name,
                "arguments": dict(request.arguments),
                "mutation": request.mutation.value,
            },
            integrity={"input_hash": mutation_input_hash(request)},
        )
        self.operations[request.call_id] = _LedgerOperation(request, event)
        self.translator.session.record_event(event)
        return event

    def record_result(self, request: WkToolRequest, result: WkToolResult) -> WkEventEnvelope:
        operation = self.operations.get(request.call_id)
        if operation is None or operation.result is not None:
            raise WkLedgerError(f"tool result has no unique start: {request.call_id}")
        if mutation_input_hash(operation.request) != mutation_input_hash(request):
            raise WkLedgerError(f"tool request changed: {request.call_id}")
        event = self.translator.sequencer.emit(
            run_id=self.translator.run_id,
            agent_id=self.translator.agent_id,
            kind="tool.completed" if result.success else "tool.failed",
            phase=WkEventPhase.TOOL,
            provider=self.translator.provider,
            lane=self.translator.lane,
            disposition=WkDisposition.RENDERED,
            ts=self.translator.timestamp(),
            payload={"call_id": request.call_id, "result": result.to_dict()},
            integrity={"input_hash": mutation_input_hash(request), "result_hash": _hash_result(result)},
        )
        self.operations[request.call_id] = _LedgerOperation(request, operation.started, result)
        self.translator.session.record_event(event)
        return event

    def record_integrity_event(self, detail: str) -> WkEventEnvelope:
        event = self.translator.sequencer.emit(
            run_id=self.translator.run_id,
            agent_id=self.translator.agent_id,
            kind="wk.integrity_violation",
            phase=WkEventPhase.STATUS,
            provider=self.translator.provider,
            lane=self.translator.lane,
            disposition=WkDisposition.RENDERED,
            ts=self.translator.timestamp(),
            payload={"detail": detail},
        )
        self.translator.session.record_event(event)
        return event

    def restore(self, events: Sequence[WkEventEnvelope]) -> None:
        for event in sorted(events, key=lambda item: item.source_seq):
            call_id = event.payload.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if event.kind == "tool.started":
                arguments = event.payload.get("arguments")
                if not isinstance(arguments, Mapping):
                    raise WkLedgerError(f"durable tool request is not typed: {call_id}")
                self.operations[call_id] = _LedgerOperation(
                    WkToolRequest(
                        call_id=call_id,
                        name=str(event.payload.get("name") or ""),
                        arguments=dict(arguments),
                        mutation=WkMutationClass(
                            str(event.payload.get("mutation", WkMutationClass.NONE.value))
                        ),
                    ),
                    event,
                )
            elif event.kind in {"tool.completed", "tool.failed"}:
                operation = self.operations.get(call_id)
                result = event.payload.get("result")
                if operation is None or not isinstance(result, Mapping):
                    raise WkLedgerError(f"durable tool result has no start: {call_id}")
                self.operations[call_id] = _LedgerOperation(
                    operation.request,
                    operation.started,
                    WkToolResult.from_dict(result),
                )

    def restore_transport_frames(self, raws: Sequence[Mapping[str, Any]]) -> None:
        for raw in raws:
            self.record_transport_frame(raw)

    def record_transport_frame(self, raw: Mapping[str, Any]) -> None:
        message = raw.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_id = block.get("id")
                    if isinstance(tool_id, str) and tool_id:
                        if tool_id in self._transport_pending:
                            raise WkLedgerError(f"duplicate transport tool call: {tool_id}")
                        self._transport_pending.add(tool_id)
                elif block_type == "tool_result":
                    tool_id = block.get("tool_use_id")
                    if isinstance(tool_id, str) and tool_id:
                        if tool_id not in self._transport_pending:
                            raise WkLedgerError(f"transport result has no start: {tool_id}")
                        self._transport_pending.remove(tool_id)
        params = raw.get("params")
        item = params.get("item") if isinstance(params, Mapping) else None
        if not isinstance(item, Mapping):
            return
        if item.get("type") not in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}:
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return
        if raw.get("method") == "item/started":
            if item_id in self._transport_pending:
                raise WkLedgerError(f"duplicate transport tool call: {item_id}")
            self._transport_pending.add(item_id)
        elif raw.get("method") == "item/completed":
            if item_id not in self._transport_pending:
                raise WkLedgerError(f"transport result has no start: {item_id}")
            self._transport_pending.remove(item_id)

    def reconcile_transport(self) -> None:
        if self._transport_pending:
            raise WkLedgerError("missing transport tool results: " + ", ".join(sorted(self._transport_pending)))

    def reconcile(self, events: Sequence[WkEventEnvelope] | None = None) -> None:
        source = list(events if events is not None else self.events)
        started = {str(event.payload["call_id"]): event for event in source if event.kind == "tool.started" and event.payload.get("call_id")}
        completed = {str(event.payload["call_id"]): event for event in source if event.kind in {"tool.completed", "tool.failed"} and event.payload.get("call_id")}
        missing = sorted(set(started) - set(completed))
        if missing:
            raise WkLedgerError("missing tool results: " + ", ".join(missing))
        unknown = sorted(set(completed) - set(started))
        if unknown:
            raise WkLedgerError("tool results without starts: " + ", ".join(unknown))
        for call_id, event in started.items():
            result_event = completed[call_id]
            result = result_event.payload.get("result")
            if not isinstance(result, Mapping):
                raise WkLedgerError(f"tool result is not typed: {call_id}")
            exit_code = result.get("exit_code")
            if result.get("success") is not (exit_code == 0):
                raise WkLedgerError(f"tool success disagrees with exit code: {call_id}")
            mutation = str(event.payload.get("mutation", WkMutationClass.NONE.value))
            if mutation != WkMutationClass.NONE.value:
                if result.get("mutation") != mutation:
                    raise WkLedgerError(f"tool mutation class changed: {call_id}")
                receipt = result.get("mutation_receipt")
                if not isinstance(receipt, Mapping):
                    raise WkLedgerError(f"mutation result has no receipt: {call_id}")
            if event.payload.get("name") != "wk.gate":
                continue
            started_hash = event.integrity.get("input_hash") if event.integrity else None
            result_hash = result_event.integrity.get("input_hash") if result_event.integrity else None
            if started_hash != result_hash:
                raise WkLedgerError(f"tool input hash changed: {call_id}")
            receipt = result.get("mutation_receipt")
            if not isinstance(receipt, Mapping) or not isinstance(receipt.get("pid"), int) or receipt["pid"] <= 0:
                raise WkLedgerError(f"gate result has no process receipt: {call_id}")
            if any(not isinstance(receipt.get(field), str) or len(receipt[field]) != 64 for field in ("stdout_sha256", "stderr_sha256")):
                raise WkLedgerError(f"gate result has no process receipt: {call_id}")
            verdict = receipt.get("verdict")
            if not isinstance(verdict, Mapping) or type(verdict.get("ready")) is not bool:
                raise WkLedgerError(f"gate result has no boolean verdict: {call_id}")
            if verdict["ready"] and exit_code != 0:
                raise WkLedgerError(f"gate ready verdict disagrees with exit code: {call_id}")

    def check_merge_ready(
        self,
        *,
        ignore_call_id: str | None = None,
        role: str = "review",
        current_head_sha: str | None = None,
    ) -> tuple[bool, str]:
        """Return whether the ledger proves a safe merge-ready transition."""

        source = [
            event
            for event in self.events
            if event.payload.get("call_id") != ignore_call_id
        ]
        try:
            self.reconcile(source)
        except WkLedgerError as exc:
            return False, str(exc)
        open_mutations = [
            operation.request.call_id
            for operation in self.operations.values()
            if operation.result is None
            and operation.request.call_id != ignore_call_id
            and operation.request.mutation is not WkMutationClass.NONE
        ]
        if open_mutations:
            return False, "open mutation operations: " + ", ".join(sorted(open_mutations))
        started_by_call = {
            str(event.payload["call_id"]): event
            for event in source
            if event.kind == "tool.started" and event.payload.get("call_id")
        }
        completed = {
            str(event.payload["call_id"]): event
            for event in source
            if event.kind in {"tool.completed", "tool.failed"}
            and event.payload.get("call_id")
        }
        gate_events = [
            event
            for event in completed.values()
            if (
                started_by_call.get(str(event.payload.get("call_id"))) is not None
                and started_by_call[str(event.payload["call_id"])].payload.get("name") == "wk.gate"
            )
        ]
        if not gate_events:
            return False, "no gate receipt proves merge-ready"
        latest_gate = max(gate_events, key=lambda event: event.source_seq)
        result = latest_gate.payload.get("result")
        receipt = result.get("mutation_receipt") if isinstance(result, Mapping) else None
        verdict = receipt.get("verdict") if isinstance(receipt, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or result.get("success") is not True
            or result.get("exit_code") != 0
            or not isinstance(verdict, Mapping)
            or verdict.get("ready") is not True
        ):
            return False, "latest gate receipt is not a passing ready verdict"
        started = next(
            (
                candidate
                for candidate in source
                if candidate.kind == "tool.started"
                and candidate.payload.get("call_id") == latest_gate.payload.get("call_id")
            ),
            None,
        )
        arguments = started.payload.get("arguments") if started else None
        gate_role = arguments.get("role", "review") if isinstance(arguments, Mapping) else "review"
        if gate_role != role:
            return False, "latest gate receipt has the wrong role"
        head_sha = verdict.get("head_sha")
        if not isinstance(current_head_sha, str) or not current_head_sha:
            return False, "current git head is unavailable"
        if head_sha != current_head_sha:
            return False, "gate verdict does not match current git head"
        for event in source:
            if event.source_seq <= latest_gate.source_seq:
                continue
            call_id = event.payload.get("call_id")
            if event.kind not in {"tool.completed", "tool.failed"} or not isinstance(call_id, str):
                continue
            started = next(
                (
                    candidate
                    for candidate in source
                    if candidate.kind == "tool.started"
                    and candidate.payload.get("call_id") == call_id
                ),
                None,
            )
            if started is not None and started.payload.get("mutation") != WkMutationClass.NONE.value:
                return False, "a mutation completed after the latest gate"
        return True, "latest gate receipt proves merge-ready"


def replay_wk_events(path: Path | None) -> tuple[list[dict[str, Any]], list[WkEventEnvelope]]:
    if path is None or not path.is_file():
        return [], []
    raw_rows: list[dict[str, Any]] = []
    events: list[WkEventEnvelope] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            raw_rows.append(row)
            payload = row.get("payload")
            envelope = payload.get("_wk_event") if isinstance(payload, Mapping) else None
            if isinstance(envelope, Mapping):
                events.append(WkEventEnvelope.from_dict(envelope))
    return raw_rows, sorted(events, key=lambda event: event.source_seq)


@dataclass(frozen=True)
class _SteeringItem:
    message_id: str
    message: str
    mode: str


class WkSteeringQueue:
    """Durable steering messages shared by provider lanes."""

    def __init__(self, path: Path):
        self.path = path
        self._items: list[_SteeringItem] = []
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                raise WkLedgerError("wk steering queue is not a list")
            self._items = [_SteeringItem(str(item["id"]), str(item["message"]), str(item["mode"])) for item in value]

    @property
    def pending(self) -> tuple[_SteeringItem, ...]:
        return tuple(self._items)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump([{"id": item.message_id, "message": item.message, "mode": item.mode} for item in self._items], handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def enqueue(self, message: str, *, mode: str) -> _SteeringItem:
        item = _SteeringItem(str(uuid4()), message, mode)
        self._items.append(item)
        self._save()
        return item

    def acknowledge(self, message_id: str) -> None:
        self._items = [item for item in self._items if item.message_id != message_id]
        self._save()


class WkToolBridge:
    """Execute only the registry tools and record both ledger events."""

    def __init__(self, *, registry: WkToolRegistry, ledger: WkToolLedger, loop: WkLoop, allowed_names: Sequence[str] = ("wk.read", "wk.write", "wk.edit", "wk.bash", "wk.gate", "wk.status")):
        self.registry = registry
        self.ledger = ledger
        self.loop = loop
        self.allowed_names = frozenset(allowed_names)
        self.event_publisher: Callable[[Sequence[WkEventEnvelope]], Awaitable[None]] | None = None

    async def invoke(self, name: str, arguments: Mapping[str, object], *, call_id: str | None = None) -> WkToolResult:
        if name not in self.allowed_names:
            raise WkLedgerError(f"unsupported Wiki tool: {name!r}")
        mutation = WkMutationClass.PROCESS if name in {"wk.bash", "wk.gate"} else WkMutationClass.FILE if name in {"wk.write", "wk.edit"} else WkMutationClass.NONE
        request = WkToolRequest(call_id=call_id or str(uuid4()), name=name, arguments=dict(arguments), mutation=mutation)
        started_event = self.ledger.record_started(request)
        try:
            result = await self.registry.execute(request)
        except WkIntegrityError as exc:
            self.loop.write_status(
                state="blocked",
                pr=None,
                step="wk integrity guard blocked the requested status",
                blocker=str(exc),
            )
            result = WkToolResult(
                success=False,
                exit_code=None,
                error_class=type(exc).__name__,
                error_detail=str(exc),
                mutation=request.mutation,
            )
        except Exception as exc:
            result = WkToolResult(success=False, exit_code=None, error_class=type(exc).__name__, error_detail=str(exc), mutation=request.mutation)
        result_event = self.ledger.record_result(request, result)
        if self.event_publisher is not None:
            await self.event_publisher(
                (started_event, *self.loop.drain_integrity_events(), result_event)
            )
        try:
            self.ledger.reconcile()
        except WkLedgerError as exc:
            self.loop.write_status(
                state="blocked",
                pr=None,
                step="wk ledger reconciliation blocked the run",
                blocker=str(exc),
            )
            raise
        return result
