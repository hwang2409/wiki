"""Supervisor-native fleet monitor.

Watches every live worker and pushes state-transition notifications to the
owning orchestrator session via ``send_now``. Also fires the fleet-doctrine
re-alarms (unrouted verdict, review-gap, staleness). Transition dedupe is
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
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .store import RunStore
from .types import LifecycleState, RunRecord, TERMINAL_STATES


SendNow = Callable[[str, str, str | None, str | None], Awaitable[Any]]
FLEET_MONITOR_SOURCE = "fleet-monitor"


DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_UNROUTED_VERDICT_REALARM_SECONDS = 300.0
DEFAULT_REVIEW_GAP_THRESHOLD_SECONDS = 300.0
DEFAULT_REVIEW_GAP_REALARM_SECONDS = 600.0
DEFAULT_STALENESS_THRESHOLD_SECONDS = 1800.0
DEFAULT_GRAPH_HEALTH_REALARM_SECONDS = 300.0
DEFAULT_REVIEW_ROUTE_SUPPRESSION_SECONDS = 900.0
DEFAULT_SEND_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_CONCURRENT_SENDS = 4


logger = logging.getLogger(__name__)


def _workgraph_module():
    """Load graph support lazily so backend-only daemon bundles still boot."""

    from .. import workgraph

    return workgraph


def _default_clock() -> float:
    try:
        return _workgraph_module().CLOCK()
    except ModuleNotFoundError:
        # Historical daemon checkouts may contain only ``backend/`` and not
        # the graph package. Their fleet monitor still needs a wall clock.
        return datetime.now(timezone.utc).timestamp()


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
class _GraphHealthSnapshot:
    """Alarm state for one ticket-level composite-health scan."""

    last_blocking_no_reviewer_alarm_at: float | None = None
    blocking_no_reviewer_active: bool = False
    blocking_no_reviewer_episode: int = 0
    latest_verdict_marker: tuple[int, str | None] | None = None
    graph_unavailable_active: bool = False
    graph_unavailable_episode: int = 0
    last_graph_unavailable_alarm_at: float | None = None
    last_clock: float | None = None
    stall_active: bool = False
    stall_episode: int = 0
    stall_append_acknowledged: bool = False
    stall_notification_sent: bool = False
    iteration_active: bool = False
    iteration_episode: int = 0
    iteration_append_acknowledged: bool = False
    iteration_notification_sent: bool = False


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
    """Merged view of one worker: run record + on-disk status."""

    record: RunRecord
    status_state: str | None
    pr: str | None
    step: str | None
    blocker: str | None
    status_mtime: float | None


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


def _ticket_prefix(agent_id: str) -> str:
    """Trim -REVIEW*/-SIM*/-PLAN* siblings back to the base ticket id."""

    for suffix in ("-REVIEW", "-SIM", "-PLAN", "-AUDIT", "-CANARY", "-THERMO", "-EVAL"):
        marker = agent_id.find(suffix)
        if marker != -1:
            return agent_id[:marker]
    return agent_id


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
        ownership_lock: Callable[[str], asyncio.Lock] | None = None,
    ):
        self.store = store
        self.send_now = send_now
        # Workgraph, its renderer, and its health endpoint all use
        # workgraph.CLOCK. Defaulting to that callable keeps the detector on
        # the same clock; tests can inject one clock into this monitor and
        # pass the same value to the graph APIs.
        self.wall_clock = clock or _default_clock
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
        self.send_timeout = send_timeout
        self.max_concurrent_sends = max_concurrent_sends
        self.ownership_lock = ownership_lock
        self._instance_id = uuid4().hex[:12]
        self._snapshots: dict[str, _WorkerSnapshot] = {}
        self._graph_health_snapshots: dict[str, _GraphHealthSnapshot] = {}
        self._sent_dedupe_keys: set[tuple[str, str, str]] = set()
        self._send_semaphores: dict[str, asyncio.Semaphore] = {}

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

    def _reset_agent_state(self, agent_id: str) -> None:
        self._snapshots.pop(agent_id, None)
        self._sent_dedupe_keys = {
            identity
            for identity in self._sent_dedupe_keys
            if identity[0] != agent_id
        }

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
            self._reset_agent_state(record.agent_id)
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
                if notif is not None or self._dedupe_was_sent(view, dedupe_key):
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
        """Run the ticket-level ``graph_health`` detector once per ticket."""

        by_ticket: dict[str, _WorkerView] = {}
        for view in views:
            ticket = _ticket_prefix(view.record.agent_id)
            prior = by_ticket.get(ticket)
            # Prefer the base implement worker: its orchestrator mapping and
            # notification identity are the canonical ticket-level ones.
            if prior is None or view.record.agent_id == ticket:
                by_ticket[ticket] = view

        results: list[Notification] = []
        for ticket, view in by_ticket.items():
            graph = self._load_graph(ticket)
            if graph is None:
                results.extend(await self._maybe_graph_unavailable(ticket, view, now))
                continue
            state = self._graph_health_snapshots.setdefault(
                ticket, _GraphHealthSnapshot()
            )
            state.graph_unavailable_active = False
            state.last_graph_unavailable_alarm_at = None
            try:
                result = await self._maybe_graph_health(ticket, view, graph, now)
            except Exception:
                logger.exception("fleet_monitor: graph health scan failed for %s", ticket)
                continue
            results.extend(result)
        return results

    def _load_graph(self, ticket: str) -> dict[str, Any] | None:
        try:
            graph_module = _workgraph_module()
        except ModuleNotFoundError as exc:
            logger.error("fleet_monitor: graph support unavailable for %s: %s", ticket, exc)
            return None

        def valid(candidate: object, source: str) -> dict[str, Any] | None:
            if not isinstance(candidate, dict):
                return None
            try:
                violations = graph_module.graph_lint.validate_document(
                    candidate, "workgraph"
                )
            except Exception as exc:
                logger.error(
                    "fleet_monitor: could not validate %s workgraph for %s: %s",
                    source,
                    ticket,
                    exc,
                )
                return None
            if violations:
                logger.error(
                    "fleet_monitor: %s workgraph for %s failed schema validation: %s",
                    source,
                    ticket,
                    violations,
                )
                return None
            return candidate

        try:
            graph = graph_module.load_workgraph(ticket, self.store.paths.status_dir)
        except ModuleNotFoundError:
            return None
        except graph_module.WorkgraphCorruptError as exc:
            logger.error(
                "fleet_monitor: hot workgraph unavailable for %s; trying snapshot: %s",
                ticket,
                exc,
            )
            graph = None
        if graph is not None:
            validated = valid(graph, "hot")
            if validated is not None:
                return validated
            logger.error(
                "fleet_monitor: hot workgraph unavailable for %s; trying snapshot",
                ticket,
            )
        try:
            snapshot = graph_module.load_snapshot(ticket)
        except Exception as exc:
            logger.error("fleet_monitor: could not load graph snapshot for %s: %s", ticket, exc)
            return None
        return valid(snapshot, "durable snapshot")

    @staticmethod
    def _iteration_cap(graph: dict[str, Any]) -> int:
        graph_module = _workgraph_module()
        direct = graph.get("iteration_cap")
        if isinstance(direct, int) and not isinstance(direct, bool) and direct > 0:
            return direct
        template_id = graph.get("template")
        if isinstance(template_id, str):
            try:
                from wiki_cli import workgraph_templates

                template = workgraph_templates.load_templates().get(template_id)
            except workgraph_templates.TemplateSelectionError:
                template = None
            if isinstance(template, dict):
                cap = template.get("iteration_cap")
                if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                    return cap
        return graph_module.DEFAULT_ITERATION_CAP

    @staticmethod
    def _latest_verdict_marker(
        graph: dict[str, Any],
    ) -> tuple[int, str | None] | None:
        for index in range(len(graph.get("edges", [])) - 1, -1, -1):
            edge = graph["edges"][index]
            if isinstance(edge, dict) and edge.get("kind") == "verdict":
                created_at = edge.get("created_at")
                return index, created_at if isinstance(created_at, str) else None
        return None

    @staticmethod
    def _latest_verdict_route_time(graph: dict[str, Any], ticket: str) -> float | None:
        """Return the route timestamp only when it follows the latest verdict."""

        edges = graph.get("edges", [])
        latest_verdict_index = None
        for index in range(len(edges) - 1, -1, -1):
            edge = edges[index]
            if isinstance(edge, dict) and edge.get("kind") == "verdict":
                latest_verdict_index = index
                break
        if latest_verdict_index is None:
            return None
        for edge in edges[latest_verdict_index + 1 :]:
            if not isinstance(edge, dict) or edge.get("kind") != "steer":
                continue
            payload = edge.get("payload")
            target = payload.get("target_worker") if isinstance(payload, dict) else None
            if target != ticket and edge.get("to") != ticket:
                continue
            timestamp = _workgraph_module()._parse_ts(edge.get("created_at"))  # noqa: SLF001
            if timestamp is not None:
                return timestamp
        return None

    async def _maybe_graph_unavailable(
        self, ticket: str, view: _WorkerView, now: float
    ) -> list[Notification]:
        state = self._graph_health_snapshots.setdefault(ticket, _GraphHealthSnapshot())
        if state.last_clock is not None and now < state.last_clock:
            state.last_graph_unavailable_alarm_at = None
            state.graph_unavailable_episode += 1
        state.last_clock = now
        if not state.graph_unavailable_active:
            state.graph_unavailable_active = True
            state.graph_unavailable_episode += 1
            state.last_graph_unavailable_alarm_at = None
        last = state.last_graph_unavailable_alarm_at
        if last is not None and now - last < self.graph_health_realarm:
            return []
        window = int(now // max(self.graph_health_realarm, 1.0))
        notif = await self._emit(
            view,
            event_type="graph-unavailable",
            message=(
                f"[fleet] {ticket}: workgraph unavailable or invalid. "
                "Restore the graph before relying on composite health."
            ),
            dedupe_key=(
                f"fleet:{ticket}:graph-unavailable:{state.graph_unavailable_episode}:{window}"
            ),
        )
        if notif is not None:
            state.last_graph_unavailable_alarm_at = now
            return [notif]
        return []

    async def _maybe_graph_health(
        self,
        ticket: str,
        view: _WorkerView,
        graph: dict[str, Any],
        now: float,
    ) -> list[Notification]:
        graph_module = _workgraph_module()
        state = self._graph_health_snapshots.setdefault(ticket, _GraphHealthSnapshot())
        if state.last_clock is not None and now < state.last_clock:
            state.last_blocking_no_reviewer_alarm_at = None
            state.blocking_no_reviewer_active = False
            state.stall_active = False
            state.stall_append_acknowledged = False
            state.stall_notification_sent = False
            state.iteration_active = False
            state.iteration_append_acknowledged = False
            state.iteration_notification_sent = False
        state.last_clock = now
        verdict_marker = self._latest_verdict_marker(graph)
        if verdict_marker != state.latest_verdict_marker:
            state.latest_verdict_marker = verdict_marker
            state.last_blocking_no_reviewer_alarm_at = None
            state.blocking_no_reviewer_active = False
        cap = self._iteration_cap(graph)
        current = graph_module.current_health(graph, iteration_cap=cap, now_ts=now)
        health = current["health"]
        alarms = {alarm["check"]: alarm for alarm in current["alarms"]}
        results: list[Notification] = []

        no_reviewer = "blocking_no_reviewer" in alarms
        if not no_reviewer:
            state.last_blocking_no_reviewer_alarm_at = None
            state.blocking_no_reviewer_active = False
        else:
            if not state.blocking_no_reviewer_active:
                state.blocking_no_reviewer_active = True
                state.blocking_no_reviewer_episode += 1
            routed_at = self._latest_verdict_route_time(graph, ticket)
            route_age = now - routed_at if routed_at is not None else None
            suppressed = route_age is not None and 0 <= route_age < self.review_route_suppression
            last = state.last_blocking_no_reviewer_alarm_at
            if not suppressed and (
                last is None or now - last >= self.graph_health_realarm
            ):
                blocking = health.get("blocking", 0)
                window = int(now // max(self.graph_health_realarm, 1.0))
                notif = await self._emit(
                    view,
                    event_type="graph-health",
                    message=(
                        f"[fleet] {ticket}: graph health has {blocking} BLOCKING "
                        "finding(s) and no live reviewer. Spawn a reviewer or "
                        "route the review."
                    ),
                    dedupe_key=(
                        f"fleet:{ticket}:graph-health:blocking-no-reviewer:"
                        f"{state.blocking_no_reviewer_episode}:{window}"
                    ),
                )
                if notif is not None:
                    state.last_blocking_no_reviewer_alarm_at = now
                    results.append(notif)

        if "node_stall" not in alarms:
            state.stall_active = False
            state.stall_append_acknowledged = False
            state.stall_notification_sent = False
        else:
            if not state.stall_active:
                state.stall_active = True
                state.stall_episode += 1
                state.stall_append_acknowledged = False
                state.stall_notification_sent = False
            stall = health.get("slowest_node_stall_seconds", 0)
            if not state.stall_append_acknowledged:
                state.stall_append_acknowledged = await self._append_escalation(
                    ticket,
                    view,
                    reason=f"node stall exceeded {graph_module.STALL_ALARM_SECONDS}s ({stall}s)",
                    prior_findings=self._open_findings(graph),
                    condition="stall",
                    episode=state.stall_episode,
                    now=now,
                )
            if state.stall_append_acknowledged and not state.stall_notification_sent:
                notif = await self._emit(
                    view,
                    event_type="graph-health-stall",
                    message=(
                        f"[fleet] {ticket}: graph node stalled {stall}s "
                        f"(> {graph_module.STALL_ALARM_SECONDS}s); escalation sent to Henry."
                    ),
                    dedupe_key=f"fleet:{ticket}:graph-health:stall:{state.stall_episode}",
                )
                if notif is not None:
                    state.stall_notification_sent = True
                    results.append(notif)

        if "iteration_cap" not in alarms:
            state.iteration_active = False
            state.iteration_append_acknowledged = False
            state.iteration_notification_sent = False
        else:
            if not state.iteration_active:
                state.iteration_active = True
                state.iteration_episode += 1
                state.iteration_append_acknowledged = False
                state.iteration_notification_sent = False
            iterations = health.get("iteration_count", 0)
            if not state.iteration_append_acknowledged:
                state.iteration_append_acknowledged = await self._append_escalation(
                    ticket,
                    view,
                    reason=f"iteration count exceeded cap {cap} ({iterations})",
                    prior_findings=self._open_findings(graph),
                    condition="iteration-cap",
                    episode=state.iteration_episode,
                    now=now,
                )
            if state.iteration_append_acknowledged and not state.iteration_notification_sent:
                notif = await self._emit(
                    view,
                    event_type="graph-health-iteration-cap",
                    message=(
                        f"[fleet] {ticket}: iteration count {iterations} exceeds "
                        f"cap {cap}; escalation sent to Henry."
                    ),
                    dedupe_key=(
                        f"fleet:{ticket}:graph-health:iteration-cap:{state.iteration_episode}"
                    ),
                )
                if notif is not None:
                    state.iteration_notification_sent = True
                    results.append(notif)
        return results

    @staticmethod
    def _open_findings(graph: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            finding
            for finding in _workgraph_module().collect_findings(graph).values()
            if not finding.get("resolved_by")
        ]

    async def _append_escalation(
        self,
        ticket: str,
        view: _WorkerView,
        *,
        reason: str,
        prior_findings: list[dict[str, Any]],
        condition: str,
        episode: int,
        now: float,
    ) -> bool:
        orch = view.record.orchestrator_id
        from .. import workgraph_service

        ack = workgraph_service.record_escalation(
            ticket=ticket,
            orch=orch,
            reason=reason,
            prior_findings=prior_findings,
            target="henry",
            request_id=f"fleet:{ticket}:graph-health:{condition}:{episode}",
            status_dir=self.store.paths.status_dir,
            now_ts=now,
            wait_for_delivery=True,
        )
        if ack is None:
            logger.error("fleet_monitor: escalation append was not accepted for %s", ticket)
            return False
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(ack)), timeout=self.send_timeout
            )
        except Exception as exc:
            logger.warning(
                "fleet_monitor: escalation append failed for %s (%s): %s",
                ticket,
                condition,
                exc,
            )
            return False
        return True

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
            try:
                async with self._send_semaphore(orch_agent_id):
                    await asyncio.wait_for(
                        self.send_now(
                            orch_run_id,
                            message,
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
            return Notification(
                ticket=view.record.agent_id,
                orch_agent_id=orch_agent_id,
                orch_run_id=orch_run_id,
                event_type=event_type,
                message=message,
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
