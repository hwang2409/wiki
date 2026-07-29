from __future__ import annotations

import unittest
from typing import Any

from backend.app.agent_runtime.loop_state import (
    DANGER_DANGER,
    DANGER_NORMAL,
    DANGER_WARNING,
    derive_loop_state,
)


ORCH_ID = "wiki:main"
IMPL_ID = "WIKI-172"


def _spawn_review(round_index: int, *, iso: str, request_id: str) -> dict[str, Any]:
    reviewer = f"{IMPL_ID}-REVIEW{round_index}"
    return {
        "kind": "spawn",
        "from": ORCH_ID,
        "to": reviewer,
        "payload": {
            "ticket": reviewer,
            "role": "review",
            "model": "gpt-5.6-sol",
            "worktree": f"/tmp/{reviewer}",
            "request_id": request_id,
        },
        "created_at": iso,
    }


def _spawn_implementer(*, iso: str) -> dict[str, Any]:
    return {
        "kind": "spawn",
        "from": ORCH_ID,
        "to": IMPL_ID,
        "payload": {
            "ticket": IMPL_ID,
            "role": "implement",
            "model": "gpt-5.6-luna",
            "worktree": f"/tmp/{IMPL_ID}",
            "request_id": "spawn-impl",
        },
        "created_at": iso,
    }


def _verdict(
    round_index: int,
    *,
    iso: str,
    state: str = "NOT-MERGE-READY",
    title: str = "review found something wrong",
    finding_id: str = "F-abc123",
    sha: str = "0123456",
) -> dict[str, Any]:
    reviewer = f"{IMPL_ID}-REVIEW{round_index}"
    return {
        "kind": "verdict",
        "from": reviewer,
        "to": ORCH_ID,
        "payload": {
            "worker": reviewer,
            "sha": sha,
            "state": state,
            "created_at": iso,
            "findings": [
                {
                    "id": finding_id,
                    "severity": "BLOCKING",
                    "title": title,
                    "observed": "observed body",
                    "why_wrong": "why wrong body",
                    "do_instead": "do instead body",
                    "source_worker": reviewer,
                    "source_sha": sha,
                    "created_at": iso,
                }
            ],
        },
        "created_at": iso,
    }


def _steer(
    round_index: int,
    *,
    iso: str,
    text: str = "route the review",
) -> dict[str, Any]:
    reviewer = f"{IMPL_ID}-REVIEW{round_index}"
    return {
        "kind": "steer",
        "from": ORCH_ID,
        "to": IMPL_ID,
        "payload": {
            "target_worker": IMPL_ID,
            "source_worker": reviewer,
            "mode": "now",
            "text": text,
            "findings": [],
            "request_id": f"steer-{round_index}",
        },
        "created_at": iso,
    }


def _archive_reviewer(round_index: int, *, iso: str) -> dict[str, Any]:
    reviewer = f"{IMPL_ID}-REVIEW{round_index}"
    return {
        "kind": "archive",
        "from": ORCH_ID,
        "to": reviewer,
        "payload": {"outcome": "archived", "ended_at": iso},
        "created_at": iso,
    }


def _graph(*, edges: list[dict[str, Any]], iteration_cap: int | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "ticket": IMPL_ID,
        "orch": "wiki",
        "created_at": "2026-07-29T08:00:00Z",
        "updated_at": "2026-07-29T08:00:00Z",
        "nodes": [
            {"id": ORCH_ID, "kind": "orchestrator", "label": "wiki orch"},
            {"id": IMPL_ID, "kind": "implement", "label": IMPL_ID, "worker_id": IMPL_ID},
        ],
        "edges": edges,
    }
    if iteration_cap is not None:
        doc["iteration_cap"] = iteration_cap
    return doc


class DeriveLoopStateTest(unittest.TestCase):
    def test_zero_rounds(self) -> None:
        graph = _graph(edges=[_spawn_implementer(iso="2026-07-29T08:00:00Z")])
        state = derive_loop_state(graph, iteration_cap=8)
        self.assertEqual(state.round, 0)
        self.assertEqual(state.cap, 8)
        self.assertEqual(state.danger, DANGER_NORMAL)
        self.assertEqual(state.unrouted_verdict_count, 0)
        self.assertEqual(state.plateau_length, 0)
        self.assertIsNone(state.latest_verdict)
        self.assertEqual(state.history, [])

    def test_one_round_fresh_verdict_unrouted(self) -> None:
        edges = [
            _spawn_implementer(iso="2026-07-29T08:00:00Z"),
            _spawn_review(1, iso="2026-07-29T08:05:00Z", request_id="rev-1"),
            _verdict(1, iso="2026-07-29T08:15:00Z"),
        ]
        state = derive_loop_state(_graph(edges=edges, iteration_cap=8))
        self.assertEqual(state.round, 1)
        self.assertEqual(state.danger, DANGER_NORMAL)
        self.assertEqual(state.unrouted_verdict_count, 1)
        self.assertEqual(state.plateau_length, 1)
        assert state.latest_verdict is not None
        self.assertEqual(state.latest_verdict["state"], "NOT-MERGE-READY")
        self.assertIsNone(state.latest_verdict["routed_at"])
        self.assertEqual(state.latest_verdict["reviewer"], f"{IMPL_ID}-REVIEW1")
        self.assertEqual(state.latest_verdict["top_finding"]["severity"], "BLOCKING")
        self.assertEqual(len(state.history), 1)
        self.assertEqual(state.history[0].verdict_state, "NOT-MERGE-READY")
        self.assertIsNone(state.history[0].routed_at)

    def test_three_rounds_routed_but_plateau_holds(self) -> None:
        # Same finding title recurs across all three rounds — plateau = 3.
        title = "middleware still boots without the fix"
        edges = [_spawn_implementer(iso="2026-07-29T08:00:00Z")]
        for round_index in range(1, 4):
            base_hour = 8 + round_index  # 09:xx / 10:xx / 11:xx
            edges.extend([
                _spawn_review(
                    round_index,
                    iso=f"2026-07-29T{base_hour:02d}:00:00Z",
                    request_id=f"rev-{round_index}",
                ),
                _verdict(
                    round_index,
                    iso=f"2026-07-29T{base_hour:02d}:10:00Z",
                    title=title,
                    finding_id=f"F-plat{round_index}",
                ),
                _steer(round_index, iso=f"2026-07-29T{base_hour:02d}:12:00Z"),
                _archive_reviewer(round_index, iso=f"2026-07-29T{base_hour:02d}:15:00Z"),
            ])
        state = derive_loop_state(_graph(edges=edges, iteration_cap=8))
        self.assertEqual(state.round, 3)
        self.assertEqual(state.danger, DANGER_NORMAL)
        self.assertEqual(state.unrouted_verdict_count, 0)
        self.assertEqual(state.plateau_length, 3)
        assert state.latest_verdict is not None
        self.assertIsNotNone(state.latest_verdict["routed_at"])
        for entry in state.history:
            self.assertIsNotNone(entry.routed_at)
            self.assertIsNotNone(entry.archived_at)

    def test_plateau_breaks_when_signature_changes(self) -> None:
        edges = [
            _spawn_implementer(iso="2026-07-29T08:00:00Z"),
            _spawn_review(1, iso="2026-07-29T09:00:00Z", request_id="rev-1"),
            _verdict(1, iso="2026-07-29T09:10:00Z", title="first thing", finding_id="F-aaaaaa"),
            _steer(1, iso="2026-07-29T09:12:00Z"),
            _archive_reviewer(1, iso="2026-07-29T09:15:00Z"),
            _spawn_review(2, iso="2026-07-29T10:00:00Z", request_id="rev-2"),
            _verdict(2, iso="2026-07-29T10:10:00Z", title="different thing", finding_id="F-bbbbbb"),
        ]
        state = derive_loop_state(_graph(edges=edges, iteration_cap=8))
        self.assertEqual(state.plateau_length, 1)
        self.assertEqual(state.unrouted_verdict_count, 1)

    def test_warning_and_danger_tiers(self) -> None:
        # Six review spawns → warning tier.
        warning_edges = [_spawn_implementer(iso="2026-07-29T08:00:00Z")]
        for round_index in range(1, 7):
            warning_edges.append(
                _spawn_review(
                    round_index,
                    iso=f"2026-07-29T{8 + round_index:02d}:00:00Z",
                    request_id=f"rev-{round_index}",
                )
            )
        warn_state = derive_loop_state(_graph(edges=warning_edges, iteration_cap=8))
        self.assertEqual(warn_state.round, 6)
        self.assertEqual(warn_state.danger, DANGER_WARNING)

        # Add a seventh spawn → danger tier.
        danger_edges = warning_edges + [
            _spawn_review(7, iso="2026-07-29T15:00:00Z", request_id="rev-7")
        ]
        danger_state = derive_loop_state(_graph(edges=danger_edges, iteration_cap=8))
        self.assertEqual(danger_state.round, 7)
        self.assertEqual(danger_state.danger, DANGER_DANGER)

    def test_iteration_cap_at_or_over(self) -> None:
        edges = [_spawn_implementer(iso="2026-07-29T08:00:00Z")]
        for round_index in range(1, 9):
            edges.append(
                _spawn_review(
                    round_index,
                    iso=f"2026-07-29T{8 + round_index:02d}:00:00Z",
                    request_id=f"rev-{round_index}",
                )
            )
        state = derive_loop_state(_graph(edges=edges, iteration_cap=8))
        self.assertEqual(state.round, 8)
        self.assertEqual(state.cap, 8)
        self.assertEqual(state.danger, DANGER_DANGER)

    def test_merge_ready_verdict_not_counted_unrouted(self) -> None:
        edges = [
            _spawn_implementer(iso="2026-07-29T08:00:00Z"),
            _spawn_review(1, iso="2026-07-29T09:00:00Z", request_id="rev-1"),
            _verdict(
                1,
                iso="2026-07-29T09:10:00Z",
                state="MERGE-READY",
                title="ok",
                finding_id="F-mrgok0",
            ),
        ]
        state = derive_loop_state(_graph(edges=edges, iteration_cap=8))
        self.assertEqual(state.unrouted_verdict_count, 0)
        assert state.latest_verdict is not None
        self.assertEqual(state.latest_verdict["state"], "MERGE-READY")

    def test_cap_from_graph_explicit(self) -> None:
        edges = [_spawn_implementer(iso="2026-07-29T08:00:00Z")]
        state = derive_loop_state(_graph(edges=edges, iteration_cap=3))
        # No explicit iteration_cap kwarg — must fall back to the graph field.
        self.assertEqual(state.cap, 3)


if __name__ == "__main__":
    unittest.main()
