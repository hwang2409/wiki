from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime import next_review as next_review_module
from backend.app import main
from backend.app.main import SpawnWorkerIn


class NextReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_tmp = tempfile.TemporaryDirectory()
        self.status_dir = Path(self.runtime_tmp.name) / "status"
        self.status_dir.mkdir()
        self.runtime_patch = mock.patch.object(
            main, "AGENT_RUNTIME_DIR", Path(self.runtime_tmp.name)
        )
        self.status_patch = mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir)
        self.runtime_patch.start()
        self.status_patch.start()
        next_review_module._REQUEST_RESULTS.clear()  # noqa: SLF001
        next_review_module._REQUEST_STAGES.clear()  # noqa: SLF001
        next_review_module._REQUEST_STATE_LOADED = False  # noqa: SLF001

    def tearDown(self) -> None:
        next_review_module._REQUEST_RESULTS.clear()  # noqa: SLF001
        next_review_module._REQUEST_STAGES.clear()  # noqa: SLF001
        next_review_module._REQUEST_STATE_LOADED = False  # noqa: SLF001
        self.status_patch.stop()
        self.runtime_patch.stop()
        self.runtime_tmp.cleanup()

    def test_happy_path_increments_round_spawns_and_archives_previous(self) -> None:
        calls: list[tuple[str, object]] = []

        def gate(pr: int, sha: str) -> dict:
            calls.append(("gate", (pr, sha)))
            return {"verdict": "pass"}

        def worktree(**kwargs: object) -> Path:
            calls.append(("worktree", kwargs))
            return Path("/repo/.codex/worktrees/wiki-171-review4")

        def spawn(request: SpawnWorkerIn) -> dict:
            calls.append(("spawn", request))
            return {"run_id": "run-4"}

        def archive(reviewer: str) -> dict:
            calls.append(("archive", reviewer))
            return {"state": "completed"}

        result = next_review_module.next_review(
            ticket="WIKI-171",
            pr_number=171,
            expected_sha="a" * 40,
            orch="wiki",
            gate=gate,
            resolve_root=lambda _orch: Path("/repo"),
            worktree=worktree,
            spawn=spawn,
            archive=archive,
            archived=lambda: [
                {"ticket": "WIKI-171-REVIEW1"},
            ],
            registry=lambda: {
                "WIKI-171-REVIEW3": {"current": {"state": "completed"}}
            },
            request_id="next-review-1",
        )

        self.assertEqual(result["status"], "spawned")
        self.assertEqual(result["reviewer"], "WIKI-171-REVIEW4")
        self.assertEqual(result["run_id"], "run-4")
        self.assertEqual(result["orch"], "wiki")
        self.assertEqual([name for name, _value in calls], ["gate", "worktree", "spawn", "archive"])
        spawn_args = calls[2][1]
        assert isinstance(spawn_args, SpawnWorkerIn)
        self.assertEqual(spawn_args.ticket, "WIKI-171-REVIEW4")
        self.assertEqual(spawn_args.orch, "wiki")
        self.assertIn("a" * 40, spawn_args.prompt)
        self.assertEqual(calls[3][1], "WIKI-171-REVIEW3")

    def test_gate_failure_has_no_side_effects(self) -> None:
        side_effects: list[str] = []

        result = next_review_module.next_review(
            ticket="WIKI-171",
            pr_number=171,
            expected_sha="b" * 40,
            orch="wiki",
            gate=lambda _pr, _sha: {"verdict": "fail", "summary": "checks pending"},
            resolve_root=lambda _orch: side_effects.append("root") or Path("/repo"),
            worktree=lambda **_kwargs: side_effects.append("worktree") or Path("/repo/wt"),
            spawn=lambda _request: side_effects.append("spawn") or {"run_id": "run"},
            archive=lambda _reviewer: side_effects.append("archive") or {},
            archived=lambda: side_effects.append("archived") or [],
            registry=lambda: side_effects.append("registry") or {},
            request_id="next-review-gate-fail",
        )

        self.assertEqual(result, {"status": "gate_failed", "detail": "checks pending"})
        self.assertEqual(side_effects, [])

    def test_request_id_replays_same_run_without_repeating_steps(self) -> None:
        counts = {"gate": 0, "worktree": 0, "spawn": 0, "archive": 0}

        def gate(_pr: int, _sha: str) -> dict:
            counts["gate"] += 1
            return {"verdict": "pass"}

        def worktree(**_kwargs: object) -> Path:
            counts["worktree"] += 1
            return Path("/repo/wt")

        def spawn(_request: SpawnWorkerIn) -> dict:
            counts["spawn"] += 1
            return {"run_id": "run-idempotent"}

        def archive(_reviewer: str) -> dict:
            counts["archive"] += 1
            return {}

        kwargs = dict(
            ticket="WIKI-171",
            pr_number=171,
            expected_sha="c" * 40,
            orch="wiki",
            gate=gate,
            resolve_root=lambda _orch: Path("/repo"),
            worktree=worktree,
            spawn=spawn,
            archive=archive,
            archived=lambda: [],
            registry=lambda: {},
            request_id="next-review-retry",
        )
        first = next_review_module.next_review(**kwargs)
        second = next_review_module.next_review(**kwargs)

        self.assertEqual(first, second)
        self.assertEqual(counts, {"gate": 1, "worktree": 1, "spawn": 1, "archive": 0})

    def test_merge_ready_previous_reviewer_is_archived(self) -> None:
        archived: list[str] = []
        (self.status_dir / "WIKI-171-REVIEW2.json").write_text(
            json.dumps({"state": "merge-ready"}), encoding="utf-8"
        )

        result = next_review_module.next_review(
            "WIKI-171",
            171,
            "d" * 40,
            orch="wiki",
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **_kwargs: Path("/repo/review3"),
            spawn=lambda _request: {"run_id": "run-3"},
            archive=lambda reviewer: archived.append(reviewer) or {"outcome": "closed"},
            archived=lambda: [{"ticket": "WIKI-171-REVIEW1"}],
            registry=lambda: {
                "WIKI-171-REVIEW2": {"current": {"state": "working"}}
            },
            request_id="merge-ready-previous",
        )

        self.assertEqual(result["reviewer"], "WIKI-171-REVIEW3")
        self.assertEqual(archived, ["WIKI-171-REVIEW2"])

    def test_archive_failure_stages_spawn_and_retry_does_not_respawn(self) -> None:
        spawn_count = 0
        archive_count = 0

        def spawn(_request: SpawnWorkerIn) -> dict:
            nonlocal spawn_count
            spawn_count += 1
            return {"run_id": "run-3"}

        def archive(_reviewer: str) -> dict:
            nonlocal archive_count
            archive_count += 1
            if archive_count == 1:
                raise RuntimeError("archive temporarily unavailable")
            return {"outcome": "closed"}

        kwargs = dict(
            ticket="WIKI-171",
            pr_number=171,
            expected_sha="e" * 40,
            orch="wiki",
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **_kwargs: Path("/repo/review3"),
            spawn=spawn,
            archive=archive,
            archived=lambda: [{"ticket": "WIKI-171-REVIEW1"}],
            registry=lambda: {
                "WIKI-171-REVIEW2": {"current": {"state": "merge-ready"}}
            },
            request_id="staged-archive-retry",
        )
        with self.assertRaisesRegex(RuntimeError, "archive temporarily unavailable"):
            next_review_module.next_review(**kwargs)
        result = next_review_module.next_review(**kwargs)

        self.assertEqual(result["reviewer"], "WIKI-171-REVIEW3")
        self.assertEqual(result["run_id"], "run-3")
        self.assertEqual(spawn_count, 1)
        self.assertEqual(archive_count, 2)

    def test_spawn_boundary_retry_reuses_durable_reviewer_intent(self) -> None:
        spawn_count = 0
        spawned: dict[str, str] = {}
        registry_state: dict[str, dict[str, dict[str, str]]] = {
            "WIKI-171-REVIEW2": {"current": {"state": "completed"}}
        }
        request_id = "spawn-boundary"

        def spawn(request: SpawnWorkerIn) -> dict:
            nonlocal spawn_count
            if request.request_id not in spawned:
                spawn_count += 1
                spawned[request.request_id or ""] = "run-3"
            registry_state[request.ticket] = {
                "current": {
                    "state": "working",
                    "request_id": request.request_id or "",
                }
            }
            return {"run_id": spawned[request.request_id or ""]}

        original_persist = next_review_module._persist_request_state  # noqa: SLF001
        raised = False

        def fail_after_spawn() -> None:
            nonlocal raised
            staged = next_review_module._REQUEST_STAGES.get(request_id)  # noqa: SLF001
            if staged and staged.get("spawn_completed") and not raised:
                raised = True
                raise RuntimeError("crash after spawn")
            original_persist()

        kwargs = dict(
            ticket="WIKI-171",
            pr_number=171,
            expected_sha="1" * 40,
            orch="wiki",
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **_kwargs: Path("/repo/review3"),
            spawn=spawn,
            archive=lambda _reviewer: {"outcome": "closed"},
            archived=lambda: [],
            registry=lambda: registry_state,
            request_id=request_id,
        )
        with mock.patch.object(next_review_module, "_persist_request_state", fail_after_spawn):
            with self.assertRaisesRegex(RuntimeError, "crash after spawn"):
                next_review_module.next_review(**kwargs)
        self.assertIn("WIKI-171-REVIEW3", registry_state)

        next_review_module._REQUEST_RESULTS.clear()  # noqa: SLF001
        next_review_module._REQUEST_STAGES.clear()  # noqa: SLF001
        next_review_module._REQUEST_STATE_LOADED = False  # noqa: SLF001
        result = next_review_module.next_review(**kwargs)

        self.assertEqual(result["reviewer"], "WIKI-171-REVIEW3")
        self.assertEqual(result["run_id"], "run-3")
        self.assertEqual(spawn_count, 1)

    def test_archive_boundary_retry_verifies_archive_before_retrying(self) -> None:
        archive_count = 0
        archived_rows: list[dict[str, str]] = []
        request_id = "archive-boundary"

        def archive(reviewer: str) -> dict:
            nonlocal archive_count
            archive_count += 1
            archived_rows.append({"ticket": reviewer})
            return {"outcome": "closed"}

        original_persist = next_review_module._persist_request_state  # noqa: SLF001
        raised = False

        def fail_after_archive() -> None:
            nonlocal raised
            staged = next_review_module._REQUEST_STAGES.get(request_id)  # noqa: SLF001
            if staged and staged.get("archive_completed") and not raised:
                raised = True
                raise RuntimeError("crash after archive")
            original_persist()

        kwargs = dict(
            ticket="WIKI-171",
            pr_number=171,
            expected_sha="2" * 40,
            orch="wiki",
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **_kwargs: Path("/repo/review3"),
            spawn=lambda _request: {"run_id": "run-3"},
            archive=archive,
            archived=lambda: list(archived_rows),
            registry=lambda: {
                "WIKI-171-REVIEW2": {"current": {"state": "completed"}}
            },
            request_id=request_id,
        )
        with mock.patch.object(next_review_module, "_persist_request_state", fail_after_archive):
            with self.assertRaisesRegex(RuntimeError, "crash after archive"):
                next_review_module.next_review(**kwargs)

        next_review_module._REQUEST_RESULTS.clear()  # noqa: SLF001
        next_review_module._REQUEST_STAGES.clear()  # noqa: SLF001
        next_review_module._REQUEST_STATE_LOADED = False  # noqa: SLF001
        result = next_review_module.next_review(**kwargs)

        self.assertEqual(result["reviewer"], "WIKI-171-REVIEW3")
        self.assertEqual(archive_count, 1)

    def test_claude_review_defaults_effort_to_none(self) -> None:
        captured: list[SpawnWorkerIn] = []

        result = next_review_module.next_review(
            "WIKI-171",
            171,
            "f" * 40,
            reviewer_kind="cc",
            reviewer_model="sonnet",
            orch="wiki",
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **_kwargs: Path("/repo/review1"),
            spawn=lambda request: captured.append(request) or {"run_id": "cc-run"},
            archived=lambda: [],
            registry=lambda: {},
            request_id="claude-review",
        )

        self.assertEqual(result["run_id"], "cc-run")
        self.assertEqual(captured[0].effort, None)


if __name__ == "__main__":
    unittest.main()
