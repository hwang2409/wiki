from __future__ import annotations

import asyncio
import json
import socket
import stat
from pathlib import Path
from typing import Any

from .supervisor import Supervisor


# Existing agent kickoff prompts may be 100 KiB. Leave bounded headroom for
# JSON metadata and future protocol fields while rejecting unbounded lines.
MAX_PROTOCOL_LINE_BYTES = 256 * 1024


class UnixSupervisorServer:
    def __init__(self, supervisor: Supervisor, socket_path: Path):
        self.supervisor = supervisor
        self.socket_path = socket_path
        self.server: asyncio.AbstractServer | None = None
        self._bound_ino: int | None = None

    async def start(self) -> None:
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
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
            limit=MAX_PROTOCOL_LINE_BYTES,
        )
        self.socket_path.chmod(0o600)
        self._bound_ino = self.socket_path.lstat().st_ino

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
            except (BrokenPipeError, ConnectionResetError):
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
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        try:
            # A successor daemon may have rebound this path; only remove the
            # socket this server created (WIKI-217).
            current = self.socket_path.lstat()
            if stat.S_ISSOCK(current.st_mode) and current.st_ino == self._bound_ino:
                self.socket_path.unlink()
        except OSError:
            pass

    async def __aenter__(self) -> UnixSupervisorServer:
        await self.start()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()
