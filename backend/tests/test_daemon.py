"""LaunchAgent and persistent backend lifecycle tests."""

from __future__ import annotations

import json
import http.server
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend import native_server
from backend.app import daemon
from backend.app.agent_runtime.version import frozen_runtime_fingerprint
from backend.native_server import DaemonAuthSocket, rotate_log_file


class LaunchAgentConfigTests(unittest.TestCase):
    def _config(self, root: Path) -> daemon.DaemonConfig:
        return daemon.DaemonConfig(
            label="com.example.wiki.test",
            port=18213,
            repo_dir=Path("/tmp/wiki-repo"),
            vault_dir=Path("/tmp/wiki-vault"),
            runtime_dir=Path("/tmp/wiki-runtime"),
            executable=Path("/usr/bin/python3"),
            python_module="backend.native_server",
            log_path=Path("/tmp/wiki-log/wiki-backend-daemon.log"),
            launch_agents_dir=root / "LaunchAgents",
        )

    def test_plist_matches_golden(self) -> None:
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            expected = (
                Path(__file__).parent / "fixtures" / "wiki-backend.launchd.plist"
            ).read_text(encoding="utf-8")
            self.assertEqual(daemon.render_plist(config), expected)

    def test_config_reads_persisted_non_default_connection_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            daemon.daemon_settings_path(runtime).write_text(
                '{"label":"com.example.wiki.custom","port":19321}\n',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"WIKI_BACKEND_PORT": "", "WIKI_DAEMON_LABEL": ""},
            ):
                config = daemon.config_from_env(
                    overrides={
                        "WIKI_AGENT_RUNTIME_DIR": str(runtime),
                        "WIKI_LAUNCH_AGENTS_DIR": str(Path(tmp) / "LaunchAgents"),
                    }
                )
            self.assertEqual(config.label, "com.example.wiki.custom")
            self.assertEqual(config.port, 19321)
            self.assertIn("19321", daemon.render_plist(config))

    def test_config_rejects_absolute_traversal_and_control_labels(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            launch_agents = Path(tmp) / "LaunchAgents"
            for label in ("/tmp/wiki-review31.plist", "../outside", "com.example.\nwiki"):
                with self.subTest(label=repr(label)):
                    with self.assertRaisesRegex(
                        daemon.DaemonError, "safe reverse-DNS"
                    ):
                        daemon.config_from_env(
                            overrides={
                                "WIKI_DAEMON_LABEL": label,
                                "WIKI_AGENT_RUNTIME_DIR": str(runtime),
                                "WIKI_LAUNCH_AGENTS_DIR": str(launch_agents),
                            }
                        )
            runtime.mkdir()
            daemon.daemon_settings_path(runtime).write_text(
                '{"label":"../../outside","port":19321}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(daemon.DaemonError, "safe reverse-DNS"):
                daemon.config_from_env(
                    overrides={
                        "WIKI_AGENT_RUNTIME_DIR": str(runtime),
                        "WIKI_LAUNCH_AGENTS_DIR": str(launch_agents),
                    }
                )

    def test_corrupt_settings_cannot_fall_back_to_default_after_custom_install(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            launch_agents = root / "LaunchAgents"
            runtime.mkdir()
            custom = daemon.config_from_env(
                overrides={
                    "WIKI_AGENT_RUNTIME_DIR": str(runtime),
                    "WIKI_LAUNCH_AGENTS_DIR": str(launch_agents),
                    "WIKI_DAEMON_LABEL": "com.example.wiki.custom",
                    "WIKI_BACKEND_PORT": "19321",
                }
            )
            launch_agents.mkdir()
            custom.plist_path.write_text("custom plist", encoding="utf-8")
            daemon.daemon_settings_path(runtime).write_text(
                "{not valid json\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(daemon.DaemonError, "invalid daemon settings"):
                daemon.config_from_env(
                    overrides={
                        "WIKI_AGENT_RUNTIME_DIR": str(runtime),
                        "WIKI_LAUNCH_AGENTS_DIR": str(launch_agents),
                    }
                )
            self.assertTrue(custom.plist_path.exists())

    def test_service_absence_matches_captured_macos_output(self) -> None:
        config = self._config(Path("/tmp/LaunchAgents"))
        captured = subprocess.CompletedProcess(
            ["launchctl", "print", config.target],
            113,
            "",
            daemon._service_absent_message(config) + "\n",
        )
        self.assertTrue(daemon._service_absent(config, captured))
        wrong_domain = subprocess.CompletedProcess(
            captured.args,
            113,
            "",
            "Bad request.\n"
            f'Could not find service "{config.target}" in domain for system',
        )
        self.assertFalse(daemon._service_absent(config, wrong_domain))

    def test_bootout_absence_matches_captured_macos_output(self) -> None:
        config = self._config(Path("/tmp/LaunchAgents"))
        captured = subprocess.CompletedProcess(
            ["launchctl", "bootout", config.target],
            3,
            "",
            "Boot-out failed: 3: No such process\n",
        )
        self.assertTrue(daemon._bootout_absent(config, captured))
        wrong_output = subprocess.CompletedProcess(
            captured.args,
            3,
            "",
            "Boot-out failed: 3: Input/output error\n",
        )
        self.assertFalse(daemon._bootout_absent(config, wrong_output))

    def test_status_rejects_stale_running_backend_fingerprint(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "wiki-backend"
            executable.write_bytes(b"installed backend")
            executable.chmod(0o755)
            config = daemon.DaemonConfig(
                **{**self._config(root).__dict__, "executable": executable}
            )
            stale = {
                "healthy": True,
                "payload": {
                    "status": "ok",
                    "daemon_managed": True,
                    "backend_fingerprint": "stale-backend",
                },
            }
            with patch.object(daemon, "_service_loaded", return_value=True), patch.object(
                daemon, "_health", return_value=stale
            ):
                result = daemon.status(config)
            self.assertFalse(result["healthy"])
            self.assertFalse(result["fingerprint_match"])
            self.assertEqual(result["backend_fingerprint"], "stale-backend")
            self.assertEqual(
                result["expected_backend_fingerprint"],
                frozen_runtime_fingerprint(executable),
            )

    def test_health_rejects_unrelated_server_with_matching_fingerprint(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "Wiki.app/Contents/Resources/wiki-backend-sidecar/wiki-backend"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"installed backend")
            executable.chmod(0o755)
            config = daemon.DaemonConfig(
                **{**self._config(root).__dict__, "executable": executable}
            )

            class HealthHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    body = json.dumps(
                        {
                            "status": "ok",
                            "daemon_managed": True,
                            "backend_fingerprint": frozen_runtime_fingerprint(executable),
                            "process_id": os.getpid(),
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, _format: str, *_args: object) -> None:
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
            config = daemon.DaemonConfig(
                **{**config.__dict__, "port": server.server_port}
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(daemon, "_service_pid", return_value=os.getpid()):
                    health = daemon._health(config)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
            self.assertFalse(health["healthy"])
            self.assertFalse(health["identity_matches"])

    def test_wait_for_healthy_retries_a_retiring_fingerprint(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "wiki-backend"
            executable.write_bytes(b"new backend")
            executable.chmod(0o755)
            config = daemon.DaemonConfig(
                **{**self._config(root).__dict__, "executable": executable}
            )
            expected = daemon._expected_backend_fingerprint(config)
            health = iter(
                [
                    {
                        "healthy": True,
                        "identity_matches": True,
                        "payload": {
                            "backend_fingerprint": "old backend",
                        },
                    },
                    {
                        "healthy": True,
                        "identity_matches": True,
                        "payload": {"backend_fingerprint": expected},
                    },
                ]
            )
            with patch.object(daemon, "_health", side_effect=lambda _config: next(health)), patch.object(
                daemon, "HEALTH_POLL_SECONDS", 0.0
            ):
                result = daemon._wait_for_healthy(config)
            self.assertEqual(result["payload"]["backend_fingerprint"], expected)

    def test_plist_exports_bundle_path_for_source_build_artifact(self) -> None:
        config = self._config(Path("/tmp/LaunchAgents"))
        executable = (
            Path("/tmp/src-tauri/target/release/bundle/macos/Wiki.app")
            / "Contents/Resources/wiki-backend-sidecar/wiki-backend"
        )
        config = daemon.DaemonConfig(**{**config.__dict__, "executable": executable})
        self.assertEqual(
            daemon.plist_payload(config)["EnvironmentVariables"]["WIKI_APP_PATH"],
            "/tmp/src-tauri/target/release/bundle/macos/Wiki.app",
        )

    def test_install_preserves_seeded_live_runs_and_archive(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            config = daemon.DaemonConfig(
                **{
                    **config.__dict__,
                    "repo_dir": root / "repo",
                    "vault_dir": root / "vault",
                    "runtime_dir": root / "runtime",
                    "log_path": root / "logs" / "backend.log",
                }
            )
            config.runtime_dir.mkdir(parents=True)
            (config.runtime_dir / daemon.DAEMON_TRANSACTION_LOCK_NAME).touch()
            run_dir = config.runtime_dir / "runs" / "run-live-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-live-1",
                        "agent_id": "WIKI-168",
                        "session_id": "session-live-1",
                        "history": [{"state": "working"}],
                    }
                ),
                encoding="utf-8",
            )
            archive_dir = root / "agent-archive" / "WIKI-168" / "session-live-1"
            archive_dir.mkdir(parents=True)
            (archive_dir / "meta.json").write_text(
                '{"run_id":"run-archived-1","session_id":"session-old-1"}\n',
                encoding="utf-8",
            )
            registry = root / "agent-registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "WIKI-168": {
                            "current": {
                                "run_id": "run-live-1",
                                "session_id": "session-live-1",
                            },
                            "history": [{"run_id": "run-archived-1"}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            tracked = [config.runtime_dir, archive_dir, registry]
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for base in tracked
                for path in ([base] if base.is_file() else base.rglob("*"))
                if path.is_file()
            }
            calls: list[tuple[str, ...]] = []

            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                calls.append(arguments)
                if arguments[0] == "print":
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        113,
                        "",
                        daemon._service_absent_message(config),
                    )
                return subprocess.CompletedProcess(
                    ["launchctl", *arguments], 0, "", ""
                )

            health = {
                "healthy": True,
                "payload": {
                    "status": "ok",
                    "daemon_managed": True,
                    "backend_fingerprint": daemon._expected_backend_fingerprint(config),
                },
            }
            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl), patch.object(
                daemon, "_health", return_value=health
            ):
                result = daemon.install(config)

            self.assertEqual(result["action"], "installed")
            self.assertEqual([call[0] for call in calls], ["print", "bootstrap"])
            self.assertTrue(config.plist_path.is_file())
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for base in tracked
                for path in ([base] if base.is_file() else base.rglob("*"))
                if path.is_file()
            }
            settings = after.pop("runtime/daemon-settings.json")
            self.assertEqual(after, before)
            self.assertEqual(
                json.loads(settings),
                {"label": config.label, "port": config.port},
            )

    def test_uninstall_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            config.launch_agents_dir.mkdir()
            config.plist_path.write_text("plist", encoding="utf-8")
            with patch.object(
                daemon,
                "_launchctl",
                side_effect=lambda _config, *arguments: (
                    subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        3,
                        "",
                        "Boot-out failed: 3: No such process",
                    )
                    if arguments[0] == "bootout"
                    else subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        113 if arguments[0] == "print" else 0,
                        "",
                        daemon._service_absent_message(config),
                    )
                ),
            ):
                result = daemon.uninstall(config)
            self.assertEqual(result["action"], "uninstalled")
            self.assertFalse(config.plist_path.exists())

    def test_uninstall_keeps_plist_when_launchctl_error_is_not_absence(self) -> None:
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            config.launch_agents_dir.mkdir()
            config.plist_path.write_text("plist", encoding="utf-8")
            with patch.object(
                daemon,
                "_launchctl",
                return_value=subprocess.CompletedProcess(
                    ["launchctl"], 1, "", "Input/output error: service database not found"
                ),
            ):
                with self.assertRaises(daemon.DaemonError):
                    daemon.uninstall(config)
            self.assertTrue(config.plist_path.exists())

    def test_uninstall_keeps_plist_when_bootout_fails_even_if_probe_is_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            config.launch_agents_dir.mkdir()
            config.plist_path.write_text("plist", encoding="utf-8")

            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                if arguments[0] == "bootout":
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        1,
                        "",
                        "Input/output error",
                    )
                return subprocess.CompletedProcess(
                    ["launchctl", *arguments],
                    113,
                    "",
                    daemon._service_absent_message(config),
                )

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl):
                with self.assertRaises(daemon.DaemonError):
                    daemon.uninstall(config)
            self.assertTrue(config.plist_path.exists())


class DaemonLogTests(unittest.TestCase):
    def test_daemon_secret_is_absent_from_every_log_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "logs" / "backend.log"
            socket_path = root / "runtime" / "wiki-app-secret.sock"
            secret_copy = root / "secret-copy.txt"
            mode_copy = root / "secret-mode.txt"
            driver = root / "driver.py"
            driver.write_text(
                textwrap.dedent(
                    f"""
                    import os
                    import socket
                    import signal
                    import sys
                    from pathlib import Path
                    from backend import native_server

                    class FakeServer:
                        def __init__(self, _config):
                            pass

                        def run(self):
                            path = Path({str(socket_path)!r})
                            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                                client.connect(str(path))
                                secret = client.recv(4096).decode().strip()
                            Path({str(secret_copy)!r}).write_text(secret, encoding="utf-8")
                            Path({str(mode_copy)!r}).write_text(str(path.stat().st_mode), encoding="utf-8")
                            os.kill(os.getpid(), signal.SIGTERM)

                    native_server.uvicorn.Config = lambda *args, **kwargs: object()
                    native_server.uvicorn.Server = FakeServer
                    native_server.is_trusted_tauri_peer = lambda _connection: True
                    sys.argv = [
                        "wiki-backend", "--port", "18213", "--daemon",
                        "--log-path", {str(log_path)!r},
                    ]
                    os.environ["WIKI_APP_SECRET"] = "daemon-secret-never-logged"
                    os.environ["WIKI_AGENT_RUNTIME_DIR"] = {str(root / 'runtime')!r}
                    native_server.main()
                    """
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
            env.pop("WIKI_FRONTEND_DIST", None)
            subprocess.run(
                [sys.executable, str(driver)],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )
            secret = secret_copy.read_text(encoding="utf-8")
            self.assertEqual(
                int(mode_copy.read_text(encoding="utf-8"), 10) & 0o777,
                0o600,
            )
            self.assertFalse(socket_path.exists())
            self.assertFalse((root / "runtime" / "wiki-app-secret").exists())
            logs = list(root.rglob("*.log"))
            self.assertTrue(logs)
            self.assertTrue(all(secret not in path.read_text() for path in logs))

    def test_build_fingerprint_matches_frozen_bundle_executable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "dist" / "wiki-backend-sidecar"
            bundle.mkdir(parents=True)
            executable = bundle / "wiki-backend"
            launcher = root / "dist" / "wiki-backend"
            executable.write_bytes(b"frozen backend executable")
            launcher.write_bytes(b"shell launcher")
            script = Path(__file__).resolve().parents[2] / "scripts" / "native_backend_fingerprint.py"
            result = subprocess.run(
                [sys.executable, str(script), str(executable)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout.strip(), frozen_runtime_fingerprint(executable))
            self.assertNotEqual(
                result.stdout.strip(), frozen_runtime_fingerprint(launcher)
            )
            build_script = script.parent / "build-native-app.sh"
            self.assertIn(
                'backend_binary="$pyinstaller_dist/wiki-backend-sidecar/wiki-backend"',
                build_script.read_text(encoding="utf-8"),
            )

    def test_log_rotates_with_bounded_backups(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "wiki-backend-daemon.log"
            path.write_text("current", encoding="utf-8")
            path.with_name(path.name + ".1").write_text("one", encoding="utf-8")
            path.with_name(path.name + ".2").write_text("two", encoding="utf-8")
            rotate_log_file(path, max_bytes=1, backups=2)
            self.assertFalse(path.exists())
            self.assertEqual(
                path.with_name(path.name + ".1").read_text(encoding="utf-8"),
                "current",
            )
            self.assertEqual(
                path.with_name(path.name + ".2").read_text(encoding="utf-8"),
                "one",
            )

    def test_runtime_log_rotator_reopens_after_size_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "wiki-backend-daemon.log"
            path.write_text("current", encoding="utf-8")
            self.assertTrue(
                native_server.rotate_daemon_log(
                    path, max_bytes=1, backups=1, reopen=False
                )
            )
            self.assertEqual(
                path.with_name(path.name + ".1").read_text(encoding="utf-8"),
                "current",
            )


class DaemonArtifactTests(unittest.TestCase):
    def test_install_fails_before_bootstrap_when_bundle_is_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(root / "Wiki.app"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                }
            )
            with patch.object(daemon, "_launchctl") as launchctl:
                with self.assertRaisesRegex(daemon.DaemonError, "missing or not executable"):
                    daemon.install(config)
            launchctl.assert_not_called()

    def test_default_install_uses_frozen_bundle_executable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = (
                root
                / "Wiki.app"
                / "Contents"
                / "Resources"
                / "wiki-backend-sidecar"
                / "wiki-backend"
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"frozen backend")
            executable.chmod(0o755)
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(root / "Wiki.app"),
                    "WIKI_VAULT_DIR": str(root / "vault"),
                    "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                }
            )
            self.assertEqual(config.executable, executable)
            self.assertEqual(config.python_module, "")
            self.assertEqual(
                daemon.plist_payload(config)["ProgramArguments"][0], str(executable)
            )
            self.assertEqual(
                frozen_runtime_fingerprint(executable),
                frozen_runtime_fingerprint(config.executable),
            )

    def test_install_rejects_complete_adhoc_bundle_before_bootstrap(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = (
                root
                / "Wiki.app"
                / "Contents"
                / "Resources"
                / "wiki-backend-sidecar"
                / "wiki-backend"
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"ad-hoc backend")
            executable.chmod(0o755)
            info = root / "Wiki.app" / "Contents" / "Info.plist"
            info.write_bytes(b"plist")
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(root / "Wiki.app"),
                    "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                }
            )
            with patch.object(
                daemon, "_bundle_team_identifier", return_value=None
            ), patch.object(daemon, "_launchctl") as launchctl:
                with self.assertRaisesRegex(
                    daemon.DaemonError, "ad-hoc or unsigned Wiki.app"
                ):
                    daemon.install(config)
            launchctl.assert_not_called()

    def test_signed_built_artifact_install_gate(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("requires macOS codesign and launchd")
        if not os.environ.get("WIKI_NATIVE_SIGNING_IDENTITY"):
            self.skipTest(
                "set WIKI_NATIVE_SIGNING_IDENTITY to run the signed native artifact test"
            )
        bundle = daemon.SOURCE_WIKI_APP_PATH
        if not bundle.is_dir():
            self.skipTest("build a trusted Wiki.app before running the artifact test")
        team = daemon._bundle_team_identifier(bundle)
        if team is None:
            self.skipTest("built Wiki.app has no trusted TeamIdentifier")
        executable = (
            bundle / "Contents" / "Resources" / "wiki-backend-sidecar" / "wiki-backend"
        )
        if not executable.is_file():
            self.skipTest("built Wiki.app has no bundled daemon executable")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(bundle),
                    "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                    "WIKI_DAEMON_LOG_PATH": str(root / "daemon.log"),
                }
            )

            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                if arguments[0] == "print":
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        113,
                        "",
                        daemon._service_absent_message(config),
                    )
                return subprocess.CompletedProcess(["launchctl", *arguments], 0, "", "")

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl), patch.object(
                daemon,
                "_wait_for_healthy",
                return_value={"healthy": True, "payload": {"status": "ok"}},
            ):
                result = daemon.install(config)
            self.assertEqual(result["action"], "installed")
            self.assertEqual(daemon._bundle_team_identifier(bundle), team)

    def test_install_fails_if_bootstrapped_backend_is_unhealthy(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = (
                root
                / "Wiki.app"
                / "Contents"
                / "Resources"
                / "wiki-backend-sidecar"
                / "wiki-backend"
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"stable bundled backend")
            executable.chmod(0o755)
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(root / "Wiki.app"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                }
            )

            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                if arguments[0] == "print":
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        113,
                        "",
                        daemon._service_absent_message(config),
                    )
                return subprocess.CompletedProcess(["launchctl", *arguments], 0, "", "")

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl) as launchctl, patch.object(
                daemon, "_health", return_value={"healthy": False}
            ), patch.object(daemon, "HEALTH_TIMEOUT_SECONDS", 0.0):
                with self.assertRaisesRegex(daemon.DaemonError, "did not become healthy"):
                    daemon.install(config)
            self.assertTrue(
                any(
                    call.args[1:] == ("bootout", config.target)
                    for call in launchctl.call_args_list
                )
            )
            self.assertFalse(config.plist_path.exists())

    def test_failed_upgrade_restores_prior_plist_and_service(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "Wiki.app/Contents/Resources/wiki-backend-sidecar/wiki-backend"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"new backend")
            executable.chmod(0o755)
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(root / "Wiki.app"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                }
            )
            config.plist_path.parent.mkdir(parents=True)
            prior_executable = root / "prior" / "wiki-backend"
            prior_executable.parent.mkdir(parents=True)
            prior_executable.write_bytes(b"prior backend")
            prior_executable.chmod(0o755)
            prior_config = daemon.DaemonConfig(
                **{**config.__dict__, "executable": prior_executable}
            )
            prior_plist = daemon.render_plist(prior_config).encode("utf-8")
            config.plist_path.write_bytes(prior_plist)
            loaded = True
            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                nonlocal loaded
                if arguments[0] == "print":
                    if loaded:
                        return subprocess.CompletedProcess(["launchctl", *arguments], 0, "", "")
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        113,
                        "",
                        daemon._service_absent_message(config),
                    )
                if arguments[0] == "bootout":
                    loaded = False
                elif arguments[0] == "bootstrap":
                    loaded = True
                return subprocess.CompletedProcess(["launchctl", *arguments], 0, "", "")

            def fake_wait(
                current: daemon.DaemonConfig,
                **_kwargs: object,
            ) -> dict[str, object]:
                if current.executable == config.executable:
                    raise daemon.DaemonError("did not become healthy")
                self.assertEqual(current.executable, prior_executable)
                return {
                    "healthy": True,
                    "identity_matches": True,
                    "payload": {
                        "status": "ok",
                        "daemon_managed": True,
                        "backend_fingerprint": daemon._expected_backend_fingerprint(
                            prior_config
                        ),
                    },
                }

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl), patch.object(
                daemon, "_wait_for_healthy", side_effect=fake_wait
            ):
                with self.assertRaisesRegex(daemon.DaemonError, "did not become healthy"):
                    daemon.install(config)

            self.assertEqual(config.plist_path.read_bytes(), prior_plist)
            self.assertTrue(loaded)

    def test_failed_upgrade_verifies_restored_prior_port(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "Wiki.app/Contents/Resources/wiki-backend-sidecar/wiki-backend"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"new backend")
            executable.chmod(0o755)
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(root / "Wiki.app"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                    "WIKI_BACKEND_PORT": "18213",
                }
            )
            config.plist_path.parent.mkdir(parents=True)
            prior_executable = root / "prior" / "wiki-backend"
            prior_executable.parent.mkdir(parents=True)
            prior_executable.write_bytes(b"prior backend")
            prior_executable.chmod(0o755)
            prior_config = daemon.DaemonConfig(
                **{
                    **config.__dict__,
                    "executable": prior_executable,
                    "port": 18214,
                }
            )
            config.plist_path.write_bytes(daemon.render_plist(prior_config).encode())
            loaded = True

            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                nonlocal loaded
                if arguments[0] == "print":
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        0 if loaded else 113,
                        "" if loaded else daemon._service_absent_message(config),
                        "",
                    )
                if arguments[0] == "bootout":
                    loaded = False
                elif arguments[0] == "bootstrap":
                    loaded = True
                return subprocess.CompletedProcess(["launchctl", *arguments], 0, "", "")

            seen_ports: list[int] = []

            def fake_wait(
                current: daemon.DaemonConfig,
                **_kwargs: object,
            ) -> dict[str, object]:
                seen_ports.append(current.port)
                if current.executable == config.executable:
                    raise daemon.DaemonError("new daemon did not become healthy")
                self.assertEqual(current.port, 18214)
                return {
                    "healthy": True,
                    "identity_matches": True,
                    "payload": {
                        "status": "ok",
                        "daemon_managed": True,
                        "backend_fingerprint": daemon._expected_backend_fingerprint(
                            prior_config
                        ),
                    },
                }

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl), patch.object(
                daemon, "_wait_for_healthy", side_effect=fake_wait
            ):
                with self.assertRaisesRegex(
                    daemon.DaemonError, "new daemon did not become healthy"
                ):
                    daemon.install(config)

            self.assertEqual(seen_ports, [18213, 18214])
            self.assertTrue(loaded)

    def test_loaded_unparseable_prior_plist_aborts_without_mutation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "Wiki.app/Contents/Resources/wiki-backend-sidecar/wiki-backend"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"new backend")
            executable.chmod(0o755)
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(root / "Wiki.app"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                }
            )
            config.plist_path.parent.mkdir(parents=True)
            config.plist_path.write_bytes(b"prior working plist\n")
            before = config.plist_path.read_bytes()
            loaded = True

            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                nonlocal loaded
                if arguments[0] == "print":
                    if loaded:
                        return subprocess.CompletedProcess(["launchctl", *arguments], 0, "", "")
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        113,
                        "",
                        daemon._service_absent_message(config),
                    )
                if arguments[0] == "bootout":
                    loaded = False
                elif arguments[0] == "bootstrap":
                    loaded = True
                return subprocess.CompletedProcess(["launchctl", *arguments], 0, "", "")

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl) as launchctl:
                with self.assertRaisesRegex(daemon.DaemonError, "verify the loaded prior"):
                    daemon.install(config)

            mutation_calls = [
                call
                for call in launchctl.call_args_list
                if call.args[1:] in {
                    ("bootout", config.target),
                    ("bootstrap", config.domain, str(config.plist_path)),
                }
            ]
            self.assertEqual(mutation_calls, [])
            self.assertTrue(loaded)
            self.assertEqual(config.plist_path.read_bytes(), before)

    def test_failed_install_reports_health_and_cleanup_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "Wiki.app/Contents/Resources/wiki-backend-sidecar/wiki-backend"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"new backend")
            executable.chmod(0o755)
            config = daemon.config_from_env(
                overrides={
                    "WIKI_APP_PATH": str(root / "Wiki.app"),
                    "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                }
            )

            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                if arguments[0] == "print":
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        113,
                        "",
                        daemon._service_absent_message(config),
                    )
                if arguments[0] == "bootout":
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments], 1, "", "cleanup failed"
                    )
                return subprocess.CompletedProcess(["launchctl", *arguments], 0, "", "")

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl), patch.object(
                daemon, "_health", return_value={"healthy": False}
            ), patch.object(daemon, "HEALTH_TIMEOUT_SECONDS", 0.0):
                with self.assertRaisesRegex(
                    daemon.DaemonError, "did not become healthy.*cleanup failed"
                ):
                    daemon.install(config)
            self.assertFalse(config.plist_path.exists())


class DaemonHandshakeTests(unittest.TestCase):
    def test_daemon_rejects_external_frontend_override(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            driver = root / "driver.py"
            driver.write_text(
                "from backend import native_server\nnative_server.main()\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "WIKI_FRONTEND_DIST": str(root / "replacement-frontend"),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(driver),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18213",
                    "--daemon",
                    "--frontend-dist",
                    str(root / "replacement-frontend"),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("external frontend overrides", result.stderr)

    def test_security_framework_reads_live_pid_identity(self) -> None:
        if sys.platform != "darwin" or not Path("/usr/bin/osascript").is_file():
            self.skipTest("requires macOS Security.framework")
        process = subprocess.Popen(["/usr/bin/osascript", "-e", "delay 2"])
        try:
            identity = native_server._security_code_identity(process.pid)
        finally:
            process.terminate()
            process.wait(timeout=2)
        self.assertIsNotNone(identity)
        self.assertEqual(identity[0], "com.apple.osascript")
        self.assertIsNone(identity[1])
        self.assertTrue(identity[2])

    def test_forged_adhoc_peer_with_same_identifier_is_rejected(self) -> None:
        details = "\n".join(
            [
                "Identifier=com.hwang2409.wiki",
                "Signature=adhoc",
                "TeamIdentifier=not set",
            ]
        )
        with patch.object(native_server, "_bundle_team_identifier", return_value="ABCDE12345"), patch.object(
            native_server, "_peer_pid", return_value=123
        ), patch.object(
            native_server, "_peer_executable", return_value=Path("/tmp/forged-wiki")
        ), patch.object(
            native_server,
            "_security_code_identity",
            return_value=("com.hwang2409.wiki", None, frozenset({"forged"})),
        ), patch.object(
            native_server.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["codesign"], 0, "", details
            ),
        ), socket.socket() as peer:
            self.assertFalse(native_server.is_trusted_tauri_peer(peer))

    def test_selected_adhoc_bundle_is_rejected_for_daemon_handoff(self) -> None:
        with TemporaryDirectory() as tmp:
            app = Path(tmp) / "Wiki.app"
            selected = app / "Contents" / "MacOS" / "wiki-native"
            selected.parent.mkdir(parents=True)
            selected.write_bytes(b"selected ad-hoc executable")
            selected.chmod(0o755)
            forged = Path(tmp) / "forged-wiki"
            forged.write_bytes(b"forged ad-hoc executable")
            forged.chmod(0o755)
            with (app / "Contents" / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleExecutable": "wiki-native",
                        "CFBundleIdentifier": native_server.TAURI_BUNDLE_IDENTIFIER,
                    },
                    handle,
                )
            details = "\n".join(
                [
                    "Identifier=com.hwang2409.wiki",
                    "Signature=adhoc",
                    "CDHash=0123456789abcdef",
                ]
            )

            def fake_run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if len(arguments) > 1 and arguments[1] == "-dvvv":
                    if Path(arguments[-1]) in {app, selected}:
                        return subprocess.CompletedProcess(arguments, 0, "", details)
                    return subprocess.CompletedProcess(arguments, 1, "", "unsigned launcher")
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with patch.object(native_server, "TAURI_BUNDLE_PATH", app), patch.object(
                native_server, "_peer_pid", return_value=123
            ), patch.object(native_server, "_peer_executable", return_value=selected), patch.object(
                native_server, "_security_code_identity", return_value=(
                    native_server.TAURI_BUNDLE_IDENTIFIER,
                    None,
                    frozenset({"0123456789abcdef"}),
                )
            ), patch.object(native_server.subprocess, "run", side_effect=fake_run
            ), socket.socket() as peer:
                self.assertFalse(native_server.is_trusted_tauri_peer(peer))

            with patch.object(native_server, "TAURI_BUNDLE_PATH", app), patch.object(
                native_server, "_peer_pid", return_value=123
            ), patch.object(native_server, "_peer_executable", return_value=forged), patch.object(
                native_server, "_security_code_identity", return_value=(
                    native_server.TAURI_BUNDLE_IDENTIFIER,
                    None,
                    frozenset({"0123456789abcdef"}),
                )
            ), patch.object(native_server.subprocess, "run", side_effect=fake_run
            ), socket.socket() as peer:
                self.assertFalse(native_server.is_trusted_tauri_peer(peer))

            with patch.object(native_server, "TAURI_BUNDLE_PATH", app), patch.object(
                native_server, "_peer_pid", return_value=123
            ), patch.object(native_server, "_peer_executable", return_value=selected), patch.object(
                native_server, "_security_code_identity", return_value=(
                    native_server.TAURI_BUNDLE_IDENTIFIER,
                    None,
                    frozenset({"old-process-identity"}),
                )
            ), patch.object(native_server.subprocess, "run", side_effect=fake_run
            ), socket.socket() as peer:
                self.assertFalse(native_server.is_trusted_tauri_peer(peer))

    def test_real_adhoc_bundle_is_rejected_for_daemon_handoff(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "src-tauri"
            / "target"
            / "release"
            / "bundle"
            / "macos"
            / "Wiki.app"
        )
        if not source.is_dir() or not Path("/usr/bin/codesign").is_file():
            self.skipTest("requires a locally built macOS Wiki.app")

        with TemporaryDirectory() as tmp:
            app = Path(tmp) / "Wiki.app"
            (app / "Contents").mkdir(parents=True)
            shutil.copy2(source / "Contents" / "Info.plist", app / "Contents" / "Info.plist")
            shutil.copytree(
                source / "Contents" / "MacOS",
                app / "Contents" / "MacOS",
            )
            code_signature = source / "Contents" / "_CodeSignature"
            if code_signature.is_dir():
                shutil.copytree(code_signature, app / "Contents" / "_CodeSignature")
            launcher = app / "Contents" / "MacOS" / "wiki-backend"
            main = app / "Contents" / "MacOS" / "wiki-native"
            subprocess.run(["/usr/bin/codesign", "--remove-signature", str(launcher)], check=True)
            forged = Path(tmp) / "forged-wiki-native"
            shutil.copy2(main, forged)

            with patch.object(native_server, "TAURI_BUNDLE_PATH", app), patch.object(
                native_server, "_peer_pid", return_value=123
            ), patch.object(native_server, "_peer_executable", return_value=main), patch.object(
                native_server,
                "_security_code_identity",
                return_value=(
                    native_server.TAURI_BUNDLE_IDENTIFIER,
                    None,
                    native_server._code_directory_identities(
                        native_server._codesign_details(main) or []
                    ),
                ),
            ), socket.socket() as peer:
                self.assertFalse(native_server.is_trusted_tauri_peer(peer))

            with patch.object(native_server, "TAURI_BUNDLE_PATH", app), patch.object(
                native_server, "_peer_pid", return_value=123
            ), patch.object(native_server, "_peer_executable", return_value=forged), patch.object(
                native_server,
                "_security_code_identity",
                return_value=(
                    native_server.TAURI_BUNDLE_IDENTIFIER,
                    None,
                    native_server._code_directory_identities(
                        native_server._codesign_details(main) or []
                    ),
                ),
            ), socket.socket() as peer:
                self.assertFalse(native_server.is_trusted_tauri_peer(peer))

    def test_developer_signature_requires_team_and_designated_requirement(self) -> None:
        details = "\n".join(
            [
                "Identifier=com.hwang2409.wiki",
                "Signature=CMS",
                "TeamIdentifier=ABCDE12345",
                "Authority=Apple Development: Wiki",
                "CDHash=0123456789abcdef",
            ]
        )
        calls: list[list[str]] = []

        def fake_run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", details)

        with patch.object(native_server, "_bundle_team_identifier", return_value="ABCDE12345"), patch.object(
            native_server, "_peer_pid", return_value=123
        ), patch.object(
            native_server, "_peer_executable", return_value=Path("/tmp/signed-wiki")
        ), patch.object(
            native_server,
            "_security_code_identity",
            return_value=(
                native_server.TAURI_BUNDLE_IDENTIFIER,
                "ABCDE12345",
                frozenset({"0123456789abcdef"}),
            ),
        ), patch.object(native_server.subprocess, "run", side_effect=fake_run), socket.socket() as peer:
            self.assertTrue(native_server.is_trusted_tauri_peer(peer))

        self.assertEqual(
            calls[1][0:4],
            ["/usr/bin/codesign", "--verify", "--strict", "--test-requirement"],
        )
        self.assertIn('anchor apple generic', calls[1][4])
        self.assertIn('certificate leaf[subject.OU] = "ABCDE12345"', calls[1][4])

    def test_auth_thread_survives_client_disconnect_before_send(self) -> None:
        with TemporaryDirectory() as tmp:
            first_checked = threading.Event()
            calls = 0

            def peer_checker(connection: socket.socket) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    first_checked.set()
                    connection.close()
                return True

            server = DaemonAuthSocket(
                Path(tmp) / "runtime", "survives", peer_checker=peer_checker
            )
            server.start()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first:
                first.connect(str(server.path))
                self.assertTrue(first_checked.wait(timeout=1))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second:
                second.settimeout(1)
                second.connect(str(server.path))
                self.assertEqual(second.recv(4096).decode().strip(), "survives")
            server.close()

    def test_sidecar_pipe_secret_is_not_in_child_environment(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            driver = root / "sidecar_pipe.py"
            child_env_copy = root / "child-env.txt"
            driver.write_text(
                textwrap.dedent(
                    f"""
                    import os
                    import subprocess
                    import sys
                    from pathlib import Path
                    from backend.native_server import read_sidecar_secret

                    os.environ["WIKI_APP_SECRET"] = "pipe-secret"
                    secret = read_sidecar_secret()
                    Path({str(root / 'secret.txt')!r}).write_text(secret, encoding="utf-8")
                    child = subprocess.run(
                        [sys.executable, "-c", "import os; print(os.environ.get('WIKI_APP_SECRET', '<absent>'))"],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    Path({str(child_env_copy)!r}).write_text(child.stdout, encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
            subprocess.run(
                [sys.executable, str(driver)],
                input="pipe-secret\n",
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual((root / "secret.txt").read_text(encoding="utf-8"), "pipe-secret")
            self.assertEqual(child_env_copy.read_text(encoding="utf-8").strip(), "<absent>")

    def test_untrusted_sibling_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            server = DaemonAuthSocket(Path(tmp) / "runtime", "not-for-siblings")
            server.start()
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import socket, sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); print(s.recv(4096).decode())",
                    str(server.path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            server.close()
            self.assertEqual(result.stdout.strip(), "")

    def test_overlapping_starts_and_reversed_shutdown_keep_socket_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp) / "runtime"
            first = DaemonAuthSocket(runtime_dir, "first")
            first.start()
            second = DaemonAuthSocket(runtime_dir, "second")
            with self.assertRaises(RuntimeError):
                second.start()

            first.close()
            second.start()
            first.close()
            self.assertTrue(second.path.exists())
            second.close()

    def test_handshake_reissues_secret_after_daemon_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp) / "runtime"

            first = DaemonAuthSocket(
                runtime_dir, "secret-before-restart", peer_checker=lambda _socket: True
            )
            first.start()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(first.path))
                self.assertEqual(
                    client.recv(4096).decode().strip(), "secret-before-restart"
                )
            first.close()

            second = DaemonAuthSocket(
                runtime_dir, "secret-after-restart", peer_checker=lambda _socket: True
            )
            second.start()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(second.path))
                self.assertEqual(
                    client.recv(4096).decode().strip(), "secret-after-restart"
                )
            second.close()

            self.assertFalse(first.path.exists())
            self.assertTrue((runtime_dir / "wiki-app-secret.lock").exists())


class DaemonCliTests(unittest.TestCase):
    def _write_launchctl_stub(self, root: Path) -> tuple[Path, Path]:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        log_path = root / "launchctl.log"
        stub = bin_dir / "launchctl"
        stub.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                printf '%s\\n' "$*" >> "{log_path}"
                if [ "$1" = "print" ]; then
                    if [ -f "{root / 'loaded'}" ]; then
                        printf 'pid = %s\\n' "$(cat \"{root / 'server.pid'}\")"
                        exit 0
                    fi
                    printf 'Bad request.\\nCould not find service "{daemon.DEFAULT_LABEL}" in domain for user gui: %s\\n' "$(id -u)" >&2
                    exit 113
                fi
                if [ "$1" = "bootout" ]; then
                    rm -f "{root / 'loaded'}"
                    exit 0
                fi
                if [ "$1" = "bootstrap" ]; then
                    touch "{root / 'loaded'}"
                fi
                exit 0
                """
            ),
            encoding="utf-8",
        )
        stub.chmod(0o755)
        return bin_dir, log_path

    def test_cli_install_status_uninstall_use_mocked_launchctl(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        wiki_cli = repo_root / "wiki"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = (
                daemon._process_executable(os.getpid())
                or Path(sys.executable).resolve()
            )
            fingerprint = frozen_runtime_fingerprint(executable)

            class HealthHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    if self.path != "/health":
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = json.dumps(
                        {
                            "status": "ok",
                            "daemon_managed": True,
                            "backend_fingerprint": fingerprint,
                            "process_id": os.getpid(),
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, _format: str, *_args: object) -> None:
                    pass

            health_server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), HealthHandler
            )
            health_thread = threading.Thread(
                target=health_server.serve_forever, daemon=True
            )
            health_thread.start()
            (root / "server.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            bin_dir, launchctl_log = self._write_launchctl_stub(root)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                "WIKI_DAEMON_LOG_PATH": str(root / "logs" / "backend.log"),
                "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                "WIKI_REPO_DIR": str(root / "repo"),
                "WIKI_VAULT_DIR": str(root / "vault"),
                "WIKI_APP_PATH": str(root / "Wiki.app"),
                "WIKI_BACKEND_EXECUTABLE": str(executable),
                "WIKI_BACKEND_PORT": str(health_server.server_port),
            }
            commands = [
                ["daemon", "install", "--json"],
                ["daemon", "status", "--json"],
                ["daemon", "uninstall", "--json"],
            ]
            try:
                results = [
                    subprocess.run(
                        [sys.executable, str(wiki_cli), *command],
                        capture_output=True,
                        text=True,
                        env=env,
                        check=False,
                        timeout=20,
                    )
                    for command in commands
                ]
            finally:
                health_server.shutdown()
                health_thread.join(timeout=2)
                health_server.server_close()
            for result in results:
                self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(
                [line.split(" ", 1)[0] for line in launchctl_log.read_text().splitlines()],
                [
                    "print",
                    "bootstrap",
                    "print",
                    "print",
                    "print",
                    "bootout",
                    "print",
                ],
            )
            self.assertFalse((root / "LaunchAgents" / f"{daemon.DEFAULT_LABEL}.plist").exists())

    def test_cli_rejects_unsafe_and_corrupt_labels_for_all_operations(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        wiki_cli = repo_root / "wiki"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            launch_agents = root / "LaunchAgents"
            base_env = {
                **os.environ,
                "WIKI_AGENT_RUNTIME_DIR": str(runtime),
                "WIKI_LAUNCH_AGENTS_DIR": str(launch_agents),
            }
            for label in ("/tmp/wiki-review31.plist", "../outside"):
                for operation in ("install", "status", "uninstall"):
                    with self.subTest(label=label, operation=operation):
                        result = subprocess.run(
                            [
                                sys.executable,
                                str(wiki_cli),
                                "daemon",
                                operation,
                                "--label",
                                label,
                                "--json",
                            ],
                            capture_output=True,
                            text=True,
                            env=base_env,
                            check=False,
                            timeout=5,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("safe reverse-DNS", result.stderr)

            runtime.mkdir(parents=True)
            daemon.daemon_settings_path(runtime).write_text(
                '{"label":"../../outside","port":8213}\n',
                encoding="utf-8",
            )
            for operation in ("install", "status", "uninstall"):
                with self.subTest(corrupt_settings=True, operation=operation):
                    result = subprocess.run(
                        [sys.executable, str(wiki_cli), "daemon", operation, "--json"],
                        capture_output=True,
                        text=True,
                        env=base_env,
                        check=False,
                        timeout=5,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("safe reverse-DNS", result.stderr)


if __name__ == "__main__":
    unittest.main()
