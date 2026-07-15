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
import sys
import termios
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketException, status
from starlette.websockets import WebSocketState


ROOT_DIR = Path(os.environ.get("WIKI_REPO_DIR", Path(__file__).resolve().parents[2])).resolve()
TERMINAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_COLS = 80
DEFAULT_ROWS = 24
DEFAULT_MAX_SESSIONS = int(os.environ.get("WIKI_TERMINAL_MAX_SESSIONS", "8"))
FLOW_HIGH_WATERMARK = 512 * 1024
FLOW_LOW_WATERMARK = 128 * 1024
OUTPUT_BATCH_WINDOW_SECONDS = max(
    0.0,
    float(os.environ.get("WIKI_TERMINAL_OUTPUT_BATCH_WINDOW_MS", "5")) / 1000.0,
)
OUTPUT_BATCH_MAX_BYTES = max(1024, int(os.environ.get("WIKI_TERMINAL_OUTPUT_BATCH_MAX_BYTES", str(64 * 1024))))
TRUSTED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
TRUSTED_HOST_NAMES = {"localhost", "127.0.0.1", "::1"}

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


def _hostname_from_host_header(value: str | None) -> str:
    host = (value or "").strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def trusted_host(value: str | None) -> bool:
    return _hostname_from_host_header(value) in TRUSTED_HOST_NAMES


def trusted_origin(value: str | None) -> bool:
    if not value:
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return False
    return (parsed.hostname or "").lower() in TRUSTED_HOST_NAMES


def validate_trusted_headers(host: str | None, origin: str | None) -> bool:
    return trusted_host(host) and trusted_origin(origin)


def require_trusted_request(request: Request) -> None:
    if not validate_trusted_headers(request.headers.get("host"), request.headers.get("origin")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Untrusted terminal origin")


def require_trusted_websocket(websocket: WebSocket) -> None:
    if not validate_trusted_headers(websocket.headers.get("host"), websocket.headers.get("origin")):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)


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


def terminal_child_command(shell_path: str, cwd: Path) -> list[str]:
    """Build the child command for both source checkouts and frozen binaries."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--terminal-child", shell_path, str(cwd)]
    return [sys.executable, str(Path(__file__).with_name("terminal_child.py")), shell_path, str(cwd)]


@dataclass(frozen=True)
class TerminalExit:
    exit_code: int | None


@dataclass(frozen=True)
class TerminalOutputBatch:
    data: bytes
    exit_frame: TerminalExit | None = None


class TerminalSessionError(RuntimeError):
    pass


class MissingTerminalSessionError(TerminalSessionError):
    pass


class TerminalLimitError(TerminalSessionError):
    pass


class TerminalAlreadyAttachedError(TerminalSessionError):
    pass


class TerminalSession:
    _spawn_lock = threading.Lock()

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
        self._master_fd_lock = threading.Lock()
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
        with self._spawn_lock:
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
                    terminal_child_command(self.shell_path, self.cwd),
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
        with self._master_fd_lock:
            if self._closed:
                publish_master = False
            else:
                self._master_fd = master_fd
                publish_master = True
        if not publish_master:
            self._close_master_fd(master_fd)
            if self.process is not None:
                self._terminate_process(self.process)
            self._enqueue_exit_once(self.process.poll() if self.process is not None else None)
            return
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"wiki-terminal-{self.terminal_id}",
            daemon=True,
        )
        self._reader_thread.start()

    @property
    def closed(self) -> bool:
        with self._master_fd_lock:
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

    def write_input(self, data: str | bytes) -> None:
        if not data:
            return
        payload = data.encode("utf-8", errors="ignore") if isinstance(data, str) else data
        offset = 0
        while offset < len(payload):
            with self._master_fd_lock:
                if self._master_fd is None or self._closed:
                    return
                try:
                    write_fd = os.dup(self._master_fd)
                except OSError:
                    return
            try:
                written = os.write(write_fd, payload[offset:])
            except InterruptedError:
                continue
            except OSError:
                return
            finally:
                with contextlib.suppress(OSError):
                    os.close(write_fd)
            if written <= 0:
                return
            offset += written

    def resize(self, rows: int, cols: int) -> None:
        with self._master_fd_lock:
            if self._master_fd is None or self._closed:
                return
            with contextlib.suppress(OSError):
                set_winsize(self._master_fd, rows, cols)

    def read_output(self, timeout: float | None = None) -> bytes | TerminalExit:
        return self._output_queue.get(timeout=timeout)

    def read_output_batch(self, timeout: float | None = None) -> TerminalOutputBatch | TerminalExit:
        first = self._output_queue.get(timeout=timeout)
        if isinstance(first, TerminalExit):
            return first
        chunks = [first]
        total = len(first)
        exit_frame: TerminalExit | None = None
        if OUTPUT_BATCH_WINDOW_SECONDS <= 0:
            return TerminalOutputBatch(data=first)
        deadline = time.monotonic() + OUTPUT_BATCH_WINDOW_SECONDS
        while total < OUTPUT_BATCH_MAX_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._output_queue.get(timeout=remaining)
            except queue.Empty:
                break
            if isinstance(item, TerminalExit):
                exit_frame = item
                break
            chunks.append(item)
            total += len(item)
        return TerminalOutputBatch(data=b"".join(chunks), exit_frame=exit_frame)

    def _enqueue_exit_once(self, exit_code: int | None) -> None:
        with self._state_lock:
            if self._exit_enqueued:
                return
            self._exit_enqueued = True
            self.exit_code = exit_code
        self._output_queue.put(TerminalExit(exit_code=exit_code))

    def _reader_loop(self) -> None:
        try:
            while True:
                with self._flow_gate:
                    while self._pending_bytes >= FLOW_HIGH_WATERMARK and not self.closed:
                        self._flow_gate.wait(timeout=0.1)
                if self.closed:
                    break
                with self._master_fd_lock:
                    if self._master_fd is None or self._closed:
                        break
                    try:
                        reader_fd = os.dup(self._master_fd)
                    except OSError:
                        break
                try:
                    chunk = os.read(reader_fd, 65536)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        break
                    continue
                finally:
                    with contextlib.suppress(OSError):
                        os.close(reader_fd)
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
            master_fd = self._take_master_fd()
            if master_fd is not None:
                self._close_master_fd(master_fd)

    def _take_master_fd(self) -> int | None:
        with self._master_fd_lock:
            master_fd = self._master_fd
            self._master_fd = None
            return master_fd

    @staticmethod
    def _close_master_fd(master_fd: int) -> None:
        with contextlib.suppress(OSError):
            os.close(master_fd)

    def mark_output_delivered(self, size: int) -> None:
        with self._flow_gate:
            self._pending_bytes = max(0, self._pending_bytes - size)
            if self._pending_bytes <= FLOW_LOW_WATERMARK:
                self._flow_gate.notify_all()

    def close(self) -> None:
        with self._master_fd_lock:
            if self._closed:
                return
            self._closed = True
            master_fd = self._master_fd
            self._master_fd = None
        with self._flow_gate:
            self._flow_gate.notify_all()
        process = self.process
        if process is not None and process.poll() is None:
            self._terminate_process(process)
        if master_fd is not None:
            self._close_master_fd(master_fd)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
        if process is not None:
            self._enqueue_exit_once(process.poll())

    @classmethod
    def _terminate_process(cls, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        cls._signal_child(process, signal.SIGHUP)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)
        if process.poll() is None:
            cls._signal_child(process, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
        if process.poll() is None:
            cls._signal_child(process, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)

    @staticmethod
    def _signal_child(process: subprocess.Popen[bytes], signum: int) -> None:
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        try:
            if pgid == process.pid:
                os.killpg(pgid, signum)
            else:
                os.kill(process.pid, signum)
        except ProcessLookupError:
            return

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
            await asyncio.to_thread(self.close)
            if websocket.application_state != WebSocketState.DISCONNECTED:
                with contextlib.suppress(Exception):
                    await websocket.close()

    async def _send_loop(self, websocket: WebSocket) -> None:
        await websocket.send_json(
            {
                "type": "hello",
                "capabilities": {"binaryInput": True},
            }
        )
        while True:
            frame = await asyncio.to_thread(self.read_output_batch)
            if isinstance(frame, TerminalOutputBatch):
                try:
                    await websocket.send_bytes(frame.data)
                finally:
                    self.mark_output_delivered(len(frame.data))
                if frame.exit_frame is None:
                    continue
                frame = frame.exit_frame
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
            payload = message.get("bytes")
            if payload is not None:
                await asyncio.to_thread(self.write_input, payload)
                continue
            payload = message.get("text")
            if not payload:
                continue
            try:
                body = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if body.get("type") == "input":
                await asyncio.to_thread(self.write_input, str(body.get("data", "")))
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
        self._creating: set[str] = set()

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
            if terminal_id in self._creating:
                raise TerminalAlreadyAttachedError("terminal is already starting")
            if not create:
                raise MissingTerminalSessionError("session-missing")
            if len(self._sessions) + len(self._creating) >= self.max_sessions:
                raise TerminalLimitError(
                    f"Terminal limit reached ({self.max_sessions}). Close another terminal and try again."
                )
            self._creating.add(terminal_id)

        try:
            session = TerminalSession(
                terminal_id,
                cwd=self.cwd,
                shell_path=self.shell_path,
            )
        except Exception:
            with self._lock:
                self._creating.discard(terminal_id)
            raise

        with self._lock:
            self._creating.discard(terminal_id)
            current = self._sessions.get(terminal_id)
            if current is not None:
                session.close()
                return current
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
def terminal_token(request: Request) -> dict[str, object]:
    require_trusted_request(request)
    return {
        "token": current_boot_token(),
        "limit": TERMINAL_MANAGER.max_sessions,
    }


@router.websocket("/ws/terminal/{terminal_id}")
async def terminal_socket(websocket: WebSocket, terminal_id: str) -> None:
    require_trusted_websocket(websocket)
    if not valid_terminal_id(terminal_id):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    token = websocket.query_params.get("token")
    if not token or not secrets.compare_digest(token, current_boot_token()):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    create = websocket.query_params.get("create") in {"1", "true", "yes"}
    await websocket.accept()
    try:
        session = await asyncio.to_thread(TERMINAL_MANAGER.get_or_create, terminal_id, create=create)
        session.attach()
    except MissingTerminalSessionError:
        await websocket.send_json(
            {
                "type": "missing",
                "message": "Session ended. Restart to launch a fresh shell.",
            }
        )
        await websocket.close(code=1000)
        return
    except TerminalLimitError as exc:
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
        await websocket.send_json(
            {
                "type": "error",
                "code": "attached",
                "message": "Terminal is already open in another pane.",
            }
        )
        await websocket.close(code=1000)
        return

    try:
        await session.serve(websocket)
    finally:
        TERMINAL_MANAGER.discard(terminal_id, session)
