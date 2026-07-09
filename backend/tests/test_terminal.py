from __future__ import annotations

import json
import os
import re
import shlex
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
import uvicorn
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect as ws_connect

from backend.app import terminal


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=terminal.TRUSTED_HOSTS)
    app.include_router(terminal.router)
    app.add_event_handler("startup", terminal.refresh_boot_token)
    app.add_event_handler("shutdown", terminal.TERMINAL_MANAGER.close_all)
    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveServer:
    def __init__(self, app: FastAPI) -> None:
        self.port = _free_port()
        self._config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def http_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_base(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def __enter__(self) -> "_LiveServer":
        self._thread.start()
        ready = _wait_for(lambda: _http_get_json(f"{self.http_base}/api/terminal-token") is not None, timeout=5)
        if not ready:
            raise AssertionError("terminal test server never became ready")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _http_get_json(url: str) -> dict | None:
    try:
        with urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for(condition, *, timeout: float = 5.0, step: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


def _read_session_text(session: terminal.TerminalSession, marker: str, *, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    chunks: list[str] = []
    while time.time() < deadline:
        frame = session.read_output(timeout=max(0.1, deadline - time.time()))
        if isinstance(frame, bytes):
            chunks.append(frame.decode("utf-8", errors="replace"))
            joined = "".join(chunks)
            if marker in joined:
                return joined
        else:
            break
    raise AssertionError(f"Timed out waiting for {marker!r}; got {''.join(chunks)!r}")


def _read_websocket_text(ws, marker: str, *, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    chunks: list[str] = []
    while time.time() < deadline:
        payload = ws.recv(timeout=max(0.1, deadline - time.time()))
        if isinstance(payload, bytes):
            chunks.append(payload.decode("utf-8", errors="replace"))
            joined = "".join(chunks)
            if marker in joined:
                return joined
            continue
        if payload:
            chunks.append(payload)
            joined = "".join(chunks)
            if marker in joined:
                return joined
    raise AssertionError(f"Timed out waiting for {marker!r}; got {''.join(chunks)!r}")


class TerminalSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        terminal.TERMINAL_MANAGER.close_all()
        self.tmp.cleanup()

    def test_spawn_resize_and_cleanup_leave_no_orphans(self) -> None:
        session = terminal.TerminalSession("term-1", cwd=self.root, shell_path="/bin/sh")
        self.addCleanup(session.close)

        session.write_input("printf '%s\\n' \"$PWD\"\r")
        self.assertIn(str(self.root), _read_session_text(session, str(self.root)))

        session.resize(rows=41, cols=119)
        session.write_input("stty size\r")
        size_text = _read_session_text(session, "41 119")
        match = re.search(r"(^|\s)(\d+)\s+(\d+)(\s|$)", size_text.replace("\r", ""))
        self.assertIsNotNone(match)
        self.assertEqual((match.group(2), match.group(3)), ("41", "119"))

        pid_file = self.root / "child.pid"
        child_cmd = (
            "python3 -c "
            + shlex.quote(
                "import os, pathlib, time; "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(30)"
            )
            + "\r"
        )
        session.write_input(child_cmd)
        self.assertTrue(_wait_for(pid_file.exists), "child pid file never appeared")
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        shell_pid = session.process.pid if session.process is not None else None
        self.assertIsNotNone(shell_pid)
        self.assertTrue(_pid_is_alive(child_pid))

        session.close()

        self.assertTrue(_wait_for(lambda: not _pid_is_alive(shell_pid), timeout=5))
        self.assertTrue(_wait_for(lambda: not _pid_is_alive(child_pid), timeout=5))


class TerminalWebSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_manager = terminal.TERMINAL_MANAGER
        terminal.TERMINAL_MANAGER = terminal.TerminalManager(cwd=self.root, shell_path="/bin/sh")

    def tearDown(self) -> None:
        terminal.TERMINAL_MANAGER.close_all()
        terminal.TERMINAL_MANAGER = self.original_manager
        self.tmp.cleanup()

    def test_token_auth_rejects_missing_and_accepts_valid(self) -> None:
        with _LiveServer(_make_app()) as server:
            with self.assertRaises(InvalidStatus):
                with ws_connect(f"{server.ws_base}/ws/terminal/test-term?create=1"):
                    pass

            token = _http_get_json(f"{server.http_base}/api/terminal-token")["token"]
            with ws_connect(f"{server.ws_base}/ws/terminal/test-term?token={token}&create=1") as ws:
                ws.send(json.dumps({"type": "input", "data": "printf '%s\\n' \"$PWD\"\r"}))
                output = _read_websocket_text(ws, str(self.root))
                self.assertIn(str(self.root), output)

    def test_terminal_endpoints_reject_untrusted_host_and_origin(self) -> None:
        with _LiveServer(_make_app()) as server:
            with self.assertRaises(HTTPError) as bad_host:
                urlopen(
                    UrlRequest(
                        f"{server.http_base}/api/terminal-token",
                        headers={"Host": "evil.test"},
                    ),
                    timeout=2,
                )
            self.assertEqual(bad_host.exception.code, 400)

            with self.assertRaises(HTTPError) as bad_origin:
                urlopen(
                    UrlRequest(
                        f"{server.http_base}/api/terminal-token",
                        headers={"Origin": "http://evil.test"},
                    ),
                    timeout=2,
                )
            self.assertEqual(bad_origin.exception.code, 403)

            token = _http_get_json(f"{server.http_base}/api/terminal-token")["token"]
            with self.assertRaises(InvalidStatus):
                with ws_connect(
                    f"{server.ws_base}/ws/terminal/bad-origin?token={token}&create=1",
                    origin="http://evil.test",
                ):
                    pass

    def test_terminal_limit_returns_clear_error(self) -> None:
        terminal.TERMINAL_MANAGER = terminal.TerminalManager(
            max_sessions=1,
            cwd=self.root,
            shell_path="/bin/sh",
        )
        with _LiveServer(_make_app()) as server:
            token = _http_get_json(f"{server.http_base}/api/terminal-token")["token"]
            with ws_connect(f"{server.ws_base}/ws/terminal/one?token={token}&create=1") as first:
                first.send(json.dumps({"type": "input", "data": "printf '%s\\n' \"$PWD\"\r"}))
                _read_websocket_text(first, str(self.root))
                with ws_connect(f"{server.ws_base}/ws/terminal/two?token={token}&create=1") as second:
                    payload = json.loads(second.recv())
                    self.assertEqual(payload["type"], "error")
                    self.assertEqual(payload["code"], "limit")
                    self.assertIn("Terminal limit reached", payload["message"])

    def test_websocket_control_bytes_reach_shell(self) -> None:
        ctrl_a_path = self.root / "ctrl-a.bin"
        ctrl_c_path = self.root / "ctrl-c.txt"
        with _LiveServer(_make_app()) as server:
            token = _http_get_json(f"{server.http_base}/api/terminal-token")["token"]
            with ws_connect(f"{server.ws_base}/ws/terminal/ctrl-probe?token={token}&create=1") as ws:
                time.sleep(0.5)

                ws.send(json.dumps({"type": "input", "data": f"cat > {ctrl_a_path}\r"}))
                time.sleep(0.2)
                ws.send(json.dumps({"type": "input", "data": "\x01Z\x04"}))
                self.assertTrue(
                    _wait_for(lambda: ctrl_a_path.exists() and ctrl_a_path.read_bytes() == b"\x01Z", timeout=5),
                    "ctrl-a probe bytes never settled",
                )
                self.assertEqual(ctrl_a_path.read_bytes(), b"\x01Z")

                ws.send(json.dumps({"type": "input", "data": "sleep 100\r"}))
                time.sleep(0.2)
                ws.send(json.dumps({"type": "input", "data": "\x03"}))
                time.sleep(1.0)
                ws.send(json.dumps({"type": "input", "data": f"echo done > {ctrl_c_path}\r"}))
                self.assertTrue(_wait_for(ctrl_c_path.exists, timeout=5), "ctrl-c probe file never appeared")
                self.assertEqual(ctrl_c_path.read_text(encoding="utf-8").strip(), "done")


if __name__ == "__main__":
    unittest.main()
