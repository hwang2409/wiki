from __future__ import annotations

import json
import threading

import pytest

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


def test_child_delta_ignores_sqlite_flag_in_pr4a() -> None:
    with DualStackHarness() as harness:
        legacy = harness.delta()
        flagged = harness.delta(flags=("delta",))

    assert legacy.status_code == 200
    assert flagged.status_code == 200
    legacy_payload = legacy.json()
    flagged_payload = flagged.json()
    assert [event["text"] for event in legacy_payload["events"]] == [
        "Inspect child-only branch for WIKI-282.",
        "child-only event",
    ]
    assert flagged_payload == legacy_payload


@pytest.mark.parametrize("flags", [(), ("session",), ("delta",), ("older",)])
def test_older_route_ignores_all_sqlite_flags(flags: tuple[str, ...]) -> None:
    with DualStackHarness() as harness:
        response = harness.request(
            "GET",
            f"/api/agents/{harness.ticket}/session/older",
            flags=flags,
            params={"before": 1000},
        )

    assert response.status_code == 200
    assert not response.json()["path"].startswith("sqlite://")


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
