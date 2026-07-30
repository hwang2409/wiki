from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.store import RuntimePaths
from backend.app.agent_runtime.unknown_kind_telemetry import UnknownKindTelemetry


class UnknownKindTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = RuntimePaths(
            runtime_dir=root / "runtime",
            socket_path=root / "runtime" / "supervisor.sock",
            registry_path=root / "registry.json",
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
