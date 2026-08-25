"""Archive export parity and bounded SQLite backfill helpers."""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import transcripts
from .event_store import NORMALIZER_VERSION, SQLiteEventStore, replay_raw_jsonl
from .store import RunStore
from .types import TERMINAL_STATES


_IGNORED_PARITY_KEYS = frozenset(
    {"normalized_at", "path", "file_path", "transcript_path"}
)

_LOG = logging.getLogger(__name__)


def harness_error_fallback_path(event_store: SQLiteEventStore) -> Path:
    """Return the append-only fallback journal for failed parity writes."""

    return event_store.path.parent / "archive-parity-harness-errors.jsonl"


def _record_parity_record(
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    normalizer_version: str,
    record_type: str,
    path: str,
    detail: dict[str, Any],
    expected: Any = None,
    actual: Any = None,
    raw_seq: int | None = None,
) -> bool:
    """Record parity output without allowing recorder failures to stop a batch."""

    try:
        event_store.record_parity_record(
            run_id,
            normalizer_version=normalizer_version,
            record_type=record_type,
            path=path,
            detail=detail,
            expected=expected,
            actual=actual,
            raw_seq=raw_seq,
        )
    except Exception as exc:
        fallback = {
            "run_id": run_id,
            "normalizer_version": normalizer_version,
            "record_type": record_type,
            "path": path,
            "detail": detail,
            "raw_seq": raw_seq,
            "recording_error": f"{type(exc).__name__}: {exc}",
        }
        _LOG.exception("archive parity recorder failed for %s", run_id)
        try:
            fallback_path = harness_error_fallback_path(event_store)
            fallback_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with fallback_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(fallback, sort_keys=True))
                handle.write("\n")
        except Exception:
            _LOG.exception("archive parity fallback recorder failed for %s", run_id)
        return False
    return True


def _record_harness_error(
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    path: str,
    error: Exception,
) -> None:
    _record_parity_record(
        event_store,
        run_id,
        normalizer_version=_safe_normalizer_version(event_store, run_id),
        record_type="harness_error",
        path=path,
        detail={"error": f"{type(error).__name__}: {error}"},
    )


@dataclass(frozen=True)
class ParityMismatch:
    run_id: str
    normalizer_version: str
    path: str
    expected: Any
    actual: Any
    raw_seq: int | None = None


@dataclass(frozen=True)
class ParityReport:
    run_id: str
    normalizer_version: str
    matched: bool
    mismatches: tuple[ParityMismatch, ...]
    boundary: str = "final"
    raw_seq: int | None = None


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


def _legacy_payload_at_path(
    provider: Any,
    normalized_path: Path,
) -> dict[str, Any]:
    transcripts._cache.clear()
    result = transcripts.read_session_delta(
        f"{provider.value}-normalized",
        normalized_path,
        cursor=0,
        tail_window=False,
    )
    state = transcripts._read_cached_state(  # noqa: SLF001
        f"{provider.value}-normalized",
        normalized_path,
        str(normalized_path),
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


def _legacy_payload(store: RunStore, run_id: str) -> dict[str, Any]:
    record = store.get(run_id)
    return _legacy_payload_at_path(
        record.provider,
        store.normalized_events_path(run_id),
    )


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
    boundary: str = "final",
    raw_seq: int | None = None,
) -> ParityReport:
    """Compare the legacy parser and SQLite payload for one run."""

    cursor = event_store.cursor(run_id)
    normalizer_version = cursor.normalizer_version
    effective_raw_seq = raw_seq if raw_seq is not None else cursor.raw_seq
    expected = _canonical(_legacy_payload(store, run_id))
    actual = _canonical(_sqlite_payload(event_store, run_id))
    mismatches = tuple(
        ParityMismatch(
            run_id,
            normalizer_version,
            path,
            left,
            right,
            effective_raw_seq,
        )
        for path, left, right in _diff_paths(expected, actual, "")
    )
    if record:
        for mismatch in mismatches:
            _record_parity_record(
                event_store,
                run_id,
                normalizer_version=normalizer_version,
                record_type="mismatch",
                path=mismatch.path,
                expected=mismatch.expected,
                actual=mismatch.actual,
                detail={
                    "source": "legacy-parser-vs-sqlite",
                    "boundary": boundary,
                    "raw_seq": effective_raw_seq,
                },
                raw_seq=effective_raw_seq,
            )
    return ParityReport(
        run_id=run_id,
        normalizer_version=normalizer_version,
        matched=not mismatches,
        mismatches=mismatches,
        boundary=boundary,
        raw_seq=effective_raw_seq,
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def _compare_boundary(
    event_store: SQLiteEventStore,
    run_id: str,
    expected: Any,
    actual: Any,
    *,
    boundary: str,
    raw_seq: int | None,
    record: bool,
) -> ParityReport:
    normalizer_version = event_store.cursor(run_id).normalizer_version
    mismatches = tuple(
        ParityMismatch(run_id, normalizer_version, path, left, right, raw_seq)
        for path, left, right in _diff_paths(
            _canonical(expected), _canonical(actual), ""
        )
    )
    if record:
        for mismatch in mismatches:
            _record_parity_record(
                event_store,
                run_id,
                normalizer_version=normalizer_version,
                record_type="mismatch",
                path=mismatch.path,
                expected=mismatch.expected,
                actual=mismatch.actual,
                detail={
                    "source": "legacy-parser-vs-sqlite",
                    "boundary": boundary,
                    "raw_seq": raw_seq,
                },
                raw_seq=raw_seq,
            )
    return ParityReport(
        run_id,
        normalizer_version,
        not mismatches,
        mismatches,
        boundary=boundary,
        raw_seq=raw_seq,
    )


def _compare_raw_prefixes(
    store: RunStore,
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    record: bool,
) -> tuple[ParityReport, ...]:
    record_data = store.get(run_id)
    raw_rows = sorted(store.read_raw_events(run_id), key=lambda row: int(row["seq"]))
    normalized_rows = list(store.iter_normalized_events(run_id))
    reports: list[ParityReport] = []
    with tempfile.TemporaryDirectory(prefix="wiki-282-parity-") as directory:
        directory_path = Path(directory)
        for raw_row in raw_rows:
            prefix_seq = int(raw_row["seq"])
            raw_path = directory_path / f"raw-{prefix_seq}.jsonl"
            normalized_path = directory_path / f"normalized-{prefix_seq}.jsonl"
            _write_jsonl(
                raw_path,
                [row for row in raw_rows if int(row["seq"]) <= prefix_seq],
            )
            _write_jsonl(
                normalized_path,
                [
                    row
                    for row in normalized_rows
                    if int(row.get("raw_seq", 0)) <= prefix_seq
                ],
            )
            prefix_store = replay_raw_jsonl(
                raw_path,
                directory_path / f"prefix-{prefix_seq}.sqlite3",
                run_id=run_id,
                agent_id=record_data.agent_id,
                provider=record_data.provider,
                created_at=record_data.created_at,
            )
            reports.append(
                _compare_boundary(
                    event_store,
                    run_id,
                    _legacy_payload_at_path(record_data.provider, normalized_path),
                    _sqlite_payload(prefix_store, run_id),
                    boundary="raw_prefix",
                    raw_seq=prefix_seq,
                    record=record,
                )
            )
    return tuple(reports)


def _compare_crash_boundary(
    store: RunStore,
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    record: bool,
) -> ParityReport:
    record_data = store.get(run_id)
    with tempfile.TemporaryDirectory(prefix="wiki-282-crash-") as directory:
        rebuilt = replay_raw_jsonl(
            store.raw_events_path(run_id),
            Path(directory) / "replayed.sqlite3",
            run_id=run_id,
            agent_id=record_data.agent_id,
            provider=record_data.provider,
            created_at=record_data.created_at,
        )
        return _compare_boundary(
            event_store,
            run_id,
            _sqlite_payload(rebuilt, run_id),
            _sqlite_payload(event_store, run_id),
            boundary="crash_boundary",
            raw_seq=event_store.cursor(run_id).raw_seq,
            record=record,
        )


def _compare_stale_cursor(
    store: RunStore,
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    record: bool,
) -> ParityReport:
    record_data = store.get(run_id)
    cursor = event_store.cursor(run_id)
    transcripts._cache.clear()
    expected = transcripts.read_session_delta(
        f"{record_data.provider.value}-normalized",
        store.normalized_events_path(run_id),
        cursor=cursor.change_cursor + 1,
        tail_window=False,
    )
    expected.pop("tail_from", None)
    expected.pop("has_older", None)
    actual = _sqlite_payload(event_store, run_id)
    actual["patches"] = []
    return _compare_boundary(
        event_store,
        run_id,
        expected,
        actual,
        boundary="stale_cursor",
        raw_seq=cursor.raw_seq,
        record=record,
    )


def _compare_older_page(
    store: RunStore,
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    record: bool,
) -> ParityReport:
    record_data = store.get(run_id)
    cursor = event_store.cursor(run_id)
    transcripts._cache.clear()
    expected = transcripts.read_older_session(
        f"{record_data.provider.value}-normalized",
        store.normalized_events_path(run_id),
        before=len(list(store.iter_normalized_events(run_id))),
        count=max(1, len(list(store.iter_normalized_events(run_id)))),
    )
    actual = {
        "events": event_store.read_events(run_id),
        "base": cursor.event_base,
        "has_older": False,
    }
    return _compare_boundary(
        event_store,
        run_id,
        expected,
        actual,
        boundary="older_page",
        raw_seq=cursor.raw_seq,
        record=record,
    )


def _compare_patch_only_delta(
    store: RunStore,
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    record: bool,
) -> ParityReport:
    record_data = store.get(run_id)
    cursor = event_store.cursor(run_id)
    patch_cursor = max(0, cursor.change_cursor - 1)
    transcripts._cache.clear()
    expected = transcripts.read_session_delta(
        f"{record_data.provider.value}-normalized",
        store.normalized_events_path(run_id),
        cursor=patch_cursor,
        tail_window=False,
    )
    actual = {
        "patches": [
            {"id": patch.event_id, **patch.patch}
            for patch in event_store.read_patches(run_id, after_cursor=patch_cursor)
        ]
    }
    return _compare_boundary(
        event_store,
        run_id,
        {"patches": expected.get("patches", [])},
        actual,
        boundary="patch_only_delta",
        raw_seq=cursor.raw_seq,
        record=record,
    )


def compare_run_boundaries(
    store: RunStore,
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    record: bool = True,
) -> tuple[ParityReport, ...]:
    """Compare raw prefixes and each cursor boundary independently."""

    reports = list(_compare_raw_prefixes(store, event_store, run_id, record=record))
    reports.extend(
        (
            _compare_crash_boundary(store, event_store, run_id, record=record),
            _compare_stale_cursor(store, event_store, run_id, record=record),
            _compare_older_page(store, event_store, run_id, record=record),
            _compare_patch_only_delta(store, event_store, run_id, record=record),
        )
    )
    reports.append(compare_run_parity(store, event_store, run_id, record=record))
    return tuple(reports)


def _safe_normalizer_version(
    event_store: SQLiteEventStore,
    run_id: str,
) -> str:
    metadata = _cursor_metadata(event_store, run_id)
    return metadata[0] if metadata is not None else NORMALIZER_VERSION


def run_parity_batch(
    store: RunStore,
    event_store: SQLiteEventStore,
    run_ids: list[str],
) -> dict[str, tuple[ParityReport, ...]]:
    """Run parity checks without allowing one broken run to stop the batch."""

    reports: dict[str, tuple[ParityReport, ...]] = {}
    for run_id in run_ids:
        try:
            reports[run_id] = compare_run_boundaries(store, event_store, run_id)
        except Exception as exc:
            _record_harness_error(
                event_store,
                run_id,
                path="comparison",
                error=exc,
            )
            continue
    return reports


def _cursor_metadata(
    event_store: SQLiteEventStore,
    run_id: str,
) -> tuple[str, str] | None:
    try:
        cursor = event_store.cursor(run_id)
    except Exception:
        return None
    return cursor.normalizer_version, cursor.rebuild_state


def _record_backfill_skip(
    event_store: SQLiteEventStore,
    run_id: str,
    *,
    normalizer_version: str,
    reason: str,
    actual: Any,
) -> None:
    _record_parity_record(
        event_store,
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
    completed = event_store.backfill_completed_run_ids(NORMALIZER_VERSION)
    terminal = sorted(
        (
            record
            for record in store.list_runs()
            if record.provider_session_id is not None
            and record.state in TERMINAL_STATES
            and record.run_id not in completed
        ),
        key=lambda record: record.run_id,
    )
    cursor = event_store.backfill_cursor(NORMALIZER_VERSION)
    candidates = [record for record in terminal if record.run_id > cursor]
    candidates.extend(record for record in terminal if record.run_id <= cursor)
    batch = candidates[:batch_size]
    metadata_by_run = {
        record.run_id: _cursor_metadata(event_store, record.run_id)
        for record in batch
    }

    for record in batch:
        metadata = metadata_by_run[record.run_id]
        version = metadata[0] if metadata is not None else NORMALIZER_VERSION
        lock = event_store.run_lock(record.run_id)
        if not lock.acquire(blocking=False):
            _record_backfill_skip(
                event_store,
                record.run_id,
                normalizer_version=version,
                reason="locked",
                actual="live_materializer",
            )
            event_store.advance_backfill_cursor(NORMALIZER_VERSION, record.run_id)
            results.append(
                BackfillResult(record.run_id, "skipped", version, "locked")
            )
            continue
        lock_held = True
        try:
            current = store.get(record.run_id)
            if current.state not in TERMINAL_STATES:
                _record_backfill_skip(
                    event_store,
                    record.run_id,
                    normalizer_version=version,
                    reason="nonterminal",
                    actual=current.state.value,
                )
                results.append(
                    BackfillResult(record.run_id, "skipped", version, "nonterminal")
                )
                continue
            if metadata is not None:
                version, rebuild_state = metadata
                if version != NORMALIZER_VERSION:
                    _record_backfill_skip(
                        event_store,
                        record.run_id,
                        normalizer_version=version,
                        reason="normalizer_version",
                        actual=version,
                    )
                    results.append(
                        BackfillResult(
                            record.run_id,
                            "skipped",
                            version,
                            "normalizer_version",
                        )
                    )
                    continue
                if rebuild_state != "ready":
                    _record_backfill_skip(
                        event_store,
                        record.run_id,
                        normalizer_version=version,
                        reason="rebuild_state",
                        actual=rebuild_state,
                    )
                    results.append(
                        BackfillResult(
                            record.run_id,
                            "skipped",
                            version,
                            "rebuild_state",
                        )
                    )
                    continue
            raw_path = store.raw_events_path(record.run_id)
            if not raw_path.is_file():
                _record_backfill_skip(
                    event_store,
                    record.run_id,
                    normalizer_version=version,
                    reason="raw_jsonl",
                    actual="missing",
                )
                results.append(
                    BackfillResult(record.run_id, "skipped", version, "raw_jsonl")
                )
                continue
            with tempfile.TemporaryDirectory(prefix="wiki-282-backfill-") as directory:
                temporary_path = (
                    Path(directory) / "runs" / record.run_id / "events.sqlite3"
                )
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
                    raise RuntimeError(
                        f"backfill replay failed validation for {record.run_id}"
                    )
                event_store.replace_run_from(temporary_path, record.run_id)
            # The boundary compare replays every raw prefix — O(events^2).
            # Holding the run lock across it starves a concurrent archive of
            # the same run for the whole sweep (2026-08-24 write outage), so
            # release now; a compare racing an archive fails into the
            # harness_error path below, which is safe to retry.
            lock.release()
            lock_held = False
            reports = compare_run_boundaries(store, event_store, record.run_id)
            mismatches = tuple(
                mismatch for report in reports for mismatch in report.mismatches
            )
            matched = not mismatches
            if not _record_parity_record(
                event_store,
                record.run_id,
                normalizer_version=NORMALIZER_VERSION,
                record_type="backfill_completed",
                path="run",
                detail={"matched": matched, "boundaries": len(reports)},
            ):
                results.append(
                    BackfillResult(
                        record.run_id,
                        "harness_error",
                        version,
                        "backfill completion record failed",
                    )
                )
                continue
            results.append(
                BackfillResult(
                    record.run_id,
                    "ready" if matched else "ready_with_mismatches",
                    NORMALIZER_VERSION,
                    mismatches=mismatches,
                )
            )
        except Exception as exc:
            _record_harness_error(
                event_store,
                record.run_id,
                path="backfill",
                error=exc,
            )
            results.append(
                BackfillResult(record.run_id, "harness_error", version, str(exc)))
        finally:
            if lock_held:
                lock.release()
            event_store.advance_backfill_cursor(NORMALIZER_VERSION, record.run_id)
    return results


parity_harness = compare_run_parity
backfill = backfill_headless_runs
