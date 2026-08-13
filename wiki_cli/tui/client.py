"""HTTP + SSE client for the local sidecar, stdlib only.

Kept small and injectable — the app uses these plain functions so tests
can swap in a fake transport.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Callable, Iterator
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT = 5.0
SSE_TIMEOUT = 60.0


class BackendUnavailable(Exception):
    """Raised when the sidecar is unreachable or returns non-JSON."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:  # pragma: no cover
        return self.reason


def get_json(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
        raise BackendUnavailable(reason=f"connect failed: {exc}") from exc
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise BackendUnavailable(reason=f"invalid json: {exc}") from exc


def iter_sse(url: str, *, timeout: float = SSE_TIMEOUT) -> Iterator[dict]:
    """Yield parsed JSON payloads from a text/event-stream endpoint.

    Yields one dict per ``data:`` frame; skips comment lines. Raises
    :class:`BackendUnavailable` on connection failure. Stops silently
    when the server closes the stream — the caller decides whether to
    reconnect.
    """
    req = Request(url, headers={"Accept": "text/event-stream"})
    try:
        resp = urlopen(req, timeout=timeout)
    except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
        raise BackendUnavailable(reason=f"sse connect failed: {exc}") from exc

    with resp:
        buf: list[str] = []
        while True:
            try:
                raw = resp.readline()
            except (socket.timeout, TimeoutError):
                # Idle heartbeat window elapsed with no bytes; server
                # will send a ": ping" every ~15s so a real gap here
                # means the connection died.
                return
            except (OSError, ConnectionError):
                return
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if buf:
                    payload = "\n".join(buf)
                    buf = []
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                buf.append(line[5:].lstrip())


def start_sse_thread(
    url: str, on_event: Callable[[dict], None], stop: threading.Event
) -> threading.Thread:
    """Run :func:`iter_sse` in a background thread with reconnect.

    Reconnects with a brief back-off; stops when ``stop`` is set.
    Callers must be tolerant of ``on_event`` firing from a non-main
    thread.
    """

    def _run() -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                for event in iter_sse(url):
                    if stop.is_set():
                        return
                    on_event(event)
                backoff = 1.0
            except BackendUnavailable:
                if stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 15.0)
                continue
            # normal stream end — try again shortly
            if stop.wait(1.0):
                return

    t = threading.Thread(target=_run, name="wiki-tui-sse", daemon=True)
    t.start()
    return t
