"""Legacy event-store migration and recovery."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import store as runtime_store
from .event_store_metadata import SQLiteMetadataStore
from .event_store_shard import (
    NORMALIZER_VERSION,
    SQLiteEventStore,
    connect_event_db,
    replay_raw_jsonl,
    runtime_event_db_path,
    runtime_metadata_db_path,
)
from .types import LifecycleState, utc_now


def _quarantine_sqlite_set(path: Path, label: str) -> None:
    """Move a SQLite database and its sidecars out of the active path."""

    stamp = utc_now()[:10].replace("-", "")
    destination = path.with_name(f"{path.name}.{label}-{stamp}")
    suffix = 1
    while destination.exists():
        destination = path.with_name(f"{path.name}.{label}-{stamp}-{suffix}")
        suffix += 1
    moved = False
    for sidecar in ("", "-wal", "-shm"):
        source = path.with_name(path.name + sidecar)
        if not source.exists():
            continue
        target = destination.with_name(destination.name + sidecar)
        os.replace(source, target)
        moved = True
    if moved:
        runtime_store._fsync_directory(path.parent)


def _rebuild_run_shards_from_raw(runtime_dir: Path) -> None:
    """Rebuild every run shard from raw JSONL after cache corruption."""

    runs_dir = runtime_dir / "runs"
    if not runs_dir.is_dir():
        return
    metadata = SQLiteMetadataStore(runtime_metadata_db_path(runtime_dir))
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        try:
            run_value = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            run_id = str(run_value.get("run_id") or run_dir.name)
            agent_id = str(run_value.get("agent_id") or run_id)
            provider = str(run_value.get("provider") or "codex")
            created_at = str(run_value.get("created_at") or utc_now())
            state = str(run_value.get("state") or LifecycleState.STARTING.value)
            target = runtime_event_db_path(runtime_dir, run_id)
            temporary = target.with_name(f".{target.name}.raw-rebuild")
            for suffix in ("", "-wal", "-shm"):
                temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
            raw_path = run_dir / "raw.jsonl"
            if raw_path.is_file() and raw_path.stat().st_size:
                replay_raw_jsonl(
                    raw_path,
                    temporary,
                    metadata,
                    run_id=run_id,
                    agent_id=agent_id,
                    provider=provider,
                    created_at=created_at,
                    normalizer_version=NORMALIZER_VERSION,
                )
            else:
                rebuilt = SQLiteEventStore(temporary, metadata)
                rebuilt.create_run(
                    run_id,
                    agent_id=agent_id,
                    provider=provider,
                    created_at=created_at,
                    state=state,
                )
            with connect_event_db(temporary) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            for suffix in ("-wal", "-shm"):
                temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
            runtime_store._fsync_file(temporary)
            runtime_store._fsync_directory(temporary.parent)
            os.replace(temporary, target)
            runtime_store._fsync_directory(target.parent)
        except (
            OSError,
            sqlite3.DatabaseError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            _quarantine_sqlite_set(
                runtime_event_db_path(runtime_dir, run_dir.name),
                "raw-rebuild-failed",
            )


def _corrupt_raw_run_ids(runtime_dir: Path) -> set[str]:
    """Return runs whose raw JSONL contains a malformed record."""

    corrupt: set[str] = set()
    runs_dir = runtime_dir / "runs"
    if not runs_dir.is_dir():
        return corrupt
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        raw_path = run_dir / "raw.jsonl"
        if not raw_path.is_file():
            continue
        try:
            with raw_path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("raw JSONL row must be an object")
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            corrupt.add(run_dir.name)
    return corrupt


def _sqlite_corruption_confirmed(path: Path, error: sqlite3.DatabaseError) -> bool:
    """Return true only when SQLite reports corruption, not an I/O failure."""

    corruption_markers = (
        "database disk image is malformed",
        "file is not a database",
        "database corruption",
        "database corrupt",
    )
    if any(marker in str(error).lower() for marker in corruption_markers):
        return True
    try:
        with connect_event_db(path, read_only=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as check_error:
        return any(
            marker in str(check_error).lower() for marker in corruption_markers
        )
    return result is not None and str(result[0]).lower() != "ok"


def _migrate_legacy_event_db(runtime_dir: Path) -> bool:
    """Shard a legacy shared database into crash-resumable run databases."""

    runtime_path = Path(runtime_dir)
    legacy_path = runtime_event_db_path(runtime_path)
    if not legacy_path.is_file():
        return False
    metadata = SQLiteMetadataStore(runtime_metadata_db_path(runtime_path))
    runtime_path.joinpath("runs").mkdir(mode=0o700, parents=True, exist_ok=True)
    with connect_event_db(legacy_path) as checkpoint:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for suffix in ("-wal", "-shm"):
        legacy_path.with_name(legacy_path.name + suffix).unlink(missing_ok=True)
    with connect_event_db(legacy_path, read_only=True) as legacy:
        run_rows = legacy.execute(
            "SELECT run_id, agent_id, provider, format, normalizer_version, "
            "created_at, state, archive_state FROM runs ORDER BY run_id"
        ).fetchall()
        live_run_ids = {
            str(row[0]) for row in run_rows if str(row[7]) == "live"
        }
        parity_rows = legacy.execute(
            "SELECT record_id, run_id, normalizer_version, record_type, path, "
            "expected_json, actual_json, detail_json, recorded_at, raw_seq "
            "FROM parity_records"
        ).fetchall()
        has_generation_table = legacy.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'rebuild_generations'"
        ).fetchone()
        legacy_generation_rows = (
            legacy.execute(
                "SELECT run_id, generation FROM rebuild_generations"
            ).fetchall()
            if has_generation_table is not None
            else []
        )
        generation_by_run_id = {
            str(row[0]): int(row[1]) for row in legacy_generation_rows
        }
        parity_generation_rows = legacy.execute(
            "SELECT run_id, COUNT(*) FROM parity_records "
            "WHERE record_type = 'rebuild_generation' GROUP BY run_id"
        ).fetchall()
        for row in parity_generation_rows:
            generation_by_run_id[str(row[0])] = max(
                generation_by_run_id.get(str(row[0]), 0), int(row[1])
            )
        schema_migration_rows = legacy.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        backfill_rows = legacy.execute(
            "SELECT normalizer_version, cursor_run_id FROM backfill_progress"
        ).fetchall()
        child_rows = legacy.execute(
            "SELECT parent_run_id, child_id, child_run_id, source_path, "
            "source_size, created_at FROM child_runs"
        ).fetchall()
        live_run_ids.update(
            str(row[2]) for row in child_rows if str(row[0]) in live_run_ids
        )
        parity_rows = [row for row in parity_rows if str(row[1]) in live_run_ids]
        child_rows = [
            row
            for row in child_rows
            if str(row[0]) in live_run_ids or str(row[2]) in live_run_ids
        ]
        generation_by_run_id = {
            run_id: generation
            for run_id, generation in generation_by_run_id.items()
            if run_id in live_run_ids
        }

        with metadata.connection() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO parity_records "
                "(record_id, run_id, normalizer_version, record_type, path, "
                "expected_json, actual_json, detail_json, recorded_at, raw_seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                parity_rows,
            )
            connection.executemany(
                "INSERT OR REPLACE INTO backfill_progress "
                "(normalizer_version, cursor_run_id) VALUES (?, ?)",
                backfill_rows,
            )
            connection.executemany(
                "INSERT OR IGNORE INTO child_runs "
                "(parent_run_id, child_id, child_run_id, source_path, "
                "source_size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                child_rows,
            )

        for run_row in run_rows:
            run_id = str(run_row[0])
            if run_id not in live_run_ids:
                continue
            target = runtime_event_db_path(runtime_path, run_id)
            if target.is_file():
                try:
                    existing = SQLiteEventStore(
                        target, metadata, migrate=False
                    )
                    if (
                        existing.run_is_healthy(run_id)
                        and existing.rebuild_generation(run_id)
                        >= generation_by_run_id.get(run_id, 0)
                    ):
                        continue
                except (OSError, sqlite3.DatabaseError, KeyError, ValueError):
                    pass
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.migration")
            for suffix in ("", "-wal", "-shm"):
                temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
            SQLiteEventStore(temporary, metadata).ensure_schema()
            with connect_event_db(temporary) as destination:
                destination.execute("ATTACH DATABASE ? AS legacy", (str(legacy_path),))
                destination.executemany(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    schema_migration_rows,
                )
                for table, columns in (
                    (
                        "runs",
                        (
                            "run_id, agent_id, provider, format, normalizer_version, "
                            "created_at, state, archive_state"
                        ),
                    ),
                    (
                        "run_cursors",
                        (
                            "run_id, raw_seq, materialized_raw_seq, next_event_id, "
                            "event_base, event_count, change_cursor, patch_base_cursor, "
                            "last_causal_raw_seq, last_lifecycle_change, "
                            "normalizer_version, rebuild_state"
                        ),
                    ),
                    (
                        "run_projections",
                        (
                            "run_id, current_turn_json, tasks_json, pr_json, "
                            "session_meta_json, pending_requests_json, "
                            "composer_messages_json, disposition_counts_json, "
                            "tokens_json, projection_revision, unread_event_seq"
                        ),
                    ),
                    (
                        "dispositions",
                        (
                            "run_id, raw_seq, disposition, normalized_kind, "
                            "normalized_json, normalizer_version, created_at"
                        ),
                    ),
                    (
                        "events",
                        (
                            "run_id, event_id, raw_seq, kind, event_json, created_at, "
                            "updated_at, revision, deleted"
                        ),
                    ),
                    (
                        "patches",
                        (
                            "run_id, change_cursor, event_id, raw_seq, patch_json, "
                            "event_revision, created_at"
                        ),
                    ),
                ):
                    destination.execute(
                        f"INSERT INTO {table} ({columns}) "
                        f"SELECT {columns} FROM legacy.{table} WHERE run_id = ?",
                        (run_id,),
                    )
                generation = generation_by_run_id.get(run_id, 0)
                destination.execute(
                    "INSERT INTO rebuild_generations(run_id, generation) "
                    "VALUES (?, ?)",
                    (run_id, generation),
                )
                destination.commit()
                destination.execute("DETACH DATABASE legacy")
                destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            for suffix in ("-wal", "-shm"):
                temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
            runtime_store._fsync_file(temporary)
            runtime_store._fsync_directory(temporary.parent)
            stale_sidecars: list[tuple[Path, Path]] = []
            for suffix in ("-wal", "-shm"):
                original = target.with_name(target.name + suffix)
                if not original.exists():
                    continue
                stale = target.with_name(f".{target.name}.stale{suffix}")
                stale.unlink(missing_ok=True)
                os.replace(original, stale)
                stale_sidecars.append((original, stale))
            runtime_store._fsync_directory(target.parent)
            try:
                os.replace(temporary, target)
                runtime_store._fsync_directory(target.parent)
            except BaseException:
                for original, stale in reversed(stale_sidecars):
                    if stale.exists():
                        os.replace(stale, original)
                runtime_store._fsync_directory(target.parent)
                raise
            for _original, stale in stale_sidecars:
                stale.unlink(missing_ok=True)
            runtime_store._fsync_directory(target.parent)

    legacy_target = legacy_path.with_name(
        f"events.sqlite3.legacy-{utc_now()[:10].replace('-', '')}"
    )
    suffix = 1
    while legacy_target.exists():
        legacy_target = legacy_path.with_name(
            f"events.sqlite3.legacy-{utc_now()[:10].replace('-', '')}-{suffix}"
        )
        suffix += 1
    for sidecar in ("", "-wal", "-shm"):
        source = legacy_path.with_name(legacy_path.name + sidecar)
        if source.exists():
            os.replace(
                source,
                legacy_target.with_name(legacy_target.name + sidecar),
            )
    runtime_store._fsync_directory(runtime_path)
    return True


def migrate_legacy_event_db(runtime_dir: Path | str) -> bool:
    """Shard legacy SQLite, falling back to raw logs after corruption."""

    runtime_path = Path(runtime_dir)
    legacy_path = runtime_event_db_path(runtime_path)
    if not legacy_path.is_file():
        for suffix in ("-wal", "-shm"):
            legacy_path.with_name(legacy_path.name + suffix).unlink(missing_ok=True)
        return False
    try:
        return _migrate_legacy_event_db(runtime_path)
    except sqlite3.DatabaseError as error:
        if not _sqlite_corruption_confirmed(legacy_path, error):
            raise
        _quarantine_sqlite_set(legacy_path, "corrupt")
        _rebuild_run_shards_from_raw(runtime_path)
        return True


class EventStoreMigration:
    """Namespace for legacy event-store migration operations."""

    migrate = staticmethod(migrate_legacy_event_db)
    migrate_legacy = staticmethod(_migrate_legacy_event_db)
    rebuild_from_raw = staticmethod(_rebuild_run_shards_from_raw)
