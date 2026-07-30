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
from backend.app import blast_radius_discovery


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
                blast_radius.OpenPRBranch(
                    name,
                    refs[name].head_sha,
                    attestation=blast_radius.SourceAttestation(f"open-pr-row:{name}", True, True, True),
                )
                for name in names
            ),
            True,
            time.time(),
            attestation=blast_radius.SourceAttestation("open-pr-snapshot", True, True, True),
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

    def test_cache_recomputes_when_main_head_moves(self) -> None:
        cache = blast_radius.DiffCache()
        calls = 0

        def compute() -> tuple[str, ...]:
            nonlocal calls
            calls += 1
            return ("styles.css",)

        cache.get_or_compute("one", "branch-head", compute, candidate_head_sha="candidate", main_head_sha="main-a")
        cache.get_or_compute("one", "branch-head", compute, candidate_head_sha="candidate", main_head_sha="main-a")
        cache.get_or_compute("one", "branch-head", compute, candidate_head_sha="candidate", main_head_sha="main-b")
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

    def test_merge_base_failure_is_incomplete(self) -> None:
        original_run_git = blast_radius._run_git

        def fail_diff(repo: Path, args: list[str], *, timeout: float) -> str:
            if args and args[0] == "diff":
                raise blast_radius.GitAnalysisError("no merge base")
            return original_run_git(repo, args, timeout=timeout)

        with mock.patch.object(blast_radius, "_run_git", side_effect=fail_diff):
            payload = blast_radius.analyze(
                self.fixture.root,
                {},
                cache=blast_radius.DiffCache(),
                pr_snapshot=self.snapshot("one"),
            )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertTrue(any("no merge base" in failure["reason"] for failure in payload["failed_branches"]))

    def test_worker_and_pr_heads_use_the_newer_descendant(self) -> None:
        old_one = self.refs()["one"].head_sha
        git(self.fixture.root, "switch", "two")
        (self.fixture.root / "collision.md").write_text("two\n", encoding="utf-8")
        git(self.fixture.root, "add", "--", "collision.md")
        git(self.fixture.root, "commit", "-m", "two collision")
        git(self.fixture.root, "switch", "one")
        (self.fixture.root / "collision.md").write_text("one\n", encoding="utf-8")
        git(self.fixture.root, "add", "--", "collision.md")
        git(self.fixture.root, "commit", "-m", "one collision")
        new_one = git(self.fixture.root, "rev-parse", "HEAD")
        git(self.fixture.root, "switch", "main")
        git(self.fixture.root, "update-ref", "refs/heads/one", old_one)
        git(self.fixture.root, "update-ref", "refs/remotes/origin/one", new_one)
        refs = self.refs()
        snapshot = blast_radius.OpenPRSnapshotState(
            (
                blast_radius.OpenPRBranch(
                    "one",
                    new_one,
                    attestation=blast_radius.SourceAttestation("open-pr-row:one", True, True, True),
                ),
                blast_radius.OpenPRBranch(
                    "two",
                    refs["two"].head_sha,
                    attestation=blast_radius.SourceAttestation("open-pr-row:two", True, True, True),
                ),
            ),
            True,
            time.time(),
            attestation=blast_radius.SourceAttestation("open-pr-snapshot", True, True, True),
        )
        payload = blast_radius.analyze(
            self.fixture.root,
            {"ONE": {"current": {"role": "implement", "branch": "one"}}},
            cache=blast_radius.DiffCache(),
            pr_snapshot=snapshot,
        )
        self.assertTrue(payload["complete"])
        self.assertTrue(
            any(
                "collision.md" in collision["overlap"]
                for collision in payload["collisions"]
            )
        )

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

    def test_unreadable_local_implement_worktree_is_incomplete(self) -> None:
        missing = self.fixture.root / ".codex" / "worktrees" / "missing-worker"
        payload = blast_radius.analyze(
            self.fixture.root,
            {
                "MISSING": {
                    "current": {"role": "implement", "worktree": str(missing)}
                }
            },
            cache=blast_radius.DiffCache(),
            pr_snapshot=blast_radius.OpenPRSnapshotState(
                (),
                True,
                time.time(),
                attestation=blast_radius.SourceAttestation("open-pr-snapshot", True, True, True),
            ),
        )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertTrue(any(str(missing) in failure["reason"] for failure in payload["failed_branches"]))

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
            pr_snapshot=blast_radius.OpenPRSnapshotState(
                (),
                True,
                time.time(),
                attestation=blast_radius.SourceAttestation("open-pr-snapshot", True, True, True),
            ),
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
            pr_snapshot=blast_radius.OpenPRSnapshotState(
                (),
                True,
                time.time(),
                attestation=blast_radius.SourceAttestation("open-pr-snapshot", True, True, True),
            ),
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
        snapshot = blast_radius.OpenPRSnapshot(lambda: [{"headRefName": "one", "headRefOid": "0" * 40}])
        state = snapshot.refresh()
        self.assertTrue(state.complete)
        self.assertEqual([branch.name for branch in state.branches], ["one"])
        broken = blast_radius.OpenPRSnapshot(lambda: (_ for _ in ()).throw(RuntimeError("offline")))
        self.assertFalse(broken.refresh().complete)

        analyzed = blast_radius.analyze(
            self.fixture.root,
            {},
            cache=blast_radius.DiffCache(),
            pr_snapshot=broken,
        )
        self.assertFalse(analyzed["complete"])
        self.assertIsNone(analyzed["risk"])
        self.assertTrue(any(failure["branch"] == "open PR snapshot" for failure in analyzed["failed_branches"]))

    def test_malformed_provider_row_is_incomplete(self) -> None:
        snapshot = blast_radius.OpenPRSnapshot(
            lambda: [
                {"headRefName": "one", "headRefOid": "0" * 40},
                {"headRefName": "bad branch", "headRefOid": "1" * 40},
            ]
        )
        state = snapshot.refresh()
        self.assertFalse(state.complete)
        self.assertIn("invalid branch row", state.error or "")

    def test_provider_truncation_is_incomplete(self) -> None:
        snapshot = blast_radius.OpenPRSnapshot(
            lambda: [
                {"headRefName": f"branch-{index}", "headRefOid": f"{index + 1:040x}"}
                for index in range(blast_radius.MAX_ACTIVE_BRANCHES + 1)
            ]
        )
        state = snapshot.refresh()
        self.assertFalse(state.complete)
        self.assertIn("truncated", state.error or "")

    def test_default_provider_binds_gh_to_the_repository(self) -> None:
        completed = subprocess.CompletedProcess(
            ["gh"],
            0,
            stdout="[]",
            stderr="",
        )
        with mock.patch.object(blast_radius.subprocess, "run", return_value=completed) as run:
            self.assertEqual(blast_radius._default_open_pr_provider(self.fixture.root), [])
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "pr", "list"])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.fixture.root.resolve()))

    def test_malformed_registry_and_branch_input_do_not_escape_git(self) -> None:
        payload = blast_radius.analyze(
            self.fixture.root,
            {"BAD": {"current": {"worktree": "--upload-pack=evil"}}},
            "--upload-pack=evil",
            cache=blast_radius.DiffCache(),
        )
        self.assertFalse(payload["candidate_found"])
        self.assertEqual(payload["branches"], [])

    def test_malformed_registry_entry_is_incomplete(self) -> None:
        payload = blast_radius.analyze(
            self.fixture.root,
            {"BAD": "not an entry"},
            cache=blast_radius.DiffCache(),
            pr_snapshot=blast_radius.OpenPRSnapshotState((), True, time.time()),
        )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertTrue(any(failure["branch"] == "BAD" for failure in payload["failed_branches"]))

    def test_malformed_git_refs_output_is_incomplete(self) -> None:
        with mock.patch.object(blast_radius, "_run_git", return_value="malformed refs output"):
            payload = blast_radius.analyze(
                self.fixture.root,
                {},
                cache=blast_radius.DiffCache(),
                pr_snapshot=blast_radius.OpenPRSnapshotState((), True, time.time()),
            )
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertIn("git refs", {failure["branch"] for failure in payload["failed_branches"]})

    def test_every_unattested_source_produces_unknown_risk(self) -> None:
        valid_snapshot = self.snapshot("one")

        def assert_unknown(payload: dict[str, object]) -> None:
            self.assertFalse(payload["complete"])
            self.assertIsNone(payload["risk"])
            self.assertFalse(payload["attestation"]["complete"])

        with self.subTest("provider failure"):
            broken = blast_radius.OpenPRSnapshot(lambda: (_ for _ in ()).throw(RuntimeError("provider offline")))
            assert_unknown(blast_radius.analyze(self.fixture.root, {}, cache=blast_radius.DiffCache(), pr_snapshot=broken))

        with self.subTest("provider row missing head"):
            snapshot = blast_radius.OpenPRSnapshotState(
                (blast_radius.OpenPRBranch("one", None),),
                True,
                time.time(),
            )
            assert_unknown(blast_radius.analyze(self.fixture.root, {}, cache=blast_radius.DiffCache(), pr_snapshot=snapshot))

        with self.subTest("stale provider snapshot"):
            snapshot = blast_radius.OpenPRSnapshotState(
                (),
                True,
                time.time() - blast_radius.OPEN_PR_MAX_AGE_SECONDS - 1,
            )
            assert_unknown(blast_radius.analyze(self.fixture.root, {}, cache=blast_radius.DiffCache(), pr_snapshot=snapshot))

        with self.subTest("snapshot attestation flags are authoritative"):
            snapshot = blast_radius.OpenPRSnapshotState(
                (),
                True,
                time.time(),
                attestation=blast_radius.SourceAttestation("open-pr-snapshot", False, True, False, "provider is stale"),
            )
            assert_unknown(blast_radius.analyze(self.fixture.root, {}, cache=blast_radius.DiffCache(), pr_snapshot=snapshot))

        with self.subTest("corrupt registry stays shape-invalid"):
            assert_unknown(
                blast_radius.analyze(
                    self.fixture.root,
                    [],
                    cache=blast_radius.DiffCache(),
                    pr_snapshot=valid_snapshot,
                )
            )

        with self.subTest("registry row without locator"):
            assert_unknown(
                blast_radius.analyze(
                    self.fixture.root,
                    {"BAD": {"current": {"role": "implement"}}},
                    cache=blast_radius.DiffCache(),
                    pr_snapshot=self.snapshot("one"),
                )
            )

        with self.subTest("registry read failure"):
            assert_unknown(
                blast_radius.analyze(
                    self.fixture.root,
                    {},
                    cache=blast_radius.DiffCache(),
                    pr_snapshot=self.snapshot("one"),
                    registry_error="registry unavailable",
                )
            )

        with self.subTest("git refs corruption"):
            with mock.patch.object(blast_radius, "_run_git", return_value="malformed refs output"):
                assert_unknown(blast_radius.analyze(self.fixture.root, {}, cache=blast_radius.DiffCache(), pr_snapshot=valid_snapshot))

        with self.subTest("worktree discovery failure"):
            with mock.patch.object(blast_radius_discovery, "worktree_branches", side_effect=blast_radius.GitAnalysisError("worktree list failed")):
                assert_unknown(blast_radius.analyze(self.fixture.root, {}, cache=blast_radius.DiffCache(), pr_snapshot=self.snapshot("one")))

        with self.subTest("git diff failure"):
            with mock.patch.object(blast_radius, "changed_files", side_effect=blast_radius.GitAnalysisError("diff failed")):
                assert_unknown(blast_radius.analyze(self.fixture.root, {}, cache=blast_radius.DiffCache(), pr_snapshot=self.snapshot("one")))

        with self.subTest("cache entry stale"):
            class UnattestedCache:
                def get_or_compute(self, *args: object, **kwargs: object) -> blast_radius.ChangedFiles:
                    del args, kwargs
                    return blast_radius.ChangedFiles(
                        ("styles.css",),
                        cache_attestation=blast_radius.SourceAttestation("cache-entry", False, True, False, "stale"),
                    )

            assert_unknown(blast_radius.analyze(self.fixture.root, {}, cache=UnattestedCache(), pr_snapshot=self.snapshot("one")))

        with self.subTest("cache attestation dropped"):
            class DroppedCacheAttestation:
                def get_or_compute(self, *args: object, **kwargs: object) -> blast_radius.ChangedFiles:
                    del args, kwargs
                    return blast_radius.ChangedFiles(("styles.css",))

            assert_unknown(
                blast_radius.analyze(
                    self.fixture.root,
                    {},
                    cache=DroppedCacheAttestation(),
                    pr_snapshot=self.snapshot("one"),
                )
            )

        with self.subTest("missing candidate"):
            assert_unknown(blast_radius.analyze(self.fixture.root, {}, "deleted", cache=blast_radius.DiffCache(), pr_snapshot=self.snapshot("one")))


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

    def test_route_carries_registry_read_failure_into_incomplete_result(self) -> None:
        from backend.app import main

        fixture = FixtureRepo()
        self.addCleanup(fixture.close)
        with mock.patch.object(main, "ROOT_DIR", fixture.root), mock.patch.object(
            main, "_read_agent_registry", side_effect=OSError("registry unreadable")
        ):
            payload = main.blast_radius_view(candidate="all", branch=None, ticket=None)
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["risk"])
        self.assertTrue(any(failure["branch"] == "agent registry" for failure in payload["failed_branches"]))


if __name__ == "__main__":
    unittest.main()
