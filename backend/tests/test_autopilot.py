from __future__ import annotations

import asyncio
import tempfile
import unittest
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
                merge=lambda _ticket: None,
            )
            merged: list[str] = []
            controller.merge = lambda ticket: merged.append(ticket)
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
                    merge=lambda ticket: merged.append(ticket),
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


if __name__ == "__main__":
    unittest.main()
