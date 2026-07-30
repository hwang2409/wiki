from __future__ import annotations

import asyncio
import errno
import json
import os
import queue
import re
import shlex
import socket
import threading
import time
import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
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
        session = terminal.TerminalSession("term-1", cwd=self.root, shell_path="/bin/bash")
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

    def test_ctrl_c_interrupts_foreground_sleep(self) -> None:
        session = terminal.TerminalSession("term-ctrl-c", cwd=self.root, shell_path="/bin/bash")
        self.addCleanup(session.close)

        session.write_input("sleep 30\r")
        time.sleep(0.2)
        session.write_input(b"\x03")
        time.sleep(0.5)
        session.write_input("echo __AFTER_CTRL_C__\r")

        output = _read_session_text(session, "__AFTER_CTRL_C__")
        self.assertIn("__AFTER_CTRL_C__", output)
        self.assertTrue(session.alive)

    def test_spawn_under_thread_load_has_no_python_fork_warning(self) -> None:
        errors: list[BaseException] = []

        def spawn_and_close(index: int) -> None:
            try:
                session = terminal.TerminalSession(f"thread-load-{index}", cwd=self.root, shell_path="/bin/sh")
                session.close()
            except BaseException as error:
                errors.append(error)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            workers = [threading.Thread(target=spawn_and_close, args=(index,)) for index in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertFalse(any("fork()" in str(item.message) for item in caught))

    def test_immediate_close_during_spawn_never_signals_parent_group(self) -> None:
        parent_pgid = os.getpgrp()
        original_killpg = terminal.os.killpg
        killpg_calls: list[int] = []

        def guarded_killpg(pgid: int, signum: int) -> None:
            killpg_calls.append(pgid)
            self.assertNotEqual(pgid, parent_pgid)
            original_killpg(pgid, signum)

        with patch.object(terminal.os, "killpg", side_effect=guarded_killpg):
            session = terminal.TerminalSession("immediate-close", cwd=self.root, shell_path="/bin/sh")
            session.close()

        self.assertTrue(session.closed)
        self.assertNotIn(parent_pgid, killpg_calls)

    def test_write_input_retries_short_writes(self) -> None:
        session = terminal.TerminalSession.__new__(terminal.TerminalSession)
        session._master_fd = 42
        session._master_fd_lock = threading.Lock()
        session._closed = False
        writes: list[bytes] = []

        def short_write(_fd: int, payload: bytes) -> int:
            writes.append(payload)
            return min(2, len(payload))

        with patch.object(terminal.os, "dup", return_value=43), patch.object(
            terminal.os, "write", side_effect=short_write
        ):
            session.write_input(b"abcdef")

        self.assertEqual(writes, [b"abcdef", b"cdef", b"ef"])

    def test_frozen_terminal_child_command_uses_native_dispatch(self) -> None:
        with patch.object(terminal.sys, "frozen", True, create=True), patch.object(
            terminal.sys, "executable", "/Applications/Wiki.app/Contents/MacOS/wiki-backend"
        ):
            command = terminal.terminal_child_command("/bin/sh", self.root)

        self.assertEqual(
            command,
            ["/Applications/Wiki.app/Contents/MacOS/wiki-backend", "--terminal-child", "/bin/sh", str(self.root)],
        )

    @staticmethod
    def _bare_session(master_fd: int = 42) -> terminal.TerminalSession:
        session = terminal.TerminalSession.__new__(terminal.TerminalSession)
        session._master_fd = master_fd
        session._master_fd_lock = threading.Lock()
        session._closed = False
        return session

    def test_close_during_read_clears_master_fd_once(self) -> None:
        session = self._bare_session()
        session._flow_gate = threading.Condition()
        session._pending_bytes = 0
        session._output_queue = queue.Queue()
        session._exit_enqueued = False
        session._state_lock = threading.Lock()
        session.process = None

        def read_and_close(_fd: int, _size: int) -> bytes:
            self.assertEqual(session._take_master_fd(), 42)
            raise OSError(errno.EBADF, "closed")

        with patch.object(terminal.os, "dup", return_value=43), patch.object(
            terminal.os, "read", side_effect=read_and_close
        ):
            session._reader_loop()

        self.assertIsNone(session._master_fd)

    def test_close_during_write_stops_before_writing_to_reused_fd(self) -> None:
        session = self._bare_session()
        writes: list[tuple[int, bytes]] = []

        def write_and_close(fd: int, payload: bytes) -> int:
            writes.append((fd, payload))
            self.assertEqual(session._take_master_fd(), 42)
            return 1

        with patch.object(terminal.os, "dup", return_value=43), patch.object(
            terminal.os, "write", side_effect=write_and_close
        ):
            session.write_input(b"abcdef")

        self.assertEqual(writes, [(43, b"abcdef")])

    def test_fd_reuse_by_new_session_cannot_receive_stale_input(self) -> None:
        old_session = self._bare_session(42)
        old_session._take_master_fd()
        new_session = self._bare_session(42)
        writes: list[bytes] = []

        with patch.object(terminal.os, "dup", return_value=43), patch.object(
            terminal.os, "write", side_effect=lambda _fd, payload: writes.append(payload) or len(payload)
        ):
            old_session.write_input(b"stale")
            new_session.write_input(b"current")

        self.assertEqual(writes, [b"current"])


class TerminalWebSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_manager = terminal.TERMINAL_MANAGER
        terminal.TERMINAL_MANAGER = terminal.TerminalManager(cwd=self.root, shell_path="/bin/bash")

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
                ws.send(b"printf '%s\\n' \"$PWD\"\r")
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
                first.send(b"printf '%s\\n' \"$PWD\"\r")
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

                ws.send(f"cat > {ctrl_a_path}\r".encode("utf-8"))
                time.sleep(0.2)
                ws.send(b"\x01Z\x04")
                self.assertTrue(
                    _wait_for(lambda: ctrl_a_path.exists() and ctrl_a_path.read_bytes() == b"\x01Z", timeout=5),
                    "ctrl-a probe bytes never settled",
                )
                self.assertEqual(ctrl_a_path.read_bytes(), b"\x01Z")

                ws.send(b"sleep 100\r")
                time.sleep(0.2)
                ws.send(b"\x03")
                time.sleep(1.0)
                ws.send(f"echo done > {ctrl_c_path}\r".encode("utf-8"))
                self.assertTrue(_wait_for(ctrl_c_path.exists, timeout=5), "ctrl-c probe file never appeared")
                self.assertEqual(ctrl_c_path.read_text(encoding="utf-8").strip(), "done")

    def test_resize_delivers_sigwinch_to_child(self) -> None:
        ready_path = self.root / "winch-ready.txt"
        winch_path = self.root / "winch-hit.txt"
        with _LiveServer(_make_app()) as server:
            token = _http_get_json(f"{server.http_base}/api/terminal-token")["token"]
            with ws_connect(f"{server.ws_base}/ws/terminal/winch-probe?token={token}&create=1") as ws:
                time.sleep(0.5)
                ws.send(
                    (
                        "python3 -c "
                        + shlex.quote(
                            "import pathlib, signal, time; "
                            f"ready = pathlib.Path({str(ready_path)!r}); "
                            f"winch = pathlib.Path({str(winch_path)!r}); "
                            "signal.signal(signal.SIGWINCH, lambda *_: winch.write_text('hit', encoding='utf-8')); "
                            "ready.write_text('ready', encoding='utf-8'); "
                            "time.sleep(30)"
                        )
                        + "\r"
                    ).encode("utf-8")
                )
                self.assertTrue(_wait_for(ready_path.exists, timeout=5), "WINCH probe never became ready")
                ws.send(json.dumps({"type": "resize", "rows": 41, "cols": 119}))
                self.assertTrue(_wait_for(winch_path.exists, timeout=5), "WINCH probe never observed a resize")
                self.assertEqual(winch_path.read_text(encoding="utf-8").strip(), "hit")

    def test_send_loop_batches_output_frames(self) -> None:
        class FakeWebSocket:
            def __init__(self) -> None:
                self.binary_frames: list[bytes] = []
                self.control_frames: list[dict[str, object]] = []

            async def send_bytes(self, data: bytes) -> None:
                self.binary_frames.append(data)

            async def send_json(self, payload: dict[str, object]) -> None:
                self.control_frames.append(payload)

        session = terminal.TerminalSession.__new__(terminal.TerminalSession)
        session.cwd = Path.home() / "projects" / "demo"
        session.shell_path = "/bin/zsh"
        session._flow_gate = threading.Condition()
        session._pending_bytes = 0
        session._output_queue = queue.Queue()
        for _ in range(20):
            chunk = b"x" * 4096
            session._pending_bytes += len(chunk)
            session._output_queue.put(chunk)
        session._output_queue.put(terminal.TerminalExit(exit_code=0))

        websocket = FakeWebSocket()
        asyncio.run(session._send_loop(websocket))

        self.assertEqual([frame["type"] for frame in websocket.control_frames], ["hello", "exit"])
        self.assertEqual(websocket.control_frames[0]["capabilities"], {"binaryInput": True})
        self.assertEqual(websocket.control_frames[0]["shell"], "/bin/zsh")
        self.assertEqual(websocket.control_frames[0]["cwd"], "~/projects/demo")
        self.assertLess(len(websocket.binary_frames), 20)
        self.assertEqual(sum(len(frame) for frame in websocket.binary_frames), 20 * 4096)

    def test_send_loop_preserves_output_boundaries_without_input_loopback(self) -> None:
        class FakeWebSocket:
            def __init__(self) -> None:
                self.binary_frames: list[bytes] = []
                self.control_frames: list[dict[str, object]] = []

            async def send_bytes(self, data: bytes) -> None:
                self.binary_frames.append(data)

            async def send_json(self, payload: dict[str, object]) -> None:
                self.control_frames.append(payload)

        session = terminal.TerminalSession.__new__(terminal.TerminalSession)
        session.cwd = Path("/tmp")
        session.shell_path = "/bin/zsh"
        session._flow_gate = threading.Condition()
        session._pending_bytes = 0
        session._output_queue = queue.Queue()
        expected_chunks = [
            b"ls\x1b[?2004l\r\r\n",
            b"backend\r\nfrontend\r\n",
        ]
        for chunk in expected_chunks:
            session._pending_bytes += len(chunk)
            session._output_queue.put(chunk)
        session._output_queue.put(terminal.TerminalExit(exit_code=0))

        websocket = FakeWebSocket()
        asyncio.run(session._send_loop(websocket))

        self.assertEqual([frame["type"] for frame in websocket.control_frames], ["hello", "exit"])
        self.assertEqual(b"".join(websocket.binary_frames), b"".join(expected_chunks))
        self.assertNotIn(b"lsbackend", b"".join(websocket.binary_frames))

    def test_receive_loop_routes_binary_input_only_to_pty(self) -> None:
        class FakeWebSocket:
            def __init__(self) -> None:
                self.messages = [
                    {"type": "websocket.receive", "bytes": b"ls\r"},
                    {"type": "websocket.receive", "text": json.dumps({"type": "input", "data": "pwd\r"})},
                    {"type": "websocket.disconnect"},
                ]

            async def receive(self) -> dict[str, object]:
                return self.messages.pop(0)

        session = terminal.TerminalSession.__new__(terminal.TerminalSession)
        writes: list[object] = []
        session.write_input = writes.append  # type: ignore[method-assign]

        asyncio.run(session._receive_loop(FakeWebSocket()))

        self.assertEqual(writes, [b"ls\r", "pwd\r"])


if __name__ == "__main__":
    unittest.main()
