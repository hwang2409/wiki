from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import os
import signal
import time
import traceback
from pathlib import Path
from typing import BinaryIO

from .factory import RealAdapterFactory
from .fake import FixtureAdapterFactory
from .fleet_monitor import FleetMonitor
from .autopilot import AutopilotController
from .protocol import UnixSupervisorServer
from .store import RunStore, RuntimePaths
from .supervisor import Supervisor


def fleet_monitor_request_id(run_id: str, dedupe_key: str | None) -> str:
    """Scope the durable FleetMonitor request id to the target orchestrator run.

    The notification ``dedupe_key`` alone is stable across daemon boots and
    across orchestrator run replacements (several monitor keys hash only the
    ticket + event). If the target orchestrator gets a replacement run, the
    monitor retries reuse the same ``request_id`` with a different ``run_id``
    in the payload, and the command log rejects it as a conflicting payload
    (``CommandConflict``). Steers can no longer reach the new orchestrator
    run (WIKI-232 R3 H2).

    Bind the request id to the current run instead — a bounded SHA-256 digest
    of ``run_id + dedupe_key`` — so a replacement run receives its own
    request-id namespace, while ``dedupe_key`` itself stays unchanged for
    per-run message dedupe.
    """

    payload = f"{run_id or ''}:{dedupe_key or ''}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"fleet-monitor:{digest}"


def fleet_monitor_message_dedupe_key(
    run_id: str, dedupe_key: str | None
) -> str | None:
    """Scope the transport ``dedupe_key`` to the target orchestrator run.

    ``RunStore.replace`` copies ``message_dedupe_keys`` from the old run to
    the replacement so a mid-flight composer retry stays idempotent. Several
    FleetMonitor alarm keys are stable across daemon boots and across
    replacements (staleness / unrouted-verdict / graph-health hash only the
    ticket + event). After replacement, the same alarm therefore lands on
    an already-claimed dedupe entry inherited from the old run's effect,
    ``claim_message_dedupe_key`` rejects the different owner, and the
    dispatch returns ``deduplicated`` without ever calling the provider
    (WIKI-232 R4 H2).

    Bind the transport dedupe_key to ``(run_id, dedupe_key)`` at the
    callback layer. Within a single run, retries of the same alarm still
    collapse to one delivery; across replacement, the replacement run's
    dedupe namespace is disjoint so the new orchestrator gets exactly one
    fresh delivery of an already-fired alarm.
    """

    if dedupe_key is None:
        return None
    payload = f"{run_id or ''}:{dedupe_key}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"fleet-monitor:{digest}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wiki headless agent supervisor")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--socket")
    parser.add_argument("--registry")
    parser.add_argument("--fake-fixture-dir")
    return parser.parse_args()


def _paths_from_args(args: argparse.Namespace) -> RuntimePaths:
    env = dict(os.environ)
    if args.runtime_dir:
        env["WIKI_AGENT_RUNTIME_DIR"] = args.runtime_dir
    if args.socket:
        env["WIKI_SUPERVISOR_SOCKET_PATH"] = args.socket
    if args.registry:
        env["WIKI_AGENT_REGISTRY_PATH"] = args.registry
    return RuntimePaths.from_env(env)


def _acquire_single_instance(paths: RuntimePaths) -> BinaryIO:
    paths.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.runtime_dir.chmod(0o700)
    handle = paths.lock_path.open("a+b")
    os.chmod(paths.lock_path, 0o600)
    handover = os.environ.get("WIKI_SUPERVISOR_HANDOVER") == "1"
    deadline = time.monotonic() + 20.0
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if not handover or time.monotonic() >= deadline:
                handle.close()
                raise RuntimeError("wiki supervisor is already running") from exc
            time.sleep(0.05)
    paths.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    paths.pid_path.chmod(0o600)
    return handle


async def _recovery_loop(supervisor: Supervisor, stop: asyncio.Event) -> None:
    """Recheck orphaned provider PIDs until exact-session resume is safe."""

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except TimeoutError:
            try:
                await supervisor.recover_on_start()
            except Exception:
                # stderr is the daemon's private supervisor.log; polling must
                # survive a corrupt sibling run or transient filesystem error.
                traceback.print_exc()


async def _shutdown(
    server: UnixSupervisorServer,
    supervisor: Supervisor,
    tasks: list[asyncio.Task[None] | None],
    lock: BinaryIO,
    paths: RuntimePaths,
) -> None:
    for task in tasks:
        if task is not None:
            task.cancel()
    pending = [task for task in tasks if task is not None]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await server.close()
    # Release the single-instance lock before the provider drain: the drain
    # waits on long-lived provider turns, and holding the lock through it
    # blocks every replacement daemon from binding (WIKI-217).
    paths.pid_path.unlink(missing_ok=True)
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        lock.close()
    await supervisor.close()


async def run_daemon(args: argparse.Namespace) -> None:
    paths = _paths_from_args(args)
    lock = _acquire_single_instance(paths)
    fixture_dir = args.fake_fixture_dir or os.environ.get(
        "WIKI_SUPERVISOR_FAKE_FIXTURE_DIR"
    )
    factory = (
        FixtureAdapterFactory(Path(fixture_dir))
        if fixture_dir
        else RealAdapterFactory()
    )
    supervisor = Supervisor(RunStore(paths), factory)
    server = UnixSupervisorServer(supervisor, paths.socket_path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    recovery_task: asyncio.Task[None] | None = None
    fleet_task: asyncio.Task[None] | None = None
    try:
        await supervisor.recover_on_start()
        await server.start()
        recovery_task = asyncio.create_task(
            _recovery_loop(supervisor, stop),
            name="agent-supervisor-recovery",
        )
        fleet_monitor = FleetMonitor(
            supervisor.store,
            # WIKI-232 H3: route monitor steers through the command queue
            # so they join the durable total order — a daemon stop between
            # queue admission and provider delivery replays exactly once
            # instead of vanishing without a receipt. R3 H2 scopes the
            # request id to run_id so orchestrator replacement does not
            # collide on the same dedupe_key. R4 H2 scopes the transport
            # dedupe_key to run_id as well so replacements do not
            # accidentally reuse an inherited dedupe entry from the old
            # run and swallow the first post-replacement delivery.
            lambda run_id, message, dedupe_key, source: supervisor.dispatch(
                "run/send_now",
                {
                    "run_id": run_id,
                    "text": message,
                    "dedupe_key": fleet_monitor_message_dedupe_key(
                        run_id, dedupe_key
                    ),
                    "source": source,
                    "request_id": fleet_monitor_request_id(run_id, dedupe_key),
                },
            ),
            ownership_lock=supervisor._agent_lock,  # noqa: SLF001
            on_transition=AutopilotController(
                notify=AutopilotController.live_notify,
            ).on_transition,
        )
        fleet_task = asyncio.create_task(
            fleet_monitor.run(stop),
            name="agent-supervisor-fleet-monitor",
        )
        await stop.wait()
    finally:
        await _shutdown(server, supervisor, [recovery_task, fleet_task], lock, paths)


def main() -> None:
    asyncio.run(run_daemon(parse_args()))


if __name__ == "__main__":
    main()
