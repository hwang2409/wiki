"""Incremental cost aggregation for headless agent runtime runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from ..pathwalk import open_relative_directory, open_relative_file, open_root_directory
from .archive_protocol import archive_is_committed
from .ticket import base_ticket

# USD per one million tokens. Sources:
# developers.openai.com/api/docs/models/gpt-5.5,
# developers.openai.com/api/docs/models/compare,
# docs.anthropic.com/en/docs/about-claude/pricing.
PRICING_USD_PER_MILLION: dict[str, dict[str, float]] = {
    "gpt-5.6-sol": {"input": 5.0, "cache_read": 0.5, "cache_write_5m": 6.25, "cache_write_1h": 6.25, "output": 30.0},
    "gpt-5.6-terra": {"input": 2.5, "cache_read": 0.25, "cache_write_5m": 3.125, "cache_write_1h": 3.125, "output": 15.0},
    "gpt-5.6-luna": {"input": 1.0, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 1.25, "output": 6.0},
    "gpt-5.5": {"input": 5.0, "cache_read": 0.5, "cache_write_5m": 6.25, "cache_write_1h": 6.25, "output": 30.0},
    "gpt-5.4": {"input": 2.5, "cache_read": 0.25, "cache_write_5m": 3.125, "cache_write_1h": 3.125, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "cache_read": 0.075, "cache_write_5m": 0.9375, "cache_write_1h": 0.9375, "output": 4.5},
    "claude-fable-5": {"input": 10.0, "cache_read": 1.0, "cache_write_5m": 12.5, "cache_write_1h": 20.0, "output": 50.0},
    "claude-opus-4-7": {"input": 5.0, "cache_read": 0.5, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "cache_read": 0.3, "cache_write_5m": 3.75, "cache_write_1h": 6.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0, "output": 5.0},
}

MODEL_ID_ALIASES = {
    "opus-4.7": "claude-opus-4-7",
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "sonnet-4.6": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
    "haiku-4.5": "claude-haiku-4-5",
}

STATE_VERSION = 4
CHUNK_BYTES = 64 * 1024
MAX_EVENT_LINE_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_DEDUPE_IDS = 4096
CURSOR_TAIL_BYTES = 256
REFRESH_INTERVAL_SECONDS = float(os.environ.get("WIKI_COST_REFRESH_INTERVAL_SECONDS", "5"))
_REFRESH_LOCK = threading.Lock()
_BACKGROUND_STATE: dict[str, Any] | None = None
_CHECKPOINT_INDEX_LOCK = threading.Lock()
_CHECKPOINT_INDEX: dict[Path, tuple[Path, ...]] = {}


ACCOUNTING_FIELDS = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")


@dataclass(frozen=True, slots=True)
class RunSource:
    """The source that exists for a run at one decision point."""

    kind: Literal["HOT", "ARCHIVED", "GONE"]
    path: Path | None = None
    committed: bool = False


class _CostState(dict[str, Any]):
    """In-memory state with a save-pending marker outside the JSON payload."""

    dirty: bool
    dirty_run_ids: set[str]
    deleted_run_ids: set[str]
    checkpoint_generation: int
    checkpoint_recovery_run_ids: set[str]


def _fsync_directory(path: Path) -> bool:
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(dir_fd)
    except OSError:
        return False
    finally:
        os.close(dir_fd)
    return True


def runtime_runs_dir() -> Path:
    runtime = Path(os.environ.get("WIKI_AGENT_RUNTIME_DIR") or Path.home() / ".wiki" / "agent-runtime")
    return Path(os.environ.get("WIKI_AGENT_RUNS_DIR") or runtime / "runs").expanduser()


def cost_state_path() -> Path:
    runtime = Path(os.environ.get("WIKI_AGENT_RUNTIME_DIR") or Path.home() / ".wiki" / "agent-runtime")
    return Path(os.environ.get("WIKI_COST_STATE_PATH") or runtime / "cost-aggregation.json").expanduser()


def cost_heartbeat_path() -> Path:
    return Path(f"{cost_state_path()}.heartbeat")


def _empty_state() -> _CostState:
    state = _CostState(
        {
            "version": STATE_VERSION,
            "updated_at": None,
            "runs_dir_signature": None,
            "active_runs": [],
            "runs": {},
            "records": {},
        }
    )
    state.dirty = False
    state.dirty_run_ids = set()
    state.deleted_run_ids = set()
    state.checkpoint_generation = 0
    state.checkpoint_recovery_run_ids = set()
    return state


def _load_state() -> dict[str, Any]:
    try:
        value = json.loads(cost_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if value.get("version") != STATE_VERSION or not isinstance(value.get("runs"), dict):
        return _empty_state()
    records = value.get("records")
    if not isinstance(records, dict):
        return _empty_state()
    value.setdefault("updated_at", None)
    active_runs = value.get("active_runs")
    value["active_runs"] = (
        [run_id for run_id in active_runs if isinstance(run_id, str)]
        if isinstance(active_runs, list)
        else []
    )
    value.pop("pending_runs", None)
    try:
        heartbeat = json.loads(cost_heartbeat_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        heartbeat = None
    if isinstance(heartbeat, dict) and isinstance(heartbeat.get("updated_at"), str):
        value["updated_at"] = heartbeat["updated_at"]
    state = _CostState(value)
    state.dirty = False
    state.dirty_run_ids = set()
    state.deleted_run_ids = set()
    state.checkpoint_generation = _number(value.get("checkpoint_generation"))
    raw_deleted_run_ids = value.get("deleted_run_ids")
    deleted_run_ids = {
        run_id
        for run_id in (raw_deleted_run_ids if isinstance(raw_deleted_run_ids, list) else [])
        if isinstance(run_id, str)
    }
    state.deleted_run_ids = deleted_run_ids
    state.checkpoint_recovery_run_ids = set()
    for checkpoint in _checkpoint_paths():
        try:
            checkpoint_value = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        run_id = checkpoint_value.get("run_id") if isinstance(checkpoint_value, dict) else None
        run_state = checkpoint_value.get("state") if isinstance(checkpoint_value, dict) else None
        generation = (
            _number(checkpoint_value.get("generation"))
            if isinstance(checkpoint_value, dict)
            else 0
        )
        if (
            isinstance(run_id, str)
            and run_id not in deleted_run_ids
            and isinstance(run_state, dict)
            and (generation == 0 or generation <= state.checkpoint_generation)
        ):
            value["runs"][run_id] = run_state
            if generation < state.checkpoint_generation:
                state.checkpoint_recovery_run_ids.add(run_id)
    return state


def _checkpoint_paths() -> tuple[Path, ...]:
    checkpoint_dir = cost_run_checkpoints_dir()
    with _CHECKPOINT_INDEX_LOCK:
        cached = _CHECKPOINT_INDEX.get(checkpoint_dir)
        if cached is not None:
            return cached
        try:
            paths = tuple(checkpoint_dir.glob("*.json"))
        except OSError:
            paths = ()
        _CHECKPOINT_INDEX[checkpoint_dir] = paths
        return paths


def _remember_checkpoint_paths(
    checkpoint_dir: Path, run_ids: list[str], deleted_run_ids: set[str]
) -> None:
    with _CHECKPOINT_INDEX_LOCK:
        paths = set(_CHECKPOINT_INDEX.get(checkpoint_dir, ()))
        paths.update(_run_checkpoint_path(run_id) for run_id in run_ids)
        paths.difference_update(
            _run_checkpoint_path(run_id) for run_id in deleted_run_ids
        )
        _CHECKPOINT_INDEX[checkpoint_dir] = tuple(sorted(paths, key=str))


def cost_run_checkpoints_dir() -> Path:
    path = cost_state_path()
    return path.parent / f"{path.name}.runs"


def _run_checkpoint_path(run_id: str) -> Path:
    return cost_run_checkpoints_dir() / f"{quote(run_id, safe='')}.json"


def _state_with_bounded_runs(state: dict[str, Any]) -> dict[str, Any]:
    """Keep the aggregate checkpoint small; run cursors live in per-run files."""

    payload = dict(state)
    payload.pop("dirty", None)
    payload.pop("dirty_run_ids", None)
    payload.pop("deleted_run_ids", None)
    payload["runs"] = {}
    payload["run_checkpoint_version"] = 1
    return payload


def _save_json(path: Path, value: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            if not _fsync_directory(path.parent):
                return False
            return True
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        return False


def _save_state(state: dict[str, Any]) -> bool:
    deleted_run_ids = getattr(state, "deleted_run_ids", set())
    generation = getattr(state, "checkpoint_generation", 0) + 1
    payload = _state_with_bounded_runs(state)
    payload["checkpoint_generation"] = generation
    payload["deleted_run_ids"] = sorted(deleted_run_ids)
    # Publish the aggregate first. If a crash happens before checkpoints,
    # loading accepts the older checkpoint and advances its cursor without
    # adding those already-published totals again.
    if not _save_json(cost_state_path(), payload):
        return False
    try:
        checkpoint_dir = cost_run_checkpoints_dir()
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_run_ids = sorted(
            run_id
            for run_id, run_state in state.get("runs", {}).items()
            if isinstance(run_id, str)
            and isinstance(run_state, dict)
            and isinstance(run_state.get("offset"), int)
        )
        for run_id in checkpoint_run_ids:
            checkpoint = _run_checkpoint_path(run_id)
            if not _save_json(
                checkpoint,
                {
                    "generation": generation,
                    "run_id": run_id,
                    "state": state["runs"].get(run_id, {}),
                },
            ):
                return False
    except OSError:
        return False
    deleted_checkpoints_removed = True
    for run_id in sorted(deleted_run_ids):
        try:
            _run_checkpoint_path(run_id).unlink(missing_ok=True)
        except OSError:
            deleted_checkpoints_removed = False
    if deleted_checkpoints_removed and deleted_run_ids:
        deleted_checkpoints_removed = _fsync_directory(checkpoint_dir)
    _remember_checkpoint_paths(
        checkpoint_dir, checkpoint_run_ids, set(deleted_run_ids)
    )
    if hasattr(state, "dirty_run_ids"):
        state.dirty_run_ids.clear()
    if deleted_checkpoints_removed and hasattr(state, "deleted_run_ids"):
        state.deleted_run_ids.clear()
    if hasattr(state, "checkpoint_generation"):
        state.checkpoint_generation = generation
    return True


def _save_heartbeat(state: dict[str, Any]) -> bool:
    return _save_json(cost_heartbeat_path(), {"updated_at": state.get("updated_at")})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_day(value: object) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.date().isoformat() if parsed else None


def _number(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _pick(value: dict[str, Any], *names: str) -> int:
    for name in names:
        if name in value:
            return _number(value[name])
    return 0


def _usage_values(value: object, *, input_includes_cache: bool) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    cache_read = _pick(
        value,
        "cacheReadInputTokens",
        "cachedInputTokens",
        "cache_read_input_tokens",
        "cached_input_tokens",
    )
    cache_write_legacy = _pick(
        value,
        "cacheWriteInputTokens",
        "cache_write_input_tokens",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
    )
    creation = value.get("cache_creation")
    cache_write_5m = _pick(
        value,
        "cacheWrite5mInputTokens",
        "cache_write_5m_input_tokens",
        "cache_creation_5m_input_tokens",
    )
    cache_write_1h = _pick(
        value,
        "cacheWrite1hInputTokens",
        "cache_write_1h_input_tokens",
        "cache_creation_1h_input_tokens",
    )
    if isinstance(creation, dict):
        cache_write_5m = cache_write_5m or _pick(creation, "ephemeral_5m_input_tokens", "5m_input_tokens")
        cache_write_1h = cache_write_1h or _pick(creation, "ephemeral_1h_input_tokens", "1h_input_tokens")
    if not cache_write_5m and not cache_write_1h:
        cache_write_5m = cache_write_legacy
    input_tokens = _pick(value, "inputTokens", "input_tokens")
    if input_includes_cache:
        input_tokens = max(0, input_tokens - cache_read - cache_write_5m - cache_write_1h)
    result = {
        "input": input_tokens,
        "cache_read": cache_read,
        "cache_write_5m": cache_write_5m,
        "cache_write_1h": cache_write_1h,
        "output": _pick(value, "outputTokens", "output_tokens"),
    }
    return result if any(result.values()) else None


def _codex_usage(payload: dict[str, Any], run_state: dict[str, Any]) -> dict[str, int] | None:
    if payload.get("method") != "thread/tokenUsage/updated":
        return None
    params = payload.get("params")
    usage = params.get("tokenUsage") if isinstance(params, dict) else None
    total = usage.get("total") if isinstance(usage, dict) else None
    current = _usage_values(total, input_includes_cache=True)
    if current is None:
        return None
    previous = run_state.get("cumulative")
    run_state["cumulative"] = current
    if not isinstance(previous, dict):
        return current
    if any(current[key] < _number(previous.get(key)) for key in ACCOUNTING_FIELDS):
        return {key: 0 for key in ACCOUNTING_FIELDS}
    return {key: current[key] - _number(previous.get(key)) for key in ACCOUNTING_FIELDS}


def _claude_usage(payload: dict[str, Any], run_state: dict[str, Any]) -> dict[str, int] | None:
    if payload.get("type") != "assistant":
        return None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        seen = run_state.setdefault("seen_message_ids", {})
        if isinstance(seen, list):
            seen = {item: True for item in seen if isinstance(item, str)}
            run_state["seen_message_ids"] = seen
        if message_id in seen:
            return None
        seen[message_id] = True
        while len(seen) > MAX_MESSAGE_DEDUPE_IDS:
            seen.pop(next(iter(seen)))
    return _usage_values(message.get("usage"), input_includes_cache=False)


def _open_root() -> int | None:
    root = runtime_runs_dir().absolute().resolve(strict=False)
    try:
        fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return fd
    except OSError:
        return None


def _read_json_fd(fd: int) -> dict[str, Any]:
    try:
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _iter_new_json_lines(
    fd: int, offset: int, on_chunk: Callable[[], None] | None = None
) -> tuple[Iterator[tuple[int, dict[str, Any]]], list[int]]:
    progress = [offset]

    def read() -> Iterator[tuple[int, dict[str, Any]]]:
        try:
            with os.fdopen(os.dup(fd), "rb") as handle:
                handle.seek(offset)
                carry = b""
                base = offset
                while chunk := handle.read(CHUNK_BYTES):
                    if on_chunk is not None:
                        on_chunk()
                    data = carry + chunk
                    start = 0
                    while True:
                        end = data.find(b"\n", start)
                        if end < 0:
                            break
                        line = data[start:end]
                        line_start = base + start
                        progress[0] = base + end + 1
                        start = end + 1
                        if not line or len(line) > MAX_EVENT_LINE_BYTES:
                            continue
                        try:
                            row = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(row, dict):
                            yield line_start, row
                    carry = data[start:]
                    base += start
                    if len(carry) > MAX_EVENT_LINE_BYTES:
                        base += len(carry)
                        carry = b""
                        progress[0] = base
                progress[0] = base
        except OSError:
            return

    return read(), progress


def _cursor_tail_fingerprint(fd: int, offset: int) -> str:
    if offset <= 0:
        return hashlib.sha256(b"").hexdigest()
    try:
        with os.fdopen(os.dup(fd), "rb") as handle:
            start = max(0, offset - CURSOR_TAIL_BYTES)
            handle.seek(start)
            return hashlib.sha256(handle.read(offset - start)).hexdigest()
    except OSError:
        return ""


def _record_key(day: str, metadata: dict[str, Any], model: str) -> str:
    worker = str(metadata.get("agent_id") or "unknown")
    ticket = base_ticket(worker)
    orchestrator = str(metadata.get("orchestrator_id") or "")
    return "\x1f".join((day, worker, ticket, orchestrator, model))


def _new_record(key: str, metadata: dict[str, Any], model: str, day: str) -> dict[str, Any]:
    worker = str(metadata.get("agent_id") or "unknown")
    return {
        "key": key,
        "day": day,
        "worker": worker,
        "ticket": base_ticket(worker),
        "orchestrator": str(metadata.get("orchestrator_id") or ""),
        "model": model,
        **{field: 0 for field in ACCOUNTING_FIELDS},
    }


def _add_contribution(
    state: dict[str, Any],
    run_state: dict[str, Any],
    key: str,
    metadata: dict[str, Any],
    model: str,
    day: str,
    usage: dict[str, int],
) -> None:
    records = state["records"]
    record = records.setdefault(key, _new_record(key, metadata, model, day))
    local = run_state.setdefault("records", {}).setdefault(key, {field: 0 for field in ACCOUNTING_FIELDS})
    for field in ACCOUNTING_FIELDS:
        amount = int(usage[field])
        record[field] += amount
        local[field] += amount


def _remove_run_contributions(state: dict[str, Any], run_state: dict[str, Any]) -> None:
    for key, local in (run_state.get("records") or {}).items():
        record = state["records"].get(key)
        if not isinstance(record, dict):
            continue
        for field in ACCOUNTING_FIELDS:
            record[field] -= _number(local.get(field))
        if not any(record[field] for field in ACCOUNTING_FIELDS):
            state["records"].pop(key, None)


def _discard_run(state: dict[str, Any], run_id: str) -> bool:
    # Resolve again immediately before subtracting. A source found by an
    # earlier scan is not evidence that the run is gone now.
    if resolve_run_source(run_id).kind != "GONE":
        return False
    old_run = state["runs"].pop(run_id, None)
    if isinstance(old_run, dict):
        _remove_run_contributions(state, old_run)
        deleted_run_ids = getattr(state, "deleted_run_ids", None)
        if isinstance(deleted_run_ids, set):
            deleted_run_ids.add(run_id)
    active_runs = state.get("active_runs")
    if isinstance(active_runs, list):
        state["active_runs"] = [item for item in active_runs if item != run_id]
    return True


def _set_run_active(state: dict[str, Any], run_id: str, active: bool) -> None:
    active_runs = state.setdefault("active_runs", [])
    if not isinstance(active_runs, list):
        active_runs = []
        state["active_runs"] = active_runs
    if active and run_id not in active_runs:
        active_runs.append(run_id)
    elif not active and run_id in active_runs:
        active_runs.remove(run_id)


def _stat_signature(value: os.stat_result) -> list[int]:
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def _run_signature(root_fd: int, run_id: str) -> dict[str, list[int] | None] | None:
    """Return cheap file signatures without reading a run's event stream."""

    try:
        run_fd = open_relative_directory(root_fd, (run_id,))
    except OSError:
        return None
    raw_fd: int | None = None
    metadata_fd: int | None = None
    try:
        if not stat.S_ISDIR(os.fstat(run_fd).st_mode):
            return None
        try:
            raw_fd = open_relative_file(run_fd, ("raw.jsonl",), extra_final_flags=getattr(os, "O_NONBLOCK", 0))
            raw_stat = os.fstat(raw_fd)
            if not stat.S_ISREG(raw_stat.st_mode):
                return None
        except OSError:
            return None
        try:
            metadata_fd = open_relative_file(run_fd, ("run.json",), extra_final_flags=getattr(os, "O_NONBLOCK", 0))
            metadata_stat = os.fstat(metadata_fd)
            metadata_signature = _stat_signature(metadata_stat) if stat.S_ISREG(metadata_stat.st_mode) else None
        except FileNotFoundError:
            metadata_signature = None
        except OSError:
            return None
        return {"raw": _stat_signature(raw_stat), "metadata": metadata_signature}
    finally:
        if metadata_fd is not None:
            os.close(metadata_fd)
        if raw_fd is not None:
            os.close(raw_fd)
        os.close(run_fd)


def _run_is_terminal(root_fd: int, run_id: str) -> bool:
    """Read only run metadata before deciding whether to scan its JSONL."""

    run_fd: int | None = None
    metadata_fd: int | None = None
    try:
        run_fd = open_relative_directory(root_fd, (run_id,))
    except OSError:
        return False
    try:
        metadata_fd = open_relative_file(
            run_fd,
            ("run.json",),
            extra_final_flags=getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        os.close(run_fd)
        return False
    try:
        metadata = _read_json_fd(metadata_fd)
    finally:
        if metadata_fd is not None:
            os.close(metadata_fd)
        if run_fd is not None:
            os.close(run_fd)
    return metadata.get("state") in {"dead", "completed"} or bool(
        metadata.get("ended_at")
    )


def _archive_root() -> Path:
    return Path(
        os.environ.get("WIKI_AGENT_ARCHIVE_DIR")
        or Path.home() / "me" / "fun" / "agent-archive"
    ).expanduser()


def _hot_source_is_usable(root_fd: int, run_id: str) -> bool:
    run_fd: int | None = None
    raw_fd: int | None = None
    metadata_fd: int | None = None
    try:
        run_fd = open_relative_directory(root_fd, (run_id,))
        if not stat.S_ISDIR(os.fstat(run_fd).st_mode):
            return False
        raw_fd = open_relative_file(
            run_fd,
            ("raw.jsonl",),
            extra_final_flags=getattr(os, "O_NONBLOCK", 0),
        )
        if not stat.S_ISREG(os.fstat(raw_fd).st_mode):
            return False
        try:
            metadata_fd = open_relative_file(
                run_fd,
                ("run.json",),
                extra_final_flags=getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return True
        return stat.S_ISREG(os.fstat(metadata_fd).st_mode)
    except OSError:
        return False
    finally:
        if metadata_fd is not None:
            os.close(metadata_fd)
        if raw_fd is not None:
            os.close(raw_fd)
        if run_fd is not None:
            os.close(run_fd)


def resolve_run_source(run_id: str) -> RunSource:
    """Resolve a run's current source without using a cached archive view."""

    root_fd = _open_root()
    if root_fd is not None:
        try:
            if _hot_source_is_usable(root_fd, run_id):
                return RunSource(
                    "HOT", runtime_runs_dir().absolute() / run_id
                )
        finally:
            os.close(root_fd)

    archive_root = _archive_root()
    try:
        session_dirs = tuple(archive_root.glob("*/*"))
    except OSError:
        session_dirs = ()
    for session_dir in session_dirs:
        if not archive_is_committed(session_dir):
            continue
        try:
            value = json.loads((session_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if isinstance(value, dict) and value.get("run_id") == run_id:
            return RunSource("ARCHIVED", session_dir, committed=True)
    return RunSource("GONE")


@dataclass(slots=True)
class _OpenedRunSource:
    raw_fd: int
    raw_stat: os.stat_result
    metadata: dict[str, Any]
    metadata_signature: list[int] | None

    def close(self) -> None:
        os.close(self.raw_fd)


def _open_run_source_files(
    run_fd: int,
    *,
    require_metadata: bool,
    require_events: bool,
) -> _OpenedRunSource | None:
    raw_fd: int | None = None
    metadata_fd: int | None = None
    events_fd: int | None = None
    keep_raw_fd = False
    try:
        raw_fd = open_relative_file(
            run_fd,
            ("raw.jsonl",),
            extra_final_flags=getattr(os, "O_NONBLOCK", 0),
        )
        raw_stat = os.fstat(raw_fd)
        if not stat.S_ISREG(raw_stat.st_mode):
            return None
        if require_events:
            events_fd = open_relative_file(
                run_fd,
                ("events.jsonl",),
                extra_final_flags=getattr(os, "O_NONBLOCK", 0),
            )
            if not stat.S_ISREG(os.fstat(events_fd).st_mode):
                return None
        try:
            metadata_fd = open_relative_file(
                run_fd,
                ("run.json",),
                extra_final_flags=getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            if require_metadata:
                return None
            metadata = {}
            metadata_signature = None
        except OSError:
            return None
        else:
            metadata_stat = os.fstat(metadata_fd)
            if not stat.S_ISREG(metadata_stat.st_mode):
                return None
            metadata_signature = _stat_signature(metadata_stat)
            metadata = _read_json_fd(metadata_fd)
        keep_raw_fd = True
        return _OpenedRunSource(raw_fd, raw_stat, metadata, metadata_signature)
    except OSError:
        return None
    finally:
        if metadata_fd is not None:
            os.close(metadata_fd)
        if events_fd is not None:
            os.close(events_fd)
        os.close(run_fd)
        if raw_fd is not None and not keep_raw_fd:
            os.close(raw_fd)


def _open_hot_run_source(root_fd: int, run_id: str) -> _OpenedRunSource | None:
    try:
        run_fd = open_relative_directory(root_fd, (run_id,))
    except OSError:
        return None
    return _open_run_source_files(
        run_fd,
        require_metadata=False,
        require_events=False,
    )


def _open_archived_run_source(source: RunSource) -> _OpenedRunSource | None:
    if source.path is None or not source.committed:
        return None
    archive_root = _archive_root().absolute()
    session_dir = source.path.absolute()
    try:
        relative = session_dir.relative_to(archive_root)
        archive_fd = open_root_directory(archive_root)
        try:
            run_fd = open_relative_directory(archive_fd, relative.parts)
        finally:
            os.close(archive_fd)
    except OSError:
        return None
    return _open_run_source_files(
        run_fd,
        require_metadata=True,
        require_events=True,
    )


def _open_resolved_run_source(
    source: RunSource, run_id: str, root_fd: int
) -> _OpenedRunSource | None:
    if source.kind == "HOT":
        return _open_hot_run_source(root_fd, run_id)
    if source.kind == "ARCHIVED":
        return _open_archived_run_source(source)
    return None


def _same_run_source(left: RunSource, right: RunSource) -> bool:
    return (
        left.kind == right.kind
        and left.path == right.path
        and left.committed == right.committed
    )


def _scan_run(
    state: dict[str, Any],
    run_id: str,
    root_fd: int,
    *,
    account: bool = True,
) -> bool:
    opened: _OpenedRunSource | None = None
    for _attempt in range(3):
        source = resolve_run_source(run_id)
        if source.kind == "GONE":
            if _discard_run(state, run_id):
                return True
            continue
        opened = _open_resolved_run_source(source, run_id, root_fd)
        if opened is None:
            continue
        current_source = resolve_run_source(run_id)
        if _same_run_source(source, current_source):
            break
        opened.close()
        opened = None
    if opened is None:
        return False

    raw_fd = opened.raw_fd
    raw_stat = opened.raw_stat
    metadata = opened.metadata
    metadata_signature = opened.metadata_signature

    metadata.setdefault("agent_id", run_id)
    run_state = state["runs"].get(run_id)
    if not isinstance(run_state, dict):
        run_state = {"offset": 0, "cumulative": None, "seen_message_ids": {}, "records": {}}
        state["runs"][run_id] = run_state
    offset = _number(run_state.get("offset"))
    tail_fingerprint = _cursor_tail_fingerprint(raw_fd, offset)
    if (
        raw_stat.st_size < offset
        or raw_stat.st_ino != _number(run_state.get("inode"))
        or (offset and run_state.get("cursor_tail_fingerprint") != tail_fingerprint)
    ):
        if account:
            _remove_run_contributions(state, run_state)
        run_state = {"offset": 0, "cumulative": None, "seen_message_ids": {}, "records": {}}
        state["runs"][run_id] = run_state
        offset = 0
    read_chunks = [0]
    iterator, progress = _iter_new_json_lines(raw_fd, offset, lambda: read_chunks.__setitem__(0, read_chunks[0] + 1))
    provider = str(metadata.get("provider") or "")
    model = str(metadata.get("model") or "unknown")
    for _line_start, envelope in iterator:
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            continue
        usage = _codex_usage(payload, run_state) if provider in {"codex", "cdx"} else _claude_usage(payload, run_state)
        if usage is None:
            continue
        timestamp = envelope.get("received_at") or envelope.get("timestamp")
        event_dt = _parse_timestamp(timestamp)
        if event_dt is not None:
            iso = event_dt.isoformat().replace("+00:00", "Z")
            run_state.setdefault("first_event_at", iso)
            run_state["last_event_at"] = iso
            run_state["event_tokens"] = _number(run_state.get("event_tokens")) + sum(usage.values())
        day = _event_day(timestamp)
        if day is not None and any(usage.values()):
            key = _record_key(day, metadata, model)
            if account:
                _add_contribution(state, run_state, key, metadata, model, day, usage)
            else:
                local = run_state.setdefault("records", {}).setdefault(
                    key, {field: 0 for field in ACCOUNTING_FIELDS}
                )
                for field in ACCOUNTING_FIELDS:
                    local[field] += int(usage[field])
    final_stat = os.fstat(raw_fd)
    run_state.update(
        {
            "offset": progress[0],
            "size": final_stat.st_size,
            "inode": final_stat.st_ino,
            "cursor_tail_fingerprint": _cursor_tail_fingerprint(raw_fd, progress[0]),
            "read_chunks": read_chunks[0],
            "prompt_chars": len(str(metadata.get("initial_prompt") or "")),
            "prompt_tokens_estimate": max(0, len(str(metadata.get("initial_prompt") or "")) // 4),
            "model": model,
            "worker": str(metadata.get("agent_id") or run_id),
            "ticket": base_ticket(str(metadata.get("agent_id") or run_id)),
            "orchestrator": str(metadata.get("orchestrator_id") or ""),
            "created_at": metadata.get("created_at"),
            "active": metadata.get("state") not in {"dead", "completed"} and not metadata.get("ended_at"),
            "scan_signature": {
                "raw": _stat_signature(final_stat),
                "metadata": metadata_signature,
            },
        }
    )
    _set_run_active(state, run_id, bool(run_state["active"]))
    dirty_run_ids = getattr(state, "dirty_run_ids", None)
    if isinstance(dirty_run_ids, set):
        dirty_run_ids.add(run_id)
    opened.close()
    return True


def refresh(state: dict[str, Any] | None = None) -> dict[str, Any]:
    if state is None:
        state = _load_state()
    elif not isinstance(state, _CostState):
        cached_state = _CostState(state)
        cached_state.dirty = False
        cached_state.dirty_run_ids = set()
        cached_state.deleted_run_ids = set()
        cached_state.checkpoint_recovery_run_ids = set()
        cached_state.checkpoint_generation = _number(
            state.get("checkpoint_generation")
        )
        state = cached_state
    root_fd = _open_root()
    seen_runs: set[str] = set()
    runs_dir_signature: list[int] | None = None
    state_changed = False
    if root_fd is not None:
        try:
            runs_dir_signature = _stat_signature(os.fstat(root_fd))
            root_changed = state.get("runs_dir_signature") != runs_dir_signature
            recovery_run_ids = getattr(state, "checkpoint_recovery_run_ids", set())
            for run_id in list(recovery_run_ids):
                if run_id not in state.get("runs", {}):
                    recovery_run_ids.discard(run_id)
                    continue
                if _scan_run(state, run_id, root_fd, account=False):
                    recovery_run_ids.discard(run_id)
                    state_changed = True
            if root_changed:
                # os.scandir(fd) dups the fd internally and closes only its own
                # dup — an explicit os.dup() here is owned by nobody and leaks
                # one runs-dir fd per refresh (wedged the backend at the GUI
                # 256-fd rlimit; wedge #7, 2026-07-30).
                with os.scandir(root_fd) as entries:
                    run_ids = [
                        entry.name
                        for entry in entries
                        if (
                            not entry.name.startswith(".")
                            and not entry.is_symlink()
                            and entry.is_dir(follow_symlinks=False)
                        )
                    ]
                for run_id in run_ids:
                    seen_runs.add(run_id)
                    if _run_is_terminal(root_fd, run_id):
                        continue
                    _scan_run(
                        state,
                        run_id,
                        root_fd,
                    )
                state_changed = True
            else:
                # A run directory's mtime does not change when raw.jsonl grows.
                # Check only active runs for cheap signatures, then parse new
                # bytes only when a signature changed. Completed history stays
                # out of the five-second refresh path.
                for run_id in list(state.get("active_runs", [])):
                    run_state = state["runs"].get(run_id)
                    if not isinstance(run_state, dict) or not run_state.get("active", True):
                        continue
                    if _run_signature(root_fd, run_id) != run_state.get("scan_signature"):
                        _scan_run(
                            state,
                            run_id,
                            root_fd,
                        )
                        state_changed = True
        finally:
            os.close(root_fd)
    else:
        root_changed = True
        state_changed = True
    if root_changed:
        for run_id in list(state["runs"]):
            if run_id not in seen_runs:
                _discard_run(state, run_id)
        state["active_runs"] = [
            run_id
            for run_id, run_state in state["runs"].items()
            if isinstance(run_state, dict) and run_state.get("active", True)
        ]
    state["runs_dir_signature"] = runs_dir_signature
    state["updated_at"] = _now_iso()
    state.dirty = bool(state.dirty or state_changed)
    state_saved = _save_state(state) if state.dirty else True
    if state_saved:
        state.dirty = False
    if state_saved:
        _save_heartbeat(state)
    return state


def invalidate_background_state() -> None:
    """Force the next background refresh to reload its durable state."""

    global _BACKGROUND_STATE
    _BACKGROUND_STATE = None


async def refresh_in_background() -> bool:
    global _BACKGROUND_STATE
    if not _REFRESH_LOCK.acquire(blocking=False):
        return False
    try:
        if _BACKGROUND_STATE is None:
            _BACKGROUND_STATE = _load_state()
        _BACKGROUND_STATE = await asyncio.to_thread(refresh, _BACKGROUND_STATE)
    finally:
        _REFRESH_LOCK.release()
    return True


async def background_loop() -> None:
    while True:
        await refresh_in_background()
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


def _state_stale(state: dict[str, Any]) -> bool:
    if not isinstance(state.get("updated_at"), str):
        return True
    parsed = _parse_timestamp(state["updated_at"])
    return parsed is None or (datetime.now(timezone.utc) - parsed).total_seconds() > REFRESH_INTERVAL_SECONDS * 2


def _money_and_pricing(model: str, usage: dict[str, Any]) -> tuple[float | None, str, int]:
    rates = PRICING_USD_PER_MILLION.get(MODEL_ID_ALIASES.get(model, model))
    tokens = sum(_number(usage.get(field)) for field in ACCOUNTING_FIELDS)
    if rates is None:
        return None, "unpriced", tokens
    cost = sum(_number(usage.get(field)) * rates[field] / 1_000_000 for field in ACCOUNTING_FIELDS)
    return cost, "priced", 0


def _rollup(items: list[dict[str, Any]], label_key: str, label: str | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        name = label if label is not None else str(item.get(label_key) or "(none)")
        row = grouped.setdefault(
            name,
            {"label": name, **{field: 0 for field in ACCOUNTING_FIELDS}, "cost_usd": 0.0, "unpriced_tokens": 0, "models": set(), "pricing_flags": set()},
        )
        for field in ACCOUNTING_FIELDS:
            row[field] += _number(item.get(field))
        row["models"].add(item.get("model") or "unknown")
        cost, pricing, unpriced = _money_and_pricing(str(item.get("model") or "unknown"), item)
        if cost is not None:
            row["cost_usd"] += cost
        row["unpriced_tokens"] += unpriced
        row["pricing_flags"].add(pricing)
    output = []
    for row in grouped.values():
        has_priced = "priced" in row["pricing_flags"]
        has_unpriced = row["unpriced_tokens"] > 0
        row["pricing"] = "mixed" if has_priced and has_unpriced else "unpriced" if has_unpriced else "priced"
        row["cost_usd"] = round(row["cost_usd"], 8) if has_priced else None
        row["cache_write"] = row.pop("cache_write_5m") + row.pop("cache_write_1h")
        row["cached"] = row["cache_read"] + row["cache_write"]
        row["total_tokens"] = row["input"] + row["cached"] + row["output"]
        row["models"] = sorted(row["models"])
        row.pop("pricing_flags")
        output.append(row)
    output.sort(key=lambda item: (item["cost_usd"] is not None, item["cost_usd"] or 0, item["total_tokens"]), reverse=True)
    return output


def _range_matches_day(day: str, from_dt: datetime | None, to_dt: datetime | None) -> bool:
    parsed = _parse_timestamp(f"{day}T00:00:00Z")
    return parsed is not None and not (from_dt and parsed.date() < from_dt.date()) and not (to_dt and parsed.date() >= to_dt.date())


def _matching_runs(state: dict[str, Any], records: list[dict[str, Any]], ticket: str | None) -> list[dict[str, Any]]:
    keys = {str(record.get("key") or "") for record in records}
    wanted = base_ticket(ticket) if ticket else None
    return [
        run
        for run in state["runs"].values()
        if isinstance(run, dict)
        and keys.intersection((run.get("records") or {}).keys())
        and (wanted is None or base_ticket(str(run.get("worker"))) == wanted)
    ]


def _prompt_distribution(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bins = (("0-1k", 0, 1_000), ("1k-4k", 1_000, 4_000), ("4k-16k", 4_000, 16_000), ("16k+", 16_000, None))
    return [
        {"bucket": label, "runs": sum(1 for run in runs if _number(run.get("prompt_tokens_estimate")) >= lower and (upper is None or _number(run.get("prompt_tokens_estimate")) < upper))}
        for label, lower, upper in bins
    ]


def _query_state(state: dict[str, Any], from_ts: str | None = None, to_ts: str | None = None, ticket: str | None = None) -> dict[str, Any]:
    from_dt = _parse_timestamp(from_ts) if from_ts else None
    to_dt = _parse_timestamp(to_ts) if to_ts else None
    records = [
        record
        for record in state["records"].values()
        if (not ticket or base_ticket(str(record.get("ticket") or "")) == base_ticket(ticket))
        and _range_matches_day(str(record.get("day") or ""), from_dt, to_dt)
    ]
    totals = _rollup(records, "ticket", "all")
    all_total = totals[0] if totals else {"label": "all", "input": 0, "cache_read": 0, "cache_write": 0, "cached": 0, "output": 0, "total_tokens": 0, "cost_usd": 0.0, "unpriced_tokens": 0, "pricing": "priced", "models": []}
    runs = _matching_runs(state, records, ticket)
    if from_dt or to_dt:
        selected_days = [_parse_timestamp(f"{record['day']}T00:00:00Z") for record in records]
        selected_days = [value for value in selected_days if value]
        starts = selected_days
        ends = [value.replace(hour=23, minute=59, second=59) for value in selected_days]
    else:
        starts = [_parse_timestamp(run.get("first_event_at")) for run in runs]
        ends = [_parse_timestamp(run.get("last_event_at")) for run in runs]
    starts = [value for value in starts if value]
    ends = [value for value in ends if value]
    span = max(60, int((max(ends) - min(starts)).total_seconds()) if starts and ends else 60)
    velocity = {"tokens_per_minute": round(all_total["total_tokens"] / (span / 60), 2), "window_seconds": span, "tokens": all_total["total_tokens"]}
    return {
        "updated_at": state.get("updated_at"),
        "totals": all_total,
        "top": {"worker": _rollup(records, "worker"), "ticket": _rollup(records, "ticket"), "orchestrator": _rollup(records, "orchestrator"), "day": _rollup(records, "day")},
        "prompt_size_distribution": _prompt_distribution(runs),
        "velocity": velocity,
        "runs_scanned": len(runs),
        "refreshing": False,
    }


def query(from_ts: str | None = None, to_ts: str | None = None, ticket: str | None = None) -> dict[str, Any]:
    state = _load_state()
    response = _query_state(state, from_ts, to_ts, ticket)
    response["refreshing"] = _state_stale(state)
    return response
