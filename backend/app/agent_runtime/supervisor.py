from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import math
import os
import re
import time
import warnings
from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .. import accounts, provider_health
from .command_log import AgentCommand, CommandConflict, CommandQueue, CommandRetryable
from .normalizer import NormalizedProviderEvent, normalize_provider_event
from .process import (
    ProviderProcessStatus,
    orphaned_provider_process,
    terminate_detached_provider_pid,
)
from .runtime_card import inject_runtime_card
from .provider import (
    AdapterStatus,
    ProviderAdapter,
    ProviderEvent,
    ProviderProcessError,
    StartRequest,
)
from .store import RunNotFound, RunStore, StoreConflict
from .types import (
    LifecycleState,
    EventDisposition,
    ProviderKind,
    RecoveryAction,
    RunRecord,
    TERMINAL_STATES,
    restart_recovery_decision,
)
from .version import RUNTIME_FINGERPRINT


AdapterFactory = Callable[[RunRecord], ProviderAdapter]
DEFAULT_REAPER_INTERVAL_SECONDS = 30.0
DEFAULT_REAPER_GRACE_SECONDS = 60.0
DEFAULT_ADAPTER_DETACH_GRACE_SECONDS = 1.0
DEFAULT_IDEMPOTENCY_CACHE_SIZE = 256
DEFAULT_WORKER_SOFT_CAP = 5
_IDEMPOTENT_METHODS = frozenset({"run/start", "run/send_now", "run/send_on_idle"})
_COMMAND_METHODS = frozenset(
    {"run/start", "run/send_now", "run/send_on_idle", "run/archive", "run/replace"}
)
DEFAULT_APPROVAL_RECOVERY_TIMEOUT_SECONDS = 30.0
logger = logging.getLogger(__name__)


class _HandoverDrainOutcome(str, Enum):
    SUPERVISOR_INTERRUPTED = "supervisor-interrupted"
    NATURAL_COMPLETED = "natural-completed"
    NEW_APPROVAL = "new-approval"
    CONTROL_CANCEL_REQUEST = "control-cancel-request"
    USER_RESPONSE = "user-response"
    NONE = "none"


class _HandoverPendingDisposition(str, Enum):
    PRESERVE_CAPTURED = "preserve-captured"
    MERGE_CAPTURED_AND_DRAINED = "merge-captured-and-drained"
    CLEAR = "clear"


# This is the complete handover finalization matrix. Keep all 18 cells
# explicit: provider stop events can be late, reordered, or provider-specific.
_HANDOVER_FINALIZATION_MATRIX: dict[
    tuple[LifecycleState, _HandoverDrainOutcome],
    tuple[LifecycleState, _HandoverPendingDisposition],
] = {
    (LifecycleState.WORKING, _HandoverDrainOutcome.SUPERVISOR_INTERRUPTED): (
        LifecycleState.WORKING,
        _HandoverPendingDisposition.PRESERVE_CAPTURED,
    ),
    (LifecycleState.WORKING, _HandoverDrainOutcome.NATURAL_COMPLETED): (
        LifecycleState.IDLE,
        _HandoverPendingDisposition.CLEAR,
    ),
    (LifecycleState.WORKING, _HandoverDrainOutcome.NEW_APPROVAL): (
        LifecycleState.WAITING_APPROVAL,
        _HandoverPendingDisposition.MERGE_CAPTURED_AND_DRAINED,
    ),
    (LifecycleState.WORKING, _HandoverDrainOutcome.CONTROL_CANCEL_REQUEST): (
        LifecycleState.WORKING,
        _HandoverPendingDisposition.PRESERVE_CAPTURED,
    ),
    (LifecycleState.WORKING, _HandoverDrainOutcome.USER_RESPONSE): (
        LifecycleState.WORKING,
        _HandoverPendingDisposition.CLEAR,
    ),
    (LifecycleState.WORKING, _HandoverDrainOutcome.NONE): (
        LifecycleState.WORKING,
        _HandoverPendingDisposition.PRESERVE_CAPTURED,
    ),
    (LifecycleState.WAITING_APPROVAL, _HandoverDrainOutcome.SUPERVISOR_INTERRUPTED): (
        LifecycleState.WAITING_APPROVAL,
        _HandoverPendingDisposition.PRESERVE_CAPTURED,
    ),
    (LifecycleState.WAITING_APPROVAL, _HandoverDrainOutcome.NATURAL_COMPLETED): (
        LifecycleState.IDLE,
        _HandoverPendingDisposition.CLEAR,
    ),
    (LifecycleState.WAITING_APPROVAL, _HandoverDrainOutcome.NEW_APPROVAL): (
        LifecycleState.WAITING_APPROVAL,
        _HandoverPendingDisposition.MERGE_CAPTURED_AND_DRAINED,
    ),
    (LifecycleState.WAITING_APPROVAL, _HandoverDrainOutcome.CONTROL_CANCEL_REQUEST): (
        LifecycleState.WAITING_APPROVAL,
        _HandoverPendingDisposition.PRESERVE_CAPTURED,
    ),
    (LifecycleState.WAITING_APPROVAL, _HandoverDrainOutcome.USER_RESPONSE): (
        LifecycleState.WORKING,
        _HandoverPendingDisposition.CLEAR,
    ),
    (LifecycleState.WAITING_APPROVAL, _HandoverDrainOutcome.NONE): (
        LifecycleState.WAITING_APPROVAL,
        _HandoverPendingDisposition.PRESERVE_CAPTURED,
    ),
    (LifecycleState.IDLE, _HandoverDrainOutcome.SUPERVISOR_INTERRUPTED): (
        LifecycleState.IDLE,
        _HandoverPendingDisposition.PRESERVE_CAPTURED,
    ),
    (LifecycleState.IDLE, _HandoverDrainOutcome.NATURAL_COMPLETED): (
        LifecycleState.IDLE,
        _HandoverPendingDisposition.CLEAR,
    ),
    (LifecycleState.IDLE, _HandoverDrainOutcome.NEW_APPROVAL): (
        LifecycleState.WAITING_APPROVAL,
        _HandoverPendingDisposition.MERGE_CAPTURED_AND_DRAINED,
    ),
    (LifecycleState.IDLE, _HandoverDrainOutcome.CONTROL_CANCEL_REQUEST): (
        LifecycleState.IDLE,
        _HandoverPendingDisposition.CLEAR,
    ),
    (LifecycleState.IDLE, _HandoverDrainOutcome.USER_RESPONSE): (
        LifecycleState.IDLE,
        _HandoverPendingDisposition.CLEAR,
    ),
    (LifecycleState.IDLE, _HandoverDrainOutcome.NONE): (
        LifecycleState.IDLE,
        _HandoverPendingDisposition.PRESERVE_CAPTURED,
    ),
}


def _approval_recovery_prompt(record: RunRecord) -> str | None:
    if not record.pending_requests:
        return None
    details: list[str] = []
    for pending in record.pending_requests.values():
        payload = pending.get("payload") if isinstance(pending, dict) else None
        if isinstance(payload, dict):
            details.append(json.dumps(payload, sort_keys=True))
    request_details = "\n".join(details) or "the prior approval request"
    return (
        "The provider transport restarted while an approval was pending. "
        "Recreate the exact approval question now and wait for the user's answer. "
        "Do not continue without that answer. Prior request details:\n"
        f"{request_details}"
    )


def _validated_pending_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("pending_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("pending_id must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError("pending_id must be a canonical UUID")
    return value


def _validated_dedupe_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("dedupe_key must be a non-empty string up to 200 characters")
    return value


_SOURCE_MAX_LENGTH = 64
_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _validated_source(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _SOURCE_MAX_LENGTH:
        raise ValueError(
            f"source must be a non-empty string up to {_SOURCE_MAX_LENGTH} characters"
        )
    if not _SOURCE_PATTERN.match(value):
        raise ValueError(
            "source may only contain letters, digits, and _.:- characters"
        )
    return value


def _validated_idempotency_request_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("request_id must be a non-empty string up to 200 characters")
    return value


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        warnings.warn(
            f"{name} must be a positive integer; using default {default}",
            RuntimeWarning,
            stacklevel=2,
        )
        return default
    if value < 1:
        warnings.warn(
            f"{name} must be a positive integer; using default {default}",
            RuntimeWarning,
            stacklevel=2,
        )
        return default
    return value


def _provider_user_text(
    provider: ProviderKind,
    kind: str,
    payload: dict[str, Any],
) -> str | None:
    if provider is ProviderKind.CLAUDE and kind == "claude_user":
        content = (payload.get("message") or {}).get("content")
    elif provider is ProviderKind.CODEX and kind in {"item_started", "item_completed"}:
        item = (payload.get("params") or {}).get("item") or {}
        if item.get("type") != "userMessage":
            return None
        content = item.get("content")
    else:
        return None
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    text = "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    return text or None


def _validated_seconds(name: str, value: float) -> float:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")
    return value


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    return _validated_seconds(name, value)


def provider_pid_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def resolve_safe_worktree(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("worktree must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"worktree does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ValueError("worktree must be a directory")
    if resolved == Path.home().resolve():
        raise ValueError("worktree resolves to $HOME; refusing ambiguous agent cwd")
    return resolved


def _public_run(record: RunRecord) -> dict[str, Any]:
    value = record.to_dict()
    value.pop("initial_prompt", None)
    return value


class Supervisor:
    def __init__(
        self,
        store: RunStore,
        adapter_factory: AdapterFactory,
        *,
        pid_alive: Callable[[int | None], bool] = provider_pid_is_alive,
        recovery_stability_seconds: float = 30.0,
        approval_recovery_timeout_seconds: float | None = None,
        reaper_interval_seconds: float | None = None,
        reaper_grace_seconds: float | None = None,
        adapter_detach_grace_seconds: float = DEFAULT_ADAPTER_DETACH_GRACE_SECONDS,
        orphan_archive_grace_seconds: float = 0.5,
        idempotency_cache_size: int = DEFAULT_IDEMPOTENCY_CACHE_SIZE,
        worker_soft_cap: int | None = None,
    ):
        if idempotency_cache_size < 1:
            raise ValueError("idempotency_cache_size must be positive")
        self.store = store
        self.adapter_factory = adapter_factory
        self.pid_alive = pid_alive
        self.recovery_stability_seconds = recovery_stability_seconds
        self.approval_recovery_timeout_seconds = _validated_seconds(
            "approval_recovery_timeout_seconds",
            (
                approval_recovery_timeout_seconds
                if approval_recovery_timeout_seconds is not None
                else _env_seconds(
                    "WIKI_APPROVAL_RECOVERY_TIMEOUT_SECONDS",
                    DEFAULT_APPROVAL_RECOVERY_TIMEOUT_SECONDS,
                )
            ),
        )
        self.reaper_interval_seconds = _validated_seconds(
            "WIKI_REAPER_INTERVAL_SECONDS",
            (
                reaper_interval_seconds
                if reaper_interval_seconds is not None
                else _env_seconds(
                    "WIKI_REAPER_INTERVAL_SECONDS",
                    DEFAULT_REAPER_INTERVAL_SECONDS,
                )
            ),
        )
        self.reaper_grace_seconds = _validated_seconds(
            "WIKI_REAPER_GRACE_SECONDS",
            (
                reaper_grace_seconds
                if reaper_grace_seconds is not None
                else _env_seconds(
                    "WIKI_REAPER_GRACE_SECONDS",
                    DEFAULT_REAPER_GRACE_SECONDS,
                )
            ),
        )
        self.adapter_detach_grace_seconds = _validated_seconds(
            "adapter_detach_grace_seconds", adapter_detach_grace_seconds
        )
        self.last_reaper_at = 0.0
        self.detached_at_monotonic: dict[str, float] = {}
        self.orphan_archive_grace_seconds = orphan_archive_grace_seconds
        self.adapters: dict[str, ProviderAdapter] = {}
        self.event_tasks: dict[str, asyncio.Task[None]] = {}
        self.event_processing_locks: dict[str, asyncio.Lock] = {}
        self.event_inflight_counts: dict[str, int] = {}
        self.event_drain_condition = asyncio.Condition()
        self.event_routes: dict[tuple[int, int], str] = {}
        self.queue_locks: dict[str, asyncio.Lock] = {}
        self.agent_locks: dict[str, asyncio.Lock] = {}
        self.handover_condition = asyncio.Condition()
        self.active_run_mutations = 0
        self.handover_pending = False
        self.handover_active = False
        self.handover_result: dict[str, Any] | None = None
        self.handover_event_queue: dict[
            str, list[tuple[ProviderAdapter, ProviderEvent]]
        ] = {}
        self.codex_fleet_lock = asyncio.Lock()
        self.codex_rotation_task: asyncio.Task[dict[str, Any]] | None = None
        self.codex_rotation_operation_id: str | None = None
        self.recovery_scan_lock = asyncio.Lock()
        # A supervisor boot invalidates any provider stdin write that had not
        # completed before shutdown: even if the row is at "sending", the
        # previous transport is gone. Sweep once per boot so the on-idle
        # queue drain does not wedge on a stale head (WIKI-232).
        self._sending_effects_reconciled = False
        self.pipeline_failures: dict[str, str] = {}
        self.expected_stream_ends: set[int] = set()
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.monitor_tasks: set[asyncio.Task[Any]] = set()
        self.auth_dead_attempts: dict[str, list[float]] = {}
        self.auth_dead_alert_at: dict[str, float] = {}
        self.auth_dead_recoveries: dict[str, asyncio.Task[None]] = {}
        self.codex_turn_fingerprints: dict[tuple[str, str], str | None] = {}
        # Claude usage-limit alerts are throttled per RUN, not per ticket.
        # Keying by ticket would suppress a fresh limit hit on a
        # replacement run under the same ticket (the round-7 lifecycle
        # cleared the old notice by run_id, so B's hit must not be
        # silenced by A's alert timestamp). Pruned in _terminal_cleanup.
        self.last_limit_alert_at: dict[str, float] = {}
        self.last_no_eligible_alert: float = 0.0
        self.idempotency_cache_size = idempotency_cache_size
        self.idempotency_results: OrderedDict[
            tuple[str, str], tuple[str, Any]
        ] = OrderedDict()
        self.idempotency_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        # WIKI-219 owns durable implicit-id receipts across supervisor restarts.
        self.implicit_idempotency_keys: set[tuple[str, str]] = set()
        self.implicit_idempotency_runs: dict[tuple[str, str], str] = {}
        self.idempotency_lock = asyncio.Lock()
        self.command_queue = CommandQueue(
            self.store.command_log,
            self.store.command_state,
            self.store.command_state_for,
            recovery_factory=self._recovery_executor,
            failure_state_provider=self.store.authoritative_command_state_for,
        )
        self.worker_soft_cap = (
            worker_soft_cap
            if worker_soft_cap is not None
            else _env_positive_int("WIKI_WORKER_SOFT_CAP", DEFAULT_WORKER_SOFT_CAP)
        )
        if self.worker_soft_cap < 1:
            raise ValueError("worker_soft_cap must be positive")

    def _runtime_status(self, record: RunRecord) -> dict[str, Any]:
        value = _public_run(record)
        adapter = self.adapters.get(record.run_id)
        snapshot = adapter.snapshot() if adapter is not None else None
        value["control_attached"] = adapter is not None
        value["provider_alive"] = bool(
            snapshot is not None
            and snapshot.state not in {LifecycleState.DEAD, LifecycleState.COMPLETED}
        )
        if snapshot is not None:
            value["state"] = snapshot.state.value
            value["provider_pid"] = snapshot.pid
            value["active_turn_id"] = snapshot.active_turn_id
        return value

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    async def _publish(self, event: dict[str, Any]) -> None:
        for queue in tuple(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow UI can recover from the durable event files. Dropping
                # a transient invalidation is safer than blocking providers.
                continue

    async def _publish_agent_change(self, agent_id: str) -> None:
        await self._publish(
            {"type": "agents", "tickets": [agent_id], "surface": "agents"}
        )

    def _spawn_monitor_task(
        self,
        coro: Any,
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self.monitor_tasks.add(task)

        def observe(completed: asyncio.Task[Any]) -> None:
            self.monitor_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception:
                logger.exception("background monitor task failed: %s", name)

        task.add_done_callback(observe)
        return task

    @staticmethod
    def _seconds_since(ts: float) -> float:
        return time.monotonic() - ts if ts else float("inf")

    def _current_codex_tickets(self) -> list[str]:
        return sorted(self._current_codex_run_ids())

    def _current_codex_run_ids(self) -> dict[str, str]:
        return {
            record.agent_id: record.run_id
            for record in self.store.list_runs()
            if record.provider is ProviderKind.CODEX
            and self.store.is_current(record)
            and not record.replaced_by_run_id
            and record.state not in TERMINAL_STATES
        }

    def _active_worker_count(self) -> int:
        return sum(
            1
            for record in self.store.list_runs()
            if record.role != "orchestrator"
            and record.state not in TERMINAL_STATES
            and not record.replaced_by_run_id
            and self.store.is_current(record)
        )

    def _agent_lock(self, agent_id: str) -> asyncio.Lock:
        return self.agent_locks.setdefault(agent_id, asyncio.Lock())

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        return self._agent_lock(self.store.get(run_id).agent_id)

    @asynccontextmanager
    async def _run_mutation_admission(self, *, wait_for_handover: bool = False):
        """Coordinate run mutations with the native handover barrier."""

        while True:
            async with self.handover_condition:
                while self.handover_pending:
                    await self.handover_condition.wait()
                if self.handover_active:
                    if not wait_for_handover:
                        raise StoreConflict("supervisor handover is in progress")
                    await self.handover_condition.wait()
                    continue
                self.active_run_mutations += 1
                break
        try:
            yield
        finally:
            async with self.handover_condition:
                self.active_run_mutations -= 1
                self.handover_condition.notify_all()

    @asynccontextmanager
    async def _event_mutation_admission(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        event: ProviderEvent,
    ):
        """Defer provider events while a handover owns the run set."""

        async with self.handover_condition:
            while self.handover_pending:
                await self.handover_condition.wait()
            if self.handover_active:
                self.handover_event_queue.setdefault(run_id, []).append(
                    (adapter, event)
                )
                yield False
                return
            self.active_run_mutations += 1
        try:
            yield True
        finally:
            async with self.handover_condition:
                self.active_run_mutations -= 1
                self.handover_condition.notify_all()

    async def _orphaned_provider_process(
        self, pid: int | None
    ) -> ProviderProcessStatus | None:
        return await orphaned_provider_process(pid)

    async def _terminate_orphan_provider_pid(
        self, process: ProviderProcessStatus | None
    ) -> bool:
        return await terminate_detached_provider_pid(
            process,
            grace=self.orphan_archive_grace_seconds,
        )

    @staticmethod
    def _detached_terminal_status(
        record: RunRecord,
        target: LifecycleState,
        *,
        detail: str | None,
    ) -> AdapterStatus:
        return AdapterStatus(
            state=target,
            session_id=record.provider_session_id,
            pid=None,
            generation=record.provider_generation,
            active_turn_id=None,
            transcript_path=record.transcript_path,
            detail=detail,
        )

    def _model_change_prompt(
        self,
        record: RunRecord,
        *,
        old_model: str,
        new_model: str,
    ) -> str:
        status_path = self.store.status_path(record.agent_id)
        return f"""You are the same {record.role} for {record.agent_id}.
Your model changed from {old_model} to {new_model} at a natural idle boundary.

Recover context from:
- prior transcript: {record.transcript_path or "not resolved"}
- raw provider events: {self.store.raw_events_path(record.run_id)}
- status file: {status_path}

Preserve the same identity, role, worktree, orchestrator grouping, PR gates, and status-file contract. Re-read the current ticket/PR state, update the status file before long operations, then continue from the last durable step.
"""

    def _append_model_changed_event(
        self,
        record: RunRecord,
        *,
        old_model: str,
        new_model: str,
        trigger: str,
    ) -> None:
        payload = {
            "type": "model_changed",
            "from_model": old_model,
            "to_model": new_model,
            "trigger": trigger,
            "message": f"model changed to {new_model}",
        }
        raw = self.store.append_raw(
            record.run_id,
            provider="supervisor",
            direction="supervisor",
            payload=payload,
            generation=record.provider_generation,
        )
        self.store.append_normalized(
            record.run_id,
            raw_seq=int(raw["seq"]),
            disposition=EventDisposition.RENDERED,
            kind="model_changed",
            payload=payload,
            lifecycle_state=record.state,
        )

    def _codex_rotation_active(self) -> bool:
        task = self.codex_rotation_task
        if task is not None and not task.done():
            return True
        journal = self.store.read_codex_rotation_journal()
        return bool(journal and journal.get("phase") != "complete")

    def _assert_codex_fleet_available(self) -> None:
        if self._codex_rotation_active():
            raise StoreConflict("Codex fleet is quiesced for account rotation")

    def _automatic_resume_is_stable(self, record: RunRecord) -> bool:
        if not record.automatic_resume_guarded_at:
            return False
        try:
            guarded_at = datetime.fromisoformat(record.automatic_resume_guarded_at)
        except ValueError:
            return False
        if guarded_at.tzinfo is None:
            guarded_at = guarded_at.replace(tzinfo=timezone.utc)
        return (
            datetime.now(timezone.utc) - guarded_at
        ).total_seconds() >= self.recovery_stability_seconds

    def _reaper_due(self) -> bool:
        return self._seconds_since(self.last_reaper_at) >= self.reaper_interval_seconds

    def _mark_adapter_loss(self, run_id: str) -> None:
        self.detached_at_monotonic.setdefault(run_id, time.monotonic())

    def _clear_adapter_loss(self, run_id: str) -> None:
        self.detached_at_monotonic.pop(run_id, None)

    def _adapter_recently_detached(self, run_id: str) -> bool:
        detached_at = self.detached_at_monotonic.get(run_id)
        return (
            detached_at is not None
            and self._seconds_since(detached_at) < self.adapter_detach_grace_seconds
        )

    def _reapable_adapter_loss(self, record: RunRecord) -> bool:
        if record.state in TERMINAL_STATES:
            self._clear_adapter_loss(record.run_id)
            return False
        if record.run_id in self.adapters:
            self._clear_adapter_loss(record.run_id)
            return False
        if record.provider_pid is not None:
            self._clear_adapter_loss(record.run_id)
            return False
        if record.quiesce_operation_id is not None:
            self._clear_adapter_loss(record.run_id)
            return False
        detached_at = self.detached_at_monotonic.get(record.run_id)
        if detached_at is None:
            # Monotonic timestamps do not survive process restart. If the
            # daemon boots with a persisted adapterless no-PID run, start a
            # fresh grace window rather than reaping immediately from wall
            # clock metadata unrelated to detach time.
            self._mark_adapter_loss(record.run_id)
            return False
        return self._seconds_since(detached_at) >= self.reaper_grace_seconds

    async def _pump_events(self, run_id: str, adapter: ProviderAdapter) -> None:
        stream_key = id(adapter)
        try:
            async for event in adapter.events():
                # Mark the local slot before the first await. The detach
                # barrier cannot observe an event between queue removal and
                # this increment.
                self.event_inflight_counts[run_id] = (
                    self.event_inflight_counts.get(run_id, 0) + 1
                )
                try:
                    async with self.event_drain_condition:
                        self.event_drain_condition.notify_all()
                    event_run_id = self.event_routes.get(
                        (id(adapter), event.generation),
                        run_id,
                    )
                    event_lock = self.event_processing_locks.setdefault(
                        event_run_id, asyncio.Lock()
                    )
                    async with event_lock:
                        await self._handle_provider_event(event_run_id, adapter, event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._record_pipeline_failure(
                        run_id,
                        adapter,
                        f"provider event persistence failed: {exc}",
                    )
                    return
                finally:
                    remaining = self.event_inflight_counts.get(run_id, 1) - 1
                    if remaining:
                        self.event_inflight_counts[run_id] = remaining
                    else:
                        self.event_inflight_counts.pop(run_id, None)
                    async with self.event_drain_condition:
                        self.event_drain_condition.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if stream_key not in self.expected_stream_ends:
                await self._record_stream_loss(
                    run_id, adapter, f"provider event stream failed: {exc}"
                )
        else:
            if stream_key not in self.expected_stream_ends:
                await self._record_stream_loss(
                    run_id, adapter, "provider event stream ended"
                )
        finally:
            current = asyncio.current_task()
            if self.event_tasks.get(run_id) is current:
                self.event_tasks.pop(run_id, None)

    async def _record_pipeline_failure(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        reason: str,
    ) -> None:
        """Stop an unobservable provider and suppress automatic restart."""

        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                self.pipeline_failures[run_id] = reason
                try:
                    await adapter.close()
                except Exception:
                    pass
                try:
                    record = self.store.transition(
                        run_id, LifecycleState.BLOCKED, reason=reason
                    )
                except Exception:
                    record = None
                self._remove_adapter_mapping(run_id, adapter)
                if record is not None and record.provider_pid is None:
                    self._mark_adapter_loss(run_id)
                if record is not None:
                    await self._publish_agent_change(record.agent_id)

    async def _record_stream_loss(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        reason: str,
    ) -> None:
        """Preserve the last lifecycle state so dead-PID recovery remains eligible."""

        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                try:
                    await adapter.close()
                except Exception:
                    pass
                try:
                    record = self.store.get(run_id)
                    if record.state in {LifecycleState.DEAD, LifecycleState.COMPLETED}:
                        record = None
                    else:
                        record = self.store.transition(run_id, record.state, reason=reason)
                except (RunNotFound, ValueError):
                    record = None
                self._remove_adapter_mapping(run_id, adapter)
                if record is not None:
                    self._mark_adapter_loss(run_id)
                if record is not None:
                    await self._publish_agent_change(record.agent_id)

    async def _handle_provider_event(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        event: ProviderEvent,
    ) -> None:
        async with self._event_mutation_admission(run_id, adapter, event) as admitted:
            if not admitted:
                return
            await self._handle_provider_event_without_admission(
                run_id,
                adapter,
                event,
            )

    async def _handle_provider_event_without_admission(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        event: ProviderEvent,
        *,
        update_adapter_snapshot: bool = True,
        schedule_monitor_actions: bool = True,
    ) -> None:
        prior = self.store.get(run_id)
        raw = self.store.append_raw(
            run_id,
            provider=event.provider.value,
            direction=event.direction,
            payload=event.payload,
            generation=event.generation,
            received_at=event.received_at,
        )
        try:
            normalized = normalize_provider_event(
                event.provider,
                event.payload,
                direction=event.direction,
            )
        except Exception as exc:
            normalized = NormalizedProviderEvent(
                EventDisposition.UNKNOWN,
                "normalization_error",
                {"error": str(exc), "raw_payload": event.payload},
            )
        pending_message = None
        echoed_text = _provider_user_text(
            event.provider,
            normalized.kind,
            normalized.payload,
        )
        normalized_payload = normalized.payload
        if echoed_text is not None:
            pending_message = self.store.match_pending_user_message(
                run_id,
                echoed_text,
            )
            if pending_message is not None:
                self.store.command_log.acknowledge_steer_for_pending(
                    run_id, pending_message["pending_id"]
                )
                normalized_payload = {
                    **normalized.payload,
                    "pending_id": pending_message["pending_id"],
                    "composer_text": pending_message["text"],
                    "composer_sent_at": pending_message["sent_at"],
                }
                pending_source = pending_message.get("source")
                if isinstance(pending_source, str) and pending_source:
                    normalized_payload["source"] = pending_source
        self.store.append_normalized(
            run_id,
            raw_seq=int(raw["seq"]),
            disposition=normalized.disposition,
            kind=normalized.kind,
            payload=normalized_payload,
            lifecycle_state=normalized.lifecycle_state,
        )

        record = self.store.get(run_id)
        if normalized.lifecycle_state is not None and update_adapter_snapshot:
            try:
                adapter_status = adapter.snapshot()
                record = self.store.update_adapter_status(run_id, adapter_status)
            except Exception:
                # The durable normalized lifecycle event is still authoritative
                # if a late provider snapshot conflicts with a terminal state.
                record = self.store.get(run_id)
            await self._publish_agent_change(record.agent_id)

        # Preserve the existing external SSE invalidation contract. The backend
        # will proxy these dictionaries unchanged when it switches transports.
        params = event.payload.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        method = event.payload.get("method")
        if (
            event.provider is ProviderKind.CODEX
            and event.direction != "client"
            and method == "turn/started"
            and isinstance(turn_id, str)
        ):
            self.codex_turn_fingerprints[(run_id, turn_id)] = (
                provider_health.credential_fingerprint("cdx")
            )

        await self._publish(
            {"type": "session", "ticket": record.agent_id, "surface": "session"}
        )
        turn_status = turn.get("status") if isinstance(turn, dict) else None
        credential_fingerprint = (
            self.codex_turn_fingerprints.pop((run_id, turn_id), None)
            if (
                event.provider is ProviderKind.CODEX
                and event.direction != "client"
                and method == "turn/completed"
                and isinstance(turn_id, str)
            )
            else None
        )
        if (
            event.provider is ProviderKind.CODEX
            and event.direction != "client"
            and method == "turn/completed"
            and turn_status == "completed"
        ):
            verified_event: dict[str, Any] = {
                "type": "codex_auth_verified",
                "provider": "codex",
                "credential_source": "current",
                "success": True,
                # Per-ticket proof: only THIS worker made a Codex turn, so the
                # notice store must clear only this ticket from the exhausted /
                # revive-failed rollups. A ticketless event proves nothing
                # about any other worker still awaiting recovery.
                "ticket": record.agent_id,
                "run_id": run_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            if credential_fingerprint is not None:
                verified_event["credential_fingerprint"] = credential_fingerprint
            await self._publish(verified_event)
        claude_turn_succeeded = (
            event.provider is ProviderKind.CLAUDE
            and event.direction != "stdin"
            and accounts.claude_turn_succeeded(event.payload)
        )
        if schedule_monitor_actions and not claude_turn_succeeded:
            self._schedule_monitor_actions(
                run_id,
                adapter,
                event,
                prior_state=prior.state,
                record=record,
            )
        if claude_turn_succeeded:
            # A successful Claude result is a provider response from this
            # run. Pane redraws and generic session events are not proof.
            # Emit a run-scoped clear even after supervisor restart; the
            # durable notice store owns whether this run has a notice.
            self.last_limit_alert_at.pop(run_id, None)
            await self._publish(
                {
                    "type": "claude_limit_cleared",
                    "provider": "claude",
                    "ticket": record.agent_id,
                    "run_id": run_id,
                    "window": "",
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )

        if schedule_monitor_actions and (
            record.state is LifecycleState.IDLE
            and id(adapter) not in self.expected_stream_ends
        ):
            self._spawn_monitor_task(
                self._deliver_next_queued(run_id, adapter),
                name=f"agent-idle-boundary-{run_id}",
            )

    async def _flush_handover_events(
        self, run_id: str, *, schedule_monitor_actions: bool = False
    ) -> None:
        """Persist events queued while the handover barrier owned admission."""

        while True:
            async with self.handover_condition:
                events = self.handover_event_queue.pop(run_id, [])
            if not events:
                return
            for index, (adapter, event) in enumerate(events):
                try:
                    await self._handle_provider_event_without_admission(
                        run_id,
                        adapter,
                        event,
                        update_adapter_snapshot=False,
                        schedule_monitor_actions=schedule_monitor_actions,
                    )
                except BaseException:
                    async with self.handover_condition:
                        self.handover_event_queue.setdefault(run_id, []).extend(
                            events[index:]
                        )
                    raise

    async def _flush_all_handover_events(
        self, *, schedule_monitor_actions: bool = False
    ) -> None:
        """Replay every routed queue before handover continues or releases admission."""

        while True:
            async with self.handover_condition:
                run_ids = list(self.handover_event_queue)
            if not run_ids:
                return
            for run_id in run_ids:
                async with self._run_lock(run_id):
                    await self._flush_handover_events(
                        run_id, schedule_monitor_actions=schedule_monitor_actions
                    )

    async def _capture_live_handover_events(self) -> None:
        """Move buffered events from live adapters into the handover queue."""

        for run_id, adapter in list(self.adapters.items()):
            event_lock = self.event_processing_locks.setdefault(
                run_id, asyncio.Lock()
            )
            async with event_lock:
                for event in await adapter.drain_events():
                    event_run_id = self.event_routes.get(
                        (id(adapter), event.generation), run_id
                    )
                    async with self.handover_condition:
                        self.handover_event_queue.setdefault(
                            event_run_id, []
                        ).append((adapter, event))

    async def _drain_stopped_adapter(
        self, run_id: str, adapter: ProviderAdapter
    ) -> None:
        """Drain the adapter and every event already taken by its pump."""

        event_lock = self.event_processing_locks.setdefault(
            run_id, asyncio.Lock()
        )
        while True:
            async with event_lock:
                for event in await adapter.drain_events():
                    event_run_id = self.event_routes.get(
                        (id(adapter), event.generation), run_id
                    )
                    async with self.handover_condition:
                        handover_active = self.handover_active
                        if handover_active:
                            self.handover_event_queue.setdefault(
                                event_run_id, []
                            ).append((adapter, event))
                    if not handover_active:
                        await self._handle_provider_event_without_admission(
                            event_run_id,
                            adapter,
                            event,
                            update_adapter_snapshot=True,
                            schedule_monitor_actions=True,
                        )
            async with self.event_drain_condition:
                if not self.event_inflight_counts.get(run_id):
                    return
                await self.event_drain_condition.wait()

    def _schedule_monitor_actions(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        event: ProviderEvent,
        *,
        prior_state: LifecycleState,
        record: RunRecord,
    ) -> None:
        if not self.store.is_current(record) or record.replaced_by_run_id:
            return
        if event.provider is ProviderKind.CODEX:
            reached = accounts.codex_rate_limit_reached_type(event.payload)
            if reached is not None:
                outgoing_reset_at = (
                    accounts.rate_limit_reset_from_snapshot(event.payload)
                    or accounts.fallback_reset_time()
                )
                self._spawn_monitor_task(
                    self._handle_codex_rate_limit_event(
                        run_id,
                        outgoing_reset_at=outgoing_reset_at,
                    ),
                    name=f"codex-rate-limit-{run_id}",
                )
            if (
                event.direction != "client"
                and accounts.detect_codex_auth_dead_payload(event.payload)
            ):
                existing = self.auth_dead_recoveries.get(run_id)
                if existing is None or existing.done():
                    task = self._spawn_monitor_task(
                        self._recover_codex_auth_dead(
                            run_id,
                            adapter,
                            prior_state=prior_state,
                        ),
                        name=f"codex-auth-dead-{run_id}",
                    )
                    self.auth_dead_recoveries[run_id] = task

                    def clear_finished(done: asyncio.Task[None]) -> None:
                        if self.auth_dead_recoveries.get(run_id) is done:
                            self.auth_dead_recoveries.pop(run_id, None)

                    task.add_done_callback(clear_finished)
            return
        if (
            event.provider is ProviderKind.CLAUDE
            and event.direction != "stdin"
            and not accounts.claude_turn_succeeded(event.payload)
            and accounts.detect_claude_limit_payload(event.payload)
        ):
            # Throttle per run_id so a replacement run under the same
            # ticket can emit its own limit notice within the hour. See
            # last_limit_alert_at comment.
            if self._seconds_since(self.last_limit_alert_at.get(record.run_id, 0.0)) < 3600:
                return
            self.last_limit_alert_at[record.run_id] = time.monotonic()
            self._spawn_monitor_task(
                self._publish(
                    {
                        "type": "claude_limit_hit",
                        "provider": "claude",
                        "ticket": record.agent_id,
                        # Per-ticket run_id lets the notice store reconcile
                        # against the live registry: a replaced Claude worker
                        # gets a new run_id and its stale-limit banner
                        # drops on the next refresh.
                        "run_id": record.run_id,
                        "window": "",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                name=f"claude-limit-{run_id}",
            )

    async def _handle_codex_rate_limit_event(
        self,
        run_id: str,
        *,
        outgoing_reset_at: str,
    ) -> None:
        try:
            await self.request_codex_rotation(
                operation_id=str(uuid4()),
                force_target=None,
                outgoing_reset_at=outgoing_reset_at,
            )
        except accounts.RotationDebouncedError:
            return
        except StoreConflict as exc:
            if "another Codex account rotation is active" in str(exc):
                return
            run_ids = self._current_codex_run_ids()
            await self._publish(
                {
                    "type": "codex_rotation_failed",
                    "error": str(exc),
                    "tickets": sorted(run_ids),
                    "run_ids": run_ids,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
        except accounts.NoEligibleAccountError:
            if self._seconds_since(self.last_no_eligible_alert) < 3600:
                return
            self.last_no_eligible_alert = time.monotonic()
            run_ids = self._current_codex_run_ids()
            tickets = sorted(run_ids)
            if not tickets:
                try:
                    record = self.store.get(run_id)
                    tickets = [record.agent_id]
                    run_ids = {record.agent_id: record.run_id}
                except RunNotFound:
                    tickets = []
            await self._publish(
                {
                    "type": "codex_limit_no_eligible",
                    "tickets": tickets,
                    "run_ids": run_ids,
                    "reset_at": outgoing_reset_at,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
        except accounts.RotationError as exc:
            run_ids = self._current_codex_run_ids()
            await self._publish(
                {
                    "type": "codex_rotation_failed",
                    "error": str(exc),
                    "tickets": sorted(run_ids),
                    "run_ids": run_ids,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )

    async def _recover_codex_auth_dead(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        *,
        prior_state: LifecycleState,
    ) -> None:
        if prior_state not in {LifecycleState.WORKING, LifecycleState.IDLE}:
            return
        try:
            initial = self.store.get(run_id)
        except RunNotFound:
            return
        now_mono = time.monotonic()
        history = self.auth_dead_attempts.setdefault(initial.run_id, [])
        history[:] = [
            ts
            for ts in history
            if now_mono - ts < accounts.AUTH_DEAD_WINDOW_SECONDS
        ]
        if history and now_mono - history[-1] < accounts.AUTH_DEAD_COOLDOWN_SECONDS:
            await self._publish_auth_dead_exhausted(initial.agent_id, initial.run_id, now_mono)
            return
        if len(history) >= accounts.AUTH_DEAD_MAX_ATTEMPTS:
            await self._publish_auth_dead_exhausted(initial.agent_id, initial.run_id, now_mono)
            return
        history.append(now_mono)

        operation_id = str(uuid4())
        revived: list[str] = []
        revived_run_ids: dict[str, str] = {}
        failed: list[str] = []
        failed_reasons: dict[str, str] = {}
        failed_run_ids: dict[str, str] = {}

        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                try:
                    record = self.store.get(run_id)
                    if (
                        not self.store.is_current(record)
                        or record.replaced_by_run_id
                        or record.state in TERMINAL_STATES
                    ):
                        return
                    if self.adapters.get(run_id) is not adapter:
                        return
                    session_id = record.provider_session_id
                    if not session_id:
                        raise StoreConflict("run has no provider session id")
                    self.store.mark_quiesce_intent(
                        run_id,
                        operation_id,
                        session_id,
                        resume_state=prior_state,
                    )
                    await self._close_and_drain_adapter(run_id, adapter)
                    detached = self.store.finish_provider_detached(
                        run_id,
                        operation_id,
                        reason="quiesced for auth-dead recovery",
                    )
                    await self._publish_agent_change(detached.agent_id)
                    await self._resume_run_without_admission(run_id, automatic=False)
                    revived.append(record.agent_id)
                    revived_run_ids[record.agent_id] = record.run_id
                except Exception as exc:
                    try:
                        current = self.store.transition(
                            run_id,
                            LifecycleState.BLOCKED,
                            reason=f"auth-dead exact-session resume failed: {exc}",
                        )
                    except Exception:
                        current = None
                    if current is not None:
                        await self._publish_agent_change(current.agent_id)
                        failed.append(current.agent_id)
                        failed_reasons[current.agent_id] = str(exc)
                        failed_run_ids[current.agent_id] = current.run_id
                    else:
                        failed.append(initial.agent_id)
                        failed_reasons[initial.agent_id] = str(exc)
                        failed_run_ids[initial.agent_id] = initial.run_id

        if revived or failed:
            await self._publish(
                {
                    "type": "codex_auth_dead_revival",
                    "provider": "codex",
                    "failure": "auth",
                    "credential_source": "current",
                    "revived": revived,
                    "failed": failed,
                    "failed_reasons": failed_reasons,
                    # Per-ticket run_id lets the notice store reconcile
                    # against the live registry: a replaced ticket has a new
                    # run_id and its notice can then be dropped. See
                    # AccountNoticeStore.reconcile_with_live and WIKI-228.
                    "failed_run_ids": failed_run_ids,
                    "revived_run_ids": revived_run_ids,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )

    async def _publish_auth_dead_exhausted(
        self,
        agent_id: str,
        run_id: str,
        now_mono: float,
    ) -> None:
        if (
            now_mono - self.auth_dead_alert_at.get(run_id, 0.0)
            < accounts.AUTH_DEAD_ALERT_INTERVAL_SECONDS
        ):
            return
        self.auth_dead_alert_at[run_id] = now_mono
        await self._publish(
            {
                "type": "codex_auth_dead_exhausted",
                "provider": "codex",
                "failure": "auth",
                "credential_source": "current",
                "exhausted": True,
                "tickets": [agent_id],
                # Per-ticket run_id lets the notice store recognize a
                # replaced worker (same ticket, different run_id) and drop
                # the stale notice on reconciliation. See WIKI-228.
                "run_ids": {agent_id: run_id},
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def _deliver_next_queued(self, run_id: str, adapter: ProviderAdapter) -> None:
        async with self._run_mutation_admission(wait_for_handover=True):
            async with self._run_lock(run_id):
                await self._deliver_next_queued_locked(run_id, adapter)

    async def _apply_desired_model_locked(
        self,
        run_id: str,
        adapter: ProviderAdapter | None,
        *,
        trigger: str,
    ) -> tuple[str, ProviderAdapter | None, RunRecord]:
        record = self.store.get(run_id)
        desired_model = record.desired_model
        if not desired_model:
            return run_id, adapter, record
        if desired_model == record.model:
            record = self.store.set_desired_model(run_id, None)
            return run_id, adapter, record
        if not self.store.is_current(record) or record.replaced_by_run_id:
            raise StoreConflict("model-change target is no longer current")
        if adapter is None:
            adapter = self.adapters.get(run_id)
        queued_messages = self.store.queued_messages(run_id)
        old_model = record.model
        prompt = self._model_change_prompt(
            record,
            old_model=old_model,
            new_model=desired_model,
        )
        replacement = await self._replace_without_admission(
            run_id,
            prompt,
            desired_model,
        )
        if queued_messages:
            replacement = self.store.replace_queued_messages(
                replacement.run_id,
                queued_messages,
            )
        self._append_model_changed_event(
            replacement,
            old_model=old_model,
            new_model=desired_model,
            trigger=trigger,
        )
        await self._publish(
            {
                "type": "model_changed",
                "ticket": replacement.agent_id,
                "from_model": old_model,
                "to_model": desired_model,
                "run_id": replacement.run_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self._publish(
            {
                "type": "session",
                "ticket": replacement.agent_id,
                "surface": "session",
            }
        )
        return replacement.run_id, self.adapters.get(replacement.run_id), replacement

    async def _deliver_next_queued_locked(
        self,
        run_id: str,
        adapter: ProviderAdapter,
    ) -> None:
        """Acknowledge a durable on-idle message only after provider acceptance."""

        if self.adapters.get(run_id) is not adapter:
            return
        lock = self.queue_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            original_run_id = run_id
            run_id, adapter, _ = await self._apply_desired_model_locked(
                run_id,
                adapter,
                trigger="queued-message",
            )
            if adapter is None or self.adapters.get(run_id) is not adapter:
                return
            if run_id != original_run_id:
                self._spawn_monitor_task(
                    self._deliver_next_queued(run_id, adapter),
                    name=f"agent-idle-boundary-{run_id}",
                )
                return
            queued = self.store.peek_queued_message(run_id)
            if queued is None:
                return
            pending_id = queued.get("pending_id")
            queued_source = queued.get("source")
            effect: dict[str, Any] | None = None
            if pending_id is not None:
                effect = self.store.command_log.steer_effect_for_pending(
                    run_id, pending_id
                )
                if effect is not None and effect["status"] in {"sent", "acknowledged"}:
                    self.store.remove_queued_message_by_pending_id(run_id, pending_id)
                    return
                if effect is not None and effect["status"] == "sending":
                    if self.store.steer_delivery_observed(run_id, pending_id):
                        result = {"status": "sent", "pending_id": pending_id}
                        self.store.command_log.update_steer_effect(
                            str(effect["method"]),
                            str(effect["request_id"]),
                            "acknowledged",
                            result,
                        )
                        self.store.remove_queued_message_by_pending_id(
                            run_id, pending_id
                        )
                    # No echo yet. The recover_on_start sweep resolves
                    # sending effects whose transport died; here we just
                    # wait so the healthy in-flight case can still finish.
                    return
            # WIKI-232 R3 H1: preserve the queued item and its (still
            # queued) effect when the provider is busy. Every entry point
            # that reaches here for a fresh delivery already had reason
            # to believe the adapter is IDLE — the natural WORKING->IDLE
            # transition, an inline send_on_idle path that verified
            # ``adapter.status()``, or the resume/recovery path that
            # checked ``status.state`` — but the state can flip between
            # that check and the queue-lock acquisition. Confirm with a
            # fresh snapshot: if the provider is no longer idle, back off
            # so ``send_on_idle`` never raises ProviderBusy inside the
            # exception handler, which would terminate the effect
            # uncertain and cascade through the queue.
            #
            # Only gate when we have a durable effect to preserve
            # (``effect`` is not None and status is ``queued``). Ad-hoc
            # deliveries with no bound effect fall through to the
            # historical behavior so the fixture-driven "adapter queues
            # internally" tests keep passing.
            if effect is not None and effect["status"] == "queued":
                try:
                    fresh_status = adapter.snapshot()
                except Exception:
                    fresh_status = None
                if (
                    fresh_status is not None
                    and fresh_status.state is not LifecycleState.IDLE
                ):
                    return
            if pending_id is not None:
                self.store.track_pending_user_message(
                    run_id,
                    pending_id,
                    queued["text"],
                    source=queued_source if isinstance(queued_source, str) else None,
                )
            try:
                if pending_id is not None:
                    self.store.command_log.mark_steer_sending_for_pending(
                        run_id, pending_id
                    )
                status = await adapter.send_on_idle(queued["text"])
            except Exception as exc:
                if pending_id is not None:
                    self.store.discard_pending_user_message(run_id, pending_id)
                    # The "sending" marker was written above and, per state
                    # machine, cannot revert to "queued"; if we do not
                    # terminate this effect it wedges every later queued
                    # message behind an effect whose provider echo can
                    # never arrive (WIKI-232 H2). Report uncertainty and
                    # drop the head so the queue can drain.
                    effect_for_pending = (
                        self.store.command_log.steer_effect_for_pending(
                            run_id, pending_id
                        )
                    )
                    if (
                        effect_for_pending is not None
                        and effect_for_pending["status"] == "sending"
                    ):
                        self.store.command_log.update_steer_effect(
                            str(effect_for_pending["method"]),
                            str(effect_for_pending["request_id"]),
                            "acknowledged",
                            {
                                "status": "uncertain",
                                "pending_id": pending_id,
                                "reason": f"adapter send failed: {exc}",
                            },
                        )
                        self.store.remove_queued_message_by_pending_id(
                            run_id, pending_id
                        )
                record = self.store.get(run_id)
                record = self.store.transition(
                    run_id,
                    record.state,
                    reason=f"queued message delivery failed: {exc}",
                )
                await self._publish(
                    {"type": "session", "ticket": record.agent_id, "surface": "queue"}
                )
                # Dropping an uncertain head does not itself emit any later
                # idle transition, so anything queued behind it would stay
                # stranded until the next external trigger (WIKI-232 R2).
                # Schedule a fresh drain if there is more work, this adapter
                # is still the one attached, AND the provider is currently
                # IDLE — otherwise the spawned task would hit ProviderBusy
                # on the next head, terminate it uncertain, and cascade
                # (WIKI-232 R3 H1). The natural WORKING->IDLE transition
                # schedules the drain when the provider frees up.
                try:
                    drain_status = adapter.snapshot()
                except Exception:
                    drain_status = None
                if (
                    self.store.peek_queued_message(run_id) is not None
                    and self.adapters.get(run_id) is adapter
                    and drain_status is not None
                    and drain_status.state is LifecycleState.IDLE
                ):
                    self._spawn_monitor_task(
                        self._deliver_next_queued(run_id, adapter),
                        name=f"agent-queue-drain-{run_id}",
                    )
                return
            if pending_id is not None:
                self.store.command_log.mark_steer_sent_for_pending(run_id, pending_id)
            self.store.pop_queued_message(run_id)
            record = self.store.update_adapter_status(run_id, status)
            record = self.store.clear_automatic_resume_suppression(run_id)
            await self._publish(
                {"type": "session", "ticket": record.agent_id, "surface": "queue"}
            )

    async def queue_model_change(self, run_id: str, model: str) -> dict[str, Any]:
        async with self._run_mutation_admission():
            if self.store.get(run_id).provider is ProviderKind.CODEX:
                self._assert_codex_fleet_available()
                async with self.codex_fleet_lock:
                    self._assert_codex_fleet_available()
                    async with self._run_lock(run_id):
                        return await self._queue_model_change_locked(run_id, model)
            async with self._run_lock(run_id):
                return await self._queue_model_change_locked(run_id, model)

    async def _queue_model_change_locked(
        self,
        run_id: str,
        model: str,
    ) -> dict[str, Any]:
        record = self.store.get(run_id)
        if not self.store.is_current(record) or record.replaced_by_run_id:
            raise StoreConflict("model-change target is no longer current")
        if record.state in TERMINAL_STATES:
            raise StoreConflict(f"{record.state.value} runs cannot change model")
        if model == record.model:
            raise ValueError("desired model matches current model")
        record = self.store.set_desired_model(run_id, model)
        await self._publish_agent_change(record.agent_id)
        await self._publish(
            {"type": "session", "ticket": record.agent_id, "surface": "session"}
        )
        if record.state in {LifecycleState.IDLE, LifecycleState.BLOCKED}:
            adapter = self.adapters.get(run_id)
            next_run_id, _, applied = await self._apply_desired_model_locked(
                run_id,
                adapter,
                trigger=record.state.value,
            )
            return {
                "status": "applied",
                "run_id": next_run_id,
                "desired_model": applied.desired_model,
                "model": applied.model,
            }
        return {"status": "queued", "desired_model": model}

    async def cancel_model_change(self, run_id: str) -> dict[str, Any]:
        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                record = self.store.set_desired_model(run_id, None)
                await self._publish_agent_change(record.agent_id)
                await self._publish(
                    {"type": "session", "ticket": record.agent_id, "surface": "session"}
                )
                return {"status": "canceled", "desired_model": None}

    def _route_adapter_generation(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        generation: int,
    ) -> None:
        self.event_routes[(id(adapter), generation)] = run_id

    def _remove_adapter_mapping(self, run_id: str, adapter: ProviderAdapter) -> None:
        if self.adapters.get(run_id) is adapter:
            self.adapters.pop(run_id, None)
            try:
                self.store.set_control_attached(run_id, False)
            except RunNotFound:
                pass
        adapter_key = id(adapter)
        self.event_routes = {
            key: target
            for key, target in self.event_routes.items()
            if key[0] != adapter_key
        }

    def _attach_adapter(self, run_id: str, adapter: ProviderAdapter) -> None:
        existing = self.adapters.get(run_id)
        if existing is not None and existing is not adapter:
            raise StoreConflict(
                f"run already has an attached provider adapter: {run_id}"
            )
        self._clear_adapter_loss(run_id)
        old_task = self.event_tasks.pop(run_id, None)
        if old_task is not None:
            old_task.cancel()
        self.adapters[run_id] = adapter
        adapter.set_process_created_callback(
            lambda pid: self.store.record_provider_process_created(run_id, pid)
        )
        self.store.set_control_attached(run_id, True)
        self.event_tasks[run_id] = asyncio.create_task(
            self._pump_events(run_id, adapter),
            name=f"agent-events-{run_id}",
        )

    async def _detach_adapter(
        self, run_id: str, *, preserve_event_routes: bool = False
    ) -> None:
        self._clear_adapter_loss(run_id)
        # Prune the per-run Claude limit-alert timestamp so a replacement
        # run under the same ticket can emit its own limit notice inside
        # the hour (round-8 finding 5).
        self.last_limit_alert_at.pop(run_id, None)
        adapter = self.adapters.pop(run_id, None)
        if adapter is not None:
            try:
                self.store.set_control_attached(run_id, False)
            except RunNotFound:
                pass
        task = self.event_tasks.pop(run_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if adapter is not None and not preserve_event_routes:
            adapter_key = id(adapter)
            self.event_routes = {
                key: target
                for key, target in self.event_routes.items()
                if key[0] != adapter_key
            }

    def _clear_auth_dead_recovery_state(self, run_id: str) -> None:
        self.auth_dead_attempts.pop(run_id, None)
        self.auth_dead_alert_at.pop(run_id, None)

    async def _close_and_drain_adapter(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        *,
        finalize: str = "close",
        suppress_operation_errors: bool = True,
        timeout: float = 2.0,
    ) -> AdapterStatus | None:
        """Persist already-emitted failure/exit events before detaching transport."""

        stream_key = id(adapter)
        self.expected_stream_ends.add(stream_key)
        if self.adapters.get(run_id) is not adapter or run_id not in self.event_tasks:
            self._attach_adapter(run_id, adapter)
        status: AdapterStatus | None = None
        operation_error: Exception | None = None
        try:
            if finalize in {"stop", "archive"}:
                try:
                    status = (
                        await adapter.stop()
                        if finalize == "stop"
                        else await adapter.archive()
                    )
                except Exception as exc:
                    operation_error = exc
            elif finalize != "close":
                raise ValueError(f"unknown adapter finalizer: {finalize}")
            try:
                await adapter.close()
            except Exception:
                pass
            task = self.event_tasks.get(run_id)
            if task is not None and task is not asyncio.current_task():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            self.expected_stream_ends.discard(stream_key)
            await self._detach_adapter(run_id)
        if operation_error is not None and not suppress_operation_errors:
            raise operation_error
        return status

    def _reset_status_for_replacement(self, agent_id: str) -> None:
        """Detach the prior run's status before replacement ownership changes."""

        # Callers hold the per-agent lock. The old provider has already been
        # detached or drained, so no old-run status survives into the
        # replacement boundary; replacement launch paths call this before
        # publishing the new current projection.
        self.store.status_path(agent_id).unlink(missing_ok=True)

    async def _quiesce_adapter_for_replacement(
        self,
        run_id: str,
        adapter: ProviderAdapter,
    ) -> None:
        """Stop the old provider before its status ownership is reset."""

        stream_key = id(adapter)
        self.expected_stream_ends.add(stream_key)
        try:
            await adapter.stop()
            # Keep the pump attached through provider stop. This barrier
            # covers both its local event and the adapter's buffered queue.
            await self._drain_stopped_adapter(run_id, adapter)
            await self._detach_adapter(run_id, preserve_event_routes=True)
        finally:
            self.expected_stream_ends.discard(stream_key)

    async def _await_cleanup(self, awaitable: Any) -> Any:
        """Finish a cleanup operation even when its caller is cancelled."""

        task = asyncio.create_task(awaitable)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    try:
                        return task.result()
                    except BaseException:
                        return None

    async def _cleanup_cancelled_replacement(
        self,
        old: RunRecord,
        replacement: RunRecord,
        adapter: ProviderAdapter,
        *,
        published: bool,
    ) -> None:
        """Leave no live provider or unowned current run after cancellation."""

        cleanup_run_id = replacement.run_id if published else old.run_id
        try:
            # Do not wait for a second provider RPC after cancellation. The
            # adapter close contract terminates the process group and drains
            # its reader tasks, which is the hard ownership boundary needed
            # for this abort path.
            await self._await_cleanup(adapter.close())
        except BaseException:
            # A provider can reject close while its process is still live. A
            # second close attempt is deliberately cancellation-shielded too;
            # close is the adapter contract's hard process-group teardown.
            try:
                await self._await_cleanup(adapter.close())
            except BaseException:
                pass
        finally:
            await self._await_cleanup(self._detach_adapter(cleanup_run_id))

        terminal_status = AdapterStatus(
            state=LifecycleState.DEAD,
            session_id=None,
            pid=None,
            generation=0,
            active_turn_id=None,
            transcript_path=None,
        )
        if published:
            try:
                self.store.abort_replace(
                    old.run_id,
                    replacement.run_id,
                    reason="replacement cancelled",
                    adapter_status=terminal_status,
                )
            except BaseException:
                # If rollback itself lost a filesystem race, terminalize the
                # published child rather than leaving a nonterminal run with
                # no adapter control.
                try:
                    child = self.store.get(replacement.run_id)
                    if child.state not in TERMINAL_STATES:
                        self.store.transition(
                            replacement.run_id,
                            LifecycleState.DEAD,
                            reason="replacement cancelled",
                            adapter_status=terminal_status,
                        )
                except BaseException:
                    pass
        else:
            try:
                current = self.store.get(old.run_id)
                if current.state not in TERMINAL_STATES:
                    self.store.transition(
                        old.run_id,
                        LifecycleState.DEAD,
                        reason="replacement cancelled before publication",
                        adapter_status=terminal_status,
                    )
            except BaseException:
                pass
        self._clear_auth_dead_recovery_state(old.run_id)
        self._clear_auth_dead_recovery_state(replacement.run_id)
        try:
            await self._publish_agent_change(old.agent_id)
        except BaseException:
            pass

    async def _cleanup_cancelled_launch(
        self,
        record: RunRecord,
        adapter: ProviderAdapter,
        *,
        rollback_start: bool = False,
    ) -> None:
        """Abort a cancelled provider start/resume without leaving controlless state."""

        try:
            await self._await_cleanup(adapter.close())
        except BaseException:
            pass
        finally:
            await self._await_cleanup(self._detach_adapter(record.run_id))
        if rollback_start:
            try:
                self.store.abort_start(
                    record.run_id,
                    reason="provider launch cancelled",
                )
            except BaseException:
                pass
            try:
                await self._publish_agent_change(record.agent_id)
            except BaseException:
                pass
            return
        terminal_status = AdapterStatus(
            state=LifecycleState.DEAD,
            session_id=None,
            pid=None,
            generation=0,
            active_turn_id=None,
            transcript_path=None,
        )
        try:
            current = self.store.get(record.run_id)
            if current.state not in TERMINAL_STATES:
                self.store.transition(
                    record.run_id,
                    LifecycleState.DEAD,
                    reason="provider launch cancelled",
                    adapter_status=terminal_status,
                )
            self._clear_auth_dead_recovery_state(record.run_id)
            await self._publish_agent_change(current.agent_id)
        except BaseException:
            pass

    async def _cleanup_precommit_adapter(
        self,
        run_id: str,
        adapter: ProviderAdapter,
    ) -> None:
        """Close an adapter whose fresh start never committed attachment."""

        try:
            await adapter.close()
        except BaseException:
            pass
        self._clear_adapter_loss(run_id)
        task = self.event_tasks.pop(run_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.adapters.get(run_id) is adapter:
            self.adapters.pop(run_id, None)
        adapter_key = id(adapter)
        self.event_routes = {
            key: target
            for key, target in self.event_routes.items()
            if key[0] != adapter_key
        }

    async def start_run(
        self,
        *,
        agent_id: str,
        provider: ProviderKind,
        role: str,
        model: str,
        worktree: str,
        prompt: str,
        effort: str | None = None,
        orchestrator_id: str | None = None,
        migrate_legacy: bool = False,
        backend_base_url: str | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
        implicit_request_id: bool = False,
    ) -> RunRecord:
        if request_id:
            durable = self.store.find_start_request(request_id)
            if durable is not None:
                return durable
        resolved = resolve_safe_worktree(worktree)
        record = RunRecord.new(
            agent_id=agent_id,
            provider=provider,
            role=role,
            model=model,
            worktree=str(resolved),
            prompt=prompt,
            effort=effort,
            orchestrator_id=orchestrator_id,
            backend_base_url=backend_base_url,
            run_id=run_id,
            start_request_id=request_id,
            implicit_start_request=implicit_request_id,
        )
        prompt = inject_runtime_card(
            record,
            prompt,
            status_path=self.store.status_path(record.agent_id),
        )
        record.initial_prompt = prompt
        async with self._run_mutation_admission():
            if provider is ProviderKind.CODEX:
                self._assert_codex_fleet_available()
                async with self.codex_fleet_lock:
                    self._assert_codex_fleet_available()
                    async with self._agent_lock(record.agent_id):
                        self.store.create(
                            record,
                            migrate_legacy=migrate_legacy,
                            transactional_start=True,
                        )
                        return await self._launch_record(record, prompt, rollback_start=True)
            async with self._agent_lock(record.agent_id):
                self.store.create(
                    record,
                    migrate_legacy=migrate_legacy,
                    transactional_start=True,
                )
                return await self._launch_record(record, prompt, rollback_start=True)

    async def _launch_record(
        self,
        record: RunRecord,
        prompt: str,
        *,
        rollback_start: bool = False,
    ) -> RunRecord:
        adapter: ProviderAdapter | None = None
        try:
            adapter = self.adapter_factory(record)
            self._attach_adapter(record.run_id, adapter)
        except asyncio.CancelledError:
            if adapter is not None:
                await self._cleanup_precommit_adapter(record.run_id, adapter)
            if rollback_start:
                self.store.abort_start(
                    record.run_id,
                    reason="provider launch cancelled before attachment",
                )
                await self._publish_agent_change(record.agent_id)
            raise
        except Exception as exc:
            if adapter is not None:
                await self._cleanup_precommit_adapter(record.run_id, adapter)
            if rollback_start:
                reason = f"provider attachment failed: {exc}"
                self.store.abort_start(record.run_id, reason=reason)
                raise ProviderProcessError(reason) from exc
            raise
        assert adapter is not None
        request = StartRequest(
            prompt=prompt,
            model=record.model,
            effort=record.effort,
            worktree=record.worktree,
            run_id=record.run_id,
            agent_id=record.agent_id,
        )
        try:
            status = await adapter.start(request)
            record = self.store.update_adapter_status(record.run_id, status)
            self._route_adapter_generation(record.run_id, adapter, status.generation)
        except asyncio.CancelledError:
            await self._cleanup_cancelled_launch(
                record,
                adapter,
                rollback_start=rollback_start,
            )
            raise
        except Exception as exc:
            await self._close_and_drain_adapter(record.run_id, adapter)
            reason = f"provider start failed: {exc}"
            if rollback_start:
                self.store.abort_start(record.run_id, reason=reason)
                raise ProviderProcessError(reason) from exc
            else:
                record = self.store.transition(
                    record.run_id,
                    LifecycleState.DEAD,
                    reason=reason,
                )
                self._clear_auth_dead_recovery_state(record.run_id)
                await self._publish_agent_change(record.agent_id)
            raise
        self.store.commit_start(record.run_id)
        await self._publish_agent_change(record.agent_id)
        return record

    async def resume_run(self, run_id: str) -> RunRecord:
        async with self._run_mutation_admission():
            if self.store.get(run_id).provider is ProviderKind.CODEX:
                self._assert_codex_fleet_available()
                async with self.codex_fleet_lock:
                    self._assert_codex_fleet_available()
                    async with self._run_lock(run_id):
                        return await self._resume_run_without_admission(
                            run_id,
                            automatic=False,
                        )
            async with self._run_lock(run_id):
                return await self._resume_run_without_admission(
                    run_id,
                    automatic=False,
                )

    async def _resume_run_without_admission(
        self,
        run_id: str,
        *,
        automatic: bool,
    ) -> RunRecord:
        """Resume a run after the caller has acquired mutation admission."""

        return await self._resume_run_without_handover(
            run_id,
            automatic=automatic,
        )

    async def _resume_run_without_handover(
        self,
        run_id: str,
        *,
        automatic: bool,
    ) -> RunRecord:
        # Re-read immediately before resume so stale recovery snapshots cannot
        # revive a deregistered, terminal, or replaced run (PR #31 invariant).
        record = self.store.get(run_id)
        self.pipeline_failures.pop(run_id, None)
        if run_id in self.adapters:
            raise StoreConflict("run already has an attached provider adapter")
        if not self.store.is_current(record):
            raise StoreConflict("run is no longer current")
        if record.replaced_by_run_id:
            raise StoreConflict("run was replaced")
        if record.state in {LifecycleState.DEAD, LifecycleState.COMPLETED}:
            raise StoreConflict(f"{record.state.value} runs never resume")
        recovery_state = record.recovery_from_state or record.state
        if recovery_state not in {
            LifecycleState.WORKING,
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.IDLE,
        }:
            raise StoreConflict(
                f"state {recovery_state.value} is not eligible for exact-session resume"
            )
        session_id = record.provider_session_id
        quiesce_operation_id = record.quiesce_operation_id
        if not session_id:
            raise StoreConflict("run has no provider session id")
        if self.pid_alive(record.provider_pid):
            raise StoreConflict(
                "provider PID is live without attached control; refusing duplicate resume"
            )
        resolve_safe_worktree(record.worktree)
        # Request ids belong to the old transport generation. A resumed
        # provider must re-emit any still-actionable request before the UI can
        # answer it through the new adapter.
        approval_prompt = (
            _approval_recovery_prompt(record)
            if recovery_state is LifecycleState.WAITING_APPROVAL
            else None
        )
        old_pending_requests = deepcopy(record.pending_requests)

        adapter = self.adapter_factory(record)
        self._attach_adapter(record.run_id, adapter)
        try:
            status = await adapter.resume(session_id)
            record = self.store.update_adapter_status(
                run_id,
                status,
                guard_automatic_resume=automatic,
            )
            if approval_prompt is not None:
                status = await adapter.send_now(approval_prompt)
                record = self.store.update_adapter_status(
                    run_id,
                    status,
                    guard_automatic_resume=automatic,
                )
                deadline = time.monotonic() + self.approval_recovery_timeout_seconds
                while time.monotonic() < deadline:
                    current = self.store.get(run_id)
                    if (
                        current.state is LifecycleState.WAITING_APPROVAL
                        and current.pending_requests
                        and current.pending_requests != old_pending_requests
                    ):
                        record = current
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise StoreConflict(
                        "provider did not re-emit the pending approval after transport recovery"
                    )
                for key, pending in old_pending_requests.items():
                    if (
                        record.pending_requests.get(key) == pending
                    ):
                        record = self.store.clear_pending_request_key(run_id, key)
        except asyncio.CancelledError:
            if approval_prompt is not None:
                await self._close_and_drain_adapter(record.run_id, adapter)
                self.store.restore_pending_requests(run_id, old_pending_requests)
            else:
                await self._cleanup_cancelled_launch(record, adapter)
            raise
        except Exception:
            await self._close_and_drain_adapter(record.run_id, adapter)
            if approval_prompt is not None:
                self.store.restore_pending_requests(run_id, old_pending_requests)
            raise
        if quiesce_operation_id is not None:
            record = self.store.clear_quiesce_marker(
                run_id,
                quiesce_operation_id,
            )
        elif not automatic:
            record = self.store.clear_automatic_resume_suppression(run_id)
        self._route_adapter_generation(run_id, adapter, status.generation)
        if status.state is LifecycleState.IDLE:
            await self._deliver_next_queued_locked(run_id, adapter)
            record = self.store.get(run_id)
        await self._publish_agent_change(record.agent_id)
        return record

    async def recover_on_start(self) -> list[dict[str, str]]:
        async with self._run_mutation_admission():
            async with self.recovery_scan_lock:
                self.store.abort_uncommitted_starts()
                results = await self._recover_once()
                await self._reconcile_sending_steer_effects()
                await self.command_queue.recover_pending()
                if self._reaper_due():
                    by_run_id = {
                        item["run_id"]: index for index, item in enumerate(results)
                    }
                    for reaped in await self._reap_lost_runs():
                        index = by_run_id.get(reaped["run_id"])
                        if index is None:
                            results.append(reaped)
                        else:
                            results[index] = reaped
                return results

    async def _reconcile_sending_steer_effects(self) -> None:
        """Resolve every steer effect left at status='sending' by a prior boot.

        A daemon stop between ``mark_steer_sending_for_pending`` and the
        provider echo leaves the on-idle queue head bound to a `sending`
        effect that no new echo can reach: the previous adapter transport
        is dead. Without this sweep the drain returns without changing
        the effect or removing the head, and every later queued message
        is starved (WIKI-232 H2).

        Called once per boot from ``recover_on_start`` after per-run
        recovery has replayed persisted events (so ``steer_delivery_observed``
        sees any composer echo that was already durable). Effects whose
        delivery is observed are promoted to ``acknowledged`` normally;
        effects whose delivery is not observed are marked ``acknowledged``
        with an ``uncertain`` payload, and the queued message plus the
        pending user message are dropped so the queue can drain.
        """

        if self._sending_effects_reconciled:
            return
        self._sending_effects_reconciled = True
        for effect in self.store.command_log.sending_steer_effects():
            run_id = str(effect.get("run_id") or "")
            pending_id = effect.get("pending_id")
            method = str(effect.get("method") or "")
            request_id = str(effect.get("request_id") or "")
            if not run_id or not method or not request_id:
                continue
            pending_id_str = str(pending_id) if isinstance(pending_id, str) else None
            async with self._run_lock(run_id):
                try:
                    self.store.get(run_id)
                except RunNotFound:
                    continue
                observed = False
                if pending_id_str is not None:
                    try:
                        observed = self.store.steer_delivery_observed(
                            run_id, pending_id_str
                        )
                    except RunNotFound:
                        continue
                if observed:
                    result: dict[str, Any] = {"status": "sent"}
                    if pending_id_str is not None:
                        result["pending_id"] = pending_id_str
                    self.store.command_log.update_steer_effect(
                        method, request_id, "acknowledged", result
                    )
                    if pending_id_str is not None:
                        self.store.remove_queued_message_by_pending_id(
                            run_id, pending_id_str
                        )
                    continue
                uncertain: dict[str, Any] = {"status": "uncertain"}
                if pending_id_str is not None:
                    uncertain["pending_id"] = pending_id_str
                uncertain["reason"] = "supervisor_restart_dropped_send"
                self.store.command_log.update_steer_effect(
                    method, request_id, "acknowledged", uncertain
                )
                if pending_id_str is not None:
                    self.store.remove_queued_message_by_pending_id(
                        run_id, pending_id_str
                    )
                    self.store.discard_pending_user_message(run_id, pending_id_str)
                # Removing the wedged head does not fire a new idle event,
                # so anything queued behind it would remain stranded until
                # the next external trigger (WIKI-232 R2). If the recovered
                # adapter is attached, the provider is IDLE, and there is
                # more work, schedule a drain — the monitor task will re-enter
                # through the normal queue lock once this reconcile step
                # releases its run lock. Gate on IDLE so a still-WORKING
                # adapter does not cascade uncertain-drops through the queue
                # via ProviderBusy (WIKI-232 R3 H1); the natural
                # WORKING->IDLE transition drains later.
                adapter = self.adapters.get(run_id)
                try:
                    drain_status = adapter.snapshot() if adapter is not None else None
                except Exception:
                    drain_status = None
                if (
                    adapter is not None
                    and self.store.peek_queued_message(run_id) is not None
                    and drain_status is not None
                    and drain_status.state is LifecycleState.IDLE
                ):
                    self._spawn_monitor_task(
                        self._deliver_next_queued(run_id, adapter),
                        name=f"agent-recover-queue-drain-{run_id}",
                    )

    async def _reap_lost_runs(self) -> list[dict[str, str]]:
        self.last_reaper_at = time.monotonic()
        results: list[dict[str, str]] = []
        for snapshot in self.store.list_runs():
            async with self._run_lock(snapshot.run_id):
                try:
                    record = self.store.get(snapshot.run_id)
                except RunNotFound:
                    continue
                if not self._reapable_adapter_loss(record):
                    continue
                record = self.store.transition(
                    record.run_id,
                    LifecycleState.COMPLETED,
                    reason="adapter_lost",
                )
                self._clear_adapter_loss(record.run_id)
                await self._publish_agent_change(record.agent_id)
                results.append(
                    {
                        "run_id": record.run_id,
                        "action": "reap",
                        "reason": "adapter_lost",
                    }
                )
        return results

    async def request_codex_rotation(
        self,
        *,
        operation_id: str,
        force_target: str | None,
        outgoing_reset_at: str | None = None,
    ) -> dict[str, Any]:
        """Run or join one shielded, daemon-owned account rotation."""

        try:
            parsed = UUID(operation_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("operation_id must be a canonical UUID") from exc
        if str(parsed) != operation_id:
            raise ValueError("operation_id must be a canonical UUID")

        task = self.codex_rotation_task
        if task is not None and not task.done():
            if operation_id != self.codex_rotation_operation_id:
                raise StoreConflict("another Codex account rotation is active")
            return await asyncio.shield(task)

        journal = self.store.read_codex_rotation_journal()
        if journal and journal.get("phase") == "complete":
            if journal.get("operation_id") == operation_id:
                result = journal.get("result")
                if isinstance(result, dict) and journal.get("status") == "succeeded":
                    return dict(result)
                raise accounts.RotationError(
                    str(journal.get("error") or "Codex account rotation failed")
                )

        task = asyncio.create_task(
            self._rotate_codex_fleet(
                operation_id,
                force_target,
                outgoing_reset_at=outgoing_reset_at,
            ),
            name=f"codex-account-rotation-{operation_id}",
        )
        self.codex_rotation_task = task
        self.codex_rotation_operation_id = operation_id

        def clear_finished(done: asyncio.Task[dict[str, Any]]) -> None:
            if self.codex_rotation_task is done:
                self.codex_rotation_task = None
                self.codex_rotation_operation_id = None

        task.add_done_callback(clear_finished)
        return await asyncio.shield(task)

    async def _rotate_codex_fleet(
        self,
        operation_id: str,
        force_target: str | None,
        *,
        outgoing_reset_at: str | None,
    ) -> dict[str, Any]:
        async with self._run_mutation_admission():
            return await self._rotate_codex_fleet_without_admission(
                operation_id,
                force_target,
                outgoing_reset_at=outgoing_reset_at,
            )

    async def _rotate_codex_fleet_without_admission(
        self,
        operation_id: str,
        force_target: str | None,
        *,
        outgoing_reset_at: str | None,
    ) -> dict[str, Any]:
        async with self.codex_fleet_lock:
            recovered = await self._recover_codex_rotation_locked()
            if not recovered:
                raise StoreConflict(
                    "a previous Codex rotation is waiting for provider PIDs to exit"
                )

            state = await asyncio.to_thread(accounts.read_state)
            state = await asyncio.to_thread(accounts.ensure_state_initialized, state)
            elapsed = accounts.seconds_since_last_rotation(state)
            if elapsed < accounts.debounce_seconds():
                raise accounts.RotationDebouncedError(
                    f"rotation debounced ({elapsed:.0f}s since last)"
                )
            available = await asyncio.to_thread(accounts.list_available_accounts)
            if force_target and force_target not in available:
                raise ValueError("unknown account")
            target = force_target or await asyncio.to_thread(
                accounts.pick_next_account, state
            )
            if target is None:
                raise accounts.NoEligibleAccountError(
                    "no eligible account for rotation"
                )
            if target == state.active:
                raise accounts.NoEligibleAccountError(
                    "target account is already active"
                )

            legacy = self.store.legacy_codex_agent_ids()
            if legacy:
                raise StoreConflict(
                    "Codex account rotation requires headless migration for: "
                    + ", ".join(legacy)
                )

            runs, close_only = await self._rotation_preflight_locked()
            now = datetime.now(timezone.utc).isoformat()
            journal: dict[str, Any] = {
                "schema_version": 1,
                "operation_id": operation_id,
                "phase": "prepared",
                "status": "working",
                "started_at": now,
                "updated_at": now,
                "outgoing": state.active,
                "incoming": target,
                "previous_state": state.to_dict(),
                "committed_state": None,
                "rollback_requires_auth_restore": False,
                "runs": runs,
                "close_only_run_ids": close_only,
                "result": None,
                "error": None,
            }
            self.store.write_codex_rotation_journal(journal)

            try:
                await self._quiesce_rotation_runs_locked(journal)
                journal["phase"] = "quiesced"
                self._write_rotation_journal(journal)

                outgoing = journal.get("outgoing")
                if isinstance(outgoing, str) and outgoing:
                    await asyncio.to_thread(accounts.snapshot_active_auth, outgoing)
                journal["phase"] = "installing"
                journal["rollback_requires_auth_restore"] = True
                self._write_rotation_journal(journal)
                account_result = await asyncio.to_thread(
                    accounts.rotate_credentials,
                    state=state,
                    force_target=target,
                    outgoing_reset_at=outgoing_reset_at,
                    snapshot_outgoing=False,
                )
                committed = await asyncio.to_thread(accounts.read_state)
                journal["committed_state"] = committed.to_dict()
                journal["phase"] = "committed"
                self._write_rotation_journal(journal)
            except Exception as exc:
                await self._rollback_rotation_locked(journal, exc)
                if isinstance(exc, (accounts.RotationError, StoreConflict)):
                    raise
                raise accounts.RotationError(
                    f"Codex credential rotation failed: {exc}"
                ) from exc

            journal["phase"] = "resuming"
            self._write_rotation_journal(journal)
            revival = await self._resume_rotation_runs_locked(journal)
            result = {
                "from": account_result.outgoing,
                "to": account_result.incoming,
                "revived": revival["revived"],
                "failed": revival["failed"],
                "failed_reasons": revival["failed_reasons"],
                "failed_run_ids": revival["failed_run_ids"],
                "revived_run_ids": revival["revived_run_ids"],
            }
            journal.update(
                {
                    "phase": "complete",
                    "status": "succeeded",
                    "result": result,
                    "error": None,
                }
            )
            self._write_rotation_journal(journal)
            account_result.revived = list(result["revived"])
            account_result.failed = list(result["failed"])
            account_result.failed_reasons = dict(result["failed_reasons"])
            account_result.failed_run_ids = dict(result["failed_run_ids"])
            await asyncio.to_thread(accounts.record_rotation_log, account_result)
            await self._publish(
                {
                    "type": "codex_rotation",
                    **result,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
            return result

    async def _rotation_preflight_locked(
        self,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Fail closed on every Wiki-owned Codex transport, including stale runs."""

        runs: list[dict[str, Any]] = []
        close_only: list[str] = []
        blockers: dict[str, str] = {}
        for snapshot in self.store.list_runs():
            if snapshot.provider is not ProviderKind.CODEX:
                continue
            adapter = self.adapters.get(snapshot.run_id)
            if adapter is None and self.pid_alive(snapshot.provider_pid):
                blockers[snapshot.agent_id] = (
                    "provider PID is live without supervisor control"
                )
                continue
            current = self.store.is_current(snapshot) and not snapshot.replaced_by_run_id
            resumable = snapshot.state in {
                LifecycleState.WORKING,
                LifecycleState.IDLE,
            }
            if not current or snapshot.state in {
                LifecycleState.DEAD,
                LifecycleState.COMPLETED,
            }:
                if adapter is not None:
                    close_only.append(snapshot.run_id)
                continue
            if not resumable:
                blockers[snapshot.agent_id] = (
                    f"state {snapshot.state.value} is not safe for auth rotation"
                )
                continue
            if not snapshot.provider_session_id:
                blockers[snapshot.agent_id] = "provider session id is missing"
                continue
            if snapshot.pending_requests:
                blockers[snapshot.agent_id] = "provider approval is pending"
                continue
            if adapter is not None:
                try:
                    status = await adapter.status()
                except Exception as exc:
                    blockers[snapshot.agent_id] = f"provider status failed: {exc}"
                    continue
                if status.state not in {
                    LifecycleState.WORKING,
                    LifecycleState.IDLE,
                }:
                    blockers[snapshot.agent_id] = (
                        f"provider reports {status.state.value}"
                    )
                    continue
                if status.session_id != snapshot.provider_session_id:
                    blockers[snapshot.agent_id] = (
                        "provider session identity does not match durable metadata"
                    )
                    continue
            runs.append(
                {
                    "run_id": snapshot.run_id,
                    "agent_id": snapshot.agent_id,
                    "session_id": snapshot.provider_session_id,
                    "resume_state": snapshot.state.value,
                    "detached": False,
                    "resumed": False,
                    "skipped": False,
                    "failed_reason": None,
                }
            )
        if blockers:
            details = "; ".join(
                f"{agent_id}: {reason}" for agent_id, reason in sorted(blockers.items())
            )
            raise StoreConflict(f"Codex fleet is not safe to rotate: {details}")
        return runs, close_only

    async def _quiesce_rotation_runs_locked(self, journal: dict[str, Any]) -> None:
        operation_id = str(journal["operation_id"])
        rows = journal["runs"]
        for row in rows:
            self.store.mark_quiesce_intent(
                row["run_id"],
                operation_id,
                row["session_id"],
            )
        journal["phase"] = "quiescing"
        self._write_rotation_journal(journal)

        for run_id in journal["close_only_run_ids"]:
            async with self._run_lock(run_id):
                record = self.store.get(run_id)
                adapter = self.adapters.get(run_id)
                if adapter is not None:
                    await self._close_and_drain_adapter(run_id, adapter)
                if self.pid_alive(record.provider_pid):
                    raise StoreConflict(
                        f"stale Codex provider PID remained live for {record.agent_id}"
                    )
                record = self.store.get(run_id)
                if record.state not in {
                    LifecycleState.DEAD,
                    LifecycleState.COMPLETED,
                }:
                    self.store.transition(
                        run_id,
                        LifecycleState.DEAD,
                        reason="stale provider closed for account rotation",
                    )
                self._clear_auth_dead_recovery_state(run_id)

        for row in rows:
            run_id = row["run_id"]
            async with self._run_lock(run_id):
                adapter = self.adapters.get(run_id)
                if adapter is not None:
                    status = await adapter.status()
                    if status.state is LifecycleState.WORKING or status.active_turn_id:
                        status = await adapter.interrupt()
                        self.store.update_adapter_status(run_id, status)
                    if status.active_turn_id:
                        raise StoreConflict(
                            f"provider turn did not interrupt for {row['agent_id']}"
                        )
                    await self._close_and_drain_adapter(run_id, adapter)
                record = self.store.get(run_id)
                if self.pid_alive(record.provider_pid):
                    raise StoreConflict(
                        f"provider PID remained live for {row['agent_id']}"
                    )
                record = self.store.finish_provider_detached(
                    run_id,
                    operation_id,
                    reason="quiesced for account rotation",
                )
                row["detached"] = True
                self._write_rotation_journal(journal)
                await self._publish_agent_change(record.agent_id)

    def _write_rotation_journal(self, journal: dict[str, Any]) -> None:
        journal["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.write_codex_rotation_journal(journal)

    async def _rollback_rotation_locked(
        self,
        journal: dict[str, Any],
        cause: Exception,
    ) -> None:
        restore_error: str | None = None
        restore_credentials = bool(journal.get("rollback_requires_auth_restore"))
        journal["phase"] = "rolling-back"
        self._write_rotation_journal(journal)
        if restore_credentials:
            try:
                await asyncio.to_thread(self._restore_previous_account, journal)
            except Exception as exc:
                restore_error = str(exc)
        revival = await self._resume_rotation_runs_locked(journal)
        reasons = dict(revival["failed_reasons"])
        if restore_error:
            reasons["credentials"] = restore_error
        detail = str(cause)
        if reasons:
            detail += f"; rollback failures: {reasons}"
        if restore_error or revival["pending"]:
            journal.update(
                {
                    "phase": "rolling-back",
                    "status": "working",
                    "result": None,
                    "error": detail,
                }
            )
            self._write_rotation_journal(journal)
            return
        journal.update(
            {
                "phase": "complete",
                "status": "failed",
                "result": {
                    "from": journal.get("outgoing"),
                    "to": journal.get("incoming"),
                    "revived": revival["revived"],
                    "failed": revival["failed"],
                    "failed_reasons": reasons,
                    "failed_run_ids": revival["failed_run_ids"],
                    "revived_run_ids": revival["revived_run_ids"],
                },
                "error": detail,
            }
        )
        self._write_rotation_journal(journal)

    @staticmethod
    def _restore_previous_account(journal: dict[str, Any]) -> None:
        outgoing = journal.get("outgoing")
        if isinstance(outgoing, str) and outgoing:
            if not accounts.install_incoming_auth(outgoing):
                raise accounts.RotationError(
                    f"outgoing auth.json missing for account {outgoing!r}"
                )
        previous = journal.get("previous_state")
        if not isinstance(previous, dict):
            raise accounts.RotationError("rotation journal lost previous account state")
        accounts.write_state(accounts.AccountState.from_dict(previous))

    @staticmethod
    def _ensure_committed_account(journal: dict[str, Any]) -> None:
        incoming = journal.get("incoming")
        if not isinstance(incoming, str) or not accounts.install_incoming_auth(incoming):
            raise accounts.RotationError("committed incoming auth.json is missing")
        if not accounts.codex_login_status():
            raise accounts.RotationError("committed Codex login verification failed")
        committed = journal.get("committed_state")
        if not isinstance(committed, dict):
            raise accounts.RotationError("rotation journal lost committed account state")
        accounts.write_state(accounts.AccountState.from_dict(committed))

    async def _resume_rotation_runs_locked(
        self,
        journal: dict[str, Any],
    ) -> dict[str, Any]:
        revived: list[str] = []
        revived_run_ids: dict[str, str] = {}
        failed: list[str] = []
        failed_reasons: dict[str, str] = {}
        failed_run_ids: dict[str, str] = {}
        pending = False
        operation_id = str(journal["operation_id"])
        for row in journal["runs"]:
            if row.get("resumed") or row.get("skipped"):
                if row.get("resumed"):
                    revived.append(row["agent_id"])
                    revived_run_ids[row["agent_id"]] = row["run_id"]
                continue
            run_id = row["run_id"]
            agent_id = row["agent_id"]
            async with self._run_lock(run_id):
                try:
                    record = self.store.get(run_id)
                    if not self.store.is_current(record) or record.replaced_by_run_id:
                        if record.quiesce_operation_id == operation_id:
                            self.store.abandon_quiesce_marker(run_id, operation_id)
                        row["skipped"] = True
                        continue
                    if record.state in {
                        LifecycleState.DEAD,
                        LifecycleState.COMPLETED,
                    }:
                        if record.quiesce_operation_id == operation_id:
                            self.store.abandon_quiesce_marker(run_id, operation_id)
                        row["skipped"] = True
                        continue
                    if self.adapters.get(run_id) is not None:
                        if record.quiesce_operation_id == operation_id:
                            self.store.clear_quiesce_marker(run_id, operation_id)
                        elif record.quiesce_operation_id is not None:
                            raise StoreConflict(
                                "run belongs to another quiesce operation"
                            )
                        row["resumed"] = True
                        revived.append(agent_id)
                        revived_run_ids[agent_id] = run_id
                        continue
                    if self.pid_alive(record.provider_pid):
                        pending = True
                        continue
                    await self._resume_run_without_admission(run_id, automatic=False)
                    row["resumed"] = True
                    row["failed_reason"] = None
                    revived.append(agent_id)
                    revived_run_ids[agent_id] = run_id
                except Exception as exc:
                    reason = str(exc)
                    row["failed_reason"] = reason
                    failed.append(agent_id)
                    failed_reasons[agent_id] = reason
                    failed_run_ids[agent_id] = run_id
                finally:
                    self._write_rotation_journal(journal)
        return {
            "revived": revived,
            "failed": failed,
            "failed_reasons": failed_reasons,
            # Per-ticket run_id lets the notice store reconcile a rotation
            # failure notice against the live registry: a replaced ticket
            # gets a new run_id and its notice drops on the next refresh.
            "failed_run_ids": failed_run_ids,
            "revived_run_ids": revived_run_ids,
            "pending": pending,
        }

    async def _recover_codex_rotation_locked(self) -> bool:
        journal = self.store.read_codex_rotation_journal()
        if not journal or journal.get("phase") == "complete":
            return True
        phase = str(journal.get("phase") or "")
        try:
            if phase in {
                "prepared",
                "quiescing",
                "quiesced",
            }:
                journal["phase"] = "rolling-back"
            elif phase in {"installing", "rolling-back"}:
                if journal.get("rollback_requires_auth_restore"):
                    await asyncio.to_thread(self._restore_previous_account, journal)
                journal["phase"] = "rolling-back"
            elif phase in {"committed", "resuming"}:
                await asyncio.to_thread(self._ensure_committed_account, journal)
                journal["phase"] = "resuming"
            else:
                raise accounts.RotationError(
                    f"unknown Codex rotation journal phase: {phase!r}"
                )
            self._write_rotation_journal(journal)
            revival = await self._resume_rotation_runs_locked(journal)
            if revival["pending"]:
                return False
            succeeded = phase in {"committed", "resuming"}
            result = {
                "from": journal.get("outgoing"),
                "to": journal.get("incoming"),
                "revived": revival["revived"],
                "failed": revival["failed"],
                "failed_reasons": revival["failed_reasons"],
                "failed_run_ids": revival["failed_run_ids"],
                "revived_run_ids": revival["revived_run_ids"],
            }
            journal.update(
                {
                    "phase": "complete",
                    "status": "succeeded" if succeeded else "failed",
                    "result": result,
                    "error": None if succeeded else "recovered by rolling back auth",
                }
            )
            self._write_rotation_journal(journal)
            return True
        except Exception as exc:
            journal["error"] = f"rotation recovery failed: {exc}"
            self._write_rotation_journal(journal)
            return False

    async def _recover_once(self) -> list[dict[str, str]]:
        async with self.codex_fleet_lock:
            await self._recover_codex_rotation_locked()
        results: list[dict[str, str]] = []
        for snapshot in self.store.list_runs():
            if snapshot.provider is ProviderKind.CODEX:
                async with self.codex_fleet_lock:
                    async with self._agent_lock(snapshot.agent_id):
                        results.append(await self._recover_run(snapshot.run_id))
                continue
            async with self._agent_lock(snapshot.agent_id):
                results.append(await self._recover_run(snapshot.run_id))
        return results

    async def _handover_preflight(self) -> list[str]:
        """Validate every current provider before any adapter is drained."""

        run_ids: list[str] = []
        for snapshot in self.store.list_runs():
            if (
                snapshot.state in TERMINAL_STATES
                or snapshot.replaced_by_run_id
                or not self.store.is_current(snapshot)
            ):
                continue
            try:
                lock = self._run_lock(snapshot.run_id)
            except RunNotFound:
                continue
            async with lock:
                try:
                    record = self.store.get(snapshot.run_id)
                except RunNotFound:
                    continue
                if (
                    record.state in TERMINAL_STATES
                    or record.replaced_by_run_id
                    or not self.store.is_current(record)
                ):
                    continue
                runtime = self._runtime_status(record)
                provider_alive = bool(runtime["provider_alive"]) or self.pid_alive(
                    record.provider_pid
                )
                if provider_alive and not runtime["control_attached"]:
                    raise StoreConflict(
                        f"cannot hand over {record.agent_id}: "
                        "live provider control is detached"
                    )
                if not runtime["control_attached"] and not provider_alive:
                    continue
                if runtime.get("state") not in {
                    LifecycleState.WORKING.value,
                    LifecycleState.WAITING_APPROVAL.value,
                    LifecycleState.IDLE.value,
                }:
                    raise StoreConflict(
                        f"cannot hand over {record.agent_id}: state "
                        f"{runtime.get('state') or 'unknown'} is not resumable"
                    )
                if not isinstance(runtime.get("provider_session_id"), str) or not runtime.get(
                    "provider_session_id"
                ):
                    raise StoreConflict(
                        f"cannot hand over {record.agent_id}: provider session id is missing"
                    )
                run_ids.append(record.run_id)
        return run_ids

    @staticmethod
    def _classify_handover_finalization(
        captured_state: LifecycleState,
        events: list[tuple[ProviderAdapter, ProviderEvent]],
        captured_pending_requests: dict[str, dict[str, Any]],
        drained_pending_requests: dict[str, dict[str, Any]],
    ) -> tuple[LifecycleState, dict[str, dict[str, Any]]]:
        """Apply the complete captured-state and stop-event matrix."""

        outcome = _HandoverDrainOutcome.NONE
        for _adapter, event in events:
            normalized = normalize_provider_event(
                event.provider,
                event.payload,
                direction=event.direction,
            )
            if normalized.kind in {"approval_response", "approval_resolved"}:
                outcome = _HandoverDrainOutcome.USER_RESPONSE
                continue
            if event.payload.get("type") == "control_cancel_request":
                outcome = _HandoverDrainOutcome.CONTROL_CANCEL_REQUEST
                continue
            if normalized.lifecycle_state is LifecycleState.WAITING_APPROVAL:
                outcome = _HandoverDrainOutcome.NEW_APPROVAL
                continue
            if normalized.lifecycle_state is LifecycleState.INTERRUPTED:
                outcome = _HandoverDrainOutcome.SUPERVISOR_INTERRUPTED
                continue
            if normalized.lifecycle_state is LifecycleState.IDLE:
                outcome = _HandoverDrainOutcome.NATURAL_COMPLETED
                continue
            if normalized.lifecycle_state in {
                LifecycleState.COMPLETED,
                LifecycleState.BLOCKED,
            }:
                raise StoreConflict(
                    "provider produced non-resumable handover state: "
                    f"{normalized.lifecycle_state.value}"
                )

        try:
            final_state, pending_disposition = _HANDOVER_FINALIZATION_MATRIX[
                (captured_state, outcome)
            ]
        except KeyError as exc:
            raise StoreConflict(
                f"unsupported handover finalization: {captured_state.value}/{outcome.value}"
            ) from exc
        if pending_disposition is _HandoverPendingDisposition.CLEAR:
            pending_requests: dict[str, dict[str, Any]] = {}
        elif pending_disposition is _HandoverPendingDisposition.MERGE_CAPTURED_AND_DRAINED:
            pending_requests = {
                **captured_pending_requests,
                **drained_pending_requests,
            }
        else:
            pending_requests = dict(captured_pending_requests)
        return final_state, pending_requests

    async def prepare_handover(self, _run_ids: list[str] | None = None) -> dict[str, Any]:
        """Block admission, validate providers, then drain that exact set."""

        async with self.handover_condition:
            while self.handover_pending:
                await self.handover_condition.wait()
            if self.handover_active:
                if self.handover_result is not None:
                    return {
                        "drained_run_ids": list(
                            self.handover_result["drained_run_ids"]
                        ),
                        "runs": [dict(run) for run in self.handover_result["runs"]],
                    }
                raise StoreConflict("supervisor handover is already in progress")
            self.handover_pending = True
            try:
                while self.active_run_mutations:
                    await self.handover_condition.wait()
                self.handover_pending = False
                self.handover_active = True
                self.handover_condition.notify_all()
            except BaseException:
                self.handover_pending = False
                self.handover_condition.notify_all()
                raise
        try:
            run_ids = await self._handover_preflight()
            drained: list[str] = []
            runs: list[dict[str, Any]] = []
            for run_id in run_ids:
                async with self._run_lock(run_id):
                    await self._flush_handover_events(run_id)
                    record = self.store.get(run_id)
                    runtime = self._runtime_status(record)
                    if (
                        record.state in TERMINAL_STATES
                        or record.replaced_by_run_id
                        or not self.store.is_current(record)
                    ):
                        raise StoreConflict(
                            f"handover target changed before detach: {run_id}"
                        )
                    if runtime.get("state") not in {
                        LifecycleState.WORKING.value,
                        LifecycleState.WAITING_APPROVAL.value,
                        LifecycleState.IDLE.value,
                    }:
                        raise StoreConflict(
                            f"cannot hand over {record.agent_id}: state "
                            f"{runtime.get('state') or 'unknown'} is not resumable"
                        )
                    provider_session_id = runtime.get("provider_session_id")
                    if not isinstance(provider_session_id, str) or not provider_session_id:
                        raise StoreConflict(
                            f"cannot hand over {record.agent_id}: provider session id is missing"
                        )
                    captured_state = record.state
                    if captured_state not in {
                        LifecycleState.WORKING,
                        LifecycleState.WAITING_APPROVAL,
                        LifecycleState.IDLE,
                    }:
                        raise StoreConflict(
                            f"cannot hand over {record.agent_id}: durable state "
                            f"{captured_state.value} is not resumable"
                        )
                    captured_pending_requests = deepcopy(record.pending_requests)
                    captured_generation = record.provider_generation
                    captured_transcript_path = record.transcript_path
                    adapter = self.adapters.get(run_id)
                    if adapter is None:
                        raise StoreConflict(
                            f"provider control detached during handover: {run_id}"
                        )
                    await self._quiesce_adapter_for_replacement(run_id, adapter)
                    async with self.handover_condition:
                        stop_events = list(self.handover_event_queue.get(run_id, []))
                    await self._flush_handover_events(run_id)
                    post_stop = self.store.get(run_id)
                    final_state, final_pending_requests = (
                        self._classify_handover_finalization(
                            captured_state,
                            stop_events,
                            captured_pending_requests,
                            post_stop.pending_requests,
                        )
                    )
                    current = self.store.finalize_handover_detach(
                        run_id,
                        state=final_state,
                        session_id=provider_session_id,
                        generation=captured_generation,
                        transcript_path=captured_transcript_path,
                        pending_requests=final_pending_requests,
                    )
                    handover_run = self._runtime_status(self.store.get(run_id))
                    handover_run.update(
                        {
                            "state": current.state.value,
                            "provider_session_id": provider_session_id,
                            "provider_pid": None,
                            "active_turn_id": None,
                            "control_attached": False,
                            "provider_alive": False,
                        }
                    )
                    runs.append(handover_run)
                    drained.append(run_id)
            await self._flush_all_handover_events()
            if self.handover_event_queue:
                raise StoreConflict(
                    "handover completed with unpersisted provider events"
                )
            self.handover_result = {"drained_run_ids": drained, "runs": runs}
            return {
                "drained_run_ids": list(drained),
                "runs": [dict(run) for run in runs],
            }
        except BaseException:
            capture_error: BaseException | None = None
            try:
                await self._capture_live_handover_events()
            except BaseException as exc:
                capture_error = exc
            async with self.handover_condition:
                self.handover_active = False
                self.handover_pending = False
                self.handover_result = None
                self.handover_condition.notify_all()
            try:
                await self._flush_all_handover_events(
                    schedule_monitor_actions=True
                )
            except BaseException:
                raise
            if capture_error is not None:
                raise capture_error
            raise

    async def _recover_run(self, run_id: str) -> dict[str, str]:
        # Decision inputs are refreshed per run; no stale list snapshot can
        # override a replacement that happened while recovery was running.
        record = self.store.get(run_id)
        pipeline_failure = self.pipeline_failures.get(record.run_id)
        if pipeline_failure:
            return {
                "run_id": record.run_id,
                "action": RecoveryAction.BLOCK.value,
                "reason": pipeline_failure,
            }
        adapter = self.adapters.get(record.run_id)
        if adapter is not None:
            if record.state in {
                LifecycleState.DEAD,
                LifecycleState.COMPLETED,
            }:
                await self._detach_adapter(record.run_id)
                self._clear_auth_dead_recovery_state(record.run_id)
                await adapter.close()
                return {
                    "run_id": record.run_id,
                    "action": RecoveryAction.SKIP.value,
                    "reason": f"run is {record.state.value}",
                }
            if (
                record.automatic_resume_suppressed
                and record.automatic_resume_guarded_at
                and self._automatic_resume_is_stable(record)
            ):
                record = self.store.clear_automatic_resume_suppression(record.run_id)
            # The adapter's reader/event pump is the liveness monitor. Do
            # not issue thread/read or fsync unchanged state every second;
            # stream loss detaches this adapter and makes the next scan
            # apply the closed recovery table below.
            return {
                "run_id": record.run_id,
                "action": RecoveryAction.RETAIN.value,
                "reason": "provider control channel is attached",
            }
        if self._adapter_recently_detached(record.run_id):
            return {
                "run_id": record.run_id,
                "action": RecoveryAction.BLOCK.value,
                "reason": "provider control channel recently detached",
            }
        if record.start_transaction is not None:
            # An uncommitted start must never be resumed by general session
            # recovery. The scan-time abort skips a live-unverifiable PID; if
            # that PID exits between the scan and this call, retry the abort
            # now so command replay can start fresh. When abort still cannot
            # run — PID stays live-unverifiable — leave the start intent
            # retryable so the next scan can converge.
            if self.store.abort_uncommitted_start(record.run_id):
                return {
                    "run_id": record.run_id,
                    "action": RecoveryAction.SKIP.value,
                    "reason": "uncommitted start aborted",
                }
            return {
                "run_id": record.run_id,
                "action": RecoveryAction.BLOCK.value,
                "reason": "uncommitted start pending abort",
            }
        if record.automatic_resume_suppressed:
            return {
                "run_id": record.run_id,
                "action": RecoveryAction.BLOCK.value,
                "reason": record.state_reason or "automatic resume is suppressed",
            }
        decision = restart_recovery_decision(
            record,
            is_current=self.store.is_current(record),
            provider_pid_alive=self.pid_alive(record.provider_pid),
            provider_control_attached=record.run_id in self.adapters,
        )
        result = {
            "run_id": record.run_id,
            "action": decision.action.value,
            "reason": decision.reason,
        }
        if decision.action is RecoveryAction.RESUME:
            recovery_state = record.recovery_from_state or record.state
            try:
                await self._resume_run_without_admission(
                    record.run_id,
                    automatic=True,
                )
            except Exception as exc:
                try:
                    self.store.mark_automatic_resume_failed(
                        record.run_id,
                        reason=f"automatic resume failed: {exc}",
                        recovery_state=recovery_state,
                    )
                except ValueError:
                    pass
                result["action"] = RecoveryAction.BLOCK.value
                result["reason"] = f"automatic resume failed: {exc}"
        elif decision.action is RecoveryAction.BLOCK:
            try:
                if decision.retryable:
                    self.store.mark_recovery_blocked(
                        record.run_id,
                        reason=decision.reason,
                    )
                else:
                    self.store.transition(
                        record.run_id,
                        LifecycleState.BLOCKED,
                        reason=decision.reason,
                    )
            except ValueError:
                pass
        return result

    def _resolve_run_id(self, params: dict[str, Any]) -> str:
        run_id = params.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
        agent_id = params.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            current = self.store.current_run_id(agent_id)
            if current:
                return current
        raise RunNotFound("no current run")

    def _record_launched_replacement_effect(
        self, effect_id: str, record: RunRecord
    ) -> None:
        """Persist a provider_completed checkpoint before marking completed.

        The fresh and cross-provider replacement paths call _launch_record and
        then mark the effect completed. A crash between those two writes would
        leave the effect at "published" while the replacement provider is
        durably started; recovery must promote such effects rather than kill
        the running replacement.
        """

        self.store.command_log.update_replace_effect(
            "run/replace",
            effect_id,
            "provider_completed",
            {
                "state": record.state.value,
                "session_id": record.provider_session_id,
                "pid": record.provider_pid,
                "generation": record.provider_generation,
                "active_turn_id": record.active_turn_id,
                "transcript_path": record.transcript_path,
                "detail": record.state_reason,
            },
        )

    async def _require_replacement_control(self, run_id: str) -> RunRecord:
        """Require live replacement state and attached provider control."""

        try:
            record = self.store.get(run_id)
        except RunNotFound:
            raise
        adapter = self.adapters.get(run_id)
        if adapter is None or record.state is LifecycleState.BLOCKED:
            raise CommandRetryable("replacement provider control is not attached")
        try:
            status = await adapter.status()
        except Exception as exc:
            raise CommandRetryable("replacement provider control is unavailable") from exc
        if status.state is LifecycleState.BLOCKED:
            raise CommandRetryable("replacement provider is blocked")
        record = self.store.update_adapter_status(run_id, status)
        if record.state is LifecycleState.BLOCKED:
            raise CommandRetryable("replacement provider state is blocked")
        return record

    async def send_now(
        self,
        run_id: str,
        message: str,
        pending_id: str | None = None,
        dedupe_key: str | None = None,
        source: str | None = None,
        effect_id: str | None = None,
        command_hash: str | None = None,
        retryable_if_detached: bool = False,
    ) -> dict[str, Any]:
        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                return await self._send_now(
                    run_id,
                    message,
                    pending_id,
                    dedupe_key,
                    source,
                    effect_id,
                    command_hash,
                    retryable_if_detached,
                )

    async def _send_now(
        self,
        run_id: str,
        message: str,
        pending_id: str | None = None,
        dedupe_key: str | None = None,
        source: str | None = None,
        effect_id: str | None = None,
        command_hash: str | None = None,
        retryable_if_detached: bool = False,
    ) -> dict[str, Any]:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            if retryable_if_detached:
                raise CommandRetryable("provider control is not attached")
            raise StoreConflict("run has no attached provider adapter")
        expose_pending_id = pending_id is not None or source is not None
        status = await adapter.status()
        if status.state is LifecycleState.IDLE:
            run_id, adapter, _ = await self._apply_desired_model_locked(
                run_id,
                adapter,
                trigger="send-now",
            )
            if adapter is None:
                raise StoreConflict("run has no attached provider adapter")
        if effect_id is not None and pending_id is None:
            pending_id = str(uuid4())
        steer_effect = None
        if effect_id is not None:
            steer_effect = self.store.command_log.steer_effect(
                method="run/send_now",
                request_id=effect_id,
                agent_id=self.store.get(run_id).agent_id,
                command_hash=command_hash or "",
                run_id=run_id,
                pending_id=pending_id,
                message=message,
                mode="now",
            )
            pending_id = str(steer_effect["pending_id"])
            if steer_effect["status"] in {"sent", "acknowledged"}:
                result = steer_effect.get("result")
                if isinstance(result, dict):
                    return result
                return {"status": "sent", "pending_id": pending_id}
            if steer_effect["status"] == "sending":
                if self.store.steer_delivery_observed(run_id, pending_id):
                    result = {"status": "sent", "pending_id": pending_id}
                    self.store.command_log.update_steer_effect(
                        "run/send_now", effect_id, "acknowledged", result
                    )
                    return result
                # The provider may have accepted the message before the
                # supervisor stopped. Do not resend without durable evidence.
                return {
                    "status": "uncertain",
                    "pending_id": pending_id,
                }
            record = self.store.get(run_id)
            if any(
                item.get("pending_id") == pending_id
                for item in record.composer_messages
            ):
                result = {"status": "sent", "pending_id": pending_id}
                self.store.command_log.update_steer_effect(
                    "run/send_now", effect_id, "acknowledged", result
                )
                return result
        if dedupe_key is not None:
            # Bind the dedupe claim to the steer effect so replay after a
            # crash between the claim and provider delivery can resume
            # (WIKI-232). The owner is method-scoped because the command log
            # permits the same request_id once per method (send_now +
            # send_on_idle); an unscoped owner would let a cross-method
            # collision reclaim its sibling's key. Non-command paths
            # (effect_id is None) keep the legacy owner-less behavior and
            # remain single-shot.
            owner = f"run/send_now:{effect_id}" if effect_id is not None else None
            _, claimed = self.store.claim_message_dedupe_key(
                run_id, dedupe_key, owner=owner
            )
            if not claimed:
                return {"status": "deduplicated", "dedupe_key": dedupe_key}
        if pending_id is None and source is not None:
            # Source metadata is only propagated through pending_user_messages,
            # so mint a durable id for synthetic sources without a composer id.
            pending_id = str(uuid4())
        # Cleanup obligation is established BEFORE the tracker runs. A partial
        # tracker success (atomic os.replace committed but a follow-up chmod
        # or dir fsync raises) would otherwise leak a durable pending row
        # while releasing the dedupe key — a retry would then land a second
        # pending row for the same pending_id, and the stale first row could
        # consume the retry's provider echo.
        accepted = False
        try:
            if pending_id is not None:
                try:
                    self.store.track_pending_user_message(
                        run_id, pending_id, message, source=source
                    )
                except Exception:
                    # Even a raise here may have persisted a partial row —
                    # unconditionally discard by pending_id before rethrowing.
                    self.store.discard_pending_user_message(run_id, pending_id)
                    raise
            if effect_id is not None:
                self.store.command_log.update_steer_effect(
                    "run/send_now", effect_id, "sending"
                )
            status = await adapter.send_now(message)
            accepted = True
        except Exception:
            if dedupe_key is not None and not accepted:
                self.store.release_message_dedupe_key(run_id, dedupe_key)
            if pending_id is not None and not accepted:
                self.store.discard_pending_user_message(run_id, pending_id)
            raise
        if effect_id is not None:
            self.store.command_log.update_steer_effect(
                "run/send_now", effect_id, "sent"
            )
        record = self.store.update_adapter_status(run_id, status)
        record = self.store.clear_automatic_resume_suppression(run_id)
        await self._publish_agent_change(record.agent_id)
        response: dict[str, Any] = {"status": "sent"}
        if pending_id is not None and expose_pending_id:
            response["pending_id"] = pending_id
        if dedupe_key is not None:
            response["dedupe_key"] = dedupe_key
        if effect_id is not None:
            self.store.command_log.update_steer_effect(
                "run/send_now", effect_id, "sent", response
            )
        return response

    async def send_on_idle(
        self,
        run_id: str,
        message: str,
        pending_id: str | None = None,
        dedupe_key: str | None = None,
        source: str | None = None,
        effect_id: str | None = None,
        command_hash: str | None = None,
        retryable_if_detached: bool = False,
    ) -> dict[str, Any]:
        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                return await self._send_on_idle(
                    run_id,
                    message,
                    pending_id,
                    dedupe_key,
                    source,
                    effect_id,
                    command_hash,
                    retryable_if_detached,
                )

    async def _send_on_idle(
        self,
        run_id: str,
        message: str,
        pending_id: str | None = None,
        dedupe_key: str | None = None,
        source: str | None = None,
        effect_id: str | None = None,
        command_hash: str | None = None,
        retryable_if_detached: bool = False,
    ) -> dict[str, Any]:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            if retryable_if_detached:
                raise CommandRetryable("provider control is not attached")
            raise StoreConflict("run has no attached provider adapter")
        if effect_id is not None and pending_id is None:
            pending_id = str(uuid4())
        steer_effect = None
        if effect_id is not None:
            steer_effect = self.store.command_log.steer_effect(
                method="run/send_on_idle",
                request_id=effect_id,
                agent_id=self.store.get(run_id).agent_id,
                command_hash=command_hash or "",
                run_id=run_id,
                pending_id=pending_id,
                message=message,
                mode="on-idle",
            )
            pending_id = str(steer_effect["pending_id"])
            if steer_effect["status"] in {"sent", "acknowledged"}:
                self.store.remove_queued_message_by_pending_id(run_id, pending_id)
                result = steer_effect.get("result")
                if isinstance(result, dict):
                    return result
                return {"status": "sent", "pending_id": pending_id}
            if steer_effect["status"] == "sending":
                if self.store.steer_delivery_observed(run_id, pending_id):
                    result = {"status": "sent", "pending_id": pending_id}
                    self.store.command_log.update_steer_effect(
                        "run/send_on_idle", effect_id, "acknowledged", result
                    )
                    self.store.remove_queued_message_by_pending_id(run_id, pending_id)
                    return result
                return {"status": "uncertain", "pending_id": pending_id}
        if dedupe_key is not None:
            # Method-scope the owner: see the twin comment in _send_now.
            owner = (
                f"run/send_on_idle:{effect_id}" if effect_id is not None else None
            )
            record, claimed = self.store.claim_message_dedupe_key(
                run_id, dedupe_key, owner=owner
            )
            if not claimed:
                return {
                    "status": "deduplicated",
                    "dedupe_key": dedupe_key,
                    "messages": list(record.queued_messages),
                }
        if pending_id is None and source is not None:
            pending_id = str(uuid4())
        try:
            record = self.store.get(run_id)
            existing = next(
                (
                    item
                    for item in record.queued_messages
                    if item.get("pending_id") == pending_id
                ),
                None,
            )
            if existing is None:
                record = self.store.queue_message(
                    run_id, message, pending_id, source=source
                )
        except Exception:
            if dedupe_key is not None:
                self.store.release_message_dedupe_key(run_id, dedupe_key)
            raise
        response = {
            "status": "queued",
            "position": len(record.queued_messages),
            "messages": list(record.queued_messages),
        }
        if dedupe_key is not None:
            response["dedupe_key"] = dedupe_key
        await self._publish(
            {"type": "session", "ticket": record.agent_id, "surface": "queue"}
        )
        status = await adapter.status()
        if status.state is LifecycleState.IDLE:
            await self._deliver_next_queued_locked(run_id, adapter)
            remaining = self.store.queued_messages(run_id)
            if pending_id is not None and not any(
                item.get("pending_id") == pending_id for item in remaining
            ):
                return {
                    "status": "sent",
                    "pending_id": pending_id,
                    **({"dedupe_key": dedupe_key} if dedupe_key is not None else {}),
                }
        if effect_id is not None:
            self.store.command_log.update_steer_effect(
                "run/send_on_idle", effect_id, "queued", response
            )
        return response

    async def delete_queued(self, run_id: str, index: int) -> dict[str, Any]:
        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                record = self.store.delete_queued_message(run_id, index)
                await self._publish(
                    {
                        "type": "session",
                        "ticket": record.agent_id,
                        "surface": "queue",
                    }
                )
                return {"messages": list(record.queued_messages)}

    async def interrupt(self, run_id: str) -> RunRecord:
        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                return await self._interrupt(run_id)

    async def _interrupt(self, run_id: str) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
        status = await adapter.interrupt()
        record = self.store.update_adapter_status(run_id, status)
        record = self.store.clear_automatic_resume_suppression(run_id)
        await self._publish_agent_change(record.agent_id)
        return record

    async def stop(self, run_id: str) -> RunRecord:
        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                return await self._stop(run_id)

    async def _stop(self, run_id: str) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            record = self.store.get(run_id)
            if record.state not in {LifecycleState.DEAD, LifecycleState.COMPLETED}:
                if self.pid_alive(record.provider_pid):
                    raise StoreConflict(
                        "provider PID is live without attached control; refusing false stop"
                    )
                record = self.store.transition(
                    run_id, LifecycleState.DEAD, reason="stopped"
                )
            self._clear_auth_dead_recovery_state(run_id)
            return record
        try:
            status = await self._close_and_drain_adapter(
                run_id,
                adapter,
                finalize="stop",
                suppress_operation_errors=False,
            )
        except Exception as exc:
            await self._record_failed_terminal_control(
                run_id,
                LifecycleState.DEAD,
                f"provider stop failed: {exc}",
            )
            raise
        if status is None:
            raise StoreConflict("provider stop returned no status")
        record = self.store.update_adapter_status(run_id, status)
        self._clear_auth_dead_recovery_state(run_id)
        await self._publish_agent_change(record.agent_id)
        return record

    async def archive(
        self,
        run_id: str,
        *,
        outcome: str | None = None,
        effect_id: str | None = None,
        command_hash: str | None = None,
        command_hash_payload: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        async with self._run_mutation_admission():
            async with self._run_lock(run_id):
                return await self._archive(
                    run_id,
                    outcome=outcome,
                    effect_id=effect_id,
                    command_hash=command_hash,
                    command_hash_payload=command_hash_payload,
                )

    async def _archive(
        self,
        run_id: str,
        *,
        outcome: str | None = None,
        effect_id: str | None = None,
        command_hash: str | None = None,
        command_hash_payload: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            record = self.store.get(run_id)
            if self.pid_alive(record.provider_pid):
                orphan = await self._orphaned_provider_process(record.provider_pid)
                if orphan is None:
                    raise StoreConflict(
                        "provider PID is live without attached control; refusing false archive"
                    )
                if not await self._terminate_orphan_provider_pid(orphan):
                    record = self.store.transition(
                        run_id,
                        record.state,
                        reason="orphan_kill_failed",
                        adapter_status=self._detached_terminal_status(
                            record,
                            record.state,
                            detail="orphan_kill_failed",
                        ),
                    )
                    await self._publish_agent_change(record.agent_id)
                    raise StoreConflict(
                        "orphan provider PID remained live after forced archive cleanup"
                    )
                target = (
                    record.state
                    if record.state in TERMINAL_STATES
                    else LifecycleState.COMPLETED
                )
                record = self.store.transition(
                    run_id,
                    target,
                    reason=record.state_reason if target in TERMINAL_STATES else "archived",
                    adapter_status=self._detached_terminal_status(
                        record,
                        target,
                        detail=record.state_reason
                        if target in TERMINAL_STATES
                        else "archived",
                    ),
                )
            elif record.state not in TERMINAL_STATES:
                record = self.store.transition(
                    run_id,
                    LifecycleState.COMPLETED,
                    reason="archived",
                )
            archived, _ = self.store.archive_current(run_id, outcome=outcome)
            if effect_id is not None:
                effect_payload = {
                    "agent_id": archived.agent_id,
                    "run_id": run_id,
                    "outcome": outcome,
                    "request_id": effect_id,
                    "method": "run/archive",
                }
                if command_hash_payload is not None:
                    effect_payload["command_hash_payload"] = dict(
                        command_hash_payload
                    )
                self.store.command_log.complete_effect(
                    AgentCommand(
                        "run/archive",
                        archived.agent_id,
                        effect_id,
                        effect_payload,
                    ),
                    _public_run(archived),
                    command_hash=command_hash,
                )
            self._forget_implicit_idempotency_for_run(run_id)
            self._clear_adapter_loss(run_id)
            self._clear_auth_dead_recovery_state(run_id)
            await self._publish_agent_change(archived.agent_id)
            return archived
        try:
            status = await self._close_and_drain_adapter(
                run_id,
                adapter,
                finalize="archive",
                suppress_operation_errors=False,
            )
        except Exception as exc:
            await self._record_failed_terminal_control(
                run_id,
                LifecycleState.COMPLETED,
                f"provider archive failed: {exc}",
            )
            raise
        if status is None:
            raise StoreConflict("provider archive returned no status")
        record = self.store.update_adapter_status(run_id, status)
        archived, _ = self.store.archive_current(run_id, outcome=outcome)
        if effect_id is not None:
            effect_payload = {
                "agent_id": archived.agent_id,
                "run_id": run_id,
                "outcome": outcome,
                "request_id": effect_id,
                "method": "run/archive",
            }
            if command_hash_payload is not None:
                effect_payload["command_hash_payload"] = dict(command_hash_payload)
            self.store.command_log.complete_effect(
                AgentCommand(
                    "run/archive",
                    archived.agent_id,
                    effect_id,
                    effect_payload,
                ),
                _public_run(archived),
                command_hash=command_hash,
            )
        self._forget_implicit_idempotency_for_run(run_id)
        self._clear_adapter_loss(run_id)
        self._clear_auth_dead_recovery_state(run_id)
        await self._publish_agent_change(archived.agent_id)
        return archived

    async def _record_failed_terminal_control(
        self,
        run_id: str,
        target: LifecycleState,
        reason: str,
    ) -> None:
        """Keep explicit stop/archive intent non-resumable after control failure."""

        try:
            record = self.store.get(run_id)
            if (
                record.state
                not in {
                    LifecycleState.DEAD,
                    LifecycleState.COMPLETED,
                }
                or record.state is target
            ):
                record = self.store.transition(run_id, target, reason=reason)
            self._clear_auth_dead_recovery_state(run_id)
            await self._publish_agent_change(record.agent_id)
        except Exception:
            # Preserve the original provider/control error for the caller. A
            # concurrent terminal transition is already non-resumable.
            pass

    async def replace(
        self,
        run_id: str,
        prompt: str,
        model: str | None = None,
        provider: ProviderKind | None = None,
        effort: str | None = None,
        backend_base_url: str | None = None,
        replacement_run_id: str | None = None,
        effect_id: str | None = None,
        command_hash: str | None = None,
    ) -> RunRecord:
        async with self._run_mutation_admission():
            old = self.store.get(run_id)
            target_provider = provider or old.provider
            if ProviderKind.CODEX in {old.provider, target_provider}:
                self._assert_codex_fleet_available()
                async with self.codex_fleet_lock:
                    self._assert_codex_fleet_available()
                    async with self._run_lock(run_id):
                        replacement = await self._replace_without_admission(
                            run_id,
                            prompt,
                            model,
                            target_provider,
                            effort if provider is not None else old.effort,
                            backend_base_url,
                            replacement_run_id,
                            effect_id=effect_id,
                            command_hash=command_hash,
                        )
            else:
                async with self._run_lock(run_id):
                    replacement = await self._replace_without_admission(
                        run_id,
                        prompt,
                        model,
                        target_provider,
                        effort if provider is not None else old.effort,
                        backend_base_url,
                        replacement_run_id,
                        effect_id=effect_id,
                        command_hash=command_hash,
                    )
        if old.model != replacement.model:
            self._append_model_changed_event(
                replacement,
                old_model=old.model,
                new_model=replacement.model,
                trigger="replace",
            )
            await self._publish(
                {
                    "type": "model_changed",
                    "ticket": replacement.agent_id,
                    "from_model": old.model,
                    "to_model": replacement.model,
                    "run_id": replacement.run_id,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._publish(
                {
                    "type": "session",
                    "ticket": replacement.agent_id,
                    "surface": "session",
                }
            )
        return replacement

    async def _replace_without_admission(
        self,
        run_id: str,
        prompt: str,
        model: str | None = None,
        provider: ProviderKind | None = None,
        effort: str | None = None,
        backend_base_url: str | None = None,
        replacement_run_id: str | None = None,
        *,
        effect_id: str | None = None,
        command_hash: str | None = None,
    ) -> RunRecord:
        """Replace a run after the caller has acquired mutation admission."""

        return await self._replace_without_handover(
            run_id,
            prompt,
            model,
            provider,
            effort,
            backend_base_url,
            replacement_run_id,
            effect_id=effect_id,
            command_hash=command_hash,
        )

    async def _replace_without_handover(
        self,
        run_id: str,
        prompt: str,
        model: str | None = None,
        provider: ProviderKind | None = None,
        effort: str | None = None,
        backend_base_url: str | None = None,
        replacement_run_id: str | None = None,
        *,
        effect_id: str | None = None,
        command_hash: str | None = None,
    ) -> RunRecord:
        old = self.store.get(run_id)
        if not self.store.is_current(old):
            raise StoreConflict("replacement target is no longer current")
        resolve_safe_worktree(old.worktree)
        target_provider = provider or old.provider
        target_model = model or old.model
        target_effort = effort if target_provider is ProviderKind.CODEX else None
        replacement = RunRecord.new(
            agent_id=old.agent_id,
            provider=target_provider,
            role=old.role,
            model=target_model,
            worktree=old.worktree,
            prompt=prompt,
            effort=target_effort,
            orchestrator_id=old.orchestrator_id,
            replaces_run_id=old.run_id,
            backend_base_url=backend_base_url or old.backend_base_url,
            run_id=replacement_run_id,
        )
        prompt = inject_runtime_card(
            replacement,
            prompt,
            status_path=self.store.status_path(replacement.agent_id),
        )
        replacement.initial_prompt = prompt
        if effect_id is not None:
            self.store.command_log.begin_replace_effect(
                method="run/replace",
                request_id=effect_id,
                agent_id=old.agent_id,
                command_hash=command_hash or "",
                old_run_id=old.run_id,
                replacement_run_id=replacement.run_id,
            )
        old_adapter = self.adapters.get(run_id)
        if old_adapter is None:
            if self.pid_alive(old.provider_pid):
                orphan = await self._orphaned_provider_process(old.provider_pid)
                if orphan is None:
                    raise StoreConflict(
                        "replacement target has a live PID without attached control"
                    )
                if not await self._terminate_orphan_provider_pid(orphan):
                    raise StoreConflict(
                        "replacement target's orphan provider PID remained live"
                    )
                terminal_state = (
                    old.state if old.state in TERMINAL_STATES else LifecycleState.DEAD
                )
                old = self.store.transition(
                    run_id,
                    terminal_state,
                    reason="orphan stopped for replacement",
                    adapter_status=self._detached_terminal_status(
                        old,
                        terminal_state,
                        detail="orphan stopped for replacement",
                    ),
                )
            self._reset_status_for_replacement(old.agent_id)
            self.store.replace(old.run_id, replacement, reset_status=False)
            self._clear_auth_dead_recovery_state(old.run_id)
            if effect_id is not None:
                self.store.command_log.update_replace_effect(
                    "run/replace", effect_id, "published"
                )
            result = await self._launch_record(replacement, prompt)
            if effect_id is not None:
                self._record_launched_replacement_effect(effect_id, result)
                self.store.command_log.update_replace_effect(
                    "run/replace", effect_id, "completed", _public_run(result)
                )
            return result

        if target_provider is not old.provider:
            try:
                await self._close_and_drain_adapter(
                    run_id,
                    old_adapter,
                    finalize="stop",
                    suppress_operation_errors=False,
                )
            except asyncio.CancelledError:
                await self._cleanup_cancelled_replacement(
                    old,
                    replacement,
                    old_adapter,
                    published=False,
                )
                raise
            self._reset_status_for_replacement(old.agent_id)
            self.store.replace(old.run_id, replacement, reset_status=False)
            self._clear_auth_dead_recovery_state(old.run_id)
            if effect_id is not None:
                self.store.command_log.update_replace_effect(
                    "run/replace", effect_id, "published"
                )
            result = await self._launch_record(replacement, prompt)
            if effect_id is not None:
                self._record_launched_replacement_effect(effect_id, result)
                self.store.command_log.update_replace_effect(
                    "run/replace", effect_id, "completed", _public_run(result)
                )
            return result
        published = False
        try:
            await self._quiesce_adapter_for_replacement(run_id, old_adapter)
            self._reset_status_for_replacement(old.agent_id)
            old_adapter.prepare_replacement(replacement)
            self.store.replace(old.run_id, replacement, reset_status=False)
            self._clear_auth_dead_recovery_state(old.run_id)
            published = True
            if effect_id is not None:
                self.store.command_log.update_replace_effect(
                    "run/replace", effect_id, "published"
                )
        except asyncio.CancelledError:
            await self._cleanup_cancelled_replacement(
                old,
                replacement,
                old_adapter,
                published=published,
            )
            raise
        except Exception as exc:
            await self._close_and_drain_adapter(run_id, old_adapter)
            try:
                current = self.store.get(run_id)
                failure_state = (
                    LifecycleState.DEAD
                    if current.state is LifecycleState.DEAD
                    else LifecycleState.BLOCKED
                )
                self.store.transition(
                    run_id,
                    failure_state,
                    reason=f"replacement metadata commit failed: {exc}",
                    adapter_status=AdapterStatus(
                        state=failure_state,
                        session_id=old.provider_session_id,
                        pid=None,
                        generation=old.provider_generation,
                        active_turn_id=None,
                        transcript_path=old.transcript_path,
                    ),
                )
            except (ValueError, StoreConflict):
                pass
            self._clear_auth_dead_recovery_state(old.run_id)
            await self._publish_agent_change(old.agent_id)
            raise

        old_adapter.set_process_created_callback(
            lambda pid: self.store.record_provider_process_created(
                replacement.run_id, pid
            )
        )
        if effect_id is not None:
            self.store.command_log.update_replace_effect(
                "run/replace", effect_id, "provider_started"
            )
        try:
            status = await old_adapter.replace(prompt, model, target_effort)
        except asyncio.CancelledError:
            await self._cleanup_cancelled_replacement(
                old,
                replacement,
                old_adapter,
                published=True,
            )
            raise
        except Exception as exc:
            await self._close_and_drain_adapter(
                old.run_id,
                old_adapter,
                finalize="stop",
            )
            try:
                self.store.abort_replace(
                    old.run_id,
                    replacement.run_id,
                    reason=f"provider replacement failed: {exc}",
                    adapter_status=AdapterStatus(
                        state=LifecycleState.BLOCKED,
                        session_id=old.provider_session_id,
                        pid=None,
                        generation=old.provider_generation,
                        active_turn_id=None,
                        transcript_path=old.transcript_path,
                    ),
                )
            except Exception:
                pass
            self._clear_auth_dead_recovery_state(old.run_id)
            self._clear_auth_dead_recovery_state(replacement.run_id)
            await self._publish_agent_change(old.agent_id)
            if isinstance(exc, RuntimeError) and str(exc) == "adapter has not started":
                # Fixture and resumed transports can expose a session without
                # the local start request. Rebind through a fresh adapter.
                return await self._replace_without_handover(
                    old.run_id,
                    prompt,
                    model,
                    provider,
                    effort,
                    backend_base_url,
                    replacement_run_id,
                    effect_id=effect_id,
                    command_hash=command_hash,
                )
            raise
        if effect_id is not None:
            self.store.command_log.update_replace_effect(
                "run/replace",
                effect_id,
                "provider_completed",
                {
                    "state": status.state.value,
                    "session_id": status.session_id,
                    "pid": status.pid,
                    "generation": status.generation,
                    "active_turn_id": status.active_turn_id,
                    "transcript_path": status.transcript_path,
                    "detail": status.detail,
                },
            )
        replacement = self.store.update_adapter_status(replacement.run_id, status)
        self._route_adapter_generation(
            replacement.run_id, old_adapter, status.generation
        )
        self._attach_adapter(replacement.run_id, old_adapter)
        replacement = self.store.get(replacement.run_id)
        if effect_id is not None:
            self.store.command_log.update_replace_effect(
                "run/replace", effect_id, "completed", _public_run(replacement)
            )
        await self._publish_agent_change(replacement.agent_id)
        return replacement

    async def respond(
        self,
        run_id: str,
        request_id: str | int,
        response: dict[str, Any],
    ) -> RunRecord:
        # Provider questions can arrive before the long-running start/turn RPC
        # returns. Taking the agent operation lock here would deadlock that RPC.
        async with self._run_mutation_admission():
            return await self._respond(run_id, request_id, response)

    async def _respond(
        self,
        run_id: str,
        request_id: str | int,
        response: dict[str, Any],
    ) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
        status = await adapter.respond(request_id, response)
        if self.adapters.get(run_id) is not adapter:
            raise StoreConflict("provider adapter changed while sending response")
        record = self.store.update_adapter_status(run_id, status)
        record = self.store.clear_pending_request(run_id, request_id)
        if isinstance(request_id, str):
            record = self.store.clear_pending_request_by_tool_use_id(run_id, request_id)
        record = self.store.clear_automatic_resume_suppression(run_id)
        await self._publish_agent_change(record.agent_id)
        await self._publish(
            {"type": "session", "ticket": record.agent_id, "surface": "session"}
        )
        return record

    def _recovery_executor(self, command: AgentCommand) -> Callable[[], Any]:
        command_params = dict(command.payload)
        command_params["agent_id"] = command.agent_id
        command_params["request_id"] = command.request_id
        command_params["_recovery_replay"] = True

        async def execute() -> Any:
            return await self._dispatch(
                command.method,
                command_params,
                command_hash=command.command_hash,
            )

        return execute

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method in _COMMAND_METHODS:
            await self.command_queue.recover_pending()
            command_params = dict(params)
            request_id = _validated_idempotency_request_id(
                command_params.get("request_id")
            )
            if request_id is None:
                request_id = f"{method}:{uuid4()}"
                command_params["request_id"] = request_id
            if command_params.get("implicit_request_id") is True:
                self.implicit_idempotency_keys.add((method, request_id))
            if method == "run/start":
                agent_id = command_params.get("agent_id")
                if not isinstance(agent_id, str) or not agent_id:
                    raise ValueError("agent_id is required")
            else:
                agent_id = command_params.get("agent_id")
                if not isinstance(agent_id, str) or not agent_id:
                    run_id = command_params.get("run_id")
                    if not isinstance(run_id, str) or not run_id:
                        raise ValueError("agent_id or run_id is required")
                    agent_id = self.store.get(run_id).agent_id
                    command_params["agent_id"] = agent_id
                else:
                    current_run_id = self.store.current_run_id(agent_id)
                    if current_run_id:
                        command_params.setdefault("run_id", current_run_id)
            if method == "run/start":
                command_params.setdefault(
                    "run_id", str(uuid5(NAMESPACE_URL, f"{method}:{request_id}:run"))
                )
                if command_params.get("implicit_request_id") is True:
                    archived_start = self.store.find_archived_start_request(request_id)
                    if archived_start is not None:
                        command_params["run_id"] = str(uuid4())
            elif method == "run/replace":
                command_params.setdefault(
                    "replacement_run_id",
                    str(uuid5(NAMESPACE_URL, f"{method}:{request_id}:replacement")),
                )
            command = AgentCommand(
                method=method,
                agent_id=agent_id,
                request_id=request_id,
                payload={**command_params, "method": method},
            )
            # A committed start is its own durable receipt. Repair the sqlite
            # receipt before returning when the daemon died after commit_start.
            if method == "run/start":
                durable = self.store.find_start_request(request_id)
                if durable is not None:
                    if self.store.command_log.known(method, request_id):
                        await asyncio.to_thread(
                            self.store.command_log.append_intent,
                            command,
                            self.store.command_state_for(agent_id),
                        )
                    result = await self._dispatch(method, command_params)
                    if self.store.command_log.receipt(method, request_id) is None:
                        await asyncio.to_thread(
                            self.store.command_log.complete,
                            command,
                            result,
                            self.store.command_state(),
                        )
                    return result
            if method in _IDEMPOTENT_METHODS:
                async def execute() -> Any:
                    return await self._dispatch_idempotently(
                        method,
                        request_id,
                        command_params,
                        command_hash=command.command_hash,
                    )
            else:
                async def execute() -> Any:
                    return await self._dispatch(
                        method,
                        command_params,
                        command_hash=command.command_hash,
                    )
            return await self.command_queue.submit(command, execute)
        if method not in _IDEMPOTENT_METHODS:
            return await self._dispatch(method, params)
        request_id = _validated_idempotency_request_id(params.get("request_id"))
        if request_id is None:
            return await self._dispatch(method, params)
        if params.get("implicit_request_id") is True:
            self.implicit_idempotency_keys.add((method, request_id))
        return await self._dispatch_idempotently(method, request_id, params)

    def _forget_implicit_idempotency_for_run(self, run_id: str) -> None:
        for key, mapped_run_id in list(self.implicit_idempotency_runs.items()):
            if mapped_run_id != run_id:
                continue
            self.store.command_log.forget(key[0], key[1])
            self.implicit_idempotency_runs.pop(key, None)
            self.implicit_idempotency_keys.discard(key)
            self.idempotency_results.pop(key, None)

    def _complete_idempotency_task(
        self,
        key: tuple[str, str],
        task: asyncio.Task[Any],
    ) -> None:
        """Cache a completed operation even when every waiter was cancelled."""

        if self.idempotency_tasks.get(key) is not task:
            return
        self.idempotency_tasks.pop(key, None)
        if task.cancelled():
            return
        try:
            outcome: tuple[str, Any] = ("result", deepcopy(task.result()))
        except Exception as exc:
            if key in self.implicit_idempotency_keys:
                self.implicit_idempotency_keys.discard(key)
                return
            outcome = ("error", (type(exc), exc.args))
        self.idempotency_results[key] = outcome
        self.idempotency_results.move_to_end(key)
        if key in self.implicit_idempotency_keys:
            result = outcome[1]
            if isinstance(result, dict) and isinstance(result.get("run_id"), str):
                self.implicit_idempotency_runs[key] = result["run_id"]
        while len(self.idempotency_results) > self.idempotency_cache_size:
            self.idempotency_results.popitem(last=False)

    @staticmethod
    def _replay_idempotency_result(outcome: tuple[str, Any]) -> Any:
        kind, value = outcome
        if kind == "error":
            error_type, args = value
            raise error_type(*args)
        return deepcopy(value)

    async def _dispatch_idempotently(
        self,
        method: str,
        request_id: str,
        params: dict[str, Any],
        *,
        command_hash: str | None = None,
    ) -> Any:
        key = (method, request_id)
        async with self.idempotency_lock:
            if key in self.idempotency_results:
                self.idempotency_results.move_to_end(key)
                return self._replay_idempotency_result(self.idempotency_results[key])
            task = self.idempotency_tasks.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._dispatch(method, params, command_hash=command_hash),
                    name=f"agent-idempotency-{method}-{request_id}",
                )
                self.idempotency_tasks[key] = task
                task.add_done_callback(
                    lambda completed: self._complete_idempotency_task(key, completed)
                )
        return deepcopy(await asyncio.shield(task))

    async def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        *,
        command_hash: str | None = None,
    ) -> Any:
        if method == "ping":
            return {
                "status": "ok",
                "pid": os.getpid(),
                "runtime_fingerprint": RUNTIME_FINGERPRINT,
            }
        if method == "idempotency/status":
            target_method = params.get("method")
            if target_method not in _COMMAND_METHODS:
                raise ValueError("method must be a command supervisor operation")
            request_id = _validated_idempotency_request_id(params.get("request_id"))
            if request_id is None:
                raise ValueError("request_id is required")
            key = (target_method, request_id)
            receipt = self.store.command_log.receipt(target_method, request_id)
            requested_agent = params.get("agent_id")
            if requested_agent is not None and not isinstance(requested_agent, str):
                raise ValueError("agent_id must be a string")
            requested_hash = params.get("command_hash")
            if requested_hash is not None and not isinstance(requested_hash, str):
                raise ValueError("command_hash must be a string")
            if receipt is not None:
                if (
                    requested_agent is not None
                    and str(receipt.agent_id).upper() != requested_agent.upper()
                ):
                    raise CommandConflict(
                        f"request_id {request_id} belongs to another agent"
                    )
                if requested_hash is not None and receipt.command_hash != requested_hash:
                    raise CommandConflict(
                        f"request_id {request_id} was used with a different payload"
                    )
            if receipt is not None and not receipt.ok:
                self.store.command_log.raise_receipt(target_method, request_id)
            response: dict[str, Any] = {
                "known": (
                    key in self.idempotency_results
                    or key in self.idempotency_tasks
                    or self.store.command_log.known(target_method, request_id)
                    or self.store.find_start_request(request_id) is not None
                ),
            }
            if receipt is not None:
                response["receipt"] = {
                    "ok": receipt.ok,
                    "result": receipt.result,
                    "error_type": receipt.error_type,
                    "agent_id": receipt.agent_id,
                    "command_hash": receipt.command_hash,
                }
            return response
        if method == "run/start":
            migrate_legacy = params.get("migrate_legacy", False)
            if not isinstance(migrate_legacy, bool):
                raise ValueError("migrate_legacy must be a boolean")
            requested_run_id = params.get("run_id")
            current_run_id = self.store.current_run_id(str(params["agent_id"]))
            if requested_run_id is not None and current_run_id == requested_run_id:
                current = self.store.get(current_run_id)
                durable = self.store.find_start_request(
                    str(params.get("request_id") or "")
                )
                if current.start_transaction is not None or durable is None:
                    raise CommandRetryable(
                        "matching run start is not durably committed"
                    )
                return _public_run(self.store.get(current_run_id))
            record = await self.start_run(
                agent_id=str(params["agent_id"]),
                provider=ProviderKind(params["provider"]),
                role=str(params["role"]),
                model=str(params["model"]),
                worktree=str(params["worktree"]),
                prompt=str(params["prompt"]),
                effort=params.get("effort"),
                orchestrator_id=params.get("orchestrator_id"),
                migrate_legacy=migrate_legacy,
                backend_base_url=params.get("backend_base_url"),
                request_id=params.get("request_id"),
                run_id=params.get("run_id"),
                implicit_request_id=params.get("implicit_request_id") is True,
            )
            result = _public_run(record)
            active_worker_count = self._active_worker_count()
            if record.role != "orchestrator" and active_worker_count >= self.worker_soft_cap:
                result["warning"] = (
                    f"{active_worker_count} active workers; soft cap {self.worker_soft_cap} "
                    "— expect provider timeouts under load"
                )
            return result
        if method == "run/list":
            return {
                "status": "ok",
                "pid": os.getpid(),
                "runs": [
                    self._runtime_status(record) for record in self.store.list_runs()
                ],
            }
        if method == "fleet/rotate_codex":
            account = params.get("account")
            if account is not None and not isinstance(account, str):
                raise ValueError("account must be a string or null")
            return await self.request_codex_rotation(
                operation_id=str(params.get("operation_id") or ""),
                force_target=account,
            )
        if method == "run/status":
            return self._runtime_status(self.store.get(self._resolve_run_id(params)))
        if method == "run/resume":
            return _public_run(await self.resume_run(self._resolve_run_id(params)))
        if method == "run/send_now":
            return await self.send_now(
                self._resolve_run_id(params),
                str(params["text"]),
                _validated_pending_id(params.get("pending_id")),
                _validated_dedupe_key(params.get("dedupe_key")),
                _validated_source(params.get("source")),
                effect_id=params.get("request_id"),
                command_hash=command_hash,
                retryable_if_detached=bool(params.get("_recovery_replay")),
            )
        if method == "run/send_on_idle":
            return await self.send_on_idle(
                self._resolve_run_id(params),
                str(params["text"]),
                _validated_pending_id(params.get("pending_id")),
                _validated_dedupe_key(params.get("dedupe_key")),
                _validated_source(params.get("source")),
                effect_id=params.get("request_id"),
                command_hash=command_hash,
                retryable_if_detached=bool(params.get("_recovery_replay")),
            )
        if method == "run/queue":
            run_id = self._resolve_run_id(params)
            return {"messages": self.store.queued_messages(run_id)}
        if method == "run/queue/delete":
            index = params.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                raise ValueError("queue index must be an integer")
            return await self.delete_queued(self._resolve_run_id(params), index)
        if method == "run/queue_model_change":
            model = params.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model must be a non-empty string")
            return await self.queue_model_change(
                self._resolve_run_id(params),
                model.strip(),
            )
        if method == "run/cancel_model_change":
            return await self.cancel_model_change(self._resolve_run_id(params))
        if method == "run/interrupt":
            return _public_run(await self.interrupt(self._resolve_run_id(params)))
        if method == "run/stop":
            return _public_run(await self.stop(self._resolve_run_id(params)))
        if method == "run/archive":
            outcome = params.get("outcome")
            if outcome is not None and not isinstance(outcome, str):
                raise ValueError("outcome must be a string or null")
            request_id = params.get("request_id")
            if isinstance(request_id, str):
                effect = self.store.command_log.effect_result(
                    method,
                    request_id,
                    agent_id=str(params["agent_id"]),
                    command_hash=command_hash,
                )
                if isinstance(effect, dict):
                    return effect
            run_id = self._resolve_run_id(params)
            archived = self.store.finalize_archived_run(run_id)
            if archived is not None:
                return _public_run(archived)
            return _public_run(
                await self.archive(
                    run_id,
                    outcome=outcome,
                    effect_id=request_id if isinstance(request_id, str) else None,
                    command_hash=command_hash,
                    command_hash_payload=(
                        params.get("command_hash_payload")
                        if isinstance(params.get("command_hash_payload"), Mapping)
                        else None
                    ),
                )
            )
        if method == "run/replace":
            provider = params.get("provider")
            replacement_run_id = params.get("replacement_run_id")
            if isinstance(replacement_run_id, str):
                effect = (
                    self.store.command_log.replace_effect(
                        method,
                        str(params["request_id"]),
                        agent_id=str(params["agent_id"]),
                        command_hash=command_hash,
                    )
                    if isinstance(params.get("request_id"), str)
                    else None
                )
                if effect is not None and effect["status"] == "completed":
                    saved = effect.get("result")
                    if isinstance(saved, dict):
                        replacement_id = effect.get("replacement_run_id")
                        if not isinstance(replacement_id, str):
                            raise CommandRetryable(
                                "completed replacement has no durable target"
                            )
                        try:
                            await self._require_replacement_control(replacement_id)
                        except RunNotFound as exc:
                            if self.store.find_archived_run(replacement_id) is None:
                                raise CommandRetryable(
                                    "replacement run is not available"
                                ) from exc
                        return saved
                current_id = self.store.current_run_id(str(params["agent_id"]))
                if current_id == replacement_run_id:
                    current = self.store.get(current_id)
                    if (
                        current.replaces_run_id == params.get("run_id")
                        and effect is not None
                        and effect["status"] == "completed"
                    ):
                        await self._require_replacement_control(replacement_run_id)
                        saved = effect.get("result")
                        return saved if isinstance(saved, dict) else _public_run(current)
                    if (
                        current.replaces_run_id == params.get("run_id")
                        and effect is not None
                        and effect["status"] == "published"
                    ):
                        # Fresh/cross-provider replace publishes the effect
                        # before _launch_record. A crash inside _launch_record
                        # can leave the effect at "published" while the
                        # provider actually committed. Reconcile against the
                        # durable replacement: if the start committed
                        # (start_transaction cleared and provider_session_id
                        # set), promote the effect instead of killing the
                        # live replacement.
                        replacement_record = self.store.get(replacement_run_id)
                        if (
                            replacement_record.start_transaction is None
                            and replacement_record.provider_session_id
                            and replacement_record.state not in TERMINAL_STATES
                        ):
                            self._record_launched_replacement_effect(
                                str(params["request_id"]),
                                replacement_record,
                            )
                            await self._require_replacement_control(replacement_run_id)
                            result = _public_run(self.store.get(replacement_run_id))
                            self.store.command_log.update_replace_effect(
                                method,
                                str(params["request_id"]),
                                "completed",
                                result,
                            )
                            return result
                        self.store.abort_replace(
                            str(params["run_id"]),
                            replacement_run_id,
                            reason="recovered before provider replacement effect",
                            adapter_status=AdapterStatus(
                                state=LifecycleState.BLOCKED,
                                session_id=None,
                                pid=None,
                                generation=current.provider_generation,
                                active_turn_id=None,
                                transcript_path=current.transcript_path,
                            ),
                        )
                    elif (
                        current.replaces_run_id == params.get("run_id")
                        and effect is not None
                        and effect["status"] == "prepared"
                    ):
                        self.store.abort_replace(
                            str(params["run_id"]),
                            replacement_run_id,
                            reason="recovered before provider replacement effect",
                            adapter_status=AdapterStatus(
                                state=LifecycleState.BLOCKED,
                                session_id=None,
                                pid=None,
                                generation=current.provider_generation,
                                active_turn_id=None,
                                transcript_path=current.transcript_path,
                            ),
                        )
                    elif (
                        current.replaces_run_id == params.get("run_id")
                        and effect is not None
                        and effect["status"] == "provider_completed"
                    ):
                        saved = effect.get("result")
                        if isinstance(saved, dict):
                            self.store.update_adapter_status(
                                replacement_run_id,
                                AdapterStatus(
                                    state=LifecycleState(str(saved.get("state", "starting"))),
                                    session_id=saved.get("session_id"),
                                    pid=saved.get("pid"),
                                    generation=int(saved.get("generation", 1)),
                                    active_turn_id=saved.get("active_turn_id"),
                                    transcript_path=saved.get("transcript_path"),
                                    detail=saved.get("detail"),
                                ),
                            )
                        await self._require_replacement_control(replacement_run_id)
                        result = _public_run(self.store.get(replacement_run_id))
                        self.store.command_log.update_replace_effect(
                            method,
                            str(params["request_id"]),
                            "completed",
                            result,
                        )
                        return result
                    elif (
                        current.replaces_run_id == params.get("run_id")
                        and effect is not None
                        and effect["status"] == "provider_started"
                    ):
                        self.store.abort_replace(
                            str(params["run_id"]),
                            replacement_run_id,
                            reason="recovered before provider replacement effect",
                            adapter_status=AdapterStatus(
                                state=LifecycleState.BLOCKED,
                                session_id=None,
                                pid=None,
                                generation=current.provider_generation,
                                active_turn_id=None,
                                transcript_path=current.transcript_path,
                            ),
                        )
            return _public_run(
                await self.replace(
                    self._resolve_run_id(params),
                    str(params["prompt"]),
                    params.get("model"),
                    ProviderKind(provider) if provider is not None else None,
                    params.get("effort"),
                    params.get("backend_base_url"),
                    replacement_run_id,
                    effect_id=params.get("request_id"),
                    command_hash=command_hash,
                )
            )
        if method == "run/respond":
            request_id = params["request_id"]
            if not isinstance(request_id, (str, int)):
                raise ValueError("request_id must be a string or integer")
            return _public_run(
                await self.respond(
                    self._resolve_run_id(params),
                    request_id,
                    dict(params.get("response") or {}),
                )
            )
        if method == "events/read":
            run_id = self._resolve_run_id(params)
            after_seq = params.get("after_seq", 0)
            limit = params.get("limit", 200)
            if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
                raise ValueError("event cursor must be a non-negative integer")
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 1000
            ):
                raise ValueError("event limit must be between 1 and 1000")
            record = self.store.get(run_id)
            return {
                "run_id": run_id,
                "provider": record.provider.value,
                "state": record.state.value,
                "raw_count": record.raw_event_count,
                "normalized_count": record.normalized_event_count,
                "dispositions": dict(record.disposition_counts),
                "pending_requests": [
                    dict(request) for request in record.pending_requests.values()
                ],
                "composer_messages": [
                    dict(message) for message in record.composer_messages
                ],
                "current_turn_diff": self.store.current_turn_diff(run_id),
                "events": self.store.read_normalized_events(
                    run_id,
                    after_seq=after_seq,
                    limit=limit,
                ),
                "raw": self.store.read_raw_events(
                    run_id,
                    after_seq=after_seq,
                    limit=limit,
                )
                if params.get("include_raw")
                else None,
            }
        if method == "supervisor/recover":
            return {"runs": await self.recover_on_start()}
        if method == "supervisor/handover":
            run_ids = params.get("run_ids")
            if run_ids is not None and (
                not isinstance(run_ids, list)
                or not all(isinstance(run_id, str) and run_id for run_id in run_ids)
            ):
                raise ValueError("run_ids must be a list of non-empty strings")
            return await self.prepare_handover(run_ids)
        raise ValueError(f"unknown supervisor method: {method}")

    async def close(self) -> None:
        rotation = self.codex_rotation_task
        if rotation is not None and not rotation.done():
            try:
                await asyncio.shield(rotation)
            except Exception:
                # The durable journal records the failed phase; shutdown still
                # has to release provider transports for restart recovery.
                pass
        owned: list[tuple[str, ProviderAdapter]] = []
        seen: set[int] = set()
        for run_id, adapter in list(self.adapters.items()):
            if id(adapter) in seen:
                continue
            seen.add(id(adapter))
            owned.append((run_id, adapter))
        for run_id, adapter in owned:
            try:
                async with self._run_lock(run_id):
                    if self.adapters.get(run_id) is adapter:
                        await self._close_and_drain_adapter(run_id, adapter)
            except Exception:
                # Shutdown still has to release sibling transports. Any raw
                # persistence failure remains visible in the run files/log.
                continue
        for task in self.event_tasks.values():
            task.cancel()
        if self.event_tasks:
            await asyncio.gather(*self.event_tasks.values(), return_exceptions=True)
        self.event_tasks.clear()
        if self.monitor_tasks:
            for task in self.monitor_tasks:
                task.cancel()
            await asyncio.gather(*self.monitor_tasks, return_exceptions=True)
        self.monitor_tasks.clear()
        self.event_routes.clear()
        self.event_processing_locks.clear()
        self.event_inflight_counts.clear()
        self.queue_locks.clear()
        self.agent_locks.clear()
        self.pipeline_failures.clear()
        self.expected_stream_ends.clear()
        self.auth_dead_attempts.clear()
        self.auth_dead_alert_at.clear()
        self.auth_dead_recoveries.clear()
        self.last_limit_alert_at.clear()
        self.last_no_eligible_alert = 0.0
        self.idempotency_results.clear()
        self.idempotency_tasks.clear()
        self.implicit_idempotency_keys.clear()
        self.implicit_idempotency_runs.clear()
        self.detached_at_monotonic.clear()
        self.adapters.clear()
        await self.command_queue.close()
