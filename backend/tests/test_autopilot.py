from __future__ import annotations

import asyncio
import json
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.app.agent_runtime.autopilot import (
    AutopilotController as RealAutopilotController,
    AutopilotStore,
    Finding,
    Verdict,
    build_steer_message,
    parse_verdict,
    verdict_from_graph,
)


PR_URL = "https://github.com/hwang2409/wiki/pull/173"


class HarnessAutopilotController(RealAutopilotController):
    """Keep controller notifications inside the test harness."""

    def __init__(self, *args, **kwargs):
        self.notifications: list[tuple[str, str]] = []
        kwargs.setdefault(
            "notify",
            lambda orch, message: self.notifications.append((orch, message)),
        )
        # Stub the registry too: without this, _orchestrator() falls back to the
        # LIVE agent registry, so halt-notification tests silently depend on
        # whether the ticket happens to be registered on the host machine.
        kwargs.setdefault(
            "registry_reader",
            lambda: {"WIKI-173": {"current": {"orch": "wiki"}}},
        )
        super().__init__(*args, **kwargs)


AutopilotController = HarnessAutopilotController


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

    def test_json_fallback_for_nonstandard_output_cannot_authorize_merge(self) -> None:
        verdict = parse_verdict(
            "review output omitted the normal header",
            fallback=lambda _text: {"state": "MERGE-READY", "findings": []},
        )
        self.assertIsNone(verdict)

    def test_json_severity_aliases_persist_before_actions(self) -> None:
        for alias, canonical in {
            "CRITICAL": "BLOCKING",
            "MAJOR": "HIGH",
            "MINOR": "MEDIUM",
        }.items():
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as directory:
                artifact = Path(directory) / "reviewer-output.json"
                artifact.write_text(
                    json.dumps(
                        {
                            "state": "NOT-MERGE-READY",
                            "sha": "0123456",
                            "findings": [
                                {
                                    "severity": alias,
                                    "file": "backend/app/main.py",
                                    "line": 1,
                                    "observed": "unsafe",
                                    "do_instead": "fix it",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                graph = _graph([])
                graph["edges"] = [
                    {
                        "kind": "spawn",
                        "to": "WIKI-173-REVIEW1",
                        "payload": {"role": "review"},
                    }
                ]
                actions: list[object] = []

                def record_verdict(_ticket, reviewer, verdict, _request_id):
                    actions.append(("persist", verdict.findings[0].severity))
                    graph["edges"].append(
                        {
                            "kind": "verdict",
                            "from": reviewer,
                            "payload": {
                                "worker": reviewer,
                                "state": verdict.state,
                                "sha": verdict.source_sha,
                                "findings": [
                                    finding.to_dict() for finding in verdict.findings
                                ],
                            },
                        }
                    )
                    return True

                controller = AutopilotController(
                    store=AutopilotStore(Path(directory) / "state"),
                    status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                    graph_loader=lambda _ticket: graph,
                    record_verdict=record_verdict,
                    steer=lambda _ticket, _message: actions.append("steer"),
                    archive=lambda _reviewer: actions.append("archive"),
                )
                controller.enable("WIKI-173")
                self.assertTrue(
                    asyncio.run(
                        controller.on_transition(
                            {
                                "agent_id": "WIKI-173-REVIEW1",
                                "run_id": "json-alias",
                                "status_state": "merge-ready",
                                "status_mtime": 1,
                                "verdict_path": str(artifact),
                            }
                        )
                    )
                )
                self.assertEqual(
                    actions,
                    [("persist", canonical), "steer", "archive"],
                )

    def test_text_severity_aliases_persist_before_actions(self) -> None:
        for alias, canonical in {
            "CRITICAL": "BLOCKING",
            "MAJOR": "HIGH",
            "MINOR": "MEDIUM",
        }.items():
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as directory:
                artifact = Path(directory) / "reviewer-output.txt"
                artifact.write_text(
                    f"""NOT-MERGE-READY: 1 finding

source sha: 0123456

1. [{alias}] backend/app/main.py:1 - unsafe
   fix: fix it
""",
                    encoding="utf-8",
                )
                graph = _graph([])
                graph["edges"] = [
                    {
                        "kind": "spawn",
                        "to": "WIKI-173-REVIEW1",
                        "payload": {"role": "review"},
                    }
                ]
                actions: list[object] = []

                def record_verdict(_ticket, reviewer, verdict, _request_id):
                    actions.append(("persist", verdict.findings[0].severity))
                    graph["edges"].append(
                        {
                            "kind": "verdict",
                            "from": reviewer,
                            "payload": {
                                "worker": reviewer,
                                "state": verdict.state,
                                "sha": verdict.source_sha,
                                "findings": [
                                    finding.to_dict() for finding in verdict.findings
                                ],
                            },
                        }
                    )
                    return True

                controller = AutopilotController(
                    store=AutopilotStore(Path(directory) / "state"),
                    status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                    graph_loader=lambda _ticket: graph,
                    record_verdict=record_verdict,
                    steer=lambda _ticket, _message: actions.append("steer"),
                    archive=lambda _reviewer: actions.append("archive"),
                )
                controller.enable("WIKI-173")
                self.assertTrue(
                    asyncio.run(
                        controller.on_transition(
                            {
                                "agent_id": "WIKI-173-REVIEW1",
                                "run_id": "text-alias",
                                "status_state": "merge-ready",
                                "status_mtime": 1,
                                "verdict_path": str(artifact),
                            }
                        )
                    )
                )
                self.assertEqual(
                    actions,
                    [("persist", canonical), "steer", "archive"],
                )

    def test_parser_rejects_echoed_conflicting_verdict_and_findings_on_clean(self) -> None:
        self.assertIsNone(
            parse_verdict(
                """VERDICT: MERGE-READY

review prompt echo:
VERDICT: NOT-MERGE-READY: 1 finding
- [BLOCKING] backend/app/main.py:1 - unsafe merge
  fix: stop the merge
""",
                source_sha="0123456",
            )
        )
        verdict = parse_verdict(
            """MERGE-READY
- [HIGH] backend/app/main.py:1 - hidden finding
  fix: expose the finding
""",
            source_sha="0123456",
        )
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertFalse(verdict.clean)

    def test_parser_rejects_duplicate_same_state_header_with_hidden_finding(self) -> None:
        self.assertIsNone(
            parse_verdict(
                """MERGE-READY

quoted answer:
MERGE-READY
- [HIGH] backend/app/main.py:1 - hidden finding
  fix: expose the finding
""",
                source_sha="0123456",
            )
        )

    def test_maybe_merge_blocks_nonclean_verdict_inside_merge_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate_calls: list[str] = []
            merged: list[str] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                gate=lambda pr, _sha: gate_calls.append(pr) or {"verdict": "pass", "pr": pr},
                merge=lambda pr, _sha: merged.append(pr),
            )
            controller.enable("WIKI-173")
            state = controller.store.load("WIKI-173")
            verdict = Verdict(
                state="MERGE-READY",
                source_sha="0123456",
                findings=(
                    Finding(
                        severity="BLOCKING",
                        path="backend/app/main.py",
                        line=1,
                        problem="unsafe merge",
                        fix="stop the merge",
                    ),
                ),
            )
            self.assertTrue(asyncio.run(controller._maybe_merge("WIKI-173", state, verdict)))
            self.assertEqual(gate_calls, [])
            self.assertEqual(merged, [])

    def test_unknown_repository_is_blocked_by_autopilot_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate_calls: list[str] = []
            merged: list[str] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {
                    "pr": "https://github.com/example/other/pull/173",
                    "sha": "0123456",
                },
                gate=lambda pr, _sha: gate_calls.append(pr) or {"verdict": "pass", "pr": pr},
                merge=lambda pr, _sha: merged.append(pr),
            )
            controller.enable("WIKI-173")
            state = controller.store.load("WIKI-173")
            verdict = Verdict(state="MERGE-READY", source_sha="0123456")
            self.assertTrue(asyncio.run(controller._maybe_merge("WIKI-173", state, verdict)))
            self.assertEqual(gate_calls, [])
            self.assertEqual(merged, [])

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
                gate=lambda pr, _sha: {"verdict": "pass", "pr": pr},
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
            self.assertEqual(merged, [PR_URL])

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
                    gate=lambda pr, _sha, gate_result=gate_result: {
                        **gate_result,
                        "pr": pr,
                    },
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

    def test_gate_and_merge_share_exact_pr_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            merged: list[str] = []
            gate_urls: list[str] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                graph_loader=lambda _ticket: _graph(
                    [{"state": "MERGE-READY", "sha": "0123456", "findings": []}]
                ),
                gate=lambda pr, _sha: (
                    gate_urls.append(pr)
                    or {"verdict": "pass", "pr": "https://github.com/hwang2409/wiki/pull/999"}
                ),
                merge=lambda pr, _sha: merged.append(pr),
            )
            controller.enable("WIKI-173")
            self.assertTrue(
                asyncio.run(
                    controller.on_transition(
                        {"agent_id": "WIKI-173-REVIEW1", "run_id": "r1", "status_state": "merge-ready"}
                    )
                )
            )
            self.assertEqual(gate_urls, [PR_URL])
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
            self.assertEqual(len(controller.notifications), 1)
            self.assertIn("autopilot halted WIKI-173: iteration-cap", controller.notifications[0][1])

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
                gate=lambda pr, _sha: {"verdict": "pass", "pr": pr},
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
            self.assertEqual(merged, [(PR_URL, "0123456")])

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
                gate=lambda pr, _sha: {"verdict": "pass", "pr": pr},
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

    def test_transition_retries_until_durable_verdict_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph([])
            graph["edges"].append(
                {"kind": "spawn", "to": "WIKI-173-REVIEW1", "payload": {"role": "review"}}
            )
            steers: list[str] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                graph_loader=lambda _ticket: graph,
                steer=lambda _ticket, message: steers.append(message),
                archive=lambda _reviewer: None,
            )
            controller.enable("WIKI-173")
            event = {
                "agent_id": "WIKI-173-REVIEW1",
                "run_id": "r1",
                "status_state": "merge-ready",
                "status_mtime": 1,
            }
            self.assertFalse(asyncio.run(controller.on_transition(event)))
            self.assertIsNone(controller.status("WIKI-173")["last_event_key"])
            graph["edges"].append(
                {
                    "kind": "verdict",
                    "from": "WIKI-173-REVIEW1",
                    "payload": {
                        "worker": "WIKI-173-REVIEW1",
                        "state": "NOT-MERGE-READY",
                        "sha": "0123456",
                        "findings": [
                            {
                                "id": "F-abc123",
                                "severity": "HIGH",
                                "title": "same finding",
                                "observed": "bad",
                                "why_wrong": "wrong",
                                "do_instead": "fix",
                                "source_worker": "WIKI-173-REVIEW1",
                                "source_sha": "0123456",
                            }
                        ],
                    },
                }
            )
            self.assertTrue(asyncio.run(controller.on_transition(event)))
            self.assertEqual(len(steers), 1)
            self.assertIsNotNone(controller.status("WIKI-173")["last_event_key"])

    def test_reviewer_artifact_flows_through_parser_and_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "reviewer-output.txt"
            artifact.write_text(
                """NOT-MERGE-READY: 1 finding

source sha: 0123456

1. [BLOCKING] backend/app/main.py:1 - unsafe merge
   fix: stop the merge
""",
                encoding="utf-8",
            )
            graph = _graph([])
            graph["edges"] = [
                {"kind": "spawn", "to": "WIKI-173-REVIEW1", "payload": {"role": "review"}}
            ]
            steers: list[str] = []
            archived: list[str] = []
            persisted: list[tuple[str, str]] = []

            def record_verdict(_ticket, reviewer, verdict, _request_id):
                persisted.append((reviewer, verdict.source_sha or ""))
                graph["edges"].append(
                    {
                        "kind": "verdict",
                        "from": reviewer,
                        "payload": {
                            "worker": reviewer,
                            "state": verdict.state,
                            "sha": verdict.source_sha,
                            "findings": [finding.to_dict() for finding in verdict.findings],
                        },
                    }
                )
                return True
            controller = AutopilotController(
                store=AutopilotStore(Path(directory) / "state"),
                status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                graph_loader=lambda _ticket: graph,
                record_verdict=record_verdict,
                steer=lambda _ticket, message: steers.append(message),
                archive=lambda reviewer: archived.append(reviewer),
            )
            controller.enable("WIKI-173")
            event = {
                "agent_id": "WIKI-173-REVIEW1",
                "run_id": "r1",
                "status_state": "merge-ready",
                "status_mtime": 1,
                "verdict_path": str(artifact),
            }
            self.assertTrue(asyncio.run(controller.on_transition(event)))
            self.assertEqual(persisted, [("WIKI-173-REVIEW1", "0123456")])
            self.assertEqual(len(steers), 1)
            self.assertEqual(archived, ["WIKI-173-REVIEW1"])
            self.assertIsNotNone(controller.status("WIKI-173")["last_event_key"])

    def test_artifact_without_sha_is_invalid_and_cannot_merge_or_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "reviewer-output.json"
            artifact.write_text(
                '{"state":"MERGE-READY","findings":[]}',
                encoding="utf-8",
            )
            graph = _graph([])
            graph["edges"] = [
                {"kind": "spawn", "to": "WIKI-173-REVIEW1", "payload": {"role": "review"}}
            ]
            persisted: list[str] = []
            merged: list[str] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory) / "state"),
                status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                graph_loader=lambda _ticket: graph,
                record_verdict=lambda *_args: persisted.append("persisted"),
                gate=lambda pr, _sha: {"verdict": "pass", "pr": pr},
                merge=lambda pr, _sha: merged.append(pr),
            )
            controller.enable("WIKI-173")
            result = asyncio.run(
                controller.on_transition(
                    {
                        "agent_id": "WIKI-173-REVIEW1",
                        "run_id": "r1",
                        "status_state": "merge-ready",
                        "status_mtime": 1,
                        "verdict_path": str(artifact),
                    }
                )
            )
            self.assertTrue(result)
            self.assertEqual(persisted, [])
            self.assertEqual(merged, [])

    def test_steer_and_archive_stages_resume_without_duplicate_steer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(
                [
                    {
                        "state": "NOT-MERGE-READY",
                        "sha": "0123456",
                        "findings": [
                            {
                                "id": "F-retry1",
                                "severity": "HIGH",
                                "path": "backend/app/main.py",
                                "line": 1,
                                "problem": "bad",
                                "fix": "fix",
                            }
                        ],
                    }
                ]
            )
            steers: list[str] = []
            archives: list[str] = []

            def archive(reviewer: str) -> None:
                archives.append(reviewer)
                if len(archives) == 1:
                    raise RuntimeError("archive unavailable")

            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                graph_loader=lambda _ticket: graph,
                steer=lambda _ticket, message: steers.append(message),
                archive=archive,
            )
            controller.enable("WIKI-173")
            event = {
                "agent_id": "WIKI-173-REVIEW1",
                "run_id": "r1",
                "status_state": "merge-ready",
                "status_mtime": 1,
            }
            self.assertFalse(asyncio.run(controller.on_transition(event)))
            self.assertTrue(asyncio.run(controller.on_transition(event)))
            self.assertEqual(len(steers), 1)
            self.assertEqual(archives, ["WIKI-173-REVIEW1", "WIKI-173-REVIEW1"])

    def test_enable_reconciles_existing_merge_ready_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            merged: list[str] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {
                    "state": "merge-ready",
                    "pr": PR_URL,
                    "sha": "0123456",
                },
                graph_loader=lambda _ticket: _graph(
                    [{"state": "MERGE-READY", "sha": "0123456", "findings": []}]
                ),
                gate=lambda pr, _sha: {"verdict": "pass", "pr": pr},
                merge=lambda pr, _sha: merged.append(pr),
            )
            controller.enable("WIKI-173")
            self.assertEqual(merged, [PR_URL])

    def test_enable_reconcile_routes_dirty_state_to_next_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spawned: list[dict] = []
            steers: list[str] = []
            graph = _graph(
                [
                    {
                        "state": "NOT-MERGE-READY",
                        "sha": "0123456",
                        "findings": [
                            {
                                "id": "F-abc123",
                                "severity": "HIGH",
                                "title": "stale finding",
                                "observed": "bad",
                                "why_wrong": "wrong",
                                "do_instead": "fix",
                            }
                        ],
                    }
                ]
            )
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {
                    "state": "merge-ready",
                    "pr": PR_URL,
                    "sha": "0123456",
                },
                graph_loader=lambda _ticket: graph,
                next_review=lambda **kwargs: spawned.append(kwargs) or {"status": "spawned"},
                steer=lambda _ticket, message: steers.append(message),
                registry_reader=lambda: {"WIKI-173": {"current": {"orch": "wiki"}}},
            )
            controller.enable("WIKI-173")
            self.assertEqual(len(spawned), 1)
            self.assertEqual(steers, [])

    def test_transient_gate_failure_retries_same_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate_results = iter(
                [
                    {"verdict": "fail", "pr": PR_URL},
                    {"verdict": "pass", "pr": PR_URL},
                ]
            )
            merged: list[str] = []
            controller = AutopilotController(
                store=AutopilotStore(Path(directory)),
                status_reader=lambda _ticket: {"pr": PR_URL, "sha": "0123456"},
                graph_loader=lambda _ticket: _graph(
                    [{"state": "MERGE-READY", "sha": "0123456", "findings": []}]
                ),
                gate=lambda _pr, _sha: next(gate_results),
                merge=lambda pr, _sha: merged.append(pr),
            )
            controller.enable("WIKI-173")
            event = {"agent_id": "WIKI-173-REVIEW1", "run_id": "r1", "status_state": "merge-ready"}
            self.assertFalse(asyncio.run(controller.on_transition(event)))
            self.assertTrue(asyncio.run(controller.on_transition(event)))
            self.assertEqual(merged, [PR_URL])

    def test_canonical_finding_fields_survive_steer_conversion(self) -> None:
        verdict = verdict_from_graph(
            {
                "worker": "WIKI-173-REVIEW1",
                "state": "NOT-MERGE-READY",
                "sha": "0123456",
                "findings": [
                    {
                        "id": "F-abc123",
                        "severity": "HIGH",
                        "title": "title",
                        "file": "backend/app/main.py",
                        "line": 42,
                        "observed": "observed",
                        "why_wrong": "why wrong",
                        "do_instead": "do this",
                        "constraint": "keep the gate pinned",
                        "source_worker": "WIKI-173-REVIEW1",
                        "source_sha": "0123456",
                    }
                ],
            }
        )
        assert verdict is not None
        steer_finding = verdict.findings[0].to_steer_dict(
            source_worker="WIKI-173-REVIEW1",
            source_sha=verdict.source_sha,
            created_at="2026-07-30T01:00:00+00:00",
        )
        self.assertEqual(steer_finding["id"], "F-abc123")
        self.assertEqual(steer_finding["why_wrong"], "why wrong")
        self.assertEqual(steer_finding["constraint"], "keep the gate pinned")
        self.assertEqual(steer_finding["source_worker"], "WIKI-173-REVIEW1")
        message = build_steer_message(verdict, target_worker="WIKI-173")
        for field in ("why wrong", "constraint", "source worker", "finding id"):
            self.assertIn(field, message)

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
                gate=lambda pr, _sha: {"verdict": "pass", "pr": pr},
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
