from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import signal
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
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("wiki supervisor is already running") from exc
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
        await server.start()
        await supervisor.recover_on_start()
        recovery_task = asyncio.create_task(
            _recovery_loop(supervisor, stop),
            name="agent-supervisor-recovery",
        )
        fleet_monitor = FleetMonitor(
            supervisor.store,
            lambda run_id, message, dedupe_key, source: supervisor.send_now(
                run_id, message, dedupe_key=dedupe_key, source=source
            ),
            ownership_lock=supervisor._agent_lock,  # noqa: SLF001
            on_transition=AutopilotController().on_transition,
        )
        fleet_task = asyncio.create_task(
            fleet_monitor.run(stop),
            name="agent-supervisor-fleet-monitor",
        )
        await stop.wait()
    finally:
        for task in (recovery_task, fleet_task):
            if task is not None:
                task.cancel()
        pending = [task for task in (recovery_task, fleet_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await server.close()
        await supervisor.close()
        paths.pid_path.unlink(missing_ok=True)
        lock.close()


def main() -> None:
    asyncio.run(run_daemon(parse_args()))


if __name__ == "__main__":
    main()
