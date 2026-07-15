"""Preflight checks for native bundle builds.

The native app's headless supervisor owns a lock in the runtime directory for
the lifetime of the sidecar.  The PID file is useful as a second signal, but a
stale lock file by itself must never block a build.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeStatus:
    running: bool
    reason: str | None = None


def pid_is_alive(pid: int) -> bool:
    """Return whether *pid* currently exists, without signaling it."""

    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists; the build user just cannot inspect it further.
        return True
    return True


def _read_pid(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid if pid > 1 else None


def _lock_is_held(lock_path: Path) -> bool:
    """Check the supervisor lock without treating its file as a liveness flag."""

    if not lock_path.exists():
        return False
    try:
        handle = lock_path.open("a+b")
    except OSError:
        # A runtime directory that cannot be inspected is unsafe to replace.
        return True
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def inspect_runtime(runtime_dir: Path | str) -> RuntimeStatus:
    """Inspect the supervisor's runtime directory for a live sidecar.

    A stale ``supervisor.lock`` is safe when it is not held and its PID is
    absent/dead.  A live PID is deliberately treated as active even if the
    lock has disappeared, so a partially-written runtime cannot make a build
    unsafe.
    """

    runtime = Path(runtime_dir).expanduser()
    lock_path = runtime / "supervisor.lock"
    pid_path = runtime / "supervisor.pid"

    if _lock_is_held(lock_path):
        return RuntimeStatus(True, f"supervisor lock is held: {lock_path}")

    pid = _read_pid(pid_path)
    if pid is not None and pid_is_alive(pid):
        return RuntimeStatus(True, f"supervisor PID {pid} is alive: {pid_path}")

    return RuntimeStatus(False)


def _default_runtime_dir() -> Path:
    return Path(
        os.environ.get("WIKI_AGENT_RUNTIME_DIR")
        or Path.home() / ".wiki" / "agent-runtime"
    ).expanduser()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether Wiki native build is safe")
    parser.add_argument("--runtime-dir", type=Path, default=_default_runtime_dir())
    args = parser.parse_args()
    status = inspect_runtime(args.runtime_dir)
    if status.running:
        print(
            "REFUSING native build: Wiki.app or its sidecar is running "
            f"({status.reason}). Quit Wiki.app and retry; the live bundle was "
            "not touched.",
            file=sys.stderr,
        )
        return 1
    print(f"native build preflight OK: no live Wiki supervisor in {args.runtime_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
