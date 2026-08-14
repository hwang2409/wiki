"""Discrimination tests for WIKI-282's independently flipped read routes."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from backend.app import main, palette
from backend.app.agent_runtime import supervisor


class _IndexedStore:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            rebuild_state="ready",
            change_cursor=7,
            patch_base_cursor=0,
            event_base=0,
            normalizer_version="wiki-282-1",
        )
        self.event = {"id": 0, "kind": "message", "text": "hello"}
        self.projection = (
            "{}",
            "[]",
            None,
            "{}",
            "[]",
            "[]",
            '{"rendered": 1, "summarized": 0, "ignored": 0, "unknown": 0}',
            None,
        )

    def cursor(self, _run_id: str):
        return self.state

    def view_rows(self, _run_id: str):
        return {"projections": [self.projection]}

    def read_events(self, _run_id: str):
        return [self.event]

    def read_patches(self, _run_id: str, *, after_cursor: int):
        del after_cursor
        return []

    def read_normalized_events(self, _run_id: str, *, after_seq: int, limit: int):
        del after_seq, limit
        return []

    def read_artifact_events(self):
        return [
            (
                "run-1",
                {
                    "kind": "artifact",
                    "id": "artifact-1",
                    "artifact": {"kind": "code", "filename": "read.py"},
                    "title": "read adapter",
                },
            )
        ]


def _sqlite_payload(*, cursor: int, client_path: str | None):
    indexed = _IndexedStore()
    with mock.patch.object(main, "_sqlite_event_store", return_value=indexed):
        return main._sqlite_session_payload(
            "run-1",
            fmt="codex",
            cursor=cursor,
            client_path=client_path,
            model=None,
            desired_model=None,
            kind="cdx",
            provider="codex",
            working=True,
        )


def test_each_read_flag_is_independent(monkeypatch) -> None:
    for route, variable in main._SQLITE_READ_FLAG_ENV.items():
        monkeypatch.delenv(variable, raising=False)
        assert not main._sqlite_read_enabled(route)
        monkeypatch.setenv(variable, "1")
        assert main._sqlite_read_enabled(route)
        monkeypatch.delenv(variable, raising=False)


def test_sqlite_source_change_forces_a_v2_cursor_reset() -> None:
    reset = _sqlite_payload(cursor=7, client_path="/legacy/transcript.jsonl")
    steady = _sqlite_payload(cursor=7, client_path="sqlite://run-1")

    assert reset is not None
    assert reset["path"] == "sqlite://run-1"
    assert reset["events"] == [{"id": 0, "kind": "message", "text": "hello"}]
    assert steady is not None
    assert steady["events"] == []
    assert "has_older" not in steady


def test_sqlite_adapter_refuses_a_nonready_materializer() -> None:
    indexed = _IndexedStore()
    indexed.state.rebuild_state = "needed"
    with mock.patch.object(main, "_sqlite_event_store", return_value=indexed):
        assert main._sqlite_session_payload(
            "run-1",
            fmt="codex",
            cursor=0,
            client_path=None,
            model=None,
            desired_model=None,
            kind="cdx",
            provider="codex",
            working=True,
        ) is None


def test_delta_flag_flips_only_the_delta_adapter(monkeypatch) -> None:
    legacy = {
        "version": 2,
        "format": "codex",
        "path": "/legacy.jsonl",
        "tokens": None,
        "tasks": [],
        "pr": None,
        "session_meta": {},
        "dispositions": {},
        "base": 0,
        "cursor": 1,
        "tail_from": 0,
        "events": [],
        "patches": [],
    }
    sqlite = {**legacy, "path": "sqlite://run-1"}
    with (
        mock.patch.object(main.transcripts, "read_session_delta", return_value=legacy),
        mock.patch.object(main, "_sqlite_session_payload", return_value=sqlite),
        mock.patch.object(main, "_record_read_mismatch") as mismatch,
    ):
        monkeypatch.delenv("WIKI_SQLITE_READ_DELTA", raising=False)
        off = main._session_delta_payload("codex", Path("/legacy.jsonl"), cursor=0, run_id="run-1")
        monkeypatch.setenv("WIKI_SQLITE_READ_DELTA", "1")
        on = main._session_delta_payload("codex", Path("/legacy.jsonl"), cursor=0, run_id="run-1")

    assert off["path"] == "/legacy.jsonl"
    assert on["path"] == "sqlite://run-1"
    mismatch.assert_called_once_with("delta", "run-1", off, sqlite)


def test_archive_flag_keeps_the_jsonl_fallback_when_sqlite_is_not_ready(monkeypatch) -> None:
    with TemporaryDirectory() as tmp:
        archive_dir = Path(tmp)
        (archive_dir / "events.jsonl").write_text(
            json.dumps(
                {
                    "seq": 1,
                    "kind": "assistant",
                    "disposition": "rendered",
                    "payload": {"message": "archive fallback"},
                }
            ) + "\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(
                main,
                "_archive_runtime_identity",
                return_value=({"run_id": "run-1"}, None, "cdx", "codex"),
            ),
            mock.patch.object(main, "_sqlite_session_payload", return_value=None),
        ):
            monkeypatch.setenv("WIKI_SQLITE_READ_ARCHIVE", "1")
            payload = main._archived_events_payload(
                archive_dir,
                cursor=0,
                client_path=None,
                fallback_kind="cdx",
            )

    assert payload is not None
    assert payload["path"].endswith("events.jsonl")
    assert payload["format"] == "provider-events"


def test_provider_event_flag_uses_sqlite_only_when_ready(monkeypatch) -> None:
    registry = {
        "WIKI-282": {
            "current": {"run_id": "run-1", "kind": "cdx", "provider": "codex"}
        }
    }
    indexed = _IndexedStore()
    with (
        mock.patch.object(main, "_read_agent_registry", return_value=registry),
        mock.patch.object(main, "_supervisor_request", return_value={"events": [{"seq": 9}]}),
        mock.patch.object(main, "_sqlite_event_store", return_value=indexed),
    ):
        monkeypatch.setenv("WIKI_SQLITE_READ_PROVIDER_EVENTS", "1")
        payload = main._provider_events("WIKI-282")

    assert payload is not None
    assert payload["events"] == []


def test_older_flag_uses_indexed_page_after_a_sqlite_session(monkeypatch) -> None:
    indexed = _IndexedStore()
    indexed.read_events_before = mock.Mock(return_value=([indexed.event], False))
    with (
        mock.patch.object(
            main,
            "agent_session",
            return_value={"format": "codex", "path": "sqlite://run-1"},
        ),
        mock.patch.object(main, "_sqlite_event_store", return_value=indexed),
    ):
        monkeypatch.setenv("WIKI_SQLITE_READ_OLDER", "1")
        payload = main.agent_session_older("WIKI-282", before=1, count=5)

    assert payload["path"] == "sqlite://run-1"
    assert payload["events"] == [indexed.event]
    indexed.read_events_before.assert_called_once_with("run-1", before_event_id=1, limit=5)


def test_palette_reads_the_sqlite_artifact_index_without_jsonl_walk() -> None:
    items = palette.collect_artifact_items_from_index(
        _IndexedStore(), ticket_by_run={"run-1": "WIKI-282"}
    )

    assert items is not None
    assert [(item.artifact_id, item.ticket) for item in items] == [
        ("artifact-1", "WIKI-282")
    ]


def test_palette_keeps_the_legacy_scan_fallback_when_index_is_unavailable() -> None:
    class BrokenIndex:
        def read_artifact_events(self):
            raise OSError("index absent")

    assert palette.collect_artifact_items_from_index(BrokenIndex()) is None


def test_session_sse_is_published_after_the_materialized_write() -> None:
    source = inspect.getsource(supervisor.Supervisor._handle_provider_event_without_admission)
    normal_path = source.index("normalized_payload = normalized.payload")
    write_index = source.index("await self._dual_write_normalized_async(", normal_path)
    publish_index = source.index(
        'await self._publish(\n            {"type": "session", "ticket": record.agent_id',
        normal_path,
    )

    assert "await self._publish(" not in source[normal_path:write_index]
    assert write_index < publish_index
