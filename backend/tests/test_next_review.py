from __future__ import annotations

import unittest
from pathlib import Path

from backend.app.agent_runtime import next_review as next_review_module


class NextReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        next_review_module._REQUEST_RESULTS.clear()  # noqa: SLF001

    def test_happy_path_increments_round_spawns_and_archives_previous(self) -> None:
        calls: list[tuple[str, object]] = []

        def gate(pr: int, sha: str) -> dict:
            calls.append(("gate", (pr, sha)))
            return {"verdict": "pass"}

        def worktree(**kwargs: object) -> Path:
            calls.append(("worktree", kwargs))
            return Path("/repo/.codex/worktrees/wiki-171-review4")

        def spawn(**kwargs: object) -> dict:
            calls.append(("spawn", kwargs))
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
                {"ticket": "WIKI-171-REVIEW3"},
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
        assert isinstance(spawn_args, dict)
        self.assertEqual(spawn_args["reviewer_id"], "WIKI-171-REVIEW4")
        self.assertEqual(spawn_args["orch"], "wiki")
        self.assertIn("a" * 40, spawn_args["prompt"])
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
            spawn=lambda **_kwargs: side_effects.append("spawn") or {"run_id": "run"},
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

        def spawn(**_kwargs: object) -> dict:
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


if __name__ == "__main__":
    unittest.main()
