"""WIKI-163 round 2: canonical spawn/steer/archive actions write the workgraph.

The three canonical action paths (CLI, MCP agent tools, app UI) all converge
on the backend endpoints, which route through ``workgraph_service``. These
tests prove an ordinary action sequence creates a valid workgraph with no
manual ``wiki graph append`` calls, and that graph failures never break the
underlying agent operation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import BackgroundTasks

from backend.app import main, workgraph, workgraph_service
from wiki_cli import graph_lint


class BaseTicketTests(unittest.TestCase):
    def test_role_suffixes_strip_to_base_ticket(self) -> None:
        self.assertEqual(workgraph_service.base_ticket("WIKI-9"), "WIKI-9")
        self.assertEqual(workgraph_service.base_ticket("WIKI-9-REVIEW1"), "WIKI-9")
        self.assertEqual(workgraph_service.base_ticket("PHO-14060-PLAN2"), "PHO-14060")
        self.assertEqual(workgraph_service.base_ticket("PHO-14060-SIM3"), "PHO-14060")
        self.assertEqual(workgraph_service.base_ticket("TIX-REVIEWER"), "TIX-REVIEWER")


class ServiceSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"
        patcher = mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def load(self, ticket: str = "WIKI-9") -> dict:
        path = self.status_dir / f"{ticket}.workgraph.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_ordinary_sequence_builds_valid_graph(self) -> None:
        workgraph_service.record_spawn(
            agent_id="WIKI-9",
            orch="wiki",
            role="implement",
            model="claude-opus-4-6",
            effort=None,
            worktree="/tmp/wt",
            request_id="req-1",
            status_dir=self.status_dir,
        )
        workgraph_service.record_spawn(
            agent_id="WIKI-9-REVIEW1",
            orch="wiki",
            role="review",
            model="gpt-5.6-sol",
            effort="high",
            worktree="/tmp/wt-r",
            request_id="req-2",
            status_dir=self.status_dir,
        )
        workgraph_service.record_steer(
            agent_id="WIKI-9",
            orch="wiki",
            mode="now",
            text="address the review findings",
            source="supervisor-steer",
            request_id="req-3",
            status_dir=self.status_dir,
        )
        workgraph_service.record_archive(
            agent_id="WIKI-9-REVIEW1",
            orch="wiki",
            outcome="merged",
            status_dir=self.status_dir,
        )

        graph = self.load()
        self.assertEqual(graph_lint.validate_document(graph, "workgraph"), [])
        self.assertEqual(graph["ticket"], "WIKI-9")
        self.assertEqual(graph["orch"], "wiki")
        self.assertEqual(
            [e["kind"] for e in graph["edges"]], ["spawn", "spawn", "steer", "archive"]
        )
        nodes = {node["id"]: node["kind"] for node in graph["nodes"]}
        self.assertEqual(
            nodes,
            {
                "orch:wiki": "orchestrator",
                "WIKI-9": "implement",
                "WIKI-9-REVIEW1": "review",
            },
        )
        # Both worker agents share the base ticket's single graph file.
        self.assertFalse((self.status_dir / "WIKI-9-REVIEW1.workgraph.json").exists())

    def test_missing_orch_falls_back_to_henry_actor(self) -> None:
        workgraph_service.record_spawn(
            agent_id="WIKI-9",
            orch=None,
            role="implement",
            model="claude-opus-4-6",
            effort=None,
            worktree="/tmp/wt",
            request_id=None,
            status_dir=self.status_dir,
        )
        graph = self.load()
        self.assertEqual(graph["orch"], "henry")
        self.assertEqual(graph["nodes"][0]["id"], "orch:henry")

    def test_append_failure_is_swallowed_and_logged(self) -> None:
        with mock.patch.object(
            workgraph, "append_edge", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("wiki.workgraph", level="ERROR"):
                workgraph_service.record_archive(
                    agent_id="WIKI-9",
                    orch="wiki",
                    outcome="merged",
                    status_dir=self.status_dir,
                )


class CanonicalEndpointTests(unittest.TestCase):
    """Spawn -> steer -> archive through the real backend handlers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.status_dir.mkdir(parents=True)
        self.snapshot_dir = root / "workgraphs"
        self.workdir = root / "worktree"
        self.workdir.mkdir()
        self.registry: dict = {"_orchestrators": {"wiki": {}}}
        for target, attr, value in (
            (main, "AGENT_STATUS_DIR", self.status_dir),
            (workgraph, "SNAPSHOT_DIR", self.snapshot_dir),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, replacement in (
            ("_read_agent_registry", lambda: self.registry),
            ("_supervisor_request", lambda method, params=None: {"run_id": "r1"}),
            ("tmux_live_windows", lambda: set()),
            ("_require_allowed_model", lambda kind, model, target: None),
        ):
            patcher = mock.patch.object(main, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(main.PROVIDER_HEALTH, "spawn_hint", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def spawn_body(self, ticket: str = "WIKI-9", role: str = "implement") -> dict:
        return {
            "ticket": ticket,
            "kind": "cc",
            "role": role,
            "model": "claude-opus-4-6",
            "workdir": str(self.workdir),
            "orch": "wiki",
            "prompt": "implement the thing",
            "request_id": f"req-{ticket}",
        }

    def register_worker(self, ticket: str, role: str) -> None:
        self.registry[ticket] = {
            "current": {"run_id": "r1", "role": role, "orch": "wiki", "kind": "cc"}
        }

    def test_spawn_steer_archive_produce_workgraph_without_manual_appends(self) -> None:
        main.spawn_agent(self.spawn_body())
        self.register_worker("WIKI-9", "implement")
        main.agent_message(
            "WIKI-9",
            main.MessageIn(text="fix the cache", mode="now", source="supervisor-steer"),
            BackgroundTasks(),
        )
        main.archive_agent("WIKI-9", main.AgentArchiveIn(outcome="merged"))

        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        self.assertEqual(graph_lint.validate_document(graph, "workgraph"), [])
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn", "steer", "archive"])
        nodes = {node["id"]: node["kind"] for node in graph["nodes"]}
        self.assertEqual(nodes, {"orch:wiki": "orchestrator", "WIKI-9": "implement"})
        self.assertEqual(graph["composite_health"]["state"], "archived")
        spawn_edge = graph["edges"][0]
        self.assertEqual(spawn_edge["payload"]["worktree"], str(self.workdir.resolve()))
        self.assertEqual(spawn_edge["payload"]["request_id"], "req-WIKI-9")
        steer_edge = graph["edges"][1]
        self.assertEqual(steer_edge["payload"]["target_worker"], "WIKI-9")
        self.assertEqual(steer_edge["payload"]["mode"], "now")

    def test_reviewer_spawn_lands_on_base_ticket_graph(self) -> None:
        main.spawn_agent(self.spawn_body())
        main.spawn_agent(self.spawn_body("WIKI-9-REVIEW1", "review"))
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        nodes = {node["id"]: node["kind"] for node in graph["nodes"]}
        self.assertEqual(nodes["WIKI-9-REVIEW1"], "review")
        self.assertFalse((self.status_dir / "WIKI-9-REVIEW1.workgraph.json").exists())

    def test_steer_to_orchestrator_writes_no_graph(self) -> None:
        self.registry["wiki"] = {
            "current": {"run_id": "r2", "role": "orchestrator", "kind": "cc"}
        }
        main.agent_message(
            "wiki",
            main.MessageIn(text="status?", mode="now"),
            BackgroundTasks(),
        )
        self.assertEqual(list(self.status_dir.glob("*.workgraph.json")), [])

    def test_workgraph_failure_never_fails_the_spawn(self) -> None:
        with mock.patch.object(
            workgraph, "append_edge", side_effect=RuntimeError("graph exploded")
        ):
            with self.assertLogs("wiki.workgraph", level="ERROR"):
                result = main.spawn_agent(self.spawn_body())
        self.assertEqual(result["run_id"], "r1")
        self.assertEqual(list(self.status_dir.glob("*.workgraph.json")), [])

    def test_replayed_spawn_request_appends_no_duplicate_edge(self) -> None:
        main.spawn_agent(self.spawn_body())
        self.register_worker("WIKI-9", "implement")
        idempotent = {
            "idempotency/status": {"known": True},
        }

        def supervisor(method: str, params=None):
            return idempotent.get(method, {"run_id": "r1"})

        with mock.patch.object(main, "_supervisor_request", supervisor):
            main.spawn_agent(self.spawn_body())
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn"])


if __name__ == "__main__":
    unittest.main()
