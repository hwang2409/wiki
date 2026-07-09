from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .normalizer import NormalizedProviderEvent, normalize_provider_event
from .provider import AdapterStatus, ProviderAdapter, ProviderEvent, StartRequest
from .store import RunNotFound, RunStore, StoreConflict
from .types import (
    LifecycleState,
    EventDisposition,
    ProviderKind,
    RecoveryAction,
    RunRecord,
    restart_recovery_decision,
)


AdapterFactory = Callable[[RunRecord], ProviderAdapter]


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
    ):
        self.store = store
        self.adapter_factory = adapter_factory
        self.pid_alive = pid_alive
        self.recovery_stability_seconds = recovery_stability_seconds
        self.adapters: dict[str, ProviderAdapter] = {}
        self.event_tasks: dict[str, asyncio.Task[None]] = {}
        self.event_routes: dict[tuple[int, int], str] = {}
        self.queue_locks: dict[str, asyncio.Lock] = {}
        self.agent_locks: dict[str, asyncio.Lock] = {}
        self.recovery_scan_lock = asyncio.Lock()
        self.pipeline_failures: dict[str, str] = {}
        self.expected_stream_ends: set[int] = set()
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

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

    def _agent_lock(self, agent_id: str) -> asyncio.Lock:
        return self.agent_locks.setdefault(agent_id, asyncio.Lock())

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        return self._agent_lock(self.store.get(run_id).agent_id)

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
            if record is not None:
                await self._publish_agent_change(record.agent_id)

    async def _handle_provider_event(
        self,
        run_id: str,
        adapter: ProviderAdapter,
        event: ProviderEvent,
    ) -> None:
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

        if (
            record.state is LifecycleState.IDLE
            and id(adapter) not in self.expected_stream_ends
        ):
            await self._deliver_next_queued(run_id, adapter)

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
        if not record.provider_session_id:
            raise StoreConflict("run has no provider session id")
        if self.pid_alive(record.provider_pid):
            raise StoreConflict(
                "provider PID is live without attached control; refusing duplicate resume"
            )
        resolve_safe_worktree(record.worktree)

        adapter = self.adapter_factory(record)
        self._attach_adapter(record.run_id, adapter)
        try:
            status = await adapter.resume(record.provider_session_id)
        except Exception:
            await self._close_and_drain_adapter(record.run_id, adapter)
            raise
        record = self.store.update_adapter_status(
            run_id,
            status,
            guard_automatic_resume=automatic,
        )
        if not automatic:
            record = self.store.clear_automatic_resume_suppression(run_id)
        self._route_adapter_generation(run_id, adapter, status.generation)
        await self._publish_agent_change(record.agent_id)
        return record

    async def recover_on_start(self) -> list[dict[str, str]]:
        async with self.recovery_scan_lock:
            return await self._recover_once()

    async def _recover_once(self) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for snapshot in self.store.list_runs():
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

    async def archive(self, run_id: str) -> RunRecord:
        async with self._run_lock(run_id):
            return await self._archive(run_id)

    async def _archive(self, run_id: str) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
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
        await self._publish_agent_change(record.agent_id)
        return record

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
        self.store.update_adapter_status(run_id, status)
        return self.store.clear_automatic_resume_suppression(run_id)

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
            return _public_run(await self.archive(self._resolve_run_id(params)))
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
        self.event_routes.clear()
        self.queue_locks.clear()
        self.agent_locks.clear()
        self.pipeline_failures.clear()
        self.expected_stream_ends.clear()
        self.adapters.clear()
