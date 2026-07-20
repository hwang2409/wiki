"""Supervisor-native fleet monitor.

Watches every live worker and pushes state-transition notifications to the
owning orchestrator session via ``send_now``. Also fires the fleet-doctrine
re-alarms (unrouted verdict, review-gap, staleness). Dedupe is in-memory
per (ticket, event-type, payload); supervisor restarts intentionally clear
that memory (the ticket accepts re-emit as a tradeoff).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .store import RunStore
from .types import LifecycleState, RunRecord, TERMINAL_STATES


SendNow = Callable[[str, str, str | None], Awaitable[Any]]


DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_UNROUTED_VERDICT_REALARM_SECONDS = 300.0
DEFAULT_REVIEW_GAP_THRESHOLD_SECONDS = 300.0
DEFAULT_REVIEW_GAP_REALARM_SECONDS = 600.0
DEFAULT_STALENESS_THRESHOLD_SECONDS = 1800.0
DEFAULT_SEND_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_CONCURRENT_SENDS = 4


logger = logging.getLogger(__name__)


@dataclass
class _WorkerSnapshot:
    """Last observed state for one worker; used to detect transitions."""

    run_id: str | None = None
    status_state: str | None = None
    runtime_state: LifecycleState | None = None
    pr: str | None = None
    step: str | None = None
    blocker: str | None = None
    status_mtime: float | None = None
    merge_ready_since: float | None = None
    last_unrouted_verdict_alarm_at: float | None = None
    last_review_gap_alarm_at: float | None = None
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
    """Merged view of one worker: run record + on-disk status."""

    record: RunRecord
    status_state: str | None
    pr: str | None
    step: str | None
    blocker: str | None
    status_mtime: float | None
    status_file_present: bool


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


def _created_at_timestamp(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


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

    def __init__(
        self,
        store: RunStore,
        send_now: SendNow,
        *,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] | None = None,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        unrouted_verdict_realarm: float = DEFAULT_UNROUTED_VERDICT_REALARM_SECONDS,
        review_gap_threshold: float = DEFAULT_REVIEW_GAP_THRESHOLD_SECONDS,
        review_gap_realarm: float = DEFAULT_REVIEW_GAP_REALARM_SECONDS,
        staleness_threshold: float = DEFAULT_STALENESS_THRESHOLD_SECONDS,
        send_timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
        max_concurrent_sends: int = DEFAULT_MAX_CONCURRENT_SENDS,
    ):
        self.store = store
        self.send_now = send_now
        self.wall_clock = clock
        # Keep the old injected ``clock`` useful for deterministic callers,
        # while the daemon uses a real monotonic clock for elapsed timers.
        self.monotonic_clock = (
            monotonic_clock
            if monotonic_clock is not None
            else (time.monotonic if clock is time.time else clock)
        )
        self.interval = interval
        self.unrouted_verdict_realarm = unrouted_verdict_realarm
        self.review_gap_threshold = review_gap_threshold
        self.review_gap_realarm = review_gap_realarm
        self.staleness_threshold = staleness_threshold
        if send_timeout <= 0:
            raise ValueError("send_timeout must be positive")
        if max_concurrent_sends < 1:
            raise ValueError("max_concurrent_sends must be positive")
        self.send_timeout = send_timeout
        self.max_concurrent_sends = max_concurrent_sends
        self._snapshots: dict[str, _WorkerSnapshot] = {}
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
            if (
                status_data is not None
                and mtime is not None
                and (created_at := _created_at_timestamp(record.created_at)) is not None
                and mtime < created_at
            ):
                status_data = None
            if status_data is None:
                views.append(
                    _WorkerView(
                        record=record,
                        status_state=None,
                        pr=None,
                        step=None,
                        blocker=None,
                        status_mtime=mtime,
                        status_file_present=mtime is not None,
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
                    status_file_present=True,
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
            if view.status_state == "merge-ready":
                snapshot.merge_ready_since = monotonic_now
            snapshot.seeded = True

        current_status_context = self._status_context_tuple(view)
        prior_status_context = self._snapshot_status_context(snapshot)
        if current_status_context != prior_status_context:
            dedupe_key = self._transition_dedupe_key(
                view,
                event_type="status-transition",
                snapshot=snapshot,
            )
            notif = await self._emit(
                view,
                event_type="status-transition",
                message=self._status_transition_message(view, snapshot),
                dedupe_key=dedupe_key,
            )
            if notif is not None:
                results.append(notif)
            if notif is not None or self._dedupe_was_sent(view, dedupe_key):
                self._apply_status_context(snapshot, current_status_context)
                self._clear_dedupe_key(view, dedupe_key)

        if record.state != snapshot.runtime_state:
            dedupe_key = self._transition_dedupe_key(
                view,
                event_type="runtime-transition",
                snapshot=snapshot,
            )
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
                self._clear_dedupe_key(view, dedupe_key)

        if view.status_state == "merge-ready":
            if snapshot.merge_ready_since is None:
                snapshot.merge_ready_since = monotonic_now
        else:
            snapshot.merge_ready_since = None
            snapshot.last_review_gap_alarm_at = None

        results.extend(
            await self._maybe_unrouted_verdict(view, snapshot, monotonic_now)
        )
        results.extend(
            await self._maybe_review_gap(
                view, snapshot, all_views, monotonic_now
            )
        )
        results.extend(await self._maybe_staleness(view, snapshot, wall_now))

        snapshot.status_mtime = view.status_mtime
        self._snapshots[record.agent_id] = snapshot
        return results

    def _transition_dedupe_key(
        self,
        view: _WorkerView,
        *,
        event_type: str,
        snapshot: _WorkerSnapshot,
    ) -> str:
        prior_status, prior_step, prior_pr, prior_blocker = (
            self._snapshot_status_context(snapshot)
        )
        payload = {
            "from_status": prior_status,
            "from_step": prior_step,
            "from_pr": prior_pr,
            "from_blocker": prior_blocker,
            "from_runtime": (
                snapshot.runtime_state.value if snapshot.runtime_state else None
            ),
            "status": view.status_state,
            "runtime": view.record.state.value,
            "pr": view.pr,
            "step": view.step,
            "blocker": view.blocker,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"fleet:{view.record.agent_id}:{event_type}:{encoded}"

    @staticmethod
    def _status_context_tuple(
        view: _WorkerView,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        return (view.status_state, view.step, view.pr, view.blocker)

    @staticmethod
    def _snapshot_status_context(
        snapshot: _WorkerSnapshot,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        return (
            snapshot.status_state,
            snapshot.step,
            snapshot.pr,
            snapshot.blocker,
        )

    @staticmethod
    def _apply_status_context(
        snapshot: _WorkerSnapshot,
        context: tuple[str | None, str | None, str | None, str | None],
    ) -> None:
        (
            snapshot.status_state,
            snapshot.step,
            snapshot.pr,
            snapshot.blocker,
        ) = context

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

    async def _maybe_review_gap(
        self,
        view: _WorkerView,
        snapshot: _WorkerSnapshot,
        all_views: list[_WorkerView],
        now: float,
    ) -> list[Notification]:
        record = view.record
        if record.role != "implement":
            return []
        if view.status_state != "merge-ready":
            return []
        since = snapshot.merge_ready_since
        if since is None:
            return []
        if (now - since) < self.review_gap_threshold:
            return []
        prefix = _ticket_prefix(record.agent_id)
        has_reviewer = any(
            other.record.role == "review"
            and other.record.orchestrator_id == record.orchestrator_id
            and _ticket_prefix(other.record.agent_id) == prefix
            and other.record.agent_id != record.agent_id
            for other in all_views
        )
        if has_reviewer:
            snapshot.last_review_gap_alarm_at = None
            return []
        last = snapshot.last_review_gap_alarm_at
        if last is not None and (now - last) < self.review_gap_realarm:
            return []
        window = int(now // max(self.review_gap_realarm, 1.0))
        elapsed_min = int((now - since) // 60)
        notif = await self._emit(
            view,
            event_type="review-gap",
            message=(
                f"[fleet] {record.agent_id} ({record.role}): merge-ready "
                f"{elapsed_min}m with no live reviewer for {prefix}. "
                "Spawn a reviewer or route the PR."
            ),
            dedupe_key=f"fleet:{record.agent_id}:review-gap:{window}",
        )
        if notif is not None:
            snapshot.last_review_gap_alarm_at = now
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
        # Supervisor.send_now persists its dedupe key. Fleet-monitor delivery
        # is intentionally local: a later cycle with new step/PR context must
        # not be swallowed by an old supervisor key.
        dedupe_identity = self._dedupe_identity(view, dedupe_key)
        if dedupe_identity in self._sent_dedupe_keys:
            return None
        try:
            async with self._send_semaphore(orch_agent_id):
                await asyncio.wait_for(
                    self.send_now(orch_run_id, message, None),
                    timeout=self.send_timeout,
                )
        except Exception as exc:
            # A dead orchestrator adapter or a race with archive must not
            # break the monitor loop; log for the supervisor operator.
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
