from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime import archive_protocol as protocol
from backend.app.agent_runtime.archive_protocol import (
    ARCHIVE_COMPLETION_MARKER,
    archive_is_committed,
    commit_archive,
)
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.types import LifecycleState, ProviderKind, RunRecord


class _SimulatedCrash(BaseException):
    pass


class ArchiveProtocolTests(unittest.TestCase):
    def _archive_fixture(self, root: Path) -> tuple[Path, list[Path]]:
        directory = root / "archive" / "WIKI-272" / "session"
        directory.mkdir(parents=True)
        files = [directory / "run.json", directory / "raw.jsonl"]
        files[0].write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
        files[1].write_text("raw\n", encoding="utf-8")
        return directory, files

    def test_commit_archive_crash_matrix_retries_without_source_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe, files = self._archive_fixture(root)
            events: list[str] = []
            probe_calls = 0
            real_replace = protocol.os.replace
            real_file = protocol._fsync_file
            real_directory = protocol._fsync_directory

            def count_file(path: Path) -> None:
                nonlocal probe_calls
                probe_calls += 1
                events.append(f"file-fsync:{path.name}")
                real_file(path)

            def count_directory(path: Path) -> None:
                nonlocal probe_calls
                probe_calls += 1
                events.append("directory-fsync")
                real_directory(path)

            def count_replace(source: Path, destination: Path) -> None:
                nonlocal probe_calls
                probe_calls += 1
                events.append(f"replace:{Path(destination).name}")
                real_replace(source, destination)

            with (
                mock.patch.object(protocol, "_fsync_file", side_effect=count_file),
                mock.patch.object(
                    protocol, "_fsync_directory", side_effect=count_directory
                ),
                mock.patch.object(protocol.os, "replace", side_effect=count_replace),
            ):
                commit_archive(
                    probe,
                    run_id="run-1",
                    completed_at="2026-08-01T00:00:00Z",
                    expected_paths=files,
                )
            self.assertGreater(probe_calls, 0)

            for fail_at in range(1, probe_calls + 1):
                with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as case:
                    case_root = Path(case)
                    directory, case_files = self._archive_fixture(case_root)
                    live_run = case_root / "live-run"
                    live_run.mkdir()
                    (live_run / "run.json").write_text("live", encoding="utf-8")
                    calls = 0

                    def maybe_crash(operation, *args):
                        nonlocal calls
                        calls += 1
                        result = operation(*args)
                        if calls == fail_at:
                            raise _SimulatedCrash(f"crash at step {fail_at}")
                        return result

                    def fsync_file(path: Path) -> None:
                        maybe_crash(real_file, path)

                    def fsync_directory(path: Path) -> None:
                        maybe_crash(real_directory, path)

                    def replace(source: Path, destination: Path) -> None:
                        maybe_crash(real_replace, source, destination)

                    with (
                        mock.patch.object(protocol, "_fsync_file", side_effect=fsync_file),
                        mock.patch.object(
                            protocol, "_fsync_directory", side_effect=fsync_directory
                        ),
                        mock.patch.object(protocol.os, "replace", side_effect=replace),
                    ):
                        with self.assertRaises(_SimulatedCrash):
                            commit_archive(
                                directory,
                                run_id="run-1",
                                completed_at="2026-08-01T00:00:00Z",
                                expected_paths=case_files,
                            )

                    self.assertTrue(live_run.is_dir())
                    self.assertFalse(archive_is_committed(directory))
                    commit_archive(
                        directory,
                        run_id="run-1",
                        completed_at="2026-08-01T00:00:00Z",
                        expected_paths=case_files,
                    )
                    self.assertTrue(archive_is_committed(directory))

    def test_legacy_marker_is_ignored_then_rearchive_keeps_seed_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths(
                runtime_dir=root / "runtime",
                socket_path=root / "runtime" / "supervisor.sock",
                registry_path=root / "registry.json",
                archive_dir=root / "archive",
            )
            store = RunStore(paths)
            record = store.create(
                RunRecord.new(
                    agent_id="WIKI-272",
                    provider=ProviderKind.CODEX,
                    role="implement",
                    model="fixture-codex",
                    effort="high",
                    worktree=str(root / "worktree"),
                    prompt="work on WIKI-272",
                    orchestrator_id="wiki-dev",
                )
            )
            store.transition(record.run_id, LifecycleState.COMPLETED)

            legacy = paths.archive_dir / record.agent_id / "legacy"
            legacy.mkdir(parents=True)
            (legacy / "run.json").write_text(
                json.dumps({"run_id": record.run_id}), encoding="utf-8"
            )
            (legacy / "raw.jsonl").write_text("legacy\n", encoding="utf-8")
            (legacy / ARCHIVE_COMPLETION_MARKER).write_text(
                json.dumps(
                    {
                        "run_id": record.run_id,
                        "files": {"run.json": 1, "raw.jsonl": 1},
                    }
                ),
                encoding="utf-8",
            )

            self.assertFalse(archive_is_committed(legacy))
            self.assertTrue(store.run_dir(record.run_id).is_dir())
            store.archive_current(record.run_id)

            self.assertTrue(legacy.exists())
            self.assertFalse(store.run_dir(record.run_id).exists())
            self.assertIsNotNone(store.find_archived_run(record.run_id))

    def test_manifest_verification_mutation_guard_rejects_legacy_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory, _ = self._archive_fixture(Path(tmp))
            marker = directory / ARCHIVE_COMPLETION_MARKER
            marker.write_text(
                json.dumps({"run_id": "run-1", "files": {"run.json": 1}}),
                encoding="utf-8",
            )
            self.assertFalse(archive_is_committed(directory))

    def test_marker_directory_fsync_follows_atomic_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory, files = self._archive_fixture(Path(tmp))
            events: list[str] = []
            real_replace = protocol.os.replace
            real_directory = protocol._fsync_directory

            def replace(source: Path, destination: Path) -> None:
                events.append(f"replace:{Path(destination).name}")
                real_replace(source, destination)

            def fsync_directory(path: Path) -> None:
                events.append("directory-fsync")
                real_directory(path)

            with (
                mock.patch.object(protocol.os, "replace", side_effect=replace),
                mock.patch.object(
                    protocol, "_fsync_directory", side_effect=fsync_directory
                ),
            ):
                commit_archive(
                    directory,
                    run_id="run-1",
                    completed_at="2026-08-01T00:00:00Z",
                    expected_paths=files,
                )

            marker_replace = events.index(f"replace:{ARCHIVE_COMPLETION_MARKER}")
            self.assertEqual(events[-1], "directory-fsync")
            self.assertGreater(marker_replace, events.index("directory-fsync"))
