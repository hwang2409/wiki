from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import pty
import pwd
import queue
import re
import secrets
import signal
import struct
import subprocess
import termios
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketException, status
from starlette.websockets import WebSocketState


ROOT_DIR = Path(os.environ.get("WIKI_REPO_DIR", Path(__file__).resolve().parents[2])).resolve()
TERMINAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_COLS = 80
DEFAULT_ROWS = 24
DEFAULT_MAX_SESSIONS = int(os.environ.get("WIKI_TERMINAL_MAX_SESSIONS", "8"))
FLOW_HIGH_WATERMARK = 512 * 1024
FLOW_LOW_WATERMARK = 128 * 1024

router = APIRouter()
_boot_token: str | None = None


def refresh_boot_token() -> None:
    global _boot_token
    _boot_token = secrets.token_urlsafe(32)


def current_boot_token() -> str:
    if _boot_token is None:
        refresh_boot_token()
    return _boot_token or ""


def valid_terminal_id(terminal_id: str) -> bool:
    return bool(TERMINAL_ID_PATTERN.fullmatch(terminal_id))


def resolve_shell_path() -> str:
    shell = (os.environ.get("SHELL") or "").strip()
    if shell and Path(shell).is_file():
        return shell
    with contextlib.suppress(KeyError):
        candidate = pwd.getpwuid(os.getuid()).pw_shell
        if candidate and Path(candidate).is_file():
            return candidate
    return "/bin/sh"


def set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


@dataclass(frozen=True)
class TerminalExit:
    exit_code: int | None


class TerminalSessionError(RuntimeError):
    pass


class MissingTerminalSessionError(TerminalSessionError):
    pass


class TerminalLimitError(TerminalSessionError):
    pass


class TerminalAlreadyAttachedError(TerminalSessionError):
    pass


class TerminalSession:
    def __init__(
        self,
        terminal_id: str,
        *,
        cwd: Path | None = None,
        shell_path: str | None = None,
    ) -> None:
        self.terminal_id = terminal_id
        self.cwd = (cwd or ROOT_DIR).resolve()
        self.shell_path = shell_path or resolve_shell_path()
        self.process: subprocess.Popen[bytes] | None = None
        self.exit_code: int | None = None
        self._master_fd: int | None = None
        self._attached = False
        self._closed = False
        self._output_queue: queue.Queue[bytes | TerminalExit] = queue.Queue()
        self._flow_gate = threading.Condition()
        self._pending_bytes = 0
        self._exit_enqueued = False
        self._state_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._spawn()

    def _spawn(self) -> None:
        master_fd, slave_fd = pty.openpty()
        set_winsize(master_fd, DEFAULT_ROWS, DEFAULT_COLS)
        set_winsize(slave_fd, DEFAULT_ROWS, DEFAULT_COLS)
        env = {
            **os.environ,
            "TERM": os.environ.get("TERM", "xterm-256color"),
            "COLORTERM": os.environ.get("COLORTERM", "truecolor"),
        }
        try:
            self.process = subprocess.Popen(
                [self.shell_path, "-il"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self.cwd),
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        self._master_fd = master_fd
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"wiki-terminal-{self.terminal_id}",
            daemon=True,
        )
        self._reader_thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def attach(self) -> None:
        with self._state_lock:
            if self._attached:
                raise TerminalAlreadyAttachedError("terminal is already attached")
            self._attached = True

    def detach(self) -> None:
        with self._state_lock:
            self._attached = False

    def write_input(self, data: str) -> None:
        if not data:
            return
        master_fd = self._master_fd
        if master_fd is None or self._closed:
            return
        with contextlib.suppress(OSError):
            os.write(master_fd, data.encode("utf-8", errors="ignore"))

    def resize(self, rows: int, cols: int) -> None:
        master_fd = self._master_fd
        if master_fd is None or self._closed:
            return
        with contextlib.suppress(OSError):
            set_winsize(master_fd, rows, cols)

    def read_output(self, timeout: float | None = None) -> bytes | TerminalExit:
        return self._output_queue.get(timeout=timeout)

    def _enqueue_exit_once(self, exit_code: int | None) -> None:
        with self._state_lock:
            if self._exit_enqueued:
                return
            self._exit_enqueued = True
            self.exit_code = exit_code
        self._output_queue.put(TerminalExit(exit_code=exit_code))

    def _reader_loop(self) -> None:
        master_fd = self._master_fd
        try:
            if master_fd is None:
                return
            while True:
                with self._flow_gate:
                    while self._pending_bytes >= FLOW_HIGH_WATERMARK and not self._closed:
                        self._flow_gate.wait(timeout=0.1)
                if self._closed:
                    break
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        break
                    continue
                if not chunk:
                    break
                with self._flow_gate:
                    self._pending_bytes += len(chunk)
                self._output_queue.put(chunk)
        finally:
            process = self.process
            exit_code = process.poll() if process is not None else None
            if process is not None and exit_code is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    exit_code = process.wait(timeout=0.5)
            self._enqueue_exit_once(exit_code)
            if master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(master_fd)

    def mark_output_delivered(self, size: int) -> None:
        with self._flow_gate:
            self._pending_bytes = max(0, self._pending_bytes - size)
            if self._pending_bytes <= FLOW_LOW_WATERMARK:
                self._flow_gate.notify_all()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        with self._flow_gate:
            self._flow_gate.notify_all()
        process = self.process
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGHUP)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
        master_fd = self._master_fd
        self._master_fd = None
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
        if process is not None:
            self._enqueue_exit_once(process.poll())

    async def serve(self, websocket: WebSocket) -> None:
        sender = asyncio.create_task(self._send_loop(websocket))
        receiver = asyncio.create_task(self._receive_loop(websocket))
        try:
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                with contextlib.suppress(Exception):
                    task.result()
        finally:
            self.detach()
            self.close()
            if websocket.application_state != WebSocketState.DISCONNECTED:
                with contextlib.suppress(Exception):
                    await websocket.close()

    async def _send_loop(self, websocket: WebSocket) -> None:
        while True:
            frame = await asyncio.to_thread(self.read_output)
            if isinstance(frame, bytes):
                try:
                    await websocket.send_bytes(frame)
                finally:
                    self.mark_output_delivered(len(frame))
                continue
            await websocket.send_json(
                {
                    "type": "exit",
                    "exitCode": frame.exit_code,
                    "message": "Session ended. Restart to launch a fresh shell.",
                }
            )
            return

    async def _receive_loop(self, websocket: WebSocket) -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            payload = message.get("text")
            if not payload:
                continue
            try:
                body = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if body.get("type") == "input":
                self.write_input(str(body.get("data", "")))
            elif body.get("type") == "resize":
                try:
                    rows = int(body.get("rows", DEFAULT_ROWS))
                    cols = int(body.get("cols", DEFAULT_COLS))
                except (TypeError, ValueError):
                    continue
                self.resize(rows=rows, cols=cols)


class TerminalManager:
    def __init__(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        cwd: Path | None = None,
        shell_path: str | None = None,
    ) -> None:
        self.max_sessions = max_sessions
        self.cwd = cwd
        self.shell_path = shell_path
        self._lock = threading.Lock()
        self._sessions: dict[str, TerminalSession] = {}

    @property
    def sessions(self) -> dict[str, TerminalSession]:
        return self._sessions

    def _prune_dead_locked(self) -> None:
        dead = [terminal_id for terminal_id, session in self._sessions.items() if not session.alive]
        for terminal_id in dead:
            self._sessions.pop(terminal_id, None)

    def get_or_create(self, terminal_id: str, *, create: bool) -> TerminalSession:
        with self._lock:
            self._prune_dead_locked()
            session = self._sessions.get(terminal_id)
            if session is not None:
                return session
            if not create:
                raise MissingTerminalSessionError("session-missing")
            if len(self._sessions) >= self.max_sessions:
                raise TerminalLimitError(
                    f"Terminal limit reached ({self.max_sessions}). Close another terminal and try again."
                )
            session = TerminalSession(
                terminal_id,
                cwd=self.cwd,
                shell_path=self.shell_path,
            )
            self._sessions[terminal_id] = session
        return session

    def discard(self, terminal_id: str, session: TerminalSession) -> None:
        with self._lock:
            current = self._sessions.get(terminal_id)
            if current is session:
                self._sessions.pop(terminal_id, None)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


TERMINAL_MANAGER = TerminalManager()


@router.get("/api/terminal-token")
def terminal_token() -> dict[str, object]:
    return {
        "token": current_boot_token(),
        "limit": TERMINAL_MANAGER.max_sessions,
    }


@router.websocket("/ws/terminal/{terminal_id}")
async def terminal_socket(websocket: WebSocket, terminal_id: str) -> None:
    if not valid_terminal_id(terminal_id):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    token = websocket.query_params.get("token")
    if not token or token != current_boot_token():
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    create = websocket.query_params.get("create") in {"1", "true", "yes"}
    try:
        session = TERMINAL_MANAGER.get_or_create(terminal_id, create=create)
        session.attach()
    except MissingTerminalSessionError:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "missing",
                "message": "Session ended. Restart to launch a fresh shell.",
            }
        )
        await websocket.close(code=1000)
        return
    except TerminalLimitError as exc:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "code": "limit",
                "message": str(exc),
            }
        )
        await websocket.close(code=1013)
        return
    except TerminalAlreadyAttachedError:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "code": "attached",
                "message": "Terminal is already open in another pane.",
            }
        )
        await websocket.close(code=1000)
        return

    await websocket.accept()
    try:
        await session.serve(websocket)
    finally:
        TERMINAL_MANAGER.discard(terminal_id, session)
