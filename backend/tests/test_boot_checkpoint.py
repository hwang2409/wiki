"""WIKI-375: boot skips projection replay for clean and terminal runs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

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


def _seed_run(store: RunStore, root: Path, agent_id: str) -> RunRecord:
    record = store.create(
        RunRecord.new(
            agent_id=agent_id,
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture",
            worktree=str(root),
            prompt="checkpoint",
        )
    )
    raw = store.append_raw(
        record.run_id,
        provider="codex",
        direction="provider",
        payload={"method": "turn/started", "params": {"turn": {"id": "t-1"}}},
    )
    store.append_normalized(
        record.run_id,
        raw_seq=int(raw["seq"]),
        disposition=EventDisposition.RENDERED,
        kind="turn_started",
        payload={"turn": {"id": "t-1"}},
    )
    return store.get(record.run_id)


def _append_untracked_normalized_line(store: RunStore, run_id: str) -> None:
    """Simulate a crash between the JSONL fsync and the run.json write."""

    path = store.normalized_events_path(run_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "seq": 2,
                    "raw_seq": 2,
                    "kind": "turn_completed",
                    "disposition": "rendered",
                    "payload": {},
                }
            )
            + "\n"
        )


def test_clean_restart_skips_projection_rebuild(tmp_path: Path) -> None:
    store = RunStore(_paths(tmp_path))
    record = _seed_run(store, tmp_path, "WIKI-375-CLEAN")

    with mock.patch.object(
        RunStore,
        "rebuild_projections_from_normalized",
        autospec=True,
    ) as rebuild_spy:
        restarted = RunStore(_paths(tmp_path))
    rebuilt_ids = [call.args[1] for call in rebuild_spy.call_args_list]
    assert record.run_id not in rebuilt_ids
    assert restarted.get(record.run_id).raw_event_count == 1


def test_restart_rebuilds_after_untracked_jsonl_append(tmp_path: Path) -> None:
    store = RunStore(_paths(tmp_path))
    record = _seed_run(store, tmp_path, "WIKI-375-CRASHED")
    _append_untracked_normalized_line(store, record.run_id)

    with mock.patch.object(
        RunStore,
        "rebuild_projections_from_normalized",
        autospec=True,
    ) as rebuild_spy:
        RunStore(_paths(tmp_path))
    rebuilt_ids = [call.args[1] for call in rebuild_spy.call_args_list]
    assert record.run_id in rebuilt_ids


def test_clean_terminal_run_skips_rebuild_but_dirty_one_reconciles(
    tmp_path: Path,
) -> None:
    # Terminal runs still repair at boot when their logs outran run.json —
    # retention pruning must never discard unnormalized raw events. Clean
    # terminal runs skip via the same size checkpoint as live runs.
    store = RunStore(_paths(tmp_path))
    clean = _seed_run(store, tmp_path, "WIKI-375-DONE-CLEAN")
    store.transition(clean.run_id, LifecycleState.COMPLETED, reason="done")
    dirty = _seed_run(store, tmp_path, "WIKI-375-DONE-DIRTY")
    store.transition(dirty.run_id, LifecycleState.COMPLETED, reason="done")
    _append_untracked_normalized_line(store, dirty.run_id)

    with mock.patch.object(
        RunStore,
        "rebuild_projections_from_normalized",
        autospec=True,
    ) as rebuild_spy:
        RunStore(_paths(tmp_path))
    rebuilt_ids = [call.args[1] for call in rebuild_spy.call_args_list]
    assert clean.run_id not in rebuilt_ids
    assert dirty.run_id in rebuilt_ids
