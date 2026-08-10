from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime import archive_protocol as protocol
from backend.app.agent_runtime.archive_protocol import (
    ARCHIVE_COMPLETION_MARKER,
    ARCHIVE_MANIFEST_NAME,
    archive_is_committed,
    commit_archive,
)
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.types import LifecycleState, ProviderKind, RunRecord


class ArchiveProtocolTests(unittest.TestCase):
    def _archive_fixture(self, root: Path) -> tuple[Path, list[Path]]:
        directory = root / "archive" / "WIKI-272" / "session"
        directory.mkdir(parents=True)
        files = [
            directory / "run.json",
            directory / "raw.jsonl",
            directory / "events.jsonl",
        ]
        files[0].write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
        files[1].write_text("raw\n", encoding="utf-8")
        files[2].write_text("events\n", encoding="utf-8")
        return directory, files

    def test_commit_archive_crash_matrix_retries_without_source_loss(self) -> None:
        static_steps = (
            "manifest-temp-fsync",
            "manifest-rename",
            "manifest-file-fsync",
            "manifest-directory-fsync",
            "marker-temp-write-fsync",
            "marker-temp-file-fsync",
            "marker-rename",
            "marker-directory-fsync",
        )

        def classify_fsync(
            fd: int,
            fd_paths: dict[int, Path],
            directory: Path,
            observed: list[str],
        ) -> str:
            path = fd_paths.get(fd)
            if path is None:
                return "unknown-fsync"
            if path.name.startswith(f".{ARCHIVE_MANIFEST_NAME}."):
                return "manifest-temp-fsync"
            if path.name == ARCHIVE_MANIFEST_NAME:
                return "manifest-file-fsync"
            if path.name.startswith(f".{ARCHIVE_COMPLETION_MARKER}."):
                if "marker-temp-write-fsync" not in observed:
                    return "marker-temp-write-fsync"
                return "marker-temp-file-fsync"
            if path == directory:
                if "manifest-directory-fsync" not in observed:
                    return "manifest-directory-fsync"
                return "marker-directory-fsync"
            return "unknown-fsync"

        def run_child(
            directory: Path,
            files: list[Path],
            live_run: Path,
            crash_step: str,
            *,
            hard_crash: bool,
        ) -> int:
            cleanup_sentinel = live_run.parent / "cleanup-ran"
            fd_paths: dict[int, Path] = {}
            observed: list[str] = []
            real_open = protocol.os.open
            real_fsync = protocol.os.fsync
            real_replace = protocol.os.replace
            real_unlink = protocol.os.unlink

            def open_file(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                fd_paths[fd] = Path(path)
                return fd

            def inject_step(step: str) -> None:
                observed.append(step)
                if step == crash_step and not hard_crash:
                    raise OSError(f"injected failure at {step}")

            def fsync(fd: int) -> None:
                step = classify_fsync(fd, fd_paths, directory, observed)
                if step == "unknown-fsync":
                    raise AssertionError(f"unexpected fsync fd: {fd}")
                inject_step(step)
                real_fsync(fd)
                if step == crash_step and hard_crash:
                    os._exit(73)

            def replace(source, destination) -> None:
                destination_path = Path(destination)
                if destination_path.name == ARCHIVE_MANIFEST_NAME:
                    step = "manifest-rename"
                elif destination_path.name == ARCHIVE_COMPLETION_MARKER:
                    step = "marker-rename"
                else:
                    raise AssertionError(f"unexpected archive rename: {destination}")
                inject_step(step)
                real_replace(source, destination)
                if step == crash_step and hard_crash:
                    os._exit(73)

            def unlink(path, *args, **kwargs):
                cleanup_sentinel.write_text("cleanup", encoding="ascii")
                return real_unlink(path, *args, **kwargs)

            protocol.os.open = open_file
            protocol.os.fsync = fsync
            protocol.os.replace = replace
            protocol.os.unlink = unlink
            try:
                try:
                    commit_archive(
                        directory,
                        run_id="run-1",
                        completed_at="2026-08-01T00:00:00Z",
                        expected_paths=files,
                    )
                except OSError:
                    if hard_crash:
                        os._exit(1)
                    os._exit(0)
                os._exit(1)
            except BaseException:
                os._exit(1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe, files = self._archive_fixture(root)
            observed: list[str] = []
            fd_paths: dict[int, Path] = {}
            real_open = protocol.os.open
            real_fsync = protocol.os.fsync
            real_replace = protocol.os.replace

            def open_file(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                fd_paths[fd] = Path(path)
                return fd

            def fsync(fd: int) -> None:
                step = classify_fsync(fd, fd_paths, probe, observed)
                self.assertNotEqual(step, "unknown-fsync")
                observed.append(step)
                real_fsync(fd)

            def replace(source, destination) -> None:
                destination_path = Path(destination)
                if destination_path.name == ARCHIVE_MANIFEST_NAME:
                    step = "manifest-rename"
                elif destination_path.name == ARCHIVE_COMPLETION_MARKER:
                    step = "marker-rename"
                else:
                    self.fail(f"unexpected archive rename: {destination}")
                observed.append(step)
                real_replace(source, destination)

            with (
                mock.patch.object(protocol.os, "open", side_effect=open_file),
                mock.patch.object(protocol.os, "fsync", side_effect=fsync),
                mock.patch.object(protocol.os, "replace", side_effect=replace),
            ):
                commit_archive(
                    probe,
                    run_id="run-1",
                    completed_at="2026-08-01T00:00:00Z",
                    expected_paths=files,
                )
            self.assertEqual(observed, list(static_steps))
            self.assertTrue(archive_is_committed(probe))
            manifest = json.loads(
                (probe / ARCHIVE_MANIFEST_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["required_files"], ["raw.jsonl", "events.jsonl", "run.json"]
            )
            self.assertEqual(manifest["optional_files"], [])

        for hard_crash in (True, False):
            for crash_step in static_steps:
                with self.subTest(hard_crash=hard_crash, crash_step=crash_step):
                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        directory, files = self._archive_fixture(root)
                        live_run = root / "live-run"
                        live_run.mkdir()
                        (live_run / "run.json").write_text("live", encoding="utf-8")
                        pid = os.fork()
                        if pid == 0:
                            run_child(
                                directory,
                                files,
                                live_run,
                                crash_step,
                                hard_crash=hard_crash,
                            )
                        _pid, status = os.waitpid(pid, 0)
                        self.assertEqual(_pid, pid)
                        self.assertTrue(os.WIFEXITED(status))
                        if hard_crash:
                            self.assertEqual(os.WEXITSTATUS(status), 73)
                            self.assertFalse((root / "cleanup-ran").exists())
                        else:
                            self.assertEqual(os.WEXITSTATUS(status), 0)
                            self.assertTrue((root / "cleanup-ran").exists())
                        self.assertTrue(
                            live_run.exists() or archive_is_committed(directory)
                        )
                        commit_archive(
                            directory,
                            run_id="run-1",
                            completed_at="2026-08-01T00:00:00Z",
                            expected_paths=files,
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
