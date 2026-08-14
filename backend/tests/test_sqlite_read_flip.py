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
        self.patches = tuple()
        self.generation = 0

    def cursor(self, _run_id: str):
        return self.state

    def view_rows(self, _run_id: str):
        return {"projections": [self.projection]}

    def read_session_snapshot(self, _run_id: str, *, source_class: str, after_cursor: int):
        del after_cursor
        return SimpleNamespace(
            state=self.state,
            source_key=f"sqlite://{source_class}/run-1/rebuild-{self.generation}",
            projection=self.projection,
            events=(self.event,),
            patches=self.patches,
        )

    def read_older_snapshot(
        self, _run_id: str, *, source_class: str, before_event_id: int, limit: int
    ):
        del before_event_id, limit
        return SimpleNamespace(
            state=self.state,
            source_key=f"sqlite://{source_class}/run-1/rebuild-{self.generation}",
            events=(self.event,),
            has_older=False,
        )

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
        for other_variable in main._SQLITE_READ_FLAG_ENV.values():
            monkeypatch.delenv(other_variable, raising=False)
        monkeypatch.setenv(variable, "1")
        assert main._sqlite_read_enabled(route)
        assert all(
            main._sqlite_read_enabled(other_route) is (other_route == route)
            for other_route in main._SQLITE_READ_FLAG_ENV
        )
        monkeypatch.delenv(variable, raising=False)


def test_route_registry_assigns_one_adapter_per_flag() -> None:
    routes = main._SQLITE_READ_ROUTES
    assert len(routes) == len(main._SQLITE_READ_FLAG_ENV)
    assert len({flag for flag, _adapter in routes.values()}) == len(routes)
    assert len({adapter for _flag, adapter in routes.values()}) == len(routes)


def test_sqlite_source_key_round_trips_as_typed_data() -> None:
    key = main.SQLiteSourceKey("archive", "run-1", 12)

    assert main.SQLiteSourceKey.parse(key.format()) == key


def test_session_delta_does_not_flip_when_only_delta_flag_is_enabled(monkeypatch) -> None:
    legacy = {
        "events": [],
        "base": 0,
        "cursor": 1,
        "tail_from": 0,
        "patches": [],
    }
    with (
        mock.patch.object(main.transcripts, "read_session_delta", return_value=legacy),
        mock.patch.object(main, "_sqlite_session_payload") as sqlite,
    ):
        monkeypatch.setenv("WIKI_SQLITE_READ_DELTA", "1")
        payload = main._session_delta_payload(
            "codex",
            Path("/legacy.jsonl"),
            cursor=0,
            run_id="run-1",
            read_route="session",
        )

    assert payload["path"] == "/legacy.jsonl"
    sqlite.assert_not_called()


def test_sqlite_source_change_forces_a_v2_cursor_reset() -> None:
    reset = _sqlite_payload(cursor=7, client_path="/legacy/transcript.jsonl")
    steady = _sqlite_payload(cursor=7, client_path="sqlite://live/run-1/rebuild-0")

    assert reset is not None
    assert reset["path"] == "sqlite://live/run-1/rebuild-0"
    assert reset["events"] == [{"id": 0, "kind": "message", "text": "hello"}]
    assert steady is not None
    assert steady["events"] == []
    assert "has_older" not in steady


def test_rebuild_generation_change_forces_a_reset() -> None:
    indexed = _IndexedStore()
    with mock.patch.object(main, "_sqlite_event_store", return_value=indexed):
        first = main._sqlite_session_payload(
            "run-1",
            fmt="codex",
            cursor=7,
            client_path="sqlite://live/run-1/rebuild-0",
            model=None,
            desired_model=None,
            kind="cdx",
            provider="codex",
            working=True,
        )
        indexed.generation = 1
        rebuilt = main._sqlite_session_payload(
            "run-1",
            fmt="codex",
            cursor=7,
            client_path="sqlite://live/run-1/rebuild-0",
            model=None,
            desired_model=None,
            kind="cdx",
            provider="codex",
            working=True,
        )

    assert first is not None and first["events"] == []
    assert rebuilt is not None
    assert rebuilt["path"] == "sqlite://live/run-1/rebuild-1"
    assert rebuilt["events"] == [indexed.event]


def test_sqlite_cursor_advance_returns_a_true_tail_delta() -> None:
    indexed = _IndexedStore()
    indexed.state.change_cursor = 8
    indexed.patches = (SimpleNamespace(event_id=0, patch={"event": indexed.event}),)
    with mock.patch.object(main, "_sqlite_event_store", return_value=indexed):
        payload = main._sqlite_session_payload(
            "run-1",
            fmt="codex",
            cursor=7,
            client_path="sqlite://live/run-1/rebuild-0",
            model=None,
            desired_model=None,
            kind="cdx",
            provider="codex",
            working=True,
        )

    assert payload is not None
    assert payload["events"] == [indexed.event]
    assert "has_older" not in payload


def test_enrichment_pipeline_is_shared_for_sqlite_and_legacy_deltas() -> None:
    legacy_raw = {
        "events": [{"id": 0, "kind": "tool", "tool": {"name": "Agent"}}],
        "base": 0,
        "cursor": 1,
        "tail_from": 0,
        "patches": [],
    }
    sqlite_raw = {**legacy_raw, "events": [dict(legacy_raw["events"][0])]}
    enriched_events = [{"id": 0, "kind": "tool", "tool": {"name": "Agent", "agent_id": "child"}}]
    provider_inspector = {
        "events": [{"seq": 2, "kind": "tool"}],
        "composer_messages": [{"text": "compose"}],
    }
    with (
        mock.patch.object(
            main.transcripts,
            "annotate_agent_events",
            side_effect=[enriched_events, list(enriched_events)],
        ),
        mock.patch.object(
            main,
            "_overlay_pending_questions",
            side_effect=lambda delta, **_kwargs: {
                **delta,
                "events": [*delta["events"], {"id": 1, "kind": "question", "text": "pending"}],
            },
        ),
        mock.patch.object(
            main,
            "_provider_events",
            side_effect=[provider_inspector, dict(provider_inspector)],
        ),
        mock.patch.object(
            main,
            "_queue_messages",
            side_effect=[[{"text": "queued"}], [{"text": "queued"}]],
        ),
    ):
        legacy = main._compose_session_payload(
            legacy_raw,
            fmt="claude",
            source_path=Path("/main.jsonl"),
            raw_path=Path("/raw.jsonl"),
            client_cursor=0,
            source_key="/legacy.jsonl",
            model=None,
            desired_model=None,
            kind="cc",
            provider="claude",
            working=True,
            include_subagents=False,
            include_queue=True,
            ticket="WIKI-282",
        )
        sqlite = main._compose_session_payload(
            sqlite_raw,
            fmt="claude",
            source_path=Path("/main.jsonl"),
            raw_path=Path("/raw.jsonl"),
            client_cursor=0,
            source_key="sqlite://live/run-1/rebuild-0",
            model=None,
            desired_model=None,
            kind="cc",
            provider="claude",
            working=True,
            include_subagents=False,
            include_queue=True,
            ticket="WIKI-282",
        )

    legacy_without_source = {key: value for key, value in legacy.items() if key != "path"}
    sqlite_without_source = {key: value for key, value in sqlite.items() if key != "path"}
    assert legacy_without_source == sqlite_without_source
    assert legacy["provider_inspector"] == provider_inspector
    assert legacy["composer_messages"] == provider_inspector["composer_messages"]


def test_sqlite_adapter_cannot_bypass_shared_enrichment() -> None:
    indexed = _IndexedStore()
    indexed.event = {"id": 0, "kind": "tool", "tool": {"name": "Agent"}}
    enriched = [{"id": 0, "kind": "tool", "tool": {"name": "Agent", "agent_id": "child"}}]
    with (
        mock.patch.object(main, "_sqlite_event_store", return_value=indexed),
        mock.patch.object(main.transcripts, "annotate_agent_events", return_value=enriched),
    ):
        payload = main._sqlite_session_payload(
            "run-1",
            fmt="claude",
            cursor=0,
            client_path=None,
            model=None,
            desired_model=None,
            kind="cc",
            provider="claude",
            working=True,
            transcript_path=Path("/main.jsonl"),
        )

    assert payload is not None
    assert payload["events"] == enriched


def test_sse_flip_has_its_own_flag() -> None:
    assert main._SQLITE_READ_ROUTES["sse"] == (
        "WIKI_SQLITE_READ_SSE",
        "sse_adapter",
    )


def test_sse_sqlite_adapter_emits_a_distinct_source(monkeypatch) -> None:
    published: list[dict] = []

    async def capture(event: dict) -> None:
        published.append(event)

    monkeypatch.setattr(main, "publish_agent_event", capture)
    import asyncio

    asyncio.run(main._publish_sqlite_session_event({"type": "session"}))

    assert published == [{"type": "session", "source": "sqlite"}]


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
        off = main._session_delta_payload(
            "codex",
            Path("/legacy.jsonl"),
            cursor=0,
            run_id="run-1",
            read_route="delta",
        )
        monkeypatch.setenv("WIKI_SQLITE_READ_DELTA", "1")
        on = main._session_delta_payload(
            "codex",
            Path("/legacy.jsonl"),
            cursor=0,
            run_id="run-1",
            read_route="delta",
        )

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
            return_value={
                "format": "codex",
                "path": "sqlite://live/run-1/rebuild-0",
            },
        ),
        mock.patch.object(main, "_sqlite_event_store", return_value=indexed),
    ):
        monkeypatch.setenv("WIKI_SQLITE_READ_OLDER", "1")
        payload = main.agent_session_older("WIKI-282", before=1, count=5)

    assert payload["path"] == "sqlite://live/run-1/rebuild-0"
    assert payload["events"] == [indexed.event]


def test_older_telemetry_uses_an_independent_legacy_page(monkeypatch) -> None:
    indexed = _IndexedStore()
    legacy_page = {
        "events": [{"id": 0, "kind": "message", "text": "legacy"}],
        "base": 0,
        "has_older": False,
    }
    with (
        mock.patch.object(
            main,
            "agent_session",
            return_value={
                "format": "codex",
                "path": "sqlite://live/run-1/rebuild-0",
            },
        ),
        mock.patch.object(main, "_sqlite_event_store", return_value=indexed),
        mock.patch.object(
            main,
            "_legacy_source_for_sqlite_run",
            return_value=("codex", Path("/legacy.jsonl")),
        ),
        mock.patch.object(main.transcripts, "read_older_session", return_value=legacy_page),
        mock.patch.object(main, "_record_read_mismatch") as mismatch,
    ):
        monkeypatch.setenv("WIKI_SQLITE_READ_OLDER", "1")
        main.agent_session_older("WIKI-282", before=1, count=5)

    legacy, sqlite = mismatch.call_args.args[-2:]
    assert legacy != sqlite
    assert legacy["events"] != sqlite["events"]


def test_palette_reads_the_sqlite_artifact_index_without_jsonl_walk() -> None:
    items = palette.collect_artifact_items_from_index(
        _IndexedStore(), ticket_by_run={"run-1": "WIKI-282"}
    )

    assert items is not None
    assert [(item.artifact_id, item.ticket) for item in items] == [
        ("artifact-1", "WIKI-282")
    ]


def test_archived_palette_artifact_link_keeps_run_identity() -> None:
    items = palette.collect_artifact_items_from_index(
        _IndexedStore(),
        archive_by_run={"run-1": ("WIKI-282", "2026-08-13T12:00:00+00:00")},
    )

    assert items is not None
    assert "run_id=run-1" in items[0].url
    assert "archived_at=2026-08-13T12%3A00%3A00%2B00%3A00" in items[0].url


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
