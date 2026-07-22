"""WIKI-163: workgraph writer, composite health, and serving endpoints."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from backend.app import main, workgraph

BASE_TS = 1_784_700_000.0


def finding(fid: str = "F-abc123", severity: str = "BLOCKING", resolved_by: str | None = None):
    return {
        "id": fid,
        "severity": severity,
        "title": "stale cache read",
        "observed": "cache returns old rows",
        "why_wrong": "misses invalidation",
        "do_instead": "bust key on write",
        "source_worker": "TST-1-REVIEW1",
        "source_sha": "abc1234",
        "resolved_by": resolved_by,
        "created_at": "2026-07-22T12:00:00Z",
    }


def spawn_payload(ticket: str = "TST-1", role: str = "implement"):
    return {
        "ticket": ticket,
        "role": role,
        "model": "gpt-5.6-luna",
        "effort": "high",
        "worktree": "/tmp/wt",
        "request_id": f"req-{ticket}",
    }


def verdict_payload(state: str = "NOT-MERGE-READY", findings: list | None = None):
    return {
        "worker": "TST-1-REVIEW1",
        "sha": "abc1234",
        "state": state,
        "findings": [finding()] if findings is None else findings,
        "created_at": "2026-07-22T12:00:00Z",
    }


def steer_payload():
    return {
        "target_worker": "TST-1",
        "mode": "now",
        "findings": [finding()],
        "created_at": "2026-07-22T12:01:00Z",
    }


def archive_payload():
    return {"outcome": "merged", "ended_at": "2026-07-22T13:00:00Z"}


class WorkgraphWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def append(self, kind: str, from_node: str, to_node: str, payload: dict, ts: float, **kwargs):
        return workgraph.append_edge(
            "TST-1",
            kind,
            from_node,
            to_node,
            payload,
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=ts,
            **kwargs,
        )

    def scripted_sequence(self) -> dict:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.append("spawn", "N-1", "N-3", spawn_payload("TST-1-REVIEW1", "review"), BASE_TS + 60)
        self.append("verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 120)
        self.append("steer", "N-1", "N-2", steer_payload(), BASE_TS + 180)
        return self.append("archive", "N-1", "N-3", archive_payload(), BASE_TS + 240)

    def test_scripted_sequence_produces_valid_workgraph(self) -> None:
        graph = self.scripted_sequence()
        self.assertEqual(graph["ticket"], "TST-1")
        self.assertEqual(graph["orch"], "wiki")
        self.assertEqual([n["id"] for n in graph["nodes"]], ["N-1", "N-2", "N-3"])
        self.assertEqual([n["kind"] for n in graph["nodes"]], ["orchestrator", "implement", "review"])
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn", "spawn", "verdict", "steer", "archive"])

        on_disk = json.loads((self.status_dir / "TST-1.workgraph.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["edges"], graph["edges"])
        self.assertEqual(validate_against_workgraph_schema(on_disk), [])

    def test_snapshot_written_for_meaningful_appends_only(self) -> None:
        self.scripted_sequence()
        snapshots = sorted(self.snapshot_dir.glob("TST-1-*.workgraph.json"))
        # spawn, spawn, verdict, archive — steer stays hot-only.
        self.assertEqual(len(snapshots), 4)
        newest = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        self.assertEqual(newest, snapshots[-1])

    def test_no_temp_file_leftovers(self) -> None:
        self.scripted_sequence()
        self.assertEqual(list(self.status_dir.glob("*.workgraph-tmp")), [])
        self.assertEqual(list(self.snapshot_dir.glob("*.workgraph-tmp")), [])

    def test_bad_payload_rejected_and_graph_untouched(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        before = (self.status_dir / "TST-1.workgraph.json").read_text(encoding="utf-8")
        with self.assertRaises(workgraph.WorkgraphError):
            self.append("verdict", "N-3", "N-1", {"worker": "x"}, BASE_TS + 60)
        after = (self.status_dir / "TST-1.workgraph.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_bad_finding_inside_verdict_rejected(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        bad = verdict_payload(findings=[{**finding(), "severity": "SEVERE"}])
        with self.assertRaises(workgraph.WorkgraphError) as ctx:
            self.append("verdict", "N-3", "N-1", bad, BASE_TS + 60)
        self.assertIn("severity", str(ctx.exception))

    def test_unknown_edge_kind_rejected(self) -> None:
        with self.assertRaises(workgraph.WorkgraphError):
            self.append("merge", "N-1", "N-2", {}, BASE_TS, orch="wiki")

    def test_new_graph_requires_orch(self) -> None:
        with self.assertRaises(workgraph.WorkgraphError) as ctx:
            self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS)
        self.assertIn("--orch", str(ctx.exception))

    def test_composite_health_iterating_with_blocking(self) -> None:
        graph = self.scripted_sequence()
        health = graph["composite_health"]
        self.assertEqual(health["state"], "iterating")
        self.assertEqual(health["open_findings"], 1)
        self.assertEqual(health["blocking"], 1)
        self.assertEqual(health["iteration_count"], 1)

    def test_composite_health_merge_ready_after_resolution(self) -> None:
        self.scripted_sequence()
        resolved = verdict_payload("MERGE-READY", findings=[finding(resolved_by="def5678")])
        graph = self.append("verdict", "N-3", "N-1", resolved, BASE_TS + 300)
        health = graph["composite_health"]
        self.assertEqual(health["state"], "merge-ready")
        self.assertEqual(health["open_findings"], 0)
        self.assertEqual(health["blocking"], 0)
        self.assertEqual(health["iteration_count"], 2)

    def test_composite_health_archived_when_no_live_workers(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        graph = self.append("archive", "N-1", "N-2", archive_payload(), BASE_TS + 60)
        self.assertEqual(graph["composite_health"]["state"], "archived")
        self.assertEqual(graph["composite_health"]["slowest_node_stall_seconds"], 0)

    def test_stall_measures_oldest_live_node_activity(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.append("spawn", "N-1", "N-3", spawn_payload("TST-1-REVIEW1", "review"), BASE_TS + 2000)
        graph = workgraph.load_workgraph("TST-1", self.status_dir)
        health = workgraph.compute_composite_health(graph, now_ts=BASE_TS + 2100)
        self.assertEqual(health["slowest_node_stall_seconds"], 2100)

    def test_health_alarm_blocking_without_live_reviewer(self) -> None:
        graph = self.scripted_sequence()  # archive removes the only reviewer
        alarms = workgraph.health_alarms(graph, now_ts=BASE_TS + 250)
        self.assertEqual([a["check"] for a in alarms], ["blocking_no_reviewer"])

    def test_health_alarm_absent_while_reviewer_live(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.append("spawn", "N-1", "N-3", spawn_payload("TST-1-REVIEW1", "review"), BASE_TS + 60)
        graph = self.append("verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 120)
        alarms = workgraph.health_alarms(graph, now_ts=BASE_TS + 130)
        self.assertEqual(alarms, [])

    def test_health_alarm_stall(self) -> None:
        graph = self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        alarms = workgraph.health_alarms(graph, now_ts=BASE_TS + 2000)
        self.assertEqual([a["check"] for a in alarms], ["node_stall"])

    def test_health_alarm_iteration_cap(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.append("spawn", "N-1", "N-3", spawn_payload("TST-1-REVIEW1", "review"), BASE_TS + 1)
        graph = None
        clean = verdict_payload("NOT-MERGE-READY", findings=[])
        for round_index in range(3):
            graph = self.append("verdict", "N-3", "N-1", clean, BASE_TS + 2 + round_index)
        alarms = workgraph.health_alarms(graph, iteration_cap=2, now_ts=BASE_TS + 10)
        self.assertEqual([a["check"] for a in alarms], ["iteration_cap"])


def validate_against_workgraph_schema(graph: dict) -> list[str]:
    return workgraph.validate_instance(graph, workgraph.load_schema("workgraph"))


class WorkgraphEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"
        self.status_dir.mkdir(parents=True)
        self.snapshot_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build_graph(self) -> dict:
        return workgraph.append_edge(
            "TST-9",
            "spawn",
            "N-1",
            "N-2",
            spawn_payload("TST-9"),
            orch="wiki",
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS,
        )

    def test_serves_live_workgraph(self) -> None:
        self.build_graph()
        with (
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
        ):
            payload = main.agent_workgraph("TST-9")
        self.assertEqual(payload["source"], "live")
        self.assertEqual(payload["workgraph"]["ticket"], "TST-9")

    def test_falls_back_to_newest_snapshot(self) -> None:
        self.build_graph()
        (self.status_dir / "TST-9.workgraph.json").unlink()
        with (
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
        ):
            payload = main.agent_workgraph("TST-9")
        self.assertEqual(payload["source"], "snapshot")
        self.assertEqual(payload["workgraph"]["ticket"], "TST-9")

    def test_missing_workgraph_is_404(self) -> None:
        with (
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.agent_workgraph("TST-9")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_health_endpoint_reports_alarms(self) -> None:
        workgraph.append_edge(
            "TST-9",
            "spawn",
            "N-1",
            "N-2",
            spawn_payload("TST-9"),
            orch="wiki",
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS,
        )
        workgraph.append_edge(
            "TST-9",
            "verdict",
            "N-2",
            "N-1",
            verdict_payload(),
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS + 60,
        )
        with (
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
        ):
            payload = main.agent_workgraph_health("TST-9")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["health"]["blocking"], 1)
        checks = [a["check"] for a in payload["alarms"]]
        self.assertIn("blocking_no_reviewer", checks)


if __name__ == "__main__":
    unittest.main()
