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


def read_run_events(path: Path, *, after_seq: int) -> RunEventBatch:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    events: list[ParsedRunEvent] = []
    malformed = 0
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
        ts = event.get("normalized_at") or event.get("ts")
        if not ts:
            ts = payload.get("timestamp") or payload.get("ts")
        events.append(
            ParsedRunEvent(
                seq=seq,
                event_type=event_type,
                ts=str(ts) if ts is not None else None,
                text=text,
                excerpt=re.sub(r"\s+", " ", text).strip()[:500],
            )
        )
    return RunEventBatch(tuple(events), malformed)
