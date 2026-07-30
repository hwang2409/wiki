"""Swap a native bundle and restart its daemon under one lock transaction."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import daemon as backend_daemon
from backend.app.native_lifecycle import hold_app_lock, hold_supervisor_lock
from scripts.atomic_swap import atomic_replace, rollback_replace
from scripts.native_daemon_restart import restart_daemon_in_process


RestartDaemon = Callable[[Path, Path, Path], bool]
UninstallDaemon = Callable[[Path, Path, Path], None]


def _uninstall_daemon(live_bundle: Path, runtime_dir: Path, repo_root: Path) -> None:
    config = backend_daemon.config_from_env(
        overrides={
            "WIKI_APP_PATH": str(live_bundle),
            "WIKI_AGENT_RUNTIME_DIR": str(runtime_dir),
            "WIKI_REPO_DIR": str(repo_root),
        }
    )
    backend_daemon.uninstall(config, transaction_lock_held=True)


def _supervisor_pid(runtime_dir: Path) -> int | None:
    try:
        value = (runtime_dir / "supervisor.pid").read_text(encoding="utf-8").strip()
        pid = int(value)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _supervisor_lock_is_free(runtime_dir: Path) -> bool:
    path = runtime_dir / "supervisor.lock"
    if not path.exists():
        return True
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    return True


def _stop_supervisor(runtime_dir: Path, *, timeout: float = 15.0) -> None:
    """Quiesce the live supervisor before taking its single-instance lock."""

    if _supervisor_lock_is_free(runtime_dir):
        return
    pid = _supervisor_pid(runtime_dir)
    if pid is None:
        raise RuntimeError("supervisor lock is held but supervisor.pid is missing")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while not _supervisor_lock_is_free(runtime_dir):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"supervisor {pid} did not stop before swap")
        time.sleep(0.05)


def swap_native_app(
    stage_root: Path,
    repo_root: Path,
    runtime_dir: Path,
    *,
    allow_missing_app_lock: bool = False,
    restart: RestartDaemon = restart_daemon_in_process,
    uninstall: UninstallDaemon = _uninstall_daemon,
) -> None:
    """Swap, restart, and recover while retaining both runtime locks."""

    stage_root = stage_root.expanduser().resolve()
    repo_root = repo_root.expanduser().resolve()
    runtime_dir = runtime_dir.expanduser().resolve()
    staged_bundle = stage_root / "target/release/bundle/macos/Wiki.app"
    live_bundle = repo_root / "src-tauri/target/release/bundle/macos/Wiki.app"
    swap_intent = stage_root / ".swap-intent"
    success_sentinel = stage_root / ".swap-complete"

    if not staged_bundle.is_dir() and not swap_intent.is_file():
        raise FileNotFoundError(f"missing staged Wiki.app at {staged_bundle}")

    with hold_app_lock(
        runtime_dir,
        allow_missing_app_lock=allow_missing_app_lock,
    ):
        _stop_supervisor(runtime_dir)
        with hold_supervisor_lock(runtime_dir):
            with backend_daemon.hold_daemon_transaction_lock(runtime_dir):
                atomic_replace(
                    staged_bundle,
                    live_bundle,
                    success_sentinel,
                    swap_intent,
                )
                try:
                    restart(live_bundle, runtime_dir, repo_root)
                except Exception as new_error:
                    try:
                        rollback_replace(
                            staged_bundle,
                            live_bundle,
                            success_sentinel,
                            swap_intent,
                        )
                    except Exception as rollback_error:
                        raise RuntimeError(
                            f"new daemon failed and old bundle rollback failed: {rollback_error}"
                        ) from new_error

                    try:
                        restart(live_bundle, runtime_dir, repo_root)
                    except Exception as old_error:
                        try:
                            uninstall(live_bundle, runtime_dir, repo_root)
                        except Exception as uninstall_error:
                            raise RuntimeError(
                                "old daemon recovery failed; daemon unload also failed: "
                                f"{uninstall_error}"
                            ) from old_error
                        raise RuntimeError(
                            "old daemon did not become healthy after bundle rollback"
                        ) from old_error
                    raise RuntimeError(
                        "new daemon failed; restored old bundle and verified old daemon health"
                    ) from new_error

                shutil.rmtree(stage_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage_root", type=Path)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--allow-missing-app-lock",
        action="store_true",
        default=os.environ.get("WIKI_NATIVE_ALLOW_MISSING_APP_LOCK", "").lower()
        in {"1", "true", "yes", "on"},
    )
    args = parser.parse_args()
    try:
        swap_native_app(
            args.stage_root,
            args.repo_root,
            args.runtime_dir,
            allow_missing_app_lock=args.allow_missing_app_lock,
        )
    except Exception as error:
        print(f"native bundle transaction failed: {error}", file=sys.stderr)
        return 1
    print("swapped staged Wiki.app and verified its daemon")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
