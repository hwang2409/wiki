from __future__ import annotations

import ast
import errno
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

from backend.app.agent_runtime.event_store import (
    SCHEMA_VERSION,
    EventReducerAdapter,
    RuntimeEventStore,
    SQLiteEventStore,
    _migrate_legacy_event_db,
    migrate_event_db,
    migrate_legacy_event_db,
    replay_raw_jsonl,
    runtime_event_db_path,
)
from backend.app.agent_runtime.normalizer import NormalizedProviderEvent
from backend.app.agent_runtime.types import EventDisposition


@pytest.mark.parametrize(
    "module",
    (
        "event_store_shard",
        "event_store_metadata",
        "event_store_migration",
        "event_store_router",
        "event_store",
    ),
)
def test_event_store_modules_import_independently(module: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import backend.app.agent_runtime.{module}",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_event_store_module_dependency_graph_is_acyclic() -> None:
    modules = {
        "event_store_shard",
        "event_store_metadata",
        "event_store_migration",
        "event_store_router",
        "event_store",
    }
    dependencies: dict[str, set[str]] = {}
    source_root = Path(__file__).resolve().parents[1] / "app" / "agent_runtime"
    for module in modules:
        tree = ast.parse(
            (source_root / f"{module}.py").read_text(encoding="utf-8"),
            filename=module,
        )
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                imported_module = node.module
                if imported_module in modules:
                    found.add(imported_module)
            elif isinstance(node, ast.Import):
                found.update(
                    alias.name.rsplit(".", 1)[-1]
                    for alias in node.names
                    if alias.name.rsplit(".", 1)[-1] in modules
                )
        dependencies[module] = found

    assert not dependencies["event_store_shard"] & (modules - {"event_store_shard"})
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str) -> None:
        if module in visiting:
            raise AssertionError(f"event-store import cycle includes {module}")
        if module in visited:
            return
        visiting.add(module)
        for dependency in dependencies[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in modules:
        visit(module)


def test_router_migrate_false_does_not_scan_raw_logs() -> None:
    with TemporaryDirectory() as tmp, mock.patch(
        "backend.app.agent_runtime.event_store_router._corrupt_raw_run_ids"
    ) as scan:
        router = RuntimeEventStore(tmp, migrate=False)

    scan.assert_not_called()
    assert router.corrupt_raw_run_ids == set()


def _raw(seq: int, method: str, params: dict, *, received_at: str) -> dict:
    return {
        "seq": seq,
        "received_at": received_at,
        "provider": "codex",
        "direction": "server",
        "payload": {"method": method, "params": params},
    }


def _tool_rows() -> list[dict]:
    return [
        _raw(
            2,
            "item/completed",
            {
                "item": {
                    "type": "commandExecution",
                    "id": "command-1",
                    "command": "printf done",
                    "status": "completed",
                    "result": {"output": "done", "exitCode": 0},
                }
            },
            received_at="2026-08-13T00:00:02Z",
        ),
        _raw(
            1,
            "item/started",
            {
                "item": {
                    "type": "commandExecution",
                    "id": "command-1",
                    "command": "printf done",
                }
            },
            received_at="2026-08-13T00:00:01Z",
        ),
    ]


def test_sqlite_schema_uses_wal_and_foreign_keys() -> None:
    with TemporaryDirectory() as tmp:
        store = SQLiteEventStore(Path(tmp) / "events.sqlite3")
        with store.connection(read_only=True) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        store.create_run(
            "run-1",
            agent_id="WIKI-282",
            provider="codex",
            created_at="2026-08-13T00:00:00Z",
        )
        assert store.cursor("run-1").rebuild_state == "ready"


def test_replay_is_byte_identical_for_the_same_raw_input() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        raw_path = root / "raw.jsonl"
        raw_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in _tool_rows()),
            encoding="utf-8",
        )
        first = replay_raw_jsonl(raw_path, root / "first.sqlite3", run_id="run-1")
        second = replay_raw_jsonl(raw_path, root / "second.sqlite3", run_id="run-1")
        assert first.view_rows("run-1") == second.view_rows("run-1")


def test_replay_orders_rows_before_reducing_and_keeps_tool_id() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        raw_path = root / "raw.jsonl"
        raw_path.write_text(
            "".join(json.dumps(row) + "\n" for row in _tool_rows()),
            encoding="utf-8",
        )
        store = replay_raw_jsonl(raw_path, root / "events.sqlite3", run_id="run-1")
        rows = store.view_rows("run-1")["events"]
        assert [row[1] for row in rows] == [2]
        event = json.loads(rows[0][3])
        assert event["id"] == 0
        assert event["tool"]["call_id"] == "command-1"
        assert store.cursor("run-1").change_cursor >= 2


def test_replay_ordering_preserves_causal_lifecycle_state() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        raw_path = root / "raw.jsonl"
        rows = [
            _raw(
                2,
                "turn/completed",
                {"turn": {"id": "turn-1", "status": "completed"}},
                received_at="2026-08-13T00:00:02Z",
            ),
            _raw(
                1,
                "turn/started",
                {"turn": {"id": "turn-1"}},
                received_at="2026-08-13T00:00:01Z",
            ),
        ]
        raw_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        store = replay_raw_jsonl(raw_path, root / "events.sqlite3", run_id="run-1")
        with store.connection(read_only=True) as connection:
            state, current_turn = connection.execute(
                "SELECT runs.state, run_projections.current_turn_json "
                "FROM runs JOIN run_projections USING (run_id) WHERE runs.run_id = ?",
                ("run-1",),
            ).fetchone()
        assert state == "idle"
        assert json.loads(current_turn)["turn_id"] == "turn-1"


def test_live_adapter_preserves_turn_ordering_and_updates_projection() -> None:
    with TemporaryDirectory() as tmp:
        store = SQLiteEventStore(Path(tmp) / "events.sqlite3")
        store.create_run(
            "run-1",
            agent_id="WIKI-282",
            provider="codex",
            created_at="2026-08-13T00:00:00Z",
        )
        reducer = EventReducerAdapter("codex")
        store.materialize(
            "run-1",
            _raw(
                1,
                "turn/started",
                {"turn": {"id": "turn-1"}},
                received_at="2026-08-13T00:00:01Z",
            ),
            reducer,
        )
        store.materialize(
            "run-1",
            _raw(
                2,
                "turn/diff/updated",
                {"diff": "diff-1"},
                received_at="2026-08-13T00:00:02Z",
            ),
            reducer,
        )
        with store.connection(read_only=True) as connection:
            projection = connection.execute(
                "SELECT current_turn_json FROM run_projections WHERE run_id = ?",
                ("run-1",),
            ).fetchone()[0]
        assert json.loads(projection) == {
            "diff": "diff-1",
            "raw_seq": 2,
            "turn_id": "turn-1",
        }


def test_projection_only_turn_diff_advances_change_cursor() -> None:
    with TemporaryDirectory() as tmp:
        store = SQLiteEventStore(Path(tmp) / "events.sqlite3")
        store.create_run(
            "run-1",
            agent_id="WIKI-282",
            provider="codex",
            created_at="2026-08-13T00:00:00Z",
        )
        reducer = EventReducerAdapter("codex")
        store.materialize(
            "run-1",
            _raw(
                1,
                "turn/started",
                {"turn": {"id": "turn-1"}},
                received_at="2026-08-13T00:00:01Z",
            ),
            reducer,
        )
        before = store.cursor("run-1").change_cursor
        result = store.materialize(
            "run-1",
            _raw(
                2,
                "turn/diff/updated",
                {"diff": "diff-2"},
                received_at="2026-08-13T00:00:02Z",
            ),
            reducer,
        )
        assert result.projection_changed
        assert result.change_cursor > before
        assert store.cursor("run-1").change_cursor == result.change_cursor


def test_projection_stores_unread_and_composer_messages() -> None:
    with TemporaryDirectory() as tmp:
        store = SQLiteEventStore(Path(tmp) / "events.sqlite3")
        store.create_run(
            "run-1",
            agent_id="WIKI-282",
            provider="codex",
            created_at="2026-08-13T00:00:00Z",
        )
        reducer = EventReducerAdapter("codex")
        composer_row = _raw(
            1,
            "warning",
            {"message": "worker output"},
            received_at="2026-08-13T00:00:01Z",
        )
        composer_row["payload"].update(
            {
                "pending_id": "pending-1",
                "composer_text": "hello",
                "composer_sent_at": "2026-08-13T00:00:00Z",
            }
        )
        store.materialize("run-1", composer_row, reducer)
        with store.connection(read_only=True) as connection:
            unread, composer_json = connection.execute(
                "SELECT unread_event_seq, composer_messages_json "
                "FROM run_projections WHERE run_id = ?",
                ("run-1",),
            ).fetchone()
        assert unread == 1
        assert json.loads(composer_json) == [
            {
                "pending_id": "pending-1",
                "text": "hello",
                "sent_at": "2026-08-13T00:00:00Z",
                "echoed_at": "2026-08-13T00:00:01Z",
                "seq": 1,
            }
        ]


def test_failed_write_restores_reducer_before_continuing() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        rows = sorted(_tool_rows(), key=lambda row: int(row["seq"]))
        clean = SQLiteEventStore(root / "clean.sqlite3")
        clean.create_run(
            "run-1",
            agent_id="WIKI-282",
            provider="codex",
            created_at="2026-08-13T00:00:00Z",
        )
        clean_reducer = EventReducerAdapter("codex")
        for row in rows:
            clean.materialize("run-1", row, clean_reducer)

        store = SQLiteEventStore(root / "failed.sqlite3")
        store.create_run(
            "run-1",
            agent_id="WIKI-282",
            provider="codex",
            created_at="2026-08-13T00:00:00Z",
        )
        reducer = EventReducerAdapter("codex")
        store.materialize("run-1", rows[0], reducer)
        persist_projections = store._persist_projections

        def fail_after_projection(*args: object) -> None:
            persist_projections(*args)
            raise RuntimeError("injected SQLite write failure")

        with (
            mock.patch.object(
                store,
                "_persist_projections",
                side_effect=fail_after_projection,
            ),
            mock.patch.object(store, "_persist_cursor") as persist_cursor,
        ):
            try:
                store.materialize("run-1", rows[1], reducer)
            except RuntimeError as exc:
                assert str(exc) == "injected SQLite write failure"
            else:
                raise AssertionError("injected failure did not raise")
            persist_cursor.assert_not_called()

        store.materialize("run-1", rows[1], reducer)
        assert store.view_rows("run-1") == clean.view_rows("run-1")


@pytest.mark.parametrize(
    ("table", "predicate"),
    [
        ("events", "event_id = 0"),
        ("patches", "change_cursor = 2"),
        ("run_projections", "run_id = 'run-1'"),
        ("run_cursors", "run_id = 'run-1'"),
    ],
)
def test_health_check_covers_every_derived_table(table: str, predicate: str) -> None:
    with TemporaryDirectory() as tmp:
        store = SQLiteEventStore(Path(tmp) / "events.sqlite3")
        store.create_run(
            "run-1",
            agent_id="WIKI-282",
            provider="codex",
            created_at="2026-08-13T00:00:00Z",
        )
        reducer = EventReducerAdapter("codex")
        for row in sorted(_tool_rows(), key=lambda item: int(item["seq"])):
            store.materialize("run-1", row, reducer)
        assert store.run_is_healthy("run-1")
        with store.connection() as connection:
            connection.execute(f"DELETE FROM {table} WHERE {predicate}")
        assert not store.run_is_healthy("run-1")


def _user_events(store: SQLiteEventStore, run_id: str) -> list[dict]:
    return [
        event
        for event in (
            json.loads(row[3]) for row in store.view_rows(run_id)["events"]
        )
        if event.get("kind") == "user"
    ]


def test_claude_stdin_echo_does_not_duplicate_replayed_user_event() -> None:
    """WIKI-337: the supervisor's stdin write is echoed as an ignored
    claude_client_message row, then the CLI replays the same message on
    stdout (--replay-user-messages). Only the replay may render."""

    with TemporaryDirectory() as tmp:
        store = SQLiteEventStore(Path(tmp) / "events.sqlite3")
        store.create_run(
            "run-1",
            agent_id="WIKI-337",
            provider="claude",
            created_at="2026-08-18T00:00:00Z",
        )
        reducer = EventReducerAdapter("claude")
        message = {
            "role": "user",
            "content": [{"type": "text", "text": "steer: tighten scope"}],
        }
        store.materialize(
            "run-1",
            {
                "seq": 1,
                "received_at": "2026-08-18T00:00:01Z",
                "provider": "claude",
                "direction": "stdin",
                "payload": {"type": "user", "session_id": "", "message": message},
            },
            reducer,
        )
        store.materialize(
            "run-1",
            {
                "seq": 2,
                "received_at": "2026-08-18T00:00:02Z",
                "provider": "claude",
                "direction": "stdout",
                "payload": {"type": "user", "message": message},
            },
            reducer,
        )
        user_events = _user_events(store, "run-1")
        assert len(user_events) == 1
        assert user_events[0]["text"] == "steer: tighten scope"
        # The survivor is the stdout replay, not the stdin echo.
        assert user_events[0]["ts"] == "2026-08-18T00:00:02Z"
        assert store.run_is_healthy("run-1")


def test_codex_client_request_rows_never_render_events() -> None:
    with TemporaryDirectory() as tmp:
        store = SQLiteEventStore(Path(tmp) / "events.sqlite3")
        store.create_run(
            "run-1",
            agent_id="WIKI-337",
            provider="codex",
            created_at="2026-08-18T00:00:00Z",
        )
        reducer = EventReducerAdapter("codex")
        store.materialize(
            "run-1",
            {
                "seq": 1,
                "received_at": "2026-08-18T00:00:01Z",
                "provider": "codex",
                "direction": "client",
                "payload": {
                    "id": 7,
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "userMessage",
                            "id": "client-item",
                            "content": [{"type": "text", "text": "client echo"}],
                        },
                    },
                },
            },
            reducer,
        )
        store.materialize(
            "run-1",
            {
                "seq": 2,
                "received_at": "2026-08-18T00:00:02Z",
                "provider": "codex",
                "direction": "server",
                "payload": {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "userMessage",
                            "id": "item-1",
                            "content": [{"type": "text", "text": "hello codex"}],
                        }
                    },
                },
            },
            reducer,
        )
        user_events = _user_events(store, "run-1")
        assert len(user_events) == 1
        assert user_events[0]["text"] == "hello codex"
        dispositions = [
            row[1] for row in store.view_rows("run-1")["dispositions"]
        ]
        assert dispositions.count("intentionally_ignored") == 1
        assert dispositions.count("rendered") == 1
        assert store.run_is_healthy("run-1")


def test_half_applied_migration_is_idempotent() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "events.sqlite3"
        store = SQLiteEventStore(path)
        with store.connection() as connection:
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = 2"
            )
        migrate_event_db(path)
        with store.connection(read_only=True) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(run_projections)"
                ).fetchall()
            }
        assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
        assert "unread_event_seq" in columns


def test_runtime_event_store_isolates_runs_in_separate_files() -> None:
    with TemporaryDirectory() as tmp:
        runtime = RuntimeEventStore(Path(tmp))
        for run_id in ("run-a", "run-b"):
            runtime.create_run(
                run_id,
                agent_id=run_id,
                provider="codex",
                created_at="2026-08-18T00:00:00Z",
            )

        assert runtime_event_db_path(tmp, "run-a").is_file()
        assert runtime_event_db_path(tmp, "run-b").is_file()
        assert runtime_event_db_path(tmp, "run-a") != runtime_event_db_path(
            tmp, "run-b"
        )

        runtime.record_parity_record(
            "run-a",
            normalizer_version="test",
            record_type="test",
            path="test",
            detail={},
        )
        assert runtime.parity_records("run-a")[0]["run_id"] == "run-a"
        assert runtime.parity_records("run-b") == []


def test_runtime_event_store_migrates_shared_database_and_resumes_partial_shard() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        for run_id in ("run-a", "run-b"):
            legacy.create_run(
                run_id,
                agent_id=run_id,
                provider="codex",
                created_at="2026-08-18T00:00:00Z",
            )
        partial = runtime_event_db_path(runtime_path, "run-a")
        partial.parent.mkdir(parents=True)
        partial.touch()

        runtime = RuntimeEventStore(runtime_path)

        assert not runtime_event_db_path(runtime_path).exists()
        assert runtime.cursor("run-a").run_id == "run-a"
        assert runtime.cursor("run-b").run_id == "run-b"
        assert list(runtime_path.glob("events.sqlite3.legacy-*"))

        reopened = RuntimeEventStore(runtime_path)
        assert reopened.cursor("run-a").run_id == "run-a"


def test_legacy_migration_skips_archived_runs() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        legacy.create_run(
            "live",
            agent_id="live",
            provider="codex",
            created_at="now",
        )
        legacy.create_run(
            "archived",
            agent_id="archived",
            provider="codex",
            created_at="now",
            archive_state="archived",
        )
        with legacy.connection() as connection:
            connection.execute(
                "INSERT INTO parity_records(run_id, normalizer_version, "
                "record_type, path, detail_json, recorded_at) "
                "VALUES ('archived', 'test', 'archived', 'migration', '{}', 'now')"
            )

        runtime = RuntimeEventStore(runtime_path)

        assert runtime_event_db_path(runtime_path, "live").is_file()
        assert not runtime_event_db_path(runtime_path, "archived").exists()
        with runtime.metadata_store.connection(read_only=True) as connection:
            assert connection.execute(
                "SELECT run_id FROM parity_records WHERE run_id = 'archived'"
            ).fetchall() == []


def test_runtime_event_store_failure_is_limited_to_one_run() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        runtime = RuntimeEventStore(runtime_path)
        for run_id in ("run-a", "run-b"):
            runtime.create_run(
                run_id,
                agent_id=run_id,
                provider="codex",
                created_at="2026-08-18T00:00:00Z",
            )

        runtime_event_db_path(runtime_path, "run-a").write_bytes(b"")
        normalized = NormalizedProviderEvent(
            EventDisposition.UNKNOWN,
            "unknown",
            {},
            None,
        )
        with pytest.raises(sqlite3.DatabaseError):
            runtime.materialize(
                "run-a",
                {
                    "seq": 1,
                    "received_at": "now",
                    "provider": "codex",
                    "payload": {},
                },
                EventReducerAdapter("codex"),
                normalized=normalized,
            )
        runtime.materialize(
            "run-b",
            {
                "seq": 1,
                "received_at": "now",
                "provider": "codex",
                "payload": {},
            },
            EventReducerAdapter("codex"),
            normalized=normalized,
        )
        assert runtime.cursor("run-b").raw_seq == 1


def test_legacy_migration_reopens_after_mid_shard_crash_and_copies_all_tables() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        for run_id in ("run-a", "run-b"):
            legacy.create_run(
                run_id,
                agent_id=run_id,
                provider="codex",
                created_at="2026-08-18T00:00:00Z",
            )
        legacy.materialize(
            "run-a",
            _raw(
                1,
                "item/completed",
                {
                    "item": {
                        "type": "userMessage",
                        "id": "user-1",
                        "content": [{"type": "text", "text": "hello"}],
                    }
                },
                received_at="now",
            ),
            EventReducerAdapter("codex"),
        )
        with legacy.connection() as connection:
            connection.execute(
                "INSERT INTO backfill_progress(normalizer_version, cursor_run_id) "
                "VALUES ('test', 'run-a')"
            )
            connection.execute(
                "INSERT INTO child_runs(parent_run_id, child_id, child_run_id, "
                "source_path, source_size, created_at) VALUES "
                "('run-a', 'child', 'run-b', 'child.jsonl', 1, 'now')"
            )
            connection.execute(
                "INSERT INTO parity_records(run_id, normalizer_version, record_type, "
                "path, detail_json, recorded_at) VALUES "
                "('run-a', 'test', 'test', 'migration', '{}', 'now')"
            )
            connection.executemany(
                "INSERT INTO parity_records(run_id, normalizer_version, record_type, "
                "path, detail_json, recorded_at) VALUES "
                "('run-a', 'test', 'rebuild_generation', 'migration', '{}', ?)",
                [("generation-1",), ("generation-2",)],
            )
        with legacy.connection(read_only=True) as connection:
            expected_shard_rows = {
                table: connection.execute(
                    f"SELECT * FROM {table} WHERE run_id = ?",
                    ("run-a",),
                ).fetchall()
                for table in (
                    "runs",
                    "events",
                    "patches",
                    "run_cursors",
                    "run_projections",
                    "dispositions",
                )
            }
            expected_schema_migrations = connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            expected_parity = connection.execute(
                "SELECT * FROM parity_records ORDER BY record_id"
            ).fetchall()
            expected_backfill = connection.execute(
                "SELECT * FROM backfill_progress ORDER BY normalizer_version"
            ).fetchall()
            expected_children = connection.execute(
                "SELECT * FROM child_runs ORDER BY parent_run_id, child_id"
            ).fetchall()
        original_replace = os.replace
        failed = False

        def fail_mid_shard(
            source: str | os.PathLike[str],
            destination: str | os.PathLike[str],
        ) -> None:
            nonlocal failed
            if (
                not failed
                and Path(source).name == ".events.sqlite3.migration"
                and Path(destination).parent.name == "run-b"
            ):
                failed = True
                raise OSError("crash during shard rename")
            original_replace(source, destination)

        with mock.patch(
            "backend.app.agent_runtime.event_store.os.replace",
            side_effect=fail_mid_shard,
        ):
            with pytest.raises(OSError, match="crash during shard rename"):
                _migrate_legacy_event_db(runtime_path)
        assert runtime_event_db_path(runtime_path).is_file()
        assert _migrate_legacy_event_db(runtime_path)
        reopened = RuntimeEventStore(runtime_path)

        for run_id in ("run-a", "run-b"):
            path = runtime_event_db_path(runtime_path, run_id)
            assert SQLiteEventStore(path, migrate=False).run_is_healthy(run_id)
            with sqlite3.connect(path) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            assert {
                "runs",
                "events",
                "patches",
                "dispositions",
                "run_cursors",
                "run_projections",
                "parity_records",
                "backfill_progress",
                "child_runs",
                "rebuild_generations",
            } <= tables
        assert not runtime_event_db_path(runtime_path).exists()
        assert reopened.rebuild_generation("run-a") == 2
        with reopened.for_run("run-a").connection(read_only=True) as connection:
            for table, expected in expected_shard_rows.items():
                assert connection.execute(
                    f"SELECT * FROM {table} WHERE run_id = ?",
                    ("run-a",),
                ).fetchall() == expected
            assert connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall() == expected_schema_migrations
        with reopened.metadata_store.connection(read_only=True) as connection:
            assert connection.execute(
                "SELECT * FROM parity_records ORDER BY record_id"
            ).fetchall() == expected_parity
            assert connection.execute(
                "SELECT * FROM backfill_progress ORDER BY normalizer_version"
            ).fetchall() == expected_backfill
            assert connection.execute(
                "SELECT * FROM child_runs ORDER BY parent_run_id, child_id"
            ).fetchall() == expected_children


def test_legacy_migration_moves_stale_destination_sidecars_before_swap() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        legacy.create_run(
            "run-a",
            agent_id="run-a",
            provider="codex",
            created_at="now",
        )
        target = runtime_event_db_path(runtime_path, "run-a")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_name(target.name + "-wal").write_bytes(b"stale wal")
        target.with_name(target.name + "-shm").write_bytes(b"stale shm")

        assert _migrate_legacy_event_db(runtime_path)
        assert not target.with_name(target.name + "-wal").exists()
        assert not target.with_name(target.name + "-shm").exists()

        reopened = SQLiteEventStore(target, migrate=False)
        assert reopened.run_is_healthy("run-a")
        wal_path = target.with_name(target.name + "-wal")
        assert not wal_path.exists() or wal_path.stat().st_size == 0
        assert not list(target.parent.glob(f".{target.name}.stale*"))


def test_legacy_schema_without_generation_table_preserves_metadata() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        for run_id in ("parent", "child"):
            legacy.create_run(
                run_id,
                agent_id=run_id,
                provider="codex",
                created_at="now",
            )
        legacy.materialize(
            "parent",
            _raw(1, "warning", {"message": "parent"}, received_at="now"),
            EventReducerAdapter("codex"),
        )
        with legacy.connection() as connection:
            connection.executemany(
                "INSERT INTO parity_records "
                "(run_id, normalizer_version, record_type, path, detail_json, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("parent", "test", "rebuild_generation", "migration", "{}", "one"),
                    ("parent", "test", "rebuild_generation", "migration", "{}", "two"),
                ],
            )
            connection.execute(
                "INSERT INTO backfill_progress(normalizer_version, cursor_run_id) "
                "VALUES ('test', 'parent')"
            )
            connection.execute(
                "INSERT INTO child_runs(parent_run_id, child_id, child_run_id, "
                "source_path, source_size, created_at) VALUES "
                "('parent', 'child', 'child', 'child.jsonl', 7, 'now')"
            )
            expected_parity = connection.execute(
                "SELECT * FROM parity_records ORDER BY record_id"
            ).fetchall()
            expected_backfill = connection.execute(
                "SELECT * FROM backfill_progress ORDER BY normalizer_version"
            ).fetchall()
            expected_children = connection.execute(
                "SELECT * FROM child_runs ORDER BY parent_run_id, child_id"
            ).fetchall()
            connection.execute("DROP TABLE rebuild_generations")

        assert _migrate_legacy_event_db(runtime_path)
        runtime = RuntimeEventStore(runtime_path)
        assert runtime.rebuild_generation("parent") == 2
        assert runtime.cursor("parent").raw_seq == 1
        with runtime.metadata_store.connection(read_only=True) as connection:
            assert connection.execute(
                "SELECT * FROM parity_records ORDER BY record_id"
            ).fetchall() == expected_parity
            assert connection.execute(
                "SELECT * FROM backfill_progress ORDER BY normalizer_version"
            ).fetchall() == expected_backfill
            assert connection.execute(
                "SELECT * FROM child_runs ORDER BY parent_run_id, child_id"
            ).fetchall() == expected_children


def test_legacy_migration_keeps_source_after_enospc_swap_failure() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        for run_id in ("run-a", "run-b"):
            legacy.create_run(
                run_id,
                agent_id=run_id,
                provider="codex",
                created_at="now",
            )
            legacy.materialize(
                run_id,
                _raw(1, "warning", {"message": run_id}, received_at="now"),
                EventReducerAdapter("codex"),
            )
        original_replace = os.replace

        def fail_second_swap(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if (
                Path(source).name == ".events.sqlite3.migration"
                and Path(destination).parent.name == "run-b"
            ):
                raise OSError(errno.ENOSPC, "no space left on device")
            original_replace(source, destination)

        with mock.patch(
            "backend.app.agent_runtime.event_store.os.replace",
            side_effect=fail_second_swap,
        ), pytest.raises(OSError) as raised:
            migrate_legacy_event_db(runtime_path)
        assert raised.value.errno == errno.ENOSPC
        legacy_path = runtime_event_db_path(runtime_path)
        assert legacy_path.is_file()
        with sqlite3.connect(legacy_path) as connection:
            assert connection.execute(
                "SELECT run_id FROM runs ORDER BY run_id"
            ).fetchall() == [("run-a",), ("run-b",)]
        assert not list(runtime_path.glob("events.sqlite3.corrupt-*"))
        assert SQLiteEventStore(
            runtime_event_db_path(runtime_path), migrate=False
        ).run_is_healthy("run-a")
        assert SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-a"), migrate=False
        ).run_is_healthy("run-a")
        assert not runtime_event_db_path(runtime_path, "run-b").is_file()

        assert _migrate_legacy_event_db(runtime_path)
        assert SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-b"), migrate=False
        ).run_is_healthy("run-b")


def test_schema_only_shard_does_not_skip_legacy_run() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        legacy.create_run(
            "run-a",
            agent_id="run-a",
            provider="codex",
            created_at="now",
        )
        legacy.materialize(
            "run-a",
            _raw(1, "warning", {"message": "legacy"}, received_at="now"),
            EventReducerAdapter("codex"),
        )
        partial = SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-a"),
            migrate=True,
        )
        assert not partial.run_is_healthy("run-a")

        assert _migrate_legacy_event_db(runtime_path)
        migrated = SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-a"),
            migrate=False,
        )
        assert migrated.run_is_healthy("run-a")
        assert migrated.cursor("run-a").raw_seq == 1


def test_cursor_snapshot_reads_generation_from_supplied_connection() -> None:
    with TemporaryDirectory() as tmp:
        store = SQLiteEventStore(Path(tmp) / "events.sqlite3")
        store.create_run(
            "run-a",
            agent_id="run-a",
            provider="codex",
            created_at="now",
        )
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO rebuild_generations(run_id, generation) VALUES (?, 3)",
                ("run-a",),
            )
        with store.connection(read_only=True) as connection:
            with mock.patch.object(
                store,
                "rebuild_generation",
                side_effect=AssertionError("opened a second connection"),
            ):
                cursor = store._cursor_from_connection(connection, "run-a")
        assert cursor.rebuild_generation == 3


def _write_recovery_run(run_dir: Path, run_id: str, raw: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "agent_id": run_id,
                "provider": "codex",
                "created_at": "now",
                "state": "starting",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "raw.jsonl").write_text(raw, encoding="utf-8")


def test_corrupt_legacy_boot_rebuilds_each_raw_shard() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        _write_recovery_run(
            runtime_path / "runs" / "run-a",
            "run-a",
            json.dumps(_raw(1, "warning", {"message": "a"}, received_at="now")) + "\n",
        )
        _write_recovery_run(
            runtime_path / "runs" / "run-b",
            "run-b",
            json.dumps(_raw(1, "warning", {"message": "b"}, received_at="now")) + "\n",
        )
        runtime_event_db_path(runtime_path).parent.mkdir(parents=True, exist_ok=True)
        runtime_event_db_path(runtime_path).write_bytes(b"corrupt legacy")

        assert migrate_legacy_event_db(runtime_path)
        assert SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-a"), migrate=False
        ).run_is_healthy("run-a")
        assert SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-b"), migrate=False
        ).run_is_healthy("run-b")


def test_corrupt_raw_boot_skips_only_the_corrupt_run() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        _write_recovery_run(
            runtime_path / "runs" / "run-a",
            "run-a",
            '{"seq":1}\nnot-json\n',
        )
        _write_recovery_run(
            runtime_path / "runs" / "run-b",
            "run-b",
            json.dumps(_raw(1, "warning", {"message": "b"}, received_at="now")) + "\n",
        )
        runtime_event_db_path(runtime_path).parent.mkdir(parents=True, exist_ok=True)
        runtime_event_db_path(runtime_path).write_bytes(b"corrupt legacy")

        assert migrate_legacy_event_db(runtime_path)
        assert not runtime_event_db_path(runtime_path, "run-a").exists()
        assert SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-b"), migrate=False
        ).run_is_healthy("run-b")


def test_legacy_migration_survives_crash_after_legacy_rename() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        legacy.create_run(
            "run-a",
            agent_id="run-a",
            provider="codex",
            created_at="2026-08-18T00:00:00Z",
        )
        original_replace = os.replace
        crashed = False

        def rename_then_crash(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            nonlocal crashed
            original_replace(source, destination)
            if not crashed and Path(source) == runtime_event_db_path(runtime_path):
                crashed = True
                raise OSError("crash during legacy rename")

        with mock.patch(
            "backend.app.agent_runtime.event_store.os.replace",
            side_effect=rename_then_crash,
        ):
            with pytest.raises(OSError, match="crash during legacy rename"):
                _migrate_legacy_event_db(runtime_path)
        assert SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-a"), migrate=False
        ).run_is_healthy("run-a")
        assert not list(runtime_path.glob("runs/run-a/.events.sqlite3.migration*"))


def test_legacy_migration_reopens_after_shards_before_legacy_rename() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp)
        legacy = SQLiteEventStore(runtime_event_db_path(runtime_path))
        legacy.create_run(
            "run-a",
            agent_id="run-a",
            provider="codex",
            created_at="2026-08-18T00:00:00Z",
        )
        original_replace = os.replace

        def fail_before_legacy_rename(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if Path(source) == runtime_event_db_path(runtime_path):
                raise OSError("crash before legacy rename")
            original_replace(source, destination)

        with mock.patch(
            "backend.app.agent_runtime.event_store.os.replace",
            side_effect=fail_before_legacy_rename,
        ), pytest.raises(OSError, match="crash before legacy rename"):
            _migrate_legacy_event_db(runtime_path)
        assert SQLiteEventStore(
            runtime_event_db_path(runtime_path, "run-a"), migrate=False
        ).run_is_healthy("run-a")
        assert runtime_event_db_path(runtime_path).is_file()
        assert _migrate_legacy_event_db(runtime_path)
        assert not runtime_event_db_path(runtime_path).exists()


def test_artifact_index_merges_archived_events_with_live_events() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp) / "runtime"
        archive_path = Path(tmp) / "archive"
        live = SQLiteEventStore(runtime_event_db_path(runtime_path, "live"))
        live.create_run(
            "live",
            agent_id="live",
            provider="codex",
            created_at="now",
        )
        artifact = {
            "id": "live-artifact",
            "kind": "artifact",
            "artifact": {"kind": "table", "filename": "live.html"},
        }
        with live.connection() as connection:
            connection.execute(
                "INSERT INTO events(run_id, event_id, raw_seq, kind, event_json, "
                "created_at, updated_at, revision, deleted) VALUES "
                "('live', 0, 1, 'artifact', ?, 'now', '2026-08-18T00:00:02Z', 1, 0)",
                (_json_bytes_for_test(artifact),),
            )
        session = archive_path / "ticket" / "20260818-000000"
        session.mkdir(parents=True)
        (session / "archive-complete.json").write_text("{}")
        (session / "run.json").write_text(json.dumps({"run_id": "archived"}))
        archived = {
            "id": "archived-artifact",
            "kind": "artifact",
            "artifact": {"kind": "table", "filename": "archived.html"},
        }
        (session / "events.jsonl").write_text(json.dumps(archived) + "\n")

        events = RuntimeEventStore(runtime_path, archive_dir=archive_path).read_artifact_events()
        assert {event["id"] for _run_id, event in events} == {
            "live-artifact",
            "archived-artifact",
        }


def test_artifact_index_streams_artifacts_past_the_old_limit() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp) / "runtime"
        archive_path = Path(tmp) / "archive"
        session = archive_path / "ticket" / "20260818-000000"
        session.mkdir(parents=True)
        (session / "archive-complete.json").write_text("{}")
        (session / "run.json").write_text(json.dumps({"run_id": "archived"}))
        artifacts = [
            {
                "kind": "artifact",
                "id": f"artifact-{index}",
                "artifact": {"kind": "table", "filename": f"{index}.html"},
            }
            for index in range(801)
        ]
        (session / "events.jsonl").write_text(
            "".join(json.dumps(artifact) + "\n" for artifact in artifacts)
        )

        events = list(
            RuntimeEventStore(
                runtime_path,
                archive_dir=archive_path,
            ).read_artifact_events()
        )

        assert len(events) == 801
        assert any(event["id"] == "artifact-800" for _run_id, event in events)


def test_artifact_index_skips_malformed_shard_rows() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp) / "runtime"
        archive_path = Path(tmp) / "archive"
        healthy = SQLiteEventStore(runtime_event_db_path(runtime_path, "healthy"))
        healthy.create_run(
            "healthy",
            agent_id="healthy",
            provider="codex",
            created_at="now",
        )
        healthy_artifact = {
            "id": "healthy-artifact",
            "kind": "artifact",
            "artifact": {"kind": "table", "filename": "healthy.html"},
        }
        with healthy.connection() as connection:
            connection.execute(
                "INSERT INTO events(run_id, event_id, raw_seq, kind, event_json, "
                "created_at, updated_at, revision, deleted) VALUES "
                "('healthy', 0, 1, 'artifact', ?, 'now', 'now', 1, 0)",
                (_json_bytes_for_test(healthy_artifact),),
            )

        damaged = SQLiteEventStore(runtime_event_db_path(runtime_path, "damaged"))
        damaged.create_run(
            "damaged",
            agent_id="damaged",
            provider="codex",
            created_at="now",
        )
        with damaged.connection() as connection:
            connection.execute(
                "INSERT INTO events(run_id, event_id, raw_seq, kind, event_json, "
                "created_at, updated_at, revision, deleted) VALUES "
                "('damaged', 0, 1, 'artifact', '{malformed', 'now', 'now', 1, 0)"
            )

        session = archive_path / "ticket" / "20260818-000000"
        session.mkdir(parents=True)
        (session / "archive-complete.json").write_text("{}")
        (session / "run.json").write_text(json.dumps({"run_id": "archived"}))
        archived_artifact = {
            "id": "archived-artifact",
            "kind": "artifact",
            "artifact": {"kind": "table", "filename": "archived.html"},
        }
        (session / "events.jsonl").write_text(
            json.dumps(archived_artifact) + "\n"
        )

        events = RuntimeEventStore(
            runtime_path,
            archive_dir=archive_path,
        ).read_artifact_events()
        assert {event["id"] for _run_id, event in events} == {
            "healthy-artifact",
            "archived-artifact",
        }


def test_artifact_index_stops_when_the_reader_is_cancelled() -> None:
    with TemporaryDirectory() as tmp:
        runtime_path = Path(tmp) / "runtime"
        archive_path = Path(tmp) / "archive"
        session = archive_path / "ticket" / "20260818-000000"
        session.mkdir(parents=True)
        (session / "archive-complete.json").write_text("{}")
        (session / "run.json").write_text(json.dumps({"run_id": "archived"}))
        (session / "events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "artifact",
                    "id": "archived-artifact",
                }
            )
            + "\n"
        )

        events = RuntimeEventStore(
            runtime_path,
            archive_dir=archive_path,
        ).read_artifact_events(should_cancel=lambda: True)

        assert list(events) == []


def _json_bytes_for_test(value: dict[str, object]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
