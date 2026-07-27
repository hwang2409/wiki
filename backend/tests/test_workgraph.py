"""WIKI-163: workgraph writer, composite health, and serving endpoints."""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from backend.app import main, workgraph
from wiki_cli import graph_lint

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
            # Distinct sha per round: identical repeated payloads would be
            # collapsed by the retry-replay guard, as a real re-review never is.
            round_payload = {**clean, "sha": f"abc123{round_index}"}
            graph = self.append("verdict", "N-3", "N-1", round_payload, BASE_TS + 2 + round_index)
        alarms = workgraph.health_alarms(graph, iteration_cap=2, now_ts=BASE_TS + 10)
        self.assertEqual([a["check"] for a in alarms], ["iteration_cap"])


def validate_against_workgraph_schema(graph: dict) -> list:
    return graph_lint.validate_document(graph, "workgraph")


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


def handoff_payload():
    return {
        "from_session": "sess-a",
        "to_session": "sess-b",
        "worktree": "/tmp/wt",
        "pr_url": None,
        "remaining_summary": "finish the tests",
    }


def monitor_alarm_payload():
    return {"alarm_kind": "node_stall", "message": "stalled 2000s", "watchlist_row": "TST-1"}


def capability_grant_payload():
    return {"capability": "review", "granted_by": "orch:wiki"}


def escalation_payload():
    return {"reason": "iteration cap", "prior_findings": [], "target": "henry"}


class CommitOrderingTests(unittest.TestCase):
    """Round 2: snapshot must commit before the hot pointer (items 2/6)."""

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

    def hot(self) -> Path:
        return self.status_dir / "TST-1.workgraph.json"

    def assert_no_temp_files(self) -> None:
        self.assertEqual(list(self.status_dir.glob("*.workgraph-tmp")), [])
        if self.snapshot_dir.is_dir():
            self.assertEqual(list(self.snapshot_dir.glob("*.workgraph-tmp")), [])

    def test_snapshot_renamed_before_hot(self) -> None:
        original_rename = Path.rename
        commits: list[Path] = []

        def tracking(path_self: Path, target):
            commits.append(Path(target))
            return original_rename(path_self, target)

        with mock.patch.object(Path, "rename", tracking):
            self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        targets = [c for c in commits if c.name.endswith(".workgraph.json")]
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].parent, self.snapshot_dir)
        self.assertEqual(targets[1], self.hot())

    def test_hot_commit_failure_keeps_old_hot_and_new_snapshot(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        before = self.hot().read_bytes()
        original_rename = Path.rename

        def failing(path_self: Path, target):
            if Path(target) == self.hot():
                raise OSError("disk full")
            return original_rename(path_self, target)

        with mock.patch.object(Path, "rename", failing):
            with self.assertRaises(workgraph.WorkgraphError):
                self.append("verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 60)

        # Hot pointer is byte-identical to the pre-append state; the durable
        # snapshot committed first and carries the new edge.
        self.assertEqual(self.hot().read_bytes(), before)
        newest = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        snapshot = json.loads(newest.read_text(encoding="utf-8"))
        self.assertEqual([e["kind"] for e in snapshot["edges"]], ["spawn", "verdict"])
        self.assert_no_temp_files()

        # Retry is safe: the edge lands exactly once in the hot graph.
        graph = self.append("verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 120)
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn", "verdict"])
        on_disk = json.loads(self.hot().read_text(encoding="utf-8"))
        self.assertEqual(sum(1 for e in on_disk["edges"] if e["kind"] == "verdict"), 1)

    def test_snapshot_commit_failure_commits_nothing(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        before = self.hot().read_bytes()
        original_rename = Path.rename

        def failing(path_self: Path, target):
            if Path(target).parent == self.snapshot_dir:
                raise OSError("disk full")
            return original_rename(path_self, target)

        with mock.patch.object(Path, "rename", failing):
            with self.assertRaises(workgraph.WorkgraphError):
                self.append("verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 60)

        self.assertEqual(self.hot().read_bytes(), before)
        snapshots = list(self.snapshot_dir.glob("TST-1-*.workgraph.json"))
        self.assertEqual(len(snapshots), 1)  # only the original spawn snapshot
        self.assert_no_temp_files()

    def test_stage_failure_wrapped_and_cleaned(self) -> None:
        self.append("steer", "N-1", "N-2", steer_payload(), BASE_TS, orch="wiki")
        before = self.hot().read_bytes()
        blocked = self.snapshot_dir.parent / "not-a-dir"
        blocked.write_text("file blocks mkdir", encoding="utf-8")
        with self.assertRaises(workgraph.WorkgraphError):
            workgraph.append_edge(
                "TST-1",
                "verdict",
                "N-3",
                "N-1",
                verdict_payload(),
                status_dir=self.status_dir,
                snapshot_dir=blocked / "workgraphs",
                now_ts=BASE_TS + 60,
            )
        self.assertEqual(self.hot().read_bytes(), before)
        self.assert_no_temp_files()

    def test_retry_replay_of_newest_edge_is_noop(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        before = self.hot().read_bytes()
        graph = self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS + 60)
        self.assertTrue(graph.get("_replayed"))
        self.assertIsNone(graph.get("_snapshot_path"))
        self.assertEqual(self.hot().read_bytes(), before)
        self.assertEqual(len(list(self.snapshot_dir.glob("TST-1-*.workgraph.json"))), 1)

    def crash_after_snapshot_rename(self, edge_kind: str, from_node: str, to_node: str,
                                    payload: dict, ts: float) -> None:
        """Simulate a process death between the snapshot and hot renames."""
        original_rename = Path.rename

        def crashing(path_self: Path, target):
            result = original_rename(path_self, target)
            if Path(target).parent == self.snapshot_dir:
                raise RuntimeError("simulated crash between snapshot and hot rename")
            return result

        with mock.patch.object(Path, "rename", crashing):
            with self.assertRaises(RuntimeError):
                self.append(edge_kind, from_node, to_node, payload, ts)

    def test_crash_between_renames_then_different_append_keeps_both_edges(self) -> None:
        # Round 5 HIGH: a crash after the snapshot rename leaves orphan
        # snapshot rN carrying edge A while hot lacks it. The next, different
        # append must promote the orphan under the lock BEFORE allocating
        # rN+1 — otherwise rN+1 commits without A and recovery silently
        # drops A from the durable history.
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.crash_after_snapshot_rename(
            "verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 60
        )

        # The crash window is real: hot lacks the verdict, the snapshot has it.
        hot_stale = json.loads(self.hot().read_text(encoding="utf-8"))
        self.assertEqual([e["kind"] for e in hot_stale["edges"]], ["spawn"])
        orphan = workgraph.load_snapshot("TST-1", self.snapshot_dir)
        self.assertEqual([e["kind"] for e in orphan["edges"]], ["spawn", "verdict"])

        # A DIFFERENT append (new snapshot revision) after the crash.
        self.append("archive", "N-1", "N-2", archive_payload(), BASE_TS + 120)

        expected = ["spawn", "verdict", "archive"]
        hot_after = json.loads(self.hot().read_text(encoding="utf-8"))
        self.assertEqual([e["kind"] for e in hot_after["edges"]], expected)
        selected = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        self.assertIn("-r3-", selected.name)  # promoted base, not a shadow of r2
        snapshot = json.loads(selected.read_text(encoding="utf-8"))
        self.assertEqual([e["kind"] for e in snapshot["edges"]], expected)

    def test_crash_between_renames_then_retry_dedupes_against_promoted_base(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.crash_after_snapshot_rename(
            "verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 60
        )

        graph = self.append("verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 120)
        self.assertTrue(graph.get("_replayed"))
        # Promotion republished hot from the orphan even though the retry
        # itself was a replay: the store is reconciled, the edge lands once.
        hot_after = json.loads(self.hot().read_text(encoding="utf-8"))
        self.assertEqual([e["kind"] for e in hot_after["edges"]], ["spawn", "verdict"])
        self.assertEqual(
            len(list(self.snapshot_dir.glob("TST-1-*.workgraph.json"))), 2
        )

    def test_missing_hot_with_valid_snapshot_promotes_snapshot_as_base(self) -> None:
        # A crash before the FIRST hot rename ever lands is the same orphan
        # shape with an empty hot prefix; the snapshot history must win over
        # a fresh graph that would shadow it.
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.hot().unlink()

        graph = self.append("archive", "N-1", "N-2", archive_payload(), BASE_TS + 60)
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn", "archive"])
        snapshot = workgraph.load_snapshot("TST-1", self.snapshot_dir)
        self.assertEqual([e["kind"] for e in snapshot["edges"]], ["spawn", "archive"])

    def test_invalid_newest_orphan_falls_back_to_newest_valid_orphan(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.crash_after_snapshot_rename(
            "verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 60
        )

        valid_orphan = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        valid_graph = json.loads(valid_orphan.read_text(encoding="utf-8"))
        invalid_orphan = self.snapshot_dir / (
            "TST-1-r3-1784700060000000000-999999-0.workgraph.json"
        )
        invalid_graph = {key: value for key, value in valid_graph.items() if key != "nodes"}
        invalid_orphan.write_text(json.dumps(invalid_graph), encoding="utf-8")

        graph = self.append("archive", "N-1", "N-2", archive_payload(), BASE_TS + 120)

        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn", "verdict", "archive"])
        self.assertEqual(graph["ticket"], "TST-1")
        hot_graph = json.loads(self.hot().read_text(encoding="utf-8"))
        self.assertEqual(hot_graph["edges"], graph["edges"])
        self.assertIn("nodes", hot_graph)

    def test_valid_divergent_newest_orphan_does_not_promote_older_history(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.crash_after_snapshot_rename(
            "verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 60
        )

        valid_orphan = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        valid_graph = json.loads(valid_orphan.read_text(encoding="utf-8"))
        divergent_graph = {
            **valid_graph,
            "edges": [
                valid_graph["edges"][0],
                {
                    "kind": "archive",
                    "from": "N-1",
                    "to": "N-2",
                    "payload": archive_payload(),
                    "created_at": "2026-07-22T12:01:30Z",
                },
            ],
        }
        divergent_orphan = self.snapshot_dir / (
            "TST-1-r3-1784700090000000000-999999-0.workgraph.json"
        )
        divergent_orphan.write_text(json.dumps(divergent_graph), encoding="utf-8")

        graph = self.append("archive", "N-1", "N-2", archive_payload(), BASE_TS + 120)

        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn", "archive"])
        hot_graph = json.loads(self.hot().read_text(encoding="utf-8"))
        self.assertEqual([e["kind"] for e in hot_graph["edges"]], ["spawn", "archive"])

    def test_normal_append_reads_only_newest_snapshot(self) -> None:
        for index in range(100):
            self.append(
                "spawn",
                "N-1",
                f"N-{index + 10}",
                {**spawn_payload(), "request_id": f"req-{index}"},
                BASE_TS + index,
                orch="wiki" if index == 0 else None,
            )

        original_read_text = Path.read_text
        snapshot_reads = 0

        def counting_read(path_self: Path, *args, **kwargs):
            nonlocal snapshot_reads
            if path_self.parent == self.snapshot_dir:
                snapshot_reads += 1
            return original_read_text(path_self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counting_read):
            self.append("steer", "N-1", "N-10", steer_payload(), BASE_TS + 200)

        self.assertEqual(snapshot_reads, 1)

    def test_two_crashes_converge_through_successive_revisions(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.crash_after_snapshot_rename(
            "verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 60
        )

        # This append recovers the first orphan before committing its own
        # revision, so the first crashed verdict remains in the graph.
        self.append("archive", "N-1", "N-2", archive_payload(), BASE_TS + 120)

        self.crash_after_snapshot_rename(
            "verdict",
            "N-4",
            "N-1",
            verdict_payload("MERGE-READY", findings=[]),
            BASE_TS + 180,
        )

        # A second recovery must promote the second orphan before this final
        # mutation, preserving every committed edge exactly once.
        graph = self.append("archive", "N-1", "N-4", archive_payload(), BASE_TS + 240)

        self.assertEqual(
            [edge["kind"] for edge in graph["edges"]],
            ["spawn", "verdict", "archive", "verdict", "archive"],
        )
        self.assertEqual(len({json.dumps(edge, sort_keys=True) for edge in graph["edges"]}), 5)
        newest = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        newest_graph = json.loads(newest.read_text(encoding="utf-8"))
        self.assertEqual(newest_graph["edges"], graph["edges"])
        self.assertEqual(validate_against_workgraph_schema(newest_graph), [])


class _FakeStagingHandle:
    """NamedTemporaryFile stand-in that can fail at the write or close boundary."""

    def __init__(self, path: Path, fail_on: str | None, events: list[str]) -> None:
        self._file = open(path, "w", encoding="utf-8")
        self.name = str(path)
        self._fail_on = fail_on
        self._events = events

    def __enter__(self) -> "_FakeStagingHandle":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False

    def write(self, text: str) -> None:
        self._events.append("write")
        if self._fail_on == "write":
            raise OSError("injected write failure")
        self._file.write(text)

    def close(self) -> None:
        self._events.append("close")
        self._file.close()
        if self._fail_on == "close":
            raise OSError("injected close failure")


class StagingCleanupTests(unittest.TestCase):
    """Round 3: _stage_json must unlink its temp file on write/close failure."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "stage"
        self.events: list[str] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def stage(self, fail_on: str | None):
        def fake_tempfile(mode, dir=None, suffix="", delete=True, encoding=None):
            return _FakeStagingHandle(
                Path(dir) / f"injected{suffix}", fail_on, self.events
            )

        with mock.patch.object(
            workgraph.tempfile, "NamedTemporaryFile", fake_tempfile
        ):
            return workgraph._stage_json(self.directory, '{"ok": true}')

    def temp_files(self) -> list[Path]:
        return list(self.directory.glob("*.workgraph-tmp"))

    def test_write_failure_unlinks_temp_file(self) -> None:
        with self.assertRaises(workgraph.WorkgraphError) as ctx:
            self.stage("write")
        self.assertIn("injected write failure", str(ctx.exception))
        self.assertEqual(self.events, ["write", "close"])
        self.assertEqual(self.temp_files(), [])

    def test_close_failure_unlinks_temp_file(self) -> None:
        with self.assertRaises(workgraph.WorkgraphError) as ctx:
            self.stage("close")
        self.assertIn("injected close failure", str(ctx.exception))
        self.assertEqual(self.events, ["write", "close"])
        self.assertEqual(self.temp_files(), [])

    def test_success_writes_bytes_in_order_and_keeps_temp(self) -> None:
        temp = self.stage(None)
        self.assertEqual(self.events, ["write", "close"])
        self.assertEqual(self.temp_files(), [temp])
        self.assertEqual(temp.read_text(encoding="utf-8"), '{"ok": true}')


def _concurrent_append_worker(
    barrier, status_dir: str, snapshot_dir: str, worker_index: int, count: int
) -> None:
    from backend.app import workgraph as wg

    barrier.wait(timeout=30)
    for i in range(count):
        payload = {
            "ticket": "TST-1",
            "role": "implement",
            "model": "gpt-5.6-luna",
            "worktree": "/tmp/wt",
            "request_id": f"req-{worker_index}-{i}",
        }
        wg.append_edge(
            "TST-1",
            "spawn",
            "N-1",
            f"N-{worker_index}-{i}",
            payload,
            orch="wiki",
            status_dir=Path(status_dir),
            snapshot_dir=Path(snapshot_dir),
            # Same instant on purpose: snapshot names must still be unique.
            now_ts=BASE_TS,
        )


class ConcurrentAppendTests(unittest.TestCase):
    """Round 3: concurrent writers must never lose edges or snapshots (item 1)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_barrier_concurrent_appends_keep_every_edge_and_snapshot(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        processes = [
            ctx.Process(
                target=_concurrent_append_worker,
                args=(barrier, str(self.status_dir), str(self.snapshot_dir), index, 5),
            )
            for index in (1, 2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=120)
        self.assertEqual([process.exitcode for process in processes], [0, 0])

        graph = workgraph.load_workgraph("TST-1", self.status_dir)
        self.assertEqual(validate_against_workgraph_schema(graph), [])
        self.assertEqual(len(graph["edges"]), 10)
        self.assertEqual(
            {edge["payload"]["request_id"] for edge in graph["edges"]},
            {f"req-{worker}-{i}" for worker in (1, 2) for i in range(5)},
        )
        snapshots = list(self.snapshot_dir.glob("TST-1-*.workgraph.json"))
        self.assertEqual(len(snapshots), 10)
        # Round 4: the recovery-SELECTED snapshot must be the complete graph.
        # All writers share one injected now_ts, so only revision ordering
        # (assigned under the lock, in commit order) can get this right.
        newest = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        newest_graph = json.loads(newest.read_text(encoding="utf-8"))
        self.assertEqual(len(newest_graph["edges"]), 10)
        self.assertEqual(
            {edge["payload"]["request_id"] for edge in newest_graph["edges"]},
            {f"req-{worker}-{i}" for worker in (1, 2) for i in range(5)},
        )

    def test_same_instant_snapshot_names_do_not_collide(self) -> None:
        for index in range(2):
            workgraph.append_edge(
                "TST-1",
                "spawn",
                "N-1",
                f"N-{index}",
                {**spawn_payload(), "request_id": f"req-{index}"},
                orch="wiki",
                status_dir=self.status_dir,
                snapshot_dir=self.snapshot_dir,
                now_ts=BASE_TS,
            )
        snapshots = list(self.snapshot_dir.glob("TST-1-*.workgraph.json"))
        self.assertEqual(len(snapshots), 2)
        newest = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        newest_graph = json.loads(newest.read_text(encoding="utf-8"))
        self.assertEqual(len(newest_graph["edges"]), 2)

    def test_recovery_orders_by_commit_order_not_wall_time(self) -> None:
        # The second commit carries an OLDER wall clock — a slower writer that
        # sampled now_ts before the lock. Recovery must still select it.
        for request_id, ts in (("req-first", BASE_TS + 100), ("req-second", BASE_TS)):
            workgraph.append_edge(
                "TST-1",
                "spawn",
                "N-1",
                f"N-{request_id}",
                {**spawn_payload(), "request_id": request_id},
                orch="wiki",
                status_dir=self.status_dir,
                snapshot_dir=self.snapshot_dir,
                now_ts=ts,
            )
        newest = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        newest_graph = json.loads(newest.read_text(encoding="utf-8"))
        self.assertEqual(
            [e["payload"]["request_id"] for e in newest_graph["edges"]],
            ["req-first", "req-second"],
        )

    def test_revisioned_snapshot_outranks_legacy_with_future_timestamp(self) -> None:
        self.snapshot_dir.mkdir(parents=True)
        future_ns = int((BASE_TS + 9999) * 1_000_000_000)
        legacy = self.snapshot_dir / f"TST-1-{future_ns}-99999-0.workgraph.json"
        legacy.write_text("{}", encoding="utf-8")
        graph = workgraph.append_edge(
            "TST-1",
            "spawn",
            "N-1",
            "N-2",
            spawn_payload(),
            orch="wiki",
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS,
        )
        newest = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        self.assertEqual(str(newest), graph["_snapshot_path"])

    def test_legacy_millisecond_snapshot_names_still_resolve(self) -> None:
        self.snapshot_dir.mkdir(parents=True)
        legacy = self.snapshot_dir / f"TST-1-{int(BASE_TS * 1000)}.workgraph.json"
        legacy.write_text("{}", encoding="utf-8")
        self.assertEqual(
            workgraph.newest_snapshot_path("TST-1", self.snapshot_dir), legacy
        )
        graph = workgraph.append_edge(
            "TST-1",
            "spawn",
            "N-1",
            "N-2",
            spawn_payload(),
            orch="wiki",
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS + 60,
        )
        newest = workgraph.newest_snapshot_path("TST-1", self.snapshot_dir)
        self.assertEqual(str(newest), graph["_snapshot_path"])


class ReplayIdempotencyTests(unittest.TestCase):
    """Round 3: request id is the stable graph-operation key (item 2)."""

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

    def compose_steer(
        self, request_id: str, message: str, created_at: str, mode: str = "now"
    ) -> dict:
        return graph_lint.compose_steer_document(
            "TST-1",
            mode,
            message,
            source_worker="orch:wiki",
            request_id=request_id,
            created_at=created_at,
        )

    def test_spawn_replay_dedupes_across_full_history(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.append("steer", "N-1", "N-2", steer_payload(), BASE_TS + 60)
        graph = self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS + 120)
        self.assertTrue(graph.get("_replayed"))
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn", "steer"])
        on_disk = workgraph.load_workgraph("TST-1", self.status_dir)
        self.assertEqual(sum(1 for e in on_disk["edges"] if e["kind"] == "spawn"), 1)
        self.assertEqual(len(list(self.snapshot_dir.glob("TST-1-*.workgraph.json"))), 1)

    def test_delayed_steer_replay_with_created_at_churn_dedupes(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        first = self.compose_steer("req-steer", "fix the cache", "2026-07-22T12:00:00Z")
        self.append("steer", "N-1", "N-2", first, BASE_TS + 60)
        self.append("verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 120)
        replay = self.compose_steer("req-steer", "fix the cache", "2026-07-22T12:09:00Z")
        graph = self.append("steer", "N-1", "N-2", replay, BASE_TS + 180)
        self.assertTrue(graph.get("_replayed"))
        on_disk = workgraph.load_workgraph("TST-1", self.status_dir)
        self.assertEqual(sum(1 for e in on_disk["edges"] if e["kind"] == "steer"), 1)

    def test_distinct_steer_requests_with_same_text_both_land(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.append(
            "steer",
            "N-1",
            "N-2",
            self.compose_steer("req-a", "continue", "2026-07-22T12:00:00Z"),
            BASE_TS + 60,
        )
        graph = self.append(
            "steer",
            "N-1",
            "N-2",
            self.compose_steer("req-b", "continue", "2026-07-22T12:01:00Z"),
            BASE_TS + 120,
        )
        self.assertFalse(graph.get("_replayed"))
        self.assertEqual(sum(1 for e in graph["edges"] if e["kind"] == "steer"), 2)

    def test_same_request_id_with_altered_body_is_replay(self) -> None:
        # Round 4: the edge-level request id is the identity — a replay whose
        # body text changed (different digest fields) must still dedupe.
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        first = self.compose_steer("req-steer", "fix the cache", "2026-07-22T12:00:00Z")
        self.append("steer", "N-1", "N-2", first, BASE_TS + 60, request_id="req-steer")
        self.append("verdict", "N-3", "N-1", verdict_payload(), BASE_TS + 120)
        altered = self.compose_steer(
            "req-steer", "fix the cache (edited)", "2026-07-22T12:09:00Z"
        )
        graph = self.append(
            "steer", "N-1", "N-2", altered, BASE_TS + 180, request_id="req-steer"
        )
        self.assertTrue(graph.get("_replayed"))
        on_disk = workgraph.load_workgraph("TST-1", self.status_dir)
        steers = [e for e in on_disk["edges"] if e["kind"] == "steer"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["request_id"], "req-steer")

    def test_distinct_request_ids_with_colliding_digest_fields_both_land(self) -> None:
        # Round 4: identical finding id/source_sha simulate a digest-prefix
        # collision; distinct exact request ids must both be recorded.
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        colliding = steer_payload()
        self.append("steer", "N-1", "N-2", colliding, BASE_TS + 60, request_id="req-a")
        graph = self.append(
            "steer", "N-1", "N-2", colliding, BASE_TS + 120, request_id="req-b"
        )
        self.assertFalse(graph.get("_replayed"))
        steers = [e for e in graph["edges"] if e["kind"] == "steer"]
        self.assertEqual([s["request_id"] for s in steers], ["req-a", "req-b"])

    def test_same_request_id_across_modes_records_both(self) -> None:
        # Round 5 MEDIUM: `now` and `on-idle` are distinct supervisor methods
        # (run/send_now vs run/send_on_idle) — one request id used once per
        # mode is two operations, not a replay.
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.append(
            "steer",
            "N-1",
            "N-2",
            self.compose_steer("req-steer", "fix the cache", "2026-07-22T12:00:00Z"),
            BASE_TS + 60,
            request_id="req-steer",
        )
        graph = self.append(
            "steer",
            "N-1",
            "N-2",
            self.compose_steer(
                "req-steer", "fix the cache", "2026-07-22T12:01:00Z", mode="on-idle"
            ),
            BASE_TS + 120,
            request_id="req-steer",
        )
        self.assertFalse(graph.get("_replayed"))
        steers = [e for e in graph["edges"] if e["kind"] == "steer"]
        self.assertEqual([s["payload"]["mode"] for s in steers], ["now", "on-idle"])

    def test_replay_dedupes_within_each_mode_after_both_recorded(self) -> None:
        # Same id + changed text within ONE mode stays a replay even after
        # the other mode landed its own edge with that id.
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        for offset, mode in ((60, "now"), (120, "on-idle")):
            self.append(
                "steer",
                "N-1",
                "N-2",
                self.compose_steer(
                    "req-steer", "fix the cache", "2026-07-22T12:00:00Z", mode=mode
                ),
                BASE_TS + offset,
                request_id="req-steer",
            )
        for offset, mode in ((180, "now"), (240, "on-idle")):
            graph = self.append(
                "steer",
                "N-1",
                "N-2",
                self.compose_steer(
                    "req-steer", "fix the cache (edited)", "2026-07-22T12:09:00Z", mode=mode
                ),
                BASE_TS + offset,
                request_id="req-steer",
            )
            self.assertTrue(graph.get("_replayed"), f"mode {mode} should replay")
        on_disk = workgraph.load_workgraph("TST-1", self.status_dir)
        self.assertEqual(sum(1 for e in on_disk["edges"] if e["kind"] == "steer"), 2)

    def test_legacy_digest_key_is_also_mode_scoped(self) -> None:
        # Edges without an edge-level request id fall back to the digest
        # fields, which are body-derived and identical across modes; the
        # fallback key must still tell the two supervisor methods apart.
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.append(
            "steer",
            "N-1",
            "N-2",
            self.compose_steer("req-steer", "fix the cache", "2026-07-22T12:00:00Z"),
            BASE_TS + 60,
        )
        graph = self.append(
            "steer",
            "N-1",
            "N-2",
            self.compose_steer(
                "req-steer", "fix the cache", "2026-07-22T12:01:00Z", mode="on-idle"
            ),
            BASE_TS + 120,
        )
        self.assertFalse(graph.get("_replayed"))
        self.assertEqual(sum(1 for e in graph["edges"] if e["kind"] == "steer"), 2)

    def test_spawn_with_different_request_id_appends(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        graph = self.append(
            "spawn",
            "N-1",
            "N-3",
            {**spawn_payload(), "request_id": "req-other"},
            BASE_TS + 60,
        )
        self.assertFalse(graph.get("_replayed"))
        self.assertEqual(sum(1 for e in graph["edges"] if e["kind"] == "spawn"), 2)


class ValidationLayerTests(unittest.TestCase):
    """Round 2: each validation layer must hold independently (item 6)."""

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

    def test_edge_validation_runs_before_any_graph_io(self) -> None:
        with mock.patch.object(workgraph, "load_workgraph") as load:
            with self.assertRaises(workgraph.WorkgraphError):
                self.append("verdict", "N-3", "N-1", {"worker": "x"}, BASE_TS, orch="wiki")
        load.assert_not_called()

    def test_bad_payload_rejected_even_without_final_graph_validation(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        real = graph_lint.validate_document

        def lenient(document, schema_name):
            if schema_name == "workgraph":
                return []
            return real(document, schema_name)

        with mock.patch.object(graph_lint, "validate_document", side_effect=lenient):
            with self.assertRaises(workgraph.WorkgraphError):
                self.append("verdict", "N-3", "N-1", {"worker": "x"}, BASE_TS + 60)

    def test_final_graph_validation_failure_writes_nothing(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        hot = self.status_dir / "TST-1.workgraph.json"
        before = hot.read_bytes()
        real_validate = workgraph._validate

        def strict(document, schema_name, what):
            if what == "workgraph":
                raise workgraph.WorkgraphError("injected final-validation failure")
            return real_validate(document, schema_name, what)

        with mock.patch.object(workgraph, "_validate", side_effect=strict):
            with self.assertRaises(workgraph.WorkgraphError):
                self.append("steer", "N-1", "N-2", steer_payload(), BASE_TS + 60)
        self.assertEqual(hot.read_bytes(), before)
        self.assertEqual(list(self.status_dir.glob("*.workgraph-tmp")), [])


class CorruptHotFileTests(unittest.TestCase):
    """Round 2: existing-but-invalid hot files fail closed (item 4)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"
        self.status_dir.mkdir(parents=True)

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

    def hot(self) -> Path:
        return self.status_dir / "TST-1.workgraph.json"

    def test_missing_file_loads_as_none(self) -> None:
        self.assertIsNone(workgraph.load_workgraph("TST-1", self.status_dir))

    def test_malformed_json_fails_closed(self) -> None:
        self.hot().write_text("{not json", encoding="utf-8")
        with self.assertRaises(workgraph.WorkgraphCorruptError):
            workgraph.load_workgraph("TST-1", self.status_dir)
        with self.assertRaises(workgraph.WorkgraphCorruptError):
            self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.assertEqual(self.hot().read_text(encoding="utf-8"), "{not json")

    def test_non_object_json_fails_closed(self) -> None:
        self.hot().write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(workgraph.WorkgraphCorruptError):
            self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.assertEqual(self.hot().read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_unreadable_hot_file_fails_closed(self) -> None:
        self.hot().mkdir()  # read_text raises IsADirectoryError, an OSError
        with self.assertRaises(workgraph.WorkgraphCorruptError):
            workgraph.load_workgraph("TST-1", self.status_dir)
        with self.assertRaises(workgraph.WorkgraphCorruptError):
            self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")

    def test_schema_invalid_existing_graph_fails_closed(self) -> None:
        self.hot().write_text('{"ticket": "TST-1"}', encoding="utf-8")
        with self.assertRaises(workgraph.WorkgraphCorruptError):
            self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.assertEqual(self.hot().read_text(encoding="utf-8"), '{"ticket": "TST-1"}')

    def test_recover_from_snapshot_restores_and_preserves_damaged_file(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        good = self.hot().read_text(encoding="utf-8")
        self.hot().write_text("{damaged", encoding="utf-8")
        result = workgraph.recover_from_snapshot(
            "TST-1", status_dir=self.status_dir, snapshot_dir=self.snapshot_dir
        )
        self.assertEqual(self.hot().read_text(encoding="utf-8"), good)
        backups = list(self.status_dir.glob("TST-1.workgraph.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{damaged")
        self.assertEqual(result["backup"], str(backups[0]))

    def test_recover_without_snapshot_fails(self) -> None:
        with self.assertRaises(workgraph.WorkgraphError):
            workgraph.recover_from_snapshot(
                "TST-1", status_dir=self.status_dir, snapshot_dir=self.snapshot_dir
            )

    def test_append_after_recover_continues_history(self) -> None:
        self.append("spawn", "N-1", "N-2", spawn_payload(), BASE_TS, orch="wiki")
        self.hot().write_text("{damaged", encoding="utf-8")
        workgraph.recover_from_snapshot(
            "TST-1", status_dir=self.status_dir, snapshot_dir=self.snapshot_dir
        )
        graph = self.append("steer", "N-1", "N-2", steer_payload(), BASE_TS + 60)
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn", "steer"])


class NodeRoleMatrixTests(unittest.TestCase):
    """Round 2: explicit endpoint node kinds for every edge kind (item 5)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def first_edge_nodes(self, kind: str, payload: dict) -> dict[str, str]:
        graph = workgraph.append_edge(
            "TST-1",
            kind,
            "N-from",
            "N-to",
            payload,
            orch="wiki",
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS,
        )
        return {node["id"]: node["kind"] for node in graph["nodes"]}

    def test_every_edge_kind_infers_declared_endpoint_kinds(self) -> None:
        cases = {
            "spawn": (spawn_payload(role="implement"), "orchestrator", "implement"),
            "steer": (steer_payload(), "orchestrator", "worker"),
            "verdict": (verdict_payload(), "review", "orchestrator"),
            "archive": (archive_payload(), "orchestrator", "worker"),
            "handoff": (handoff_payload(), "worker", "worker"),
            "monitor_alarm": (monitor_alarm_payload(), "monitor", "orchestrator"),
            "capability_grant": (capability_grant_payload(), "orchestrator", "orchestrator"),
            "escalation": (escalation_payload(), "monitor", "orchestrator"),
        }
        self.assertEqual(set(cases), set(workgraph.EDGE_KINDS))
        for kind, (payload, expected_from, expected_to) in cases.items():
            with self.subTest(kind=kind):
                nodes = self.first_edge_nodes(kind, payload)
                self.assertEqual(nodes["N-from"], expected_from)
                self.assertEqual(nodes["N-to"], expected_to)
            self.tearDown()
            self.setUp()

    def test_spawn_target_takes_role_from_payload(self) -> None:
        nodes = self.first_edge_nodes("spawn", spawn_payload("TST-1-REVIEW1", "review"))
        self.assertEqual(nodes["N-to"], "review")

    def test_first_seen_monitor_does_not_count_as_live_worker(self) -> None:
        graph = workgraph.append_edge(
            "TST-1",
            "monitor_alarm",
            "N-mon",
            "N-orch",
            monitor_alarm_payload(),
            orch="wiki",
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS,
        )
        health = workgraph.compute_composite_health(graph, now_ts=BASE_TS + 5000)
        self.assertEqual(health["slowest_node_stall_seconds"], 0)

    def test_first_seen_verdict_source_counts_as_live_reviewer(self) -> None:
        workgraph.append_edge(
            "TST-1",
            "spawn",
            "N-1",
            "N-2",
            spawn_payload(),
            orch="wiki",
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS,
        )
        graph = workgraph.append_edge(
            "TST-1",
            "verdict",
            "N-9",
            "N-1",
            verdict_payload(),
            status_dir=self.status_dir,
            snapshot_dir=self.snapshot_dir,
            now_ts=BASE_TS + 60,
        )
        node_kinds = {node["id"]: node["kind"] for node in graph["nodes"]}
        self.assertEqual(node_kinds["N-9"], "review")
        alarms = workgraph.health_alarms(graph, now_ts=BASE_TS + 90)
        self.assertNotIn("blocking_no_reviewer", [a["check"] for a in alarms])


class ClockConsistencyTests(unittest.TestCase):
    """Round 2: renderer and health endpoint share one injected clock (item 3)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"
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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_renderer_and_health_endpoint_agree_on_stall(self) -> None:
        with (
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
            mock.patch.object(workgraph, "CLOCK", lambda: BASE_TS + 900),
        ):
            rendered = main.agent_workgraph("TST-9")
            health = main.agent_workgraph_health("TST-9")
        self.assertEqual(
            rendered["workgraph"]["composite_health"]["slowest_node_stall_seconds"], 900
        )
        self.assertEqual(rendered["health"], health["health"])
        self.assertEqual(rendered["alarms"], health["alarms"])
        self.assertEqual(rendered["computed_at"], health["computed_at"])
        self.assertEqual(health["health"]["slowest_node_stall_seconds"], 900)

    def test_stall_tracks_the_injected_clock(self) -> None:
        with (
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
            mock.patch.object(workgraph, "CLOCK", lambda: BASE_TS + 2000),
        ):
            rendered = main.agent_workgraph("TST-9")
            health = main.agent_workgraph_health("TST-9")
        self.assertEqual(
            rendered["workgraph"]["composite_health"]["slowest_node_stall_seconds"], 2000
        )
        self.assertEqual([a["check"] for a in health["alarms"]], ["node_stall"])
        self.assertEqual([a["check"] for a in rendered["alarms"]], ["node_stall"])


class CorruptHotEndpointTests(unittest.TestCase):
    """Round 2: serving endpoints surface hot-file corruption (item 4)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"
        self.status_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_corrupt_hot_with_snapshot_serves_snapshot_with_warning(self) -> None:
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
        (self.status_dir / "TST-9.workgraph.json").write_text("{damaged", encoding="utf-8")
        with (
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
        ):
            payload = main.agent_workgraph("TST-9")
        self.assertEqual(payload["source"], "snapshot")
        self.assertIn("recover", payload["warning"])

    def test_corrupt_hot_without_snapshot_is_500(self) -> None:
        (self.status_dir / "TST-9.workgraph.json").write_text("{damaged", encoding="utf-8")
        with (
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.agent_workgraph("TST-9")
        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
