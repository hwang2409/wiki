"""Bounded tail extractor for worker raw.jsonl → short "screencast" frames.

The fleet view calls this once per poll (per visible worker set) to produce
a few dozen bytes of human-readable text per worker — the tail of what each
worker's provider stream produced most recently. raw.jsonl grows without
bound (multi-GB in long-running fleets), so this module:

* reads a fixed byte window from the end of the file (never the whole file)
* skips torn head/tail fragments (only newline-terminated JSON records count)
* extracts renderable text — assistant messages, user echoes, command
  output snippets — from claude and codex payload envelopes, not raw JSON

Path safety: callers pass an already-opened runs-root dir fd; each run
component is opened via ``pathwalk.open_relative_file`` with O_NOFOLLOW so
a symlink swapped in for the run directory or raw.jsonl fails immediately.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from typing import Iterable

from .pathwalk import open_relative_file


DEFAULT_WINDOW_BYTES = 256 * 1024
DEFAULT_MAX_FRAMES = 20
MAX_FRAME_TEXT = 200
_ELLIPSIS = "…"


@dataclass(frozen=True)
class ScreencastFrame:
    """One short line of the strip: kind tag + text + optional timestamp."""

    kind: str  # "assistant" | "user" | "tool" | "marker"
    text: str
    ts: str | None


def _clip(text: str, limit: int = MAX_FRAME_TEXT) -> str:
    text = text.strip()
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + _ELLIPSIS


def _iter_newline_terminated(chunk: bytes, *, drop_head: bool) -> Iterable[bytes]:
    """Yield only newline-terminated lines from ``chunk``.

    ``drop_head`` discards the first fragment (torn by seeking past a line
    boundary). The final fragment is always dropped when it is not
    newline-terminated — writers extending raw.jsonl append incomplete
    records that must not be parsed.
    """

    if not chunk:
        return
    ends_with_newline = chunk.endswith(b"\n")
    parts = chunk.split(b"\n")
    # ``split`` on a trailing newline gives a trailing empty part; drop it.
    if ends_with_newline:
        parts = parts[:-1]
    else:
        # Last part is a torn record still being written; drop it.
        parts = parts[:-1]
    if drop_head and parts:
        parts = parts[1:]
    for part in parts:
        if part:
            yield part


def _read_tail_chunk(raw_fd: int, *, window_bytes: int) -> tuple[bytes, bool]:
    """Read up to ``window_bytes`` from the end of ``raw_fd``.

    Returns ``(chunk, seeked)``. ``seeked`` is True when the read started
    past the file head — the caller must then discard the first (torn)
    fragment. Reads via ``os.pread`` so the shared fd's position is not
    mutated.
    """

    raw_stat = os.fstat(raw_fd)
    if not stat.S_ISREG(raw_stat.st_mode):
        return b"", False
    size = raw_stat.st_size
    if size <= 0:
        return b"", False
    if size <= window_bytes:
        return os.pread(raw_fd, size, 0), False
    offset = size - window_bytes
    return os.pread(raw_fd, window_bytes, offset), True


# -------------------------------------------------------------- extraction


def _claude_text_from_message(message: object) -> tuple[str, str] | None:
    """Return (kind, text) for a rendered claude envelope, or None to skip."""

    if not isinstance(message, dict):
        return None
    role = message.get("role")
    content = message.get("content")
    if not isinstance(content, list):
        return None
    fragments: list[str] = []
    tool_hint: str | None = None
    saw_tool_result = False
    for entry in content:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type == "text":
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                fragments.append(text)
        elif entry_type == "tool_use":
            name = entry.get("name")
            if isinstance(name, str) and not tool_hint:
                tool_hint = name
        elif entry_type == "tool_result":
            saw_tool_result = True
            inner = entry.get("content")
            if isinstance(inner, str) and inner.strip():
                fragments.append(inner)
            elif isinstance(inner, list):
                for chunk in inner:
                    if (
                        isinstance(chunk, dict)
                        and chunk.get("type") == "text"
                        and isinstance(chunk.get("text"), str)
                    ):
                        fragments.append(chunk["text"])
    joined = "\n".join(fragments).strip()
    if role == "assistant":
        if joined:
            return "assistant", joined
        if tool_hint:
            return "tool", f"→ {tool_hint}"
        return None
    if role == "user":
        if saw_tool_result:
            if joined:
                return "tool", joined
            return None
        if joined:
            return "user", joined
    return None


def _codex_text_from_item(item: object) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == "message":
        role = item.get("role")
        content = item.get("content")
        if not isinstance(content, list):
            return None
        pieces: list[str] = []
        for chunk in content:
            if (
                isinstance(chunk, dict)
                and isinstance(chunk.get("text"), str)
                and chunk["text"].strip()
            ):
                pieces.append(chunk["text"])
        text = "\n".join(pieces).strip()
        if not text:
            return None
        if role == "assistant":
            return "assistant", text
        if role == "user":
            return "user", text
        return None
    if item_type in {
        "custom_tool_call_output",
        "function_call_output",
        "local_shell_call_output",
    }:
        output = item.get("output")
        if isinstance(output, str) and output.strip():
            return "tool", output
        return None
    if item_type in {"custom_tool_call", "function_call", "local_shell_call"}:
        name = item.get("name") or item.get("call_type") or item_type
        return "tool", f"→ {name}"
    return None


def _frame_from_envelope(envelope: object) -> ScreencastFrame | None:
    if not isinstance(envelope, dict):
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    ts = envelope.get("received_at") if isinstance(envelope.get("received_at"), str) else None
    provider = envelope.get("provider")
    if provider == "claude":
        payload_type = payload.get("type")
        if payload_type in {"assistant", "user"}:
            extracted = _claude_text_from_message(payload.get("message"))
            if extracted is None:
                return None
            kind, text = extracted
            return ScreencastFrame(kind=kind, text=_clip(text), ts=ts)
        if payload_type == "result":
            subtype = payload.get("subtype")
            if isinstance(subtype, str) and subtype.startswith("error"):
                return ScreencastFrame(kind="marker", text=_clip(f"[{subtype}]"), ts=ts)
        return None
    if provider in {"codex", "cdx"}:
        method = payload.get("method")
        if method == "rawResponseItem/completed":
            params = payload.get("params")
            if not isinstance(params, dict):
                return None
            extracted = _codex_text_from_item(params.get("item"))
            if extracted is None:
                return None
            kind, text = extracted
            return ScreencastFrame(kind=kind, text=_clip(text), ts=ts)
        if method == "turn/completed":
            params = payload.get("params")
            if isinstance(params, dict):
                turn = params.get("turn")
                if isinstance(turn, dict):
                    status = turn.get("status")
                    if isinstance(status, str) and status not in {"completed", None}:
                        return ScreencastFrame(kind="marker", text=_clip(f"[{status}]"), ts=ts)
        return None
    return None


def frames_from_lines(
    lines: Iterable[bytes], *, max_frames: int = DEFAULT_MAX_FRAMES
) -> list[ScreencastFrame]:
    """Convert newline-terminated JSON envelopes into up to ``max_frames``.

    Consumes ``lines`` in order but keeps only the last ``max_frames``
    rendered frames — the strip shows the newest tail.
    """

    frames: list[ScreencastFrame] = []
    for line in lines:
        try:
            envelope = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        frame = _frame_from_envelope(envelope)
        if frame is None:
            continue
        frames.append(frame)
        if len(frames) > max_frames:
            del frames[0]
    return frames


# ------------------------------------------------------------- entry point


def tail_frames(
    runs_root_fd: int,
    run_id: str,
    *,
    window_bytes: int = DEFAULT_WINDOW_BYTES,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> list[ScreencastFrame]:
    """Bounded end-of-file read → renderable frames for one run.

    Opens ``<run_id>/raw.jsonl`` under ``runs_root_fd`` with O_NOFOLLOW per
    component (via ``pathwalk``) and reads at most ``window_bytes`` from
    the end. Returns ``[]`` for missing files, symlinks, non-regular
    files, or empty files.
    """

    try:
        raw_fd = open_relative_file(
            runs_root_fd,
            (run_id, "raw.jsonl"),
            extra_final_flags=getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        return []
    try:
        chunk, seeked = _read_tail_chunk(raw_fd, window_bytes=window_bytes)
    finally:
        os.close(raw_fd)
    if not chunk:
        return []
    return frames_from_lines(
        _iter_newline_terminated(chunk, drop_head=seeked),
        max_frames=max_frames,
    )


def frame_to_dict(frame: ScreencastFrame) -> dict[str, object]:
    return {"kind": frame.kind, "text": frame.text, "ts": frame.ts}
