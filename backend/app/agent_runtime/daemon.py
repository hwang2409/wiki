from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import signal
import time
import traceback
from pathlib import Path
from typing import BinaryIO

from ..nofile_limit import raise_nofile_limit
from .autopilot import AutopilotController
from .factory import RealAdapterFactory
from .fake import FixtureAdapterFactory
from .fleet_monitor import FleetMonitor
from .fleet_monitor_ids import (
    fleet_monitor_message_dedupe_key,
    fleet_monitor_request_id,
)
from .protocol import UnixSupervisorServer
from .store import RunNotFound, RunStore, RuntimePaths
from .supervisor import Supervisor


logger = logging.getLogger(__name__)


def build_fleet_monitor_dispatch(supervisor: Supervisor):
    """Return the durable dispatch callable that ``FleetMonitor`` uses.

    Extracted so tests can exercise the same callable production wires
    into ``run_daemon`` instead of hand-rolling a lookalike — a rewrite
    that skipped ``supervisor.dispatch`` or dropped the scoped request
    id / dedupe key would then cause both this helper and the test to
    fail together (WIKI-232 REVIEW11 M1). The runtime binding also
    lives in ``run_daemon`` below and MUST stay in sync with this
    helper; every property the tests assert (routes through
    ``run/send_now``, uses ``fleet_monitor_request_id``, uses
    ``fleet_monitor_message_dedupe_key``, forwards ``source``) is a
    contract of this function.
    """

    async def dispatch(
        run_id: str,
        message: str,
        dedupe_key: str | None,
        source: str | None = None,
    ):
        return await supervisor.dispatch(
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
        )

    return dispatch


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
            except RunNotFound as exc:
                logger.info("recovery skipped missing run: %s", exc)
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
    raise_nofile_limit()
    paths = _paths_from_args(args)
    lock = _acquire_single_instance(paths)
    fixture_dir = args.fake_fixture_dir or os.environ.get(
        "WIKI_SUPERVISOR_FAKE_FIXTURE_DIR"
    )
    factory = (
        FixtureAdapterFactory(Path(fixture_dir))
        if fixture_dir
        else RealAdapterFactory(runtime_dir=paths.runtime_dir)
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
            build_fleet_monitor_dispatch(supervisor),
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
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_daemon(parse_args()))


if __name__ == "__main__":
    main()
