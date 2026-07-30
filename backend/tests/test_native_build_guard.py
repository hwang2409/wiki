from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from backend.app.native_lifecycle import hold_app_lock, hold_runtime_locks
import scripts.atomic_swap as atomic_swap_module
from scripts.atomic_swap import atomic_replace, rollback_replace
from scripts.native_build_guard import inspect_runtime
from scripts.native_daemon_restart import restart_daemon_if_installed


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

    def test_gui_app_lock_alone_blocks_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor.lock").touch()
            with hold_app_lock(runtime):
                status = inspect_runtime(runtime)
            self.assertTrue(status.running)
            self.assertIn("Wiki app lock is held", status.reason or "")

    def test_gui_lock_survives_sidecar_restart_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            supervisor_lock = runtime / "supervisor.lock"
            supervisor_lock.touch()
            with hold_app_lock(runtime):
                for _ in range(2):
                    with supervisor_lock.open("a+b") as handle:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                        self.assertTrue(inspect_runtime(runtime).running)
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
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
                atomic_replace(staged, live, root / "stage" / ".swap-complete")
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
            self.assertTrue((root / "stage" / ".swap-complete").exists())
            self.assertFalse(inspect_runtime(runtime).running)

    def test_interrupt_after_exchange_writes_sentinel_and_retry_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live" / "Wiki.app"
            staged = root / "stage" / "Wiki.app"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")
            sentinel = root / "stage" / ".swap-complete"
            intent = root / "stage" / ".swap-intent"

            real_swap = atomic_swap_module._rename_swap

            def interrupt_after_exchange(first: Path, second: Path) -> bool:
                result = real_swap(first, second)
                os.kill(os.getpid(), signal.SIGINT)
                return result

            with patch.object(
                atomic_swap_module,
                "_rename_swap",
                side_effect=interrupt_after_exchange,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    atomic_swap_module.atomic_replace(staged, live, sentinel, intent)

            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")
            self.assertEqual((staged / "marker").read_text(encoding="utf-8"), "old")
            self.assertTrue(sentinel.exists())
            self.assertFalse(atomic_swap_module.atomic_replace(staged, live, sentinel, intent))
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")

    def test_swap_restarts_loaded_daemon_with_new_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_bundle = root / "target" / "release" / "bundle" / "macos" / "Wiki.app"
            live_bundle.mkdir(parents=True)
            runtime_dir = root / "runtime"
            launch_agents = root / "LaunchAgents"
            launch_agents.mkdir()
            (launch_agents / "com.hwang2409.wiki.backend.plist").write_text(
                "old daemon plist", encoding="utf-8"
            )
            launchctl_calls: list[list[str]] = []
            command_calls: list[tuple[list[str], dict[str, str]]] = []

            def fake_launchctl(
                arguments: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                launchctl_calls.append(arguments)
                return subprocess.CompletedProcess(arguments, 0, "", "")

            def fake_command(
                arguments: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                command_calls.append((arguments, kwargs["env"]))
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with patch.dict(
                os.environ, {"WIKI_LAUNCH_AGENTS_DIR": str(launch_agents)}, clear=False
            ):
                self.assertTrue(
                    restart_daemon_if_installed(
                        live_bundle,
                        runtime_dir,
                        root,
                        launchctl=fake_launchctl,
                        command=fake_command,
                    )
                )

            self.assertEqual(len(launchctl_calls), 1)
            self.assertEqual(len(command_calls), 1)
            command_args, command_env = command_calls[0]
            self.assertEqual(command_args[-3:], ["daemon", "install", "--json"])
            self.assertEqual(command_env["WIKI_APP_PATH"], str(live_bundle))
            self.assertEqual(command_env["WIKI_AGENT_RUNTIME_DIR"], str(runtime_dir))

    def test_failed_swap_restores_old_bundle_before_retrying_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live" / "Wiki.app"
            staged = root / "stage" / "Wiki.app"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")
            sentinel = root / "stage" / ".swap-complete"
            intent = root / "stage" / ".swap-intent"

            self.assertTrue(atomic_replace(staged, live, sentinel, intent))
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")
            self.assertTrue(rollback_replace(staged, live, sentinel, intent))
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "old")
            self.assertEqual((staged / "marker").read_text(encoding="utf-8"), "new")
            self.assertFalse(sentinel.exists())
