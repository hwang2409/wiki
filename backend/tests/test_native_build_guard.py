from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.app.native_lifecycle import (
    NativeRuntimeLockError,
    hold_app_lock,
    hold_runtime_locks,
)
from backend.app.agent_runtime.store import RuntimePaths
import scripts.atomic_swap as atomic_swap_module
import scripts.native_swap_transaction as native_swap_transaction
from scripts.atomic_swap import atomic_replace, rollback_replace
from scripts.native_build_guard import inspect_runtime
from scripts.native_daemon_restart import restart_daemon_if_installed
from scripts.native_swap_transaction import swap_native_app


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
            runtime_dir.mkdir()
            daemon_settings = runtime_dir / "daemon-settings.json"
            daemon_settings.write_text(
                '{"label":"com.example.wiki.custom","port":19321}\n',
                encoding="utf-8",
            )
            (launch_agents / "com.example.wiki.custom.plist").write_text(
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
            self.assertEqual(
                launchctl_calls[0][-1],
                f"gui/{os.getuid()}/com.example.wiki.custom",
            )
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

    def test_fallback_exchange_preserves_old_bundle_at_staged_path(self) -> None:
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

            with patch.object(atomic_swap_module, "_rename_swap", return_value=False):
                atomic_replace(staged, live, sentinel, intent)
                self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")
                self.assertEqual((staged / "marker").read_text(encoding="utf-8"), "old")
                rollback_replace(staged, live, sentinel, intent)
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "old")

    def test_transaction_stops_live_supervisor_before_taking_its_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "stage"
            live = root / "src-tauri/target/release/bundle/macos/Wiki.app"
            staged = stage_root / "target/release/bundle/macos/Wiki.app"
            runtime = root / "runtime"
            stage_root.mkdir(parents=True)
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            runtime.mkdir()
            (runtime / "app.lock").touch()
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "backend.app.agent_runtime.daemon",
                    "--runtime-dir",
                    str(runtime),
                    "--socket",
                    str(runtime / "supervisor.sock"),
                    "--registry",
                    str(runtime / "registry.json"),
                ],
                cwd=Path(__file__).parents[2],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not (runtime / "supervisor.pid").exists():
                    if process.poll() is not None:
                        self.fail(process.stderr.read() if process.stderr else "supervisor exited")
                    if time.monotonic() >= deadline:
                        self.fail("supervisor did not publish its PID")
                    time.sleep(0.05)

                def restart(_live: Path, _runtime: Path, _repo: Path) -> bool:
                    self.assertFalse((runtime / "supervisor.pid").exists())
                    return True

                swap_native_app(stage_root, root, runtime, restart=restart)
                self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                if process.stderr:
                    process.stderr.close()

    def test_swap_requires_existing_app_lock_unless_explicitly_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "stage"
            live = root / "src-tauri/target/release/bundle/macos/Wiki.app"
            staged = stage_root / "target/release/bundle/macos/Wiki.app"
            runtime = root / "runtime"
            stage_root.mkdir(parents=True)
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            runtime.mkdir()
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")

            with self.assertRaisesRegex(NativeRuntimeLockError, "app lock is missing"):
                swap_native_app(stage_root, root, runtime, restart=lambda *_args: True)
            self.assertFalse((runtime / "app.lock").exists())
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "old")

            swap_native_app(
                stage_root,
                root,
                runtime,
                allow_missing_app_lock=True,
                restart=lambda *_args: True,
            )
            self.assertTrue((runtime / "app.lock").exists())
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")

    def test_crash_after_exchange_keeps_rollback_bundle_without_completion_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "stage"
            live = root / "src-tauri/target/release/bundle/macos/Wiki.app"
            staged = stage_root / "target/release/bundle/macos/Wiki.app"
            runtime = root / "runtime"
            stage_root.mkdir(parents=True)
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            runtime.mkdir()
            (runtime / "app.lock").touch()
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")

            def crash_after_exchange(*_args: object) -> bool:
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                swap_native_app(stage_root, root, runtime, restart=crash_after_exchange)

            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")
            self.assertEqual((staged / "marker").read_text(encoding="utf-8"), "old")
            self.assertTrue((stage_root / ".swap-exchanged").exists())
            self.assertFalse((stage_root / ".swap-complete").exists())

    def test_retry_after_drain_crash_reloads_saved_runs_and_verifies_handover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "stage"
            live = root / "src-tauri/target/release/bundle/macos/Wiki.app"
            staged = stage_root / "target/release/bundle/macos/Wiki.app"
            runtime = root / "runtime"
            stage_root.mkdir(parents=True)
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            runtime.mkdir()
            (runtime / "app.lock").touch()
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")
            saved_runs = [
                {
                    "agent_id": "WIKI-CRASH-RECOVERY",
                    "run_id": "run-crash-recovery",
                    "provider_session_id": "session-crash-recovery",
                    "state": "idle",
                }
            ]
            handover_client = Mock()
            handover_client.prepare_for_handover.return_value = saved_runs
            started: list[tuple[Path, Path, Path]] = []
            verified: list[list[dict[str, object]]] = []

            def start_supervisor(
                bundle: Path, runtime_dir: Path, repo_root: Path
            ) -> None:
                started.append((bundle, runtime_dir, repo_root))

            def wait_for_handover(
                _runtime_dir: Path, runs: list[dict[str, object]]
            ) -> None:
                verified.append(runs)

            with (
                patch.object(
                    native_swap_transaction,
                    "_supervisor_lock_is_free",
                    side_effect=[False, True],
                ),
                patch.object(
                    native_swap_transaction,
                    "_supervisor_identity",
                    return_value=(handover_client, 1234, {"pid": 1234}),
                ),
                patch.object(
                    native_swap_transaction,
                    "_stop_supervisor",
                    side_effect=[KeyboardInterrupt, None],
                ),
                patch.object(
                    native_swap_transaction,
                    "_wait_for_handover",
                    side_effect=wait_for_handover,
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    swap_native_app(stage_root, root, runtime)

                journal = stage_root / ".handover-runs.json"
                self.assertTrue(journal.exists())
                swap_native_app(
                    stage_root,
                    root,
                    runtime,
                    restart=lambda *_args: True,
                    start_supervisor=start_supervisor,
                )

            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")
            self.assertEqual(
                started,
                [(live.resolve(), runtime.resolve(), root.resolve())],
            )
            self.assertEqual(verified, [saved_runs])
            self.assertFalse(stage_root.exists())

    def test_starting_attached_run_aborts_before_drain_or_handover_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "stage"
            live = root / "src-tauri/target/release/bundle/macos/Wiki.app"
            staged = stage_root / "target/release/bundle/macos/Wiki.app"
            runtime = root / "runtime"
            stage_root.mkdir(parents=True)
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            runtime.mkdir()
            (runtime / "app.lock").touch()
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")
            starting = {
                "agent_id": "WIKI-STARTING",
                "run_id": "run-starting",
                "state": "starting",
                "provider_session_id": None,
                "provider_pid": 424_244,
                "control_attached": True,
            }
            calls: list[str] = []
            handover_client = native_swap_transaction.SupervisorClient(
                RuntimePaths.from_env({"WIKI_AGENT_RUNTIME_DIR": str(runtime)})
            )

            def request(method: str, _params: dict[str, object] | None = None) -> object:
                calls.append(method)
                if method == "run/list":
                    return {"runs": [starting]}
                if method == "run/status":
                    return starting
                raise AssertionError(f"unexpected request: {method}")

            with (
                patch.object(
                    handover_client,
                    "request",
                    side_effect=request,
                ),
                patch.object(
                    native_swap_transaction,
                    "_supervisor_lock_is_free",
                    return_value=False,
                ),
                patch.object(
                    native_swap_transaction,
                    "_supervisor_identity",
                    return_value=(handover_client, 1234, {"pid": 1234}),
                ),
                patch.object(native_swap_transaction, "_stop_supervisor") as stop,
            ):
                with self.assertRaisesRegex(
                    native_swap_transaction.SupervisorUnavailable,
                    "state starting is not resumable",
                ):
                    swap_native_app(
                        stage_root,
                        root,
                        runtime,
                        restart=Mock(),
                    )

            stop.assert_not_called()
            self.assertEqual(calls, ["run/list", "run/status"])
            self.assertFalse((stage_root / ".handover-runs.json").exists())
            self.assertFalse((stage_root / ".swap-intent").exists())
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "old")
            self.assertEqual((staged / "marker").read_text(encoding="utf-8"), "new")

    def test_swap_rejects_stale_supervisor_pid_before_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor.lock").touch()
            (runtime / "supervisor.pid").write_text("1234\n", encoding="utf-8")
            with hold_app_lock(runtime), patch.object(
                native_swap_transaction, "_supervisor_peer_pid", return_value=5678
            ):
                # Keep the lock held in a separate descriptor so the swap must
                # inspect the PID instead of taking the lock itself.
                with (runtime / "supervisor.lock").open("a+b") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    with self.assertRaisesRegex(RuntimeError, "PID mismatch"):
                        native_swap_transaction._stop_supervisor(runtime)
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def test_swap_rejects_pid_reuse_from_authenticated_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor.lock").touch()
            (runtime / "supervisor.pid").write_text("1234\n", encoding="utf-8")
            with (
                patch.object(
                    native_swap_transaction,
                    "_supervisor_peer_pid",
                    return_value=1234,
                ),
                patch.object(
                    native_swap_transaction.SupervisorClient,
                    "ping",
                    return_value={"status": "ok", "pid": 5678},
                ),
            ):
                with (runtime / "supervisor.lock").open("a+b") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    with self.assertRaisesRegex(RuntimeError, "RPC PID"):
                        native_swap_transaction._stop_supervisor(runtime)
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def test_supervisor_identity_retries_until_delayed_socket_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor.lock").touch()
            (runtime / "supervisor.pid").write_text("1234\n", encoding="utf-8")
            with (
                (runtime / "supervisor.lock").open("a+b") as lock,
                patch.object(
                    native_swap_transaction,
                    "_supervisor_peer_pid",
                    side_effect=[
                        native_swap_transaction.SupervisorUnavailable(
                            "socket is still starting"
                        ),
                        1234,
                    ],
                ) as peer_pid,
                patch.object(
                    native_swap_transaction.SupervisorClient,
                    "ping",
                    return_value={"status": "ok", "pid": 1234},
                ),
            ):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                native_swap_transaction._supervisor_identity(runtime, timeout=1.0)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

            self.assertEqual(peer_pid.call_count, 2)

    def test_handover_requires_live_attached_provider_and_stable_state(self) -> None:
        saved = [
            {
                "agent_id": "WIKI-HANDOVER",
                "run_id": "run-before-swap",
                "provider_session_id": "provider-before-swap",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            native_swap_transaction, "SupervisorClient"
        ) as client_type:
            client = client_type.return_value
            client.ping.return_value = {"status": "ok", "pid": 1234}
            client.request.return_value = {
                "agent_id": "WIKI-HANDOVER",
                "run_id": "run-before-swap",
                "provider_session_id": "provider-before-swap",
                "control_attached": False,
                "provider_alive": False,
                "state": "idle",
            }
            with self.assertRaisesRegex(RuntimeError, "did not restore"):
                native_swap_transaction._wait_for_handover(
                    Path(tmp), saved, timeout=0.11
                )

            client.request.side_effect = [
                {
                    "agent_id": "WIKI-HANDOVER",
                    "run_id": "run-before-swap",
                    "provider_session_id": "provider-before-swap",
                    "control_attached": True,
                    "provider_alive": True,
                    "state": "idle",
                },
                {
                    "agent_id": "WIKI-HANDOVER",
                    "run_id": "run-before-swap",
                    "provider_session_id": "provider-before-swap",
                    "control_attached": True,
                    "provider_alive": True,
                    "state": "idle",
                },
            ]
            native_swap_transaction._wait_for_handover(
                Path(tmp), saved, timeout=1.0
            )

    def test_swap_uses_three_argument_default_starter_for_saved_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "stage"
            live = root / "src-tauri" / "target" / "release" / "bundle" / "macos" / "Wiki.app"
            staged = stage_root / "target" / "release" / "bundle" / "macos" / "Wiki.app"
            runtime = root / "runtime"
            sidecar = staged / "Contents/Resources/wiki-backend-sidecar/wiki-backend"
            stage_root.mkdir(parents=True)
            sidecar.parent.mkdir(parents=True)
            staged.mkdir(parents=True, exist_ok=True)
            live.mkdir(parents=True)
            runtime.mkdir()
            (runtime / "app.lock").touch()
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")
            sidecar.write_text("#!/bin/sh\n", encoding="utf-8")
            sidecar.chmod(0o755)
            saved_runs = [
                {
                    "agent_id": "WIKI-SAVED",
                    "run_id": "run-saved",
                    "provider_session_id": "session-saved",
                    "state": "idle",
                }
            ]
            started: list[tuple[Path, Path, Path]] = []
            handover_client = Mock()
            handover_client.prepare_for_handover.return_value = saved_runs

            def start_supervisor(
                bundle: Path, runtime_dir: Path, repo_root: Path
            ) -> None:
                started.append((bundle, runtime_dir, repo_root))
                native_swap_transaction._start_supervisor(bundle, runtime_dir, repo_root)

            with (
                patch.object(
                    native_swap_transaction,
                    "_supervisor_lock_is_free",
                    return_value=False,
                ),
                patch.object(
                    native_swap_transaction,
                    "_supervisor_identity",
                    return_value=(
                        handover_client,
                        1234,
                        {"status": "ok", "pid": 1234},
                    ),
                ) as identity,
                patch.object(native_swap_transaction, "_stop_supervisor"),
                patch.object(native_swap_transaction, "_wait_for_handover"),
                patch.object(
                    native_swap_transaction.subprocess,
                    "Popen",
                ) as popen,
            ):
                native_swap_transaction.swap_native_app(
                    stage_root,
                    root,
                    runtime,
                    restart=lambda *_args: True,
                    start_supervisor=start_supervisor,
                )

            self.assertEqual(identity.call_count, 1)
            self.assertEqual(
                started,
                [(live.resolve(), runtime.resolve(), root.resolve())],
            )
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0][-1], "--supervisor")
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "new")

    def test_failed_handover_rolls_back_bundle_and_recovers_saved_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "stage"
            live = root / "src-tauri" / "target" / "release" / "bundle" / "macos" / "Wiki.app"
            staged = stage_root / "target" / "release" / "bundle" / "macos" / "Wiki.app"
            runtime = root / "runtime"
            stage_root.mkdir(parents=True)
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            runtime.mkdir()
            (runtime / "app.lock").touch()
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")
            saved_runs = [
                {
                    "agent_id": "WIKI-SAVED",
                    "run_id": "run-saved",
                    "provider_session_id": "session-saved",
                    "state": "idle",
                }
            ]
            started: list[tuple[Path, Path, Path]] = []
            restarts: list[str] = []
            handover_client = Mock()
            handover_client.prepare_for_handover.return_value = saved_runs

            def restart(bundle: Path, _runtime: Path, _repo: Path) -> bool:
                restarts.append((bundle / "marker").read_text(encoding="utf-8"))
                return True

            def start_supervisor(
                bundle: Path, runtime_dir: Path, repo_root: Path
            ) -> None:
                started.append((bundle, runtime_dir, repo_root))

            with (
                patch.object(
                    native_swap_transaction,
                    "_supervisor_lock_is_free",
                    return_value=False,
                ),
                patch.object(
                    native_swap_transaction,
                    "_supervisor_identity",
                    return_value=(
                        handover_client,
                        1234,
                        {"status": "ok", "pid": 1234},
                    ),
                ),
                patch.object(native_swap_transaction, "_stop_supervisor"),
                patch.object(
                    native_swap_transaction,
                    "_wait_for_handover",
                    side_effect=[RuntimeError("provider resume is pending"), None],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "did not restore saved runs"):
                    native_swap_transaction.swap_native_app(
                        stage_root,
                        root,
                        runtime,
                        restart=restart,
                        start_supervisor=start_supervisor,
                    )

            self.assertEqual(restarts, ["new", "old"])
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "old")
            self.assertEqual(
                started,
                [
                    (live.resolve(), runtime.resolve(), root.resolve()),
                    (live.resolve(), runtime.resolve(), root.resolve()),
                ],
            )

    def test_swap_keeps_competing_process_out_during_restart_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "stage"
            live = root / "src-tauri" / "target" / "release" / "bundle" / "macos" / "Wiki.app"
            staged = stage_root / "target" / "release" / "bundle" / "macos" / "Wiki.app"
            runtime = root / "runtime"
            stage_root.mkdir(parents=True)
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            runtime.mkdir()
            (runtime / "app.lock").touch()
            (runtime / "supervisor.lock").touch()
            (live / "marker").write_text("old", encoding="utf-8")
            (staged / "marker").write_text("new", encoding="utf-8")
            restart_markers: list[str] = []

            def restart(live_bundle: Path, runtime_dir: Path, _repo_root: Path) -> bool:
                competing = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).parents[2] / "scripts" / "native_build_guard.py"),
                        "--runtime-dir",
                        str(runtime_dir),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(competing.returncode, 0)
                restart_markers.append((live_bundle / "marker").read_text(encoding="utf-8"))
                if len(restart_markers) == 1:
                    raise RuntimeError("new daemon fingerprint mismatch")
                return True

            with self.assertRaisesRegex(RuntimeError, "new daemon failed"):
                swap_native_app(
                    stage_root,
                    root,
                    runtime,
                    restart=restart,
                )

            self.assertEqual(restart_markers, ["new", "old"])
            self.assertEqual((live / "marker").read_text(encoding="utf-8"), "old")
            self.assertFalse(inspect_runtime(runtime).running)
