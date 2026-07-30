"""LaunchAgent and persistent backend lifecycle tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
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

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl):
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
            self.assertEqual(after, before)

    def test_uninstall_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            config.launch_agents_dir.mkdir()
            config.plist_path.write_text("plist", encoding="utf-8")
            with patch.object(
                daemon,
                "_launchctl",
                side_effect=lambda _config, *arguments: subprocess.CompletedProcess(
                    ["launchctl", *arguments],
                    113 if arguments[0] == "bootout" or arguments[0] == "print" else 0,
                    "",
                    daemon._service_absent_message(config),
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
    def test_default_install_uses_frozen_bundle_executable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "dist" / "wiki-backend-sidecar" / "wiki-backend"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"frozen backend")
            config = daemon.config_from_env(
                overrides={
                    "WIKI_REPO_DIR": str(root),
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


class DaemonHandshakeTests(unittest.TestCase):
    def test_handshake_reissues_secret_after_daemon_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp) / "runtime"

            first = DaemonAuthSocket(runtime_dir, "secret-before-restart")
            first.start()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(first.path))
                self.assertEqual(
                    client.recv(4096).decode().strip(), "secret-before-restart"
                )
            first.close()

            second = DaemonAuthSocket(runtime_dir, "secret-after-restart")
            second.start()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(second.path))
                self.assertEqual(
                    client.recv(4096).decode().strip(), "secret-after-restart"
                )
            second.close()

            self.assertFalse(first.path.exists())
            self.assertEqual(list(runtime_dir.iterdir()), [])


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
            bin_dir, launchctl_log = self._write_launchctl_stub(root)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "WIKI_LAUNCH_AGENTS_DIR": str(root / "LaunchAgents"),
                "WIKI_DAEMON_LOG_PATH": str(root / "logs" / "backend.log"),
                "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                "WIKI_REPO_DIR": str(root / "repo"),
                "WIKI_VAULT_DIR": str(root / "vault"),
            }
            commands = [
                ["daemon", "install", "--json"],
                ["daemon", "status", "--json"],
                ["daemon", "uninstall", "--json"],
            ]
            results = [
                subprocess.run(
                    [sys.executable, str(wiki_cli), *command],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                    timeout=15,
                )
                for command in commands
            ]
            for result in results:
                self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(
                [line.split(" ", 1)[0] for line in launchctl_log.read_text().splitlines()],
                ["print", "bootstrap", "print", "bootout", "print"],
            )
            self.assertFalse((root / "LaunchAgents" / f"{daemon.DEFAULT_LABEL}.plist").exists())


if __name__ == "__main__":
    unittest.main()
