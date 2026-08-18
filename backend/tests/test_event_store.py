from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

from backend.app.agent_runtime.event_store import (
    EventReducerAdapter,
    SCHEMA_VERSION,
    SQLiteEventStore,
    migrate_event_db,
    replay_raw_jsonl,
)


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
                    "method": "turn/start",
                    "params": {
                        "threadId": "thread-1",
                        "input": [{"type": "text", "text": "hello codex"}],
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
