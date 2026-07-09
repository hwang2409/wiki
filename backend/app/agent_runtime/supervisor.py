from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .normalizer import NormalizedProviderEvent, normalize_provider_event
from .provider import ProviderAdapter, ProviderEvent, StartRequest
from .store import RunNotFound, RunStore, StoreConflict
from .types import (
    LifecycleState,
    EventDisposition,
    ProviderKind,
    RecoveryAction,
    RunRecord,
    restart_recovery_decision,
)


AdapterFactory = Callable[[ProviderKind], ProviderAdapter]


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
    ):
        self.store = store
        self.adapter_factory = adapter_factory
        self.pid_alive = pid_alive
        self.adapters: dict[str, ProviderAdapter] = {}
        self.event_tasks: dict[str, asyncio.Task[None]] = {}
        self.event_routes: dict[tuple[int, int], str] = {}
        self.queue_locks: dict[str, asyncio.Lock] = {}
        self.recovery_lock = asyncio.Lock()
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

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
        await self._publish({"type": "agents", "tickets": [agent_id], "surface": "agents"})

    async def _pump_events(self, run_id: str, adapter: ProviderAdapter) -> None:
        try:
            async for event in adapter.events():
                event_run_id = self.event_routes.get(
                    (id(adapter), event.generation),
                    run_id,
                )
                await self._handle_provider_event(event_run_id, adapter, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                record = self.store.transition(
                    run_id,
                    LifecycleState.DEAD,
                    reason=f"provider event stream failed: {exc}",
                )
            except (RunNotFound, ValueError):
                return
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
            received_at=event.received_at,
        )
        try:
            normalized = normalize_provider_event(event.provider, event.payload)
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
            await self._publish_agent_change(record.agent_id)

        # Preserve the existing external SSE invalidation contract. The backend
        # will proxy these dictionaries unchanged when it switches transports.
        await self._publish({"type": "session", "ticket": record.agent_id, "surface": "session"})

        if record.state is LifecycleState.IDLE:
            await self._deliver_next_queued(run_id, adapter)

    async def _deliver_next_queued(self, run_id: str, adapter: ProviderAdapter) -> None:
        """Acknowledge a durable on-idle message only after provider acceptance."""

        lock = self.queue_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            queued = self.store.peek_queued_message(run_id)
            if queued is None:
                return
            status = await adapter.send_on_idle(queued["text"])
            self.store.pop_queued_message(run_id)
            record = self.store.update_adapter_status(run_id, status)
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

    def _attach_adapter(self, run_id: str, adapter: ProviderAdapter) -> None:
        old_task = self.event_tasks.pop(run_id, None)
        if old_task is not None:
            old_task.cancel()
        self.adapters[run_id] = adapter
        self.event_tasks[run_id] = asyncio.create_task(
            self._pump_events(run_id, adapter),
            name=f"agent-events-{run_id}",
        )

    async def _detach_adapter(self, run_id: str, *, preserve_event_routes: bool = False) -> None:
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
        self.store.create(record)
        adapter = self.adapter_factory(provider)
        self._attach_adapter(record.run_id, adapter)
        request = StartRequest(
            prompt=prompt,
            model=model,
            effort=effort,
            worktree=str(resolved),
            run_id=record.run_id,
            agent_id=agent_id,
        )
        try:
            status = await adapter.start(request)
            record = self.store.update_adapter_status(record.run_id, status)
            self._route_adapter_generation(record.run_id, adapter, status.generation)
        except Exception as exc:
            await self._detach_adapter(record.run_id)
            record = self.store.transition(
                record.run_id,
                LifecycleState.DEAD,
                reason=f"provider start failed: {exc}",
            )
            await self._publish_agent_change(agent_id)
            raise
        await self._publish_agent_change(agent_id)
        return record

    async def resume_run(self, run_id: str) -> RunRecord:
        # Re-read immediately before resume so stale recovery snapshots cannot
        # revive a deregistered, terminal, or replaced run (PR #31 invariant).
        record = self.store.get(run_id)
        if not self.store.is_current(record):
            raise StoreConflict("run is no longer current")
        if record.replaced_by_run_id:
            raise StoreConflict("run was replaced")
        if record.state in {LifecycleState.DEAD, LifecycleState.COMPLETED}:
            raise StoreConflict(f"{record.state.value} runs never resume")
        if not record.provider_session_id:
            raise StoreConflict("run has no provider session id")
        resolve_safe_worktree(record.worktree)

        adapter = self.adapter_factory(record.provider)
        self._attach_adapter(record.run_id, adapter)
        try:
            status = await adapter.resume(record.provider_session_id)
        except Exception:
            await self._detach_adapter(record.run_id)
            raise
        record = self.store.update_adapter_status(run_id, status)
        self._route_adapter_generation(run_id, adapter, status.generation)
        await self._publish_agent_change(record.agent_id)
        return record

    async def recover_on_start(self) -> list[dict[str, str]]:
        async with self.recovery_lock:
            return await self._recover_once()

    async def _recover_once(self) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for snapshot in self.store.list_runs():
            # Decision inputs are refreshed per run; no stale list snapshot can
            # override a replacement that happened while recovery was running.
            record = self.store.get(snapshot.run_id)
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
                try:
                    await self.resume_run(record.run_id)
                except Exception as exc:
                    try:
                        self.store.transition(
                            record.run_id,
                            LifecycleState.BLOCKED,
                            reason=f"automatic resume failed: {exc}",
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
            results.append(result)
        return results

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
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
        status = await adapter.send_now(message)
        record = self.store.update_adapter_status(run_id, status)
        await self._publish_agent_change(record.agent_id)
        return {"status": "sent"}

    async def send_on_idle(self, run_id: str, message: str) -> dict[str, Any]:
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
            await self._deliver_next_queued(run_id, adapter)
        return response

    async def interrupt(self, run_id: str) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
        status = await adapter.interrupt()
        record = self.store.update_adapter_status(run_id, status)
        await self._publish_agent_change(record.agent_id)
        return record

    async def stop(self, run_id: str) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            record = self.store.get(run_id)
            if record.state not in {LifecycleState.DEAD, LifecycleState.COMPLETED}:
                record = self.store.transition(run_id, LifecycleState.DEAD, reason="stopped")
            return record
        status = await adapter.stop()
        record = self.store.update_adapter_status(run_id, status)
        await self._publish_agent_change(record.agent_id)
        return record

    async def archive(self, run_id: str) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
        status = await adapter.archive()
        record = self.store.update_adapter_status(run_id, status)
        await self._publish_agent_change(record.agent_id)
        return record

    async def replace(self, run_id: str, prompt: str, model: str | None = None) -> RunRecord:
        old = self.store.get(run_id)
        if not self.store.is_current(old):
            raise StoreConflict("replacement target is no longer current")
        resolve_safe_worktree(old.worktree)
        old_adapter = self.adapters.get(run_id)
        if old_adapter is None:
            raise StoreConflict("replacement target has no attached provider adapter")
        await self._detach_adapter(run_id, preserve_event_routes=True)

        try:
            status = await old_adapter.replace(prompt, model)
        except Exception as exc:
            self._attach_adapter(run_id, old_adapter)
            try:
                adapter_status = await old_adapter.status()
                self.store.update_adapter_status(run_id, adapter_status)
                self.store.transition(
                    run_id,
                    LifecycleState.BLOCKED,
                    reason=f"provider replacement failed: {exc}",
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
        try:
            self.store.replace(old.run_id, replacement)
        except Exception:
            await old_adapter.stop()
            raise
        self._route_adapter_generation(replacement.run_id, old_adapter, status.generation)
        self._attach_adapter(replacement.run_id, old_adapter)
        replacement = self.store.update_adapter_status(replacement.run_id, status)
        await self._publish_agent_change(replacement.agent_id)
        return replacement

    async def respond(self, run_id: str, request_id: str, response: dict[str, Any]) -> RunRecord:
        adapter = self.adapters.get(run_id)
        if adapter is None:
            raise StoreConflict("run has no attached provider adapter")
        status = await adapter.respond(request_id, response)
        return self.store.update_adapter_status(run_id, status)

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"status": "ok", "pid": os.getpid()}
        if method == "run/start":
            record = await self.start_run(
                agent_id=str(params["agent_id"]),
                provider=ProviderKind(params["provider"]),
                role=str(params["role"]),
                model=str(params["model"]),
                worktree=str(params["worktree"]),
                prompt=str(params["prompt"]),
                effort=params.get("effort"),
                orchestrator_id=params.get("orchestrator_id"),
            )
            return _public_run(record)
        if method == "run/list":
            return {"runs": [_public_run(record) for record in self.store.list_runs()]}
        if method == "run/status":
            return _public_run(self.store.get(self._resolve_run_id(params)))
        if method == "run/resume":
            return _public_run(await self.resume_run(self._resolve_run_id(params)))
        if method == "run/send_now":
            return await self.send_now(self._resolve_run_id(params), str(params["text"]))
        if method == "run/send_on_idle":
            return await self.send_on_idle(self._resolve_run_id(params), str(params["text"]))
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
            return _public_run(
                await self.respond(
                    self._resolve_run_id(params),
                    str(params["request_id"]),
                    dict(params.get("response") or {}),
                )
            )
        if method == "events/read":
            run_id = self._resolve_run_id(params)
            return {
                "run_id": run_id,
                "events": self.store.read_normalized_events(run_id),
                "raw": self.store.read_raw_events(run_id) if params.get("include_raw") else None,
            }
        if method == "supervisor/recover":
            return {"runs": await self.recover_on_start()}
        raise ValueError(f"unknown supervisor method: {method}")

    async def close(self) -> None:
        for task in self.event_tasks.values():
            task.cancel()
        if self.event_tasks:
            await asyncio.gather(*self.event_tasks.values(), return_exceptions=True)
        self.event_tasks.clear()
        self.event_routes.clear()
        self.queue_locks.clear()
        self.adapters.clear()
