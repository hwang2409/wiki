"""Run metadata and normalized-event parsing for the Wiki knowledge index."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .knowledge_content import event_text


EVENT_CHUNK_EXCERPT_MAX_CHARS = 2_048
BASE64_BLOB_MIN_CHARS = 1_024
ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]"
)
BASE64_LINE_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")
DATA_URI_BASE64_RE = re.compile(r"data:[^,\s]+;base64,([A-Za-z0-9+/]+={0,2})\Z", re.I)


@dataclass(frozen=True)
class ParsedRunEvent:
    seq: int
    event_type: str
    ts: str | None
    text: str
    excerpt: str


@dataclass(frozen=True)
class RunEventBatch:
    events: tuple[ParsedRunEvent, ...]
    malformed_lines: int
    event_chunks_excerpted: int
    base64_blob_lines_skipped: int
    ansi_heavy_lines_skipped: int


@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    ticket: str | None
    provider: str | None
    model: str | None
    role: str | None
    spawned_at: str | None
    ended_at: str | None
    outcome: str | None


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def last_event_seq(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            offset = max(0, end - 256 * 1024)
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return 0
    lines = data.splitlines()
    if offset and lines:
        lines = lines[1:]
    for raw in reversed(lines):
        try:
            value = json.loads(raw)
            seq = int(value.get("seq", 0)) if isinstance(value, dict) else 0
        except (ValueError, TypeError):
            continue
        if seq > 0:
            return seq
    return 0


def load_run_metadata(run_dir: Path) -> RunMetadata:
    run = _read_json(run_dir / "run.json")
    meta = _read_json(run_dir / "meta.json")
    worker = meta.get("worker") if isinstance(meta.get("worker"), dict) else {}
    ticket = (
        run.get("agent_id")
        or worker.get("ticket")
        or worker.get("agent_id")
        or (run_dir.parent.name if run_dir.parent.name else None)
    )
    run_id = run.get("run_id") or worker.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        digest = hashlib.sha256(str(run_dir.absolute()).encode()).hexdigest()[:24]
        run_id = f"archive-{digest}"
    provider = run.get("provider") or worker.get("provider")
    if provider is None:
        provider = {"cdx": "codex", "cc": "claude"}.get(worker.get("kind"))
    return RunMetadata(
        run_id=run_id,
        ticket=str(ticket) if ticket else None,
        provider=str(provider) if provider else None,
        model=_optional_string(run.get("model") or worker.get("model")),
        role=_optional_string(run.get("role") or worker.get("role")),
        spawned_at=_optional_string(run.get("created_at") or worker.get("spawned_at")),
        ended_at=_optional_string(meta.get("ended_at") or worker.get("ended_at")),
        outcome=_optional_string(
            meta.get("outcome") or run.get("outcome") or worker.get("outcome")
        ),
    )


def is_supervisor_archive(run_dir: Path) -> bool:
    """Whether an archive was produced by the durable headless supervisor."""

    return _read_json(run_dir / "meta.json").get("source") == "headless-supervisor"


def _is_base64_blob_line(line: str) -> bool:
    candidate = "".join(line.split())
    match = DATA_URI_BASE64_RE.fullmatch(candidate)
    if match:
        candidate = match.group(1)
    alphabet = set(candidate.rstrip("="))
    return (
        len(candidate) >= BASE64_BLOB_MIN_CHARS
        and len(candidate) % 4 == 0
        and len(alphabet) >= 4
        and BASE64_LINE_RE.fullmatch(candidate) is not None
    )


def _is_ansi_heavy_line(line: str) -> bool:
    escapes = list(ANSI_ESCAPE_RE.finditer(line))
    if not escapes:
        return False
    ansi_chars = sum(len(match.group(0)) for match in escapes)
    visible_chars = len(ANSI_ESCAPE_RE.sub("", line).strip())
    return ansi_chars >= max(16, visible_chars)


def _strip_low_value_lines(text: str) -> tuple[str, int, int]:
    retained: list[str] = []
    base64_blob_lines_skipped = 0
    ansi_heavy_lines_skipped = 0
    for line in text.splitlines():
        if _is_base64_blob_line(line):
            base64_blob_lines_skipped += 1
        elif _is_ansi_heavy_line(line):
            ansi_heavy_lines_skipped += 1
        else:
            retained.append(line)
    return "\n".join(retained), base64_blob_lines_skipped, ansi_heavy_lines_skipped


def read_run_events(path: Path, *, after_seq: int) -> RunEventBatch:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    events: list[ParsedRunEvent] = []
    malformed = 0
    event_chunks_excerpted = 0
    base64_blob_lines_skipped = 0
    ansi_heavy_lines_skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            seq = int(event.get("seq", 0))
            if seq < 1:
                raise ValueError("event sequence is missing")
        except (TypeError, ValueError):
            malformed += 1
            continue
        if seq <= after_seq:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        event_type = str(
            event.get("kind")
            or payload.get("type")
            or payload.get("method")
            or "unknown"
        )
        text = event_text(event)
        indexed_text, base64_lines, ansi_lines = _strip_low_value_lines(text)
        base64_blob_lines_skipped += base64_lines
        ansi_heavy_lines_skipped += ansi_lines
        if len(indexed_text) > EVENT_CHUNK_EXCERPT_MAX_CHARS:
            indexed_text = indexed_text[:EVENT_CHUNK_EXCERPT_MAX_CHARS]
            event_chunks_excerpted += 1
        ts = event.get("normalized_at") or event.get("ts")
        if not ts:
            ts = payload.get("timestamp") or payload.get("ts")
        events.append(
            ParsedRunEvent(
                seq=seq,
                event_type=event_type,
                ts=str(ts) if ts is not None else None,
                text=indexed_text,
                excerpt=re.sub(r"\s+", " ", indexed_text).strip()[:500],
            )
        )
    return RunEventBatch(
        tuple(events),
        malformed,
        event_chunks_excerpted,
        base64_blob_lines_skipped,
        ansi_heavy_lines_skipped,
    )
