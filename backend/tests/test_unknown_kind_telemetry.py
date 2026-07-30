from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.store import RuntimePaths
from backend.app.agent_runtime import unknown_kind_telemetry as telemetry_module
from backend.app.agent_runtime.unknown_kind_telemetry import (
    MAX_EVENT_LINE_BYTES,
    MAX_SCHEDULE_DELAY_SECONDS,
    UnknownKindTelemetry,
    _todo_runner,
)


class UnknownKindTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = RuntimePaths(
            runtime_dir=root / "runtime",
            socket_path=root / "runtime" / "supervisor.sock",
            registry_path=root / "registry.json",
            archive_dir=root / "archive",
        )
        self.raw = self.paths.runs_dir / "run-1" / "raw.jsonl"
        self.raw.parent.mkdir(parents=True)
        self.calls: list[str] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _append(self, count: int, *, method: str = "item/novel") -> None:
        with self.raw.open("a", encoding="utf-8") as handle:
            self._write_events(handle, count, method)

    @staticmethod
    def _write_events(handle, count: int, method: str = "item/novel") -> None:
        for _ in range(count):
            handle.write(
                json.dumps(
                    {
                        "seq": 1,
                        "provider": "codex",
                        "direction": "provider",
                        "payload": {"method": method, "params": {}},
                    }
                )
                + "\n"
            )

    def _write_run(self, run_name: str, count: int) -> None:
        path = self.paths.runs_dir / run_name / "raw.jsonl"
        path.parent.mkdir(parents=True)
        with path.open("w", encoding="utf-8") as handle:
            self._write_events(handle, count)

    def _write_timestamped_run(
        self, run_name: str, count: int, received_at: str
    ) -> None:
        path = self.paths.runs_dir / run_name / "raw.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for _ in range(count):
                handle.write(
                    json.dumps(
                        {
                            "received_at": received_at,
                            "provider": "codex",
                            "direction": "provider",
                            "payload": {
                                "method": "item/novel",
                                "params": {},
                            },
                        }
                    )
                    + "\n"
                )

    def _service(self) -> UnknownKindTelemetry:
        return UnknownKindTelemetry(
            self.paths,
            threshold=100,
            todo_runner=self.calls.append,
            clock=lambda: 1_759_000_000,
        )

    def test_aggregates_threshold_and_deduplicates_across_restart(self) -> None:
        self._append(60)
        self._write_run("run-2", 41)
        service = self._service()

        first = service.run_once()
        second = service.run_once()
        restarted = self._service()
        third = restarted.run_once()

        self.assertEqual(first["unknown_counts"], {"item/novel": 101})
        self.assertEqual(second["scanned_events"], 0)
        self.assertEqual(third["scanned_events"], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('unknown provider event kind "item/novel"', self.calls[0])

    def test_cursor_persistence_scans_only_new_rows_after_restart(self) -> None:
        self._append(50)
        service = self._service()
        initial = service.run_once()
        self._append(51)

        restarted = self._service()
        result = restarted.run_once()

        self.assertEqual(initial["scanned_events"], 50)
        self.assertEqual(result["scanned_events"], 51)
        self.assertEqual(result["unknown_counts"], {"item/novel": 101})
        self.assertEqual(len(self.calls), 1)

    def test_known_classification_blocks_unknown_kind_todo(self) -> None:
        with self.raw.open("w", encoding="utf-8") as handle:
            for _ in range(101):
                handle.write(
                    json.dumps(
                        {
                            "provider": "claude",
                            "direction": "provider",
                            "payload": {"type": "attachment", "attachment": {}},
                        }
                    )
                    + "\n"
                )
        with self.raw.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "provider": "claude",
                        "direction": "provider",
                        "payload": {
                            "type": "attachment",
                            "attachment": {"type": "task_reminder"},
                        },
                    }
                )
                + "\n"
            )
        result = self._service().run_once()

        self.assertEqual(result["unknown_counts"], {"claude_attachment": 101})
        self.assertEqual(result["filed_kinds"], [])
        self.assertEqual(len(self.calls), 0)

    def test_cli_failure_retries_pending_todo_after_restart(self) -> None:
        self._append(101)
        attempts = 0

        def flaky_runner(text: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("vault unavailable")
            self.calls.append(text)

        first = UnknownKindTelemetry(
            self.paths,
            threshold=100,
            todo_runner=flaky_runner,
            clock=lambda: 1_759_000_000,
        )
        with mock.patch.object(telemetry_module.logger, "exception"):
            first.run_once()
        failed_state = json.loads(first.state_path.read_text(encoding="utf-8"))
        self.assertEqual(failed_state["filed_kinds"], [])
        self.assertIn("item/novel", failed_state["pending_kinds"])

        second = UnknownKindTelemetry(
            self.paths,
            threshold=100,
            todo_runner=flaky_runner,
            clock=lambda: 1_759_000_000,
        )
        result = second.run_once()

        self.assertEqual(attempts, 2)
        self.assertEqual(result["filed_kinds"], ["item/novel"])
        self.assertEqual(len(self.calls), 1)

    def test_counts_use_event_received_week(self) -> None:
        self._write_timestamped_run("run-1", 60, "2026-07-20T01:00:00Z")
        self._write_timestamped_run("run-2", 60, "2026-07-27T01:00:00Z")
        service = UnknownKindTelemetry(
            self.paths,
            threshold=100,
            todo_runner=self.calls.append,
            clock=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc).timestamp(),
        )

        result = service.run_once()

        self.assertEqual(
            result["unknown_counts_by_week"],
            {
                "2026-07-20": {"item/novel": 60},
                "2026-07-27": {"item/novel": 60},
            },
        )
        self.assertEqual(self.calls, [])

    def test_archived_run_tail_counts_after_live_directory_is_removed(self) -> None:
        self._append(60)
        service = self._service()
        service.run_once()

        self._append(41)
        archive_raw = self.paths.archive_dir / "WIKI-1" / "session" / "raw.jsonl"
        archive_raw.parent.mkdir(parents=True)
        shutil.copy2(self.raw, archive_raw)
        (archive_raw.parent / "run.json").write_text(
            json.dumps({"run_id": "run-1"}), encoding="utf-8"
        )
        (archive_raw.parent / telemetry_module.ARCHIVE_COMPLETION_MARKER).write_text(
            json.dumps({"run_id": "run-1"}), encoding="utf-8"
        )
        shutil.rmtree(self.raw.parent)

        result = service.run_once()
        later = service.run_once()

        self.assertEqual(result["unknown_counts"], {"item/novel": 101})
        self.assertEqual(result["filed_kinds"], ["item/novel"])
        self.assertEqual(later["scanned_runs"], 0)

    def test_partial_archive_is_ignored_until_completion_marker(self) -> None:
        archive_dir = self.paths.archive_dir / "WIKI-1" / "session"
        archive_dir.mkdir(parents=True)
        archive_raw = archive_dir / "raw.jsonl"
        with archive_raw.open("w", encoding="utf-8") as handle:
            self._write_events(handle, 170)
        (archive_dir / "run.json").write_text(
            json.dumps({"run_id": "archived-1"}), encoding="utf-8"
        )
        service = self._service()

        during_copy = service.run_once()
        state = json.loads(service.state_path.read_text(encoding="utf-8"))
        self.assertEqual(during_copy["scanned_events"], 0)
        self.assertNotIn("archived-1", state["completed_runs"])

        with archive_raw.open("a", encoding="utf-8") as handle:
            self._write_events(handle, 31)
        (archive_dir / telemetry_module.ARCHIVE_COMPLETION_MARKER).write_text(
            json.dumps({"run_id": "archived-1"}), encoding="utf-8"
        )

        published = service.run_once()

        self.assertEqual(published["unknown_counts"], {"item/novel": 201})
        self.assertEqual(published["filed_kinds"], ["item/novel"])
        self.assertIn(
            "archived-1", json.loads(service.state_path.read_text())["completed_runs"]
        )

    def test_archive_landing_after_enumeration_keeps_live_cursor(self) -> None:
        self._append(60)
        service = self._service()
        original_scan = service._scan_run

        def scan_then_archive(state, source, **kwargs):
            scanned = original_scan(state, source, **kwargs)
            if source.key == "run-1" and not source.terminal:
                archive_raw = (
                    self.paths.archive_dir / "WIKI-1" / "session" / "raw.jsonl"
                )
                archive_raw.parent.mkdir(parents=True)
                shutil.copy2(self.raw, archive_raw)
                (archive_raw.parent / "run.json").write_text(
                    json.dumps({"run_id": "run-1"}), encoding="utf-8"
                )
                (
                    archive_raw.parent / telemetry_module.ARCHIVE_COMPLETION_MARKER
                ).write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
                shutil.rmtree(self.raw.parent)
            return scanned

        with mock.patch.object(service, "_scan_run", side_effect=scan_then_archive):
            first = service.run_once()
        second = service.run_once()

        self.assertEqual(first["unknown_counts"], {"item/novel": 60})
        self.assertEqual(second["unknown_counts"], {"item/novel": 60})
        self.assertEqual(first["filed_kinds"], [])
        self.assertEqual(second["filed_kinds"], [])

    def test_published_archive_wins_over_lingering_live_remnant(self) -> None:
        self._append(60)
        archive_raw = self.paths.archive_dir / "WIKI-1" / "session" / "raw.jsonl"
        archive_raw.parent.mkdir(parents=True)
        shutil.copy2(self.raw, archive_raw)
        (archive_raw.parent / "run.json").write_text(
            json.dumps({"run_id": "run-1"}), encoding="utf-8"
        )
        (archive_raw.parent / telemetry_module.ARCHIVE_COMPLETION_MARKER).write_text(
            json.dumps({"run_id": "run-1"}), encoding="utf-8"
        )
        service = self._service()

        first = service.run_once()
        second = service.run_once()
        shutil.rmtree(self.raw.parent)
        cleanup = service.run_once()

        self.assertEqual(first["unknown_counts"], {"item/novel": 60})
        self.assertEqual(second["unknown_counts"], {"item/novel": 60})
        self.assertEqual(cleanup["unknown_counts"], {"item/novel": 60})
        self.assertEqual(first["filed_kinds"], [])
        self.assertEqual(second["filed_kinds"], [])
        self.assertEqual(cleanup["filed_kinds"], [])

    def test_live_cursor_cap_does_not_evict_active_runs(self) -> None:
        self._append(1)
        self._write_run("run-2", 1)
        self._write_run("run-3", 1)
        service = UnknownKindTelemetry(
            self.paths,
            threshold=1000,
            todo_runner=self.calls.append,
            clock=lambda: 1_759_000_000,
        )

        with mock.patch.object(telemetry_module, "MAX_CURSOR_ENTRIES", 2):
            first = service.run_once()
            second = service.run_once()

        self.assertEqual(first["unknown_counts"], {"item/novel": 3})
        self.assertEqual(second["scanned_events"], 0)
        self.assertEqual(
            json.loads(service.state_path.read_text(encoding="utf-8"))[
                "cursors"
            ].keys(),
            {"run-1", "run-2", "run-3"},
        )

    def test_clock_skew_does_not_reset_week_or_extend_scheduler_delay(self) -> None:
        service = self._service()
        future = datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp()
        earlier = datetime(2026, 7, 20, tzinfo=timezone.utc).timestamp()
        service.run_once(timestamp=future)
        backward = service.run_once(timestamp=earlier)

        self.assertEqual(backward["week_start"], "2026-08-03")
        self.assertLessEqual(
            service.seconds_until_due(timestamp=future - 1),
            MAX_SCHEDULE_DELAY_SECONDS,
        )

    def test_oversized_line_is_discarded_with_durable_state(self) -> None:
        oversized = self.raw
        oversized.write_bytes(b"x" * (MAX_EVENT_LINE_BYTES + 1))
        service = self._service()
        service.run_once()
        state = json.loads(service.state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["cursors"]["run-1"]["discarding_oversized_line"])
        self.assertEqual(state["cursors"]["run-1"]["offset"], oversized.stat().st_size)

        with oversized.open("ab") as handle:
            handle.write(b"\n")
        self._append(101)
        result = service.run_once()
        state = json.loads(service.state_path.read_text(encoding="utf-8"))

        self.assertEqual(result["unknown_counts"], {"item/novel": 101})
        self.assertEqual(state["cursors"]["run-1"]["offset"], oversized.stat().st_size)
        self.assertEqual(len(self.calls), 1)

    def test_frozen_native_runner_uses_repo_cli_not_python_executable(self) -> None:
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        expected_cli = repo / "wiki"
        with (
            mock.patch.dict(os.environ, {"WIKI_REPO_DIR": str(repo)}),
            mock.patch(
                "sys.executable", "/Applications/Wiki.app/Contents/MacOS/wiki-backend"
            ),
            mock.patch.object(telemetry_module.subprocess, "run") as run,
        ):
            _todo_runner("unknown provider event kind test")

        self.assertEqual(run.call_args.args[0][0], str(expected_cli))
        self.assertIn("--if-missing", run.call_args.args[0])

    def test_torn_state_write_keeps_previous_state(self) -> None:
        service = self._service()
        service.run_once()
        before = service.state_path.read_bytes()
        replacement = {"version": 2, "week_start": "2099-01-01"}

        with (
            mock.patch.object(
                telemetry_module.os, "replace", side_effect=OSError("torn write")
            ),
            self.assertRaises(OSError),
        ):
            service._save_state(replacement)

        self.assertEqual(service.state_path.read_bytes(), before)

    def test_raw_stream_is_not_read_with_read_text(self) -> None:
        self._append(101)
        service = self._service()
        original = Path.read_text

        def reject_raw_read(path: Path, *args, **kwargs):
            if path.name == "raw.jsonl":
                raise AssertionError("raw.jsonl was slurped")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", reject_raw_read):
            result = service.run_once()

        self.assertEqual(result["scanned_events"], 101)


if __name__ == "__main__":
    unittest.main()
