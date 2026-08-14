"""SQLite materialization for the provider-neutral session view.

The raw provider log remains the source of truth.  This module owns the
transactional cache used by the later read-path migrations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import transcripts
from . import store as runtime_store
from .normalizer import NormalizedProviderEvent, normalize_provider_event
from .types import (
    EventDisposition,
    LifecycleState,
    ProviderKind,
    utc_now,
    validate_transition,
)

SCHEMA_VERSION = 8
NORMALIZER_VERSION = "wiki-282-1"


_RUN_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_RUN_LOCKS_GUARD = threading.Lock()


def runtime_event_db_path(runtime_dir: Path | str) -> Path:
    """Return the shared event database path for a runtime directory."""

    return Path(runtime_dir) / "events.sqlite3"


def _json_bytes(value: Any) -> str:
    """Return the canonical JSON representation stored in the view."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _provider_kind(value: object) -> ProviderKind:
    if isinstance(value, ProviderKind):
        return value
    aliases = {"cdx": ProviderKind.CODEX, "cc": ProviderKind.CLAUDE}
    if isinstance(value, str) and value in aliases:
        return aliases[value]
    return ProviderKind(str(value))


def _source_size(source_path: Path | str) -> int:
    try:
        return Path(source_path).stat().st_size
    except OSError:
        return -1


def _stored_disposition(disposition: EventDisposition) -> str:
    if disposition is EventDisposition.IGNORED:
        return transcripts.EVENT_DISPOSITION_IGNORED
    return disposition.value


def _normalize(raw: dict[str, Any]) -> NormalizedProviderEvent:
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return NormalizedProviderEvent(
            EventDisposition.UNKNOWN,
            "normalization_error",
            {"error": "raw payload is not an object", "raw_payload": payload},
        )
    try:
        return normalize_provider_event(
            _provider_kind(raw.get("provider")),
            payload,
            direction=str(raw.get("direction") or "provider"),
        )
    except (TypeError, ValueError, KeyError) as exc:
        return NormalizedProviderEvent(
            EventDisposition.UNKNOWN,
            "normalization_error",
            {"error": str(exc), "raw_payload": payload},
        )


@dataclass(frozen=True)
class RunCursor:
    run_id: str
    raw_seq: int = 0
    materialized_raw_seq: int = 0
    next_event_id: int = 0
    event_base: int = 0
    event_count: int = 0
    change_cursor: int = 0
    patch_base_cursor: int = 0
    last_causal_raw_seq: int = 0
    last_lifecycle_change: tuple[int, int] | None = None
    normalizer_version: str = NORMALIZER_VERSION
    rebuild_state: str = "ready"
    rebuild_generation: int = 0


@dataclass(frozen=True)
class EventPatch:
    event_id: int
    raw_seq: int
    patch: dict[str, Any]
    event_revision: int
    change_cursor: int = 0
    created_at: str = ""

    @property
    def patch_json(self) -> str:
        return _json_bytes(self.patch)


@dataclass(frozen=True)
class SessionReadSnapshot:
    """All SQLite rows needed to build one session response."""

    state: RunCursor
    source_key: str
    projection: tuple[Any, ...]
    events: tuple[dict[str, Any], ...]
    patches: tuple[EventPatch, ...]


@dataclass(frozen=True)
class ChildRunMapping:
    """Durable parent-to-child identity used by child session reads."""

    parent_run_id: str
    child_id: str
    child_run_id: str
    source_path: str
    source_size: int
    created_at: str


@dataclass(frozen=True)
class OlderReadSnapshot:
    """One transaction's older-page rows and source identity."""

    state: RunCursor
    source_key: str
    events: tuple[dict[str, Any], ...]
    has_older: bool


@dataclass(frozen=True)
class ReducerResult:
    raw_seq: int
    normalized: dict[str, Any]
    changes: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    event_raw_seqs: dict[int, int]
    state: dict[str, Any]
    projection_changed: bool = False
    change_cursor: int = 0


@dataclass
class _Projection:
    state: LifecycleState = LifecycleState.STARTING
    unread_event_seq: int = 0
    last_causal_raw_seq: int = 0
    pending_requests: dict[str, dict[str, Any]] | None = None
    pending_user_messages: list[dict[str, Any]] | None = None
    composer_messages: list[dict[str, Any]] | None = None
    current_turn: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.pending_requests is None:
            self.pending_requests = {}
        if self.pending_user_messages is None:
            self.pending_user_messages = []
        if self.composer_messages is None:
            self.composer_messages = []


class EventReducerAdapter:
    """Run the existing transcript reducer against normalized envelopes.

    ``transcripts._new_parse_state`` is intentionally retained as the sole
    parser state.  This adapter does not copy, flatten, or replace its
    lifecycle registries and pending buffers.
    """

    def __init__(
        self,
        provider: ProviderKind | str,
        *,
        normalizer_version: str = NORMALIZER_VERSION,
    ) -> None:
        self.provider = _provider_kind(provider)
        self.format = self.provider.value
        self.normalizer_version = normalizer_version
        self.state = transcripts._new_parse_state(self.format)
        self.projection = _Projection()
        self._event_raw_seqs: dict[int, int] = {}
        self._change_cursor = 0

    def apply_raw(
        self,
        raw: dict[str, Any],
        normalized: NormalizedProviderEvent | None = None,
    ) -> ReducerResult:
        raw_seq = int(raw["seq"])
        normalized_seq = int(raw.get("normalized_seq", raw_seq))
        if normalized is None:
            normalized = _normalize(raw)
        received_at = str(raw.get("received_at") or "")
        row = {
            "seq": normalized_seq,
            "raw_seq": raw_seq,
            "normalized_at": received_at,
            "disposition": _stored_disposition(normalized.disposition),
            "kind": normalized.kind,
            "payload": normalized.payload,
            "lifecycle_state": (
                normalized.lifecycle_state.value
                if normalized.lifecycle_state is not None
                else None
            ),
        }
        return self._apply_row(row, normalized)

    def apply_normalized_row(self, row: dict[str, Any]) -> ReducerResult:
        disposition = row.get("disposition")
        if disposition in {"ignored", transcripts.EVENT_DISPOSITION_IGNORED}:
            event_disposition = EventDisposition.IGNORED
        else:
            event_disposition = EventDisposition(str(disposition))
        lifecycle_value = row.get("lifecycle_state")
        lifecycle_state = (
            LifecycleState(str(lifecycle_value))
            if lifecycle_value is not None
            else None
        )
        normalized = NormalizedProviderEvent(
            event_disposition,
            str(row["kind"]),
            row.get("payload") if isinstance(row.get("payload"), dict) else {},
            lifecycle_state,
        )
        return self._apply_row(row, normalized)

    def _apply_row(
        self,
        row: dict[str, Any],
        normalized: NormalizedProviderEvent,
    ) -> ReducerResult:
        raw_seq = int(row["raw_seq"])
        before_projection = self._client_projection_key()
        before_events = {
            int(event["id"]): _json_bytes(event)
            for event in self.state.get("events", [])
            if isinstance(event, dict) and isinstance(event.get("id"), int)
        }
        before_cursor = int(self.state.get("cursor", 0))

        if self.provider is ProviderKind.CODEX:
            transcripts._codex_normalized_apply(self.state, row)
        else:
            transcripts._claude_normalized_apply(self.state, row)

        changes = tuple(
            change
            for change in self.state.get("changes", [])
            if int(change.get("cursor", 0)) > before_cursor
        )
        current_events = tuple(
            event
            for event in self.state.get("events", [])
            if isinstance(event, dict) and isinstance(event.get("id"), int)
        )
        current_by_id = {int(event["id"]): event for event in current_events}
        for change in changes:
            if change.get("kind") == "tail":
                event_id = int(change["from"])
                if event_id in current_by_id:
                    self._event_raw_seqs[event_id] = raw_seq
            elif change.get("kind") == "patch":
                self._event_raw_seqs[int(change["id"])] = raw_seq
        for event_id in current_by_id:
            if event_id not in self._event_raw_seqs and event_id not in before_events:
                self._event_raw_seqs[event_id] = raw_seq

        self._apply_projection(row, normalized)
        projection_changed = before_projection != self._client_projection_key()
        parser_changes = tuple(
            {**change, "cursor": 0}
            for change in changes
        )
        for change in parser_changes:
            self._change_cursor += 1
            change["cursor"] = self._change_cursor
        if projection_changed and not parser_changes:
            self._change_cursor += 1
        return ReducerResult(
            raw_seq=raw_seq,
            normalized=row,
            changes=parser_changes,
            events=current_events,
            event_raw_seqs=dict(self._event_raw_seqs),
            state=self.state,
            projection_changed=projection_changed,
            change_cursor=self._change_cursor,
        )

    def _client_projection_key(self) -> str:
        projection = self.projection_json()
        projection.pop("disposition_counts", None)
        projection.pop("last_causal_raw_seq", None)
        projection.pop("pending_user_messages", None)
        return _json_bytes(projection)

    def replace_with(self, other: EventReducerAdapter) -> None:
        """Adopt a clean committed replay after a failed transaction."""

        self.state = other.state
        self.projection = other.projection
        self._event_raw_seqs = other._event_raw_seqs
        self._change_cursor = other._change_cursor

    def _apply_projection(
        self,
        row: dict[str, Any],
        normalized: NormalizedProviderEvent,
    ) -> None:
        raw_seq = int(row["raw_seq"])
        if raw_seq < self.projection.last_causal_raw_seq:
            return
        self.projection.last_causal_raw_seq = raw_seq
        if normalized.lifecycle_state is not None:
            current = self.projection.state
            target = normalized.lifecycle_state
            try:
                if target is not current:
                    validate_transition(current, target)
            except ValueError:
                pass
            else:
                self.projection.state = target
                if target in {LifecycleState.COMPLETED, LifecycleState.DEAD}:
                    self.projection.pending_requests = {}
        payload = normalized.payload
        kind = normalized.kind
        if runtime_store._is_unread_worthy(
            kind,
            payload,
            normalized.disposition.value,
        ):
            self.projection.unread_event_seq = max(
                self.projection.unread_event_seq,
                int(row["seq"]),
            )
        if kind == "turn_started":
            params = payload.get("params")
            turn = params.get("turn") if isinstance(params, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            self.projection.current_turn = {
                "turn_id": turn_id if isinstance(turn_id, str) else None,
                "raw_seq": raw_seq,
                "diff": None,
            }
        elif kind == "turn_diff_updated":
            params = payload.get("params")
            diff = params.get("diff") if isinstance(params, dict) else None
            if isinstance(diff, str) and self.projection.current_turn is not None:
                self.projection.current_turn["diff"] = diff
                self.projection.current_turn["raw_seq"] = raw_seq
        runtime_store._apply_composer_message_event(
            self.projection,
            payload=payload,
            seq=int(row["seq"]),
            normalized_at=str(row.get("normalized_at") or ""),
        )
        if kind == "approval":
            request_id = runtime_store._provider_request_id(kind, payload)
            if request_id is not None and self.projection.state not in {
                LifecycleState.COMPLETED,
                LifecycleState.DEAD,
            }:
                self.projection.pending_requests[
                    runtime_store._provider_request_key(request_id)
                ] = {
                    "request_id": request_id,
                    "request_kind": str(payload.get("method") or "approval"),
                    "received_at": row["normalized_at"],
                    "raw_seq": raw_seq,
                    "payload": dict(payload),
                }
        elif kind in {"approval_resolved", "approval_cancelled", "approval_response"}:
            request_id = runtime_store._provider_request_id(kind, payload)
            if request_id is not None:
                self.projection.pending_requests.pop(
                    runtime_store._provider_request_key(request_id), None
                )

    def projection_json(self) -> dict[str, Any]:
        state = self.state
        current_turn = self.projection.current_turn or {
            "turn_id": None,
            "raw_seq": 0,
            "diff": None,
        }
        return {
            "state": self.projection.state.value,
            "unread_event_seq": self.projection.unread_event_seq,
            "last_causal_raw_seq": self.projection.last_causal_raw_seq,
            "current_turn": current_turn,
            "tasks": state.get("tasks") or [],
            "pr": state.get("pr"),
            "session_meta": state.get("session_meta") or {},
            "pending_requests": self.projection.pending_requests,
            "pending_user_messages": self.projection.pending_user_messages,
            "composer_messages": self.projection.composer_messages,
            "disposition_counts": state.get("dispositions") or {},
            "tokens": state.get("tokens"),
        }


_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        format TEXT NOT NULL,
        normalizer_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        state TEXT NOT NULL,
        archive_state TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        event_id INTEGER NOT NULL,
        raw_seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        event_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, event_id)
    );
    CREATE INDEX IF NOT EXISTS events_read_cursor
        ON events(run_id, event_id);
    CREATE INDEX IF NOT EXISTS events_raw_order
        ON events(run_id, raw_seq, event_id);
    CREATE TABLE IF NOT EXISTS patches (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        change_cursor INTEGER NOT NULL,
        event_id INTEGER NOT NULL,
        raw_seq INTEGER NOT NULL,
        patch_json TEXT NOT NULL,
        event_revision INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, change_cursor)
    );
    CREATE INDEX IF NOT EXISTS patches_event
        ON patches(run_id, event_id, change_cursor);
    CREATE TABLE IF NOT EXISTS run_cursors (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
        raw_seq INTEGER NOT NULL DEFAULT 0,
        materialized_raw_seq INTEGER NOT NULL DEFAULT 0,
        next_event_id INTEGER NOT NULL DEFAULT 0,
        event_base INTEGER NOT NULL DEFAULT 0,
        event_count INTEGER NOT NULL DEFAULT 0,
        change_cursor INTEGER NOT NULL DEFAULT 0,
        patch_base_cursor INTEGER NOT NULL DEFAULT 0,
        last_causal_raw_seq INTEGER NOT NULL DEFAULT 0,
        last_lifecycle_change TEXT,
        normalizer_version TEXT NOT NULL,
        rebuild_state TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS dispositions (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        raw_seq INTEGER NOT NULL,
        disposition TEXT NOT NULL,
        normalized_kind TEXT NOT NULL,
        normalized_json TEXT NOT NULL,
        normalizer_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, raw_seq)
    );
    CREATE TABLE IF NOT EXISTS run_projections (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
        current_turn_json TEXT NOT NULL,
        tasks_json TEXT NOT NULL,
        pr_json TEXT,
        session_meta_json TEXT NOT NULL,
        pending_requests_json TEXT NOT NULL,
        composer_messages_json TEXT NOT NULL,
        disposition_counts_json TEXT NOT NULL,
        tokens_json TEXT,
        projection_revision INTEGER NOT NULL
    );
    """,
    2: """
    ALTER TABLE run_projections
        ADD COLUMN unread_event_seq INTEGER NOT NULL DEFAULT 0;
    """,
    3: """
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
    """,
    4: """
    ALTER TABLE parity_records ADD COLUMN raw_seq INTEGER;
    """,
    5: """
    CREATE TABLE IF NOT EXISTS backfill_progress (
        normalizer_version TEXT PRIMARY KEY,
        cursor_run_id TEXT NOT NULL DEFAULT ''
    );
    """,
    6: """
    CREATE INDEX IF NOT EXISTS events_artifact_index
        ON events(kind, updated_at DESC);
    """,
    7: """
    CREATE TABLE IF NOT EXISTS child_runs (
        parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        child_id TEXT NOT NULL,
        child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
        source_path TEXT NOT NULL,
        source_size INTEGER NOT NULL DEFAULT -1,
        created_at TEXT NOT NULL,
        PRIMARY KEY (parent_run_id, child_id)
    );
    CREATE INDEX IF NOT EXISTS child_runs_parent
        ON child_runs(parent_run_id, child_id);
    """,
    8: """
    """,
}


def connect_event_db(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a configured SQLite connection for the event store."""

    db_path = Path(path)
    if read_only:
        uri = f"file:{db_path.absolute()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def migrate_event_db(path: Path | str) -> None:
    with connect_event_db(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied_versions = {
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        for version in range(1, SCHEMA_VERSION + 1):
            if version in applied_versions:
                continue
            if version == 2:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(run_projections)"
                    ).fetchall()
                }
                if "unread_event_seq" not in columns:
                    connection.execute(
                        "ALTER TABLE run_projections "
                        "ADD COLUMN unread_event_seq INTEGER NOT NULL DEFAULT 0"
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
            elif version == 8:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(child_runs)"
                    ).fetchall()
                }
                if "source_size" not in columns:
                    connection.execute(
                        "ALTER TABLE child_runs ADD COLUMN "
                        "source_size INTEGER NOT NULL DEFAULT -1"
                    )
            else:
                connection.executescript(_MIGRATIONS[version])
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (?, ?)",
                (version, "schema-v" + str(version)),
            )


class SQLiteEventStore:
    """Transactional SQLite cache for one or more materialized runs."""

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
                migrate_event_db(self.path)
                self._schema_ready = True

    @contextmanager
    def connection(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        connection = connect_event_db(self.path, read_only=read_only)
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
        """Return the process-wide lock for one database run view."""

        key = (str(self.path.absolute()), run_id)
        with _RUN_LOCKS_GUARD:
            return _RUN_LOCKS.setdefault(key, threading.RLock())

    def backfill_completed_run_ids(self, normalizer_version: str) -> set[str]:
        """Return runs whose completed marker makes them backfill-ineligible."""

        self.ensure_schema()
        with self.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT run_id FROM parity_records "
                "WHERE normalizer_version = ? AND record_type = 'backfill_completed'",
                (normalizer_version,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def backfill_cursor(self, normalizer_version: str) -> str:
        """Return the durable next-batch position for one normalizer."""

        self.ensure_schema()
        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT cursor_run_id FROM backfill_progress "
                "WHERE normalizer_version = ?",
                (normalizer_version,),
            ).fetchone()
        return str(row[0]) if row is not None else ""

    def advance_backfill_cursor(
        self,
        normalizer_version: str,
        run_id: str,
    ) -> None:
        """Durably advance the backfill scan after one examined run."""

        self.ensure_schema()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO backfill_progress(normalizer_version, cursor_run_id) "
                "VALUES (?, ?) ON CONFLICT(normalizer_version) DO UPDATE SET "
                "cursor_run_id = excluded.cursor_run_id",
                (normalizer_version, run_id),
            )

    def create_run(
        self,
        run_id: str,
        *,
        agent_id: str,
        provider: ProviderKind | str,
        created_at: str,
        state: LifecycleState | str = LifecycleState.STARTING,
        archive_state: str = "live",
        normalizer_version: str = NORMALIZER_VERSION,
    ) -> None:
        self.ensure_schema()
        provider_kind = _provider_kind(provider)
        state_value = state.value if isinstance(state, LifecycleState) else str(state)
        with self.connection() as connection:
            connection.execute("BEGIN")
            connection.execute(
                "INSERT OR IGNORE INTO runs "
                "(run_id, agent_id, provider, format, normalizer_version, "
                "created_at, state, archive_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    agent_id,
                    provider_kind.value,
                    provider_kind.value,
                    normalizer_version,
                    created_at,
                    state_value,
                    archive_state,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO run_cursors(run_id, normalizer_version, rebuild_state) "
                "VALUES (?, ?, 'ready')",
                (run_id, normalizer_version),
            )
            connection.execute(
                "INSERT OR IGNORE INTO run_projections "
                "(run_id, current_turn_json, tasks_json, session_meta_json, "
                "pending_requests_json, composer_messages_json, "
                "disposition_counts_json, unread_event_seq, projection_revision) "
                "VALUES (?, '{}', '[]', '{}', '{}', '[]', '{}', 0, 0)",
                (run_id,),
            )

    def ensure_child_run(
        self,
        *,
        parent_run_id: str,
        child_id: str,
        child_run_id: str,
        source_path: str,
        created_at: str,
        provider: ProviderKind | str = ProviderKind.CLAUDE,
    ) -> ChildRunMapping:
        """Create one child materialized run and its durable parent mapping."""

        self.ensure_schema()
        provider_kind = _provider_kind(provider)
        with self.connection() as connection:
            connection.execute("BEGIN")
            connection.execute(
                "INSERT OR IGNORE INTO runs "
                "(run_id, agent_id, provider, format, normalizer_version, "
                "created_at, state, archive_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    child_run_id,
                    f"{parent_run_id}/{child_id}",
                    provider_kind.value,
                    provider_kind.value,
                    NORMALIZER_VERSION,
                    created_at,
                    LifecycleState.STARTING.value,
                    "live",
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO run_cursors(run_id, normalizer_version, rebuild_state) "
                "VALUES (?, ?, 'ready')",
                (child_run_id, NORMALIZER_VERSION),
            )
            connection.execute(
                "INSERT OR IGNORE INTO run_projections "
                "(run_id, current_turn_json, tasks_json, session_meta_json, "
                "pending_requests_json, composer_messages_json, "
                "disposition_counts_json, unread_event_seq, projection_revision) "
                "VALUES (?, '{}', '[]', '{}', '{}', '[]', '{}', 0, 0)",
                (child_run_id,),
            )
            connection.execute(
                "INSERT INTO child_runs "
                "(parent_run_id, child_id, child_run_id, source_path, "
                "source_size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
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
                "source_size, created_at "
                "FROM child_runs WHERE parent_run_id = ? AND child_id = ?",
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

    def child_run_for(self, parent_run_id: str, child_id: str) -> ChildRunMapping | None:
        """Return the durable child mapping without changing SQLite state."""

        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT parent_run_id, child_id, child_run_id, source_path, "
                "source_size, created_at "
                "FROM child_runs WHERE parent_run_id = ? AND child_id = ?",
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
        """Publish the consumed child file size after successful materialization."""

        with self.connection() as connection:
            connection.execute(
                "UPDATE child_runs SET source_size = ? "
                "WHERE parent_run_id = ? AND child_id = ?",
                (source_size, parent_run_id, child_id),
            )

    def invalidate_child_source_fingerprint(
        self, parent_run_id: str, child_id: str
    ) -> None:
        """Hide a child view while its source is being materialized."""

        with self.connection() as connection:
            connection.execute(
                "UPDATE child_runs SET source_size = -1 "
                "WHERE parent_run_id = ? AND child_id = ?",
                (parent_run_id, child_id),
            )

    def materialize_raw_rows(
        self,
        run_id: str,
        rows: Iterator[dict[str, Any]],
        *,
        provider: ProviderKind | str,
    ) -> None:
        """Materialize new raw rows for a producer-owned child run."""

        reducer = EventReducerAdapter(provider)
        self.restore_reducer(run_id, reducer)
        materialized = self.materialized_raw_seqs(run_id)
        for raw in rows:
            raw_seq = int(raw["seq"])
            if raw_seq in materialized:
                continue
            self.materialize(run_id, raw, reducer)
            materialized.add(raw_seq)

    def has_disposition(self, run_id: str, raw_seq: int) -> bool:
        with self.connection(read_only=True) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM dispositions WHERE run_id = ? AND raw_seq = ?",
                    (run_id, raw_seq),
                ).fetchone()
                is not None
            )

    def materialized_raw_seqs(self, run_id: str) -> set[int]:
        with self.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT raw_seq FROM dispositions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        return {int(row[0]) for row in rows}

    def run_is_healthy(self, run_id: str) -> bool:
        try:
            with self.connection(read_only=True) as connection:
                migration_rows = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                migration_versions = [int(row[0]) for row in migration_rows]
                projection_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(run_projections)"
                    ).fetchall()
                }
                if migration_versions != list(range(1, SCHEMA_VERSION + 1)):
                    return False
                if "unread_event_seq" not in projection_columns:
                    return False
                run_row = connection.execute(
                    "SELECT provider, agent_id, created_at, state FROM runs "
                    "WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if run_row is None:
                    return True
                dispositions = connection.execute(
                    "SELECT raw_seq, normalized_json, normalizer_version, created_at "
                    "FROM dispositions WHERE run_id = ? ORDER BY raw_seq",
                    (run_id,),
                ).fetchall()
                for raw_seq, normalized_json, _version, _created_at in dispositions:
                    value = json.loads(normalized_json)
                    if (
                        not isinstance(value, dict)
                        or int(value["raw_seq"]) != int(raw_seq)
                        or int(value["seq"]) < 1
                    ):
                        return False
                cursor = connection.execute(
                    "SELECT raw_seq, materialized_raw_seq, next_event_id, event_base, "
                    "event_count, change_cursor, patch_base_cursor, "
                    "last_causal_raw_seq, normalizer_version, rebuild_state "
                    "FROM run_cursors WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                projection = connection.execute(
                    "SELECT current_turn_json, tasks_json, pr_json, "
                    "session_meta_json, pending_requests_json, composer_messages_json, "
                    "disposition_counts_json, tokens_json, unread_event_seq, "
                    "projection_revision FROM run_projections WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if cursor is None or projection is None:
                    return False
                for value in projection[:8]:
                    if value is not None:
                        json.loads(value)
                event_rows = connection.execute(
                    "SELECT event_id, raw_seq, event_json, revision, deleted "
                    "FROM events WHERE run_id = ? ORDER BY event_id",
                    (run_id,),
                ).fetchall()
                event_ids = {int(row[0]) for row in event_rows}
                for event_id, raw_seq, event_json, revision, deleted in event_rows:
                    value = json.loads(event_json)
                    if (
                        not isinstance(value, dict)
                        or int(value.get("id", -1)) != int(event_id)
                        or int(raw_seq) < 1
                        or int(revision) < 1
                        or int(deleted) not in {0, 1}
                    ):
                        return False
                patch_rows = connection.execute(
                    "SELECT change_cursor, event_id, raw_seq, patch_json, "
                    "event_revision FROM patches WHERE run_id = ? "
                    "ORDER BY change_cursor",
                    (run_id,),
                ).fetchall()
                for change_cursor, event_id, raw_seq, patch_json, revision in patch_rows:
                    if (
                        int(event_id) not in event_ids
                        or int(raw_seq) < 1
                        or int(change_cursor) < 1
                        or int(revision) < 1
                        or not isinstance(json.loads(patch_json), dict)
                    ):
                        return False
                expected_prefix = (
                    max((int(row[0]) for row in dispositions), default=0),
                    max((int(row[0]) for row in dispositions), default=0),
                    max(event_ids, default=0) + 1 if event_ids else 0,
                    min(event_ids, default=0),
                    len(event_rows),
                )
                if tuple(int(value) for value in cursor[:5]) != expected_prefix:
                    return False
                if int(cursor[7]) < 0 or cursor[9] != "ready":
                    return False
                if int(projection[9]) != len(dispositions):
                    return False
            expected = self._expected_view_rows(
                run_id,
                provider=str(run_row[0]),
                agent_id=str(run_row[1]),
                created_at=str(run_row[2]),
                state=str(run_row[3]),
                dispositions=dispositions,
            )
            actual = self.view_rows(run_id)
            if actual != expected:
                return False
        except (
            sqlite3.DatabaseError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return False
        return True

    def _expected_view_rows(
        self,
        run_id: str,
        *,
        provider: str,
        agent_id: str,
        created_at: str,
        state: str,
        dispositions: list[tuple[Any, ...]],
    ) -> dict[str, list[tuple[Any, ...]]]:
        with tempfile.TemporaryDirectory(prefix="wiki-282-health-") as directory:
            expected = SQLiteEventStore(Path(directory) / "events.sqlite3")
            expected.create_run(
                run_id,
                agent_id=agent_id,
                provider=provider,
                created_at=created_at,
                state=state,
            )
            reducer = EventReducerAdapter(provider)
            for raw_seq, normalized_json, _version, disposition_created_at in sorted(
                dispositions,
                key=lambda row: int(row[0]),
            ):
                normalized_row = json.loads(normalized_json)
                normalized = NormalizedProviderEvent(
                    EventDisposition.IGNORED
                    if normalized_row["disposition"] in {
                        "ignored",
                        transcripts.EVENT_DISPOSITION_IGNORED,
                    }
                    else EventDisposition(normalized_row["disposition"]),
                    str(normalized_row["kind"]),
                    normalized_row["payload"],
                    LifecycleState(normalized_row["lifecycle_state"])
                    if normalized_row.get("lifecycle_state") is not None
                    else None,
                )
                expected.materialize(
                    run_id,
                    {
                        "seq": int(raw_seq),
                        "normalized_seq": int(normalized_row["seq"]),
                        "received_at": str(disposition_created_at),
                        "provider": provider,
                        "payload": {},
                    },
                    reducer,
                    normalized=normalized,
                )
            return expected.view_rows(run_id)

    def read_normalized_row(
        self,
        run_id: str,
        raw_seq: int,
    ) -> dict[str, Any] | None:
        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT raw_seq, disposition, normalized_kind, normalized_json, "
                "created_at FROM dispositions WHERE run_id = ? AND raw_seq = ?",
                (run_id, raw_seq),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row[3])
        if not isinstance(value, dict):
            raise ValueError(f"invalid normalized row for {run_id}:{raw_seq}")
        return value

    def restore_reducer(
        self,
        run_id: str,
        reducer: EventReducerAdapter,
    ) -> None:
        self._restore_reducer_from_committed_rows(run_id, reducer)

    def reset_run(self, run_id: str) -> None:
        """Remove one materialized run before a deterministic rebuild."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    def materialize(
        self,
        run_id: str,
        raw: dict[str, Any],
        reducer: EventReducerAdapter,
        normalized: NormalizedProviderEvent | None = None,
    ) -> ReducerResult:
        with self.run_lock(run_id):
            self.ensure_schema()
            raw_seq = int(raw["seq"])
            if self.has_disposition(run_id, raw_seq):
                raise ValueError(f"raw sequence {raw_seq} is already materialized")
            try:
                with self.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    result = reducer.apply_raw(raw, normalized)
                    received_at = str(raw.get("received_at") or "")
                    normalized = result.normalized
                    connection.execute(
                        "INSERT INTO dispositions "
                        "(run_id, raw_seq, disposition, normalized_kind, normalized_json, "
                        "normalizer_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            raw_seq,
                            normalized["disposition"],
                            normalized["kind"],
                            _json_bytes(normalized),
                            reducer.normalizer_version,
                            received_at,
                        ),
                    )
                    self._persist_events(connection, run_id, result, received_at)
                    self._persist_patches(connection, run_id, result, received_at)
                    self._persist_projections(connection, run_id, reducer, result)
                    self._persist_cursor(connection, run_id, result, reducer)
            except Exception:
                self._restore_reducer_from_committed_rows(run_id, reducer)
                raise
            return result

    def _restore_reducer_from_committed_rows(
        self,
        run_id: str,
        reducer: EventReducerAdapter,
    ) -> None:
        clean = EventReducerAdapter(
            reducer.provider,
            normalizer_version=reducer.normalizer_version,
        )
        with self.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT normalized_json FROM dispositions WHERE run_id = ? "
                "ORDER BY raw_seq",
                (run_id,),
            ).fetchall()
        for (normalized_json,) in rows:
            clean.apply_normalized_row(json.loads(normalized_json))
        reducer.replace_with(clean)

    def _persist_events(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        result: ReducerResult,
        timestamp: str,
    ) -> None:
        for event in result.events:
            event_id = int(event["id"])
            event_for_storage = dict(event)
            if not event_for_storage.get("ts"):
                event_for_storage["ts"] = None
            event_json = _json_bytes(event_for_storage)
            event_created_at = event_for_storage.get("ts") or timestamp or "replay"
            existing = connection.execute(
                "SELECT event_json, revision, created_at FROM events "
                "WHERE run_id = ? AND event_id = ?",
                (run_id, event_id),
            ).fetchone()
            raw_seq = result.event_raw_seqs.get(event_id, result.raw_seq)
            if existing is None:
                connection.execute(
                    "INSERT INTO events(run_id, event_id, raw_seq, kind, event_json, "
                    "created_at, updated_at, revision, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0)",
                    (
                        run_id,
                        event_id,
                        raw_seq,
                        str(event.get("kind") or "unknown"),
                        event_json,
                        event_created_at,
                        timestamp,
                    ),
                )
            elif existing[0] != event_json:
                connection.execute(
                    "UPDATE events SET raw_seq = ?, kind = ?, event_json = ?, "
                    "updated_at = ?, revision = ?, deleted = 0 "
                    "WHERE run_id = ? AND event_id = ?",
                    (
                        raw_seq,
                        str(event.get("kind") or "unknown"),
                        event_json,
                        timestamp,
                        int(existing[1]) + 1,
                        run_id,
                        event_id,
                    ),
                )

    def _persist_patches(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        result: ReducerResult,
        timestamp: str,
    ) -> None:
        for change in result.changes:
            cursor = int(change["cursor"])
            if change.get("kind") == "patch":
                event_id = int(change["id"])
                patch = {
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
                }
            elif change.get("kind") == "tail":
                event_id = int(change["from"])
                event = next(
                    (item for item in result.events if int(item["id"]) == event_id),
                    None,
                )
                if event is None:
                    continue
                patch = {"event": event}
            else:
                continue
            revision_row = connection.execute(
                "SELECT revision FROM events WHERE run_id = ? AND event_id = ?",
                (run_id, event_id),
            ).fetchone()
            if revision_row is None:
                continue
            connection.execute(
                "INSERT INTO patches(run_id, change_cursor, event_id, raw_seq, "
                "patch_json, event_revision, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    cursor,
                    event_id,
                    result.raw_seq,
                    _json_bytes(patch),
                    int(revision_row[0]),
                    timestamp,
                ),
            )

    def _persist_projections(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        reducer: EventReducerAdapter,
        result: ReducerResult,
    ) -> None:
        projection = reducer.projection_json()
        connection.execute(
            "UPDATE run_projections SET current_turn_json = ?, tasks_json = ?, "
            "pr_json = ?, session_meta_json = ?, pending_requests_json = ?, "
            "composer_messages_json = ?, disposition_counts_json = ?, tokens_json = ?, "
            "unread_event_seq = ?, projection_revision = projection_revision + 1 "
            "WHERE run_id = ?",
            (
                _json_bytes(projection["current_turn"]),
                _json_bytes(projection["tasks"]),
                _json_bytes(projection["pr"]) if projection["pr"] is not None else None,
                _json_bytes(projection["session_meta"]),
                _json_bytes(projection["pending_requests"]),
                _json_bytes(projection["composer_messages"]),
                _json_bytes(projection["disposition_counts"]),
                _json_bytes(projection["tokens"]) if projection["tokens"] is not None else None,
                int(projection["unread_event_seq"]),
                run_id,
            ),
        )
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (projection["state"], run_id),
        )

    def _persist_cursor(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        result: ReducerResult,
        reducer: EventReducerAdapter,
    ) -> None:
        event_count, event_base, next_event_id = connection.execute(
            "SELECT COUNT(*), COALESCE(MIN(event_id), 0), "
            "COALESCE(MAX(event_id) + 1, 0) FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        change_cursor = result.change_cursor
        patch_base = connection.execute(
            "SELECT COALESCE(MIN(change_cursor), 0) FROM patches WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        last_lifecycle_change = connection.execute(
            "SELECT last_lifecycle_change FROM run_cursors WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        if result.normalized.get("lifecycle_state") is not None:
            last_lifecycle_change = _json_bytes(
                [result.raw_seq, change_cursor]
            )
        connection.execute(
            "UPDATE run_cursors SET raw_seq = ?, materialized_raw_seq = ?, "
            "next_event_id = ?, event_base = ?, event_count = ?, change_cursor = ?, "
            "patch_base_cursor = ?, last_causal_raw_seq = ?, normalizer_version = ?, "
            "last_lifecycle_change = ?, rebuild_state = 'ready' WHERE run_id = ?",
            (
                result.raw_seq,
                result.raw_seq,
                int(next_event_id),
                int(event_base),
                int(event_count),
                change_cursor,
                int(patch_base),
                reducer.projection.last_causal_raw_seq,
                reducer.normalizer_version,
                last_lifecycle_change,
                run_id,
            ),
        )

    def cursor(self, run_id: str) -> RunCursor:
        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT run_id, raw_seq, materialized_raw_seq, next_event_id, "
                "event_base, event_count, change_cursor, patch_base_cursor, "
                "last_causal_raw_seq, last_lifecycle_change, normalizer_version, "
                "rebuild_state FROM run_cursors WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        lifecycle = json.loads(row[9]) if row[9] else None
        return RunCursor(
            *row[:9], lifecycle, row[10], row[11], self.rebuild_generation(run_id)
        )

    def run_format(self, run_id: str) -> str:
        """Return the format stored for a materialized run."""

        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT format FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None or not row[0]:
            raise KeyError(run_id)
        return str(row[0])

    @staticmethod
    def _source_key(run_id: str, source_class: str, generation: int) -> str:
        return f"sqlite://{source_class}/{run_id}/rebuild-{generation}"

    def _cursor_from_connection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> RunCursor:
        row = connection.execute(
            "SELECT run_id, raw_seq, materialized_raw_seq, next_event_id, "
            "event_base, event_count, change_cursor, patch_base_cursor, "
            "last_causal_raw_seq, last_lifecycle_change, normalizer_version, "
            "rebuild_state FROM run_cursors WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        lifecycle = json.loads(row[9]) if row[9] else None
        generation = connection.execute(
            "SELECT COUNT(*) FROM parity_records WHERE run_id = ? "
            "AND record_type = 'rebuild_generation'",
            (run_id,),
        ).fetchone()[0]
        return RunCursor(
            *row[:9], lifecycle, row[10], row[11], int(generation)
        )

    def read_session_snapshot(
        self,
        run_id: str,
        *,
        source_class: str,
        after_cursor: int,
    ) -> SessionReadSnapshot:
        """Read cursor, projection, events, and patches in one transaction."""

        with self.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            state = self._cursor_from_connection(connection, run_id)
            source_key = self._source_key(
                run_id, source_class, state.rebuild_generation
            )
            projection = connection.execute(
                "SELECT current_turn_json, tasks_json, pr_json, session_meta_json, "
                "pending_requests_json, composer_messages_json, "
                "disposition_counts_json, tokens_json, unread_event_seq, "
                "projection_revision FROM run_projections WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if projection is None:
                raise KeyError(run_id)
            event_rows = connection.execute(
                "SELECT event_json FROM events WHERE run_id = ? "
                "ORDER BY event_id",
                (run_id,),
            ).fetchall()
            patch_rows = connection.execute(
                "SELECT change_cursor, event_id, raw_seq, patch_json, "
                "event_revision, created_at FROM patches "
                "WHERE run_id = ? AND change_cursor > ? ORDER BY change_cursor",
                (run_id, after_cursor),
            ).fetchall()
        patches = tuple(
            EventPatch(
                event_id=int(row[1]),
                raw_seq=int(row[2]),
                patch=json.loads(row[3]),
                event_revision=int(row[4]),
                change_cursor=int(row[0]),
                created_at=str(row[5]),
            )
            for row in patch_rows
        )
        return SessionReadSnapshot(
            state=state,
            source_key=source_key,
            projection=tuple(projection),
            events=tuple(json.loads(row[0]) for row in event_rows),
            patches=patches,
        )

    def rebuild_generation(self, run_id: str) -> int:
        """Return the durable number of atomic replacements for one run."""

        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM parity_records WHERE run_id = ? "
                "AND record_type = 'rebuild_generation'",
                (run_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def read_older_snapshot(
        self,
        run_id: str,
        *,
        source_class: str,
        before: int | None = None,
        count: int | None = None,
        before_event_id: int | None = None,
        limit: int | None = None,
    ) -> OlderReadSnapshot:
        """Read one bounded older page and its source key in one transaction."""

        if before is None:
            before = before_event_id
        if count is None:
            count = limit
        if before is None or count is None:
            raise TypeError("before and count are required")
        page_size = max(1, count)
        with self.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            state = self._cursor_from_connection(connection, run_id)
            source_key = self._source_key(
                run_id, source_class, state.rebuild_generation
            )
            rows = connection.execute(
                "SELECT event_json FROM events WHERE run_id = ? AND event_id < ? "
                "ORDER BY event_id DESC LIMIT ?",
                (run_id, max(0, before), page_size + 1),
            ).fetchall()
        has_older = len(rows) > page_size
        events = tuple(json.loads(row[0]) for row in reversed(rows[:page_size]))
        return OlderReadSnapshot(
            state=state,
            source_key=source_key,
            events=events,
            has_older=has_older,
        )

    def view_rows(self, run_id: str) -> dict[str, list[tuple[Any, ...]]]:
        """Return deterministic raw SQLite rows for replay parity tests."""

        with self.connection(read_only=True) as connection:
            return {
                "cursors": connection.execute(
                    "SELECT raw_seq, materialized_raw_seq, next_event_id, "
                    "event_base, event_count, change_cursor, patch_base_cursor, "
                    "last_causal_raw_seq, last_lifecycle_change, normalizer_version, "
                    "rebuild_state FROM run_cursors WHERE run_id = ?",
                    (run_id,),
                ).fetchall(),
                "events": connection.execute(
                    "SELECT event_id, raw_seq, kind, event_json, created_at, "
                    "updated_at, revision, deleted FROM events WHERE run_id = ? "
                    "ORDER BY event_id",
                    (run_id,),
                ).fetchall(),
                "patches": connection.execute(
                    "SELECT change_cursor, event_id, raw_seq, patch_json, "
                    "event_revision, created_at FROM patches WHERE run_id = ? "
                    "ORDER BY change_cursor",
                    (run_id,),
                ).fetchall(),
                "dispositions": connection.execute(
                    "SELECT raw_seq, disposition, normalized_kind, normalized_json, "
                    "normalizer_version, created_at FROM dispositions WHERE run_id = ? "
                    "ORDER BY raw_seq",
                    (run_id,),
                ).fetchall(),
                "projections": connection.execute(
                    "SELECT current_turn_json, tasks_json, pr_json, session_meta_json, "
                    "pending_requests_json, composer_messages_json, "
                    "disposition_counts_json, tokens_json, unread_event_seq, "
                    "projection_revision "
                    "FROM run_projections WHERE run_id = ?",
                    (run_id,),
                ).fetchall(),
            }

    def read_events(
        self,
        run_id: str,
        *,
        after_event_id: int = -1,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT event_json FROM events WHERE run_id = ? AND event_id > ? "
            "ORDER BY event_id"
        )
        parameters: tuple[Any, ...] = (run_id, after_event_id)
        if limit is not None:
            query += " LIMIT ?"
            parameters += (limit,)
        with self.connection(read_only=True) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [json.loads(row[0]) for row in rows]

    def read_normalized_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read the inspector stream from persisted normalized envelopes."""

        with self.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT normalized_json FROM dispositions WHERE run_id = ? "
                "ORDER BY raw_seq",
                (run_id,),
            ).fetchall()
        events = [json.loads(row[0]) for row in rows]
        filtered = [event for event in events if int(event.get("seq", 0)) > after_seq]
        if limit is None:
            return filtered
        return filtered[-limit:] if after_seq == 0 else filtered[:limit]

    def read_artifact_events(self) -> list[tuple[str, dict[str, Any]]]:
        """Return indexed rendered artifact events without scanning JSONL logs."""

        with self.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT run_id, event_json FROM events WHERE kind = 'artifact' "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [(str(row[0]), json.loads(row[1])) for row in rows]

    def read_patches(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int | None = None,
    ) -> list[EventPatch]:
        query = (
            "SELECT change_cursor, event_id, raw_seq, patch_json, event_revision, "
            "created_at FROM patches WHERE run_id = ? AND change_cursor > ? "
            "ORDER BY change_cursor"
        )
        parameters: tuple[Any, ...] = (run_id, after_cursor)
        if limit is not None:
            query += " LIMIT ?"
            parameters += (limit,)
        with self.connection(read_only=True) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            EventPatch(
                event_id=int(row[1]),
                raw_seq=int(row[2]),
                patch=json.loads(row[3]),
                event_revision=int(row[4]),
                change_cursor=int(row[0]),
                created_at=str(row[5]),
            )
            for row in rows
        ]

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
        """Persist one parity or backfill decision for later inspection."""

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

    def parity_records(
        self,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
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

    def export_events_jsonl(
        self,
        run_id: str,
        destination: Path | str,
        *,
        legacy_source: Path | str | None = None,
    ) -> bool:
        """Export the committed SQLite dispositions in archive JSONL order."""

        destination = Path(destination)
        if not self.path.is_file() or not self.run_is_healthy(run_id):
            return False
        cursor = self.cursor(run_id)
        if (
            cursor.normalizer_version != NORMALIZER_VERSION
            or cursor.rebuild_state != "ready"
        ):
            return False
        with self.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT raw_seq, normalized_json FROM dispositions "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        normalized_rows = [
            (int(raw_seq), json.loads(normalized_json))
            for raw_seq, normalized_json in rows
        ]
        normalized_rows.sort(
            key=lambda row: (int(row[1].get("seq", 0)), int(row[0]))
        )
        legacy_rows: dict[int, dict[str, Any]] = {}
        if legacy_source is not None:
            try:
                with Path(legacy_source).open(encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        value = json.loads(line)
                        if isinstance(value, dict):
                            legacy_rows[int(value["raw_seq"])] = value
            except (OSError, TypeError, ValueError, KeyError):
                legacy_rows = {}
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.is_symlink():
            raise OSError(f"refusing symlink archive export: {destination}")
        fd, raw_tmp = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        temporary = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for raw_seq, value in normalized_rows:
                    exported = dict(value)
                    legacy_row = legacy_rows.get(raw_seq)
                    if legacy_row is not None:
                        exported["normalized_at"] = str(
                            legacy_row.get("normalized_at") or ""
                        )
                        if "lifecycle_state" not in legacy_row:
                            exported.pop("lifecycle_state", None)
                    handle.write(_json_bytes(exported))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            destination.chmod(0o600)
            runtime_store._fsync_file(destination)
            runtime_store._fsync_directory(destination.parent)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return True

    def replace_run_from(
        self,
        source: Path | str,
        run_id: str,
    ) -> None:
        """Atomically replace one run from a validated temporary database."""

        with self.run_lock(run_id):
            self.ensure_schema()
            source_path = Path(source).absolute()
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            with self.connection() as connection:
                connection.execute("ATTACH DATABASE ? AS rebuilt", (str(source_path),))
                attached = True
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO main.runs "
                        "(run_id, agent_id, provider, format, normalizer_version, "
                        "created_at, state, archive_state) "
                        "SELECT run_id, agent_id, provider, format, normalizer_version, "
                        "created_at, state, archive_state FROM rebuilt.runs "
                        "WHERE run_id = ? AND NOT EXISTS "
                        "(SELECT 1 FROM main.runs WHERE run_id = ?)",
                        (run_id, run_id),
                    )
                    for table, columns in (
                    (
                        "run_cursors",
                        "run_id, raw_seq, materialized_raw_seq, next_event_id, "
                        "event_base, event_count, change_cursor, patch_base_cursor, "
                        "last_causal_raw_seq, last_lifecycle_change, "
                        "normalizer_version, rebuild_state",
                    ),
                    (
                        "run_projections",
                        "run_id, current_turn_json, tasks_json, pr_json, "
                        "session_meta_json, pending_requests_json, "
                        "composer_messages_json, disposition_counts_json, "
                        "tokens_json, projection_revision, unread_event_seq",
                    ),
                    (
                        "dispositions",
                        "run_id, raw_seq, disposition, normalized_kind, "
                        "normalized_json, normalizer_version, created_at",
                    ),
                    (
                        "events",
                        "run_id, event_id, raw_seq, kind, event_json, created_at, "
                        "updated_at, revision, deleted",
                    ),
                    (
                        "patches",
                        "run_id, change_cursor, event_id, raw_seq, patch_json, "
                        "event_revision, created_at",
                    ),
                    ):
                        connection.execute(
                            f"DELETE FROM main.{table} WHERE run_id = ?", (run_id,)
                        )
                        connection.execute(
                            f"INSERT INTO main.{table} ({columns}) "
                            f"SELECT {columns} FROM rebuilt.{table} WHERE run_id = ?",
                            (run_id,),
                        )
                    connection.execute(
                        "UPDATE main.runs SET agent_id = rebuilt.agent_id, "
                        "provider = rebuilt.provider, format = rebuilt.format, "
                        "normalizer_version = rebuilt.normalizer_version, "
                        "created_at = rebuilt.created_at, state = rebuilt.state, "
                        "archive_state = rebuilt.archive_state "
                        "FROM rebuilt.runs AS rebuilt WHERE main.runs.run_id = ? "
                        "AND rebuilt.run_id = ?",
                        (run_id, run_id),
                    )
                    connection.execute(
                        "INSERT INTO parity_records "
                        "(run_id, normalizer_version, record_type, path, detail_json, recorded_at) "
                        "VALUES (?, ?, 'rebuild_generation', 'replace_run_from', ?, ?)",
                        (
                            run_id,
                            NORMALIZER_VERSION,
                            _json_bytes({"source": str(source_path)}),
                            utc_now(),
                        ),
                    )
                    connection.commit()
                    connection.execute("DETACH DATABASE rebuilt")
                    attached = False
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    if attached:
                        connection.execute("DETACH DATABASE rebuilt")


def replay_raw_jsonl(
    raw_path: Path | str,
    database_path: Path | str,
    *,
    run_id: str = "replay",
    agent_id: str = "replay",
    provider: ProviderKind | str | None = None,
    created_at: str = "replay",
    normalizer_version: str = NORMALIZER_VERSION,
) -> SQLiteEventStore:
    """Replay raw JSONL rows in sequence order through the live reducer."""

    rows: list[dict[str, Any]] = []
    with Path(raw_path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("raw JSONL row must be an object")
                rows.append(row)
    rows.sort(key=lambda row: int(row["seq"]))
    if provider is None:
        if not rows:
            raise ValueError("cannot infer provider from an empty raw JSONL file")
        provider = rows[0].get("provider")
    store = SQLiteEventStore(database_path)
    store.create_run(
        run_id,
        agent_id=agent_id,
        provider=provider,
        created_at=created_at,
        normalizer_version=normalizer_version,
    )
    reducer = EventReducerAdapter(provider, normalizer_version=normalizer_version)
    for row in rows:
        store.materialize(run_id, row, reducer)
    return store


connect = connect_event_db
materialize_reducer = EventReducerAdapter
replay = replay_raw_jsonl
