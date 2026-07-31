from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime import next_review as next_review_module
from backend.app.agent_runtime.next_review import (
    record_diverse_verdicts,
    synthesize_diverse_verdicts,
)
from backend.app.agent_runtime.diversity_orchestration import collect_diversity_verdict, journal_path
from backend.app.agent_runtime.ticket import base_ticket, parse_reviewer_id
from backend.app import main, workgraph
from backend.app.main import SpawnWorkerIn
from wiki_cli import graph_lint


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

    def test_default_prompt_is_byte_stable(self) -> None:
        expected = """review PR #151 for WIKI-181.

reviewer: WIKI-181-REVIEW1
round: 1
pinned sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
prior reviewer: none (first review round)

inspect the pinned worktree, identify actionable correctness, security, reliability,
and test issues, and report findings with file and line references. if the diff is
clean, report that explicitly. follow the repository review protocol and do not
modify the worktree. after the review, write the complete structured verdict to
/tmp/WIKI-181-REVIEW1-verdict.json. use state, source_sha, and findings fields;
each finding must include severity, path, line, problem, and fix.
"""
        self.assertEqual(
            next_review_module._default_prompt(
                ticket="WIKI-181", reviewer_id="WIKI-181-REVIEW1", pr_number=151,
                expected_sha="a" * 40, round_number=1, previous_reviewer=None,
            ),
            expected,
        )

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

    def test_implicit_request_retries_after_reviewer_becomes_terminal(self) -> None:
        registry: dict[str, dict[str, dict[str, str]]] = {}
        spawned: list[str] = []

        def spawn(request: SpawnWorkerIn) -> dict[str, str]:
            run_id = f"run-{len(spawned) + 1}"
            spawned.append(request.ticket)
            registry[request.ticket] = {
                "current": {"run_id": run_id, "state": "working"}
            }
            return {"run_id": run_id}

        kwargs = dict(
            ticket="WIKI-171",
            pr_number=171,
            expected_sha="c" * 40,
            orch="wiki",
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **_kwargs: Path("/repo/review"),
            spawn=spawn,
            archive=lambda _reviewer: {"outcome": "closed"},
            archived=lambda: [],
            registry=lambda: registry,
            status_reader=lambda _reviewer: None,
        )
        first = next_review_module.next_review(**kwargs)
        registry[first["reviewer"]]["current"]["state"] = "completed"
        second = next_review_module.next_review(**kwargs)

        self.assertEqual(first["reviewer"], "WIKI-171-REVIEW1")
        self.assertEqual(second["reviewer"], "WIKI-171-REVIEW2")
        self.assertEqual(spawned, ["WIKI-171-REVIEW1", "WIKI-171-REVIEW2"])

    def test_implicit_diversity_result_stays_current_while_one_lens_runs(self) -> None:
        registry: dict[str, dict[str, dict[str, str]]] = {}
        spawned: list[str] = []

        def spawn(request: SpawnWorkerIn) -> dict[str, str]:
            run_id = f"run-{len(spawned) + 1}"
            spawned.append(request.ticket)
            registry[request.ticket] = {
                "current": {"run_id": run_id, "state": "working"}
            }
            return {"run_id": run_id}

        kwargs = dict(
            ticket="WIKI-171",
            pr_number=171,
            expected_sha="d" * 40,
            orch="wiki",
            diversity=["correctness", "security"],
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **kwargs: Path(f"/repo/{kwargs['lens']}"),
            spawn=spawn,
            archived=lambda: [],
            registry=lambda: registry,
            status_reader=lambda _reviewer: None,
        )
        first = next_review_module.next_review(**kwargs)
        correctness = "WIKI-171-REVIEW1-correctness"
        registry[correctness]["current"]["state"] = "completed"
        second = next_review_module.next_review(**kwargs)

        self.assertEqual(second, first)
        self.assertEqual(
            spawned,
            [
                "WIKI-171-REVIEW1-correctness",
                "WIKI-171-REVIEW1-security",
            ],
        )

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

    def test_legacy_uppercase_lens_status_is_archived_before_next_round(self) -> None:
        reviewer = "WIKI-226-REVIEW1-SECURITY"
        (self.status_dir / f"{reviewer}.json").write_text(
            json.dumps({"state": "merge-ready"}), encoding="utf-8"
        )
        archived: list[str] = []

        result = next_review_module.next_review(
            "WIKI-226",
            226,
            "d" * 40,
            orch="wiki",
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **_kwargs: Path("/repo/review2"),
            spawn=lambda _request: {"run_id": "run-2"},
            archive=lambda value: archived.append(value) or {"outcome": "closed"},
            archived=lambda: [],
            registry=lambda: {
                reviewer: {"current": {"state": "working"}},
            },
        )

        self.assertEqual(result["reviewer"], "WIKI-226-REVIEW2")
        self.assertEqual(archived, ["WIKI-226-REVIEW1-security"])

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

    def test_diversity_fans_out_lenses_with_pinned_worktrees(self) -> None:
        spawned: list[SpawnWorkerIn] = []
        worktrees: list[dict[str, object]] = []

        def worktree(**kwargs: object) -> Path:
            worktrees.append(kwargs)
            return Path(f"/repo/{kwargs['lens']}")

        result = next_review_module.next_review(
            "WIKI-181",
            181,
            "a" * 40,
            orch="wiki",
            diversity=["correctness", "security"],
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=worktree,
            spawn=lambda request: spawned.append(request) or {"run_id": f"run-{request.ticket}"},
            archived=lambda: [],
            registry=lambda: {},
            request_id="diversity-fanout",
        )

        self.assertEqual(result["status"], "spawned")
        self.assertEqual(result["diversity"], ["correctness", "security"])
        self.assertEqual(
            {request.ticket for request in spawned},
            {"WIKI-181-REVIEW1-correctness", "WIKI-181-REVIEW1-security"},
        )
        self.assertEqual({item["lens"] for item in worktrees}, {"correctness", "security"})
        self.assertTrue(all("aaaaaaaa" in request.prompt for request in spawned))
        self.assertTrue(all("lens mandate:" in request.prompt for request in spawned))
        self.assertTrue(all("headRefOid" in request.prompt and "STALE-SHA" in request.prompt for request in spawned))

    def test_diversity_retry_only_respawns_unrecorded_lens(self) -> None:
        spawned: list[str] = []
        failed = True

        def spawn(request: SpawnWorkerIn) -> dict:
            nonlocal failed
            spawned.append(request.ticket)
            if request.ticket.endswith("-security") and failed:
                failed = False
                raise RuntimeError("fan-out crash")
            return {"run_id": request.ticket}

        kwargs = dict(
            ticket="WIKI-181",
            pr_number=181,
            expected_sha="b" * 40,
            orch="wiki",
            diversity=["correctness", "security"],
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **kwargs: Path(f"/repo/{kwargs['lens']}"),
            spawn=spawn,
            archived=lambda: [],
            registry=lambda: {},
            request_id="diversity-retry",
        )
        with self.assertRaisesRegex(RuntimeError, "fan-out crash"):
            next_review_module.next_review(**kwargs)
        result = next_review_module.next_review(**kwargs)

        self.assertEqual(result["status"], "spawned")
        self.assertEqual(spawned.count("WIKI-181-REVIEW1-correctness"), 1)
        self.assertEqual(spawned.count("WIKI-181-REVIEW1-security"), 2)

    def test_diversity_round_two_uses_new_lens_names(self) -> None:
        captured: list[str] = []
        result = next_review_module.next_review(
            "WIKI-181",
            181,
            "c" * 40,
            orch="wiki",
            diversity=2,
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **kwargs: Path(f"/repo/{kwargs['lens']}"),
            spawn=lambda request: captured.append(request.ticket) or {"run_id": request.ticket},
            archive=lambda _reviewer: {"outcome": "closed"},
            archived=lambda: [],
            registry=lambda: {
                "WIKI-181-REVIEW1-correctness": {"current": {"state": "completed"}},
                "WIKI-181-REVIEW1-security": {"current": {"state": "completed"}},
            },
            status_reader=lambda _reviewer: {"state": "completed"},
            request_id="diversity-round-two",
        )
        self.assertEqual(result["round"], 2)
        self.assertEqual(
            set(captured),
            {"WIKI-181-REVIEW2-correctness", "WIKI-181-REVIEW2-security"},
        )

    def test_synthesis_dedupes_and_keeps_max_severity_and_lenses(self) -> None:
        result = synthesize_diverse_verdicts(
            "WIKI-181",
            "c" * 40,
            {
                "correctness": {
                    "worker": "WIKI-181-REVIEW1-correctness",
                    "state": "NOT-MERGE-READY",
                    "findings": [{"severity": "LOW", "file": "x.py", "line": 10, "problem": "bad cache key", "fix": "fix it"}],
                },
                "security": {
                    "worker": "WIKI-181-REVIEW1-security",
                    "state": "NOT-MERGE-READY",
                    "findings": [{"severity": "BLOCKING", "file": "x.py", "line": 10, "problem": "bad cache key permits attack", "fix": "fix it"}],
                },
            },
            created_at="2026-07-30T00:00:00+00:00",
        )

        self.assertEqual(result["state"], "NOT-MERGE-READY")
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["severity"], "BLOCKING")
        self.assertEqual(result["findings"][0]["source_lenses"], ["correctness", "security"])

    def test_synthesis_records_lens_and_combined_verdict_edges(self) -> None:
        calls: list[dict] = []
        result = record_diverse_verdicts(
            ticket="WIKI-181",
            expected_sha="d" * 40,
            verdicts={
                "correctness": {"state": "MERGE-READY", "source_sha": "d" * 40, "findings": []},
                "security": {"state": "MERGE-READY", "source_sha": "d" * 40, "findings": []},
            },
            orch="wiki",
            record_verdict=lambda **kwargs: calls.append(kwargs),
            request_id="synthesis-edges",
            round_number=2,
        )

        self.assertEqual(result["state"], "MERGE-READY")
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call["payload"]["worker"] for call in calls],
            [
                "WIKI-181-REVIEW2-correctness",
                "WIKI-181-REVIEW2-security",
                "WIKI-181-REVIEW2-synthesis",
            ],
        )
        for call in calls:
            edge = {
                "kind": "verdict",
                "from": call["reviewer"],
                "to": "orch:wiki",
                "payload": call["payload"],
                "created_at": "2026-07-30T00:00:00+00:00",
            }
            self.assertEqual(graph_lint.validate_document(edge, "edge"), [])

    def test_collector_waits_for_exact_set_and_records_only_combined_route(self) -> None:
        next_review_module.next_review(
            "WIKI-181",
            181,
            "e" * 40,
            orch="wiki",
            diversity=["correctness", "security"],
            gate=lambda _pr, _sha: {"verdict": "pass"},
            resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **kwargs: Path(f"/repo/{kwargs['lens']}"),
            spawn=lambda request: {"run_id": request.ticket},
            archived=lambda: [],
            registry=lambda: {},
            request_id="collector-journal",
        )
        calls: list[dict] = []
        clean = lambda reviewer: {
            "worker": reviewer,
            "state": "MERGE-READY",
            "source_sha": "e" * 40,
            "findings": [],
        }
        first = collect_diversity_verdict(
            runtime_dir=Path(self.runtime_tmp.name),
            ticket="WIKI-181",
            reviewer="WIKI-181-REVIEW1-correctness",
            verdict=clean("WIKI-181-REVIEW1-correctness"),
            record_verdict=lambda **kwargs: calls.append(kwargs),
        )
        second = collect_diversity_verdict(
            runtime_dir=Path(self.runtime_tmp.name),
            ticket="WIKI-181",
            reviewer="WIKI-181-REVIEW1-security",
            verdict=clean("WIKI-181-REVIEW1-security"),
            record_verdict=lambda **kwargs: calls.append(kwargs),
        )

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["state"], "MERGE-READY")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1]["payload"]["worker"], "WIKI-181-REVIEW1-synthesis")
        self.assertEqual(parse_reviewer_id(calls[-1]["reviewer"]).round, 1)
        self.assertEqual(base_ticket("WIKI-181-REVIEW1-security"), "WIKI-181")

    def test_reviewer_parser_is_case_insensitive_and_round_scoped(self) -> None:
        parsed = parse_reviewer_id("wiki-181-review2-SECURITY")
        self.assertEqual(parsed.ticket, "WIKI-181")
        self.assertEqual(parsed.round, 2)
        self.assertEqual(parsed.lens, "security")
        self.assertEqual(base_ticket("wiki-181-review2-SECURITY"), "WIKI-181")

    def test_collector_restart_boundary_requires_disk_journal(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "journal is missing"):
            collect_diversity_verdict(
                runtime_dir=Path(self.runtime_tmp.name),
                ticket="WIKI-181",
                reviewer="WIKI-181-REVIEW1-security",
                verdict={"state": "MERGE-READY", "source_sha": "e" * 40, "findings": []},
                record_verdict=lambda **_kwargs: None,
            )

    def test_collector_keeps_stale_report_dirty_until_synthesis(self) -> None:
        next_review_module.next_review(
            "WIKI-181", 181, "3" * 40, orch="wiki", diversity=["correctness", "security"],
            gate=lambda _pr, _sha: {"verdict": "pass"}, resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **kwargs: Path(f"/repo/{kwargs['lens']}"),
            spawn=lambda request: {"run_id": request.ticket}, archived=lambda: [], registry=lambda: {},
            request_id="collector-stale",
        )
        calls: list[dict] = []
        for lens, sha in (("correctness", "0" * 40), ("security", "3" * 40)):
            report = collect_diversity_verdict(
                runtime_dir=Path(self.runtime_tmp.name), ticket="WIKI-181",
                reviewer=f"WIKI-181-REVIEW1-{lens}",
                verdict={"state": "MERGE-READY", "source_sha": sha, "findings": []},
                record_verdict=lambda **kwargs: calls.append(kwargs),
            )
        self.assertEqual(report["state"], "NOT-MERGE-READY")
        self.assertEqual(calls[-1]["payload"]["state"], "NOT-MERGE-READY")

    def test_synthesis_marks_missing_and_stale_lenses_dirty(self) -> None:
        missing = synthesize_diverse_verdicts(
            "WIKI-181",
            "f" * 40,
            {"correctness": {"state": "MERGE-READY", "source_sha": "f" * 40, "findings": []}},
            expected_lenses=["correctness", "security"],
        )
        stale = synthesize_diverse_verdicts(
            "WIKI-181",
            "f" * 40,
            {
                "correctness": {"state": "MERGE-READY", "source_sha": "0" * 40, "findings": []},
                "security": {"state": "MERGE-READY", "source_sha": "f" * 40, "findings": []},
            },
            expected_lenses=["correctness", "security"],
        )
        self.assertEqual(missing["state"], "NOT-MERGE-READY")
        self.assertEqual(stale["state"], "NOT-MERGE-READY")

    def test_synthesis_marks_non_iterable_findings_dirty(self) -> None:
        result = synthesize_diverse_verdicts(
            "WIKI-181",
            "f" * 40,
            {
                "correctness": {
                    "state": "MERGE-READY",
                    "source_sha": "f" * 40,
                    "findings": {"unexpected": "mapping"},
                }
            },
            expected_lenses=["correctness"],
        )
        self.assertEqual(result["state"], "NOT-MERGE-READY")

    def test_collector_duplicate_is_idempotent_but_conflicts_are_rejected(self) -> None:
        next_review_module.next_review(
            "WIKI-181", 181, "4" * 40, orch="wiki", diversity=["correctness", "security"],
            gate=lambda _pr, _sha: {"verdict": "pass"}, resolve_root=lambda _orch: Path("/repo"),
            worktree=lambda **kwargs: Path(f"/repo/{kwargs['lens']}"),
            spawn=lambda request: {"run_id": request.ticket}, archived=lambda: [], registry=lambda: {},
            request_id="collector-duplicates",
        )
        calls: list[dict] = []
        report = {
            "worker": "WIKI-181-REVIEW1-correctness",
            "state": "MERGE-READY",
            "source_sha": "4" * 40,
            "findings": [],
        }
        first = collect_diversity_verdict(
            runtime_dir=Path(self.runtime_tmp.name), ticket="WIKI-181",
            reviewer=report["worker"], verdict=report, record_verdict=lambda **kwargs: calls.append(kwargs),
        )
        duplicate = collect_diversity_verdict(
            runtime_dir=Path(self.runtime_tmp.name), ticket="WIKI-181",
            reviewer=report["worker"], verdict=dict(report), record_verdict=lambda **kwargs: calls.append(kwargs),
        )
        self.assertEqual(first["status"], "pending")
        self.assertEqual(duplicate["status"], "pending")
        with self.assertRaisesRegex(RuntimeError, "conflicting duplicate"):
            collect_diversity_verdict(
                runtime_dir=Path(self.runtime_tmp.name), ticket="WIKI-181",
                reviewer=report["worker"],
                verdict={**report, "state": "NOT-MERGE-READY"},
                record_verdict=lambda **kwargs: calls.append(kwargs),
            )
        security = {
            "worker": "WIKI-181-REVIEW1-security",
            "state": "MERGE-READY",
            "source_sha": "4" * 40,
            "findings": [],
        }
        complete = collect_diversity_verdict(
            runtime_dir=Path(self.runtime_tmp.name), ticket="WIKI-181",
            reviewer=security["worker"], verdict=security, record_verdict=lambda **kwargs: calls.append(kwargs),
        )
        again = collect_diversity_verdict(
            runtime_dir=Path(self.runtime_tmp.name), ticket="WIKI-181",
            reviewer=security["worker"], verdict=dict(security), record_verdict=lambda **kwargs: calls.append(kwargs),
        )
        self.assertEqual(complete["state"], "MERGE-READY")
        self.assertEqual(again, complete)

    def test_synthesis_is_byte_stable_and_different_lines_do_not_dedupe(self) -> None:
        verdicts = {
            "correctness": {
                "state": "NOT-MERGE-READY",
                "source_sha": "1" * 40,
                "findings": [{"severity": "HIGH", "file": "x.py", "line": 3, "problem": "unsafe input", "fix": "validate"}],
            },
            "security": {
                "state": "NOT-MERGE-READY",
                "source_sha": "1" * 40,
                "findings": [{"severity": "MEDIUM", "file": "x.py", "line": 4, "problem": "unsafe input", "fix": "validate"}],
            },
        }
        first = synthesize_diverse_verdicts("WIKI-181", "1" * 40, verdicts, expected_lenses=["correctness", "security"])
        second = synthesize_diverse_verdicts("WIKI-181", "1" * 40, verdicts, expected_lenses=["security", "correctness"])
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(len(first["findings"]), 2)

    def test_generated_diversity_edges_form_a_valid_workgraph(self) -> None:
        calls: list[dict] = []
        record_diverse_verdicts(
            ticket="WIKI-181",
            expected_sha="2" * 40,
            verdicts={
                "correctness": {"state": "MERGE-READY", "source_sha": "2" * 40, "findings": []},
                "security": {"state": "MERGE-READY", "source_sha": "2" * 40, "findings": []},
            },
            orch="wiki",
            record_verdict=lambda **kwargs: calls.append(kwargs),
            request_id="graph-generated",
            round_number=1,
            created_at="2026-07-30T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            status_dir = Path(directory) / "status"
            snapshot_dir = Path(directory) / "snapshots"
            workgraph.append_edge(
                "WIKI-181", "spawn", "orch:wiki", "WIKI-181", {
                    "ticket": "WIKI-181", "role": "implement", "model": "test",
                    "worktree": "/repo", "request_id": "graph-spawn",
                }, orch="wiki", status_dir=status_dir, snapshot_dir=snapshot_dir,
            )
            for lens in ("correctness", "security"):
                workgraph.append_edge(
                    "WIKI-181", "spawn", "WIKI-181", f"WIKI-181-REVIEW1-{lens}", {
                        "ticket": f"WIKI-181-REVIEW1-{lens}", "role": "review", "model": "test",
                        "worktree": f"/repo/{lens}", "request_id": f"graph-spawn:{lens}",
                    }, orch="wiki", status_dir=status_dir, snapshot_dir=snapshot_dir,
                )
            for call in calls:
                workgraph.append_edge(
                    "WIKI-181", "verdict", call["reviewer"], "orch:wiki", call["payload"],
                    orch="wiki", status_dir=status_dir, snapshot_dir=snapshot_dir,
                    request_id=call["request_id"],
                )
            graph = workgraph.load_workgraph("WIKI-181", status_dir)
            self.assertEqual(graph_lint.validate_document(graph, "workgraph"), [])


if __name__ == "__main__":
    unittest.main()
