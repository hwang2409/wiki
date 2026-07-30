"""Durable job/outbox state for the rebase worker.

State lives in three JSON files under ``AGENT_RUNTIME_DIR/rebase-bot/``:
``jobs.json`` (per-job records), ``outbox.json`` (pending notifications),
``delivered.json`` (deduplication of already-sent events).  Every terminal
transition persists both the job record and its outbox entry in a single
state write so a crash between the two cannot lose a result notification.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rebase_parsing import RebaseError


NotificationSender = Callable[[str, str, str], None]
"""``notify(target, message, delivery_id)``.

The stable ``delivery_id`` must reach ``MessageIn.dedupe_key`` on the
receiving side so that bounded sender retries do not stack duplicate
notifications in the orchestrator inbox.
"""

_DURABLE_STATE_LOADED = False
_DURABLE_STATE_ROOT: Path | None = None
_DURABLE_JOBS: dict[str, dict[str, Any]] = {}
_OUTBOX: dict[str, dict[str, Any]] = {}
_DELIVERED_EVENTS: set[str] = set()
_JOB_RETENTION_SECONDS = 900
_OUTBOX_MAX_ATTEMPTS = 5
_OUTBOX_BACKOFF_BASE_SECONDS = 2.0
_OUTBOX_BACKOFF_CAP_SECONDS = 300.0
_TEMP_COUNTER = 0
_TEMP_COUNTER_LOCK = threading.Lock()


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
    """Replace the in-memory collections with the disk snapshot.

    In-memory dicts are a pure CACHE of ``state.json`` — never a merge
    target.  A merge would let a stale writer resurrect an outbox entry
    another process had already delivered and dropped (the reviewer's
    "sixth send after the max-attempts bound" case).  This function
    unconditionally CLEARS the three collections before repopulating
    them from disk so a key that vanished on disk vanishes in memory
    too.
    """

    global _DURABLE_STATE_LOADED, _DURABLE_STATE_ROOT
    state_path = _durable_state_path()
    state_root = state_path.parent
    if _DURABLE_STATE_LOADED and _DURABLE_STATE_ROOT == state_root:
        return
    _DURABLE_STATE_ROOT = state_root
    _DURABLE_STATE_LOADED = True
    # Replace, never merge — memory is a cache of disk.
    _DURABLE_JOBS.clear()
    _OUTBOX.clear()
    _DELIVERED_EVENTS.clear()
    try:
        snapshot = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(snapshot, Mapping):
        return
    jobs_section = snapshot.get("jobs")
    if isinstance(jobs_section, Mapping):
        for key, item in jobs_section.items():
            if isinstance(key, str) and isinstance(item, Mapping):
                _DURABLE_JOBS[str(key)] = dict(item)
    outbox_section = snapshot.get("outbox")
    if isinstance(outbox_section, Mapping):
        for key, item in outbox_section.items():
            if isinstance(key, str) and isinstance(item, Mapping):
                _OUTBOX[str(key)] = dict(item)
    delivered_section = snapshot.get("delivered")
    if isinstance(delivered_section, list):
        for entry in delivered_section:
            if isinstance(entry, str):
                _DELIVERED_EVENTS.add(entry)


def _persist_durable_state() -> None:
    global _TEMP_COUNTER
    state_path = _durable_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "jobs": _DURABLE_JOBS,
        "outbox": _OUTBOX,
        "delivered": sorted(_DELIVERED_EVENTS),
    }
    with _TEMP_COUNTER_LOCK:
        _TEMP_COUNTER += 1
        seq = _TEMP_COUNTER
    # Unique temp filename per writer: pid + monotonic counter + a random
    # nonce keeps two concurrent processes from clobbering each other's
    # temp file before ``os.replace`` promotes it to ``state.json``.
    temporary = state_path.with_name(
        f"{state_path.name}.{os.getpid()}.{seq}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(state_path)


@contextmanager
def _state_lock():
    """Serialize the reload-mutate-publish cycle across processes.

    Every mutating helper (``_persist_job``, ``_persist_completion``,
    ``_enqueue_result``, ``_flush_outbox``) wraps its work in this
    manager.  We take an exclusive ``fcntl.flock`` on a sibling lock
    file, then FORCE a REPLACE-not-merge reload from disk so mutations
    start from the latest snapshot.  We FAIL CLOSED (``RebaseError``)
    if the OS refuses the lock — a quiet degrade to unlocked writes is
    exactly the hole the round-11 code left, and it lets a concurrent
    writer silently overwrite committed state.
    """

    global _DURABLE_STATE_LOADED
    state_path = _durable_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + ".lock")
    handle = lock_path.open("a+")
    try:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            handle.close()
            raise RebaseError(
                f"could not acquire durable-state lock {lock_path}: {exc}"
            ) from exc
    except ImportError as exc:
        handle.close()
        raise RebaseError(
            f"fcntl not available; refusing to publish durable state without a lock: {exc}"
        ) from exc
    try:
        # Under the lock, ALWAYS refresh from disk — memory is a cache.
        _DURABLE_STATE_LOADED = False
        _load_durable_state()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


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
    with _state_lock():
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
    with _state_lock():
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
    with _state_lock():
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
    """Drain the outbox with bounded retries keyed by ``delivery_id``.

    ``notify`` is called with ``(target, message, delivery_id)``.  The
    delivery id is a stable ``(pr, sha, status, head_sha)`` string that
    receiver code (``main._rebase_bot_notification_sender``) forwards as
    ``MessageIn.dedupe_key`` — so a retry after a transient failure looks
    like the same logical event to the receiver, and the inbox drops the
    duplicate.  Retries are bounded by ``_OUTBOX_MAX_ATTEMPTS`` with
    exponential backoff to keep a persistently broken sender from
    holding the outbox forever.
    """

    if notify is None:
        return
    now = _now()
    # First pass: pick eligible entries and snapshot them under the state
    # lock.  Actual ``notify`` calls happen OUTSIDE the lock so a slow or
    # blocking sender does not stall other durable-state writers.
    with _state_lock():
        dispatch: list[tuple[str, str, str, str]] = []
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
            dispatch.append((entry_id, target, message[:4000], delivery_id))
        if changed:
            _persist_durable_state()

    for entry_id, target, message, delivery_id in dispatch:
        error: str | None = None
        try:
            notify(target, message, delivery_id)
        except Exception as exc:
            error = str(exc)[:600]

        with _state_lock():
            entry = _OUTBOX.get(entry_id)
            if entry is None:
                continue
            if error is None:
                if delivery_id:
                    _DELIVERED_EVENTS.add(delivery_id)
                _OUTBOX.pop(entry_id, None)
                _persist_durable_state()
                continue
            attempts = int(entry.get("attempts") or 0) + 1
            entry["attempts"] = attempts
            entry["last_error"] = error
            if attempts >= _OUTBOX_MAX_ATTEMPTS:
                _OUTBOX.pop(entry_id, None)
            else:
                delay = min(
                    _OUTBOX_BACKOFF_CAP_SECONDS,
                    _OUTBOX_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)),
                )
                entry["next_attempt_at"] = _now() + delay
            _persist_durable_state()
