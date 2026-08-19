"""Public facade for the per-run event-store modules."""

from __future__ import annotations

import os  # noqa: F401  # kept for compatibility with existing patch targets
from pathlib import Path

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
    connect,
    connect_event_db,
    materialize_reducer,
    migrate_event_db,
    runtime_event_db_path,
    runtime_metadata_db_path,
)
from .event_store_shard import SQLiteEventStore as _SQLiteEventStore
from .event_store_shard import replay_raw_jsonl as _replay_raw_jsonl
from .types import ProviderKind

RuntimeEventStore = EventStoreRouter


def _standalone_metadata_path(path: Path | str) -> Path:
    database_path = Path(path)
    return database_path.with_name(
        f"{database_path.stem}.metadata{database_path.suffix}"
    )


class SQLiteEventStore(_SQLiteEventStore):
    """Keep the facade constructor compatible with standalone callers."""

    def __init__(
        self,
        path: Path | str,
        *,
        migrate: bool = True,
        metadata_path: Path | str | None = None,
    ) -> None:
        metadata_store = SQLiteMetadataStore(
            metadata_path
            if metadata_path is not None
            else _standalone_metadata_path(path),
            migrate=migrate,
        )
        super().__init__(path, metadata_store, migrate=migrate)


def replay_raw_jsonl(
    raw_path: Path | str,
    database_path: Path | str,
    *,
    run_id: str = "replay",
    agent_id: str = "replay",
    provider: ProviderKind | str | None = None,
    created_at: str = "replay",
    normalizer_version: str = NORMALIZER_VERSION,
) -> _SQLiteEventStore:
    """Replay raw JSONL through a standalone facade-managed store."""

    metadata_store = SQLiteMetadataStore(_standalone_metadata_path(database_path))
    return _replay_raw_jsonl(
        raw_path,
        database_path,
        metadata_store,
        run_id=run_id,
        agent_id=agent_id,
        provider=provider,
        created_at=created_at,
        normalizer_version=normalizer_version,
    )


replay = replay_raw_jsonl

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
