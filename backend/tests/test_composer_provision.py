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

    def test_pinned_worktree_accepts_short_sha_for_existing_worktree(self) -> None:
        full_sha = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "origin/main"], text=True
        ).strip()
        short_sha = full_sha[:7]
        workdir = self.repo / ".codex" / "worktrees" / "short-sha"

        main.provision_pinned_worktree(self.repo, workdir, full_sha)
        main.provision_pinned_worktree(self.repo, workdir, short_sha)

        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(workdir), "rev-parse", "HEAD"], text=True
            ).strip(),
            full_sha,
        )

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


WIKI_APP_SECRET_FIXTURE = "wiki-app-origin-secret-fixture-abcdefghij"


class WikiAppOriginAuthTests(unittest.TestCase):
    """H1 (round 6, Path B): composer endpoints require the in-memory
    Wiki.app origin secret. There is NO client-supplied credential path
    left — the earlier per-orch token endpoint was itself spoofable (any
    caller who knew the orch id could fetch the token) and is removed
    entirely. Worker CLI sessions run outside Tauri's IPC bridge and
    cannot obtain the secret; they get 403 regardless of the body they
    craft.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _init_repo(self.root)
        self._registry_path = self.root / "agent-registry.json"
        self._registry_patch = mock.patch.object(main, "AGENT_REGISTRY_PATH", self._registry_path)
        self._registry_patch.start()
        self._prior_secret = main.wiki_app_secret()
        main.set_wiki_app_secret(WIKI_APP_SECRET_FIXTURE)

    def tearDown(self) -> None:
        main.set_wiki_app_secret(self._prior_secret)
        self._registry_patch.stop()
        self.tmp.cleanup()

    def _write_registry(self, payload: dict) -> None:
        self._registry_path.write_text(json.dumps(payload), encoding="utf-8")

    def _seed_orch(self, orch_id: str, repo: Path | None = None) -> None:
        self._write_registry(
            {
                orch_id: {
                    "current": {
                        "role": "orchestrator",
                        "cwd": str(repo or self.repo),
                        "worktree": str(repo or self.repo),
                        "kind": "cc",
                        "session_id": ORCH_SID,
                    },
                    "history": [],
                }
            }
        )

    def _seed_orch_and_worker(self, orch_id: str, worker_ticket: str) -> None:
        self._write_registry(
            {
                orch_id: {
                    "current": {
                        "role": "orchestrator",
                        "cwd": str(self.repo),
                        "worktree": str(self.repo),
                        "kind": "cc",
                        "session_id": ORCH_SID,
                    },
                    "history": [],
                },
                worker_ticket: {
                    "current": {
                        "role": "implement",
                        "cwd": str(self.repo),
                        "worktree": str(self.repo),
                        "kind": "cc",
                        "orch": orch_id,
                        "session_id": WORKER_SID,
                    },
                    "history": [],
                },
            }
        )

    # --- Transport gate -----------------------------------------------------

    def test_rejects_missing_origin_header(self) -> None:
        """No `X-Wiki-App-Secret` header → 403 with a specific message.

        Reproduces the worker-curl scenario: worker forges the JSON body but
        cannot mint the Wiki.app-only secret.
        """

        self._seed_orch("wiki")
        with self.assertRaises(HTTPException) as exc:
            main.require_wiki_app_origin(x_wiki_app_secret=None)
        self.assertEqual(exc.exception.status_code, 403)
        self.assertIn("Wiki.app origin secret", str(exc.exception.detail))

    def test_rejects_empty_origin_header(self) -> None:
        with self.assertRaises(HTTPException) as exc:
            main.require_wiki_app_origin(x_wiki_app_secret="   ")
        self.assertEqual(exc.exception.status_code, 403)

    def test_rejects_wrong_origin_secret(self) -> None:
        """Attacker guesses a random string — constant-time compare rejects."""

        with self.assertRaises(HTTPException) as exc:
            main.require_wiki_app_origin(
                x_wiki_app_secret="not-the-actual-wiki-app-secret-zzz"
            )
        self.assertEqual(exc.exception.status_code, 403)

    def test_accepts_correct_origin_secret(self) -> None:
        """Passing the exact minted secret is fine — no exception raised."""

        self.assertIsNone(
            main.require_wiki_app_origin(
                x_wiki_app_secret=WIKI_APP_SECRET_FIXTURE
            )
        )

    # --- End-to-end via the FastAPI route wrapper ---------------------------

    def test_route_rejects_worker_ticket_body_even_with_valid_secret(self) -> None:
        """Even with the origin secret, a worker orch id in the body is
        rejected by the registry role check — Path B keeps the transport gate
        AND the orch-role gate (defense in depth)."""

        self._seed_orch_and_worker("wiki", "WIKI-148")
        # Route wrapper needs the secret via `Depends` — simulate by calling
        # the inner function directly after the transport gate has been
        # exercised in tests above.
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="WIKI-148")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree_route(body)
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("worker", str(exc.exception.detail))

    def test_route_provisions_when_orch_body_and_secret_are_valid(self) -> None:
        """Happy path: Wiki.app-origin secret satisfied by the dependency
        (verified above), body orch is a registered orchestrator, route
        returns a provisioned worktree."""

        self._seed_orch("wiki")
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="wiki")
        result = main.composer_provision_worktree_route(body)
        self.assertTrue(result["provisioned"])
        expected = (self.repo / ".claude" / "worktrees" / "wiki-999").resolve()
        self.assertEqual(result["workdir"], str(expected))

    def test_route_routes_body_orch_to_the_right_repo(self) -> None:
        """Body `orch` is trusted (transport gate has already run) so a
        request naming `tooling` provisions in tooling's repo, not wiki's."""

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
                    },
                    "history": [],
                },
            }
        )
        body = main.ComposerProvisionIn(ticket="TOOL-1", orch="tooling")
        result = main.composer_provision_worktree_route(body)
        expected = (other / ".claude" / "worktrees" / "tool-1").resolve()
        self.assertEqual(result["workdir"], str(expected))
        self.assertFalse((self.repo / ".claude" / "worktrees" / "tool-1").exists())

    def test_orch_token_endpoint_is_gone(self) -> None:
        """Round 6: the previously spoofable token endpoint no longer exists.

        Locking in the removal — any regression that re-adds the attribute
        would let workers harvest secrets again.
        """

        self.assertFalse(hasattr(main, "composer_orch_token"))
        self.assertFalse(hasattr(main, "_ensure_composer_token"))
        self.assertFalse(hasattr(main, "_orch_from_composer_token"))
        self.assertFalse(hasattr(main, "COMPOSER_TOKEN_DIR"))

    def test_route_rejects_missing_orch_in_body(self) -> None:
        """Round 6: the route requires `orch` in the body even after the
        transport gate. Absent it, the request is a 400 — there's no
        implicit caller identity to fall back on.
        """

        self._seed_orch("wiki")
        body = main.ComposerProvisionIn(ticket="WIKI-999")
        with self.assertRaises(HTTPException) as exc:
            main.composer_provision_worktree_route(body)
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("orch", str(exc.exception.detail))

    def test_boot_line_carries_current_secret(self) -> None:
        """`wiki_app_secret_boot_line()` returns the marker line the backend
        emits on stdout for Tauri to capture. Locks the wire format so a
        careless rename doesn't silently break secret handoff.
        """

        line = main.wiki_app_secret_boot_line()
        self.assertTrue(line.startswith("[[WIKI_APP_SECRET_BOOT]]="))
        self.assertEqual(
            line[len("[[WIKI_APP_SECRET_BOOT]]="):], WIKI_APP_SECRET_FIXTURE
        )


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
                        },
                        "history": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        body = main.ComposerProvisionIn(ticket="WIKI-999", orch="wiki")
        result = main.composer_provision_worktree(body, "wiki")
        expected = (linked_root / ".claude" / "worktrees" / "wiki-999").resolve()
        self.assertEqual(result["workdir"], str(expected))
        # Second call should re-validate the linked worktree successfully.
        again = main.composer_provision_worktree(body, "wiki")
        self.assertFalse(again["provisioned"])
        self.assertEqual(again["workdir"], str(expected))


if __name__ == "__main__":
    unittest.main()
