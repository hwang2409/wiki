from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.event_store import SQLiteEventStore
from backend.app.main import SQLiteSourceKey
from backend.tests.harness_dual_stack import DualStackHarness


def _payload_bytes(payload: dict[str, object], *, source_path: str) -> bytes:
    comparable = dict(payload)
    comparable["path"] = source_path
    return json.dumps(comparable, sort_keys=True, separators=(",", ":")).encode()


def _assert_full_payload_parity(
    legacy: dict[str, object], sqlite: dict[str, object]
) -> None:
    legacy_path = legacy["path"]
    sqlite_path = sqlite["path"]
    assert isinstance(legacy_path, str)
    assert isinstance(sqlite_path, str)
    assert legacy_path != sqlite_path
    assert sqlite_path.startswith("sqlite://")
    assert _payload_bytes(legacy, source_path="<source>") == _payload_bytes(
        sqlite, source_path="<source>"
    )


def test_dual_stack_harness_calls_real_session_route() -> None:
    with DualStackHarness() as harness:
        legacy = harness.session()
        sqlite = harness.session(flags=("session",))

    assert legacy.status_code == 200
    assert sqlite.status_code == 200
    legacy_payload = legacy.json()
    sqlite_payload = sqlite.json()
    assert legacy_payload["format"] == "claude"
    assert sqlite_payload["path"].startswith("sqlite://")
    assert sqlite_payload["events"]
    assert sqlite_payload["composer_messages"]
    assert legacy_payload["provider_inspector"] == sqlite_payload["provider_inspector"]
    assert legacy_payload["provider_inspector"]["composer_messages"]
    assert any(
        event.get("tool_use_id") == "toolu_pending_fixture"
        for event in legacy_payload["events"]
    )
    assert any(
        event.get("payload", {}).get("tool", {}).get("agent_id")
        == "child-agent-282"
        for event in legacy_payload["provider_inspector"]["events"]
    )
    assert any(
        event.get("tool", {}).get("name") == "Agent"
        and event.get("tool", {}).get("agent_id") == "abc12345"
        for event in legacy_payload["events"]
    )

    _assert_full_payload_parity(legacy_payload, sqlite_payload)


def test_headless_routes_default_to_sqlite_and_explicit_off_stays_legacy() -> None:
    with DualStackHarness() as harness:
        session_default = harness.session(defaults=True)
        session_off = harness.session(flags=())
        delta_default = harness.delta(defaults=True)
        older_default = harness.older(defaults=True)
        provider_default = harness.provider_events(defaults=True)

    assert session_default.status_code == 200
    assert session_default.json()["path"].startswith("sqlite://live/")
    assert session_off.status_code == 200
    assert not session_off.json()["path"].startswith("sqlite://")
    assert delta_default.status_code == 200
    assert delta_default.json()["path"].startswith("sqlite://child/")
    assert older_default.status_code == 200
    assert older_default.json()["path"].startswith("sqlite://older/")
    assert provider_default.status_code == 200
    assert provider_default.json()["events"]


def test_default_sqlite_corruption_is_visible_and_repair_recovers_session() -> None:
    with DualStackHarness() as harness:
        harness.corrupt_sqlite_projection()
        failed = harness.session(defaults=True)
        harness.rebuild_swap()
        repaired = harness.session(defaults=True)

    assert failed.status_code == 503
    assert "SQLite session" in failed.json()["detail"]
    assert repaired.status_code == 200
    assert repaired.json()["events"]


def test_default_headless_reads_survive_native_transcript_cleanup() -> None:
    with DualStackHarness() as harness:
        harness.delete_native_transcripts()
        session = harness.session(defaults=True)
        delta = harness.delta(defaults=True)
        older = harness.older(defaults=True)

    assert session.status_code == 200
    assert session.json()["path"].startswith("sqlite://live/")
    assert delta.status_code == 200
    assert delta.json()["path"].startswith("sqlite://child/")
    assert older.status_code == 200
    assert older.json()["path"].startswith("sqlite://older/")


def test_default_session_and_child_reads_never_open_native_transcripts() -> None:
    with DualStackHarness() as harness:
        native_paths = {
            harness.transcript_path.resolve(),
            harness.subagent_path.resolve(),
        }
        original_open = Path.open

        def guarded_open(path: Path, *args: object, **kwargs: object):
            if path.resolve() in native_paths:
                raise AssertionError(f"native transcript opened: {path}")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            session = harness.session(defaults=True)
            child = harness.delta(defaults=True)

    assert session.status_code == 200
    assert child.status_code == 200
    assert session.json()["path"].startswith("sqlite://live/")
    assert child.json()["path"].startswith("sqlite://child/")


def test_default_older_payload_is_byte_stable_after_transcript_cleanup() -> None:
    with DualStackHarness() as harness:
        native_path = harness.transcript_path.resolve()
        original_open = Path.open

        def guarded_open(path: Path, *args: object, **kwargs: object):
            if path.resolve() == native_path:
                raise AssertionError(f"native transcript opened: {path}")
            return original_open(path, *args, **kwargs)

        with mock.patch.dict(os.environ, {"WIKI_SQLITE_SHADOW_SAMPLE_RATE": "0"}):
            with mock.patch.object(Path, "open", guarded_open):
                present = harness.older(defaults=True)
            harness.delete_native_transcripts()
            deleted = harness.older(defaults=True)

    assert present.status_code == 200
    assert deleted.status_code == 200
    assert present.content == deleted.content


def test_non_headless_session_keeps_native_parser() -> None:
    with DualStackHarness() as harness:
        harness.make_non_headless()
        response = harness.session(defaults=True)

    assert response.status_code == 200
    assert not response.json()["path"].startswith("sqlite://")
    assert response.json()["events"]


def test_child_delta_uses_production_mapping_and_preserves_parity() -> None:
    with DualStackHarness() as harness:
        mapping = SQLiteEventStore(harness.sqlite_path, migrate=False).child_run_for(
            harness.run_id, harness.subagent_id
        )
        harness.mark_child_stale()
        legacy = harness.delta()
        sqlite = harness.delta(flags=("delta",))

    assert mapping is not None
    assert mapping.parent_run_id == harness.run_id
    assert mapping.child_id == harness.subagent_id
    assert legacy.status_code == 200
    assert sqlite.status_code == 200
    legacy_payload = legacy.json()
    sqlite_payload = sqlite.json()
    assert [event["text"] for event in legacy_payload["events"]] == [
        "Inspect child-only branch for WIKI-282.",
        "child-only event",
    ]
    assert sqlite_payload["path"].startswith("sqlite://child/")
    _assert_full_payload_parity(legacy_payload, sqlite_payload)


def test_child_delta_falls_back_when_file_grows_without_parent_ingest() -> None:
    with DualStackHarness() as harness:
        harness.delta(flags=("delta",))
        harness.append_child_event_after_ingest()
        response = harness.delta(flags=("delta",))

    assert response.status_code == 200
    payload = response.json()
    assert not payload["path"].startswith("sqlite://")
    assert any(event["text"] == "child grew after ingest" for event in payload["events"])


class _ChildIngestReader:
    def __init__(
        self,
        handle,
        *,
        after_lines: int,
        reached: threading.Event,
        release: threading.Event | None = None,
        fail: bool = False,
        on_eof=None,
    ) -> None:
        self.handle = handle
        self.after_lines = after_lines
        self.reached = reached
        self.release = release
        self.fail = fail
        self.on_eof = on_eof

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> object:
        return self.handle.__exit__(*args)

    def __iter__(self):
        for index, line in enumerate(self.handle):
            yield line
            if index + 1 == self.after_lines:
                self.reached.set()
                if self.fail:
                    raise RuntimeError("test child ingest crash")
                if self.release is not None:
                    assert self.release.wait(timeout=5)
        if self.on_eof is not None:
            self.on_eof(self.handle.tell())

    def tell(self) -> int:
        return self.handle.tell()


def test_child_delta_falls_back_after_mid_ingest_crash() -> None:
    with DualStackHarness() as harness:
        reached = threading.Event()
        original_open = Path.open
        used = False

        def crash_open(path: Path, *args: object, **kwargs: object):
            nonlocal used
            handle = original_open(path, *args, **kwargs)
            if path.resolve() == harness.subagent_path.resolve() and not used:
                used = True
                return _ChildIngestReader(
                    handle,
                    after_lines=1,
                    reached=reached,
                    fail=True,
                )
            return handle

        errors: list[BaseException] = []

        def ingest() -> None:
            try:
                assert harness.supervisor is not None
                harness.supervisor.sync_subagent_runs(harness.run_id)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(Path, "open", crash_open):
            worker = threading.Thread(target=ingest)
            worker.start()
            assert reached.wait(timeout=5)
            response = harness.delta(defaults=True)
            worker.join(timeout=5)

        assert errors
        assert response.status_code == 200
        assert not response.json()["path"].startswith("sqlite://child/")
        mapping = SQLiteEventStore(harness.sqlite_path, migrate=False).child_run_for(
            harness.run_id, harness.subagent_id
        )
        assert mapping is not None
        assert mapping.source_size == -1


def test_child_delta_stays_legacy_until_ingest_publishes_eof() -> None:
    with DualStackHarness() as harness:
        reached = threading.Event()
        release = threading.Event()
        original_open = Path.open
        used = False

        def gated_open(path: Path, *args: object, **kwargs: object):
            nonlocal used
            handle = original_open(path, *args, **kwargs)
            if path.resolve() == harness.subagent_path.resolve() and not used:
                used = True
                return _ChildIngestReader(
                    handle,
                    after_lines=1,
                    reached=reached,
                    release=release,
                )
            return handle

        errors: list[BaseException] = []

        def ingest() -> None:
            try:
                assert harness.supervisor is not None
                harness.supervisor.sync_subagent_runs(harness.run_id)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(Path, "open", gated_open):
            worker = threading.Thread(target=ingest)
            worker.start()
            assert reached.wait(timeout=5)
            during = harness.delta(defaults=True)
            release.set()
            worker.join(timeout=5)
            after = harness.delta(defaults=True)

        assert not errors
        assert not during.json()["path"].startswith("sqlite://child/")
        assert after.json()["path"].startswith("sqlite://child/")


def test_child_ingest_publishes_consumed_offset_before_writer_append() -> None:
    with DualStackHarness() as harness:
        original_open = Path.open
        used = False
        consumed_offsets: list[int] = []

        def append_at_eof(offset: int) -> None:
            consumed_offsets.append(offset)
            with original_open(
                harness.subagent_path, "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": "2026-07-10T16:00:03Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "appended after EOF"}
                                ],
                            },
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )

        def append_race_open(path: Path, *args: object, **kwargs: object):
            nonlocal used
            handle = original_open(path, *args, **kwargs)
            if path.resolve() == harness.subagent_path.resolve() and not used:
                used = True
                return _ChildIngestReader(
                    handle,
                    after_lines=100,
                    reached=threading.Event(),
                    on_eof=append_at_eof,
                )
            return handle

        with mock.patch.object(Path, "open", append_race_open):
            assert harness.supervisor is not None
            harness.supervisor.sync_subagent_runs(harness.run_id)
            during = harness.delta(defaults=True)
            mapping = SQLiteEventStore(harness.sqlite_path, migrate=False).child_run_for(
                harness.run_id, harness.subagent_id
            )
            harness.supervisor.sync_subagent_runs(harness.run_id)
            after = harness.delta(defaults=True)

        assert consumed_offsets
        assert mapping is not None
        assert mapping.source_size == consumed_offsets[0]
        assert mapping.source_size < harness.subagent_path.stat().st_size
        assert not during.json()["path"].startswith("sqlite://child/")
        assert after.json()["path"].startswith("sqlite://child/")


def test_older_shadow_resolution_error_does_not_change_served_response() -> None:
    with DualStackHarness() as harness:
        baseline = harness.older(defaults=True)
        native_path = harness.transcript_path.resolve()
        original_open = Path.open

        def corrupt_open(path: Path, *args: object, **kwargs: object):
            if path.resolve() == native_path:
                raise UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "fixture")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", corrupt_open):
            response = harness.older(defaults=True)

    assert baseline.status_code == 200
    assert response.status_code == 200
    assert response.content == baseline.content


def test_parent_rebuild_preserves_child_mapping_and_flip() -> None:
    with DualStackHarness() as harness:
        before = SQLiteEventStore(harness.sqlite_path, migrate=False).child_run_for(
            harness.run_id, harness.subagent_id
        )
        harness.rebuild_swap()
        after = SQLiteEventStore(harness.sqlite_path, migrate=False).child_run_for(
            harness.run_id, harness.subagent_id
        )
        response = harness.delta(flags=("delta",))

    assert before is not None
    assert after == before
    assert response.status_code == 200
    assert response.json()["path"].startswith("sqlite://child/")


def test_older_route_has_independent_sqlite_parity() -> None:
    with DualStackHarness() as harness:
        legacy = harness.older()
        sqlite = harness.older(flags=("older",))

    assert legacy.status_code == 200
    assert sqlite.status_code == 200
    _assert_full_payload_parity(legacy.json(), sqlite.json())


def test_provider_event_route_has_independent_sqlite_parity() -> None:
    with DualStackHarness(include_legacy_artifacts=False) as harness:
        legacy = harness.provider_events()
        sqlite = harness.provider_events(flags=("provider-events",))

    assert legacy.status_code == 200
    assert sqlite.status_code == 200
    assert legacy.json() == sqlite.json()


def test_sse_sqlite_producer_preserves_only_browser_contract() -> None:
    with DualStackHarness() as harness:
        event = harness.sse_session_event(flags=("sse",))

    assert event == {
        "type": "session",
        "ticket": "WIKI-282-HARNESS",
        "surface": "session",
    }


def test_telemetry_counts_only_sampled_real_route_comparisons() -> None:
    with DualStackHarness() as harness:
        response = harness.session(flags=("session",))
        telemetry = harness.request("GET", "/api/agent-read-telemetry").json()

    assert response.status_code == 200
    assert telemetry["session"]["reads"] == (
        telemetry["session"]["matches"] + telemetry["session"]["mismatches"]
    )
    assert telemetry["session"]["reads"] >= 1


def test_telemetry_records_a_real_shadow_mismatch() -> None:
    with DualStackHarness(include_pending_overlay=False) as harness:
        harness.corrupt_sqlite_session_event()
        response = harness.session(flags=("session",))
        telemetry = harness.request("GET", "/api/agent-read-telemetry").json()

    assert response.status_code == 200
    assert telemetry["session"]["mismatches"] >= 1


def test_session_flag_does_not_enable_child_real_http_adapter() -> None:
    with DualStackHarness() as harness:
        enabled = harness.session(flags=("session",))
        child = harness.delta(flags=("session",))

    assert enabled.status_code == 200
    assert enabled.json()["path"].startswith("sqlite://")
    assert child.status_code == 200
    assert not child.json()["path"].startswith("sqlite://")


def test_real_rebuild_swap_changes_source_identity_and_resets_cursor() -> None:
    with DualStackHarness() as harness:
        initial = harness.session(flags=("session",)).json()
        harness.rebuild_swap()
        rebuilt = harness.session(
            flags=("session",),
            cursor=initial["cursor"],
            client_path=initial["path"],
        ).json()

    assert rebuilt["path"] != initial["path"]
    assert rebuilt["path"].endswith("rebuild-1")
    assert rebuilt["events"]


def test_sqlite_tool_result_patch_advances_once_from_change_cursor() -> None:
    with DualStackHarness(include_pending_overlay=False) as harness:
        initial = harness.session(flags=("session",)).json()
        old_cursor = SQLiteEventStore(harness.sqlite_path, migrate=False).cursor(
            harness.run_id
        ).change_cursor
        harness.append_tool_result_patch()
        first = harness.session(
            flags=("session",),
            cursor=old_cursor,
            client_path=initial["path"],
        ).json()
        second = harness.session(
            flags=("session",),
            cursor=first["cursor"],
            client_path=first["path"],
        ).json()

    assert first["cursor"] > old_cursor
    assert len(first["patches"]) == 1
    assert first["patches"][0]["id"] == 11
    assert first["patches"][0]["output"] == "patched child output"
    assert second["patches"] == []


def test_real_snapshot_does_not_mix_generations_during_rebuild_swaps() -> None:
    with DualStackHarness() as harness:
        reader = SQLiteEventStore(harness.sqlite_path, migrate=False)
        base_count = len(
            reader.read_session_snapshot(
                harness.run_id,
                source_class="live",
                after_cursor=0,
            ).events
        )
        failures: list[tuple[int, int]] = []
        stop = threading.Event()

        def swapper() -> None:
            for index in range(20):
                harness.rebuild_swap_variant(index % 2 == 0)
            stop.set()

        writer = threading.Thread(target=swapper)
        writer.start()
        while not stop.is_set():
            snapshot = reader.read_session_snapshot(
                harness.run_id,
                source_class="live",
                after_cursor=0,
            )
            generation = SQLiteSourceKey.parse(snapshot.source_key).rebuild_generation
            expected_count = base_count + (generation % 2)
            if len(snapshot.events) != expected_count:
                failures.append((generation, len(snapshot.events)))
        writer.join(timeout=10)

    assert not failures
