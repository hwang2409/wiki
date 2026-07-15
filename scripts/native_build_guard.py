"""Preflight checks for native bundle builds.

The Tauri GUI holds ``app.lock`` for its entire process lifetime. The guard
acquires that lock and the lazy supervisor's lock together; PID files are only
diagnostic and never decide liveness.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.native_lifecycle import (
    NativeRuntimeLockError,
    hold_runtime_locks,
)


@dataclass(frozen=True)
class RuntimeStatus:
    running: bool
    reason: str | None = None


def inspect_runtime(
    runtime_dir: Path | str,
    *,
    allow_missing_app_lock: bool = False,
) -> RuntimeStatus:
    """Return whether runtime locks prevent a safe bundle replacement."""

    try:
        with hold_runtime_locks(
            runtime_dir,
            allow_missing_app_lock=allow_missing_app_lock,
        ):
            return RuntimeStatus(False)
    except NativeRuntimeLockError as exc:
        return RuntimeStatus(True, str(exc))


def _default_runtime_dir() -> Path:
    return Path(
        os.environ.get("WIKI_AGENT_RUNTIME_DIR")
        or Path.home() / ".wiki" / "agent-runtime"
    ).expanduser()


def _allow_missing_app_lock() -> bool:
    return os.environ.get("WIKI_NATIVE_ALLOW_MISSING_APP_LOCK", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether Wiki native build is safe")
    parser.add_argument("--runtime-dir", type=Path, default=_default_runtime_dir())
    parser.add_argument(
        "--allow-missing-app-lock",
        action="store_true",
        default=_allow_missing_app_lock(),
        help="one-time override for upgrading an older sidecar without app.lock",
    )
    args = parser.parse_args()
    status = inspect_runtime(
        args.runtime_dir,
        allow_missing_app_lock=args.allow_missing_app_lock,
    )
    if status.running:
        print(
            "REFUSING native build: Wiki.app or its sidecar is running, or "
            f"cannot be proven stopped ({status.reason}). Quit Wiki.app and "
            "retry; the live bundle was not touched.",
            file=sys.stderr,
        )
        return 1
    print(f"native build preflight OK: runtime locks available in {args.runtime_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
