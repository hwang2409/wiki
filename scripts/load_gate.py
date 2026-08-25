#!/usr/bin/env python3
"""WIKI-381 load gate: supervisor latency SLOs under synthetic fleet load.

Drives an isolated in-process supervisor with N synthetic runs producing
sustained provider event bursts, round-robin accepted-mode steers, one
archive, and a concurrent ping loop. Fails loudly with the offending
percentile when an SLO regresses:

- ping p99 < 50ms while the fleet is busy
- steer accepted p99 < 500ms

Usage: load_gate.py [--duration SECONDS] [--runs N]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.agent_runtime.provider import (  # noqa: E402
    AdapterStatus,
    BoundedProviderEventQueue,
    ProviderAdapter,
    ProviderEvent,
    StartRequest,
)
from backend.app.agent_runtime.store import RunStore, RuntimePaths  # noqa: E402
from backend.app.agent_runtime.supervisor import Supervisor  # noqa: E402
from backend.app.agent_runtime.types import (  # noqa: E402
    LifecycleState,
    ProviderKind,
)

PING_P99_BUDGET_MS = 50.0
STEER_ACCEPTED_P99_BUDGET_MS = 500.0
EVENTS_PER_RUN_PER_SECOND = 125  # 8 runs -> 1k events/s aggregate
STEERS_PER_SECOND = 5
PINGS_PER_SECOND = 10
EVENT_PAYLOAD = {"method": "item/updated", "params": {"item": {"text": "x" * 512}}}


class SyntheticAdapter(ProviderAdapter):
    """Steady-state provider that emits load on demand."""

    def __init__(self, record) -> None:
        self.record = record
        self._events = BoundedProviderEventQueue()
        self._generation = 1
        self._status = AdapterStatus(
            LifecycleState.STARTING, None, os.getpid(), generation=1
        )
        self._closed = False

    async def emit_payload(self, payload: dict) -> None:
        await self._events.put(
            ProviderEvent(
                ProviderKind.CODEX,
                payload,
                direction="server",
                generation=self._generation,
            ),
            size=600,
        )

    def _set(self, state: LifecycleState) -> AdapterStatus:
        self._status = AdapterStatus(
            state,
            self._status.session_id,
            os.getpid(),
            generation=self._generation,
        )
        return self._status

    async def start(self, request: StartRequest) -> AdapterStatus:
        self._status = AdapterStatus(
            LifecycleState.IDLE,
            f"synthetic-{request.run_id}",
            os.getpid(),
            generation=self._generation,
        )
        return self._status

    async def resume(self, session_id: str) -> AdapterStatus:
        self._status = AdapterStatus(
            LifecycleState.IDLE,
            session_id,
            os.getpid(),
            generation=self._generation,
        )
        return self._status

    async def send_now(self, message: str) -> AdapterStatus:
        await self.emit_payload(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": message}],
                    }
                },
            }
        )
        return self._set(LifecycleState.WORKING)

    async def send_on_idle(self, message: str) -> AdapterStatus:
        return await self.send_now(message)

    async def interrupt(self) -> AdapterStatus:
        return self._set(LifecycleState.INTERRUPTED)

    async def stop(self) -> AdapterStatus:
        return self._set(LifecycleState.DEAD)

    async def replace(
        self,
        new_prompt: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> AdapterStatus:
        return self._set(LifecycleState.IDLE)

    async def status(self) -> AdapterStatus:
        return self._status

    def snapshot(self) -> AdapterStatus:
        return self._status

    async def events(self) -> AsyncIterator[ProviderEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def archive(self) -> AdapterStatus:
        status = self._set(LifecycleState.COMPLETED)
        self._events.put_forced(None)
        return status

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._events.put_forced(None)


def _percentile(samples: list[float], fraction: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


async def run_gate(duration: float, run_count: int) -> int:
    tmp = tempfile.TemporaryDirectory(prefix="wiki-load-gate-")
    root = Path(tmp.name)
    paths = RuntimePaths(
        runtime_dir=root / "runtime",
        socket_path=root / "runtime" / "supervisor.sock",
        registry_path=root / "registry.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )
    store = RunStore(paths)
    adapters: dict[str, SyntheticAdapter] = {}

    def factory(record) -> SyntheticAdapter:
        adapter = SyntheticAdapter(record)
        adapters[record.agent_id] = adapter
        return adapter

    supervisor = Supervisor(store, factory)
    agent_ids = [f"LOAD-{index}" for index in range(1, run_count + 1)]
    for agent_id in agent_ids:
        await supervisor.dispatch(
            "run/start",
            {
                "agent_id": agent_id,
                "provider": "codex",
                "role": "implement",
                "model": "synthetic",
                "effort": "high",
                "worktree": str(root),
                "prompt": f"load gate {agent_id}",
                "request_id": f"load-start-{agent_id}",
            },
        )

    stop = asyncio.Event()
    ping_ms: list[float] = []
    steer_ms: list[float] = []
    errors: list[str] = []

    async def burst_loop(adapter: SyntheticAdapter) -> None:
        interval = 1.0 / EVENTS_PER_RUN_PER_SECOND
        while not stop.is_set():
            await adapter.emit_payload(EVENT_PAYLOAD)
            await asyncio.sleep(interval)

    async def ping_loop() -> None:
        while not stop.is_set():
            started = time.perf_counter()
            try:
                await supervisor.dispatch("ping", {})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ping: {exc}")
            ping_ms.append((time.perf_counter() - started) * 1000)
            await asyncio.sleep(1.0 / PINGS_PER_SECOND)

    async def steer_loop() -> None:
        counter = 0
        while not stop.is_set():
            agent_id = agent_ids[counter % len(agent_ids)]
            counter += 1
            started = time.perf_counter()
            try:
                response = await supervisor.dispatch(
                    "run/send_now",
                    {
                        "agent_id": agent_id,
                        "message": f"steer {counter}",
                        "request_id": f"load-steer-{uuid4()}",
                        "wait": False,
                    },
                )
                if not (
                    isinstance(response, dict)
                    and response.get("status") == "accepted"
                ):
                    errors.append(f"steer response not accepted: {response!r}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"steer: {exc}")
            steer_ms.append((time.perf_counter() - started) * 1000)
            await asyncio.sleep(1.0 / STEERS_PER_SECOND)

    async def archive_loop() -> None:
        # One archive per 10s of window, against a dedicated run so the
        # steer loop's targets stay alive.
        archived = 0
        while not stop.is_set():
            await asyncio.sleep(10)
            if stop.is_set() or archived >= 1:
                continue
            archived += 1
            victim = agent_ids[-1]
            try:
                await supervisor.dispatch(
                    "run/archive",
                    {
                        "agent_id": victim,
                        "outcome": "closed",
                        "request_id": f"load-archive-{victim}",
                    },
                )
                agent_ids.remove(victim)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"archive: {exc}")

    tasks = [
        asyncio.create_task(burst_loop(adapter)) for adapter in adapters.values()
    ]
    tasks.append(asyncio.create_task(ping_loop()))
    tasks.append(asyncio.create_task(steer_loop()))
    tasks.append(asyncio.create_task(archive_loop()))
    await asyncio.sleep(duration)
    stop.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await supervisor.close()
    tmp.cleanup()

    ping_p99 = _percentile(ping_ms, 0.99)
    steer_p99 = _percentile(steer_ms, 0.99)
    print(
        f"load-gate: duration={duration:.0f}s runs={run_count} "
        f"pings={len(ping_ms)} steers={len(steer_ms)} "
        f"ping_p50={_percentile(ping_ms, 0.5):.1f}ms "
        f"ping_p99={ping_p99:.1f}ms "
        f"steer_p50={_percentile(steer_ms, 0.5):.1f}ms "
        f"steer_accepted_p99={steer_p99:.1f}ms"
    )
    failed = False
    if errors:
        failed = True
        print(f"load-gate FAIL: {len(errors)} request errors; first: {errors[0]}")
    if ping_p99 >= PING_P99_BUDGET_MS:
        failed = True
        print(
            "load-gate FAIL: ping p99 "
            f"{ping_p99:.1f}ms >= {PING_P99_BUDGET_MS:.0f}ms"
        )
    if steer_p99 >= STEER_ACCEPTED_P99_BUDGET_MS:
        failed = True
        print(
            "load-gate FAIL: steer accepted p99 "
            f"{steer_p99:.1f}ms >= {STEER_ACCEPTED_P99_BUDGET_MS:.0f}ms"
        )
    if not failed:
        print("load-gate PASS")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=float(os.environ.get("LOAD_GATE_DURATION", "15")),
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=int(os.environ.get("LOAD_GATE_RUNS", "8")),
    )
    arguments = parser.parse_args()
    return asyncio.run(run_gate(arguments.duration, arguments.runs))


if __name__ == "__main__":
    raise SystemExit(main())
