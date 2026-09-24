"""Cross-run routing for isolated event-store shards."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from .event_store_metadata import SQLiteMetadataStore
from .event_store_migration import (
    _corrupt_raw_run_ids,
    _quarantine_sqlite_set,
    _sqlite_corruption_confirmed,
    migrate_legacy_event_db,
)
from .event_store_shard import (
    ChildRunMapping,
    EventPatch,
    OlderReadSnapshot,
    ReducerResult,
    RunCursor,
    SessionReadSnapshot,
    SQLiteEventStore,
    connect_event_db,
    runtime_event_db_path,
    runtime_metadata_db_path,
)


RECENT_ARCHIVE_ARTIFACT_SESSIONS = 40
RECENT_ARCHIVE_ARTIFACT_BYTES = 128 * 1024 * 1024


@lru_cache(maxsize=RECENT_ARCHIVE_ARTIFACT_SESSIONS)
def _archived_artifact_events(
    events_path: Path, _mtime_ns: int, _size: int
) -> tuple[dict[str, Any], ...]:
    """Parse one immutable archive file once for repeated palette searches."""

    events: list[dict[str, Any]] = []
    with events_path.open("rb") as handle:
        for line in handle:
            if b'"artifact"' not in line:
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get("kind") == "artifact":
                events.append(value)
    return tuple(events)


class EventStoreRouter:
    """Route the existing event-store API to one database per run."""

    def __init__(
        self,
        runtime_dir: Path | str,
        *,
        migrate: bool = True,
        archive_dir: Path | str | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.archive_dir = Path(archive_dir) if archive_dir is not None else None
        self.path = runtime_event_db_path(self.runtime_dir)
        self.metadata_path = runtime_metadata_db_path(self.runtime_dir)
        try:
            self.metadata_store = SQLiteMetadataStore(
                self.metadata_path,
                migrate=migrate,
            )
        except sqlite3.DatabaseError as error:
            if not migrate or not _sqlite_corruption_confirmed(
                self.metadata_path, error
            ):
                raise
            _quarantine_sqlite_set(self.metadata_path, "corrupt")
            self.metadata_store = SQLiteMetadataStore(self.metadata_path)
        self._stores: dict[str, SQLiteEventStore] = {}
        if migrate:
            migrate_legacy_event_db(self.runtime_dir)
        self.corrupt_raw_run_ids = (
            _corrupt_raw_run_ids(self.runtime_dir) if migrate else set()
        )

    def ensure_schema(self) -> None:
        self.metadata_store.ensure_schema()

    @contextmanager
    def connection(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        """Keep the raw-connection helper useful for one-run callers."""

        run_paths = list(self.runtime_dir.joinpath("runs").glob("*/events.sqlite3"))
        if len(run_paths) != 1:
            raise RuntimeError("select a run with RuntimeEventStore.for_run")
        with SQLiteEventStore(
            run_paths[0], self.metadata_store, migrate=False
        ) as store:
            with store.connection(read_only=read_only) as connection:
                yield connection

    def for_run(self, run_id: str) -> SQLiteEventStore:
        store = self._stores.get(run_id)
        if store is None:
            store = SQLiteEventStore(
                runtime_event_db_path(self.runtime_dir, run_id),
                self.metadata_store,
                migrate=False,
            )
            self._stores[run_id] = store
        return store

    def close_run(self, run_id: str) -> None:
        store = self._stores.pop(run_id, None)
        if store is not None:
            store.close()

    def close(self) -> None:
        for run_id in tuple(self._stores):
            self.close_run(run_id)

    def read_artifact_events(
        self,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Stream live artifacts and artifacts from recent archived sessions."""

        def cancelled() -> bool:
            return should_cancel is not None and should_cancel()

        for path in self.runtime_dir.joinpath("runs").glob("*/events.sqlite3"):
            if cancelled():
                break
            try:
                with connect_event_db(path, read_only=True) as connection:
                    cursor = connection.execute(
                        "SELECT run_id, event_json, updated_at FROM events "
                        "WHERE kind = 'artifact' ORDER BY updated_at DESC"
                    )
                    while True:
                        rows = cursor.fetchmany(256)
                        if not rows:
                            break
                        for row in rows:
                            if cancelled():
                                return
                            try:
                                event = json.loads(row[1])
                            except (
                                TypeError,
                                ValueError,
                                KeyError,
                                json.JSONDecodeError,
                            ):
                                continue
                            if isinstance(event, dict):
                                yield str(row[0]), event
            except (OSError, sqlite3.DatabaseError):
                continue
        if self.archive_dir is not None and self.archive_dir.is_dir():
            sessions: list[Path] = []
            try:
                for ticket_dir in self.archive_dir.iterdir():
                    if cancelled():
                        return
                    if not ticket_dir.is_dir() or ticket_dir.is_symlink():
                        continue
                    try:
                        for session_dir in ticket_dir.iterdir():
                            name = session_dir.name
                            if (
                                len(name) == 15
                                and name[8] == "-"
                                and name[:8].isdigit()
                                and name[9:].isdigit()
                                and session_dir.is_dir()
                                and not session_dir.is_symlink()
                            ):
                                sessions.append(session_dir)
                    except OSError:
                        continue
            except OSError:
                return
            sessions.sort(key=lambda path: path.name, reverse=True)
            scanned = 0
            scanned_bytes = 0
            for session_dir in sessions:
                if cancelled():
                    return
                events_path = session_dir / "events.jsonl"
                if (
                    not (session_dir / "archive-complete.json").is_file()
                    or not events_path.is_file()
                    or events_path.is_symlink()
                ):
                    continue
                try:
                    run_value = json.loads(
                        (session_dir / "run.json").read_text(encoding="utf-8")
                    )
                    run_id = str(run_value["run_id"])
                    if scanned >= RECENT_ARCHIVE_ARTIFACT_SESSIONS:
                        break
                    scanned += 1
                    stat = events_path.stat()
                    if stat.st_size > RECENT_ARCHIVE_ARTIFACT_BYTES - scanned_bytes:
                        continue
                    scanned_bytes += stat.st_size
                    for event in _archived_artifact_events(
                        events_path, stat.st_mtime_ns, stat.st_size
                    ):
                        if cancelled():
                            return
                        yield run_id, event
                except (OSError, TypeError, ValueError, KeyError):
                    continue

    def create_run(self, run_id: str, **kwargs: Any) -> None:
        self.for_run(run_id).create_run(run_id, **kwargs)

    def ensure_child_run(self, *, parent_run_id: str, **kwargs: Any) -> ChildRunMapping:
        return self.for_run(parent_run_id).ensure_child_run(
            parent_run_id=parent_run_id,
            **kwargs,
        )

    def cursor(self, run_id: str) -> RunCursor:
        return self.for_run(run_id).cursor(run_id)

    def has_disposition(self, run_id: str, raw_seq: int) -> bool:
        return self.for_run(run_id).has_disposition(run_id, raw_seq)

    def materialized_raw_seqs(self, run_id: str) -> set[int]:
        return self.for_run(run_id).materialized_raw_seqs(run_id)

    def run_lock(self, run_id: str) -> threading.RLock:
        return self.for_run(run_id).run_lock(run_id)

    def materialize(self, run_id: str, *args: Any, **kwargs: Any) -> ReducerResult:
        return self.for_run(run_id).materialize(run_id, *args, **kwargs)

    def materialize_raw_rows(self, run_id: str, *args: Any, **kwargs: Any) -> None:
        self.for_run(run_id).materialize_raw_rows(run_id, *args, **kwargs)

    def restore_reducer(self, run_id: str, *args: Any, **kwargs: Any) -> None:
        self.for_run(run_id).restore_reducer(run_id, *args, **kwargs)

    def run_format(self, run_id: str) -> str:
        return self.for_run(run_id).run_format(run_id)

    def run_is_healthy(self, run_id: str) -> bool:
        return self.for_run(run_id).run_is_healthy(run_id)

    def disposition_coverage(self, run_id: str) -> tuple[int, int, int]:
        return self.for_run(run_id).disposition_coverage(run_id)

    def has_run_projection(self, run_id: str) -> bool:
        return self.for_run(run_id).has_run_projection(run_id)

    def rebuild_generation(self, run_id: str) -> int:
        return self.for_run(run_id).rebuild_generation(run_id)

    def read_events(self, run_id: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.for_run(run_id).read_events(run_id, *args, **kwargs)

    def read_normalized_events(
        self, run_id: str, *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return self.for_run(run_id).read_normalized_events(run_id, *args, **kwargs)

    def read_older_snapshot(self, run_id: str, *args: Any, **kwargs: Any) -> OlderReadSnapshot:
        return self.for_run(run_id).read_older_snapshot(run_id, *args, **kwargs)

    def read_patches(self, run_id: str, *args: Any, **kwargs: Any) -> list[EventPatch]:
        return self.for_run(run_id).read_patches(run_id, *args, **kwargs)

    def read_session_snapshot(
        self, run_id: str, *args: Any, **kwargs: Any
    ) -> SessionReadSnapshot:
        return self.for_run(run_id).read_session_snapshot(run_id, *args, **kwargs)

    def view_rows(self, run_id: str) -> dict[str, list[tuple[Any, ...]]]:
        return self.for_run(run_id).view_rows(run_id)

    def _expected_view_rows(self, run_id: str, **kwargs: Any) -> dict[str, list[tuple[Any, ...]]]:
        return self.for_run(run_id)._expected_view_rows(run_id, **kwargs)

    def replace_run_from(self, source: Path | str, run_id: str) -> None:
        self.for_run(run_id).replace_run_from(source, run_id)

    def export_events_jsonl(self, run_id: str, destination: Path | str, **kwargs: Any) -> bool:
        return self.for_run(run_id).export_events_jsonl(run_id, destination, **kwargs)

    def backfill_completed_run_ids(self, normalizer_version: str) -> set[str]:
        return self.metadata_store.backfill_completed_run_ids(normalizer_version)

    def backfill_cursor(self, normalizer_version: str) -> str:
        return self.metadata_store.backfill_cursor(normalizer_version)

    def advance_backfill_cursor(self, normalizer_version: str, run_id: str) -> None:
        self.metadata_store.advance_backfill_cursor(normalizer_version, run_id)

    def record_parity_record(self, run_id: str, **kwargs: Any) -> None:
        self.metadata_store.record_parity_record(run_id, **kwargs)

    def parity_records(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.metadata_store.parity_records(run_id)

    def child_run_for(self, parent_run_id: str, child_id: str) -> ChildRunMapping | None:
        return self.metadata_store.child_run_for(parent_run_id, child_id)

    def refresh_child_source_fingerprint(
        self, parent_run_id: str, child_id: str, source_size: int
    ) -> None:
        self.metadata_store.refresh_child_source_fingerprint(
            parent_run_id, child_id, source_size
        )

    def invalidate_child_source_fingerprint(
        self, parent_run_id: str, child_id: str
    ) -> None:
        self.metadata_store.invalidate_child_source_fingerprint(parent_run_id, child_id)
