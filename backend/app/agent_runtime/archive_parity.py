"""Archive export parity and bounded SQLite backfill helpers."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import transcripts
from .event_store import NORMALIZER_VERSION, SQLiteEventStore, replay_raw_jsonl
from .store import RunStore


_IGNORED_PARITY_KEYS = frozenset(
    {"normalized_at", "path", "file_path", "transcript_path"}
)


@dataclass(frozen=True)
class ParityMismatch:
    run_id: str
    normalizer_version: str
    path: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class ParityReport:
    run_id: str
    normalizer_version: str
    matched: bool
    mismatches: tuple[ParityMismatch, ...]


@dataclass(frozen=True)
class BackfillResult:
    run_id: str
    status: str
    normalizer_version: str
    reason: str | None = None
    mismatches: tuple[ParityMismatch, ...] = ()


def _canonical(value: Any, *, key: str | None = None) -> Any:
    if key in _IGNORED_PARITY_KEYS:
        return None
    if isinstance(value, dict):
        return {
            name: _canonical(child, key=name)
            for name, child in value.items()
            if name not in _IGNORED_PARITY_KEYS
        }
    if isinstance(value, list):
        return [_canonical(child) for child in value]
    return value


def _diff_paths(expected: Any, actual: Any, path: str) -> list[tuple[str, Any, Any]]:
    if type(expected) is not type(actual):
        return [(path, expected, actual)]
    if isinstance(expected, dict):
        differences: list[tuple[str, Any, Any]] = []
        for key in sorted(set(expected) | set(actual)):
            child_path = f"{path}.{key}" if path else key
            if key not in expected or key not in actual:
                differences.append((child_path, expected.get(key), actual.get(key)))
            else:
                differences.extend(_diff_paths(expected[key], actual[key], child_path))
        return differences
    if isinstance(expected, list):
        differences = []
        for index in range(max(len(expected), len(actual))):
            child_path = f"{path}[{index}]"
            if index >= len(expected) or index >= len(actual):
                differences.append(
                    (
                        child_path,
                        expected[index] if index < len(expected) else None,
                        actual[index] if index < len(actual) else None,
                    )
                )
            else:
                differences.extend(_diff_paths(expected[index], actual[index], child_path))
        return differences
    if expected != actual:
        return [(path, expected, actual)]
    return []


def _legacy_payload(store: RunStore, run_id: str) -> dict[str, Any]:
    record = store.get(run_id)
    transcripts._cache.clear()
    result = transcripts.read_session_delta(
        f"{record.provider.value}-normalized",
        store.normalized_events_path(run_id),
        cursor=0,
        tail_window=False,
    )
    state = transcripts._read_cached_state(  # noqa: SLF001
        f"{record.provider.value}-normalized",
        store.normalized_events_path(run_id),
        str(store.normalized_events_path(run_id)),
    )
    result["patches"] = [
        {
            "id": int(change["id"]),
            **{
                key: change.get(key)
                for key in (
                    "call_id",
                    "output",
                    "ok",
                    "completed_at",
                    "duration_ms",
                    "status",
                    "partial",
                    "terminal_input",
                    "metadata",
                    "edit",
                )
            },
        }
        for change in state.get("changes", [])
        if change.get("kind") == "patch"
    ]
    result.pop("has_older", None)
    result.pop("tail_from", None)
    return result


def _sqlite_payload(event_store: SQLiteEventStore, run_id: str) -> dict[str, Any]:
    cursor = event_store.cursor(run_id)
    rows = event_store.view_rows(run_id)
    projection = rows["projections"]
    if len(projection) != 1:
        raise ValueError(f"SQLite projection is missing for {run_id}")
    projection_row = projection[0]
    patches = []
    for patch in event_store.read_patches(run_id):
        if "event" in patch.patch:
            continue
        patches.append({"id": patch.event_id, **patch.patch})
    return {
        "events": event_store.read_events(run_id),
        "patches": patches,
        "base": cursor.event_base,
        "tokens": json.loads(projection_row[7]) if projection_row[7] else None,
        "tasks": json.loads(projection_row[1]),
        "pr": json.loads(projection_row[2]) if projection_row[2] else None,
        "session_meta": json.loads(projection_row[3]),
        "dispositions": json.loads(projection_row[6]),
        "cursor": cursor.change_cursor,
    }


def compare_run_parity(
    store: RunStore,
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    record: bool = True,
) -> ParityReport:
    """Compare the legacy parser and SQLite payload for one run."""

    cursor = event_store.cursor(run_id)
    normalizer_version = cursor.normalizer_version
    expected = _canonical(_legacy_payload(store, run_id))
    actual = _canonical(_sqlite_payload(event_store, run_id))
    mismatches = tuple(
        ParityMismatch(run_id, normalizer_version, path, left, right)
        for path, left, right in _diff_paths(expected, actual, "")
    )
    if record:
        for mismatch in mismatches:
            event_store.record_parity_record(
                run_id,
                normalizer_version=normalizer_version,
                record_type="mismatch",
                path=mismatch.path,
                expected=mismatch.expected,
                actual=mismatch.actual,
                detail={"source": "legacy-parser-vs-sqlite"},
            )
    return ParityReport(
        run_id=run_id,
        normalizer_version=normalizer_version,
        matched=not mismatches,
        mismatches=mismatches,
    )


def _cursor_metadata(
    event_store: SQLiteEventStore,
    run_id: str,
) -> tuple[str, str] | None:
    try:
        with event_store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT normalizer_version, rebuild_state FROM run_cursors "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return str(row[0]), str(row[1])


def _record_backfill_skip(
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    normalizer_version: str,
    reason: str,
    actual: Any,
) -> None:
    event_store.record_parity_record(
        run_id,
        normalizer_version=normalizer_version,
        record_type="backfill_skipped",
        path=reason,
        actual=actual,
        detail={"reason": reason},
    )


def backfill_headless_runs(
    store: RunStore,
    event_store: SQLiteEventStore,
    *,
    batch_size: int = 32,
) -> list[BackfillResult]:
    """Backfill one bounded batch of headless runs from raw JSONL."""

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    event_store.ensure_schema()
    results: list[BackfillResult] = []
    headless = [
        record
        for record in store.list_runs()
        if record.provider_session_id is not None
    ][:batch_size]
    for record in headless:
        metadata = _cursor_metadata(event_store, record.run_id)
        if metadata is not None:
            stored_version, rebuild_state = metadata
            if stored_version != NORMALIZER_VERSION:
                _record_backfill_skip(
                    event_store,
                    record.run_id,
                    normalizer_version=stored_version,
                    reason="normalizer_version",
                    actual=stored_version,
                )
                results.append(
                    BackfillResult(
                        record.run_id,
                        "skipped",
                        stored_version,
                        "normalizer_version",
                    )
                )
                continue
            if rebuild_state != "ready":
                _record_backfill_skip(
                    event_store,
                    record.run_id,
                    normalizer_version=stored_version,
                    reason="rebuild_state",
                    actual=rebuild_state,
                )
                results.append(
                    BackfillResult(
                        record.run_id,
                        "skipped",
                        stored_version,
                        "rebuild_state",
                    )
                )
                continue
        raw_path = store.raw_events_path(record.run_id)
        if not raw_path.is_file():
            _record_backfill_skip(
                event_store,
                record.run_id,
                normalizer_version=NORMALIZER_VERSION,
                reason="raw_jsonl",
                actual="missing",
            )
            results.append(
                BackfillResult(
                    record.run_id,
                    "skipped",
                    NORMALIZER_VERSION,
                    "raw_jsonl",
                )
            )
            continue
        with tempfile.TemporaryDirectory(prefix="wiki-282-backfill-") as directory:
            temporary_path = Path(directory) / "events.sqlite3"
            replay_raw_jsonl(
                raw_path,
                temporary_path,
                run_id=record.run_id,
                agent_id=record.agent_id,
                provider=record.provider,
                created_at=record.created_at,
            )
            rebuilt = SQLiteEventStore(temporary_path, migrate=False)
            if not rebuilt.run_is_healthy(record.run_id):
                raise RuntimeError(f"backfill replay failed validation for {record.run_id}")
            event_store.replace_run_from(temporary_path, record.run_id)
        report = compare_run_parity(store, event_store, record.run_id)
        event_store.record_parity_record(
            record.run_id,
            normalizer_version=NORMALIZER_VERSION,
            record_type="backfill_completed",
            path="run",
            detail={"matched": report.matched},
        )
        results.append(
            BackfillResult(
                record.run_id,
                "ready" if report.matched else "ready_with_mismatches",
                NORMALIZER_VERSION,
                mismatches=report.mismatches,
            )
        )
    return results


parity_harness = compare_run_parity
backfill = backfill_headless_runs
