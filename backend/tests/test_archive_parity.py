from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.archive_parity import (
    backfill_headless_runs,
    compare_run_parity,
    compare_run_boundaries,
    harness_error_fallback_path,
    run_parity_batch,
)
from backend.app.agent_runtime.archive_protocol import archive_is_committed
from backend.app.agent_runtime.event_store import (
    EventReducerAdapter,
    NORMALIZER_VERSION,
    SQLiteEventStore,
)
from backend.app.agent_runtime.normalizer import (
    NormalizedProviderEvent,
    normalize_provider_event,
)
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.types import (
    EventDisposition,
    LifecycleState,
    ProviderKind,
    RunRecord,
)


def _paths(root: Path) -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=root / "runtime",
        socket_path=root / "runtime" / "supervisor.sock",
        registry_path=root / "registry.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )


def _run_with_one_event(root: Path, *, session_id: str | None = "session") -> tuple[
    RunStore,
    SQLiteEventStore,
    RunRecord,
]:
    store = RunStore(_paths(root))
    record = store.create(
        RunRecord.new(
            agent_id="WIKI-282-PARITY",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture",
            worktree=str(root),
            prompt="parity",
        )
    )
    record.provider_session_id = session_id
    store._write_record(record)  # noqa: SLF001
    payload = {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "userMessage",
                "id": "user-1",
                "content": [{"type": "text", "text": "hello"}],
            }
        },
    }
    raw = store.append_raw(
        record.run_id,
        provider="codex",
        direction="provider",
        payload=payload,
    )
    normalized = normalize_provider_event(ProviderKind.CODEX, payload)
    legacy = store.append_normalized(
        record.run_id,
        raw_seq=int(raw["seq"]),
        disposition=normalized.disposition,
        kind=normalized.kind,
        payload=normalized.payload,
        lifecycle_state=normalized.lifecycle_state,
    )
    event_store = SQLiteEventStore(root / "runtime" / "events.sqlite3")
    event_store.create_run(
        record.run_id,
        agent_id=record.agent_id,
        provider=record.provider,
        created_at=record.created_at,
        state=record.state,
    )
    reducer = EventReducerAdapter(record.provider)
    materialize_raw = {**raw, "normalized_seq": int(legacy["seq"])}
    event_store.materialize(
        record.run_id,
        materialize_raw,
        reducer,
        normalized=normalized,
    )
    return store, event_store, record


def _run_with_normalized_gap(root: Path) -> tuple[RunStore, SQLiteEventStore, RunRecord]:
    store = RunStore(_paths(root))
    record = store.create(
        RunRecord.new(
            agent_id="WIKI-282-ORDER",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture",
            worktree=str(root),
            prompt="ordering",
        )
    )
    payloads = [
        {"method": "warning", "params": {"message": "first"}},
        {"method": "warning", "params": {"message": "second"}},
    ]
    raw_rows = [
        store.append_raw(
            record.run_id,
            provider="codex",
            direction="provider",
            payload=payload,
        )
        for payload in payloads
    ]
    normalized_rows = []
    for sequence, raw in zip((2, 1), raw_rows, strict=True):
        normalized = normalize_provider_event(ProviderKind.CODEX, raw["payload"])
        normalized_rows.append(
            {
                "seq": sequence,
                "raw_seq": int(raw["seq"]),
                "normalized_at": raw["received_at"],
                "disposition": normalized.disposition.value,
                "kind": normalized.kind,
                "payload": normalized.payload,
                "lifecycle_state": None,
            }
        )
    normalized_rows.reverse()
    store.normalized_events_path(record.run_id).write_text(
        "".join(
            json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n"
            for row in normalized_rows
        ),
        encoding="utf-8",
    )
    record.normalized_event_count = 2
    store._write_record(record)  # noqa: SLF001
    event_store = SQLiteEventStore(root / "runtime" / "events.sqlite3")
    event_store.create_run(
        record.run_id,
        agent_id=record.agent_id,
        provider=record.provider,
        created_at=record.created_at,
        state=record.state,
    )
    reducer = EventReducerAdapter(record.provider)
    for raw, row in zip(raw_rows, normalized_rows[::-1], strict=True):
        normalized = NormalizedProviderEvent(
            EventDisposition.IGNORED,
            row["kind"],
            row["payload"],
        )
        event_store.materialize(
            record.run_id,
            {**raw, "normalized_seq": int(row["seq"])},
            reducer,
            normalized=normalized,
        )
    return store, event_store, record


def test_archive_export_is_byte_identical_and_marker_is_visibility_gate(tmp_path: Path) -> None:
    store, event_store, record = _run_with_one_event(tmp_path)
    store.set_archive_events_exporter(
        lambda run_id, destination, legacy_source: event_store.export_events_jsonl(
            run_id,
            destination,
            legacy_source=legacy_source,
        )
    )
    store.transition(record.run_id, LifecycleState.COMPLETED)
    legacy_bytes = store.normalized_events_path(record.run_id).read_bytes()

    archived, session_dir = store.archive_current(record.run_id, outcome="closed")

    assert archived.run_id == record.run_id
    assert archive_is_committed(session_dir)
    assert (session_dir / "events.jsonl").read_bytes() == legacy_bytes


def test_archive_export_orders_by_normalized_sequence(tmp_path: Path) -> None:
    store, event_store, record = _run_with_normalized_gap(tmp_path)
    destination = tmp_path / "archive-events.jsonl"

    assert event_store.export_events_jsonl(
        record.run_id,
        destination,
        legacy_source=store.normalized_events_path(record.run_id),
    )
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [(row["seq"], row["raw_seq"]) for row in rows] == [(1, 2), (2, 1)]


def test_parity_harness_records_divergent_event_with_run_and_version(tmp_path: Path) -> None:
    store, event_store, record = _run_with_one_event(tmp_path)
    first = compare_run_parity(store, event_store, record.run_id)
    assert first.mismatches

    with event_store.connection() as connection:
        connection.execute(
            "UPDATE events SET event_json = ? WHERE run_id = ? AND event_id = 0",
            (json.dumps({"id": 0, "kind": "divergent"}), record.run_id),
        )
    second = compare_run_parity(store, event_store, record.run_id)

    assert any(mismatch.path.startswith("events[0]") for mismatch in second.mismatches)
    records = event_store.parity_records(record.run_id)
    assert any(
        row["record_type"] == "mismatch"
        and row["normalizer_version"] == NORMALIZER_VERSION
        and row["run_id"] == record.run_id
        for row in records
    )
    assert all(row["raw_seq"] is not None for row in records if row["record_type"] == "mismatch")


def test_parity_harness_records_errors_and_continues_batch(tmp_path: Path) -> None:
    store, event_store, first = _run_with_one_event(tmp_path)
    second = store.create(
        RunRecord.new(
            agent_id="WIKI-282-PARITY-SECOND",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture",
            worktree=str(tmp_path),
            prompt="parity second",
        )
    )
    second.provider_session_id = "session-second"
    store._write_record(second)  # noqa: SLF001
    payload = {"method": "warning", "params": {"message": "second"}}
    raw = store.append_raw(
        second.run_id,
        provider="codex",
        direction="provider",
        payload=payload,
    )
    normalized = normalize_provider_event(ProviderKind.CODEX, payload)
    store.append_normalized(
        second.run_id,
        raw_seq=int(raw["seq"]),
        disposition=normalized.disposition,
        kind=normalized.kind,
        payload=normalized.payload,
        lifecycle_state=normalized.lifecycle_state,
    )
    event_store.create_run(
        second.run_id,
        agent_id=second.agent_id,
        provider=second.provider,
        created_at=second.created_at,
        state=second.state,
    )
    event_store.materialize(second.run_id, raw, EventReducerAdapter(second.provider), normalized=normalized)

    from backend.app.agent_runtime import archive_parity

    original = archive_parity.transcripts.read_session_delta
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("parser fixture failure")
        return original(*args, **kwargs)

    with mock.patch.object(
        archive_parity.transcripts,
        "read_session_delta",
        side_effect=fail_once,
    ):
        reports = run_parity_batch(store, event_store, [first.run_id, second.run_id])

    records = event_store.parity_records()
    assert any(row["run_id"] == first.run_id and row["record_type"] == "harness_error" for row in records)
    assert second.run_id in reports
    assert any(row["run_id"] == second.run_id for row in records)


def test_parity_harness_compares_raw_prefix_boundaries(tmp_path: Path) -> None:
    store, event_store, record = _run_with_one_event(tmp_path)
    with event_store.connection() as connection:
        connection.execute(
            "UPDATE events SET event_json = ? WHERE run_id = ? AND event_id = 0",
            (json.dumps({"id": 0, "kind": "divergent"}), record.run_id),
        )

    reports = compare_run_boundaries(store, event_store, record.run_id)

    assert any(report.boundary == "raw_prefix" for report in reports)
    assert any(mismatch.raw_seq == 1 for report in reports for mismatch in report.mismatches)


def test_backfill_skips_wrong_version_and_rebuild_state_and_records_both(
    tmp_path: Path,
) -> None:
    store = RunStore(_paths(tmp_path))
    wrong_version = store.create(
        RunRecord.new(
            agent_id="WIKI-282-WRONG-VERSION",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture",
            worktree=str(tmp_path),
            prompt="wrong version",
        )
    )
    wrong_version.provider_session_id = "session-old"
    store._write_record(wrong_version)  # noqa: SLF001
    store.transition(wrong_version.run_id, LifecycleState.COMPLETED)
    rebuild_needed = store.create(
        RunRecord.new(
            agent_id="WIKI-282-REBUILD-NEEDED",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture",
            worktree=str(tmp_path),
            prompt="rebuild needed",
        )
    )
    rebuild_needed.provider_session_id = "session-rebuild"
    store._write_record(rebuild_needed)  # noqa: SLF001
    store.transition(rebuild_needed.run_id, LifecycleState.COMPLETED)
    eligible = store.create(
        RunRecord.new(
            agent_id="WIKI-282-ELIGIBLE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture",
            worktree=str(tmp_path),
            prompt="eligible",
        )
    )
    eligible.provider_session_id = "session-ready"
    store._write_record(eligible)  # noqa: SLF001
    payload = {"method": "turn/started", "params": {"turn": {"id": "t"}}}
    raw = store.append_raw(
        eligible.run_id,
        provider="codex",
        direction="provider",
        payload=payload,
    )
    normalized = normalize_provider_event(ProviderKind.CODEX, payload)
    store.append_normalized(
        eligible.run_id,
        raw_seq=int(raw["seq"]),
        disposition=normalized.disposition,
        kind=normalized.kind,
        payload=normalized.payload,
        lifecycle_state=normalized.lifecycle_state,
    )
    store.transition(eligible.run_id, LifecycleState.COMPLETED)

    event_store = SQLiteEventStore(tmp_path / "runtime" / "events.sqlite3")
    event_store.create_run(
        wrong_version.run_id,
        agent_id=wrong_version.agent_id,
        provider=wrong_version.provider,
        created_at=wrong_version.created_at,
        state=wrong_version.state,
        normalizer_version="wiki-282-old",
    )
    event_store.create_run(
        rebuild_needed.run_id,
        agent_id=rebuild_needed.agent_id,
        provider=rebuild_needed.provider,
        created_at=rebuild_needed.created_at,
        state=rebuild_needed.state,
    )
    with event_store.connection() as connection:
        connection.execute(
            "UPDATE run_cursors SET rebuild_state = 'needed' WHERE run_id = ?",
            (rebuild_needed.run_id,),
        )

    results = backfill_headless_runs(store, event_store, batch_size=3)

    by_run = {result.run_id: result for result in results}
    assert by_run[wrong_version.run_id].status == "skipped"
    assert by_run[wrong_version.run_id].reason == "normalizer_version"
    assert by_run[rebuild_needed.run_id].status == "skipped"
    assert by_run[rebuild_needed.run_id].reason == "rebuild_state"
    assert by_run[eligible.run_id].status.startswith("ready")
    skipped = [row for row in event_store.parity_records() if row["record_type"] == "backfill_skipped"]
    assert {row["run_id"] for row in skipped} == {
        wrong_version.run_id,
        rebuild_needed.run_id,
    }


def _add_terminal_run(
    store: RunStore,
    event_store: SQLiteEventStore,
    *,
    index: int,
) -> RunRecord:
    record = store.create(
        RunRecord.new(
            agent_id=f"WIKI-282-BACKFILL-{index:03d}",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture",
            worktree=str(store.paths.runtime_dir.parent),
            prompt=f"backfill {index}",
        )
    )
    record.provider_session_id = f"session-{index}"
    store._write_record(record)  # noqa: SLF001
    payload = {"method": "warning", "params": {"message": str(index)}}
    raw = store.append_raw(
        record.run_id,
        provider="codex",
        direction="provider",
        payload=payload,
    )
    normalized = normalize_provider_event(ProviderKind.CODEX, payload)
    legacy = store.append_normalized(
        record.run_id,
        raw_seq=int(raw["seq"]),
        disposition=normalized.disposition,
        kind=normalized.kind,
        payload=normalized.payload,
        lifecycle_state=normalized.lifecycle_state,
    )
    event_store.create_run(
        record.run_id,
        agent_id=record.agent_id,
        provider=record.provider,
        created_at=record.created_at,
        state=record.state,
    )
    event_store.materialize(
        record.run_id,
        {**raw, "normalized_seq": int(legacy["seq"])},
        EventReducerAdapter(record.provider),
        normalized=normalized,
    )
    store.transition(record.run_id, LifecycleState.COMPLETED)
    return store.get(record.run_id)


def test_parity_batch_records_recorder_failure_and_continues(tmp_path: Path) -> None:
    store, event_store, first = _run_with_one_event(tmp_path)
    second = _add_terminal_run(store, event_store, index=2)
    store.transition(first.run_id, LifecycleState.COMPLETED)
    from backend.app.agent_runtime import archive_parity

    original_legacy_payload = archive_parity._legacy_payload_at_path  # noqa: SLF001

    calls = 0

    def corrupt_once(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("corrupt comparator input")
        return original_legacy_payload(*args, **kwargs)

    try:
        with (
            mock.patch.object(
                archive_parity,
                "_legacy_payload_at_path",
                side_effect=corrupt_once,
            ),
            mock.patch.object(
                event_store,
                "record_parity_record",
                side_effect=OSError("recorder unavailable"),
            ),
        ):
            reports = run_parity_batch(
                store,
                event_store,
                [first.run_id, second.run_id],
            )
    except OSError:
        reports = {}

    assert second.run_id in reports
    fallback = harness_error_fallback_path(event_store)
    assert fallback.is_file()
    fallback_rows = [json.loads(line) for line in fallback.read_text().splitlines()]
    assert any(
        row["run_id"] == first.run_id
        and row["record_type"] == "harness_error"
        and "corrupt comparator input" in row["detail"]["error"]
        and "recorder unavailable" in row["recording_error"]
        for row in fallback_rows
    )


def test_each_boundary_comparator_records_its_own_mismatch(tmp_path: Path) -> None:
    store, event_store, record = _run_with_one_event(tmp_path)
    from backend.app.agent_runtime import archive_parity

    comparators = (
        ("raw_prefix", "_compare_raw_prefixes", "_legacy_payload_at_path"),
        ("crash_boundary", "_compare_crash_boundary", "_sqlite_payload"),
        ("stale_cursor", "_compare_stale_cursor", "read_session_delta"),
        ("older_page", "_compare_older_page", "read_older_session"),
        ("patch_only_delta", "_compare_patch_only_delta", "read_session_delta"),
    )

    def no_op_boundary(*args: object, **kwargs: object) -> archive_parity.ParityReport:
        return archive_parity.ParityReport(
            run_id=str(args[2]),
            normalizer_version=NORMALIZER_VERSION,
            matched=True,
            mismatches=(),
        )

    for boundary, dispatch_name, target in comparators:
        with event_store.connection() as connection:
            connection.execute("DELETE FROM parity_records")
        with ExitStack() as stack:
            for _name, other_dispatch_name, _target in comparators:
                if other_dispatch_name == dispatch_name:
                    continue
                if other_dispatch_name == "_compare_raw_prefixes":
                    stack.enter_context(
                        mock.patch.object(
                            archive_parity,
                            other_dispatch_name,
                            return_value=(),
                        )
                    )
                else:
                    stack.enter_context(
                        mock.patch.object(
                            archive_parity,
                            other_dispatch_name,
                            side_effect=no_op_boundary,
                        )
                    )
            if boundary == "raw_prefix":
                stack.enter_context(mock.patch.object(
                archive_parity,
                target,
                return_value={"mismatch": boundary},
                ))
            elif boundary == "crash_boundary":
                original = archive_parity._sqlite_payload  # noqa: SLF001

                def divergent_payload(
                    source: SQLiteEventStore,
                    run_id: str,
                ) -> dict[str, object]:
                    payload = original(source, run_id)
                    if source.path.name == "replayed.sqlite3":
                        return {"mismatch": boundary}
                    return payload

                stack.enter_context(mock.patch.object(
                    archive_parity,
                    target,
                    side_effect=divergent_payload,
                ))
            elif boundary == "patch_only_delta":
                stack.enter_context(mock.patch.object(
                    archive_parity.transcripts,
                    target,
                    return_value={"patches": [{"mismatch": boundary}]},
                ))
            else:
                stack.enter_context(mock.patch.object(
                    archive_parity.transcripts,
                    target,
                    return_value={"mismatch": boundary},
                ))
            reports = compare_run_boundaries(store, event_store, record.run_id)

        assert any(
            report.boundary == boundary and report.mismatches
            for report in reports
        ), boundary
        assert any(
            row["record_type"] == "mismatch"
            and row["detail"]["boundary"] == boundary
            for row in event_store.parity_records(record.run_id)
        )


def test_backfill_cursor_moves_past_reeligible_completed_runs(tmp_path: Path) -> None:
    store = RunStore(_paths(tmp_path))
    event_store = SQLiteEventStore(tmp_path / "runtime" / "events.sqlite3")
    records = [_add_terminal_run(store, event_store, index=index) for index in range(64)]

    from backend.app.agent_runtime import archive_parity

    matched = archive_parity.ParityReport(
        run_id="fixture",
        normalizer_version=NORMALIZER_VERSION,
        matched=True,
        mismatches=(),
    )
    with (
        mock.patch.object(event_store, "backfill_completed_run_ids", return_value=set()),
        mock.patch.object(archive_parity, "compare_run_boundaries", return_value=(matched,)),
    ):
        first = backfill_headless_runs(store, event_store, batch_size=32)
        second = backfill_headless_runs(store, event_store, batch_size=32)

    assert len(first) == len(second) == 32
    assert {result.run_id for result in first}.isdisjoint(
        result.run_id for result in second
    )
    assert {result.run_id for result in first + second} == {
        record.run_id for record in records
    }


def test_backfill_skips_live_materializer_lock_then_processes_after_release(
    tmp_path: Path,
) -> None:
    store = RunStore(_paths(tmp_path))
    event_store = SQLiteEventStore(tmp_path / "runtime" / "events.sqlite3")
    record = _add_terminal_run(store, event_store, index=1)
    from backend.app.agent_runtime import archive_parity

    class LiveMaterializerLock:
        held = False

        def acquire(self, *, blocking: bool) -> bool:
            assert not blocking
            if self.held:
                return False
            self.held = True
            return True

        def release(self) -> None:
            self.held = False

        def __enter__(self) -> "LiveMaterializerLock":
            assert not self.held
            self.held = True
            return self

        def __exit__(self, *_args: object) -> None:
            self.release()

    matched = archive_parity.ParityReport(
        run_id=record.run_id,
        normalizer_version=NORMALIZER_VERSION,
        matched=True,
        mismatches=(),
    )
    live_lock = LiveMaterializerLock()
    with (
        mock.patch.object(event_store, "run_lock", return_value=live_lock),
        mock.patch.object(event_store, "replace_run_from"),
        mock.patch.object(archive_parity, "compare_run_boundaries", return_value=(matched,)),
    ):
        with live_lock:
            skipped = backfill_headless_runs(store, event_store, batch_size=1)

    assert len(skipped) == 1
    assert skipped[0].status == "skipped"
    assert skipped[0].reason == "locked"
    assert any(
        row["record_type"] == "backfill_skipped" and row["path"] == "locked"
        for row in event_store.parity_records(record.run_id)
    )


def test_backfill_releases_run_lock_before_boundary_compare(tmp_path: Path) -> None:
    # The boundary compare is O(events^2); holding the run lock across it
    # blocks a concurrent archive of the same run for the whole sweep
    # (2026-08-24 fleet write outage).
    store = RunStore(_paths(tmp_path))
    event_store = SQLiteEventStore(tmp_path / "runtime" / "events.sqlite3")
    record = _add_terminal_run(store, event_store, index=1)
    from backend.app.agent_runtime import archive_parity

    acquired_during_compare: list[bool] = []

    def probing_compare(*_args: object, **_kwargs: object) -> tuple:
        lock = event_store.run_lock(record.run_id)
        result: list[bool] = []

        def probe() -> None:
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
            result.append(acquired)

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=5)
        acquired_during_compare.append(bool(result and result[0]))
        return ()

    with mock.patch.object(
        archive_parity,
        "compare_run_boundaries",
        side_effect=probing_compare,
    ):
        results = backfill_headless_runs(store, event_store, batch_size=1)

    assert acquired_during_compare == [True]
    assert results[0].status == "ready"


def test_replace_run_from_fails_fast_when_run_lock_is_contended(
    tmp_path: Path,
) -> None:
    store = RunStore(_paths(tmp_path))
    event_store = SQLiteEventStore(tmp_path / "runtime" / "events.sqlite3")
    record = _add_terminal_run(store, event_store, index=1)
    from backend.app.agent_runtime.store import StoreConflict

    source = tmp_path / "rebuilt.sqlite3"
    source.write_bytes(b"")
    lock = event_store.run_lock(record.run_id)
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        lock.acquire()
        held.set()
        release.wait(timeout=10)
        lock.release()

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(timeout=5)
    try:
        raised: Exception | None = None
        try:
            event_store.replace_run_from(source, record.run_id, lock_timeout=0.2)
        except StoreConflict as exc:
            raised = exc
        assert isinstance(raised, StoreConflict)
    finally:
        release.set()
        thread.join(timeout=5)


def test_export_uses_cursor_gate_not_full_health_walk(tmp_path: Path) -> None:
    # WIKI-359: export must not pay run_is_healthy's O(events) json walk.
    store, event_store, record = _run_with_one_event(tmp_path)
    destination = tmp_path / "export.jsonl"
    with mock.patch.object(
        event_store,
        "run_is_healthy",
        side_effect=AssertionError("export must not call run_is_healthy"),
    ):
        assert event_store.export_events_jsonl(
            record.run_id,
            destination,
            legacy_source=store.normalized_events_path(record.run_id),
        )
    assert destination.is_file()


def test_export_refuses_disposition_coverage_gap(tmp_path: Path) -> None:
    store, event_store, record = _run_with_one_event(tmp_path)
    with event_store.connection() as connection:
        connection.execute(
            "DELETE FROM dispositions WHERE run_id = ?",
            (record.run_id,),
        )
    assert not event_store.export_events_jsonl(
        record.run_id,
        tmp_path / "export.jsonl",
        legacy_source=store.normalized_events_path(record.run_id),
    )
