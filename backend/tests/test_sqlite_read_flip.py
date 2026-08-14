from __future__ import annotations

from backend.app import main, palette


def test_sqlite_source_key_round_trips_as_typed_data() -> None:
    key = main.SQLiteSourceKey("archive", "run-1", 12)
    encoded = key.format()
    decoded = main.SQLiteSourceKey.parse(encoded)

    assert decoded == key


def test_archived_palette_artifact_link_keeps_run_identity() -> None:
    items = palette.collect_artifact_items_from_index(
        type(
            "Index",
            (),
            {
                "read_artifact_events": lambda self: [
                    (
                        "run-1",
                        {
                            "kind": "artifact",
                            "id": "artifact-1",
                            "title": "artifact",
                            "artifact": {"kind": "image"},
                        },
                    )
                ]
            },
        )(),
        archive_by_run={
            "run-1": ("WIKI-282", "2026-08-13T12:00:00+00:00")
        },
    )

    assert len(items) == 1
    assert "run_id=run-1" in items[0].url
    assert "archived_at=2026-08-13T12%3A00%3A00%2B00%3A00" in items[0].url


def test_palette_keeps_legacy_scan_fallback_when_index_is_unavailable() -> None:
    class BrokenIndex:
        def artifact_items(self) -> list[dict[str, object]]:
            raise RuntimeError("index unavailable")

    assert palette.collect_artifact_items_from_index(BrokenIndex()) is None
