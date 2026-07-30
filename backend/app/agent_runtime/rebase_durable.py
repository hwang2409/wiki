"""Durable job/outbox state for the rebase worker.

State lives in three JSON files under ``AGENT_RUNTIME_DIR/rebase-bot/``:
``jobs.json`` (per-job records), ``outbox.json`` (pending notifications),
``delivered.json`` (deduplication of already-sent events).  Every terminal
transition persists both the job record and its outbox entry in a single
state write so a crash between the two cannot lose a result notification.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NotificationSender = Callable[[str, str], None]

_DURABLE_STATE_LOADED = False
_DURABLE_STATE_ROOT: Path | None = None
_DURABLE_JOBS: dict[str, dict[str, Any]] = {}
_OUTBOX: dict[str, dict[str, Any]] = {}
_DELIVERED_EVENTS: set[str] = set()
_JOB_RETENTION_SECONDS = 900
_OUTBOX_MAX_ATTEMPTS = 5
_OUTBOX_BACKOFF_BASE_SECONDS = 2.0
_OUTBOX_BACKOFF_CAP_SECONDS = 300.0


@dataclass
class _RebaseJob:
    job_id: str
    worktree: Path
    prompt: str
    done: threading.Event
    helper: Callable[[Path], Mapping[str, Any]] | None = None
    result: dict[str, Any] | None = None
    pr_number: int = 0
    expected_sha: str = ""
    ticket: str = ""
    worker_id: str = ""
    orchestrator: str | None = None
    verdict: dict[str, Any] | None = None
    durable: bool = False
    completed_at: float | None = None


def _main() -> Any:
    """Return the backend main module.

    Delegates to :func:`rebase_bot._main` so a single ``mock.patch.object``
    on ``rebase_bot._main`` covers both call sites — the orchestrator layer
    and every durable-state helper here.  A direct ``from .. import main``
    would leave this module reading the real runtime dir even when tests
    have swapped ``rebase_bot._main`` for a fake.
    """

    from . import rebase_bot

    return rebase_bot._main()


def _durable_state_path() -> Path:
    """Single-file snapshot path.

    Consolidating jobs, outbox, and delivery dedupe into one JSON blob is
    what makes ``_persist_durable_state`` genuinely atomic — a torn write
    between three sibling files used to leave a completed job with no
    outbox entry, permanently losing the notification.
    """

    try:
        runtime_dir = getattr(_main(), "AGENT_RUNTIME_DIR", None)
    except Exception:
        runtime_dir = None
    root = (
        Path(runtime_dir)
        if isinstance(runtime_dir, (str, Path))
        else Path(tempfile.gettempdir()) / "wiki-agent-runtime"
    )
    return root / "rebase-bot" / "state.json"


def _load_durable_state() -> None:
    global _DURABLE_STATE_LOADED, _DURABLE_STATE_ROOT
    state_path = _durable_state_path()
    state_root = state_path.parent
    if _DURABLE_STATE_LOADED and _DURABLE_STATE_ROOT == state_root:
        return
    if _DURABLE_STATE_ROOT != state_root:
        _DURABLE_JOBS.clear()
        _OUTBOX.clear()
        _DELIVERED_EVENTS.clear()
    _DURABLE_STATE_ROOT = state_root
    _DURABLE_STATE_LOADED = True
    try:
        snapshot = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(snapshot, Mapping):
        return
    jobs_section = snapshot.get("jobs")
    if isinstance(jobs_section, Mapping):
        _DURABLE_JOBS.update(
            {
                str(key): dict(item)
                for key, item in jobs_section.items()
                if isinstance(key, str) and isinstance(item, Mapping)
            }
        )
    outbox_section = snapshot.get("outbox")
    if isinstance(outbox_section, Mapping):
        _OUTBOX.update(
            {
                str(key): dict(item)
                for key, item in outbox_section.items()
                if isinstance(key, str) and isinstance(item, Mapping)
            }
        )
    delivered_section = snapshot.get("delivered")
    if isinstance(delivered_section, list):
        for entry in delivered_section:
            if isinstance(entry, str):
                _DELIVERED_EVENTS.add(entry)


def _persist_durable_state() -> None:
    state_path = _durable_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "jobs": _DURABLE_JOBS,
        "outbox": _OUTBOX,
        "delivered": sorted(_DELIVERED_EVENTS),
    }
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(state_path)


def _durable_key(pr_number: int, expected_sha: str) -> str:
    return f"{pr_number}:{expected_sha}"


def _delivery_id(job: _RebaseJob, result: Mapping[str, Any]) -> str:
    # Stable across process restarts so a delivered event never re-enqueues.
    status = str(result.get("status") or "")
    head_sha = str(result.get("head_sha") or "")
    return f"{job.pr_number}:{job.expected_sha}:{status}:{head_sha}"


def _prune_durable_jobs(clear_job_slot: Callable[[int, str], None] | None = None) -> None:
    cutoff = time.time() - _JOB_RETENTION_SECONDS
    for key, record in list(_DURABLE_JOBS.items()):
        completed_at = record.get("completed_at")
        if (
            record.get("status") in {"completed", "failed"}
            and isinstance(completed_at, (int, float))
            and completed_at < cutoff
        ):
            _DURABLE_JOBS.pop(key, None)
            if clear_job_slot is not None:
                try:
                    pr_number, expected_sha = key.split(":", 1)
                    clear_job_slot(int(pr_number), expected_sha)
                except (ValueError, TypeError):
                    pass


def _result_message(worker_id: str, result: Mapping[str, Any]) -> str | None:
    status = result.get("status")
    if status == "resolved":
        resolved_files = ", ".join(
            str(item) for item in result.get("resolved_files", [])
        )
        return f"rebase-bot resolved {worker_id}: " + (
            resolved_files or "rebase completed"
        )
    if status == "escalated":
        return f"rebase-bot escalated {worker_id}: " + "; ".join(
            str(item) for item in result.get("escalated_hunks", [])
        )
    return None


def _job_record(job: _RebaseJob, result: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "pr_number": job.pr_number,
        "expected_sha": job.expected_sha,
        "ticket": job.ticket,
        "worker_id": job.worker_id,
        "worktree": str(job.worktree),
        "orchestrator": job.orchestrator,
        "prompt": job.prompt,
        "verdict": job.verdict or {},
        "status": "completed" if result is not None else "running",
        "result": dict(result) if result is not None else None,
        "updated_at": time.time(),
        "completed_at": time.time() if result is not None else None,
    }


def _persist_job(job: _RebaseJob, result: Mapping[str, Any] | None = None) -> None:
    if not job.durable:
        return
    _load_durable_state()
    key = _durable_key(job.pr_number, job.expected_sha)
    record = _DURABLE_JOBS.setdefault(key, {})
    record.update(_job_record(job, result))
    _persist_durable_state()


def _outbox_entry(job: _RebaseJob, result: Mapping[str, Any]) -> dict[str, Any] | None:
    message = _result_message(job.worker_id, result)
    if message is None:
        return None
    return {
        "id": f"{job.job_id}:result",
        "delivery_id": _delivery_id(job, result),
        "target": job.orchestrator,
        "worker_id": job.worker_id,
        "result": dict(result),
        "attempts": 0,
        "last_error": None,
        "next_attempt_at": 0.0,
    }


def _enqueue_result(job: _RebaseJob, result: Mapping[str, Any]) -> None:
    """Add ``result`` to the outbox unless its logical event was delivered."""

    if not job.durable or not job.orchestrator:
        return
    _load_durable_state()
    delivery_id = _delivery_id(job, result)
    if delivery_id in _DELIVERED_EVENTS:
        return
    entry = _outbox_entry(job, result)
    if entry is None:
        return
    _OUTBOX.setdefault(entry["id"], entry)
    _persist_durable_state()


def _persist_completion(job: _RebaseJob, result: Mapping[str, Any]) -> None:
    """Record the terminal job state and outbox entry in one state write."""

    if not job.durable:
        return
    _load_durable_state()
    key = _durable_key(job.pr_number, job.expected_sha)
    record = _DURABLE_JOBS.setdefault(key, {})
    record.update(_job_record(job, result))
    if job.orchestrator:
        delivery_id = _delivery_id(job, result)
        if delivery_id not in _DELIVERED_EVENTS:
            entry = _outbox_entry(job, result)
            if entry is not None:
                _OUTBOX.setdefault(entry["id"], entry)
    _persist_durable_state()


def _flush_outbox(
    notify: NotificationSender | None = None,
    *,
    _now: Callable[[], float] = time.time,
) -> None:
    """Drain the outbox with at-most-once semantics per ``delivery_id``.

    The delivery attempt is recorded — ``_DELIVERED_EVENTS`` gets the id and
    the entry is popped, then the state file is replaced — BEFORE the send
    call.  If the sender delivered then raised, no retry re-sends the same
    event; if the sender raised before delivery, the loss is bounded to one
    event rather than an unbounded flood of duplicates.  The persisted
    delivery id is a stable ``(pr, sha, status, head_sha)`` key so downstream
    receivers (or a future receiver-side dedupe) can spot a duplicate that
    escapes a torn write.
    """

    _load_durable_state()
    if notify is None:
        return
    now = _now()
    dispatch: list[tuple[str, str, str]] = []
    changed = False
    for entry_id, entry in list(_OUTBOX.items()):
        target = entry.get("target")
        result = entry.get("result")
        worker_id = entry.get("worker_id")
        if not isinstance(target, str) or not isinstance(result, Mapping):
            _OUTBOX.pop(entry_id, None)
            changed = True
            continue
        delivery_id = str(entry.get("delivery_id") or "")
        if delivery_id and delivery_id in _DELIVERED_EVENTS:
            _OUTBOX.pop(entry_id, None)
            changed = True
            continue
        next_attempt_at = float(entry.get("next_attempt_at") or 0.0)
        if next_attempt_at > now:
            continue
        message = _result_message(str(worker_id or "worker"), result)
        if message is None:
            _OUTBOX.pop(entry_id, None)
            changed = True
            continue
        # Record the attempt BEFORE the send so a delivered-then-raised
        # sender cannot get five copies through the retry loop.  The entry
        # leaves the outbox in the same state write.
        if delivery_id:
            _DELIVERED_EVENTS.add(delivery_id)
        _OUTBOX.pop(entry_id, None)
        changed = True
        dispatch.append((target, message[:4000], delivery_id))
    if changed:
        _persist_durable_state()
    for target, message, _delivery_id in dispatch:
        try:
            notify(target, message)
        except Exception:
            # The attempt is already durably recorded; retrying would risk
            # a duplicate delivery, and this dispatch loop is the only path
            # from the outbox to a receiver.
            continue
