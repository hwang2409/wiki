"""Swap a native bundle and restart its daemon under one lock transaction."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import socket
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import daemon as backend_daemon
from backend.app.agent_runtime.client import SupervisorClient, SupervisorUnavailable
from backend.app.agent_runtime.store import RuntimePaths
from backend.app.native_lifecycle import hold_app_lock, hold_supervisor_lock
from scripts.atomic_swap import atomic_replace, rollback_replace
from scripts.native_daemon_restart import restart_daemon_in_process


RestartDaemon = Callable[[Path, Path, Path], bool]
UninstallDaemon = Callable[[Path, Path, Path], None]
StartSupervisor = Callable[[Path, Path], None]


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


def _supervisor_peer_pid(socket_path: Path) -> int:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(1.0)
        connection.connect(str(socket_path))
        if sys.platform == "darwin":
            raw = connection.getsockopt(getattr(socket, "SOL_LOCAL", 0), 0x002, 4)
            return int.from_bytes(raw, byteorder=sys.byteorder)
        if sys.platform == "linux":
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return int.from_bytes(raw[:4], byteorder=sys.byteorder, signed=True)
    except OSError as exc:
        raise RuntimeError(f"cannot authenticate supervisor socket: {exc}") from exc
    finally:
        connection.close()
    raise RuntimeError("cannot authenticate supervisor socket on this platform")


def _supervisor_identity(
    runtime_dir: Path,
    *,
    expected_executable: Path | None = None,
    expected_fingerprint: str | None = None,
) -> tuple[SupervisorClient, int, dict[str, object]]:
    pid = _supervisor_pid(runtime_dir)
    if pid is None:
        raise RuntimeError("supervisor lock is held but supervisor.pid is missing")
    paths = RuntimePaths.from_env({"WIKI_AGENT_RUNTIME_DIR": str(runtime_dir)})
    peer_pid = _supervisor_peer_pid(paths.socket_path)
    if peer_pid != pid:
        raise RuntimeError(
            f"supervisor PID mismatch: pid file has {pid}, socket peer is {peer_pid}"
        )
    client = SupervisorClient(paths, timeout=2.0)
    try:
        health = client.ping()
    except SupervisorUnavailable as exc:
        raise RuntimeError(f"cannot authenticate supervisor RPC: {exc}") from exc
    if health.get("pid") != pid:
        raise RuntimeError("supervisor RPC PID does not match its authenticated socket")
    if (
        expected_fingerprint is not None
        and health.get("runtime_fingerprint") != expected_fingerprint
    ):
        raise RuntimeError("supervisor runtime fingerprint does not match the live bundle")
    if expected_executable is not None and expected_executable.is_file():
        executable = backend_daemon._process_executable(pid)  # noqa: SLF001
        if executable is None or not backend_daemon._same_executable(  # noqa: SLF001
            executable, expected_executable
        ):
            raise RuntimeError("supervisor executable does not match the live bundle")
    return client, pid, health


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


def _stop_supervisor(
    runtime_dir: Path,
    *,
    timeout: float = 15.0,
    expected_executable: Path | None = None,
    expected_fingerprint: str | None = None,
) -> None:
    """Stop an authenticated supervisor and wait for its full drain."""

    if _supervisor_lock_is_free(runtime_dir):
        return
    _client, pid, _health = _supervisor_identity(
        runtime_dir,
        expected_executable=expected_executable,
        expected_fingerprint=expected_fingerprint,
    )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while not _supervisor_lock_is_free(runtime_dir) or _pid_alive(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"supervisor {pid} did not stop before swap")
        time.sleep(0.05)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError:
        return True
    return bool(state) and not state.startswith("Z")


def _bundle_backend_executable(bundle: Path) -> Path:
    return bundle / "Contents/Resources/wiki-backend-sidecar/wiki-backend"


def _bundle_backend_fingerprint(
    bundle: Path,
    runtime_dir: Path,
    repo_root: Path,
) -> str | None:
    executable = _bundle_backend_executable(bundle)
    if not executable.is_file():
        return None
    return backend_daemon._expected_backend_fingerprint(  # noqa: SLF001
        backend_daemon.config_from_env(
            overrides={
                "WIKI_APP_PATH": str(bundle),
                "WIKI_AGENT_RUNTIME_DIR": str(runtime_dir),
                "WIKI_REPO_DIR": str(repo_root),
            }
        )
    )


def _start_supervisor(live_bundle: Path, runtime_dir: Path, repo_root: Path) -> None:
    executable = _bundle_backend_executable(live_bundle)
    if not executable.is_file():
        raise RuntimeError(f"new supervisor executable is missing: {executable}")
    paths = RuntimePaths.from_env({"WIKI_AGENT_RUNTIME_DIR": str(runtime_dir)})
    paths.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.runtime_dir.chmod(0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "WIKI_AGENT_RUNTIME_DIR": str(paths.runtime_dir),
            "WIKI_SUPERVISOR_SOCKET_PATH": str(paths.socket_path),
            "WIKI_AGENT_REGISTRY_PATH": str(paths.registry_path),
            "WIKI_SUPERVISOR_HANDOVER": "1",
        }
    )
    log_handle = paths.log_path.open("ab")
    try:
        subprocess.Popen(
            [str(executable), "--supervisor"],
            cwd=repo_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        log_handle.close()


def _wait_for_handover(
    runtime_dir: Path,
    runs: list[dict[str, object]],
    *,
    timeout: float = 15.0,
) -> None:
    if not runs:
        return
    paths = RuntimePaths.from_env({"WIKI_AGENT_RUNTIME_DIR": str(runtime_dir)})
    client = SupervisorClient(paths, timeout=2.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health = client.ping()
            if not isinstance(health, dict):
                raise SupervisorUnavailable("invalid supervisor health")
            stable = True
            for saved in runs:
                status = client.request("run/status", {"agent_id": saved["agent_id"]})
                if (
                    not isinstance(status, dict)
                    or status.get("run_id") != saved["run_id"]
                    or status.get("provider_session_id")
                    != saved.get("provider_session_id")
                ):
                    stable = False
                    break
            if stable:
                return
        except (SupervisorUnavailable, OSError, ValueError):
            pass
        time.sleep(0.05)
    raise RuntimeError("new supervisor did not restore every saved run and session")


def swap_native_app(
    stage_root: Path,
    repo_root: Path,
    runtime_dir: Path,
    *,
    allow_missing_app_lock: bool = False,
    restart: RestartDaemon = restart_daemon_in_process,
    uninstall: UninstallDaemon = _uninstall_daemon,
    start_supervisor: StartSupervisor = _start_supervisor,
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
        old_executable = _bundle_backend_executable(live_bundle)
        old_fingerprint = _bundle_backend_fingerprint(
            live_bundle, runtime_dir, repo_root
        )
        saved_runs: list[dict[str, object]] = []
        if not _supervisor_lock_is_free(runtime_dir):
            handover_client, _pid, _health = _supervisor_identity(
                runtime_dir,
                expected_executable=old_executable,
                expected_fingerprint=old_fingerprint,
            )
            saved_runs = handover_client.prepare_for_handover()
        _stop_supervisor(
            runtime_dir,
            expected_executable=old_executable,
            expected_fingerprint=old_fingerprint,
        )
        handover_started = False
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
                    if saved_runs:
                        start_supervisor(live_bundle, runtime_dir)
                        handover_started = True
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
                        if saved_runs:
                            start_supervisor(live_bundle, runtime_dir)
                            handover_started = True
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

        if handover_started:
            try:
                _wait_for_handover(runtime_dir, saved_runs)
            except Exception as handover_error:
                new_executable = _bundle_backend_executable(live_bundle)
                new_fingerprint = _bundle_backend_fingerprint(
                    live_bundle, runtime_dir, repo_root
                )
                _stop_supervisor(
                    runtime_dir,
                    expected_executable=new_executable,
                    expected_fingerprint=new_fingerprint,
                )
                with hold_supervisor_lock(runtime_dir):
                    with backend_daemon.hold_daemon_transaction_lock(runtime_dir):
                        rollback_replace(
                            staged_bundle,
                            live_bundle,
                            success_sentinel,
                            swap_intent,
                        )
                        restart(live_bundle, runtime_dir, repo_root)
                        start_supervisor(live_bundle, runtime_dir)
                try:
                    _wait_for_handover(runtime_dir, saved_runs)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "supervisor handover failed and old bundle recovery failed"
                    ) from rollback_error
                raise RuntimeError(
                    "new supervisor did not restore saved runs"
                ) from handover_error
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
