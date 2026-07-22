"""Tests for /api/composer/provision-worktree — WIKI-148.

Cover the multi-orchestrator registry lookup, session-identity binding
(H1: reject spoofed `orch`), existing-worktree validation
(M1: reject wrong-repo / wrong-branch), missing-upstream-branch
(M2: origin exists but lacks main), git-fetch failure surfacing, and
destructive branch-reset guardrails.
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


ORCH_SID = "orch-session-1"
WORKER_SID = "worker-session-1"


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


def _init_repo_no_main(path: Path) -> Path:
    """Create origin that exists and is reachable, but has only a `feature` branch (no main).

    Simulates `git fetch origin main` failing because the ref doesn't exist upstream.
    """

    origin = path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)

    seed = path / "seed-no-main"
    subprocess.run(["git", "init", "--initial-branch=feature", str(seed)], check=True, capture_output=True)
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
        ["git", "-C", str(seed), "push", "origin", "feature"],
        check=True, capture_output=True,
    )

    repo = path / "repo-no-main"
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

    def _orch_registry(
        self, orch_id: str, repo: Path | None = None, session_id: str = ORCH_SID
    ) -> dict:
        return {
            orch_id: {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(repo or self.repo),
                    "worktree": str(repo or self.repo),
                    "kind": "cc",
                    "session_id": session_id,
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
                    "session_id": WORKER_SID,
                },
                "history": [],
            }
        }

    def test_provisions_new_worktree_off_fresh_origin_main(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        body = main.ComposerProvisionIn(ticket="WIKI-999")
        result = main.composer_provision_worktree(body, "wiki")
        expected = (self.repo / ".claude" / "worktrees" / "wiki-999").resolve()
        self.assertEqual(result["workdir"], str(expected))
        self.assertTrue(result["provisioned"])
        self.assertTrue((expected / ".git").exists())
        branch = subprocess.check_output(
            ["git", "-C", str(expected), "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
        self.assertEqual(branch, "wiki-999")

    def test_idempotent_on_existing_worktree(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        body = main.ComposerProvisionIn(ticket="WIKI-999")
        main.composer_provision_worktree(body, "wiki")
        result = main.composer_provision_worktree(body, "wiki")
        self.assertFalse(result["provisioned"])

    def test_rejects_unknown_orchestrator(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        body = main.ComposerProvisionIn(ticket="WIKI-999")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body, "tooling")
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("Unknown orchestrator", str(exc.exception.detail))

    def test_rejects_worker_session_spawn(self) -> None:
        self._write_registry(self._worker_registry("WIKI-148"))
        body = main.ComposerProvisionIn(ticket="WIKI-999")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body, "WIKI-148")
        self.assertEqual(exc.exception.status_code, 400)
        detail = str(exc.exception.detail)
        self.assertIn("worker", detail)
        self.assertIn("WIKI-148", detail)

    def test_routes_to_non_wiki_orchestrator_repo(self) -> None:
        other = _init_repo(self.root / "tooling-tree")
        self._write_registry(self._orch_registry("tooling", repo=other))
        body = main.ComposerProvisionIn(ticket="TOOL-1")
        result = main.composer_provision_worktree(body, "tooling")
        expected = (other / ".claude" / "worktrees" / "tool-1").resolve()
        self.assertEqual(result["workdir"], str(expected))
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
                        "session_id": ORCH_SID,
                    },
                    "history": [],
                }
            }
        )
        body = main.ComposerProvisionIn(ticket="MISC-1")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body, "misc")
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("not a git repository", str(exc.exception.detail))

    def test_missing_upstream_main_is_surfaced(self) -> None:
        """M2: origin exists but lacks `main` → clean 502, not 'no remote'."""

        repo_no_main = _init_repo_no_main(self.root / "no-main")
        self._write_registry(self._orch_registry("wiki", repo=repo_no_main))
        body = main.ComposerProvisionIn(ticket="WIKI-777")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body, "wiki")
        self.assertEqual(exc.exception.status_code, 502)
        detail = str(exc.exception.detail).lower()
        # Git reports "couldn't find remote ref main" (or similar) — not a
        # transport failure.
        self.assertTrue(
            "main" in detail or "ref" in detail,
            f"expected upstream-branch failure detail, got: {exc.exception.detail!r}",
        )

    def test_existing_dir_that_is_not_worktree_is_rejected(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        stale = (self.repo / ".claude" / "worktrees" / "wiki-stale")
        stale.mkdir(parents=True)
        (stale / "hello.txt").write_text("uncommitted", encoding="utf-8")
        body = main.ComposerProvisionIn(ticket="WIKI-STALE")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body, "wiki")
        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("not a git worktree", str(exc.exception.detail))

    def test_existing_worktree_from_unrelated_repo_is_rejected(self) -> None:
        """M1: existing `.git` points at a different upstream — reject."""

        self._write_registry(self._orch_registry("wiki"))
        other = _init_repo(self.root / "other-tree")
        # Register a worktree from the OTHER repo at a scratch path, then move
        # its checkout under wiki's namespace so `.git` points at other's
        # common-dir. This mimics a stale/misplaced worktree pointing at an
        # unrelated repository.
        scratch = self.root / "alien-scratch"
        subprocess.run(
            ["git", "-C", str(other), "worktree", "add", "-b", "wiki-alien", str(scratch)],
            check=True, capture_output=True,
        )
        target_workdir = self.repo / ".claude" / "worktrees" / "wiki-alien"
        target_workdir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(scratch), str(target_workdir))
        body = main.ComposerProvisionIn(ticket="WIKI-ALIEN")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body, "wiki")
        self.assertEqual(exc.exception.status_code, 409)
        detail = str(exc.exception.detail)
        self.assertIn("different repository", detail)

    def test_existing_worktree_on_wrong_branch_is_rejected(self) -> None:
        """M1: existing worktree HEAD isn't the expected branch — reject."""

        self._write_registry(self._orch_registry("wiki"))
        target_workdir = self.repo / ".claude" / "worktrees" / "wiki-wrongbranch"
        # Create the worktree on a differently-named branch.
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "add",
                "-b",
                "actually-different",
                str(target_workdir),
                "origin/main",
            ],
            check=True, capture_output=True,
        )
        body = main.ComposerProvisionIn(ticket="WIKI-WRONGBRANCH")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body, "wiki")
        self.assertEqual(exc.exception.status_code, 409)
        detail = str(exc.exception.detail)
        self.assertIn("wiki-wrongbranch", detail)
        self.assertIn("actually-different", detail)

    def test_git_fetch_failure_is_surfaced(self) -> None:
        origin_url = subprocess.check_output(
            ["git", "-C", str(self.repo), "remote", "get-url", "origin"], text=True
        ).strip()
        broken = self.root / "gone.git"
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "set-url", "origin", str(broken)],
            check=True, capture_output=True,
        )
        self._write_registry(self._orch_registry("wiki"))
        body = main.ComposerProvisionIn(ticket="WIKI-777")
        try:
            with self.assertRaises(HTTPException) as exc:
                main.composer_provision_worktree(body, "wiki")
            self.assertEqual(exc.exception.status_code, 502)
        finally:
            subprocess.run(
                ["git", "-C", str(self.repo), "remote", "set-url", "origin", origin_url],
                check=True, capture_output=True,
            )

    def test_existing_branch_yields_clean_error_not_force_reset(self) -> None:
        self._write_registry(self._orch_registry("wiki"))
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "wiki-collide", "origin/main"],
            check=True, capture_output=True,
        )
        target_workdir = self.repo / ".claude" / "worktrees" / "wiki-collide"
        if target_workdir.exists():
            shutil.rmtree(target_workdir)
        body = main.ComposerProvisionIn(ticket="WIKI-COLLIDE")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree(body, "wiki")
        self.assertEqual(exc.exception.status_code, 502)
        detail = str(exc.exception.detail)
        self.assertIn("wiki-collide", detail)
        head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "wiki-collide"], text=True
        ).strip()
        origin_head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "origin/main"], text=True
        ).strip()
        self.assertEqual(head, origin_head)


ORCH_TOKEN = "orch-composer-token-1234567890abcdef"
WORKER_TOKEN_ATTEMPT = "not-a-registered-token-zzzz"


class ComposerTokenAuthTests(unittest.TestCase):
    """H1 (round 5): auth binds to per-orch composer tokens, NOT to any
    identifier exposed via /api/agents. Worker sessions never hold a token
    and cannot forge the header by scraping the public agent list.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _init_repo(self.root)
        self._registry_path = self.root / "agent-registry.json"
        self._registry_patch = mock.patch.object(main, "AGENT_REGISTRY_PATH", self._registry_path)
        self._registry_patch.start()
        self._token_dir = self.root / "session-tokens"
        self._token_patch = mock.patch.object(main, "COMPOSER_TOKEN_DIR", self._token_dir)
        self._token_patch.start()

    def tearDown(self) -> None:
        self._token_patch.stop()
        self._registry_patch.stop()
        self.tmp.cleanup()

    def _write_registry(self, payload: dict) -> None:
        self._registry_path.write_text(json.dumps(payload), encoding="utf-8")

    def _seed_orch_with_token(
        self, orch_id: str, repo: Path | None = None, token: str = ORCH_TOKEN
    ) -> None:
        payload = {
            orch_id: {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(repo or self.repo),
                    "worktree": str(repo or self.repo),
                    "kind": "cc",
                    "session_id": ORCH_SID,
                    "composer_token": token,
                },
                "history": [],
            }
        }
        self._write_registry(payload)

    def test_rejects_missing_token_header(self) -> None:
        self._seed_orch_with_token("wiki")
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="wiki")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree_route(body, x_wiki_composer_token=None)
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("X-Wiki-Composer-Token", str(exc.exception.detail))

    def test_rejects_forged_token_from_public_agent_list(self) -> None:
        """Worker session forges a value the /api/agents list DOES expose (session id) as the
        composer token — must be rejected, because the token is a distinct per-orch secret
        that never appears in that list.
        """

        self._write_registry(
            {
                "wiki": {
                    "current": {
                        "role": "orchestrator",
                        "cwd": str(self.repo),
                        "worktree": str(self.repo),
                        "kind": "cc",
                        "session_id": ORCH_SID,
                        "composer_token": ORCH_TOKEN,
                    },
                    "history": [],
                },
                "WIKI-148": {
                    "current": {
                        "role": "implement",
                        "cwd": str(self.repo),
                        "worktree": str(self.repo),
                        "kind": "cc",
                        "orch": "wiki",
                        "session_id": WORKER_SID,
                    },
                    "history": [],
                },
            }
        )
        # Attacker replays the wiki orch's session id (visible in /api/agents)
        # as the composer token.
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="wiki")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree_route(body, x_wiki_composer_token=ORCH_SID)
        self.assertEqual(exc.exception.status_code, 403)

    def test_rejects_worker_session_forging_arbitrary_token(self) -> None:
        self._write_registry(
            {
                "wiki": {
                    "current": {
                        "role": "orchestrator",
                        "cwd": str(self.repo),
                        "worktree": str(self.repo),
                        "kind": "cc",
                        "session_id": ORCH_SID,
                        "composer_token": ORCH_TOKEN,
                    },
                    "history": [],
                },
                "WIKI-148": {
                    "current": {
                        "role": "implement",
                        "cwd": str(self.repo),
                        "worktree": str(self.repo),
                        "kind": "cc",
                        "orch": "wiki",
                        "session_id": WORKER_SID,
                    },
                    "history": [],
                },
            }
        )
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="wiki")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree_route(
                body, x_wiki_composer_token=WORKER_TOKEN_ATTEMPT
            )
        self.assertEqual(exc.exception.status_code, 403)

    def test_rejects_unknown_token(self) -> None:
        self._seed_orch_with_token("wiki")
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="wiki")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree_route(
                body, x_wiki_composer_token="stranger-token-not-in-registry"
            )
        self.assertEqual(exc.exception.status_code, 403)

    def test_valid_orch_token_provisions(self) -> None:
        self._seed_orch_with_token("wiki")
        body = main.ComposerProvisionIn(ticket="WIKI-999")
        result = main.composer_provision_worktree_route(
            body, x_wiki_composer_token=ORCH_TOKEN
        )
        self.assertTrue(result["provisioned"])

    def test_body_orch_ignored_when_token_maps_elsewhere(self) -> None:
        """Body `orch` is a hint, not authorization. The token binds the request
        to whichever orch holds it — even if the body claims a different orch.
        """

        other = _init_repo(self.root / "tooling-tree")
        self._write_registry(
            {
                "wiki": {
                    "current": {
                        "role": "orchestrator",
                        "cwd": str(self.repo),
                        "worktree": str(self.repo),
                        "kind": "cc",
                        "session_id": ORCH_SID,
                        "composer_token": ORCH_TOKEN,
                    },
                    "history": [],
                },
                "tooling": {
                    "current": {
                        "role": "orchestrator",
                        "cwd": str(other),
                        "worktree": str(other),
                        "kind": "cc",
                        "session_id": "tooling-sid",
                        "composer_token": "tooling-composer-token-abcdef123456",
                    },
                    "history": [],
                },
            }
        )
        body = main.ComposerProvisionIn(ticket="TOOL-1", orch="wiki")
        result = main.composer_provision_worktree_route(
            body, x_wiki_composer_token="tooling-composer-token-abcdef123456"
        )
        expected = (other / ".claude" / "worktrees" / "tool-1").resolve()
        self.assertEqual(result["workdir"], str(expected))
        self.assertFalse((self.repo / ".claude" / "worktrees" / "tool-1").exists())

    def test_orch_token_endpoint_mints_and_persists_for_registered_orch(self) -> None:
        # Registered but no token yet — endpoint mints one, persists to
        # registry and mode-0600 token file.
        self._write_registry(
            {
                "wiki": {
                    "current": {
                        "role": "orchestrator",
                        "cwd": str(self.repo),
                        "worktree": str(self.repo),
                        "kind": "cc",
                        "session_id": ORCH_SID,
                    },
                    "history": [],
                }
            }
        )
        first = main.composer_orch_token("wiki")
        self.assertEqual(first["orch"], "wiki")
        self.assertGreater(len(first["token"]), 30)
        stored = json.loads(self._registry_path.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["wiki"]["current"]["composer_token"], first["token"]
        )
        # File exists at 0600 and matches.
        token_file = self._token_dir / "wiki.token"
        self.assertTrue(token_file.is_file())
        mode = token_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(token_file.read_text(encoding="utf-8"), first["token"])
        # Idempotent — second call returns same token.
        second = main.composer_orch_token("wiki")
        self.assertEqual(second["token"], first["token"])

    def test_orch_token_endpoint_rejects_unknown_orch(self) -> None:
        self._write_registry({})
        with self.assertRaises(HTTPException) as exc:
            main.composer_orch_token("nope")
        self.assertEqual(exc.exception.status_code, 404)


class LinkedWorktreeCommonDirTests(unittest.TestCase):
    """M1 (round 5): _validate_existing_worktree normalizes common-dir on
    BOTH sides — a linked worktree hosting the orchestrator must still be
    accepted, because its `--git-common-dir` matches the primary repo's.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _init_repo(self.root)
        self._registry_path = self.root / "agent-registry.json"
        self._registry_patch = mock.patch.object(main, "AGENT_REGISTRY_PATH", self._registry_path)
        self._registry_patch.start()

    def tearDown(self) -> None:
        self._registry_patch.stop()
        self.tmp.cleanup()

    def test_accepts_existing_worktree_when_orch_is_rooted_in_linked_worktree(
        self,
    ) -> None:
        # Root the orch in a LINKED worktree of the primary repo (its
        # `.git` is a file, not a directory).
        linked_root = self.root / "linked-orch"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-b", "orch-linked", str(linked_root)],
            check=True, capture_output=True,
        )
        self.assertTrue((linked_root / ".git").is_file())  # confirm it's linked
        self._registry_path.write_text(
            json.dumps(
                {
                    "wiki": {
                        "current": {
                            "role": "orchestrator",
                            "cwd": str(linked_root),
                            "worktree": str(linked_root),
                            "kind": "cc",
                            "session_id": ORCH_SID,
                            "composer_token": ORCH_TOKEN,
                        },
                        "history": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        body = main.ComposerProvisionIn(ticket="WIKI-999")
        result = main.composer_provision_worktree(body, "wiki")
        expected = (linked_root / ".claude" / "worktrees" / "wiki-999").resolve()
        self.assertEqual(result["workdir"], str(expected))
        # Second call should re-validate the linked worktree successfully.
        again = main.composer_provision_worktree(body, "wiki")
        self.assertFalse(again["provisioned"])
        self.assertEqual(again["workdir"], str(expected))


if __name__ == "__main__":
    unittest.main()
