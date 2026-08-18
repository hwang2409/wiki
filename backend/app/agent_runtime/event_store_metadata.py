"""Cross-run metadata storage for the runtime event store."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import utc_now

SCHEMA_VERSION = 8

_METADATA_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_METADATA_LOCKS_GUARD = threading.Lock()


def _connect_event_db(path: Path | str, *, read_only: bool = False):
    from .event_store import connect_event_db

    return connect_event_db(path, read_only=read_only)


def _json_bytes(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class ChildRunMapping:
    """Durable parent-to-child identity used by child session reads."""

    parent_run_id: str
    child_id: str
    child_run_id: str
    source_path: str
    source_size: int
    created_at: str


def migrate_metadata_db(path: Path | str) -> None:
    """Create the low-write cross-run metadata database."""

    with _connect_event_db(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        for version in range(1, SCHEMA_VERSION + 1):
            if version not in applied:
                if version == 1:
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS parity_records (
                            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            run_id TEXT NOT NULL,
                            normalizer_version TEXT NOT NULL,
                            record_type TEXT NOT NULL,
                            path TEXT NOT NULL,
                            expected_json TEXT,
                            actual_json TEXT,
                            detail_json TEXT NOT NULL,
                            recorded_at TEXT NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS parity_records_run
                            ON parity_records(run_id, normalizer_version, record_id);
                        CREATE TABLE IF NOT EXISTS backfill_progress (
                            normalizer_version TEXT PRIMARY KEY,
                            cursor_run_id TEXT NOT NULL DEFAULT ''
                        );
                        CREATE TABLE IF NOT EXISTS child_runs (
                            parent_run_id TEXT NOT NULL,
                            child_id TEXT NOT NULL,
                            child_run_id TEXT NOT NULL UNIQUE,
                            source_path TEXT NOT NULL,
                            source_size INTEGER NOT NULL DEFAULT -1,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY (parent_run_id, child_id)
                        );
                        CREATE INDEX IF NOT EXISTS child_runs_parent
                            ON child_runs(parent_run_id, child_id);
                        """
                    )
                elif version == 4:
                    columns = {
                        str(row[1])
                        for row in connection.execute(
                            "PRAGMA table_info(parity_records)"
                        ).fetchall()
                    }
                    if "raw_seq" not in columns:
                        connection.execute(
                            "ALTER TABLE parity_records ADD COLUMN raw_seq INTEGER"
                        )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, "schema-v" + str(version)),
                )


class SQLiteMetadataStore:
    """SQLite store for low-rate state shared by all run databases."""

    def __init__(self, path: Path | str, *, migrate: bool = True) -> None:
        self.path = Path(path)
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        if migrate:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if not self._schema_ready:
                migrate_metadata_db(self.path)
                self._schema_ready = True

    @contextmanager
    def connection(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        connection = _connect_event_db(self.path, read_only=read_only)
        try:
            yield connection
            if not read_only:
                connection.commit()
        except Exception:
            if not read_only:
                connection.rollback()
            raise
        finally:
            connection.close()

    def run_lock(self, run_id: str) -> threading.RLock:
        key = (str(self.path.absolute()), run_id)
        with _RUN_LOCKS_GUARD:
            return _RUN_LOCKS.setdefault(key, threading.RLock())

    def backfill_completed_run_ids(self, normalizer_version: str) -> set[str]:
        self.ensure_schema()
        with self.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT run_id FROM parity_records "
                "WHERE normalizer_version = ? AND record_type = 'backfill_completed'",
                (normalizer_version,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def backfill_cursor(self, normalizer_version: str) -> str:
        self.ensure_schema()
        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT cursor_run_id FROM backfill_progress "
                "WHERE normalizer_version = ?",
                (normalizer_version,),
            ).fetchone()
        return str(row[0]) if row is not None else ""

    def advance_backfill_cursor(self, normalizer_version: str, run_id: str) -> None:
        self.ensure_schema()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO backfill_progress(normalizer_version, cursor_run_id) "
                "VALUES (?, ?) ON CONFLICT(normalizer_version) DO UPDATE SET "
                "cursor_run_id = excluded.cursor_run_id",
                (normalizer_version, run_id),
            )

    def record_parity_record(
        self,
        run_id: str,
        *,
        normalizer_version: str,
        record_type: str,
        path: str,
        detail: dict[str, Any],
        expected: Any = None,
        actual: Any = None,
        raw_seq: int | None = None,
    ) -> None:
        self.ensure_schema()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO parity_records "
                "(run_id, normalizer_version, record_type, path, expected_json, "
                "actual_json, detail_json, recorded_at, raw_seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    normalizer_version,
                    record_type,
                    path,
                    _json_bytes(expected) if expected is not None else None,
                    _json_bytes(actual) if actual is not None else None,
                    _json_bytes(detail),
                    utc_now(),
                    raw_seq,
                ),
            )

    def parity_records(self, run_id: str | None = None) -> list[dict[str, Any]]:
        self.ensure_schema()
        query = (
            "SELECT record_id, run_id, normalizer_version, record_type, path, "
            "expected_json, actual_json, detail_json, recorded_at, raw_seq "
            "FROM parity_records"
        )
        parameters: tuple[Any, ...] = ()
        if run_id is not None:
            query += " WHERE run_id = ?"
            parameters = (run_id,)
        query += " ORDER BY record_id"
        with self.connection(read_only=True) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "record_id": int(row[0]),
                "run_id": str(row[1]),
                "normalizer_version": str(row[2]),
                "record_type": str(row[3]),
                "path": str(row[4]),
                "expected": json.loads(row[5]) if row[5] is not None else None,
                "actual": json.loads(row[6]) if row[6] is not None else None,
                "detail": json.loads(row[7]),
                "recorded_at": str(row[8]),
                "raw_seq": int(row[9]) if row[9] is not None else None,
            }
            for row in rows
        ]

    def child_run_for(
        self, parent_run_id: str, child_id: str
    ) -> ChildRunMapping | None:
        self.ensure_schema()
        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT parent_run_id, child_id, child_run_id, source_path, "
                "source_size, created_at FROM child_runs "
                "WHERE parent_run_id = ? AND child_id = ?",
                (parent_run_id, child_id),
            ).fetchone()
        return (
            ChildRunMapping(
                parent_run_id=str(row[0]),
                child_id=str(row[1]),
                child_run_id=str(row[2]),
                source_path=str(row[3]),
                source_size=int(row[4]),
                created_at=str(row[5]),
            )
            if row is not None
            else None
        )

    def refresh_child_source_fingerprint(
        self, parent_run_id: str, child_id: str, source_size: int
    ) -> None:
        self.ensure_schema()
        with self.connection() as connection:
            connection.execute(
                "UPDATE child_runs SET source_size = ? "
                "WHERE parent_run_id = ? AND child_id = ?",
                (source_size, parent_run_id, child_id),
            )

    def invalidate_child_source_fingerprint(
        self, parent_run_id: str, child_id: str
    ) -> None:
        self.ensure_schema()
        with self.connection() as connection:
            connection.execute(
                "UPDATE child_runs SET source_size = -1 "
                "WHERE parent_run_id = ? AND child_id = ?",
                (parent_run_id, child_id),
            )

    def ensure_child_mapping(
        self,
        *,
        parent_run_id: str,
        child_id: str,
        child_run_id: str,
        source_path: str,
        created_at: str,
    ) -> ChildRunMapping:
        self.ensure_schema()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO child_runs "
                "(parent_run_id, child_id, child_run_id, source_path, "
                "source_size, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(parent_run_id, child_id) DO UPDATE SET "
                "source_path = excluded.source_path",
                (
                    parent_run_id,
                    child_id,
                    child_run_id,
                    source_path,
                    -1,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT parent_run_id, child_id, child_run_id, source_path, "
                "source_size, created_at FROM child_runs "
                "WHERE parent_run_id = ? AND child_id = ?",
                (parent_run_id, child_id),
            ).fetchone()
        if row is None:
            raise KeyError((parent_run_id, child_id))
        return ChildRunMapping(
            parent_run_id=str(row[0]),
            child_id=str(row[1]),
            child_run_id=str(row[2]),
            source_path=str(row[3]),
            source_size=int(row[4]),
            created_at=str(row[5]),
        )
