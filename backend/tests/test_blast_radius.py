from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from backend.app import blast_radius


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class FixtureRepo:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        (self.root / "styles.css").write_text("base\n", encoding="utf-8")
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "--", "styles.css", "README.md")
        git(self.root, "commit", "-m", "base")

    def branch(self, name: str, files: dict[str, str]) -> None:
        git(self.root, "switch", "-c", name)
        for path, content in files.items():
            (self.root / path).write_text(content, encoding="utf-8")
        git(self.root, "add", "--", *files)
        git(self.root, "commit", "-m", name)
        git(self.root, "switch", "main")

    def close(self) -> None:
        self.tmp.cleanup()


class BlastRadiusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = FixtureRepo()
        self.addCleanup(self.fixture.close)
        self.fixture.branch("one", {"styles.css": "one\n", "one.md": "one\n"})
        self.fixture.branch("two", {"styles.css": "two\n", "two.md": "two\n"})
        self.fixture.branch("three", {"three.md": "three\n"})

    def refs(self) -> dict[str, blast_radius.BranchRef]:
        return blast_radius._refs(self.fixture.root, timeout=2)

    def snapshot(self, *names: str) -> blast_radius.OpenPRSnapshotState:
        refs = self.refs()
        return blast_radius.OpenPRSnapshotState(
            tuple(
                blast_radius.OpenPRBranch(name, refs[name].head_sha)
                for name in names
            ),
            True,
            time.time(),
        )

    def test_changed_files_use_main_three_dot_diff(self) -> None:
        refs = self.refs()
        self.assertEqual(
            blast_radius.changed_files(self.fixture.root, refs["one"]),
            ("one.md", "styles.css"),
        )

    def test_intersections_do_not_mutate_input_and_sort_by_overlap(self) -> None:
        first = blast_radius.BranchFiles("one", "a", ("styles.css", "one.md"))
        second = blast_radius.BranchFiles("two", "b", ("styles.css", "two.md"))
        third = blast_radius.BranchFiles("three", "c", ("styles.css", "one.md", "two.md"))
        before = (first.files, second.files, third.files)
        collisions = blast_radius.collision_pairs([third, first, second])
        self.assertEqual(before, (first.files, second.files, third.files))
        self.assertEqual(collisions[0]["overlap_count"], 2)
        self.assertEqual(collisions[0]["left"], "one")
        self.assertEqual(collisions[0]["right"], "three")
        self.assertEqual(collisions[0]["overlap"], ["one.md", "styles.css"])

    def test_cache_reuses_same_head_and_recomputes_when_head_moves(self) -> None:
        cache = blast_radius.DiffCache(max_entries=2)
        calls = 0

        def compute() -> tuple[str, ...]:
            nonlocal calls
            calls += 1
            return ("styles.css",)

        self.assertEqual(cache.get_or_compute("one", "a", compute), ("styles.css",))
        self.assertEqual(cache.get_or_compute("one", "a", compute), ("styles.css",))
        self.assertEqual(cache.get_or_compute("one", "b", compute), ("styles.css",))
        self.assertEqual(calls, 2)

    def test_cache_single_flight_runs_one_same_key_computation(self) -> None:
        cache = blast_radius.DiffCache()
        calls = 0
        calls_lock = threading.Lock()
        barrier = threading.Barrier(8)

        def compute() -> tuple[str, ...]:
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return ("styles.css",)

        def load() -> tuple[str, ...]:
            barrier.wait()
            return cache.get_or_compute("one", "same-head", compute)

        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _index: load(), range(8)))
        self.assertEqual(values, [("styles.css",)] * 8)
        self.assertEqual(calls, 1)

    def test_cache_single_flight_clears_inflight_after_error(self) -> None:
        cache = blast_radius.DiffCache()
        with self.assertRaisesRegex(RuntimeError, "failed"):
            cache.get_or_compute("one", "bad-head", lambda: (_ for _ in ()).throw(RuntimeError("failed")))
        self.assertEqual(cache.get_or_compute("one", "bad-head", lambda: ("styles.css",)), ("styles.css",))

    def test_analyze_all_reports_collision_and_hot_file(self) -> None:
        refs = self.refs()
        payload = blast_radius.analyze(
            self.fixture.root,
            {
                "ONE": {"current": {"role": "implement", "branch": "one"}},
                "TWO": {"current": {"role": "implement", "branch": "two"}},
            },
            cache=blast_radius.DiffCache(),
            pr_snapshot=self.snapshot("one", "two"),
        )
        self.assertIsNotNone(refs)
        self.assertEqual(payload["risk"]["count"], 1)
        self.assertEqual(payload["risk"]["hot_files"], ["styles.css"])
        self.assertEqual(payload["collisions"][0]["overlap"], ["styles.css"])

    def test_open_pr_snapshot_filters_closed_refs_and_deduplicates_local_remote(self) -> None:
        refs = self.refs()
        git(self.fixture.root, "update-ref", "refs/remotes/origin/one", refs["one"].head_sha)
        git(self.fixture.root, "update-ref", "refs/remotes/origin/closed", refs["two"].head_sha)
        payload = blast_radius.analyze(
            self.fixture.root,
            {},
            cache=blast_radius.DiffCache(),
            pr_snapshot=self.snapshot("one"),
        )
        self.assertTrue(payload["complete"])
        self.assertEqual([row["branch"] for row in payload["branches"]], ["one"])

    def test_stale_open_pr_head_is_an_incomplete_failure(self) -> None:
        payload = blast_radius.analyze(
            self.fixture.root,
            {},
            cache=blast_radius.DiffCache(),
            pr_snapshot=blast_radius.OpenPRSnapshotState(
                (blast_radius.OpenPRBranch("one", "0" * 40),),
                True,
            ),
        )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertTrue(payload["failed_branches"])

    def test_timeout_is_reported_and_never_becomes_no_overlap(self) -> None:
        with mock.patch.object(
            blast_radius,
            "changed_files",
            side_effect=lambda _repo, branch, **_kwargs: (
                (_ for _ in ()).throw(blast_radius.GitAnalysisError(f"timeout {branch.name}"))
                if branch.name == "two"
                else ("styles.css",),
            ),
        ):
            payload = blast_radius.analyze(
                self.fixture.root,
                {},
                cache=blast_radius.DiffCache(),
                pr_snapshot=self.snapshot("one", "two"),
            )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertIn("two", {failure["branch"] for failure in payload["failed_branches"]})

    def test_registered_worktree_branch_is_discovered_from_primary_repo(self) -> None:
        worktree = self.fixture.root.parent / "worker-one"
        git(self.fixture.root, "worktree", "add", "--", str(worktree), "one")
        self.addCleanup(lambda: git(self.fixture.root, "worktree", "remove", "--", str(worktree)))
        payload = blast_radius.analyze(
            self.fixture.root,
            {
                "ONE": {
                    "current": {
                        "role": "implement",
                        "worktree": str(worktree),
                    }
                }
            },
            cache=blast_radius.DiffCache(),
        )
        branches = {row["branch"] for row in payload["branches"]}
        self.assertIn("one", branches)

    def test_foreign_and_detached_review_worktrees_are_ignored(self) -> None:
        foreign = FixtureRepo()
        self.addCleanup(foreign.close)
        detached = self.fixture.root.parent / "detached-review"
        git(self.fixture.root, "worktree", "add", "--detach", "--", str(detached), "main")
        self.addCleanup(lambda: git(self.fixture.root, "worktree", "remove", "--force", "--", str(detached)))
        payload = blast_radius.analyze(
            self.fixture.root,
            {
                "FOREIGN": {
                    "current": {"role": "implement", "worktree": str(foreign.root), "branch": "one"}
                },
                "REVIEW": {
                    "current": {"role": "review", "worktree": str(detached), "branch": "one"}
                },
            },
            cache=blast_radius.DiffCache(),
            pr_snapshot=blast_radius.OpenPRSnapshotState((), True, time.time()),
        )
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["failed_branches"], [])
        self.assertEqual(payload["branches"], [])

    def test_stale_snapshot_is_incomplete_and_has_unknown_risk(self) -> None:
        payload = blast_radius.analyze(
            self.fixture.root,
            {},
            cache=blast_radius.DiffCache(),
            pr_snapshot=blast_radius.OpenPRSnapshotState(
                (),
                True,
                time.time() - blast_radius.OPEN_PR_MAX_AGE_SECONDS - 1,
            ),
        )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertTrue(any("stale" in failure["reason"] for failure in payload["failed_branches"]))

    def test_branch_count_truncation_is_reported_as_incomplete(self) -> None:
        with mock.patch.object(blast_radius, "MAX_ACTIVE_BRANCHES", 1):
            payload = blast_radius.analyze(
                self.fixture.root,
                {},
                cache=blast_radius.DiffCache(),
                pr_snapshot=self.snapshot("one", "two"),
            )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertTrue(any("dropped 1 branches" in failure["reason"] for failure in payload["failed_branches"]))

    def test_file_count_truncation_is_reported_as_incomplete(self) -> None:
        with mock.patch.object(blast_radius, "MAX_CHANGED_FILES", 1):
            payload = blast_radius.analyze(
                self.fixture.root,
                {},
                cache=blast_radius.DiffCache(),
                pr_snapshot=self.snapshot("one"),
            )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertTrue(any("dropped 1 files" in failure["reason"] for failure in payload["failed_branches"]))

    def test_candidate_can_be_a_ref_or_ticket_and_missing_is_clean(self) -> None:
        by_ref = blast_radius.analyze(
            self.fixture.root,
            {},
            "one",
            cache=blast_radius.DiffCache(),
            pr_snapshot=self.snapshot("one"),
        )
        self.assertTrue(by_ref["candidate_found"])
        self.assertTrue(by_ref["complete"])

        missing = blast_radius.analyze(
            self.fixture.root,
            {},
            "not-a-real-branch",
            cache=blast_radius.DiffCache(),
        )
        self.assertFalse(missing["candidate_found"])
        self.assertFalse(missing["complete"])
        self.assertEqual(missing["collisions"], [])
        self.assertIn("not-a-real-branch", {failure["branch"] for failure in missing["failed_branches"]})

    def test_deleted_open_pr_candidate_is_not_found(self) -> None:
        payload = blast_radius.analyze(
            self.fixture.root,
            {},
            "deleted",
            cache=blast_radius.DiffCache(),
            pr_snapshot=blast_radius.OpenPRSnapshotState(
                (blast_radius.OpenPRBranch("deleted", "1" * 40),),
                True,
                time.time(),
            ),
        )
        self.assertFalse(payload["candidate_found"])
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertIn("deleted", {failure["branch"] for failure in payload["failed_branches"]})

    def test_origin_main_is_a_valid_full_main_ref(self) -> None:
        base = git(self.fixture.root, "rev-parse", "main")
        git(self.fixture.root, "branch", "-m", "main", "local-main")
        git(self.fixture.root, "update-ref", "refs/remotes/origin/main", base)
        payload = blast_radius.analyze(
            self.fixture.root,
            {},
            cache=blast_radius.DiffCache(),
            pr_snapshot=blast_radius.OpenPRSnapshotState((), True, time.time()),
        )
        self.assertTrue(payload["complete"])

    def test_missing_main_ref_is_an_error_not_a_success(self) -> None:
        git(self.fixture.root, "branch", "-m", "main", "local-main")
        payload = blast_radius.analyze(
            self.fixture.root,
            {},
            cache=blast_radius.DiffCache(),
            pr_snapshot=blast_radius.OpenPRSnapshotState((), True, time.time()),
        )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertIn("main", {failure["branch"] for failure in payload["failed_branches"]})

    def test_snapshot_provider_refreshes_and_reports_provider_errors(self) -> None:
        snapshot = blast_radius.OpenPRSnapshot(lambda: [{"headRefName": "one"}])
        state = snapshot.refresh()
        self.assertTrue(state.complete)
        self.assertEqual([branch.name for branch in state.branches], ["one"])
        broken = blast_radius.OpenPRSnapshot(lambda: (_ for _ in ()).throw(RuntimeError("offline")))
        self.assertFalse(broken.refresh().complete)

    def test_malformed_registry_and_branch_input_do_not_escape_git(self) -> None:
        payload = blast_radius.analyze(
            self.fixture.root,
            {"BAD": {"current": {"worktree": "--upload-pack=evil"}}},
            "--upload-pack=evil",
            cache=blast_radius.DiffCache(),
        )
        self.assertFalse(payload["candidate_found"])
        self.assertEqual(payload["branches"], [])


class RouteTests(unittest.TestCase):
    def test_route_returns_json_shape_with_fixture_repo(self) -> None:
        from backend.app import main

        fixture = FixtureRepo()
        self.addCleanup(fixture.close)
        with mock.patch.object(main, "ROOT_DIR", fixture.root), mock.patch.object(
            main, "_read_agent_registry", return_value={}
        ):
            payload = main.blast_radius_view(candidate="all", branch=None, ticket=None)
        self.assertIn("risk", payload)


if __name__ == "__main__":
    unittest.main()
