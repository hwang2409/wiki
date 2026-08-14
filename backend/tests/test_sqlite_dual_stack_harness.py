from __future__ import annotations

import threading

import pytest

from backend.app.agent_runtime.event_store import SQLiteEventStore
from backend.app.main import SQLiteSourceKey
from backend.tests.harness_dual_stack import DualStackHarness


def _without_source(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "path"}


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

    assert _without_source(legacy_payload) == _without_source(sqlite_payload)


def test_dual_stack_harness_calls_real_delta_route() -> None:
    with DualStackHarness() as harness:
        legacy = harness.delta()
        sqlite = harness.delta(flags=("delta",))

    assert legacy.status_code == 200
    assert sqlite.status_code == 200
    assert legacy.json()["events"]
    assert sqlite.json()["path"].startswith("sqlite://")


@pytest.mark.parametrize(
    ("enabled_route", "unrelated_route"),
    (("session", "delta"), ("delta", "session")),
)
def test_route_flags_isolate_real_http_adapters(
    enabled_route: str,
    unrelated_route: str,
) -> None:
    with DualStackHarness() as harness:
        enabled = getattr(harness, enabled_route)(flags=(enabled_route,))
        unrelated = getattr(harness, unrelated_route)(flags=(enabled_route,))

    assert enabled.status_code == 200
    assert enabled.json()["path"].startswith("sqlite://")
    assert unrelated.status_code == 200
    assert not unrelated.json()["path"].startswith("sqlite://")


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
