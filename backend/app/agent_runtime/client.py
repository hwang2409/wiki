from __future__ import annotations

import asyncio
import json
import os
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


class SupervisorUnavailable(RuntimeError):
    pass


class SupervisorRemoteError(RuntimeError):
    pass


class SupervisorClient:
    def __init__(self, paths: RuntimePaths, *, timeout: float = 10.0):
        self.paths = paths
        self.timeout = timeout

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
            raise SupervisorRemoteError(str(error.get("message") or error))
        return response.get("result")

    def ping(self) -> dict[str, Any]:
        return dict(self.request("ping"))

    def send_message(self, agent_id: str, text: str, mode: str) -> dict[str, Any]:
        """Preserve POST /api/agents/<id>/message's now/on-idle contract."""

        if mode not in {"now", "on-idle"}:
            raise ValueError("mode must be now or on-idle")
        method = "run/send_now" if mode == "now" else "run/send_on_idle"
        return dict(self.request(method, {"agent_id": agent_id, "text": text}))

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
        try:
            return self.ping()
        except SupervisorUnavailable:
            pass
        if os.environ.get("WIKI_SUPERVISOR_AUTOSTART", "on").lower() in {"0", "off", "false"}:
            raise SupervisorUnavailable("supervisor is not running and autostart is disabled")
        self._spawn_detached()
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return self.ping()
            except SupervisorUnavailable as exc:
                last_error = exc
                time.sleep(0.05)
        raise SupervisorUnavailable(f"supervisor did not become ready: {last_error}")

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
