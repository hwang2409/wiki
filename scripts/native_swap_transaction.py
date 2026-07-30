"""Swap a native bundle and restart its daemon under one lock transaction."""

from __future__ import annotations

import argparse
import fcntl
import json
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
from backend.app.native_lifecycle import (
    NativeRuntimeLockError,
    hold_app_lock,
    hold_supervisor_lock,
)
from scripts.atomic_swap import atomic_replace, rollback_replace
from scripts.native_daemon_restart import restart_daemon_in_process


RestartDaemon = Callable[[Path, Path, Path], bool]
UninstallDaemon = Callable[[Path, Path, Path], None]
StartSupervisor = Callable[[Path, Path, Path], None]
_HANDOVER_STATES = frozenset({"working", "waiting-approval", "idle"})


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
        raise SupervisorUnavailable(
            f"supervisor socket is not ready: {exc}"
        ) from exc
    finally:
        connection.close()
    raise RuntimeError("cannot authenticate supervisor socket on this platform")


def _supervisor_identity(
    runtime_dir: Path,
    *,
    expected_executable: Path | None = None,
    expected_fingerprint: str | None = None,
    timeout: float = 5.0,
) -> tuple[SupervisorClient, int, dict[str, object]]:
    pid = _supervisor_pid(runtime_dir)
    if pid is None:
        raise RuntimeError("supervisor lock is held but supervisor.pid is missing")
    paths = RuntimePaths.from_env({"WIKI_AGENT_RUNTIME_DIR": str(runtime_dir)})
    client = SupervisorClient(paths, timeout=2.0)
    deadline = time.monotonic() + timeout
    while True:
        if _supervisor_pid(runtime_dir) != pid or _supervisor_lock_is_free(runtime_dir):
            raise RuntimeError("supervisor identity changed while waiting for readiness")
        try:
            peer_pid = _supervisor_peer_pid(paths.socket_path)
            if peer_pid != pid:
                raise RuntimeError(
                    f"supervisor PID mismatch: pid file has {pid}, socket peer is {peer_pid}"
                )
            health = client.ping()
            if health.get("pid") != pid:
                raise RuntimeError(
                    "supervisor RPC PID does not match its authenticated socket"
                )
            if (
                expected_fingerprint is not None
                and health.get("runtime_fingerprint") != expected_fingerprint
            ):
                raise RuntimeError(
                    "supervisor runtime fingerprint does not match the live bundle"
                )
            if expected_executable is not None and expected_executable.is_file():
                executable = backend_daemon._process_executable(pid)  # noqa: SLF001
                if executable is None or not backend_daemon._same_executable(  # noqa: SLF001
                    executable, expected_executable
                ):
                    raise RuntimeError("supervisor executable does not match the live bundle")
            return client, pid, health
        except SupervisorUnavailable as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"supervisor socket did not become ready before deadline: {exc}"
                ) from exc
            time.sleep(0.05)


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


def _write_handover_state(path: Path, runs: list[dict[str, object]]) -> None:
    """Persist the exact runs before stopping the old supervisor."""
    runs = _validate_handover_state(runs, path)
    payload = {"version": 1, "runs": runs}
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_handover_state(path: Path) -> list[dict[str, object]]:
    """Load a handover journal left by a transaction that stopped mid-swap."""
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid supervisor handover journal: {path}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError(f"invalid supervisor handover journal: {path}")
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise RuntimeError(f"invalid supervisor handover journal: {path}")
    return _validate_handover_state(runs, path)


def _validate_handover_state(
    runs: object,
    path: Path,
) -> list[dict[str, object]]:
    if not isinstance(runs, list):
        raise RuntimeError(f"invalid supervisor handover journal: {path}")
    result: list[dict[str, object]] = []
    for run in runs:
        if not isinstance(run, dict):
            raise RuntimeError(f"invalid supervisor handover journal: {path}")
        if not all(
            isinstance(run.get(key), str) and bool(run.get(key))
            for key in ("agent_id", "run_id", "provider_session_id")
        ):
            raise RuntimeError(f"invalid supervisor handover journal: {path}")
        if run.get("state") not in _HANDOVER_STATES:
            raise RuntimeError(f"invalid supervisor handover journal: {path}")
        result.append(dict(run))
    return result


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
    last_observation: tuple[tuple[object, ...], ...] | None = None
    stable_observations = 0
    while time.monotonic() < deadline:
        try:
            health = client.ping()
            if not isinstance(health, dict):
                raise SupervisorUnavailable("invalid supervisor health")
            observations: list[tuple[object, ...]] = []
            for saved in runs:
                status = client.request("run/status", {"agent_id": saved["agent_id"]})
                if (
                    not isinstance(status, dict)
                    or status.get("run_id") != saved["run_id"]
                    or status.get("provider_session_id")
                    != saved.get("provider_session_id")
                    or status.get("control_attached") is not True
                    or status.get("provider_alive") is not True
                    or status.get("state") not in {"working", "waiting-approval", "idle"}
                    or (
                        saved.get("pending_requests")
                        and (
                            status.get("state") != "waiting-approval"
                            or not status.get("pending_requests")
                        )
                    )
                ):
                    observations = []
                    break
                observations.append(
                    (
                        status["run_id"],
                        status["provider_session_id"],
                        status["control_attached"],
                        status["provider_alive"],
                        status["state"],
                    )
                )
            current_observation = tuple(observations)
            if current_observation and current_observation == last_observation:
                stable_observations += 1
            elif current_observation:
                last_observation = current_observation
                stable_observations = 1
            else:
                last_observation = None
                stable_observations = 0
            if stable_observations >= 2:
                return
        except (SupervisorUnavailable, OSError, ValueError):
            last_observation = None
            stable_observations = 0
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
    exchange_sentinel = stage_root / ".swap-exchanged"
    success_sentinel = stage_root / ".swap-complete"
    handover_state = stage_root / ".handover-runs.json"

    if not staged_bundle.is_dir() and not swap_intent.is_file():
        raise FileNotFoundError(f"missing staged Wiki.app at {staged_bundle}")
    app_lock_path = runtime_dir / "app.lock"
    if not allow_missing_app_lock and not app_lock_path.exists():
        raise NativeRuntimeLockError(
            f"app lock is missing: {app_lock_path}; the installed sidecar may be old. "
            "Quit Wiki.app and pass ALLOW_MISSING_APP_LOCK=1 for the one-time upgrade."
        )

    with hold_app_lock(
        runtime_dir,
        allow_missing_app_lock=allow_missing_app_lock,
    ):
        old_executable = _bundle_backend_executable(live_bundle)
        old_fingerprint = _bundle_backend_fingerprint(
            live_bundle, runtime_dir, repo_root
        )
        saved_runs = (
            _read_handover_state(handover_state)
            if handover_state.is_file()
            else []
        )
        if not _supervisor_lock_is_free(runtime_dir):
            handover_client, _pid, _health = _supervisor_identity(
                runtime_dir,
                expected_executable=old_executable,
                expected_fingerprint=old_fingerprint,
            )
            saved_runs = handover_client.prepare_for_handover()
            _write_handover_state(handover_state, saved_runs)
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
                    exchange_sentinel,
                    swap_intent,
                )
                try:
                    restart(live_bundle, runtime_dir, repo_root)
                    if saved_runs:
                        start_supervisor(live_bundle, runtime_dir, repo_root)
                        handover_started = True
                except Exception as new_error:
                    try:
                        rollback_replace(
                            staged_bundle,
                            live_bundle,
                            exchange_sentinel,
                            swap_intent,
                        )
                    except Exception as rollback_error:
                        raise RuntimeError(
                            f"new daemon failed and old bundle rollback failed: {rollback_error}"
                        ) from new_error

                    try:
                        restart(live_bundle, runtime_dir, repo_root)
                        if saved_runs:
                            start_supervisor(live_bundle, runtime_dir, repo_root)
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
                            exchange_sentinel,
                            swap_intent,
                        )
                        restart(live_bundle, runtime_dir, repo_root)
                        start_supervisor(live_bundle, runtime_dir, repo_root)
                try:
                    _wait_for_handover(runtime_dir, saved_runs)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "supervisor handover failed and old bundle recovery failed"
                    ) from rollback_error
                raise RuntimeError(
                    "new supervisor did not restore saved runs"
                ) from handover_error
        success_sentinel.touch()
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
