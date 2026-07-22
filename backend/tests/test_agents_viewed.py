"""Unit tests for the session-list unread indicator plumbing (WIKI-147)."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest import mock

from fastapi import HTTPException

from backend.app import main


class AgentsViewedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.registry = root / "agent-registry.json"
        self.status_dir = root / "status"
        self.archive_dir = root / "archive"
        self.viewed_path = root / "agent-viewed.json"
        self.status_dir.mkdir()
        self.archive_dir.mkdir()
        self.registry.write_text("{}", encoding="utf-8")
        self.patches = [
            mock.patch.object(main, "AGENT_REGISTRY_PATH", self.registry),
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(main, "AGENT_ARCHIVE_DIR", self.archive_dir),
            mock.patch.object(main, "AGENT_VIEWED_PATH", self.viewed_path),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def _seed_worker(
        self,
        ticket: str,
        *,
        state: str = "working",
        status_time: float | None = None,
    ) -> Path:
        self.registry.write_text(
            json.dumps(
                {
                    ticket: {
                        "history": [],
                        "current": {
                            "role": "implement",
                            "kind": "cc",
                            "spawned_at": "2026-07-20T09:00:00+00:00",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        status_path = self.status_dir / f"{ticket}.json"
        status_path.write_text(
            json.dumps({"state": state, "pr": None, "step": "editing", "blocker": None}),
            encoding="utf-8",
        )
        if status_time is not None:
            os.utime(status_path, (status_time, status_time))
        return status_path

    def test_agents_payload_includes_latest_event_and_last_viewed(self) -> None:
        status_time = 1_800_000_000.0
        self._seed_worker("WIKI-42", status_time=status_time)
        self.viewed_path.write_text(
            json.dumps({"WIKI-42": "2026-07-19T09:00:00+00:00"}),
            encoding="utf-8",
        )

        payload = main.agents()
        workers = cast(list[dict[str, Any]], payload["workers"])
        worker = next(row for row in workers if row["ticket"] == "WIKI-42")

        expected_latest = datetime.fromtimestamp(status_time, tz=timezone.utc).isoformat()
        self.assertEqual(worker["latest_event_at"], expected_latest)
        self.assertEqual(worker["last_viewed_at"], "2026-07-19T09:00:00+00:00")

    def test_first_read_backfills_last_viewed_to_latest_event(self) -> None:
        status_time = 1_800_000_100.0
        self._seed_worker("WIKI-77", status_time=status_time)
        self.assertFalse(self.viewed_path.exists())

        payload = main.agents()
        workers = cast(list[dict[str, Any]], payload["workers"])
        worker = next(row for row in workers if row["ticket"] == "WIKI-77")

        expected_latest = datetime.fromtimestamp(status_time, tz=timezone.utc).isoformat()
        self.assertEqual(worker["latest_event_at"], expected_latest)
        self.assertEqual(worker["last_viewed_at"], expected_latest)
        stored = json.loads(self.viewed_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["WIKI-77"], expected_latest)

    def test_mark_viewed_endpoint_updates_stored_timestamp(self) -> None:
        self._seed_worker("WIKI-88", status_time=1_800_000_200.0)
        result = main.mark_agent_viewed("WIKI-88")

        self.assertIsInstance(result["last_viewed_at"], str)
        stored = json.loads(self.viewed_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["WIKI-88"], result["last_viewed_at"])

        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-88"
        )
        self.assertEqual(worker["last_viewed_at"], result["last_viewed_at"])

    def test_mark_viewed_rejects_bad_ticket(self) -> None:
        with self.assertRaises(HTTPException) as bad:
            main.mark_agent_viewed("invalid ticket")
        self.assertEqual(bad.exception.status_code, 400)

    def test_new_event_after_view_makes_row_unread(self) -> None:
        self._seed_worker("WIKI-99", status_time=1_800_000_300.0)
        main.mark_agent_viewed("WIKI-99")
        # Now a new status update lands with a fresher mtime.
        status_path = self.status_dir / "WIKI-99.json"
        newer = 1_800_000_400.0
        os.utime(status_path, (newer, newer))

        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-99"
        )
        latest = worker["latest_event_at"]
        viewed = worker["last_viewed_at"]
        self.assertIsNotNone(latest)
        self.assertIsNotNone(viewed)
        self.assertGreater(latest, viewed)


if __name__ == "__main__":
    unittest.main()
