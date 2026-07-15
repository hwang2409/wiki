from __future__ import annotations

import fcntl
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from scripts.native_build_guard import inspect_runtime


class NativeBuildGuardTests(TestCase):
    def test_stale_lock_file_without_live_pid_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor.lock").touch()
            (runtime / "supervisor.pid").write_text("424242\n", encoding="utf-8")
            with mock.patch("scripts.native_build_guard.pid_is_alive", return_value=False):
                self.assertFalse(inspect_runtime(runtime).running)

    def test_live_pid_blocks_even_when_lock_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor.lock").touch()
            (runtime / "supervisor.pid").write_text("424242\n", encoding="utf-8")
            with mock.patch("scripts.native_build_guard.pid_is_alive", return_value=True):
                status = inspect_runtime(runtime)
            self.assertTrue(status.running)
            self.assertIn("424242", status.reason or "")

    def test_held_lock_blocks_even_without_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            lock_path = runtime / "supervisor.lock"
            lock_path.touch()
            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                status = inspect_runtime(runtime)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            self.assertTrue(status.running)
            self.assertIn("lock is held", status.reason or "")
