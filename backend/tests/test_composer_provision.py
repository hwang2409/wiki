"""Tests for /api/composer/provision-worktree — WIKI-148.

Cover the multi-orchestrator registry lookup, worker-session rejection,
git-fetch failure surfacing, and destructive branch-reset guardrails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from backend.app import main


def _init_repo(path: Path) -> Path:
    """Create a bare-friendly repo at `path` with an origin that has main."""

    origin = path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)

    seed = path / "seed"
    subprocess.run(["git", "init", "--initial-branch=main", str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True, capture_output=True)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    subprocess.run(
        ["git", "-C", str(seed), "remote", "add", "origin", str(origin)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"],
        check=True, capture_output=True,
    )

    repo = path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    return repo


class ProvisionWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _init_repo(self.root)
        # Route registry lookups to a temp file.
        self._registry_path = self.root / "agent-registry.json"
        self._registry_patch = mock.patch.object(main, "AGENT_REGISTRY_PATH", self._registry_path)
        self._registry_patch.start()

    def tearDown(self) -> None:
        self._registry_patch.stop()
        self.tmp.cleanup()

    def _write_registry(self, payload: dict) -> None:
        self._registry_path.write_text(json.dumps(payload), encoding="utf-8")

    def _orch_registry(self, orch_id: str, repo: Path | None = None) -> dict:
        return {
            orch_id: {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(repo or self.repo),
                    "worktree": str(repo or self.repo),
                    "kind": "cc",
                },
                "history": [],
            }
        }

    def _worker_registry(self, ticket: str) -> dict:
        return {
            ticket: {
                "current": {
                    "role": "implement",
                    "cwd": str(self.repo),
                    "worktree": str(self.repo),
                    "kind": "cc",
                    "orch": "wiki",
                },
                "history": [],
            }
        }

    def test_provisions_new_worktree_off_fresh_origin_main(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="wiki")
        result = main.composer_provision_worktree(body)
        expected = (self.repo / ".claude" / "worktrees" / "wiki-999").resolve()
        self.assertEqual(result["workdir"], str(expected))
        self.assertTrue(result["provisioned"])
        self.assertTrue((expected / ".git").exists())
        # Branch created off origin/main.
        branch = subprocess.check_output(
            ["git", "-C", str(expected), "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
        self.assertEqual(branch, "wiki-999")

    def test_idempotent_on_existing_worktree(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="wiki")
        main.composer_provision_worktree(body)
        result = main.composer_provision_worktree(body)
        self.assertFalse(result["provisioned"])

    def test_rejects_unknown_orchestrator(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="tooling")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body)
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("Unknown orchestrator", str(exc.exception.detail))

    def test_rejects_worker_session_spawn(self) -> None:
        self._write_registry(self._worker_registry("WIKI-148"))
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="WIKI-148")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body)
        self.assertEqual(exc.exception.status_code, 400)
        detail = str(exc.exception.detail)
        self.assertIn("worker", detail)
        self.assertIn("WIKI-148", detail)

    def test_routes_to_non_wiki_orchestrator_repo(self) -> None:
        other = _init_repo(self.root / "tooling-tree")
        self._write_registry(self._orch_registry("tooling", repo=other))
        body = main.ComposerProvisionIn(ticket="TOOL-1", orch="tooling")
        result = main.composer_provision_worktree(body)
        expected = (other / ".claude" / "worktrees" / "tool-1").resolve()
        self.assertEqual(result["workdir"], str(expected))
        # Not created inside the wiki repo.
        self.assertFalse((self.repo / ".claude" / "worktrees" / "tool-1").exists())

    def test_rejects_when_orchestrator_root_is_not_git(self) -> None:
        plain = self.root / "not-a-repo"
        plain.mkdir()
        self._write_registry(
            {
                "misc": {
                    "current": {
                        "role": "orchestrator",
                        "cwd": str(plain),
                        "worktree": str(plain),
                        "kind": "cc",
                    },
                    "history": [],
                }
            }
        )
        body = main.ComposerProvisionIn(ticket="MISC-1", orch="misc")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body)
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("not a git repository", str(exc.exception.detail))

    def test_git_fetch_failure_is_surfaced(self) -> None:
        # Break origin URL by removing the remote path — fetch must raise.
        origin_url = subprocess.check_output(
            ["git", "-C", str(self.repo), "remote", "get-url", "origin"], text=True
        ).strip()
        broken = self.root / "gone.git"
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "set-url", "origin", str(broken)],
            check=True, capture_output=True,
        )
        self._write_registry(self._orch_registry("wiki"))
        body = main.ComposerProvisionIn(ticket="WIKI-777", orch="wiki")
        try:
            with self.assertRaises(HTTPException) as exc:
                main.composer_provision_worktree(body)
            self.assertEqual(exc.exception.status_code, 502)
        finally:
            subprocess.run(
                ["git", "-C", str(self.repo), "remote", "set-url", "origin", origin_url],
                check=True, capture_output=True,
            )

    def test_existing_dir_that_is_not_worktree_is_rejected(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        stale = (self.repo / ".claude" / "worktrees" / "wiki-stale")
        stale.mkdir(parents=True)
        (stale / "hello.txt").write_text("uncommitted", encoding="utf-8")
        body = main.ComposerProvisionIn(ticket="WIKI-STALE", orch="wiki")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body)
        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("not a git worktree", str(exc.exception.detail))

    def test_existing_branch_yields_clean_error_not_force_reset(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        # Pre-create the branch locally, then remove any worktree so we take
        # the create path.
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "wiki-collide", "origin/main"],
            check=True, capture_output=True,
        )
        target_workdir = self.repo / ".claude" / "worktrees" / "wiki-collide"
        if target_workdir.exists():
            shutil.rmtree(target_workdir)
        body = main.ComposerProvisionIn(ticket="WIKI-COLLIDE", orch="wiki")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body)
        self.assertEqual(exc.exception.status_code, 502)
        detail = str(exc.exception.detail)
        # git prints "already exists" for -b when the branch is present.
        self.assertIn("wiki-collide", detail)
        # Branch head unchanged (still at origin/main we set).
        head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "wiki-collide"], text=True
        ).strip()
        origin_head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "origin/main"], text=True
        ).strip()
        self.assertEqual(head, origin_head)


if __name__ == "__main__":
    unittest.main()
