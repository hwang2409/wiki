"""Wiki CLI regression tests.

Only covers the WIKI-19 addition (--session round-trip on `agent update`).
The rest of the CLI has manual QA coverage.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
