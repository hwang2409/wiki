from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import uuid4

from .protocol import MAX_PROTOCOL_LINE_BYTES
from .store import RuntimePaths
from .version import RUNTIME_FINGERPRINT


DEFAULT_SWAP_DRAIN_SECONDS = 10.0


def replacement_prompt(
    agent_id: str,
    current: dict[str, Any],
    *,
    status_dir: Path,
    runs_dir: Path | None = None,
) -> str:
    """Build the WIKI-38 recovery kickoff for manual and upgrade replacements."""

    run_id = current.get("run_id")
    raw_events = current.get("log")
    if not raw_events and isinstance(run_id, str) and runs_dir is not None:
        raw_events = runs_dir / run_id / "raw.jsonl"
    return f"""You are the replacement {current.get("role") or "worker"} for {agent_id}.
Your prior provider session was {current.get("provider_session_id") or current.get("session_id") or "not recorded"}.

Recover context from:
- prior transcript: {current.get("transcript_path") or current.get("transcript") or "not resolved"}
- raw provider events: {raw_events or "not recorded"}
- status file: {status_dir / f"{agent_id}.json"}

Preserve the same identity, role, worktree, orchestrator grouping, PR gates, and status-file contract. Re-read the current ticket/PR state, update the status file before long operations, then continue from the last durable step.
"""


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SupervisorUnavailable(RuntimeError):
    pass


class SupervisorRemoteError(RuntimeError):
    def __init__(self, message: str, *, error_type: str | None = None):
        super().__init__(message)
        self.error_type = error_type


class SupervisorClient:
    def __init__(
        self,
        paths: RuntimePaths,
        *,
        timeout: float = 10.0,
        runtime_fingerprint: str = RUNTIME_FINGERPRINT,
        swap_drain_seconds: float = DEFAULT_SWAP_DRAIN_SECONDS,
    ):
        if swap_drain_seconds < 0:
            raise ValueError("swap_drain_seconds must be non-negative")
        self.paths = paths
        self.timeout = timeout
        self.runtime_fingerprint = runtime_fingerprint
        self.swap_drain_seconds = swap_drain_seconds

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = str(uuid4())
        request = {"id": request_id, "method": method, "params": params or {}}
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.paths.socket_path))
            connection.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
            with connection.makefile("rb") as reader:
                line = reader.readline()
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            raise SupervisorUnavailable(str(exc)) from exc
        finally:
            connection.close()
        if not line:
            raise SupervisorUnavailable("supervisor closed the socket without a response")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise SupervisorRemoteError("supervisor response id mismatch")
        if "error" in response:
            error = response["error"]
            raise SupervisorRemoteError(
                str(error.get("message") or error),
                error_type=error.get("type") if isinstance(error, dict) else None,
            )
        return response.get("result")

    def ping(self) -> dict[str, Any]:
        return dict(self.request("ping"))

    def send_message(self, agent_id: str, text: str, mode: str) -> dict[str, Any]:
        """Preserve POST /api/agents/<id>/message's now/on-idle contract."""

        if mode not in {"now", "on-idle"}:
            raise ValueError("mode must be now or on-idle")
        method = "run/send_now" if mode == "now" else "run/send_on_idle"
        return dict(self.request(method, {"agent_id": agent_id, "text": text}))

    def queue(self, agent_id: str) -> dict[str, Any]:
        return dict(self.request("run/queue", {"agent_id": agent_id}))

    def delete_queued(self, agent_id: str, index: int) -> dict[str, Any]:
        return dict(
            self.request(
                "run/queue/delete",
                {"agent_id": agent_id, "index": index},
            )
        )

    def queue_model_change(self, agent_id: str, model: str) -> dict[str, Any]:
        return dict(
            self.request(
                "run/queue_model_change",
                {"agent_id": agent_id, "model": model},
            )
        )

    def cancel_model_change(self, agent_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "run/cancel_model_change",
                {"agent_id": agent_id},
            )
        )

    async def subscribe_events(self) -> AsyncGenerator[dict[str, Any], None]:
        reader, writer = await asyncio.open_unix_connection(
            str(self.paths.socket_path),
            limit=MAX_PROTOCOL_LINE_BYTES,
        )
        request_id = str(uuid4())
        writer.write(
            json.dumps(
                {"id": request_id, "method": "events/subscribe", "params": {}},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        await writer.drain()
        try:
            response = json.loads(await reader.readline())
            if response.get("id") != request_id or "error" in response:
                raise SupervisorRemoteError(str(response.get("error") or "subscription failed"))
            while line := await reader.readline():
                message = json.loads(line)
                event = message.get("event")
                if isinstance(event, dict):
                    # These are the same dictionaries the existing /api/events
                    # endpoint emits; the backend must not wrap or rename them.
                    yield event
        finally:
            writer.close()
            await writer.wait_closed()

    def ensure_running(self, *, timeout: float = 5.0) -> dict[str, Any]:
        active_runs: list[dict[str, Any]] = []
        autostart_enabled = os.environ.get(
            "WIKI_SUPERVISOR_AUTOSTART", "on"
        ).lower() not in {"0", "off", "false"}
        try:
            health = self.ping()
        except SupervisorUnavailable:
            pass
        else:
            if health.get("runtime_fingerprint") == self.runtime_fingerprint:
                return health
            if health.get("runtime_fingerprint") is None and not autostart_enabled:
                # Isolated test and externally managed supervisors predate the
                # fingerprint field. With autostart disabled, use the server
                # the caller deliberately supplied instead of replacing it.
                return health
            if not autostart_enabled:
                raise SupervisorUnavailable(
                    "supervisor runtime does not match this backend and autostart is disabled"
                )
            active_runs = self._active_runs_for_swap()
            self._prepare_runs_for_swap(active_runs)
            replacement = self._stop_stale_supervisor(health, timeout=timeout)
            if replacement is not None:
                self._replace_runs_after_swap(active_runs)
                return replacement
        if not autostart_enabled:
            raise SupervisorUnavailable("supervisor is not running and autostart is disabled")
        self._spawn_detached()
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                health = self.ping()
                if health.get("runtime_fingerprint") != self.runtime_fingerprint:
                    time.sleep(0.05)
                    continue
                self._replace_runs_after_swap(active_runs)
                return health
            except SupervisorUnavailable as exc:
                last_error = exc
                time.sleep(0.05)
        raise SupervisorUnavailable(f"supervisor did not become ready: {last_error}")

    def _active_runs_for_swap(self) -> list[dict[str, Any]]:
        """Snapshot current runs whose provider transport must cross the swap."""

        try:
            result = self.request("run/list")
        except (SupervisorRemoteError, SupervisorUnavailable) as exc:
            raise SupervisorUnavailable(
                f"cannot enumerate runs before supervisor replacement: {exc}"
            ) from exc
        runs = result.get("runs") if isinstance(result, dict) else None
        if not isinstance(runs, list):
            raise SupervisorUnavailable(
                "cannot enumerate runs before supervisor replacement: bad run list"
            )

        active: list[dict[str, Any]] = []
        for candidate in runs:
            if not isinstance(candidate, dict):
                continue
            agent_id = candidate.get("agent_id")
            run_id = candidate.get("run_id")
            if not isinstance(agent_id, str) or not isinstance(run_id, str):
                continue
            try:
                current = self.request("run/status", {"agent_id": agent_id})
            except SupervisorRemoteError:
                continue
            except SupervisorUnavailable as exc:
                raise SupervisorUnavailable(
                    f"cannot inspect {agent_id} before supervisor replacement: {exc}"
                ) from exc
            if not isinstance(current, dict) or current.get("run_id") != run_id:
                continue
            if current.get("control_attached") or _pid_alive(
                current.get("provider_pid")
            ):
                active.append(dict(current))
        return active

    def _prepare_runs_for_swap(self, runs: list[dict[str, Any]]) -> None:
        """Quiesce providers while the old daemon still owns their control pipes."""

        for run in runs:
            run_id = str(run["run_id"])
            if run.get("state") == "working":
                try:
                    self.request("run/interrupt", {"run_id": run_id})
                    self._drain_interrupted_run(run_id)
                except (SupervisorRemoteError, SupervisorUnavailable):
                    # v1 permits losing an in-flight turn. The new supervisor's
                    # verified orphan cleanup is the fallback if graceful
                    # interruption cannot complete before the daemon exits.
                    pass
            try:
                self.request("run/stop", {"run_id": run_id})
            except (SupervisorRemoteError, SupervisorUnavailable):
                # Continue the swap: run/replace on the new daemon verifies and
                # terminates any provider process left behind by the old one.
                pass

    def _drain_interrupted_run(self, run_id: str) -> None:
        deadline = time.monotonic() + self.swap_drain_seconds
        while time.monotonic() < deadline:
            status = self.request("run/status", {"run_id": run_id})
            if not isinstance(status, dict):
                return
            if status.get("state") != "working" or not status.get("active_turn_id"):
                return
            time.sleep(0.05)

    def _replace_runs_after_swap(self, runs: list[dict[str, Any]]) -> None:
        failures: list[str] = []
        for run in runs:
            agent_id = str(run["agent_id"])
            run_id = str(run["run_id"])
            try:
                self.request(
                    "run/replace",
                    {
                        "run_id": run_id,
                        "prompt": replacement_prompt(
                            agent_id,
                            run,
                            status_dir=self.paths.status_dir,
                            runs_dir=self.paths.runs_dir,
                        ),
                    },
                )
            except (SupervisorRemoteError, SupervisorUnavailable) as exc:
                try:
                    current = self.request("run/status", {"agent_id": agent_id})
                except (SupervisorRemoteError, SupervisorUnavailable):
                    current = None
                if (
                    isinstance(current, dict)
                    and current.get("run_id") != run_id
                    and current.get("control_attached")
                ):
                    continue
                failures.append(f"{agent_id}: {exc}")
        if failures:
            raise SupervisorUnavailable(
                "supervisor upgraded but run replacement failed: " + "; ".join(failures)
            )

    def _stop_stale_supervisor(
        self,
        health: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        pid = health.get("pid")
        if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
            raise SupervisorUnavailable(
                "supervisor runtime does not match this backend and its PID is invalid"
            )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return None
        except PermissionError as exc:
            raise SupervisorUnavailable(
                f"cannot replace stale supervisor process {pid}: {exc}"
            ) from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                current = self.ping()
            except SupervisorUnavailable:
                return None
            if current.get("runtime_fingerprint") == self.runtime_fingerprint:
                return current
            if current.get("pid") != pid:
                return None
            time.sleep(0.05)
        raise SupervisorUnavailable(
            f"stale supervisor process {pid} did not stop within {timeout:g}s"
        )

    def _spawn_detached(self) -> None:
        self.paths.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.paths.runtime_dir.chmod(0o700)
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--supervisor"]
        else:
            command = [sys.executable, "-m", "backend.app.agent_runtime.daemon"]
        env = os.environ.copy()
        env["WIKI_AGENT_RUNTIME_DIR"] = str(self.paths.runtime_dir)
        env["WIKI_SUPERVISOR_SOCKET_PATH"] = str(self.paths.socket_path)
        env["WIKI_AGENT_REGISTRY_PATH"] = str(self.paths.registry_path)
        fd = os.open(self.paths.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        log = os.fdopen(fd, "ab", buffering=0)
        try:
            subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[3],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            log.close()
