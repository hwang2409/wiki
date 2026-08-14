from __future__ import annotations

from backend.app import main
from backend.tests.harness_dual_stack import DualStackHarness


def test_sqlite_source_key_round_trips_as_typed_data() -> None:
    key = main.SQLiteSourceKey("archive", "run-1", 12)
    encoded = key.format()
    decoded = main.SQLiteSourceKey.parse(encoded)

    assert decoded == key


def test_empty_index_not_ready_uses_real_legacy_artifacts() -> None:
    with DualStackHarness() as harness:
        harness.mark_materializer_not_ready()
        response = harness.palette("Legacy fixture")

    assert response.status_code == 200
    artifacts = {
        row["id"]: row
        for row in response.json()["results"]
        if row["kind"] == "artifact"
    }
    assert set(artifacts) == {"legacy-fixture-mermaid", "legacy-fixture-table"}


def test_populated_index_returns_real_materialized_fixture_rows() -> None:
    with DualStackHarness(include_index_artifact=True) as harness:
        response = harness.palette("Wiki.app architecture")

    assert response.status_code == 200
    artifacts = [
        row for row in response.json()["results"] if row["kind"] == "artifact"
    ]
    assert [row["id"] for row in artifacts] == [
        "6d0e7d00-2edf-4054-b0dc-fe17cd382c2a"
    ]
    assert artifacts[0]["title"] == "Wiki.app architecture"
