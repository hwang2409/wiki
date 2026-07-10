from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .. import accounts
from .normalizer import NormalizedProviderEvent, normalize_provider_event
from .process import (
    ProviderProcessStatus,
    orphaned_provider_process,
    terminate_detached_provider_pid,
)
from .provider import AdapterStatus, ProviderAdapter, ProviderEvent, StartRequest
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


AdapterFactory = Callable[[RunRecord], ProviderAdapter]
DEFAULT_REAPER_INTERVAL_SECONDS = 30.0
DEFAULT_REAPER_GRACE_SECONDS = 60.0


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
        reaper_interval_seconds: float | None = None,
        reaper_grace_seconds: float | None = None,
        orphan_archive_grace_seconds: float = 0.5,
    ):
        self.store = store
        self.adapter_factory = adapter_factory
        self.pid_alive = pid_alive
        self.recovery_stability_seconds = recovery_stability_seconds
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
        self.last_reaper_at = 0.0
        self.detached_at_monotonic: dict[str, float] = {}
        self.orphan_archive_grace_seconds = orphan_archive_grace_seconds
        self.adapters: dict[str, ProviderAdapter] = {}
        self.event_tasks: dict[str, asyncio.Task[None]] = {}
        self.event_routes: dict[tuple[int, int], str] = {}
        self.queue_locks: dict[str, asyncio.Lock] = {}
        self.agent_locks: dict[str, asyncio.Lock] = {}
        self.codex_fleet_lock = asyncio.Lock()
        self.codex_rotation_task: asyncio.Task[dict[str, Any]] | None = None
        self.codex_rotation_operation_id: str | None = None
        self.recovery_scan_lock = asyncio.Lock()
        self.pipeline_failures: dict[str, str] = {}
        self.expected_stream_ends: set[int] = set()
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.monitor_tasks: set[asyncio.Task[Any]] = set()
        self.auth_dead_attempts: dict[str, list[float]] = {}
        self.auth_dead_alert_at: dict[str, float] = {}
        self.auth_dead_recoveries: dict[str, asyncio.Task[None]] = {}
        self.last_limit_alert_at: dict[str, float] = {}
        self.last_no_eligible_alert: float = 0.0

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
        task.add_done_callback(self.monitor_tasks.discard)
        return task

    @staticmethod
    def _seconds_since(ts: float) -> float:
        return time.monotonic() - ts if ts else float("inf")

    def _current_codex_tickets(self) -> list[str]:
        tickets = {
            record.agent_id
            for record in self.store.list_runs()
            if record.provider is ProviderKind.CODEX
            and self.store.is_current(record)
            and not record.replaced_by_run_id
            and record.state not in TERMINAL_STATES
        }
        return sorted(tickets)

    def _agent_lock(self, agent_id: str) -> asyncio.Lock:
        return self.agent_locks.setdefault(agent_id, asyncio.Lock())

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        return self._agent_lock(self.store.get(run_id).agent_id)

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
                event_run_id = self.event_routes.get(
                    (id(adapter), event.generation),
                    run_id,
                )
                try:
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
            if record is not None and record.provider_pid is None:
                self._mark_adapter_loss(run_id)
            if record is not None:
                await self._publish_agent_change(record.agent_id)

    async def _handle_provider_event(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        event: ProviderEvent,
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
        self.store.append_normalized(
            run_id,
            raw_seq=int(raw["seq"]),
            disposition=normalized.disposition,
            kind=normalized.kind,
            payload=normalized.payload,
            lifecycle_state=normalized.lifecycle_state,
        )

        record = self.store.get(run_id)
        if normalized.lifecycle_state is not None:
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
        await self._publish(
            {"type": "session", "ticket": record.agent_id, "surface": "session"}
        )
        self._schedule_monitor_actions(
            run_id,
            adapter,
            event,
            prior_state=prior.state,
            record=record,
        )

        if (
            record.state is LifecycleState.IDLE
            and id(adapter) not in self.expected_stream_ends
        ):
            await self._deliver_next_queued(run_id, adapter)

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
            and accounts.detect_claude_limit_payload(event.payload)
        ):
            if self._seconds_since(self.last_limit_alert_at.get(record.agent_id, 0.0)) < 3600:
                return
            self.last_limit_alert_at[record.agent_id] = time.monotonic()
            self._spawn_monitor_task(
                self._publish(
                    {
                        "type": "claude_limit_hit",
                        "ticket": record.agent_id,
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
            await self._publish(
                {
                    "type": "codex_rotation_failed",
                    "error": str(exc),
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
        except accounts.NoEligibleAccountError:
            if self._seconds_since(self.last_no_eligible_alert) < 3600:
                return
            self.last_no_eligible_alert = time.monotonic()
            tickets = self._current_codex_tickets()
            if not tickets:
                try:
                    tickets = [self.store.get(run_id).agent_id]
                except RunNotFound:
                    tickets = []
            await self._publish(
                {
                    "type": "codex_limit_no_eligible",
                    "tickets": tickets,
                    "reset_at": outgoing_reset_at,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
        except accounts.RotationError as exc:
            await self._publish(
                {
                    "type": "codex_rotation_failed",
                    "error": str(exc),
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
        history = self.auth_dead_attempts.setdefault(initial.agent_id, [])
        history[:] = [
            ts
            for ts in history
            if now_mono - ts < accounts.AUTH_DEAD_WINDOW_SECONDS
        ]
        if history and now_mono - history[-1] < accounts.AUTH_DEAD_COOLDOWN_SECONDS:
            await self._publish_auth_dead_exhausted(initial.agent_id, now_mono)
            return
        if len(history) >= accounts.AUTH_DEAD_MAX_ATTEMPTS:
            await self._publish_auth_dead_exhausted(initial.agent_id, now_mono)
            return
        history.append(now_mono)

        operation_id = str(uuid4())
        revived: list[str] = []
        failed: list[str] = []
        failed_reasons: dict[str, str] = {}

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
                await self._resume_run(run_id, automatic=False)
                revived.append(record.agent_id)
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
                else:
                    failed.append(initial.agent_id)
                    failed_reasons[initial.agent_id] = str(exc)

        if revived or failed:
            await self._publish(
                {
                    "type": "codex_auth_dead_revival",
                    "revived": revived,
                    "failed": failed,
                    "failed_reasons": failed_reasons,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )

    async def _publish_auth_dead_exhausted(
        self,
        agent_id: str,
        now_mono: float,
    ) -> None:
        if (
            now_mono - self.auth_dead_alert_at.get(agent_id, 0.0)
            < accounts.AUTH_DEAD_ALERT_INTERVAL_SECONDS
        ):
            return
        self.auth_dead_alert_at[agent_id] = now_mono
        await self._publish(
            {
                "type": "codex_auth_dead_exhausted",
                "tickets": [agent_id],
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def _deliver_next_queued(self, run_id: str, adapter: ProviderAdapter) -> None:
        async with self._run_lock(run_id):
            await self._deliver_next_queued_locked(run_id, adapter)

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
            queued = self.store.peek_queued_message(run_id)
            if queued is None:
                return
            try:
                status = await adapter.send_on_idle(queued["text"])
            except Exception as exc:
                # Provider refusal/races are delivery failures, not event
                # persistence failures. Retain the durable message for the
                # next idle edge and keep the provider control stream alive.
                record = self.store.get(run_id)
                record = self.store.transition(
                    run_id,
                    record.state,
                    reason=f"queued message delivery failed: {exc}",
                )
                await self._publish(
                    {"type": "session", "ticket": record.agent_id, "surface": "queue"}
                )
                return
            self.store.pop_queued_message(run_id)
            record = self.store.update_adapter_status(run_id, status)
            record = self.store.clear_automatic_resume_suppression(run_id)
            await self._publish(
                {"type": "session", "ticket": record.agent_id, "surface": "queue"}
            )

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
        self.event_tasks[run_id] = asyncio.create_task(
            self._pump_events(run_id, adapter),
            name=f"agent-events-{run_id}",
        )

    async def _detach_adapter(
        self, run_id: str, *, preserve_event_routes: bool = False
    ) -> None:
        self._clear_adapter_loss(run_id)
        adapter = self.adapters.pop(run_id, None)
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
    ) -> RunRecord:
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
        )
        if provider is ProviderKind.CODEX:
            self._assert_codex_fleet_available()
            async with self.codex_fleet_lock:
                self._assert_codex_fleet_available()
                async with self._agent_lock(record.agent_id):
                    return await self._start_run(
                        record=record,
                        prompt=prompt,
                        migrate_legacy=migrate_legacy,
                    )
        async with self._agent_lock(record.agent_id):
            return await self._start_run(
                record=record,
                prompt=prompt,
                migrate_legacy=migrate_legacy,
            )

    async def _start_run(
        self,
        *,
        record: RunRecord,
        prompt: str,
        migrate_legacy: bool = False,
    ) -> RunRecord:
        self.store.create(record, migrate_legacy=migrate_legacy)
        return await self._launch_record(record, prompt)

    async def _launch_record(self, record: RunRecord, prompt: str) -> RunRecord:
        adapter = self.adapter_factory(record)
        self._attach_adapter(record.run_id, adapter)
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
        except Exception as exc:
            await self._close_and_drain_adapter(record.run_id, adapter)
            record = self.store.transition(
                record.run_id,
                LifecycleState.DEAD,
                reason=f"provider start failed: {exc}",
            )
            await self._publish_agent_change(record.agent_id)
            raise
        await self._publish_agent_change(record.agent_id)
        return record

    async def resume_run(self, run_id: str) -> RunRecord:
        if self.store.get(run_id).provider is ProviderKind.CODEX:
            self._assert_codex_fleet_available()
            async with self.codex_fleet_lock:
                self._assert_codex_fleet_available()
                async with self._run_lock(run_id):
                    return await self._resume_run(run_id, automatic=False)
        async with self._run_lock(run_id):
            return await self._resume_run(run_id, automatic=False)

    async def _resume_run(self, run_id: str, *, automatic: bool) -> RunRecord:
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
        if recovery_state not in {LifecycleState.WORKING, LifecycleState.IDLE}:
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
        record = self.store.clear_pending_requests(run_id)

        adapter = self.adapter_factory(record)
        self._attach_adapter(record.run_id, adapter)
        try:
            status = await adapter.resume(session_id)
        except Exception:
            await self._close_and_drain_adapter(record.run_id, adapter)
            raise
        record = self.store.update_adapter_status(
            run_id,
            status,
            guard_automatic_resume=automatic,
        )
        if quiesce_operation_id is not None:
            record = self.store.clear_quiesce_marker(
                run_id,
                quiesce_operation_id,
            )
        elif not automatic:
            record = self.store.clear_automatic_resume_suppression(run_id)
        self._route_adapter_generation(run_id, adapter, status.generation)
        await self._publish_agent_change(record.agent_id)
        return record

    async def recover_on_start(self) -> list[dict[str, str]]:
        async with self.recovery_scan_lock:
            results = await self._recover_once()
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
        failed: list[str] = []
        failed_reasons: dict[str, str] = {}
        pending = False
        operation_id = str(journal["operation_id"])
        for row in journal["runs"]:
            if row.get("resumed") or row.get("skipped"):
                if row.get("resumed"):
                    revived.append(row["agent_id"])
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
                        continue
                    if self.pid_alive(record.provider_pid):
                        pending = True
                        continue
                    await self._resume_run(run_id, automatic=False)
                    row["resumed"] = True
                    row["failed_reason"] = None
                    revived.append(agent_id)
                except Exception as exc:
                    reason = str(exc)
                    row["failed_reason"] = reason
                    failed.append(agent_id)
                    failed_reasons[agent_id] = reason
                finally:
                    self._write_rotation_journal(journal)
        return {
            "revived": revived,
            "failed": failed,
            "failed_reasons": failed_reasons,
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
                await self._resume_run(record.run_id, automatic=True)
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

    async def send_now(self, run_id: str, message: str) -> dict[str, Any]:
        async with self._run_lock(run_id):
            return await self._send_now(run_id, message)

    async def _send_now(self, run_id: str, message: str) -> dict[str, Any]:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
        status = await adapter.send_now(message)
        record = self.store.update_adapter_status(run_id, status)
        record = self.store.clear_automatic_resume_suppression(run_id)
        await self._publish_agent_change(record.agent_id)
        return {"status": "sent"}

    async def send_on_idle(self, run_id: str, message: str) -> dict[str, Any]:
        async with self._run_lock(run_id):
            return await self._send_on_idle(run_id, message)

    async def _send_on_idle(self, run_id: str, message: str) -> dict[str, Any]:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
        record = self.store.queue_message(run_id, message)
        response = {
            "status": "queued",
            "position": len(record.queued_messages),
            "messages": list(record.queued_messages),
        }
        await self._publish(
            {"type": "session", "ticket": record.agent_id, "surface": "queue"}
        )
        status = await adapter.status()
        if status.state is LifecycleState.IDLE:
            await self._deliver_next_queued_locked(run_id, adapter)
        return response

    async def delete_queued(self, run_id: str, index: int) -> dict[str, Any]:
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
        await self._publish_agent_change(record.agent_id)
        return record

    async def archive(
        self,
        run_id: str,
        *,
        outcome: str | None = None,
    ) -> RunRecord:
        async with self._run_lock(run_id):
            return await self._archive(run_id, outcome=outcome)

    async def _archive(
        self,
        run_id: str,
        *,
        outcome: str | None = None,
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
            self._clear_adapter_loss(run_id)
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
        self._clear_adapter_loss(run_id)
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
            await self._publish_agent_change(record.agent_id)
        except Exception:
            # Preserve the original provider/control error for the caller. A
            # concurrent terminal transition is already non-resumable.
            pass

    async def replace(
        self, run_id: str, prompt: str, model: str | None = None
    ) -> RunRecord:
        if self.store.get(run_id).provider is ProviderKind.CODEX:
            self._assert_codex_fleet_available()
            async with self.codex_fleet_lock:
                self._assert_codex_fleet_available()
                async with self._run_lock(run_id):
                    return await self._replace(run_id, prompt, model)
        async with self._run_lock(run_id):
            return await self._replace(run_id, prompt, model)

    async def _replace(
        self,
        run_id: str,
        prompt: str,
        model: str | None = None,
    ) -> RunRecord:
        old = self.store.get(run_id)
        if not self.store.is_current(old):
            raise StoreConflict("replacement target is no longer current")
        resolve_safe_worktree(old.worktree)
        old_adapter = self.adapters.get(run_id)
        if old_adapter is None:
            if self.pid_alive(old.provider_pid):
                raise StoreConflict(
                    "replacement target has a live PID without attached control"
                )
            replacement = RunRecord.new(
                agent_id=old.agent_id,
                provider=old.provider,
                role=old.role,
                model=model or old.model,
                worktree=old.worktree,
                prompt=prompt,
                effort=old.effort,
                orchestrator_id=old.orchestrator_id,
                replaces_run_id=old.run_id,
            )
            self.store.replace(old.run_id, replacement)
            return await self._launch_record(replacement, prompt)
        await self._detach_adapter(run_id, preserve_event_routes=True)

        try:
            status = await old_adapter.replace(prompt, model)
        except Exception as exc:
            await self._close_and_drain_adapter(run_id, old_adapter)
            try:
                self.store.transition(
                    run_id,
                    LifecycleState.BLOCKED,
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
            except (ValueError, StoreConflict):
                pass
            await self._publish_agent_change(old.agent_id)
            raise

        replacement = RunRecord.new(
            agent_id=old.agent_id,
            provider=old.provider,
            role=old.role,
            model=model or old.model,
            worktree=old.worktree,
            prompt=prompt,
            effort=old.effort,
            orchestrator_id=old.orchestrator_id,
            replaces_run_id=old.run_id,
        )
        # The replacement run file is the crash-recovery authority. Seed it
        # with live provider identity before the multi-file registry swap, so
        # reconciliation can exact-session resume after a partial commit.
        replacement.state = status.state
        replacement.state_reason = status.detail
        replacement.provider_session_id = status.session_id
        replacement.provider_pid = status.pid
        replacement.provider_generation = status.generation
        replacement.active_turn_id = status.active_turn_id
        replacement.transcript_path = status.transcript_path
        try:
            self.store.replace(old.run_id, replacement)
        except Exception as exc:
            try:
                self.store.get(replacement.run_id)
            except RunNotFound:
                pass
            else:
                self._route_adapter_generation(
                    replacement.run_id,
                    old_adapter,
                    status.generation,
                )
            await self._close_and_drain_adapter(
                old.run_id,
                old_adapter,
                finalize="stop",
            )
            try:
                current = self.store.get(old.run_id)
                failure_state = (
                    LifecycleState.DEAD
                    if current.state is LifecycleState.DEAD
                    else LifecycleState.BLOCKED
                )
                self.store.transition(
                    old.run_id,
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
            except Exception:
                pass
            await self._publish_agent_change(old.agent_id)
            raise
        self._route_adapter_generation(
            replacement.run_id, old_adapter, status.generation
        )
        self._attach_adapter(replacement.run_id, old_adapter)
        replacement = self.store.get(replacement.run_id)
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

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"status": "ok", "pid": os.getpid()}
        if method == "run/start":
            migrate_legacy = params.get("migrate_legacy", False)
            if not isinstance(migrate_legacy, bool):
                raise ValueError("migrate_legacy must be a boolean")
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
            )
            return _public_run(record)
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
                self._resolve_run_id(params), str(params["text"])
            )
        if method == "run/send_on_idle":
            return await self.send_on_idle(
                self._resolve_run_id(params), str(params["text"])
            )
        if method == "run/queue":
            run_id = self._resolve_run_id(params)
            return {"messages": self.store.queued_messages(run_id)}
        if method == "run/queue/delete":
            index = params.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                raise ValueError("queue index must be an integer")
            return await self.delete_queued(self._resolve_run_id(params), index)
        if method == "run/interrupt":
            return _public_run(await self.interrupt(self._resolve_run_id(params)))
        if method == "run/stop":
            return _public_run(await self.stop(self._resolve_run_id(params)))
        if method == "run/archive":
            outcome = params.get("outcome")
            if outcome is not None and not isinstance(outcome, str):
                raise ValueError("outcome must be a string or null")
            return _public_run(
                await self.archive(
                    self._resolve_run_id(params),
                    outcome=outcome,
                )
            )
        if method == "run/replace":
            return _public_run(
                await self.replace(
                    self._resolve_run_id(params),
                    str(params["prompt"]),
                    params.get("model"),
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
            await asyncio.gather(*self.monitor_tasks, return_exceptions=True)
        self.monitor_tasks.clear()
        self.event_routes.clear()
        self.queue_locks.clear()
        self.agent_locks.clear()
        self.pipeline_failures.clear()
        self.expected_stream_ends.clear()
        self.auth_dead_attempts.clear()
        self.auth_dead_alert_at.clear()
        self.auth_dead_recoveries.clear()
        self.last_limit_alert_at.clear()
        self.last_no_eligible_alert = 0.0
        self.detached_at_monotonic.clear()
        self.adapters.clear()
