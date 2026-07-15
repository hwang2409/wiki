from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

from backend.app.native_lifecycle import hold_app_lock, hold_runtime_locks
from scripts.atomic_swap import atomic_replace
from scripts.native_build_guard import inspect_runtime


class NativeBuildGuardTests(TestCase):
    def test_stale_lock_files_and_dead_pid_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "app.lock").touch()
            (runtime / "supervisor.lock").touch()
            (runtime / "supervisor.pid").write_text("424242\n", encoding="utf-8")
            self.assertFalse(inspect_runtime(runtime).running)

    def test_pid_reuse_is_advisory_when_both_locks_are_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "app.lock").touch()
            (runtime / "supervisor.lock").touch()
            (runtime / "supervisor.pid").write_text(
                f"{os.getpid()}\n", encoding="utf-8"
            )
            self.assertFalse(inspect_runtime(runtime).running)

    def test_missing_app_lock_requires_explicit_upgrade_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor.lock").touch()
            self.assertTrue(inspect_runtime(runtime).running)
            self.assertFalse(
                inspect_runtime(runtime, allow_missing_app_lock=True).running
            )
            self.assertTrue((runtime / "app.lock").exists())

    def test_held_app_lock_blocks_without_consulting_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "app.lock").touch()
            (runtime / "supervisor.lock").touch()
            with hold_runtime_locks(runtime):
                status = inspect_runtime(runtime)
            self.assertTrue(status.running)
            self.assertIn("Wiki app lock is held", status.reason or "")

    def test_backend_app_lock_alone_blocks_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor.lock").touch()
            with hold_app_lock(runtime):
                status = inspect_runtime(runtime)
            self.assertTrue(status.running)
            self.assertIn("Wiki app lock is held", status.reason or "")

    def test_locks_remain_held_through_atomic_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "app.lock").touch()
            (runtime / "supervisor.lock").touch()
            live = root / "live" / "Wiki.app"
            staged = root / "stage" / "Wiki.app"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")

            with hold_runtime_locks(runtime):
                atomic_replace(staged, live)
                probe = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).parents[2] / "scripts" / "native_build_guard.py"),
                        "--runtime-dir",
                        str(runtime),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(probe.returncode, 0)
            self.assertFalse(inspect_runtime(runtime).running)
