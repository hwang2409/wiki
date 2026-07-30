"""LaunchAgent and persistent backend lifecycle tests."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app import daemon
from backend.native_server import rotate_log_file


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

    def test_install_does_not_touch_runtime_or_run_archive(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            calls: list[tuple[str, ...]] = []

            def fake_launchctl(
                _config: daemon.DaemonConfig, *arguments: str
            ) -> subprocess.CompletedProcess[str]:
                calls.append(arguments)
                if arguments[0] == "bootout":
                    return subprocess.CompletedProcess(
                        ["launchctl", *arguments],
                        1,
                        "",
                        "Could not find service",
                    )
                return subprocess.CompletedProcess(
                    ["launchctl", *arguments], 0, "", ""
                )

            with patch.object(daemon, "_launchctl", side_effect=fake_launchctl):
                result = daemon.install(config)

            self.assertEqual(result["action"], "installed")
            self.assertEqual([call[0] for call in calls], ["bootout", "bootstrap"])
            self.assertTrue(config.plist_path.is_file())
            self.assertFalse(config.runtime_dir.exists())
            self.assertFalse((root / "agent-archive").exists())

    def test_uninstall_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            config.launch_agents_dir.mkdir()
            config.plist_path.write_text("plist", encoding="utf-8")
            with patch.object(
                daemon,
                "_launchctl",
                return_value=subprocess.CompletedProcess(
                    ["launchctl"], 1, "", "Could not find service"
                ),
            ):
                result = daemon.uninstall(config)
            self.assertEqual(result["action"], "uninstalled")
            self.assertFalse(config.plist_path.exists())


class DaemonLogTests(unittest.TestCase):
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
                if [ "$1" = "bootout" ]; then
                    echo "Could not find service" >&2
                    exit 1
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
                ["bootout", "bootstrap", "print", "bootout"],
            )
            self.assertFalse((root / "LaunchAgents" / f"{daemon.DEFAULT_LABEL}.plist").exists())


if __name__ == "__main__":
    unittest.main()
