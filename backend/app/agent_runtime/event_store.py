"""SQLite materialization for the provider-neutral session view.

The raw provider log remains the source of truth.  This module owns the
transactional cache used by the later read-path migrations.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import transcripts
from . import store as runtime_store
from .normalizer import NormalizedProviderEvent, normalize_provider_event
from .types import EventDisposition, LifecycleState, ProviderKind, validate_transition

SCHEMA_VERSION = 1
NORMALIZER_VERSION = "wiki-282-1"


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
class ReducerResult:
    raw_seq: int
    normalized: dict[str, Any]
    changes: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    event_raw_seqs: dict[int, int]
    state: dict[str, Any]


@dataclass
class _Projection:
    state: LifecycleState = LifecycleState.STARTING
    unread_event_seq: int = 0
    last_causal_raw_seq: int = 0
    pending_requests: dict[str, dict[str, Any]] | None = None
    composer_messages: list[dict[str, Any]] | None = None
    current_turn: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.pending_requests is None:
            self.pending_requests = {}
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

    def apply_raw(self, raw: dict[str, Any]) -> ReducerResult:
        raw_seq = int(raw["seq"])
        normalized = _normalize(raw)
        received_at = str(raw.get("received_at") or "")
        row = {
            "seq": raw_seq,
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
        return ReducerResult(
            raw_seq=raw_seq,
            normalized=row,
            changes=changes,
            events=current_events,
            event_raw_seqs=dict(self._event_raw_seqs),
            state=self.state,
        )

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
            self.projection.unread_event_seq = int(row["seq"])
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
        current = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            connection.executescript(_MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "schema-v" + str(version)),
            )


class SQLiteEventStore:
    """Transactional SQLite cache for one or more materialized runs."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        migrate_event_db(self.path)

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
                "disposition_counts_json, projection_revision) "
                "VALUES (?, '{}', '[]', '{}', '{}', '[]', '{}', 0)",
                (run_id,),
            )

    def has_disposition(self, run_id: str, raw_seq: int) -> bool:
        with self.connection(read_only=True) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM dispositions WHERE run_id = ? AND raw_seq = ?",
                    (run_id, raw_seq),
                ).fetchone()
                is not None
            )

    def materialize(
        self,
        run_id: str,
        raw: dict[str, Any],
        reducer: EventReducerAdapter,
    ) -> ReducerResult:
        raw_seq = int(raw["seq"])
        if self.has_disposition(run_id, raw_seq):
            raise ValueError(f"raw sequence {raw_seq} is already materialized")
        result = reducer.apply_raw(raw)
        received_at = str(raw.get("received_at") or "")
        normalized = result.normalized
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
            self._persist_projections(connection, run_id, reducer)
            self._persist_cursor(connection, run_id, result, reducer)
        return result

    def _persist_events(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        result: ReducerResult,
        timestamp: str,
    ) -> None:
        for event in result.events:
            event_id = int(event["id"])
            event_json = _json_bytes(event)
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
                        str(event.get("ts") or timestamp),
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
            if change.get("kind") == "tail" and int(revision_row[0]) == 1:
                # A tail change creates the row.  Patches only describe
                # changes to an existing visible event.
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
    ) -> None:
        projection = reducer.projection_json()
        connection.execute(
            "UPDATE run_projections SET current_turn_json = ?, tasks_json = ?, "
            "pr_json = ?, session_meta_json = ?, pending_requests_json = ?, "
            "composer_messages_json = ?, disposition_counts_json = ?, tokens_json = ?, "
            "projection_revision = projection_revision + 1 WHERE run_id = ?",
            (
                _json_bytes(projection["current_turn"]),
                _json_bytes(projection["tasks"]),
                _json_bytes(projection["pr"]) if projection["pr"] is not None else None,
                _json_bytes(projection["session_meta"]),
                _json_bytes(projection["pending_requests"]),
                _json_bytes(projection["composer_messages"]),
                _json_bytes(projection["disposition_counts"]),
                _json_bytes(projection["tokens"]) if projection["tokens"] is not None else None,
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
        change_cursor = int(reducer.state.get("cursor", 0))
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
        return RunCursor(*row[:9], lifecycle, row[10], row[11])

    def view_rows(self, run_id: str) -> dict[str, list[tuple[Any, ...]]]:
        """Return deterministic raw SQLite rows for replay parity tests."""

        with self.connection(read_only=True) as connection:
            return {
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
                    "disposition_counts_json, tokens_json, projection_revision "
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
