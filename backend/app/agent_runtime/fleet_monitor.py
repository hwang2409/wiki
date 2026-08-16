"""Supervisor-native fleet monitor.

Watches every live worker and pushes state-transition notifications to the
owning orchestrator session via ``send_now``. Also fires the fleet-doctrine
re-alarms (unrouted verdict and staleness). Transition dedupe is
per-run and per-occurrence, with a stable retry token passed to ``send_now``;
supervisor restarts intentionally clear that memory (the ticket accepts
re-emitting as a tradeoff).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import traceback
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pathlib import Path

from .fleet_monitor_ids import fleet_monitor_request_id
from .store import RunStore, _atomic_write_json
from .graph_health import GraphHealthMonitor, default_clock
from .ticket import base_ticket
from .types import LifecycleState, RunRecord, TERMINAL_STATES


SendNow = Callable[[str, str, str | None, str | None], Awaitable[Any]]
TransitionHook = Callable[[dict[str, Any]], Awaitable[Any]]
FLEET_MONITOR_SOURCE = "fleet-monitor"

__all__ = ["FleetMonitor", "Notification", "base_ticket"]


DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_UNROUTED_VERDICT_REALARM_SECONDS = 300.0
DEFAULT_REVIEW_GAP_THRESHOLD_SECONDS = 300.0
DEFAULT_REVIEW_GAP_REALARM_SECONDS = 600.0
DEFAULT_STALENESS_THRESHOLD_SECONDS = 1800.0
DEFAULT_GRAPH_HEALTH_REALARM_SECONDS = 300.0
DEFAULT_REVIEW_ROUTE_SUPPRESSION_SECONDS = 900.0
DEFAULT_SEND_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_CONCURRENT_SENDS = 4
DEFAULT_MAX_PENDING_MESSAGES = 256


logger = logging.getLogger(__name__)


@dataclass
class _WorkerSnapshot:
    """Last observed state for one worker; used to detect transitions."""

    run_id: str | None = None
    status_state: str | None = None
    runtime_state: LifecycleState | None = None
    pending_status_state: tuple[str | None] | None = None
    pending_status_dedupe_key: str | None = None
    status_occurrence: int = 0
    pending_runtime_state: LifecycleState | None = None
    pending_runtime_dedupe_key: str | None = None
    runtime_occurrence: int = 0
    last_unrouted_verdict_alarm_at: float | None = None
    staleness_alarmed_mtime: float | None = None
    seeded: bool = False


@dataclass
class Notification:
    """One dispatched fleet-monitor message (returned from tick for tests/logs)."""

    ticket: str
    orch_agent_id: str
    orch_run_id: str
    event_type: str
    message: str
    dedupe_key: str


@dataclass
class _WorkerView:
    """Merged view of one worker: run record + effective status projection."""

    record: RunRecord
    status_state: str | None
    pr: str | None
    step: str | None
    blocker: str | None
    status_mtime: float | None
    verdict_path: str | None = None


def _read_status_file(path) -> tuple[dict[str, Any] | None, float | None]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, stat.st_mtime
    try:
        value = json.loads(raw)
    except ValueError:
        return None, stat.st_mtime
    if not isinstance(value, dict):
        return None, stat.st_mtime
    return value, stat.st_mtime


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


class FleetMonitor:
    """Poll live workers, dispatch state-transition notifications to their orchestrators.

    A supervisor-owned async task. One instance per supervisor. ``run(stop)``
    is the background loop; ``tick()`` is one scan cycle exposed for tests
    and for the loop implementation.
    """

    ACTIONABLE_STATUS_STATES: frozenset[str] = frozenset(
        {
            "merge-ready",
            "blocked",
            "abandoned",
        }
    )
    ACTIONABLE_RUNTIME_STATES: frozenset[LifecycleState] = frozenset(
        {
            LifecycleState.BLOCKED,
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.DEAD,
            LifecycleState.INTERRUPTED,
        }
    )

    def __init__(
        self,
        store: RunStore,
        send_now: SendNow,
        *,
        clock: Callable[[], float] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        unrouted_verdict_realarm: float = DEFAULT_UNROUTED_VERDICT_REALARM_SECONDS,
        review_gap_threshold: float = DEFAULT_REVIEW_GAP_THRESHOLD_SECONDS,
        review_gap_realarm: float = DEFAULT_REVIEW_GAP_REALARM_SECONDS,
        staleness_threshold: float = DEFAULT_STALENESS_THRESHOLD_SECONDS,
        graph_health_realarm: float = DEFAULT_GRAPH_HEALTH_REALARM_SECONDS,
        review_route_suppression: float = DEFAULT_REVIEW_ROUTE_SUPPRESSION_SECONDS,
        send_timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
        max_concurrent_sends: int = DEFAULT_MAX_CONCURRENT_SENDS,
        max_pending_messages: int = DEFAULT_MAX_PENDING_MESSAGES,
        ownership_lock: Callable[[str], asyncio.Lock] | None = None,
        on_transition: TransitionHook | None = None,
    ):
        self.store = store
        self.send_now = send_now
        # Workgraph, its renderer, and its health endpoint all use
        # workgraph.CLOCK. Defaulting to that callable keeps the detector on
        # the same clock; tests can inject one clock into this monitor and
        # pass the same value to the graph APIs.
        self.wall_clock = clock or default_clock
        self.monotonic_clock = monotonic_clock or self.wall_clock
        self.interval = interval
        self.unrouted_verdict_realarm = unrouted_verdict_realarm
        self.review_gap_threshold = review_gap_threshold
        self.review_gap_realarm = review_gap_realarm
        self.staleness_threshold = staleness_threshold
        if graph_health_realarm <= 0:
            raise ValueError("graph_health_realarm must be positive")
        if review_route_suppression < 0:
            raise ValueError("review_route_suppression must not be negative")
        self.graph_health_realarm = graph_health_realarm
        self.review_route_suppression = review_route_suppression
        if send_timeout <= 0:
            raise ValueError("send_timeout must be positive")
        if max_concurrent_sends < 1:
            raise ValueError("max_concurrent_sends must be positive")
        if max_pending_messages < 1:
            raise ValueError("max_pending_messages must be positive")
        self.send_timeout = send_timeout
        self.max_concurrent_sends = max_concurrent_sends
        self.max_pending_messages = max_pending_messages
        self.ownership_lock = ownership_lock
        self.on_transition = on_transition
        self._instance_id = uuid4().hex[:12]
        self._snapshots: dict[str, _WorkerSnapshot] = {}
        self._graph_health = GraphHealthMonitor(
            store=self.store,
            emit=self._emit,
            graph_health_realarm=graph_health_realarm,
            review_route_suppression=review_route_suppression,
            send_timeout=send_timeout,
        )
        self._graph_health_snapshots = self._graph_health.snapshots
        self._sent_dedupe_keys: set[tuple[str, str, str]] = set()
        self._destination_run_ids: dict[str, str] = {}
        # Persisted on write / removed on success so a restarted FleetMonitor
        # replays the exact original payload for the same dedupe key. Without
        # this, staleness retries would recompute a new elapsed-time message,
        # collide with the durable send_now receipt for the original message
        # under the same request_id, and raise CommandConflict every tick
        # (WIKI-232 REVIEW8 H2).
        self._pending_messages_path: Path = (
            self.store.paths.runtime_dir / "fleet-monitor-pending.json"
        )
        self._pending_journal_unknown = False
        self._pending_journal_needs_rewrite = False
        self._pending_event_types: dict[tuple[str, str, str], str] = {}
        self._pending_messages: dict[tuple[str, str, str], str] = (
            self._load_pending_messages()
        )
        self._pending_messages_lock = asyncio.Lock()
        self._send_semaphores: dict[str, asyncio.Semaphore] = {}

    @staticmethod
    def _event_type_from_dedupe_key(dedupe_key: str) -> str:
        for marker, event_type in (
            (":status-transition:", "status-transition"),
            (":runtime-transition:", "runtime-transition"),
            (":unrouted-verdict:", "unrouted-verdict"),
            (":staleness:", "staleness"),
            (":graph-unavailable:", "graph-unavailable"),
            (":graph-health:blocking-no-reviewer:", "graph-health"),
            (":graph-health:stall:", "graph-health-stall"),
            (":graph-health:iteration-cap:", "graph-health-iteration-cap"),
        ):
            if marker in dedupe_key:
                return event_type
        return "unknown"

    def _load_pending_messages(self) -> dict[tuple[str, str, str], str]:
        try:
            data = json.loads(self._pending_messages_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "fleet_monitor: pending messages file %s unreadable; "
                "recovering matching payloads from the command log",
                self._pending_messages_path,
            )
            self._pending_journal_unknown = True
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            self._pending_journal_unknown = True
            return {}
        valid_entries = [entry for entry in entries if isinstance(entry, dict)]
        if len(valid_entries) > self.max_pending_messages:
            logger.warning(
                "fleet_monitor: pending journal has %s entries; trimming to %s",
                len(valid_entries),
                self.max_pending_messages,
            )
            valid_entries = valid_entries[-self.max_pending_messages :]
            self._pending_journal_needs_rewrite = True
        loaded: dict[tuple[str, str, str], str] = {}
        for entry in valid_entries:
            agent_id = entry.get("agent_id")
            run_id = entry.get("run_id")
            dedupe_key = entry.get("dedupe_key")
            message = entry.get("message")
            event_type = entry.get("event_type")
            if (
                isinstance(agent_id, str)
                and isinstance(run_id, str)
                and isinstance(dedupe_key, str)
                and isinstance(message, str)
            ):
                identity = (agent_id, run_id, dedupe_key)
                loaded[identity] = message
                self._pending_event_types[identity] = (
                    event_type
                    if isinstance(event_type, str)
                    else self._event_type_from_dedupe_key(dedupe_key)
                )
        return loaded

    def _persist_pending_messages(self) -> None:
        """Write the in-memory pending journal atomically.

        Raises on failure so the caller can decide whether a durable
        commit boundary depends on it (``_emit`` MUST skip dispatch
        when this raises — WIKI-232 REVIEW12 M1) or whether the write
        is best-effort cleanup (``_reconcile_worker_state`` and
        ``_reset_agent_state`` catch and log).
        """

        entries = [
            {
                "agent_id": identity[0],
                "run_id": identity[1],
                "dedupe_key": identity[2],
                "message": message,
                "event_type": self._pending_event_types.get(
                    identity,
                    self._event_type_from_dedupe_key(identity[2]),
                ),
            }
            for identity, message in sorted(self._pending_messages.items())
        ]
        if not entries:
            try:
                self._pending_messages_path.unlink()
            except FileNotFoundError:
                pass
            self._pending_journal_unknown = False
            self._pending_journal_needs_rewrite = False
            return
        _atomic_write_json(self._pending_messages_path, {"entries": entries})
        self._pending_journal_unknown = False
        self._pending_journal_needs_rewrite = False

    def _durable_pending_message(
        self, orch_run_id: str, dedupe_key: str
    ) -> tuple[bool, str | None]:
        """Return an exact prior payload when this request already exists."""

        request_id = fleet_monitor_request_id(orch_run_id, dedupe_key)
        effect = self.store.command_log.steer_effect_for_request(
            "run/send_now", request_id
        )
        if isinstance(effect, dict):
            message = effect.get("message")
            if effect.get("run_id") == orch_run_id and isinstance(message, str):
                return True, message
        for command in self.store.command_log.pending():
            if command.method != "run/send_now" or command.request_id != request_id:
                continue
            message = command.payload.get("text")
            return True, message if isinstance(message, str) else None
        return self.store.command_log.known("run/send_now", request_id), None

    def _drop_pending_identity(self, identity: tuple[str, str, str]) -> None:
        self._pending_messages.pop(identity, None)
        self._pending_event_types.pop(identity, None)

    def _drop_prior_event_occurrences(
        self,
        identity: tuple[str, str, str],
        event_type: str,
    ) -> None:
        for candidate in list(self._pending_messages):
            if candidate == identity or candidate[:2] != identity[:2]:
                continue
            candidate_type = self._pending_event_types.get(
                candidate,
                self._event_type_from_dedupe_key(candidate[2]),
            )
            if candidate_type == event_type:
                self._drop_pending_identity(candidate)

    def _persist_pending_messages_best_effort(self) -> None:
        """Cleanup-path persist: log and swallow write failures.

        Reconcile-driven persists (archived-run purge, replacement
        agent reset) prune stale entries. Losing that write is a
        cleanup hygiene issue, not a commit-boundary violation, so it
        must not crash the tick or interfere with subsequent dispatch
        attempts.
        """

        try:
            self._persist_pending_messages()
        except Exception:
            self._pending_journal_needs_rewrite = True
            logger.exception(
                "fleet_monitor: best-effort persist of pending messages "
                "to %s failed; retrying next reconcile",
                self._pending_messages_path,
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except TimeoutError:
                try:
                    await self.tick()
                except Exception:
                    # The daemon writes stderr to supervisor.log; a poisoned
                    # cycle must not kill the fleet monitor loop.
                    traceback.print_exc()

    async def tick(self) -> list[Notification]:
        # RunStore.list_runs() and status-file reads are synchronous historical
        # scans. Keep them off the daemon event loop so a large archive cannot
        # delay sends or the recovery loop.
        views = await asyncio.to_thread(self._collect_views)
        # Drop snapshots for workers that are no longer live so archived
        # tickets don't burn memory forever.
        current_run_ids = {
            view.record.agent_id: view.record.run_id for view in views
        }
        self._reconcile_worker_state(current_run_ids)
        self._reconcile_destination_runs(views)

        wall_now = self.wall_clock()
        monotonic_now = self.monotonic_clock()
        batches = await asyncio.gather(
            *(
                self._process_worker(
                    view,
                    views,
                    wall_now,
                    monotonic_now,
                )
                for view in views
            ),
            return_exceptions=True,
        )
        notifications: list[Notification] = []
        for batch in batches:
            if isinstance(batch, Exception):
                logger.error(
                    "fleet_monitor: worker scan failed: %s",
                    batch,
                    exc_info=(type(batch), batch, batch.__traceback__),
                )
                continue
            notifications.extend(batch)
        notifications.extend(
            await self._process_graph_health(views, wall_now)
        )
        self._prune_obsolete_pending(views, wall_now, monotonic_now)
        return notifications

    def _reconcile_worker_state(self, current_run_ids: dict[str, str]) -> None:
        """Forget snapshots and dedupe state for archived or replaced runs."""

        for agent_id, snapshot in list(self._snapshots.items()):
            if current_run_ids.get(agent_id) != snapshot.run_id:
                self._snapshots.pop(agent_id, None)
        self._sent_dedupe_keys = {
            identity
            for identity in self._sent_dedupe_keys
            if current_run_ids.get(identity[0]) == identity[1]
        }
        pending_before = self._pending_messages
        self._pending_messages = {
            identity: message
            for identity, message in self._pending_messages.items()
            if current_run_ids.get(identity[0]) == identity[1]
        }
        self._pending_event_types = {
            identity: event_type
            for identity, event_type in self._pending_event_types.items()
            if identity in self._pending_messages
        }
        if pending_before.keys() != self._pending_messages.keys():
            self._persist_pending_messages_best_effort()
        self._destination_run_ids = {
            agent_id: run_id
            for agent_id, run_id in self._destination_run_ids.items()
            if agent_id in current_run_ids
        }

    def _reconcile_destination_runs(self, views: list[_WorkerView]) -> None:
        """Reset delivery suppression when an orchestrator run changes."""

        for view in views:
            agent_id = view.record.agent_id
            orch_agent_id = view.record.orchestrator_id
            if not orch_agent_id:
                continue
            try:
                orch_run_id = self.store.current_run_id(orch_agent_id)
            except Exception:
                continue
            if not orch_run_id:
                continue
            prior_run_id = self._destination_run_ids.get(agent_id)
            self._destination_run_ids[agent_id] = orch_run_id
            if prior_run_id is None or prior_run_id == orch_run_id:
                continue

            # Sent suppression belongs to the destination run. The worker run
            # and durable alarm payload stay unchanged across an orchestrator
            # replacement, but the new run-scoped command identity must get
            # one delivery of every still-active alarm.
            self._sent_dedupe_keys = {
                identity
                for identity in self._sent_dedupe_keys
                if identity[0] != agent_id
            }
            snapshot = self._snapshots.get(agent_id)
            if snapshot is not None:
                snapshot.last_unrouted_verdict_alarm_at = None
                snapshot.staleness_alarmed_mtime = None

            graph_snapshot = self._graph_health_snapshots.get(
                base_ticket(agent_id)
            )
            if graph_snapshot is not None:
                graph_snapshot.last_blocking_no_reviewer_alarm_at = None
                graph_snapshot.last_graph_unavailable_alarm_at = None
                graph_snapshot.stall_notification_sent = False
                graph_snapshot.iteration_notification_sent = False

    def _reset_agent_state(
        self, agent_id: str, *, keep_run_id: str | None = None
    ) -> None:
        self._snapshots.pop(agent_id, None)
        # Preserve state that belongs to ``keep_run_id`` so a fresh
        # boot (snapshot missing, but the worker's run is unchanged)
        # does not discard durable ``_pending_messages`` entries the
        # restarted monitor must replay under the original request id
        # (WIKI-232 REVIEW8 H2).
        self._sent_dedupe_keys = {
            identity
            for identity in self._sent_dedupe_keys
            if identity[0] != agent_id or identity[1] == keep_run_id
        }
        pending_before = self._pending_messages
        self._pending_messages = {
            identity: message
            for identity, message in self._pending_messages.items()
            if identity[0] != agent_id or identity[1] == keep_run_id
        }
        self._pending_event_types = {
            identity: event_type
            for identity, event_type in self._pending_event_types.items()
            if identity in self._pending_messages
        }
        if pending_before.keys() != self._pending_messages.keys():
            self._persist_pending_messages_best_effort()

    def _prune_obsolete_pending(
        self,
        views: list[_WorkerView],
        wall_now: float,
        monotonic_now: float,
    ) -> None:
        """Keep only alarm payloads whose durable request can recur."""

        views_by_agent = {view.record.agent_id: view for view in views}
        graph_window = int(wall_now // max(self.graph_health_realarm, 1.0))
        verdict_window = int(
            monotonic_now // max(self.unrouted_verdict_realarm, 1.0)
        )
        obsolete: list[tuple[str, str, str]] = []
        for identity in self._pending_messages:
            agent_id, _run_id, dedupe_key = identity
            event_type = self._pending_event_types.get(
                identity,
                self._event_type_from_dedupe_key(dedupe_key),
            )
            view = views_by_agent.get(agent_id)
            keep = True
            if event_type in {"status-transition", "runtime-transition"}:
                keep = False
            elif event_type == "staleness":
                keep = bool(
                    view is not None
                    and view.status_state == "working"
                    and view.status_mtime is not None
                    and wall_now - view.status_mtime >= self.staleness_threshold
                    and dedupe_key
                    == f"fleet:{agent_id}:staleness:{int(view.status_mtime)}"
                )
            elif event_type == "unrouted-verdict":
                step = view.step if view is not None else None
                keep = bool(
                    view is not None
                    and view.record.role == "review"
                    and step is not None
                    and ("MERGE-READY" in step or "NOT-MERGE-READY" in step)
                    and dedupe_key
                    == f"fleet:{agent_id}:unrouted-verdict:{verdict_window}"
                )
            elif event_type.startswith("graph-"):
                parts = dedupe_key.split(":")
                ticket = parts[1] if len(parts) > 1 else ""
                graph_state = self._graph_health_snapshots.get(ticket)
                if event_type == "graph-unavailable":
                    keep = bool(
                        graph_state is not None
                        and graph_state.graph_unavailable_active
                        and dedupe_key
                        == (
                            f"fleet:{ticket}:graph-unavailable:"
                            f"{graph_state.graph_unavailable_episode}:{graph_window}"
                        )
                    )
                elif event_type == "graph-health":
                    marker = (
                        graph_state.blocking_no_reviewer_episode_marker
                        if graph_state is not None
                        else None
                    )
                    keep = bool(
                        graph_state is not None
                        and graph_state.blocking_no_reviewer_active
                        and marker
                        and dedupe_key
                        == (
                            f"fleet:{ticket}:graph-health:"
                            f"blocking-no-reviewer:{marker}:{graph_window}"
                        )
                    )
                elif event_type == "graph-health-stall":
                    marker = (
                        graph_state.stall_episode_marker
                        if graph_state is not None
                        else None
                    )
                    keep = bool(
                        graph_state is not None
                        and graph_state.stall_active
                        and marker
                        and dedupe_key
                        == f"fleet:{ticket}:graph-health:stall:{marker}"
                    )
                elif event_type == "graph-health-iteration-cap":
                    marker = (
                        graph_state.iteration_episode_marker
                        if graph_state is not None
                        else None
                    )
                    keep = bool(
                        graph_state is not None
                        and graph_state.iteration_active
                        and marker
                        and dedupe_key
                        == (
                            f"fleet:{ticket}:graph-health:iteration-cap:{marker}"
                        )
                    )
            if not keep:
                obsolete.append(identity)

        for identity in obsolete:
            self._drop_pending_identity(identity)
        if obsolete or self._pending_journal_needs_rewrite:
            self._persist_pending_messages_best_effort()

    def _collect_views(self) -> list[_WorkerView]:
        views: list[_WorkerView] = []
        for record in self.store.list_runs():
            if record.role == "orchestrator":
                continue
            if record.state in TERMINAL_STATES:
                if not self._is_new_terminal_transition(record):
                    continue
            if record.replaced_by_run_id:
                continue
            if not self.store.is_current(record):
                continue
            if not record.orchestrator_id:
                continue
            if record.execution_kind in {"wk-claude", "wk-codex"}:
                status_data = {
                    "state": record.wk_status_state,
                    "pr": record.wk_status_pr,
                    "step": record.wk_status_step,
                    "blocker": record.wk_status_blocker,
                }
                mtime = None
            else:
                status_data, mtime = _read_status_file(
                    self.store.status_path(record.agent_id)
                )
            if status_data is None:
                views.append(
                    _WorkerView(
                        record=record,
                        status_state=None,
                        pr=None,
                        step=None,
                        blocker=None,
                        verdict_path=None,
                        status_mtime=mtime,
                    )
                )
                continue
            views.append(
                _WorkerView(
                    record=record,
                    status_state=_string_or_none(status_data.get("state")),
                    pr=_string_or_none(status_data.get("pr")),
                    step=_string_or_none(status_data.get("step")),
                    blocker=_string_or_none(status_data.get("blocker")),
                    verdict_path=_string_or_none(
                        status_data.get("verdict_path") or status_data.get("artifact_path")
                    ),
                    status_mtime=mtime,
                )
            )
        return views

    async def _process_worker(
        self,
        view: _WorkerView,
        all_views: list[_WorkerView],
        wall_now: float,
        monotonic_now: float,
    ) -> list[Notification]:
        record = view.record
        snapshot = self._snapshots.get(record.agent_id)
        if snapshot is None or snapshot.run_id != record.run_id:
            self._reset_agent_state(record.agent_id, keep_run_id=record.run_id)
            snapshot = _WorkerSnapshot(run_id=record.run_id)
        results: list[Notification] = []

        if not snapshot.seeded:
            snapshot.seeded = True

        # Notify only on state transitions (e.g. working -> merge-ready);
        # step/pr/blocker rewrites within the same state are context, not
        # events, and steering the orchestrator on each one is pure noise.
        current_status_state = (view.status_state,)
        if current_status_state != (snapshot.status_state,):
            if view.status_state not in self.ACTIONABLE_STATUS_STATES:
                snapshot.status_state = current_status_state[0]
                snapshot.pending_status_state = None
                snapshot.pending_status_dedupe_key = None
            else:
                if snapshot.pending_status_state != current_status_state:
                    snapshot.status_occurrence += 1
                    snapshot.pending_status_state = current_status_state
                    snapshot.pending_status_dedupe_key = self._transition_dedupe_key(
                        view,
                        event_type="status-transition",
                        occurrence=snapshot.status_occurrence,
                    )
                dedupe_key = snapshot.pending_status_dedupe_key
                assert dedupe_key is not None
                notif = await self._emit(
                    view,
                    event_type="status-transition",
                    message=self._status_transition_message(view, snapshot),
                    dedupe_key=dedupe_key,
                )
                if notif is not None:
                    results.append(notif)
                observer_succeeded = notif is not None or self._dedupe_was_sent(
                    view, dedupe_key
                )
                # Autopilot is a control path, not observer telemetry. Run it
                # even when delivery to the orchestrator fails.
                hook_succeeded = True
                if self.on_transition is not None:
                    try:
                        hook_result = await self.on_transition(
                            {
                                "agent_id": record.agent_id,
                                "run_id": record.run_id,
                                "status_state": view.status_state,
                                "previous_status_state": snapshot.status_state,
                                "status_mtime": view.status_mtime,
                                "step": view.step,
                                "pr": view.pr,
                                "verdict_path": view.verdict_path,
                                "orch": record.orchestrator_id,
                            }
                        )
                        hook_succeeded = hook_result is not False
                    except Exception:
                        hook_succeeded = False
                        logger.exception(
                            "fleet_monitor: transition hook failed for %s",
                            record.agent_id,
                        )
                if observer_succeeded and hook_succeeded:
                    snapshot.status_state = current_status_state[0]
                    snapshot.pending_status_state = None
                    snapshot.pending_status_dedupe_key = None
                    self._clear_dedupe_key(view, dedupe_key)

        if record.state != snapshot.runtime_state:
            if record.state not in self.ACTIONABLE_RUNTIME_STATES:
                snapshot.runtime_state = record.state
                snapshot.pending_runtime_state = None
                snapshot.pending_runtime_dedupe_key = None
            else:
                if snapshot.pending_runtime_state is not record.state:
                    snapshot.runtime_occurrence += 1
                    snapshot.pending_runtime_state = record.state
                    snapshot.pending_runtime_dedupe_key = self._transition_dedupe_key(
                        view,
                        event_type="runtime-transition",
                        occurrence=snapshot.runtime_occurrence,
                    )
                dedupe_key = snapshot.pending_runtime_dedupe_key
                assert dedupe_key is not None
                notif = await self._emit(
                    view,
                    event_type="runtime-transition",
                    message=self._runtime_transition_message(view, snapshot),
                    dedupe_key=dedupe_key,
                )
                if notif is not None:
                    results.append(notif)
                if notif is not None or self._dedupe_was_sent(view, dedupe_key):
                    snapshot.runtime_state = record.state
                    snapshot.pending_runtime_state = None
                    snapshot.pending_runtime_dedupe_key = None
                    self._clear_dedupe_key(view, dedupe_key)

        results.extend(
            await self._maybe_unrouted_verdict(view, snapshot, monotonic_now)
        )
        results.extend(await self._maybe_staleness(view, snapshot, wall_now))

        self._snapshots[record.agent_id] = snapshot
        return results

    async def _process_graph_health(
        self, views: list[_WorkerView], now: float
    ) -> list[Notification]:
        return await self._graph_health.process(views, now)

    def _load_graph(self, ticket: str) -> dict[str, Any] | None:
        return self._graph_health.load_graph(ticket)
    def _transition_dedupe_key(
        self,
        view: _WorkerView,
        *,
        event_type: str,
        occurrence: int,
    ) -> str:
        payload = {
            "status": view.status_state,
            "runtime": view.record.state.value,
            "pr": view.pr,
            "step": view.step,
            "blocker": view.blocker,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        return (
            f"fleet:{self._instance_id}:{view.record.agent_id}:"
            f"{event_type}:{occurrence}:{digest}"
        )

    @staticmethod
    def _dedupe_identity(
        view: _WorkerView, dedupe_key: str
    ) -> tuple[str, str, str]:
        return (view.record.agent_id, view.record.run_id, dedupe_key)

    def _dedupe_was_sent(self, view: _WorkerView, dedupe_key: str) -> bool:
        return self._dedupe_identity(view, dedupe_key) in self._sent_dedupe_keys

    def _clear_dedupe_key(self, view: _WorkerView, dedupe_key: str) -> None:
        self._sent_dedupe_keys.discard(self._dedupe_identity(view, dedupe_key))

    async def _maybe_unrouted_verdict(
        self,
        view: _WorkerView,
        snapshot: _WorkerSnapshot,
        now: float,
    ) -> list[Notification]:
        record = view.record
        if record.role != "review":
            return []
        step = view.step or ""
        if "MERGE-READY" not in step and "NOT-MERGE-READY" not in step:
            return []
        last = snapshot.last_unrouted_verdict_alarm_at
        if last is not None and (now - last) < self.unrouted_verdict_realarm:
            return []
        window = int(now // max(self.unrouted_verdict_realarm, 1.0))
        notif = await self._emit(
            view,
            event_type="unrouted-verdict",
            message=(
                f"[fleet] {record.agent_id} (review): unrouted verdict — "
                f"step: {step!r}. Route the finding and archive the reviewer."
            ),
            dedupe_key=f"fleet:{record.agent_id}:unrouted-verdict:{window}",
        )
        if notif is not None:
            snapshot.last_unrouted_verdict_alarm_at = now
            return [notif]
        return []

    async def _maybe_staleness(
        self,
        view: _WorkerView,
        snapshot: _WorkerSnapshot,
        now: float,
    ) -> list[Notification]:
        record = view.record
        if view.status_state != "working":
            return []
        mtime = view.status_mtime
        if mtime is None:
            return []
        silent = now - mtime
        if silent < self.staleness_threshold:
            return []
        if snapshot.staleness_alarmed_mtime == mtime:
            return []
        elapsed_min = int(silent // 60)
        notif = await self._emit(
            view,
            event_type="staleness",
            message=(
                f"[fleet] {record.agent_id} ({record.role}): status file "
                f"silent {elapsed_min}m while state=working. Verify runtime."
            ),
            dedupe_key=f"fleet:{record.agent_id}:staleness:{int(mtime)}",
        )
        if notif is not None:
            snapshot.staleness_alarmed_mtime = mtime
            return [notif]
        return []

    def _status_transition_message(
        self, view: _WorkerView, snapshot: _WorkerSnapshot
    ) -> str:
        record = view.record
        return " | ".join(
            [
            f"[fleet] {record.agent_id} ({record.role}): status "
            f"{snapshot.status_state or 'none'} -> {view.status_state or 'none'}"
            ]
            + self._status_context(view)
        )

    @staticmethod
    def _status_context(view: _WorkerView) -> list[str]:
        return [
            f"step: {view.step or 'none'}",
            f"pr: {view.pr or 'none'}",
            f"blocker: {view.blocker or 'none'}",
        ]

    def _runtime_transition_message(
        self, view: _WorkerView, snapshot: _WorkerSnapshot
    ) -> str:
        record = view.record
        prior = snapshot.runtime_state.value if snapshot.runtime_state else "none"
        return " | ".join(
            [
                f"[fleet] {record.agent_id} ({record.role}): runtime {prior} -> "
                f"{record.state.value}"
            ]
            + self._status_context(view)
        )

    def _send_semaphore(self, orch_agent_id: str) -> asyncio.Semaphore:
        return self._send_semaphores.setdefault(
            orch_agent_id,
            asyncio.Semaphore(self.max_concurrent_sends),
        )

    async def _emit(
        self,
        view: _WorkerView,
        *,
        event_type: str,
        message: str,
        dedupe_key: str,
    ) -> Notification | None:
        async with self._ownership_context(view.record.agent_id):
            if not self._worker_is_current(view):
                return None
            orch_agent_id = view.record.orchestrator_id
            if not orch_agent_id:
                return None
            try:
                orch_run_id = self.store.current_run_id(orch_agent_id)
            except Exception:
                return None
            if not orch_run_id:
                return None
            try:
                orch_record = self.store.get(orch_run_id)
            except Exception:
                return None
            if orch_record.state in TERMINAL_STATES:
                return None
            dedupe_identity = self._dedupe_identity(view, dedupe_key)
            if dedupe_identity in self._sent_dedupe_keys:
                return None
            async with self._pending_messages_lock:
                existing_pending = self._pending_messages.get(dedupe_identity)
                if existing_pending is None:
                    # A corrupt journal can lose the local payload after a
                    # successful command. Recover the exact message from the
                    # durable steer effect. If a command exists without a
                    # recoverable payload, fail closed instead of submitting
                    # different time-dependent text under the same request id.
                    known_request, durable_message = self._durable_pending_message(
                        orch_run_id, dedupe_key
                    )
                    if known_request and durable_message is None:
                        logger.error(
                            "fleet_monitor: command %s has no recoverable "
                            "payload; skipping conflicting dispatch",
                            fleet_monitor_request_id(orch_run_id, dedupe_key),
                        )
                        return None
                    stable_message = durable_message or message
                    before_messages = dict(self._pending_messages)
                    before_types = dict(self._pending_event_types)
                    self._drop_prior_event_occurrences(
                        dedupe_identity, event_type
                    )
                    if len(self._pending_messages) >= self.max_pending_messages:
                        logger.error(
                            "fleet_monitor: pending journal reached hard "
                            "limit %s; skipping %s",
                            self.max_pending_messages,
                            dedupe_identity,
                        )
                        self._pending_messages = before_messages
                        self._pending_event_types = before_types
                        return None
                    self._pending_messages[dedupe_identity] = stable_message
                    self._pending_event_types[dedupe_identity] = event_type
                    try:
                        self._persist_pending_messages()
                    except Exception:
                        logger.exception(
                            "fleet_monitor: pending message journal write "
                            "for %s failed; skipping dispatch until the "
                            "payload is durable",
                            dedupe_identity,
                        )
                        self._pending_messages = before_messages
                        self._pending_event_types = before_types
                        return None
                else:
                    stable_message = existing_pending
            try:
                async with self._send_semaphore(orch_agent_id):
                    await asyncio.wait_for(
                        self.send_now(
                            orch_run_id,
                            stable_message,
                            dedupe_key,
                            FLEET_MONITOR_SOURCE,
                        ),
                        timeout=self.send_timeout,
                    )
            except Exception as exc:
                logger.info(
                    "fleet_monitor: send_now failed for %s -> %s (%s): %s",
                    view.record.agent_id,
                    orch_agent_id,
                    event_type,
                    exc,
                )
                return None
            self._sent_dedupe_keys.add(dedupe_identity)
            # Transition request ids include this monitor instance and cannot
            # recur after their successful occurrence. Alarm payloads remain
            # until their mtime, window, or episode changes; the end-of-tick
            # prune removes them at that boundary.
            if event_type in {"status-transition", "runtime-transition"}:
                async with self._pending_messages_lock:
                    self._drop_pending_identity(dedupe_identity)
                    self._persist_pending_messages_best_effort()
            return Notification(
                ticket=view.record.agent_id,
                orch_agent_id=orch_agent_id,
                orch_run_id=orch_run_id,
                event_type=event_type,
                message=stable_message,
                dedupe_key=dedupe_key,
            )

    @asynccontextmanager
    async def _ownership_context(self, agent_id: str) -> AsyncIterator[None]:
        if self.ownership_lock is None:
            yield
            return
        async with self.ownership_lock(agent_id):
            yield

    def _worker_is_current(self, view: _WorkerView) -> bool:
        try:
            if self.store.current_run_id(view.record.agent_id) != view.record.run_id:
                return False
            record = self.store.get(view.record.run_id)
        except Exception:
            return False
        return (
            record.run_id == view.record.run_id
            and record.replaced_by_run_id is None
            and self.store.is_current(record)
            and (
                record.state not in TERMINAL_STATES
                or self._is_new_terminal_transition(record)
            )
        )

    def _is_new_terminal_transition(self, record: RunRecord) -> bool:
        snapshot = self._snapshots.get(record.agent_id)
        return (
            record.state in TERMINAL_STATES
            and snapshot is not None
            and snapshot.run_id == record.run_id
            and snapshot.runtime_state is not None
            and snapshot.runtime_state not in TERMINAL_STATES
        )
