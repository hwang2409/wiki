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
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import BackgroundTasks

from backend.app import main, workgraph, workgraph_service
from wiki_cli import graph_lint


def flush() -> None:
    assert workgraph_service.flush_outbox(timeout=10)


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
        flush()

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
        flush()
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
                flush()


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
        flush()

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
        flush()
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
        flush()
        self.assertEqual(list(self.status_dir.glob("*.workgraph.json")), [])

    def test_workgraph_failure_never_fails_the_spawn(self) -> None:
        with mock.patch.object(
            workgraph, "append_edge", side_effect=RuntimeError("graph exploded")
        ):
            with self.assertLogs("wiki.workgraph", level="ERROR"):
                result = main.spawn_agent(self.spawn_body())
                flush()
        self.assertEqual(result["run_id"], "r1")
        self.assertEqual(list(self.status_dir.glob("*.workgraph.json")), [])

    def replay_supervisor(self):
        idempotent = {
            "idempotency/status": {"known": True},
        }

        def supervisor(method: str, params=None):
            return idempotent.get(method, {"run_id": "r1"})

        return mock.patch.object(main, "_supervisor_request", supervisor)

    def test_replayed_spawn_request_appends_no_duplicate_edge(self) -> None:
        main.spawn_agent(self.spawn_body())
        self.register_worker("WIKI-9", "implement")
        with self.replay_supervisor():
            main.spawn_agent(self.spawn_body())
        flush()
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn"])

    def test_replayed_spawn_heals_a_failed_first_append(self) -> None:
        with mock.patch.object(
            workgraph, "append_edge", side_effect=RuntimeError("graph exploded")
        ):
            with self.assertLogs("wiki.workgraph", level="ERROR"):
                main.spawn_agent(self.spawn_body())
                flush()
        self.assertEqual(list(self.status_dir.glob("*.workgraph.json")), [])

        # The supervisor treats the retried request as a replay; recording is
        # still attempted, so the missing spawn edge heals exactly once.
        self.register_worker("WIKI-9", "implement")
        with self.replay_supervisor():
            main.spawn_agent(self.spawn_body())
            flush()
            main.spawn_agent(self.spawn_body())
            flush()
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn"])
        self.assertEqual(graph["edges"][0]["payload"]["request_id"], "req-WIKI-9")

    def test_delayed_steer_replay_appends_no_duplicate(self) -> None:
        main.spawn_agent(self.spawn_body())
        self.register_worker("WIKI-9", "implement")
        steer = main.MessageIn(
            text="fix the cache",
            mode="now",
            source="supervisor-steer",
            request_id="req-steer-1",
        )
        main.agent_message("WIKI-9", steer, BackgroundTasks())
        main.agent_message(
            "WIKI-9",
            main.MessageIn(
                text="then run the tests",
                mode="now",
                source="supervisor-steer",
                request_id="req-steer-2",
            ),
            BackgroundTasks(),
        )
        # The delayed replay arrives after another steer landed: newest-edge
        # equality can't catch it, the request-id key must.
        main.agent_message("WIKI-9", steer, BackgroundTasks())
        flush()
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        steers = [e for e in graph["edges"] if e["kind"] == "steer"]
        self.assertEqual(
            [s["payload"]["findings"][0]["observed"] for s in steers],
            ["fix the cache", "then run the tests"],
        )


class OutboxTests(unittest.TestCase):
    """Round 3: graph writes must never block the action response (item 3)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status_dir = root / "status"
        self.snapshot_dir = root / "workgraphs"
        patcher = mock.patch.object(workgraph, "SNAPSHOT_DIR", self.snapshot_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(workgraph_service.flush_outbox)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def record_spawn(self, request_id: str = "req-1") -> None:
        workgraph_service.record_spawn(
            agent_id="WIKI-9",
            orch="wiki",
            role="implement",
            model="claude-opus-4-6",
            effort=None,
            worktree="/tmp/wt",
            request_id=request_id,
            status_dir=self.status_dir,
        )

    def test_record_returns_while_append_is_blocked(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        real_append = workgraph.append_edge

        def blocking(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=10)
            return real_append(*args, **kwargs)

        with mock.patch.object(workgraph, "append_edge", side_effect=blocking):
            started = time.monotonic()
            self.record_spawn()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
            self.assertTrue(entered.wait(timeout=10))
            # The write is still blocked: nothing on disk yet, response long gone.
            self.assertFalse((self.status_dir / "WIKI-9.workgraph.json").exists())
            release.set()
            flush()
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        self.assertEqual([e["kind"] for e in graph["edges"]], ["spawn"])

    def test_per_ticket_delivery_order_is_preserved(self) -> None:
        self.record_spawn()
        for index, text in enumerate(["first steer", "second steer", "third steer"]):
            workgraph_service.record_steer(
                agent_id="WIKI-9",
                orch="wiki",
                mode="now",
                text=text,
                source="supervisor-steer",
                request_id=f"req-steer-{index}",
                status_dir=self.status_dir,
            )
        flush()
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        steers = [e for e in graph["edges"] if e["kind"] == "steer"]
        self.assertEqual(
            [s["payload"]["findings"][0]["observed"] for s in steers],
            ["first steer", "second steer", "third steer"],
        )

    def test_failed_delivery_is_logged_and_later_edges_still_deliver(self) -> None:
        real_append = workgraph.append_edge
        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("first delivery exploded")
            return real_append(*args, **kwargs)

        with mock.patch.object(workgraph, "append_edge", side_effect=flaky):
            with self.assertLogs("wiki.workgraph", level="ERROR"):
                self.record_spawn("req-1")
                self.record_spawn("req-2")
                flush()
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [e["payload"]["request_id"] for e in graph["edges"]], ["req-2"]
        )

    def test_full_outbox_drops_edge_and_logs(self) -> None:
        outbox = workgraph_service._TelemetryOutbox(maxsize=1)
        release = threading.Event()
        entered = threading.Event()

        def blocked() -> None:
            entered.set()
            assert release.wait(timeout=10)

        try:
            # First submission occupies the worker; second fills the queue.
            self.assertTrue(outbox.submit("WIKI-9", "spawn", blocked))
            self.assertTrue(entered.wait(timeout=10))
            self.assertTrue(outbox.submit("WIKI-9", "steer", lambda: None))
            with self.assertLogs("wiki.workgraph", level="ERROR") as logs:
                self.assertFalse(outbox.submit("WIKI-9", "archive", lambda: None))
            self.assertIn("outbox full", "\n".join(logs.output))
        finally:
            release.set()
        self.assertTrue(outbox.flush(timeout=10))


if __name__ == "__main__":
    unittest.main()
