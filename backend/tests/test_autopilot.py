from __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.app.agent_runtime.autopilot import (
    AutopilotController,
    AutopilotStore,
    build_steer_message,
    parse_verdict,
)


def _graph(verdicts: list[dict]) -> dict:
    edges = [{"kind": "spawn", "to": "WIKI-173", "payload": {"role": "implement"}}]
    for index, verdict in enumerate(verdicts, start=1):
        reviewer = f"WIKI-173-REVIEW{index}"
        edges.extend(
            [
                {"kind": "spawn", "to": reviewer, "payload": {"role": "review"}},
                {
                    "kind": "verdict",
                    "from": reviewer,
                    "created_at": f"2026-07-29T{index:02d}:00:00+00:00",
                    "payload": {**verdict, "worker": reviewer},
                },
            ]
        )
    return {
        "ticket": "WIKI-173",
        "orch": "wiki",
        "iteration_cap": 8,
        "nodes": [],
        "edges": edges,
    }


ARCHIVED_VERDICTS = (
    (
        "NOT-MERGE-READY: 1 findings\n\n"
        "- [BLOCKING] backend/app/agent_runtime/autopilot.py:526 — stale reviewer verdict\n"
        "  Fix: select the current reviewer and require a matching head sha."
    ),
    (
        "NOT-MERGE-READY: 1 findings\n\n"
        "1. [BLOCKING] backend/app/github_pr.py:318 - merge has a gate-to-merge race\n"
        "   fix: pass the reviewed head sha to gh pr merge."
    ),
    (
        "VERDICT: NEEDS FIXES — 1 finding\n\n"
        "- [HIGH] `frontend/src/loop-state-chrome.tsx:279` — autopilot log hides context\n"
        "  recommendation: render verdicts, steer previews, action details, and halt reasons."
    ),
    (
        "NOT-MERGE-READY: 1 findings\n\n"
        "- [MAJOR] backend/app/workgraph.py:713: edge timestamps are lost\n"
        "  do instead: retain created_at on every edge."
    ),
    (
        "VERDICT: NOT-MERGE-READY: 1 findings\n\n"
        "2. [MEDIUM] backend/tests/test_autopilot.py:40 - source tags are not asserted\n"
        "   Fix: assert source=autopilot on every recorded action."
    ),
)


class AutopilotTests(unittest.TestCase):
    def test_parser_and_steer_golden_shape(self) -> None:
        verdict = parse_verdict(
            """NOT-MERGE-READY: 1 findings

1. [HIGH] backend/app/cache.py:42 - cache can return stale data
   fix: invalidate the entry before returning
   mutation contract: deleting the invalidation must fail the regression test
""",
            source_sha="0123456",
        )
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertEqual(verdict.state, "NOT-MERGE-READY")
        self.assertEqual(verdict.findings[0].path, "backend/app/cache.py")
        self.assertEqual(verdict.findings[0].line, 42)
        self.assertIn("source sha: 0123456", build_steer_message(verdict))
        self.assertIn("mutation contract", build_steer_message(verdict))

    def test_json_fallback_for_nonstandard_output(self) -> None:
        verdict = parse_verdict(
            "review output omitted the normal header",
            fallback=lambda _text: {"state": "MERGE-READY", "findings": []},
        )
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertTrue(verdict.clean)

    def test_gate_clean_and_verdict_clean_merges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {
                    "pr": "https://github.com/hwang2409/wiki/pull/173",
                    "sha": "0123456",
                },
                graph_loader=lambda _ticket: _graph(
                    [{"state": "MERGE-READY", "sha": "0123456", "findings": []}]
                ),
                gate=lambda _number, _sha: {"verdict": "pass"},
                merge=lambda _ticket, _sha: None,
            )
            merged: list[str] = []
            controller.merge = lambda ticket, _sha: merged.append(ticket)
            controller.enable("WIKI-173")
            asyncio.run(
                controller.on_transition(
                    {
                        "agent_id": "WIKI-173-REVIEW1",
                        "run_id": "r1",
                        "status_state": "merge-ready",
                    }
                )
            )
            self.assertEqual(merged, ["WIKI-173"])

    def test_dirty_gate_and_nonclean_verdict_never_merge(self) -> None:
        for gate_result, verdict in (
            (
                {"verdict": "fail"},
                {"state": "MERGE-READY", "sha": "0123456", "findings": []},
            ),
            (
                {"verdict": "pass"},
                {
                    "state": "NOT-MERGE-READY",
                    "sha": "0123456",
                    "findings": [
                        {
                            "id": "F-abc123",
                            "severity": "HIGH",
                            "title": "fix it",
                            "observed": "bad",
                            "why_wrong": "wrong",
                            "do_instead": "fix",
                        }
                    ],
                },
            ),
        ):
            with tempfile.TemporaryDirectory() as directory:
                merged: list[str] = []
                controller = AutopilotController(
                    store=AutopilotStore(Path(directory)),
                    status_reader=lambda _ticket: {
                        "pr": "https://github.com/hwang2409/wiki/pull/173",
                        "sha": "0123456",
                    },
                    graph_loader=lambda _ticket, verdict=verdict: _graph([verdict]),
                    gate=lambda _number, _sha, gate_result=gate_result: gate_result,
                    merge=lambda ticket, _sha: merged.append(ticket),
                    steer=lambda _ticket, _message: None,
                    archive=lambda _reviewer: None,
                )
                controller.enable("WIKI-173")
                asyncio.run(
                    controller.on_transition(
                        {
                            "agent_id": "WIKI-173-REVIEW1",
                            "run_id": "r1",
                            "status_state": "merge-ready",
                        }
                    )
                )
                self.assertEqual(merged, [])

    def test_iteration_cap_halts_before_next_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spawned: list[dict] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {
                    "pr": "https://github.com/hwang2409/wiki/pull/173",
                    "sha": "0123456",
                },
                graph_loader=lambda _ticket: {
                    **_graph(
                        [
                            {
                                "state": "NOT-MERGE-READY",
                                "sha": "0123456",
                                "findings": [
                                    {
                                        "id": "F-abc123",
                                        "severity": "HIGH",
                                        "title": "same",
                                        "observed": "bad",
                                        "why_wrong": "wrong",
                                        "do_instead": "fix",
                                    }
                                ],
                            }
                        ]
                    ),
                    "iteration_cap": 1,
                },
                next_review=lambda **kwargs: spawned.append(kwargs),
                steer=lambda _ticket, _message: None,
                archive=lambda _reviewer: None,
            )
            controller.enable("WIKI-173")
            asyncio.run(
                controller.on_transition(
                    {
                        "agent_id": "WIKI-173",
                        "run_id": "r1",
                        "status_state": "merge-ready",
                    }
                )
            )
            self.assertEqual(spawned, [])
            self.assertEqual(controller.status("WIKI-173")["halted"], "iteration-cap")

    def test_plateau_halts_after_three_overlapping_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verdicts = [
                {
                    "state": "NOT-MERGE-READY",
                    "sha": "0123456",
                    "findings": [
                        {
                            "id": f"F-abc12{i}",
                            "severity": "HIGH",
                            "title": "same finding",
                            "observed": "bad",
                            "why_wrong": "wrong",
                            "do_instead": "fix",
                        }
                    ],
                }
                for i in range(3)
            ]
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {"sha": "0123456"},
                graph_loader=lambda _ticket: _graph(verdicts),
                steer=lambda _ticket, _message: None,
                archive=lambda _reviewer: None,
            )
            controller.enable("WIKI-173")
            asyncio.run(
                controller.on_transition(
                    {
                        "agent_id": "WIKI-173-REVIEW3",
                        "run_id": "r3",
                        "status_state": "merge-ready",
                    }
                )
            )
            self.assertEqual(controller.status("WIKI-173")["halted"], "plateau")
            self.assertTrue(controller.status("WIKI-173")["enabled"])
            controller.enable("WIKI-173")
            self.assertEqual(controller.status("WIKI-173")["halted"], "plateau")
            controller.disable("WIKI-173")
            controller.enable("WIKI-173")
            self.assertIsNone(controller.status("WIKI-173")["halted"])

    def test_default_off_does_not_act_without_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spawned: list[dict] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {"pr": "173", "sha": "0123456"},
                registry_reader=lambda: {"WIKI-173": {"current": {"orch": "wiki"}}},
                graph_loader=lambda _ticket: _graph(
                    [{"state": "NOT-MERGE-READY", "sha": "0123456", "findings": []}]
                ),
                next_review=lambda **kwargs: spawned.append(kwargs),
            )
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-173", "run_id": "r1", "status_state": "merge-ready"}
                )
            )
            self.assertEqual(spawned, [])
            self.assertFalse(controller.status("WIKI-173")["enabled"])

    def test_ack_required_blocks_then_allows_clean_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            merged: list[tuple[str, str]] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {
                    "pr": "https://github.com/hwang2409/wiki/pull/173",
                    "sha": "0123456",
                },
                graph_loader=lambda _ticket: _graph(
                    [{"state": "MERGE-READY", "sha": "0123456", "findings": []}]
                ),
                gate=lambda _number, _sha: {"verdict": "pass"},
                merge=lambda ticket, sha: merged.append((ticket, sha)),
            )
            controller.enable("WIKI-173", henry_ack_required_for_merge=True)
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-173-REVIEW1", "run_id": "r1", "status_state": "merge-ready"}
                )
            )
            self.assertEqual(merged, [])
            controller.ack_merge("WIKI-173")
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-173-REVIEW1", "run_id": "r2", "status_state": "merge-ready"}
                )
            )
            self.assertEqual(merged, [("WIKI-173", "0123456")])

    def test_stale_reviewer_and_stale_sha_cannot_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            merged: list[str] = []
            graph = _graph(
                [
                    {
                        "state": "NOT-MERGE-READY",
                        "sha": "0123456",
                        "findings": [{"severity": "HIGH", "path": "x.py", "line": 1, "fix": "fix"}],
                    },
                    {"state": "MERGE-READY", "sha": "0123456", "findings": []},
                ]
            )
            graph["edges"][1]["created_at"] = "2026-07-29T20:00:00+00:00"
            graph["edges"][3]["created_at"] = "2026-07-29T21:00:00+00:00"
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {
                    "pr": "https://github.com/hwang2409/wiki/pull/173",
                    "sha": "0123456",
                },
                graph_loader=lambda _ticket: graph,
                gate=lambda _number, _sha: {"verdict": "pass"},
                merge=lambda ticket, _sha: merged.append(ticket),
                steer=lambda _ticket, _message: None,
                archive=lambda _reviewer: None,
            )
            controller.enable("WIKI-173")
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-173-REVIEW1", "run_id": "r1", "status_state": "merge-ready"}
                )
            )
            self.assertEqual(merged, [])
            graph["edges"][4]["payload"]["state"] = "MERGE-READY"
            graph["edges"][4]["payload"]["sha"] = "stale00"
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-173-REVIEW2", "run_id": "r3", "status_state": "merge-ready"}
                )
            )
            self.assertEqual(merged, [])

    def test_current_reviewer_falls_back_to_review_node_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(
                [{"state": "MERGE-READY", "sha": "0123456", "findings": []}]
            )
            graph["nodes"] = [
                {"id": "WIKI-173", "kind": "implement"},
                {"id": "WIKI-173-REVIEW1", "kind": "review"},
            ]
            graph["edges"][1]["payload"] = {}
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {
                    "pr": "https://github.com/hwang2409/wiki/pull/173",
                    "sha": "0123456",
                },
                graph_loader=lambda _ticket: graph,
                gate=lambda _number, _sha: {"verdict": "pass"},
                merge=lambda _ticket, _sha: None,
            )
            controller.enable("WIKI-173")
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-173-REVIEW1", "run_id": "r1", "status_state": "merge-ready"}
                )
            )
            self.assertEqual(
                controller.status("WIKI-173")["actions"][-1]["action"],
                "merged",
            )

    def test_parser_handles_five_archived_verdict_shapes(self) -> None:
        for text in ARCHIVED_VERDICTS:
            verdict = parse_verdict(text, source_sha="0123456")
            self.assertIsNotNone(verdict)
            assert verdict is not None
            self.assertEqual(len(verdict.findings), 1)
            finding = verdict.findings[0]
            self.assertNotEqual(finding.path, "unknown")
            self.assertIsNotNone(finding.line)
            self.assertNotEqual(finding.problem, "review finding")
            self.assertNotEqual(finding.fix, "address the finding")

    def test_steer_golden_output_and_action_sources(self) -> None:
        verdict = parse_verdict(
            """NOT-MERGE-READY: 3 findings

1. [HIGH] one.py:1 - first problem
   fix: first fix
2. [MEDIUM] two.py:2 - second problem
   fix: second fix
3. [LOW] three.py:3 - third problem
   fix: third fix
""",
            source_sha="0123456",
        )
        assert verdict is not None
        self.assertEqual(
            build_steer_message(verdict, target_worker="WIKI-173"),
            """autopilot: reviewer findings to address for WIKI-173.
1. [HIGH] one.py:1 (source sha: 0123456)
   problem: first problem
   fix: first fix
2. [MEDIUM] two.py:2 (source sha: 0123456)
   problem: second problem
   fix: second fix
3. [LOW] three.py:3 (source sha: 0123456)
   problem: third problem
   fix: third fix
do not declare merge-ready until every item is fixed and verified.""",
        )
        with tempfile.TemporaryDirectory() as directory:
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {"pr": "173", "sha": "0123456"},
                graph_loader=lambda _ticket: _graph(
                    [{"state": "NOT-MERGE-READY", "sha": "0123456", "findings": []}]
                ),
                steer=lambda _ticket, _message: None,
                archive=lambda _reviewer: None,
            )
            controller.enable("WIKI-173")
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-173-REVIEW1", "run_id": "r1", "status_state": "merge-ready"}
                )
            )
            self.assertTrue(controller.status("WIKI-173")["actions"])
            self.assertTrue(
                all(action.get("source") == "autopilot" for action in controller.status("WIKI-173")["actions"])
            )

    def test_concurrent_enable_and_disable_leave_disable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            class BlockingStore(AutopilotStore):
                entered = threading.Event()
                release = threading.Event()

                def save(self, ticket: str, state) -> None:  # type: ignore[no-untyped-def]
                    if state.enabled and not self.entered.is_set():
                        self.entered.set()
                        self.release.wait(timeout=2)
                    super().save(ticket, state)

            store = BlockingStore(Path(directory))
            controller = AutopilotController(store=store)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(controller.enable, "WIKI-173")
                self.assertTrue(store.entered.wait(timeout=2))
                second = pool.submit(controller.disable, "WIKI-173")
                store.release.set()
                first.result()
                second.result()
            self.assertFalse(controller.status("WIKI-173")["enabled"])


if __name__ == "__main__":
    unittest.main()
