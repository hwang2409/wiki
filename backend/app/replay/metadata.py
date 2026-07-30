"""Bounded ``run.json`` reads + ticket-scoped run discovery."""

from __future__ import annotations

import json
import os
import stat
from typing import Any

from . import errors as e
from .errors import ReplayError, valid_run_id
from .models import RunSummary, TicketRunsListing
from .reader import _open_run_child_fd


def _read_bounded_metadata(runs_root_fd: int, run_id: str) -> dict[str, Any]:
    cap = e.MAX_RUN_JSON_BYTES
    fd = _open_run_child_fd(runs_root_fd, run_id, "run.json")
    try:
        info = os.fstat(fd)
        if info.st_size > cap:
            raise ReplayError(
                f"run.json exceeds {cap}-byte ceiling", status_code=413
            )
        chunks: list[bytes] = []
        remaining = cap + 1
        while remaining > 0:
            chunk = os.read(fd, min(e.STREAM_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > cap:
            raise ReplayError(
                f"run.json exceeds {cap}-byte ceiling", status_code=413
            )
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ReplayError(
            f"run.json is not valid JSON: {exc}", status_code=500
        ) from exc
    if not isinstance(value, dict):
        raise ReplayError("run.json must contain an object", status_code=500)
    return value


def _excerpt(text: str, limit: int = 120) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _run_summary_from_meta(meta: dict[str, Any], fallback_run_id: str) -> RunSummary:
    initial_prompt = meta.get("initial_prompt")
    initial_excerpt = (
        _excerpt(initial_prompt, 240)
        if isinstance(initial_prompt, str) and initial_prompt.strip()
        else None
    )
    total_events = meta.get("normalized_event_count")
    if not isinstance(total_events, int):
        total_events = meta.get("raw_event_count") or 0
    return RunSummary(
        run_id=str(meta.get("run_id") or fallback_run_id),
        agent_id=meta.get("agent_id") if isinstance(meta.get("agent_id"), str) else None,
        orch_id=meta.get("orchestrator_id") if isinstance(meta.get("orchestrator_id"), str) else None,
        role=meta.get("role") if isinstance(meta.get("role"), str) else None,
        provider=meta.get("provider") if isinstance(meta.get("provider"), str) else None,
        model=meta.get("model") if isinstance(meta.get("model"), str) else None,
        outcome=meta.get("outcome") if isinstance(meta.get("outcome"), str) else None,
        state=meta.get("state") if isinstance(meta.get("state"), str) else None,
        created_at=meta.get("created_at") if isinstance(meta.get("created_at"), str) else None,
        updated_at=meta.get("updated_at") if isinstance(meta.get("updated_at"), str) else None,
        total_events=int(total_events),
        initial_prompt_excerpt=initial_excerpt,
    )


def build_run_summary(runs_root_fd: int, run_id: str) -> RunSummary:
    meta = _read_bounded_metadata(runs_root_fd, run_id)
    return _run_summary_from_meta(meta, run_id)


def _iter_root_entries(runs_root_fd: int) -> list[tuple[str, float]]:
    """Return ``(name, mtime)`` for non-symlinked subdirs of the runs root."""

    entries: list[tuple[str, float]] = []
    root_fd = os.dup(runs_root_fd)
    try:
        with os.scandir(root_fd) as scanner:
            for entry in scanner:
                if not valid_run_id(entry.name):
                    continue
                try:
                    info = os.stat(entry.name, dir_fd=runs_root_fd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                entries.append((entry.name, info.st_mtime))
    finally:
        os.close(root_fd)
    entries.sort(key=lambda pair: pair[1], reverse=True)
    return entries


def resolve_ticket_runs(runs_root_fd: int, ticket: str) -> TicketRunsListing:
    entries = _iter_root_entries(runs_root_fd)
    matches: list[RunSummary] = []
    truncated = False
    scanned = 0
    for name, _mtime in entries:
        if scanned >= e.MAX_RUN_LIST_SCAN:
            truncated = True
            break
        scanned += 1
        try:
            meta = _read_bounded_metadata(runs_root_fd, name)
        except ReplayError:
            continue
        agent_id = meta.get("agent_id")
        if agent_id != ticket:
            continue
        matches.append(_run_summary_from_meta(meta, name))
        if len(matches) >= e.MAX_RUN_LIST_ENTRIES:
            truncated = True
            break
    return TicketRunsListing(runs=matches, truncated=truncated)
