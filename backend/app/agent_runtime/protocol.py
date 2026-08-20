from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
from pathlib import Path
from typing import Any

from .supervisor import Supervisor


# Existing agent kickoff prompts may be 100 KiB. Leave bounded headroom for
# JSON metadata and future protocol fields while rejecting unbounded lines.
MAX_PROTOCOL_LINE_BYTES = 256 * 1024
_LISTENER_BACKOFF_INITIAL_SECONDS = 0.05
_LISTENER_BACKOFF_MAX_SECONDS = 1.0
_LISTENER_BACKLOG = 100
logger = logging.getLogger(__name__)


class UnixSupervisorServer:
    def __init__(self, supervisor: Supervisor, socket_path: Path):
        self.supervisor = supervisor
        self.socket_path = socket_path
        self.server: asyncio.AbstractServer | None = None
        self._bound_ino: int | None = None
        self._listener: socket.socket | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._closing = False

    async def start(self) -> None:
        if self._listener_task is not None:
            raise RuntimeError("supervisor server is already running")
        self._closing = False
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            mode = self.socket_path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"refusing to replace non-socket path: {self.socket_path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.1)
            try:
                probe.connect(str(self.socket_path))
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            except socket.timeout as exc:
                raise RuntimeError(
                    f"supervisor socket exists but did not accept promptly: {self.socket_path}"
                ) from exc
            else:
                raise RuntimeError(f"supervisor socket is already active: {self.socket_path}")
            finally:
                probe.close()
            self.socket_path.unlink()
        self._bind_listener()
        self._listener_task = asyncio.create_task(
            self._supervise_listener(),
            name="agent-supervisor-socket-listener",
        )

    def _bind_listener(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_ino: int | None = None
        try:
            listener.setblocking(False)
            listener.bind(str(self.socket_path))
            listener.listen(_LISTENER_BACKLOG)
            bound_ino = self.socket_path.lstat().st_ino
            self.socket_path.chmod(0o600)
        except BaseException:
            listener.close()
            if bound_ino is not None:
                self._unlink_bound_socket_at(bound_ino)
            raise
        self._bound_ino = bound_ino
        self._listener = listener

    def _unlink_bound_socket(self) -> None:
        if self._bound_ino is not None:
            self._unlink_bound_socket_at(self._bound_ino)

    def _unlink_bound_socket_at(self, bound_ino: int) -> None:
        try:
            current = self.socket_path.lstat()
            if stat.S_ISSOCK(current.st_mode) and current.st_ino == bound_ino:
                self.socket_path.unlink()
        except OSError:
            pass

    async def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            raise RuntimeError("supervisor listener is not bound")
        loop = asyncio.get_running_loop()
        while True:
            try:
                connection, _ = await loop.sock_accept(listener)
            except ConnectionAbortedError:
                continue
            except (BlockingIOError, InterruptedError):
                continue
            try:
                connection.setblocking(False)
                task = asyncio.create_task(
                    self._serve_connection(connection),
                    name="agent-supervisor-client",
                )
            except BaseException:
                connection.close()
                raise
            self._client_tasks.add(task)
            task.add_done_callback(self._client_task_done)

    def _client_task_done(self, task: asyncio.Task[None]) -> None:
        self._client_tasks.discard(task)
        if not task.cancelled() and (exception := task.exception()) is not None:
            logger.warning(
                "supervisor client task failed exception=%s message=%s",
                type(exception).__name__,
                str(exception),
            )

    async def _supervise_listener(self) -> None:
        backoff = _LISTENER_BACKOFF_INITIAL_SECONDS
        while not self._closing:
            try:
                await self._accept_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "supervisor listener crashed exception=%s message=%s fd_count=%s active_connections=%s",
                    type(exc).__name__,
                    str(exc),
                    self._fd_count(),
                    len(self._client_tasks),
                )
                self._close_listener()
                if self._closing:
                    return
                while not self._closing:
                    await asyncio.sleep(backoff)
                    if self._closing:
                        return
                    try:
                        self._bind_listener()
                    except asyncio.CancelledError:
                        raise
                    except Exception as restart_exc:
                        logger.error(
                            "supervisor listener restart failed exception=%s message=%s fd_count=%s active_connections=%s",
                            type(restart_exc).__name__,
                            str(restart_exc),
                            self._fd_count(),
                            len(self._client_tasks),
                        )
                        backoff = min(backoff * 2, _LISTENER_BACKOFF_MAX_SECONDS)
                    else:
                        backoff = _LISTENER_BACKOFF_INITIAL_SECONDS
                        break

    @staticmethod
    def _fd_count() -> int:
        try:
            return len(os.listdir("/dev/fd"))
        except OSError:
            return -1

    def _close_listener(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        self._unlink_bound_socket()

    async def _serve_connection(self, connection: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MAX_PROTOCOL_LINE_BYTES)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        try:
            transport, _ = await loop.connect_accepted_socket(
                lambda: protocol,
                connection,
            )
        except BaseException:
            connection.close()
            raise
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        self._writers.add(writer)
        try:
            await self._handle_client(reader, writer)
        finally:
            self._writers.discard(writer)

    async def _write(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        await writer.drain()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while line := await reader.readline():
                request_id: str | int | None = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                    request_id = request.get("id")
                    method = request.get("method")
                    params = request.get("params") or {}
                    if not isinstance(method, str) or not isinstance(params, dict):
                        raise ValueError("request requires string method and object params")
                    if method == "events/subscribe":
                        await self._subscribe(request_id, reader, writer)
                        return
                    result = await self.supervisor.dispatch(method, params)
                    await self._write(writer, {"id": request_id, "result": result})
                except Exception as exc:
                    await self._write(
                        writer,
                        {
                            "id": request_id,
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc),
                            },
                        },
                    )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    async def _subscribe(
        self,
        request_id: str | int | None,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        queue = self.supervisor.subscribe()
        disconnected = asyncio.create_task(reader.read())
        try:
            await self._write(writer, {"id": request_id, "result": {"subscribed": True}})
            while True:
                next_event = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {next_event, disconnected},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnected in done:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    return
                await self._write(writer, {"event": next_event.result()})
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            self.supervisor.unsubscribe(queue)

    async def close(self) -> None:
        self._closing = True
        self._close_listener()
        listener_task = self._listener_task
        self._listener_task = None
        if listener_task is not None:
            listener_task.cancel()
            await asyncio.gather(listener_task, return_exceptions=True)
        for writer in list(self._writers):
            writer.close()
        client_tasks = list(self._client_tasks)
        for task in client_tasks:
            task.cancel()
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        self.server = None

    async def __aenter__(self) -> UnixSupervisorServer:
        await self.start()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()
