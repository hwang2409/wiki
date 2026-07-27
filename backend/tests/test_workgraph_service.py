"""WIKI-163 round 2: canonical spawn/steer/archive actions write the workgraph.

The three canonical action paths (CLI, MCP agent tools, app UI) all converge
on the backend endpoints, which route through ``workgraph_service``. These
tests prove an ordinary action sequence creates a valid workgraph with no
manual ``wiki graph append`` calls, and that graph failures never break the
underlying agent operation.
"""

from __future__ import annotations

import fcntl
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
        # Earlier suites may have run the app lifespan, whose shutdown leaves
        # the module-global outbox closed; reopen it for this suite.
        workgraph_service.start_outbox()

    def tearDown(self) -> None:
        # Flush BEFORE removing the tmpdir: unittest runs tearDown ahead of
        # addCleanup callbacks, so a flush registered there fires too late and
        # in-flight outbox deliveries race TemporaryDirectory cleanup.
        workgraph_service.flush_outbox(timeout=10)
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

    def test_typed_escalation_is_written_and_lints(self) -> None:
        workgraph_service.record_spawn(
            agent_id="WIKI-9",
            orch="wiki",
            role="implement",
            model="gpt-5.6-luna",
            effort="high",
            worktree="/tmp/wt",
            request_id="req-1",
            status_dir=self.status_dir,
        )
        workgraph_service.record_escalation(
            ticket="WIKI-9",
            orch="wiki",
            reason="node stall exceeded 1800s (1901s)",
            prior_findings=[],
            target="henry",
            request_id="fleet:WIKI-9:graph-health:stall",
            status_dir=self.status_dir,
        )
        flush()

        graph = self.load()
        self.assertEqual(graph_lint.validate_document(graph, "workgraph"), [])
        escalation = graph["edges"][-1]
        self.assertEqual(escalation["kind"], "escalation")
        self.assertEqual(escalation["payload"]["target"], "henry")
        self.assertEqual(graph_lint.validate_document(escalation, "edge"), [])
        snapshots = sorted(self.snapshot_dir.glob("WIKI-9-*.workgraph.json"))
        self.assertTrue(snapshots)
        durable = json.loads(snapshots[-1].read_text(encoding="utf-8"))
        self.assertEqual(durable["edges"][-1]["kind"], "escalation")

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
        workgraph_service.start_outbox()

    def tearDown(self) -> None:
        workgraph_service.flush_outbox(timeout=10)
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
        # Round 4: the supervisor's exact request id rides on the edge.
        self.assertEqual(
            [s["request_id"] for s in steers], ["req-steer-1", "req-steer-2"]
        )

    def test_steer_replay_with_altered_text_appends_no_duplicate(self) -> None:
        main.spawn_agent(self.spawn_body())
        self.register_worker("WIKI-9", "implement")
        for text in ("fix the cache", "fix the cache (retry, edited)"):
            main.agent_message(
                "WIKI-9",
                main.MessageIn(
                    text=text,
                    mode="now",
                    source="supervisor-steer",
                    request_id="req-steer-1",
                ),
                BackgroundTasks(),
            )
        flush()
        graph = json.loads(
            (self.status_dir / "WIKI-9.workgraph.json").read_text(encoding="utf-8")
        )
        steers = [e for e in graph["edges"] if e["kind"] == "steer"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["request_id"], "req-steer-1")
        self.assertEqual(
            steers[0]["payload"]["findings"][0]["observed"], "fix the cache"
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
        workgraph_service.start_outbox()

    def tearDown(self) -> None:
        workgraph_service.flush_outbox(timeout=10)
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
        outbox = workgraph_service._TelemetryOutbox(max_pending=2)
        release = threading.Event()
        entered = threading.Event()

        def blocked() -> None:
            entered.set()
            assert release.wait(timeout=10)

        try:
            # First submission is in flight; second queues; third exceeds the
            # pending bound and is dropped with a log line.
            self.assertTrue(outbox.submit("WIKI-9", "spawn", blocked))
            self.assertTrue(entered.wait(timeout=10))
            self.assertTrue(outbox.submit("WIKI-9", "steer", lambda: None))
            with self.assertLogs("wiki.workgraph", level="ERROR") as logs:
                self.assertFalse(outbox.submit("WIKI-9", "archive", lambda: None))
            self.assertIn("outbox full", "\n".join(logs.output))
        finally:
            release.set()
        self.assertTrue(outbox.flush(timeout=10))

    def test_ticket_b_delivers_while_ticket_a_lock_is_held(self) -> None:
        # Round 4: a synchronous CLI writer holding ticket A's flock must not
        # stall other tickets' telemetry — the outbox is keyed per ticket.
        self.status_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.status_dir / "WIKI-1.workgraph.lock", "a+b")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

        def spawn(ticket: str) -> None:
            workgraph_service.record_spawn(
                agent_id=ticket,
                orch="wiki",
                role="implement",
                model="claude-opus-4-6",
                effort=None,
                worktree="/tmp/wt",
                request_id=f"req-{ticket}",
                status_dir=self.status_dir,
            )

        spawn("WIKI-1")
        spawn("WIKI-2")
        b_hot = self.status_dir / "WIKI-2.workgraph.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not b_hot.exists():
            time.sleep(0.01)
        self.assertTrue(b_hot.exists(), "ticket B stalled behind ticket A's held lock")
        # A is still blocked on its lock: nothing delivered for it yet.
        self.assertFalse((self.status_dir / "WIKI-1.workgraph.json").exists())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        flush()
        self.assertTrue((self.status_dir / "WIKI-1.workgraph.json").exists())

    def test_per_ticket_order_preserved_under_keyed_delivery(self) -> None:
        outbox = workgraph_service._TelemetryOutbox()
        delivered: list[str] = []
        gate = threading.Event()

        def deliver(tag: str, wait: bool = False):
            def run() -> None:
                if wait:
                    assert gate.wait(timeout=10)
                delivered.append(tag)

            return run

        # A's first delivery blocks; A's second must still run after it while
        # B proceeds independently.
        self.assertTrue(outbox.submit("WIKI-1", "spawn", deliver("a1", wait=True)))
        self.assertTrue(outbox.submit("WIKI-1", "steer", deliver("a2")))
        self.assertTrue(outbox.submit("WIKI-2", "spawn", deliver("b1")))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "b1" not in delivered:
            time.sleep(0.01)
        self.assertEqual(delivered, ["b1"])
        gate.set()
        self.assertTrue(outbox.flush(timeout=10))
        self.assertEqual(delivered, ["b1", "a1", "a2"])


class OutboxLifecycleTests(unittest.TestCase):
    """Round 4: lifespan-owned drain/stop — no silently discarded writes."""

    def test_stop_drains_pending_write(self) -> None:
        outbox = workgraph_service._TelemetryOutbox()
        delivered: list[bool] = []
        entered = threading.Event()

        def slow() -> None:
            entered.set()
            time.sleep(0.1)
            delivered.append(True)

        self.assertTrue(outbox.submit("WIKI-9", "spawn", slow))
        self.assertTrue(entered.wait(timeout=10))
        self.assertTrue(outbox.stop(timeout=10))
        self.assertEqual(delivered, [True])

    def test_stop_timeout_logs_each_undelivered_item(self) -> None:
        outbox = workgraph_service._TelemetryOutbox()
        release = threading.Event()
        entered = threading.Event()

        def stuck() -> None:
            entered.set()
            assert release.wait(timeout=30)

        try:
            self.assertTrue(outbox.submit("WIKI-9", "spawn", stuck))
            self.assertTrue(entered.wait(timeout=10))
            self.assertTrue(outbox.submit("WIKI-9", "steer", lambda: None))
            with self.assertLogs("wiki.workgraph", level="ERROR") as logs:
                self.assertFalse(outbox.stop(timeout=0.2))
            joined = "\n".join(logs.output)
            self.assertIn("undelivered steer edge for WIKI-9", joined)
            self.assertIn("spawn edge for WIKI-9 still delivering", joined)
            # Stopped outbox refuses (and logs) new work instead of silently
            # accepting writes that can never be delivered.
            with self.assertLogs("wiki.workgraph", level="ERROR") as logs:
                self.assertFalse(outbox.submit("WIKI-9", "archive", lambda: None))
            self.assertIn("outbox stopped", "\n".join(logs.output))
        finally:
            release.set()

    def test_start_reopens_a_stopped_outbox(self) -> None:
        outbox = workgraph_service._TelemetryOutbox()
        self.assertTrue(outbox.stop(timeout=1))
        self.assertFalse(outbox.submit("WIKI-9", "spawn", lambda: None))
        outbox.start()
        delivered: list[bool] = []
        self.assertTrue(outbox.submit("WIKI-9", "spawn", lambda: delivered.append(True)))
        self.assertTrue(outbox.flush(timeout=10))
        self.assertEqual(delivered, [True])


if __name__ == "__main__":
    unittest.main()
