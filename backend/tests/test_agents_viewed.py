"""Unit tests for the session-list unread indicator plumbing (WIKI-147 R2)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest import mock

from fastapi import HTTPException

from backend.app import main


def _new_run_id() -> str:
    return str(uuid.uuid4())


def _iso(offset_seconds: float = 0.0) -> str:
    base = datetime(2026, 7, 22, 15, 30, tzinfo=timezone.utc).timestamp() + offset_seconds
    return datetime.fromtimestamp(base, tz=timezone.utc).isoformat()


class AgentsViewedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.registry = root / "agent-registry.json"
        self.status_dir = root / "status"
        self.archive_dir = root / "archive"
        self.runs_dir = root / "runs"
        self.viewed_path = root / "agent-viewed.json"
        self.deploy_marker = root / "deploy-timestamp.txt"
        self.status_dir.mkdir()
        self.archive_dir.mkdir()
        self.runs_dir.mkdir()
        self.registry.write_text("{}", encoding="utf-8")
        # Freeze the deploy cutoff so tests can classify runs deterministically
        # by choosing created_at either side of it.
        self.deploy_marker.write_text(_iso(0), encoding="utf-8")
        self.patches = [
            mock.patch.object(main, "AGENT_REGISTRY_PATH", self.registry),
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(main, "AGENT_ARCHIVE_DIR", self.archive_dir),
            mock.patch.object(main, "AGENT_VIEWED_PATH", self.viewed_path),
            mock.patch.object(main, "AGENT_RUNS_DIR", self.runs_dir),
            mock.patch.object(main, "AGENT_DEPLOY_MARKER_PATH", self.deploy_marker),
            mock.patch.object(main, "_DEPLOY_TIMESTAMP", None),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        main._DEPLOY_TIMESTAMP = None
        self.tmp.cleanup()

    def _write_run(
        self,
        run_id: str,
        *,
        seq: int,
        updated_at: str,
        created_at: str | None = None,
    ) -> None:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "run_id": run_id,
            "normalized_event_count": seq,
            "updated_at": updated_at,
        }
        if created_at is not None:
            payload["created_at"] = created_at
        (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")

    def _seed_worker(
        self,
        ticket: str,
        *,
        run_id: str,
        seq: int,
        updated_at: str,
        created_at: str | None = None,
        state: str = "working",
    ) -> None:
        self._write_run(run_id, seq=seq, updated_at=updated_at, created_at=created_at)
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry[ticket] = {
            "history": [],
            "current": {
                "role": "implement",
                "kind": "cc",
                "spawned_at": "2026-07-20T09:00:00+00:00",
                "run_id": run_id,
            },
        }
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        status_path = self.status_dir / f"{ticket}.json"
        status_path.write_text(
            json.dumps({"state": state, "pr": None, "step": "editing", "blocker": None}),
            encoding="utf-8",
        )

    def test_agents_payload_uses_run_json_freshness(self) -> None:
        run_id = _new_run_id()
        self._seed_worker(
            "WIKI-42",
            run_id=run_id,
            seq=7,
            updated_at=_iso(0),
            created_at=_iso(-60),
        )

        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-42"
        )
        self.assertEqual(worker["run_id"], run_id)
        self.assertEqual(worker["latest_event_at"], _iso(0))
        self.assertEqual(worker["latest_event_seq"], 7)
        # Pre-deploy runs classify as viewed at their current seq — no dot on
        # first paint of legacy sessions.
        self.assertEqual(worker["last_viewed_seq"], 7)

    def test_new_session_created_after_deploy_shows_unread(self) -> None:
        # deploy_marker = _iso(0). A run stamped 60s later is post-deploy.
        run_id = _new_run_id()
        self._seed_worker(
            "WIKI-77",
            run_id=run_id,
            seq=3,
            updated_at=_iso(60),
            created_at=_iso(60),
        )

        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-77"
        )
        self.assertEqual(worker["latest_event_seq"], 3)
        self.assertIsNone(worker["last_viewed_seq"])
        self.assertIsNone(worker["last_viewed_at"])

    def test_pre_deploy_runs_classified_as_viewed_without_mutating_store(self) -> None:
        pre_run = _new_run_id()
        self._seed_worker(
            "WIKI-88",
            run_id=pre_run,
            seq=5,
            updated_at=_iso(0),
            created_at=_iso(-60),
        )
        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-88"
        )
        # Pre-deploy: classified as viewed at the current durable seq — no dot.
        self.assertEqual(worker["last_viewed_seq"], 5)
        # Classification is inline; the persistent viewed store is untouched.
        self.assertFalse(self.viewed_path.exists())

        # New post-deploy run — must NOT be added to viewed store either.
        new_run = _new_run_id()
        self._seed_worker(
            "WIKI-89",
            run_id=new_run,
            seq=2,
            updated_at=_iso(120),
            created_at=_iso(120),
        )
        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-89"
        )
        self.assertIsNone(worker["last_viewed_seq"])
        self.assertFalse(self.viewed_path.exists())

    def test_run_created_after_deploy_before_first_request_shows_unread(self) -> None:
        # The exact WIKI-147 R2 B1 regression: run materializes AFTER deploy
        # but BEFORE the first /api/agents request. Must not be backfilled.
        run_id = _new_run_id()
        self._seed_worker(
            "WIKI-78",
            run_id=run_id,
            seq=1,
            updated_at=_iso(5),
            created_at=_iso(1),
        )
        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-78"
        )
        self.assertEqual(worker["latest_event_seq"], 1)
        self.assertIsNone(worker["last_viewed_seq"])
        self.assertIsNone(worker["last_viewed_at"])

    def test_pre_deploy_baseline_frozen_on_first_observation(self) -> None:
        # WIKI-147 R4 B1: pre-deploy run baselines at the first observed seq.
        # Later events (seq > baseline_seq) must render as unread instead of
        # being classified as viewed forever.
        run_id = _new_run_id()
        self._seed_worker(
            "WIKI-93",
            run_id=run_id,
            seq=5,
            updated_at=_iso(0),
            created_at=_iso(-60),
        )

        first = main.agents()
        first_worker = next(
            row for row in cast(list[dict[str, Any]], first["workers"])
            if row["ticket"] == "WIKI-93"
        )
        self.assertEqual(first_worker["latest_event_seq"], 5)
        self.assertEqual(first_worker["last_viewed_seq"], 5)

        # New durable event lands — seq advances, baseline stays.
        self._write_run(run_id, seq=6, updated_at=_iso(30), created_at=_iso(-60))

        second = main.agents()
        second_worker = next(
            row for row in cast(list[dict[str, Any]], second["workers"])
            if row["ticket"] == "WIKI-93"
        )
        self.assertEqual(second_worker["latest_event_seq"], 6)
        self.assertEqual(second_worker["last_viewed_seq"], 5)

        # Baseline sidecar was frozen at the FIRST observation (seq=5) and
        # was not recomputed on the second request.
        baseline = json.loads(
            (self.runs_dir / run_id / "viewed-baseline.json").read_text(encoding="utf-8")
        )
        self.assertEqual(baseline["seq"], 5)

    def test_startup_snapshot_freezes_baseline_before_first_request(self) -> None:
        # WIKI-147 R5 B1: baselines must be frozen at boot, not at first
        # /api/agents request. Simulate a pre-deploy run, run the startup
        # snapshot, then let a durable event advance seq — the event must
        # render as unread instead of being classified as viewed.
        run_id = _new_run_id()
        self._seed_worker(
            "WIKI-147-B1",
            run_id=run_id,
            seq=5,
            updated_at=_iso(0),
            created_at=_iso(-60),
        )

        # Boot-time snapshot: no requests have been served yet.
        main._snapshot_startup_baselines()

        # Event arrives BEFORE the first request. Under the pre-R5 code path
        # this would be classified as viewed (baseline == latest at first
        # request). Under R5 the baseline was frozen at boot at seq=5.
        self._write_run(run_id, seq=8, updated_at=_iso(30), created_at=_iso(-60))

        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-147-B1"
        )
        self.assertEqual(worker["latest_event_seq"], 8)
        self.assertEqual(worker["last_viewed_seq"], 5)

        baseline = json.loads(
            (self.runs_dir / run_id / "viewed-baseline.json").read_text(encoding="utf-8")
        )
        self.assertEqual(baseline["seq"], 5)

    def test_startup_snapshot_skips_post_deploy_runs(self) -> None:
        # Runs created at/after the deploy cutoff must NOT be baselined at
        # startup — their events should render as unread naturally.
        run_id = _new_run_id()
        self._seed_worker(
            "WIKI-147-B1-POST",
            run_id=run_id,
            seq=2,
            updated_at=_iso(120),
            created_at=_iso(60),
        )

        main._snapshot_startup_baselines()

        self.assertFalse(
            (self.runs_dir / run_id / "viewed-baseline.json").exists(),
            "post-deploy runs must not have a baseline written at startup",
        )
        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-147-B1-POST"
        )
        self.assertIsNone(worker["last_viewed_seq"])

    def test_baseline_writes_leave_no_partial_files_on_concurrent_write(self) -> None:
        # WIKI-147 R5 B1: the atomic tempfile+os.replace path must ensure a
        # concurrent reader either sees a fully-formed sidecar or no file at
        # all — never partial JSON. Drive many parallel writers and readers
        # against the same run and verify no reader ever decodes garbage.
        run_id = _new_run_id()
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        errors: list[str] = []
        stop = threading.Event()

        def writer(seq: int) -> None:
            path = run_dir / "viewed-baseline.json"
            for _ in range(50):
                # Force a fresh write each iteration by unlinking the sidecar
                # and reinvoking the atomic write path.
                path.unlink(missing_ok=True)
                try:
                    main._ensure_run_baseline(run_id, _iso(seq), seq)
                except Exception as exc:  # pragma: no cover - defensive
                    errors.append(f"writer{seq}: {exc!r}")

        def reader() -> None:
            path = run_dir / "viewed-baseline.json"
            while not stop.is_set():
                try:
                    raw = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    errors.append(f"reader: {exc!r}")
                    continue
                try:
                    payload = json.loads(raw)
                except ValueError as exc:
                    errors.append(f"reader partial-json: {exc!r}: {raw!r}")
                    continue
                if not isinstance(payload, dict) or "seq" not in payload or "at" not in payload:
                    errors.append(f"reader partial-payload: {payload!r}")

        writers = [threading.Thread(target=writer, args=(seq,)) for seq in range(1, 5)]
        readers = [threading.Thread(target=reader) for _ in range(4)]
        for thread in readers + writers:
            thread.start()
        for thread in writers:
            thread.join()
        stop.set()
        for thread in readers:
            thread.join()

        self.assertFalse(errors, f"partial-file race observed: {errors[:5]}")

        # No temp files should be left behind after successful writes.
        leftovers = [
            child.name
            for child in run_dir.iterdir()
            if child.name.startswith(".viewed-baseline.json.")
        ]
        self.assertFalse(leftovers, f"tempfile leftovers not cleaned: {leftovers}")

    def test_concurrent_first_baseline_writers_agree(self) -> None:
        # WIKI-147 R6 B1: two concurrent first-time baseline writers for the
        # same run must be serialized by the per-run lock so exactly one wins
        # and the late arriver returns the persisted snapshot on recheck.
        # Without the lock, both writers race past the existence probe and
        # can overwrite each other, silently advancing the supposedly frozen
        # baseline past the winner's seq.
        run_id = _new_run_id()
        (self.runs_dir / run_id).mkdir(parents=True, exist_ok=True)

        results: list[tuple[str, int]] = []
        errors: list[str] = []
        gate = threading.Barrier(2)

        def call(at_offset: int, seq: int) -> None:
            gate.wait(timeout=5)
            try:
                results.append(main._ensure_run_baseline(run_id, _iso(at_offset), seq))
            except Exception as exc:
                errors.append(repr(exc))

        threads = [
            threading.Thread(target=call, args=(10, 3)),
            threading.Thread(target=call, args=(20, 7)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors, f"writer errors: {errors}")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1], "concurrent writers disagreed")

        stored_at, stored_seq = main._load_run_baseline(run_id)
        self.assertEqual((stored_at, stored_seq), results[0])
        self.assertIn(stored_seq, (3, 7))

        # A subsequent call with a *different* (at, seq) MUST return the frozen
        # snapshot, not the new args. This guards against the pre-lock bug where
        # a late arriver would re-write and silently advance the baseline.
        later = main._ensure_run_baseline(run_id, _iso(99), 99)
        self.assertEqual(later, (stored_at, stored_seq))

    def test_deploy_marker_persists_across_reboot(self) -> None:
        run_id = _new_run_id()
        self._seed_worker(
            "WIKI-79",
            run_id=run_id,
            seq=1,
            updated_at=_iso(5),
            created_at=_iso(5),
        )
        first = main.agents()
        first_worker = next(
            row for row in cast(list[dict[str, Any]], first["workers"])
            if row["ticket"] == "WIKI-79"
        )
        self.assertIsNone(first_worker["last_viewed_seq"])
        # Reset the in-memory cache to simulate a supervisor reboot. The
        # persisted marker file must keep the SAME cutoff so the run stays
        # classified as post-deploy.
        main._DEPLOY_TIMESTAMP = None
        second = main.agents()
        second_worker = next(
            row for row in cast(list[dict[str, Any]], second["workers"])
            if row["ticket"] == "WIKI-79"
        )
        self.assertIsNone(second_worker["last_viewed_seq"])

    def test_mark_viewed_returns_404_for_unknown_run(self) -> None:
        with self.assertRaises(HTTPException) as bad:
            main.mark_run_viewed(_new_run_id(), main.MarkViewedBody())
        self.assertEqual(bad.exception.status_code, 404)

    def test_mark_viewed_rejects_bad_run_id(self) -> None:
        with self.assertRaises(HTTPException) as bad:
            main.mark_run_viewed("not-a-uuid", main.MarkViewedBody())
        self.assertEqual(bad.exception.status_code, 400)

    def test_mark_viewed_stores_run_id_and_seq(self) -> None:
        run_id = _new_run_id()
        self._seed_worker("WIKI-90", run_id=run_id, seq=10, updated_at=_iso(0))

        result = main.mark_run_viewed(run_id, main.MarkViewedBody(seq=10))
        self.assertEqual(result["last_viewed_seq"], 10)
        stored = json.loads(self.viewed_path.read_text(encoding="utf-8"))
        self.assertEqual(stored[run_id]["seq"], 10)

    def test_mark_viewed_monotonic(self) -> None:
        run_id = _new_run_id()
        self._seed_worker("WIKI-91", run_id=run_id, seq=10, updated_at=_iso(0))
        main.mark_run_viewed(run_id, main.MarkViewedBody(seq=10))
        # Older seq must not overwrite newer.
        result = main.mark_run_viewed(run_id, main.MarkViewedBody(seq=5))
        self.assertEqual(result["last_viewed_seq"], 10)
        stored = json.loads(self.viewed_path.read_text(encoding="utf-8"))
        self.assertEqual(stored[run_id]["seq"], 10)

    def test_mark_viewed_clamps_to_server_observed_seq(self) -> None:
        run_id = _new_run_id()
        self._seed_worker("WIKI-92", run_id=run_id, seq=4, updated_at=_iso(0))
        # Client claims a higher seq than the server has observed.
        result = main.mark_run_viewed(run_id, main.MarkViewedBody(seq=100))
        self.assertEqual(result["last_viewed_seq"], 4)

    def test_concurrent_writes_both_persist(self) -> None:
        run_a = _new_run_id()
        run_b = _new_run_id()
        self._seed_worker("WIKI-100", run_id=run_a, seq=3, updated_at=_iso(0))
        self._seed_worker("WIKI-101", run_id=run_b, seq=4, updated_at=_iso(0))

        def _mark(run_id: str, seq: int) -> None:
            for _ in range(20):
                main.mark_run_viewed(run_id, main.MarkViewedBody(seq=seq))

        threads = [
            threading.Thread(target=_mark, args=(run_a, 3)),
            threading.Thread(target=_mark, args=(run_b, 4)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stored = json.loads(self.viewed_path.read_text(encoding="utf-8"))
        self.assertEqual(stored[run_a]["seq"], 3)
        self.assertEqual(stored[run_b]["seq"], 4)

    def test_new_event_after_view_makes_row_unread(self) -> None:
        run_id = _new_run_id()
        self._seed_worker("WIKI-99", run_id=run_id, seq=2, updated_at=_iso(0))
        main.mark_run_viewed(run_id, main.MarkViewedBody(seq=2))
        # Real durable event lands — seq + updated_at advance.
        self._write_run(run_id, seq=3, updated_at=_iso(60))

        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-99"
        )
        self.assertEqual(worker["latest_event_seq"], 3)
        self.assertEqual(worker["last_viewed_seq"], 2)

    def test_viewed_state_survives_reboot(self) -> None:
        run_id = _new_run_id()
        self._seed_worker("WIKI-110", run_id=run_id, seq=6, updated_at=_iso(0))
        main.mark_run_viewed(run_id, main.MarkViewedBody(seq=6))
        stored_before = json.loads(self.viewed_path.read_text(encoding="utf-8"))
        # "Reboot" — re-read via a fresh agents() call.
        payload = main.agents()
        worker = next(
            row for row in cast(list[dict[str, Any]], payload["workers"])
            if row["ticket"] == "WIKI-110"
        )
        self.assertEqual(worker["last_viewed_seq"], 6)
        stored_after = json.loads(self.viewed_path.read_text(encoding="utf-8"))
        # Migration marker preserved and file unchanged apart from that.
        self.assertEqual(stored_before[run_id], stored_after[run_id])


if __name__ == "__main__":
    unittest.main()
