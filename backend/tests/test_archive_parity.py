from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.archive_parity import (
    backfill_headless_runs,
    compare_run_parity,
    compare_run_boundaries,
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

    results = backfill_headless_runs(store, event_store, batch_size=1)

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
