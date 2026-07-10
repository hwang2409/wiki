"""Wiki CLI regression tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[2]
WIKI_CLI = REPO_ROOT / "wiki"


class AgentUpdateSessionTests(unittest.TestCase):
    def _run(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WIKI_CLI), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def test_session_id_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            worktree = Path(tmp) / "wt-15"
            worktree.mkdir()
            registry_path.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(worktree),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session": 1,
                    }
                }
            }))
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}
            proc = self._run(
                ["agent", "update", "WIKI-15", "--session", "sess-abc-123"],
                env=env,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("session_id=sess-abc-123", proc.stdout)
            reloaded = json.loads(registry_path.read_text())
            self.assertEqual(
                reloaded["WIKI-15"]["current"]["session_id"], "sess-abc-123"
            )
            # Doesn't clobber the integer session-count field.
            self.assertEqual(reloaded["WIKI-15"]["current"]["session"], 1)

    def test_update_without_session_still_works(self) -> None:
        """Backwards compat: existing fields still round-trip; --session is opt-in."""
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            registry_path.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "session": 1,
                    }
                }
            }))
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}
            proc = self._run(["agent", "update", "WIKI-15", "--window", "@99"], env=env)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            reloaded = json.loads(registry_path.read_text())
            self.assertEqual(reloaded["WIKI-15"]["current"]["window"], "@99")
            self.assertNotIn("session_id", reloaded["WIKI-15"]["current"])

    def test_orchestrator_registration_persists_model(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            env = {
                **os.environ,
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
                "CLAUDE_CODE_SESSION_ID": "claude-session-1",
            }
            proc = self._run(["agent", "orch", "wiki", "--window", "@9", "--model", "sonnet"], env=env)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            reloaded = json.loads(registry_path.read_text())
            orch = reloaded["_orchestrators"]["wiki"]
            self.assertEqual(orch["window"], "@9")
            self.assertEqual(orch["model"], "sonnet")
            self.assertEqual(orch["session_id"], "claude-session-1")

    def test_update_rejects_supervisor_owned_run(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-15": {
                            "current": {
                                "run_id": "00000000-0000-4000-8000-000000000015",
                                "window": None,
                                "kind": "cdx",
                                "role": "implement",
                            }
                        }
                    }
                )
            )
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}
            proc = self._run(["agent", "update", "WIKI-15", "--window", "@99"], env=env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("supervisor-owned headless run WIKI-15", proc.stderr)
            reloaded = json.loads(registry_path.read_text())
            self.assertIsNone(reloaded["WIKI-15"]["current"]["window"])

    def test_register_rejects_supervisor_owned_run(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-15": {
                            "current": {
                                "run_id": "00000000-0000-4000-8000-000000000015",
                                "window": None,
                                "kind": "cdx",
                                "role": "implement",
                            }
                        }
                    }
                )
            )
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}
            proc = self._run(
                [
                    "agent",
                    "register",
                    "WIKI-15",
                    "--window",
                    "@42",
                    "--kind",
                    "cdx",
                    "--role",
                    "implement",
                ],
                env=env,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("supervisor-owned headless run WIKI-15", proc.stderr)

    def test_orchestrator_registration_rejects_supervisor_owned_id(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "wiki": {
                            "current": {
                                "run_id": "00000000-0000-4000-8000-000000000099",
                                "window": None,
                                "kind": "cc",
                                "role": "orchestrator",
                            }
                        }
                    }
                )
            )
            env = {
                **os.environ,
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
                "CLAUDE_CODE_SESSION_ID": "claude-session-1",
            }
            proc = self._run(["agent", "orch", "wiki", "--window", "@9"], env=env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("supervisor-owned headless run wiki", proc.stderr)


class AgentDoneWindowCleanupTests(unittest.TestCase):
    def _run(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WIKI_CLI), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def _write_registry(self, path: Path, ticket: str, window: str = "@12") -> None:
        path.write_text(
            json.dumps(
                {
                    ticket: {
                        "current": {
                            "window": window,
                            "kind": "cdx",
                            "role": "implement",
                            "log": f"/tmp/cdx-{ticket}.log",
                        },
                        "history": [],
                    }
                }
            ),
            encoding="utf-8",
        )

    def _write_archive(self, home: Path, ticket: str) -> Path:
        session_dir = home / "me" / "fun" / "agent-archive" / ticket / "20260708-183300"
        session_dir.mkdir(parents=True)
        meta_path = session_dir / "meta.json"
        meta_path.write_text("{}\n", encoding="utf-8")
        return meta_path

    def _install_tmux_stub(self, bin_dir: Path) -> Path:
        script = bin_dir / "tmux"
        script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                if args == ["list-windows", "-a", "-F", "#{window_id}"]:
                    sys.stdout.write(os.environ.get("TMUX_WINDOWS", ""))
                    sys.exit(int(os.environ.get("TMUX_LIST_EXIT", "0")))
                if len(args) == 3 and args[:2] == ["kill-window", "-t"]:
                    log_path = os.environ.get("TMUX_KILL_LOG")
                    if log_path:
                        with Path(log_path).open("a", encoding="utf-8") as handle:
                            handle.write(args[2] + "\\n")
                    sys.exit(int(os.environ.get("TMUX_KILL_EXIT", "0")))
                sys.exit(97)
                """
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def test_done_kills_exact_recorded_window(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            kill_log = tmp_path / "killed.log"
            self._write_registry(registry_path, "WIKI-26", window="@12")
            meta_path = self._write_archive(tmp_path, "WIKI-26")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._install_tmux_stub(bin_dir)
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "TMUX_WINDOWS": "@12\n@999\n",
                "TMUX_KILL_LOG": str(kill_log),
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
            }

            proc = self._run(["agent", "done", "WIKI-26", "--outcome", "merged"], env=env)

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("deregistered WIKI-26 (outcome: merged)", proc.stdout)
            self.assertIn("killed window @12", proc.stdout)
            self.assertEqual(kill_log.read_text(encoding="utf-8"), "@12\n")
            self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8")), {})
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["outcome"], "merged")
            self.assertEqual(meta["worker"]["window"], "@12")
            self.assertIn("ended_at", meta)

    def test_done_keep_window_skips_tmux_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            kill_log = tmp_path / "killed.log"
            self._write_registry(registry_path, "WIKI-26", window="@12")
            self._write_archive(tmp_path, "WIKI-26")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._install_tmux_stub(bin_dir)
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "TMUX_WINDOWS": "@12\n",
                "TMUX_KILL_LOG": str(kill_log),
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
            }

            proc = self._run(
                ["agent", "done", "WIKI-26", "--outcome", "merged", "--keep-window"],
                env=env,
            )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("kept window @12", proc.stdout)
            self.assertFalse(kill_log.exists())

    def test_done_reports_window_already_gone(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            kill_log = tmp_path / "killed.log"
            self._write_registry(registry_path, "WIKI-26", window="@12")
            self._write_archive(tmp_path, "WIKI-26")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._install_tmux_stub(bin_dir)
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "TMUX_WINDOWS": "@999\n",
                "TMUX_KILL_LOG": str(kill_log),
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
            }

            proc = self._run(["agent", "done", "WIKI-26", "--outcome", "closed"], env=env)

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("window already gone: @12", proc.stdout)
            self.assertFalse(kill_log.exists())

    def test_done_tolerates_tmux_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            self._write_registry(registry_path, "WIKI-26", window="@12")
            self._write_archive(tmp_path, "WIKI-26")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": str(bin_dir),
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
            }

            proc = self._run(["agent", "done", "WIKI-26", "--outcome", "abandoned"], env=env)

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("window already gone: @12", proc.stdout)
            self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8")), {})

    def test_done_rejects_supervisor_owned_run(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-26": {
                            "current": {
                                "run_id": "00000000-0000-4000-8000-000000000026",
                                "window": None,
                                "kind": "cdx",
                                "role": "implement",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}

            proc = self._run(["agent", "done", "WIKI-26", "--outcome", "merged"], env=env)

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("supervisor-owned headless run WIKI-26", proc.stderr)
            self.assertIn("WIKI-26", json.loads(registry_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
