"""Public facade for the per-run event-store modules."""

from __future__ import annotations

import os  # noqa: F401  # kept for compatibility with existing patch targets

from .event_store_metadata import (
    SQLiteMetadataStore,
    migrate_metadata_db,
)
from .event_store_migration import (
    EventStoreMigration,
    _corrupt_raw_run_ids,  # noqa: F401  # compatibility re-export
    _migrate_legacy_event_db,  # noqa: F401  # compatibility re-export
    _quarantine_sqlite_set,  # noqa: F401  # compatibility re-export
    _rebuild_run_shards_from_raw,  # noqa: F401  # compatibility re-export
    migrate_legacy_event_db,
)
from .event_store_router import EventStoreRouter
from .event_store_shard import (
    NORMALIZER_VERSION,
    SCHEMA_VERSION,
    ChildRunMapping,
    EventPatch,
    EventReducerAdapter,
    OlderReadSnapshot,
    ReducerResult,
    RunCursor,
    SessionReadSnapshot,
    SQLiteEventStore,
    connect,
    connect_event_db,
    materialize_reducer,
    migrate_event_db,
    replay,
    replay_raw_jsonl,
    runtime_event_db_path,
    runtime_metadata_db_path,
)

RuntimeEventStore = EventStoreRouter

__all__ = [
    "NORMALIZER_VERSION",
    "SCHEMA_VERSION",
    "ChildRunMapping",
    "EventPatch",
    "EventReducerAdapter",
    "EventStoreMigration",
    "EventStoreRouter",
    "OlderReadSnapshot",
    "ReducerResult",
    "RunCursor",
    "RuntimeEventStore",
    "SQLiteEventStore",
    "SQLiteMetadataStore",
    "SessionReadSnapshot",
    "connect",
    "connect_event_db",
    "materialize_reducer",
    "migrate_event_db",
    "migrate_legacy_event_db",
    "migrate_metadata_db",
    "replay",
    "replay_raw_jsonl",
    "runtime_event_db_path",
    "runtime_metadata_db_path",
]
