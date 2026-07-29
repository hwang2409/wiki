from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.app.agent_runtime import rebase_bot


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


def _conflicting_repo(root: Path, *, semantic: bool = False) -> Path:
    origin = root / "origin.git"
    worktree = root / "worktree"
    _run(root, "git", "init", "--bare", str(origin))
    _run(root, "git", "init", str(worktree))
    _run(worktree, "git", "config", "user.email", "test@example.com")
    _run(worktree, "git", "config", "user.name", "test")
    base = 'def value():\n    return "base"\n' if semantic else "import base\n"
    ours = 'def value():\n    return "ours"\n' if semantic else "import ours\n"
    theirs = 'def value():\n    return "theirs"\n' if semantic else "import theirs\n"
    (worktree / "fixture.py").write_text(base, encoding="utf-8")
    _run(worktree, "git", "add", "fixture.py")
    _run(worktree, "git", "commit", "-m", "base")
    _run(worktree, "git", "branch", "-M", "main")
    _run(worktree, "git", "remote", "add", "origin", str(origin))
    _run(worktree, "git", "push", "-u", "origin", "main")
    _run(worktree, "git", "checkout", "-b", "feature")
    (worktree / "fixture.py").write_text(ours, encoding="utf-8")
    _run(worktree, "git", "commit", "-am", "feature")
    _run(worktree, "git", "checkout", "main")
    (worktree / "fixture.py").write_text(theirs, encoding="utf-8")
    _run(worktree, "git", "commit", "-am", "upstream")
    _run(worktree, "git", "push", "origin", "main")
    _run(worktree, "git", "checkout", "feature")
    return worktree


class RebaseBotTests(unittest.TestCase):
    def test_clean_gate_is_a_noop(self) -> None:
        result = rebase_bot.rebase_dirty_pr(
            175,
            "WIKI-175",
            "WIKI-175-IMPL",
            gate=lambda _pr: {"raw": {"mergeable": "MERGEABLE", "head_sha": "abc"}},
        )
        self.assertEqual(result["status"], "resolved")
        self.assertTrue(result["no_op"])

    def test_dirty_gate_spawns_luna_helper_in_existing_worktree(self) -> None:
        spawned: list[object] = []
        fake_main = SimpleNamespace(
            SpawnWorkerIn=lambda **kwargs: SimpleNamespace(**kwargs),
            _registry_agent=lambda _registry, _worker: (
                "WIKI-175-IMPL",
                {},
                {"worktree": tempfile.gettempdir(), "orch": "wiki"},
            ),
            _read_agent_registry=lambda: {},
            spawn_agent=lambda request: (
                spawned.append(request) or {"run_id": "run-rebase"}
            ),
        )
        with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
            result = rebase_bot.rebase_dirty_pr(
                175,
                "WIKI-175",
                "WIKI-175-IMPL",
                gate=lambda _pr: {"raw": {"mergeable": "CONFLICTING"}},
            )
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(len(spawned), 1)
        request = spawned[0]
        self.assertEqual(request.model, "gpt-5.6-luna")
        self.assertEqual(request.effort, "low")
        self.assertEqual(request.ticket, "WIKI-175-REBASE")
        self.assertIn("never force-push", request.prompt)

    def test_mechanical_import_conflict_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], push=False
            )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolved_files"], ["fixture.py"])

    def test_semantic_conflict_aborts_without_push(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw), semantic=True)
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], push=True
            )
            after = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(before, after)
        self.assertTrue(result["escalated_hunks"])

    def test_smoke_failure_escalates_and_restores_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            result = rebase_bot.run_rebase_helper(
                worktree,
                smoke_commands=[
                    ([sys.executable, "-c", "raise SystemExit(3)"], worktree)
                ],
                push=True,
            )
            after = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(before, after)
        self.assertIn("smoke test", result["escalated_hunks"][0])


if __name__ == "__main__":
    unittest.main()
