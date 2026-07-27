from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from concurrent.futures import Future
from contextlib import ExitStack
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from backend.app import workgraph
from backend.app.agent_runtime.fake import FixtureAdapterFactory
from backend.app.agent_runtime.fleet_monitor import FleetMonitor, Notification, _WorkerView
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor
from backend.app.agent_runtime.types import LifecycleState, ProviderKind


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


def _paths(root: Path) -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=root / "runtime",
        socket_path=root / "runtime" / "supervisor.sock",
        registry_path=root / "isolated-registry.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )


class _RecordingSend:
    """Async callable that captures send_now invocations for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.fail_with: Exception | None = None

    async def __call__(
        self,
        run_id: str,
        message: str,
        dedupe_key: str | None,
        source: str | None = None,
    ) -> dict:
        self.calls.append((run_id, message, dedupe_key, source))
        if self.fail_with is not None:
            raise self.fail_with
        return {"status": "sent"}


class _SelectiveSend:
    """Block one orchestrator while allowing another to receive messages."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.slow_run_id: str | None = None
        self.started = asyncio.Event()
        self.fast_received = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(
        self,
        run_id: str,
        message: str,
        dedupe_key: str | None,
        source: str | None = None,
    ) -> dict:
        self.calls.append((run_id, message, dedupe_key, source))
        if run_id == self.slow_run_id:
            self.started.set()
            await self.release.wait()
        else:
            self.fast_received.set()
        return {"status": "sent"}


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _write_status(store: RunStore, agent_id: str, payload: dict, mtime: float | None = None) -> None:
    path = store.status_path(agent_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _set_created_at(store: RunStore, record, timestamp: float) -> None:
    record.created_at = datetime.fromtimestamp(
        timestamp, tz=timezone.utc
    ).isoformat()
    store._write_record(record)


class FleetMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.paths = _paths(self.root)
        self.store = RunStore(self.paths)
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
        )
        self.clock = _Clock()
        self.send = _RecordingSend()
        self.monitor = FleetMonitor(
            self.store,
            self.send,
            clock=self.clock,
            interval=0.01,
            unrouted_verdict_realarm=300.0,
            review_gap_threshold=300.0,
            review_gap_realarm=600.0,
            staleness_threshold=1800.0,
            ownership_lock=self.supervisor._agent_lock,  # noqa: SLF001
        )

    async def asyncTearDown(self) -> None:
        await self.supervisor.close()
        self.tmp.cleanup()

    async def _spawn(
        self,
        agent_id: str,
        role: str,
        orch: str | None,
        *,
        provider: ProviderKind = ProviderKind.CODEX,
    ):
        return await self.supervisor.start_run(
            agent_id=agent_id,
            provider=provider,
            role=role,
            model=f"fixture-{provider.value}",
            effort="high" if provider is ProviderKind.CODEX else None,
            worktree=str(self.worktree),
            prompt=f"prompt for {agent_id}",
            orchestrator_id=orch,
        )

    def _graph(self, ticket: str, *, edges: list[dict], nodes: list[dict] | None = None) -> dict:
        return {
            "ticket": ticket,
            "orch": "wiki",
            "template": "wiki.implement",
            "created_at": "1970-01-01T00:00:00Z",
            "updated_at": "1970-01-01T00:00:00Z",
            "nodes": nodes or [{"id": ticket, "kind": "implement", "label": ticket}],
            "edges": edges,
            "composite_health": {},
        }

    def _graph_edge(self, kind: str, created_at: float, **extra: object) -> dict:
        edge = {
            "kind": kind,
            "from": extra.pop("from", "orch:wiki"),
            "to": extra.pop("to", "WIKI-900"),
            "payload": extra.pop("payload", {}),
            "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
        }
        edge.update(extra)
        return edge

    def _patch_graph(self, graph: dict):
        stack = ExitStack()
        stack.enter_context(
            mock.patch.multiple(
                workgraph,
                load_workgraph=mock.Mock(return_value=graph),
                load_snapshot=mock.Mock(return_value=None),
            )
        )
        stack.enter_context(
            mock.patch.object(workgraph.graph_lint, "validate_document", return_value=[])
        )
        return stack

    async def test_graph_health_blocking_no_reviewer_realarms_every_five_minutes(self) -> None:
        orch = await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-900", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-900",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        graph = self._graph(
            "WIKI-900",
            edges=[
                self._graph_edge(
                    "verdict",
                    self.clock.now,
                    **{
                        "from": "review",
                        "to": "orch:wiki",
                        "payload": {"findings": [{"id": "F-abc123", "severity": "BLOCKING"}]},
                    },
                )
            ],
        )
        with self._patch_graph(graph):
            first = await self.monitor.tick()
            self.assertEqual([n.event_type for n in first], ["graph-health"])
            self.assertEqual(first[0].orch_run_id, orch.run_id)

            self.clock.advance(299)
            self.assertEqual(
                [n for n in await self.monitor.tick() if n.event_type == "graph-health"],
                [],
            )
            self.clock.advance(1)
            second = await self.monitor.tick()
            self.assertEqual(
                len([n for n in second if n.event_type == "graph-health"]), 1
            )

    async def test_graph_health_suppresses_alarm_after_verdict_route(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-901", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-901",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        graph = self._graph(
            "WIKI-901",
            edges=[
                self._graph_edge(
                    "verdict",
                    self.clock.now - 1,
                    **{
                        "from": "review",
                        "to": "orch:wiki",
                        "payload": {"findings": [{"id": "F-abc123", "severity": "BLOCKING"}]},
                    },
                ),
                self._graph_edge(
                    "steer",
                    self.clock.now,
                    to="WIKI-901",
                    payload={"target_worker": "WIKI-901"},
                ),
            ],
        )
        with self._patch_graph(graph):
            self.assertEqual(
                [n for n in await self.monitor.tick() if n.event_type == "graph-health"],
                [],
            )
            self.clock.advance(899)
            self.assertEqual(
                [n for n in await self.monitor.tick() if n.event_type == "graph-health"],
                [],
            )
            self.clock.advance(1)
            self.assertEqual(
                len([n for n in await self.monitor.tick() if n.event_type == "graph-health"]),
                1,
            )

    async def test_graph_health_escalations_notify_and_use_typed_writer(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-902", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-902",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        old = self.clock.now - 1801
        nodes = [{"id": "WIKI-902", "kind": "implement", "label": "WIKI-902"}]
        graph = self._graph(
            "WIKI-902",
            nodes=nodes,
            edges=[self._graph_edge("spawn", old, to="WIKI-902")]
            + [
                self._graph_edge(
                    "verdict",
                    old + index,
                    **{
                        "from": "review",
                        "to": "orch:wiki",
                        "payload": {"findings": [], "sha": f"abc123{index}"},
                    },
                )
                for index in range(9)
            ],
        )
        escalations: list[dict] = []

        def record_escalation(**payload):
            escalations.append(payload)
            ack = Future()
            ack.set_result(None)
            return ack

        with self._patch_graph(graph), mock.patch(
            "backend.app.workgraph_service.record_escalation",
            side_effect=record_escalation,
        ):
            notes = await self.monitor.tick()

        self.assertEqual(
            {note.event_type for note in notes},
            {"graph-health-stall", "graph-health-iteration-cap"},
        )
        self.assertEqual({entry["target"] for entry in escalations}, {"henry"})
        self.assertEqual({entry["reason"] for entry in escalations}, {
            "node stall exceeded 1800s (1801s)",
            "iteration count exceeded cap 8 (9)",
        })

    async def test_escalation_append_failure_retries_before_notification(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-903", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-903",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        graph = self._graph(
            "WIKI-903",
            edges=[self._graph_edge("spawn", self.clock.now - 1801, to="WIKI-903")],
        )
        attempts = 0

        def record_escalation(**payload):
            nonlocal attempts
            attempts += 1
            ack = Future()
            if attempts == 1:
                ack.set_exception(RuntimeError("simulated append failure"))
            else:
                ack.set_result(None)
            return ack

        with self._patch_graph(graph), mock.patch(
            "backend.app.workgraph_service.record_escalation",
            side_effect=record_escalation,
        ):
            first = await self.monitor.tick()
            second = await self.monitor.tick()

        self.assertNotIn("graph-health-stall", {note.event_type for note in first})
        self.assertIn("graph-health-stall", {note.event_type for note in second})
        self.assertEqual(attempts, 2)

    async def test_escalation_episode_ids_reset_after_stall_recovery(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-904", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-904",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        graph = self._graph(
            "WIKI-904",
            edges=[self._graph_edge("spawn", self.clock.now - 1801, to="WIKI-904")],
        )
        requests: list[str] = []

        def record_escalation(**payload):
            requests.append(payload["request_id"])
            ack = Future()
            ack.set_result(None)
            return ack

        with self._patch_graph(graph), mock.patch(
            "backend.app.workgraph_service.record_escalation",
            side_effect=record_escalation,
        ):
            first = await self.monitor.tick()
            graph["edges"].append(
                self._graph_edge(
                    "steer",
                    self.clock.now,
                    to="WIKI-904",
                    payload={"target_worker": "WIKI-904"},
                )
            )
            await self.monitor.tick()
            self.clock.advance(1801)
            restarted = FleetMonitor(
                self.store,
                self.send,
                clock=self.clock,
                interval=0.01,
                graph_health_realarm=300.0,
                review_route_suppression=900.0,
                send_timeout=10.0,
                ownership_lock=self.supervisor._agent_lock,  # noqa: SLF001
            )
            second = await restarted.tick()

        first_note = next(note for note in first if note.event_type == "graph-health-stall")
        second_note = next(note for note in second if note.event_type == "graph-health-stall")
        self.assertNotEqual(first_note.dedupe_key, second_note.dedupe_key)
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0], requests[1])

    async def test_new_verdict_after_route_is_not_suppressed(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-905", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-905",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        graph = self._graph(
            "WIKI-905",
            edges=[
                self._graph_edge(
                    "verdict",
                    self.clock.now - 1,
                    **{"from": "review", "to": "orch:wiki", "payload": {}},
                ),
                self._graph_edge(
                    "steer",
                    self.clock.now,
                    to="WIKI-905",
                    payload={"target_worker": "WIKI-905"},
                ),
            ],
        )
        with self._patch_graph(graph):
            self.assertEqual(
                [n for n in await self.monitor.tick() if n.event_type == "graph-health"],
                [],
            )
            graph["edges"].append(
                self._graph_edge(
                    "verdict",
                    self.clock.now,
                    **{
                        "from": "review",
                        "to": "orch:wiki",
                        "payload": {
                            "findings": [{"id": "F-new123", "severity": "BLOCKING"}]
                        },
                    },
                )
            )
            notes = await self.monitor.tick()

        self.assertEqual(len([n for n in notes if n.event_type == "graph-health"]), 1)

    async def test_corrupt_hot_graph_falls_back_to_valid_snapshot(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        monitor = self.monitor
        snapshot = workgraph.create_workgraph(
            "WIKI-906",
            "wiki",
            created_at="1970-01-01T00:00:00Z",
        )
        with mock.patch.object(
            workgraph,
            "load_workgraph",
            side_effect=workgraph.WorkgraphCorruptError("damaged hot graph"),
        ), mock.patch.object(workgraph, "load_snapshot", return_value=snapshot):
            self.assertEqual(monitor._load_graph("WIKI-906"), snapshot)  # noqa: SLF001

    async def test_invalid_hot_graph_falls_back_and_alarms_degraded_health(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-907", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-907",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        snapshot = workgraph.create_workgraph(
            "WIKI-907",
            "wiki",
            created_at="1970-01-01T00:00:00Z",
        )
        with mock.patch.object(
            workgraph, "load_workgraph", return_value={}
        ) as hot, mock.patch.object(
            workgraph, "load_snapshot", return_value=snapshot
        ) as durable:
            first = await self.monitor.tick()
            self.assertIn(
                "graph-unavailable", {note.event_type for note in first}
            )
            self.assertNotIn("graph-health", {note.event_type for note in first})
            self.clock.advance(299)
            self.assertNotIn(
                "graph-unavailable",
                {note.event_type for note in await self.monitor.tick()},
            )
            self.clock.advance(1)
            self.assertIn(
                "graph-unavailable",
                {note.event_type for note in await self.monitor.tick()},
            )
        self.assertEqual(hot.call_count, 3)
        self.assertEqual(durable.call_count, 3)

    async def test_invalid_hot_and_snapshot_graph_alarm_without_phantom_health(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-908", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-908",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        with mock.patch.object(
            workgraph, "load_workgraph", return_value={"edges": "invalid"}
        ), mock.patch.object(
            workgraph, "load_snapshot", return_value={"edges": "invalid"}
        ):
            notes = await self.monitor.tick()

        event_types = {note.event_type for note in notes}
        self.assertIn("graph-unavailable", event_types)
        self.assertNotIn("graph-health", event_types)

    async def test_future_verdict_route_does_not_suppress_alarm(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-909", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-909",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        graph = self._graph(
            "WIKI-909",
            edges=[
                self._graph_edge(
                    "verdict",
                    self.clock.now - 1,
                    **{
                        "from": "review",
                        "to": "orch:wiki",
                        "payload": {
                            "findings": [{"id": "F-future", "severity": "BLOCKING"}]
                        },
                    },
                ),
                self._graph_edge(
                    "steer",
                    self.clock.now + 60,
                    to="WIKI-909",
                    payload={"target_worker": "WIKI-909"},
                ),
            ],
        )
        with self._patch_graph(graph):
            notes = await self.monitor.tick()

        self.assertEqual(
            len([note for note in notes if note.event_type == "graph-health"]), 1
        )

    async def test_fleet_and_writer_normalize_every_role_suffix(self) -> None:
        from backend.app import workgraph_service
        from backend.app.agent_runtime import fleet_monitor

        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        for suffix in ("REVIEW", "PLAN", "SIM", "AUDIT", "CANARY", "THERMO", "EVAL"):
            with self.subTest(suffix=suffix):
                agent_id = f"WIKI-910-{suffix}1"
                record = await self._spawn(agent_id, role="implement", orch="WIKI-ORCH")
                self.assertEqual(
                    fleet_monitor.base_ticket(agent_id),
                    workgraph_service.base_ticket(agent_id),
                )
                self.assertEqual(workgraph_service.base_ticket(agent_id), "WIKI-910")
                view = _WorkerView(
                    record=record,
                    status_state="working",
                    pr=None,
                    step="coding",
                    blocker=None,
                    status_mtime=None,
                )
                with mock.patch.object(
                    workgraph, "load_workgraph", return_value=None
                ) as load_hot, mock.patch.object(
                    workgraph, "load_snapshot", return_value=None
                ):
                    await self.monitor._process_graph_health(  # noqa: SLF001
                        [view], self.clock.now
                    )
                load_hot.assert_called_once_with(
                    "WIKI-910", self.store.paths.status_dir
                )

    async def test_status_file_transition_emits_one_message(self) -> None:
        orch = await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-100", role="implement", orch="WIKI-ORCH")
        _write_status(self.store, "WIKI-100", {"state": "working", "pr": None, "step": "coding", "blocker": None})

        # The first observation is an explicit none -> current transition.
        seed = await self.monitor.tick()
        seed_status = [n for n in seed if n.event_type == "status-transition"]
        self.assertEqual(seed_status, [])
        self.send.calls.clear()

        _write_status(
            self.store,
            "WIKI-100",
            {"state": "merge-ready", "pr": "https://gh/x/pull/1", "step": "PR open", "blocker": None},
        )
        notes = await self.monitor.tick()

        status_notes = [n for n in notes if n.event_type == "status-transition"]
        self.assertEqual(len(status_notes), 1, f"got: {status_notes}")
        note = status_notes[0]
        self.assertEqual(note.orch_run_id, orch.run_id)
        self.assertEqual(note.ticket, "WIKI-100")
        self.assertIn("working -> merge-ready", note.message)
        self.assertIn("PR open", note.message)
        self.assertIn("https://gh/x/pull/1", note.message)

        # Third tick with no change: no additional status-transition emit.
        again = await self.monitor.tick()
        self.assertEqual(
            [n for n in again if n.event_type == "status-transition"], []
        )

    async def test_no_change_between_ticks_emits_nothing(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-101", role="implement", orch="WIKI-ORCH")
        _write_status(self.store, "WIKI-101", {"state": "working", "pr": None, "step": "coding", "blocker": None})

        await self.monitor.tick()  # seed
        self.send.calls.clear()

        await self.monitor.tick()
        await self.monitor.tick()
        self.assertEqual(self.send.calls, [])

    async def test_runtime_none_to_working_is_silent(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-103", role="implement", orch="WIKI-ORCH")
        self.store.transition(worker.run_id, LifecycleState.WORKING, reason="tester")

        notes = await self.monitor.tick()

        self.assertEqual(
            [n for n in notes if n.event_type == "runtime-transition"], []
        )

    async def test_runtime_none_to_starting_is_silent(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-109", role="implement", orch="WIKI-ORCH")
        worker.state = LifecycleState.STARTING
        self.store._write_record(worker)

        notes = await self.monitor.tick()

        self.assertEqual(
            [n for n in notes if n.event_type == "runtime-transition"], []
        )

    async def test_runtime_working_to_idle_is_silent(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-104", role="implement", orch="WIKI-ORCH")
        self.store.transition(worker.run_id, LifecycleState.WORKING, reason="tester")
        await self.monitor.tick()

        self.store.transition(worker.run_id, LifecycleState.IDLE, reason="tester")
        notes = await self.monitor.tick()

        self.assertEqual(
            [n for n in notes if n.event_type == "runtime-transition"], []
        )

    async def test_runtime_working_to_blocked_emits(self) -> None:
        orch = await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-105", role="implement", orch="WIKI-ORCH")
        self.store.transition(worker.run_id, LifecycleState.WORKING, reason="tester")
        await self.monitor.tick()

        self.store.transition(worker.run_id, LifecycleState.BLOCKED, reason="tester")
        notes = await self.monitor.tick()
        runtime = [n for n in notes if n.event_type == "runtime-transition"]

        self.assertEqual(len(runtime), 1)
        self.assertEqual(runtime[0].orch_run_id, orch.run_id)
        self.assertIn("working -> blocked", runtime[0].message)

    async def test_runtime_working_to_interrupted_emits(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-111", role="implement", orch="WIKI-ORCH")
        self.store.transition(worker.run_id, LifecycleState.WORKING, reason="tester")
        await self.monitor.tick()

        self.store.transition(
            worker.run_id, LifecycleState.INTERRUPTED, reason="tester"
        )
        notes = await self.monitor.tick()

        runtime = [n for n in notes if n.event_type == "runtime-transition"]
        self.assertEqual(len(runtime), 1)
        self.assertIn("working -> interrupted", runtime[0].message)

        self.store.transition(worker.run_id, LifecycleState.WORKING, reason="tester")
        notes = await self.monitor.tick()
        self.assertEqual(
            [n for n in notes if n.event_type == "runtime-transition"], []
        )

        self.store.transition(worker.run_id, LifecycleState.BLOCKED, reason="tester")
        notes = await self.monitor.tick()
        runtime = [n for n in notes if n.event_type == "runtime-transition"]
        self.assertEqual(len(runtime), 1)
        self.assertIn("working -> blocked", runtime[0].message)

    async def test_runtime_working_to_dead_emits_once(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-112", role="implement", orch="WIKI-ORCH")
        self.store.transition(worker.run_id, LifecycleState.WORKING, reason="tester")
        await self.monitor.tick()

        self.store.transition(worker.run_id, LifecycleState.DEAD, reason="tester")
        notes = await self.monitor.tick()
        runtime = [n for n in notes if n.event_type == "runtime-transition"]

        self.assertEqual(len(runtime), 1)
        self.assertIn("working -> dead", runtime[0].message)
        self.assertEqual(
            [
                n
                for n in await self.monitor.tick()
                if n.event_type == "runtime-transition"
            ],
            [],
        )

    async def test_status_working_to_merge_ready_emits(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-106", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-106",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        self.assertEqual(
            [n for n in await self.monitor.tick() if n.event_type == "status-transition"],
            [],
        )

        _write_status(
            self.store,
            "WIKI-106",
            {"state": "merge-ready", "pr": "pr-1", "step": "ready", "blocker": None},
        )
        notes = await self.monitor.tick()

        status = [n for n in notes if n.event_type == "status-transition"]
        self.assertEqual(len(status), 1)
        self.assertIn("working -> merge-ready", status[0].message)

    async def test_status_none_to_working_is_silent(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-107", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-107",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )

        notes = await self.monitor.tick()

        self.assertEqual(
            [n for n in notes if n.event_type == "status-transition"], []
        )

    async def test_runtime_transition_advances_snapshot_even_when_silent(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-108", role="implement", orch="WIKI-ORCH")
        self.store.transition(worker.run_id, LifecycleState.WORKING, reason="tester")
        self.assertEqual(
            [n for n in await self.monitor.tick() if n.event_type == "runtime-transition"],
            [],
        )

        self.store.transition(worker.run_id, LifecycleState.IDLE, reason="tester")
        self.assertEqual(
            [n for n in await self.monitor.tick() if n.event_type == "runtime-transition"],
            [],
        )

        self.store.transition(worker.run_id, LifecycleState.BLOCKED, reason="tester")
        notes = await self.monitor.tick()
        runtime = [n for n in notes if n.event_type == "runtime-transition"]

        self.assertEqual(len(runtime), 1)
        self.assertIn("idle -> blocked", runtime[0].message)

    async def test_runtime_state_transition_emits(self) -> None:
        orch = await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-102", role="implement", orch="WIKI-ORCH")
        # No status file. Seed initial runtime state.
        await self.monitor.tick()

        # Force a runtime lifecycle transition directly on the store.
        self.store.transition(worker.run_id, LifecycleState.BLOCKED, reason="tester")

        notes = await self.monitor.tick()
        runtime = [n for n in notes if n.event_type == "runtime-transition"]
        self.assertEqual(len(runtime), 1)
        self.assertEqual(runtime[0].orch_run_id, orch.run_id)
        self.assertIn("runtime", runtime[0].message)
        self.assertIn("blocked", runtime[0].message)

    async def test_initial_merge_ready_and_waiting_approval_are_emitted(self) -> None:
        orch = await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1100", role="implement", orch="WIKI-ORCH")
        waiting = await self._spawn(
            "WIKI-1101", role="implement", orch="WIKI-ORCH"
        )
        self.store.transition(
            waiting.run_id,
            LifecycleState.WAITING_APPROVAL,
            reason="tester",
        )
        _write_status(
            self.store,
            "WIKI-1100",
            {
                "state": "merge-ready",
                "pr": "https://gh/x/pull/11",
                "step": "PR open",
                "blocker": None,
            },
        )
        _write_status(
            self.store,
            "WIKI-1101",
            {
                "state": "waiting-approval",
                "pr": None,
                "step": "approval",
                "blocker": "needs approval",
            },
        )

        notes = await self.monitor.tick()
        status = {note.ticket: note for note in notes if note.event_type == "status-transition"}
        runtime = {note.ticket: note for note in notes if note.event_type == "runtime-transition"}
        self.assertIn("none -> merge-ready", status["WIKI-1100"].message)
        self.assertNotIn("WIKI-1101", status)
        self.assertIn("none -> waiting-approval", runtime["WIKI-1101"].message)
        self.assertEqual(status["WIKI-1100"].orch_run_id, orch.run_id)

    async def test_failed_transition_retries_after_delivery_recovers(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1200", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-1200",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        await self.monitor.tick()
        self.send.calls.clear()

        _write_status(
            self.store,
            "WIKI-1200",
            {"state": "merge-ready", "pr": "pr-1", "step": "ready", "blocker": None},
        )
        self.send.fail_with = RuntimeError("adapter gone")
        self.assertEqual(await self.monitor.tick(), [])
        self.assertEqual(len(self.send.calls), 1)

        self.send.fail_with = None
        notes = await self.monitor.tick()
        self.assertEqual(
            len([note for note in notes if note.event_type == "status-transition"]),
            1,
        )
        self.assertEqual(len(self.send.calls), 2)

    async def test_transition_payload_is_local_dedupe_context(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1300", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-1300",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        await self.monitor.tick()
        self.send.calls.clear()

        for payload in (
            {"state": "merge-ready", "pr": "pr-1", "step": "ready-1", "blocker": None},
            {"state": "working", "pr": None, "step": "rework", "blocker": None},
            {"state": "merge-ready", "pr": "pr-2", "step": "ready-2", "blocker": None},
        ):
            _write_status(self.store, "WIKI-1300", payload)
            await self.monitor.tick()

        status_calls = [call for call in self.send.calls if "status " in call[1]]
        self.assertEqual(len(status_calls), 2)
        self.assertIn("pr-1", status_calls[0][1])
        self.assertIn("pr-2", status_calls[1][1])

        await self.monitor.tick()
        status_calls_after = [call for call in self.send.calls if "status " in call[1]]
        self.assertEqual(len(status_calls_after), 2)

    async def test_same_state_context_changes_do_not_emit(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1850", role="implement", orch="WIKI-ORCH")
        initial = {
            "state": "working",
            "pr": None,
            "step": "coding",
            "blocker": None,
        }
        _write_status(self.store, "WIKI-1850", initial)
        await self.monitor.tick()
        self.send.calls.clear()

        contexts = (
            {**initial, "step": "testing"},
            {**initial, "step": "testing", "pr": "pr-18"},
            {
                **initial,
                "step": "testing",
                "pr": "pr-18",
                "blocker": "waiting on test",
            },
        )
        for payload in contexts:
            _write_status(self.store, "WIKI-1850", payload)
            notes = await self.monitor.tick()
            self.assertEqual(
                [note for note in notes if note.event_type == "status-transition"],
                [],
            )
        self.assertEqual(self.send.calls, [])

        # The context that changed silently still rides along on the next
        # real state transition.
        _write_status(
            self.store,
            "WIKI-1850",
            {"state": "blocked", "pr": "pr-18", "step": "testing", "blocker": "waiting on test"},
        )
        notes = await self.monitor.tick()
        status = [note for note in notes if note.event_type == "status-transition"]
        self.assertEqual(len(status), 1)
        self.assertIn("working -> blocked", status[0].message)
        self.assertIn("pr: pr-18", status[0].message)
        self.assertIn("blocker: waiting on test", status[0].message)

    async def test_delivery_timeout_retries_same_occurrence_token(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1865", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-1865",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
        )
        first_started = asyncio.Event()
        calls: list[tuple[str, str, str | None, str | None]] = []
        fail_next = False

        async def accept_then_timeout(
            run_id: str,
            message: str,
            dedupe_key: str | None,
            source: str | None = None,
        ) -> dict:
            nonlocal fail_next
            calls.append((run_id, message, dedupe_key, source))
            if fail_next:
                fail_next = False
                first_started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError as exc:
                    raise TimeoutError("ack timeout after acceptance") from exc
            return {"status": "sent"}

        monitor = FleetMonitor(
            self.store,
            accept_then_timeout,
            clock=self.clock,
            send_timeout=0.01,
        )
        await monitor.tick()
        fail_next = True
        _write_status(
            self.store,
            "WIKI-1865",
            {"state": "merge-ready", "pr": None, "step": "testing", "blocker": None},
        )
        self.assertEqual(await monitor.tick(), [])
        await asyncio.wait_for(first_started.wait(), timeout=1)
        notes = await monitor.tick()
        self.assertEqual(
            [note.event_type for note in notes],
            ["status-transition"],
        )
        transition_calls = [call for call in calls if "step: testing" in call[1]]
        self.assertEqual(len(transition_calls), 2)
        self.assertIsNotNone(transition_calls[0][2])
        self.assertEqual(transition_calls[0][2], transition_calls[1][2])
        self.assertLessEqual(len(transition_calls[0][2] or ""), 200)

    async def test_repeated_status_context_cycle_emits_each_occurrence(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1870", role="implement", orch="WIKI-ORCH")
        context_a = {
            "state": "working",
            "pr": None,
            "step": "coding",
            "blocker": None,
        }
        context_b = {**context_a, "state": "merge-ready", "step": "testing"}
        _write_status(self.store, "WIKI-1870", context_a)
        await self.monitor.tick()
        self.send.calls.clear()

        emitted: list[Notification] = []
        for payload in (context_b, context_a, context_b):
            _write_status(self.store, "WIKI-1870", payload)
            emitted.extend(
                note
                for note in await self.monitor.tick()
                if note.event_type == "status-transition"
            )

        self.assertEqual(len(emitted), 2)
        self.assertIn("step: testing", emitted[0].message)
        self.assertIn("step: testing", emitted[1].message)

    async def test_repeated_runtime_transition_cycle_emits_each_occurrence(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-1880", role="implement", orch="WIKI-ORCH")
        await self.monitor.tick()
        self.send.calls.clear()

        emitted: list[Notification] = []
        for state in (
            LifecycleState.BLOCKED,
            LifecycleState.WORKING,
            LifecycleState.BLOCKED,
        ):
            self.store.transition(worker.run_id, state, reason="cycle")
            emitted.extend(
                note
                for note in await self.monitor.tick()
                if note.event_type == "runtime-transition"
            )

        self.assertEqual(len(emitted), 2)
        self.assertIn("idle -> blocked", emitted[0].message)
        self.assertIn("working -> blocked", emitted[1].message)

    async def test_run_creation_resets_prior_status_before_fresh_rewrite(self) -> None:
        prior_payload = {
            "state": "merge-ready",
            "pr": "old-pr",
            "step": "old step",
            "blocker": None,
        }
        _write_status(
            self.store,
            "WIKI-1890",
            prior_payload,
            mtime=time.time() - 3600,
        )
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1890", role="implement", orch="WIKI-ORCH")
        self.assertFalse(self.store.status_path("WIKI-1890").exists())

        first = await self.monitor.tick()
        self.assertEqual(
            [note for note in first if note.event_type == "status-transition"],
            [],
        )
        self.assertNotIn("old-pr", "\n".join(note.message for note in first))

        _write_status(
            self.store,
            "WIKI-1890",
            prior_payload,
            mtime=time.time() + 1,
        )
        second = await self.monitor.tick()
        status = [
            note for note in second if note.event_type == "status-transition"
        ]
        self.assertEqual(len(status), 1)
        self.assertIn("none -> merge-ready", status[0].message)
        self.assertIn("old-pr", status[0].message)

    async def test_replace_resets_prior_status_before_new_run_is_current(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        old = await self._spawn(
            "WIKI-1893", role="implement", orch="WIKI-ORCH"
        )
        _write_status(
            self.store,
            "WIKI-1893",
            {
                "state": "merge-ready",
                "pr": "old-replace-pr",
                "step": "old replacement",
                "blocker": None,
            },
        )

        replacement = await self.supervisor.replace(
            old.run_id, "replacement prompt for WIKI-1893"
        )
        self.assertEqual(self.store.current_run_id("WIKI-1893"), replacement.run_id)
        self.assertFalse(self.store.status_path("WIKI-1893").exists())

        monitor = FleetMonitor(
            self.store,
            self.send,
            clock=self.clock,
            interval=0.01,
            unrouted_verdict_realarm=300.0,
            review_gap_threshold=300.0,
            review_gap_realarm=600.0,
            staleness_threshold=1800.0,
        )
        notes = await monitor.tick()
        status = [note for note in notes if note.event_type == "status-transition"]
        self.assertEqual(status, [])
        self.assertNotIn("old-replace-pr", "\n".join(note.message for note in notes))

    async def test_replace_accepts_immediate_status_after_reset_with_coarse_and_backward_clock(
        self,
    ) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        for agent_id, mtime in (
            ("WIKI-1894", 0.0),
            ("WIKI-1895", time.time() - 3600),
        ):
            old = await self._spawn(agent_id, role="implement", orch="WIKI-ORCH")
            _write_status(
                self.store,
                agent_id,
                {
                    "state": "working",
                    "pr": "old-replace-pr",
                    "step": "old replacement",
                    "blocker": None,
                },
            )
            replacement = await self.supervisor.replace(
                old.run_id, f"replacement prompt for {agent_id}"
            )
            created_at = datetime.fromisoformat(replacement.created_at).timestamp()
            fresh_mtime = int(created_at) if mtime == 0.0 else mtime
            _write_status(
                self.store,
                agent_id,
                {
                    "state": "merge-ready",
                    "pr": f"fresh-{agent_id}",
                    "step": "fresh replacement",
                    "blocker": None,
                },
                mtime=fresh_mtime,
            )

            notes = await self.monitor.tick()
            status = [
                note for note in notes if note.event_type == "status-transition"
            ]
            self.assertEqual(len(status), 1, notes)
            self.assertIn("none -> merge-ready", status[0].message)
            self.assertIn(f"fresh-{agent_id}", status[0].message)
            self.assertEqual(
                [
                    note
                    for note in await self.monitor.tick()
                    if note.event_type == "status-transition"
                ],
                [],
            )

    async def test_replace_resets_only_after_old_provider_quiesces(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        for index, mtime_kind in enumerate(("coarse", "backward"), start=1):
            agent_id = f"WIKI-189{5 + index}"
            old = await self._spawn(agent_id, role="implement", orch="WIKI-ORCH")
            adapter = self.supervisor.adapters[old.run_id]
            stop_completed = asyncio.Event()
            allow_stop_return = asyncio.Event()
            reset_after_stop: list[bool] = []
            original_stop = adapter.stop
            original_reset = self.supervisor._reset_status_for_replacement  # noqa: SLF001

            async def delayed_stop():
                status = await original_stop()
                stop_completed.set()
                await allow_stop_return.wait()
                return status

            def checked_reset(agent: str) -> None:
                reset_after_stop.append(stop_completed.is_set())
                original_reset(agent)

            mtime = (
                int(datetime.fromisoformat(old.created_at).timestamp())
                if mtime_kind == "coarse"
                else time.time() - 3600
            )
            with (
                mock.patch.object(adapter, "stop", side_effect=delayed_stop),
                mock.patch.object(
                    self.supervisor,
                    "_reset_status_for_replacement",  # noqa: SLF001
                    side_effect=checked_reset,
                ),
            ):
                replacement_task = asyncio.create_task(
                    self.supervisor.replace(old.run_id, f"replacement {agent_id}")
                )
                await asyncio.wait_for(stop_completed.wait(), timeout=2)
                _write_status(
                    self.store,
                    agent_id,
                    {
                        "state": "merge-ready",
                        "pr": f"old-concurrent-{agent_id}",
                        "step": "old provider write",
                        "blocker": None,
                    },
                    mtime=mtime,
                )
                collected = threading.Event()
                original_collect = self.monitor._collect_views  # noqa: SLF001

                def collect_with_fence_probe():
                    views = original_collect()
                    collected.set()
                    return views

                with mock.patch.object(
                    self.monitor,
                    "_collect_views",  # noqa: SLF001
                    side_effect=collect_with_fence_probe,
                ):
                    tick = asyncio.create_task(self.monitor.tick())
                    await asyncio.wait_for(
                        asyncio.to_thread(collected.wait, 2), timeout=2
                    )
                    allow_stop_return.set()
                    gap_notes = await asyncio.wait_for(tick, timeout=2)
                self.assertNotIn(
                    f"old-concurrent-{agent_id}",
                    "\n".join(note.message for note in gap_notes),
                )
                replacement = await asyncio.wait_for(replacement_task, timeout=2)

            self.assertEqual(reset_after_stop, [True])
            self.assertEqual(
                self.store.current_run_id(agent_id), replacement.run_id
            )
            self.assertFalse(self.store.status_path(agent_id).exists())
            post_notes = await FleetMonitor(
                self.store,
                self.send,
                clock=self.clock,
                interval=0.01,
                unrouted_verdict_realarm=300.0,
                review_gap_threshold=300.0,
                review_gap_realarm=600.0,
                staleness_threshold=1800.0,
            ).tick()
            self.assertNotIn(
                f"old-concurrent-{agent_id}",
                "\n".join(note.message for note in post_notes),
            )

    async def test_earliest_replacement_status_is_current_and_emits_once(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        for index, mtime_kind in enumerate(("coarse", "backward"), start=1):
            agent_id = f"WIKI-190{index}"
            old = await self._spawn(agent_id, role="implement", orch="WIKI-ORCH")
            adapter = self.supervisor.adapters[old.run_id]
            wrote_status = asyncio.Event()
            allow_start = asyncio.Event()
            monitor = FleetMonitor(
                self.store,
                self.send,
                clock=self.clock,
                interval=0.01,
                unrouted_verdict_realarm=300.0,
                review_gap_threshold=300.0,
                review_gap_realarm=600.0,
                staleness_threshold=1800.0,
            )
            first_notes: list[Notification] = []
            original_replace = adapter.replace

            async def earliest_replace(
                prompt: str,
                model: str | None = None,
                effort: str | None = None,
            ):
                _write_status(
                    self.store,
                    agent_id,
                    {
                        "state": "merge-ready",
                        "pr": f"fresh-earliest-{agent_id}",
                        "step": "fresh provider write",
                        "blocker": None,
                    },
                    mtime=(
                        int(time.time())
                        if mtime_kind == "coarse"
                        else time.time() - 3600
                    ),
                )
                wrote_status.set()
                first_notes.extend(await monitor.tick())
                await allow_start.wait()
                return await original_replace(prompt, model, effort)

            with mock.patch.object(
                adapter, "replace", side_effect=earliest_replace
            ):
                replacement_task = asyncio.create_task(
                    self.supervisor.replace(old.run_id, f"replacement {agent_id}")
                )
                await asyncio.wait_for(wrote_status.wait(), timeout=2)
                self.assertNotEqual(self.store.current_run_id(agent_id), old.run_id)
                allow_start.set()
                replacement = await asyncio.wait_for(replacement_task, timeout=2)

            after_notes = await monitor.tick()
            status_notes = [
                note
                for note in [*first_notes, *after_notes]
                if note.event_type == "status-transition" and note.ticket == agent_id
            ]
            self.assertEqual(len(status_notes), 1, status_notes)
            self.assertIn(
                f"fresh-earliest-{agent_id}", status_notes[0].message
            )
            self.assertEqual(self.store.current_run_id(agent_id), replacement.run_id)

    async def test_post_spawn_status_is_accepted_after_reset_with_coarse_clock(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-1891", role="implement", orch="WIKI-ORCH")
        created_at = datetime.fromisoformat(worker.created_at).timestamp()
        _write_status(
            self.store,
            "WIKI-1891",
            {"state": "merge-ready", "pr": "coarse-pr", "step": "ready", "blocker": None},
            mtime=int(created_at),
        )

        notes = await self.monitor.tick()
        status = [
            note for note in notes if note.event_type == "status-transition"
        ]
        self.assertEqual(len(status), 1)
        self.assertIn("coarse-pr", status[0].message)

    async def test_post_spawn_status_is_accepted_after_reset_with_backward_clock(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1892", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-1892",
            {"state": "merge-ready", "pr": "rollback-pr", "step": "ready", "blocker": None},
            mtime=time.time() - 3600,
        )

        notes = await self.monitor.tick()
        status = [
            note for note in notes if note.event_type == "status-transition"
        ]
        self.assertEqual(len(status), 1)
        self.assertIn("rollback-pr", status[0].message)

    async def test_archive_respawn_same_id_resets_run_dedupe_state(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        first = await self._spawn("WIKI-1800", role="implement", orch="WIKI-ORCH")
        payload = {
            "state": "merge-ready",
            "pr": "pr-18",
            "step": "ready",
            "blocker": None,
        }
        _write_status(self.store, "WIKI-1800", payload)
        first_notes = await self.monitor.tick()
        self.assertTrue(
            any(
                note.ticket == "WIKI-1800"
                and note.event_type == "status-transition"
                for note in first_notes
            )
        )
        self.assertEqual(
            self.monitor._snapshots["WIKI-1800"].run_id,
            first.run_id,
        )

        await self.supervisor.archive(first.run_id, outcome="replaced")
        await self.monitor.tick()
        self.assertEqual(self.monitor._snapshots, {})
        self.assertEqual(self.monitor._sent_dedupe_keys, set())

        second = await self._spawn("WIKI-1800", role="implement", orch="WIKI-ORCH")
        self.assertNotEqual(first.run_id, second.run_id)
        _write_status(self.store, "WIKI-1800", payload)
        second_notes = await self.monitor.tick()
        status_notes = [
            note
            for note in second_notes
            if note.ticket == "WIKI-1800"
            and note.event_type == "status-transition"
        ]
        self.assertEqual(len(status_notes), 1)
        self.assertIn("none -> merge-ready", status_notes[0].message)
        self.assertTrue(
            all(
                identity[1] == second.run_id
                for identity in self.monitor._sent_dedupe_keys
            )
        )

    async def test_slow_orchestrator_does_not_block_another_destination(self) -> None:
        orch_a = await self._spawn("ORCH-SLOW", role="orchestrator", orch=None)
        orch_b = await self._spawn("ORCH-FAST", role="orchestrator", orch=None)
        await self._spawn("WIKI-1400", role="implement", orch="ORCH-SLOW")
        await self._spawn("WIKI-1401", role="implement", orch="ORCH-FAST")
        for agent_id in ("WIKI-1400", "WIKI-1401"):
            _write_status(
                self.store,
                agent_id,
                {"state": "blocked", "pr": None, "step": "blocked", "blocker": "tester"},
            )

        send = _SelectiveSend()
        send.slow_run_id = orch_a.run_id
        monitor = FleetMonitor(
            self.store,
            send,
            clock=self.clock,
            monotonic_clock=self.clock,
            send_timeout=0.5,
        )
        tick = asyncio.create_task(monitor.tick())
        await asyncio.wait_for(send.started.wait(), timeout=10.0)
        await asyncio.wait_for(send.fast_received.wait(), timeout=10.0)
        self.assertTrue(any(run_id == orch_b.run_id for run_id, *_ in send.calls))
        send.release.set()
        await asyncio.wait_for(tick, timeout=10.0)

    async def test_backward_clock_resets_graph_unavailable_cadence(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn(
            "WIKI-1500", role="implement", orch="WIKI-ORCH"
        )
        wall_clock = _Clock(self.clock.now)
        monotonic_clock = _Clock(5_000.0)
        _set_created_at(self.store, worker, wall_clock.now - 1)
        _write_status(
            self.store,
            "WIKI-1500",
            {"state": "merge-ready", "pr": "pr", "step": "ready", "blocker": None},
            mtime=wall_clock.now,
        )
        monitor = FleetMonitor(
            self.store,
            self.send,
            clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        await monitor.tick()
        wall_clock.advance(300)
        self.assertEqual(
            len([note for note in await monitor.tick() if note.event_type == "graph-unavailable"]),
            1,
        )
        wall_clock.advance(-1)
        self.assertEqual(
            len([note for note in await monitor.tick() if note.event_type == "graph-unavailable"]),
            1,
        )

    async def test_plan_worker_gets_graph_unavailable_alarm(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-1600", role="plan", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-1600",
            {"state": "merge-ready", "pr": "pr", "step": "ready", "blocker": None},
        )
        first = await self.monitor.tick()
        self.assertEqual(
            len([note for note in first if note.event_type == "graph-unavailable"]),
            1,
        )
        self.clock.advance(301)
        self.assertEqual(
            len([note for note in await self.monitor.tick() if note.event_type == "graph-unavailable"]),
            1,
        )

    async def test_runtime_transition_includes_status_context(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-1700", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-1700",
            {
                "state": "working",
                "pr": "pr-17",
                "step": "coding",
                "blocker": "waiting on test",
            },
        )
        await self.monitor.tick()
        self.store.transition(worker.run_id, LifecycleState.BLOCKED, reason="tester")
        notes = await self.monitor.tick()
        runtime = [note for note in notes if note.event_type == "runtime-transition"]
        self.assertEqual(len(runtime), 1)
        self.assertEqual(
            runtime[0].message,
            "[fleet] WIKI-1700 (implement): runtime idle -> blocked | "
            "step: coding | pr: pr-17 | blocker: waiting on test",
        )

    async def test_orchestrator_scoping_isolates_workers(self) -> None:
        orch_a = await self._spawn("ORCH-A", role="orchestrator", orch=None)
        orch_b = await self._spawn("ORCH-B", role="orchestrator", orch=None)
        await self._spawn("WIKI-200", role="implement", orch="ORCH-A")
        _write_status(self.store, "WIKI-200", {"state": "working", "pr": None, "step": "s", "blocker": None})

        await self.monitor.tick()  # seed
        _write_status(
            self.store,
            "WIKI-200",
            {"state": "merge-ready", "pr": "u", "step": "ready", "blocker": None},
        )
        notes = await self.monitor.tick()

        self.assertTrue(notes, "expected at least one notification")
        for note in notes:
            self.assertEqual(note.orch_run_id, orch_a.run_id)
            self.assertNotEqual(note.orch_run_id, orch_b.run_id)

    async def test_worker_without_orchestrator_id_is_skipped(self) -> None:
        await self._spawn("WIKI-300", role="implement", orch=None)
        _write_status(self.store, "WIKI-300", {"state": "working", "pr": None, "step": "s", "blocker": None})
        await self.monitor.tick()
        _write_status(
            self.store,
            "WIKI-300",
            {"state": "merge-ready", "pr": "u", "step": "s2", "blocker": None},
        )
        notes = await self.monitor.tick()
        self.assertEqual(notes, [])

    async def test_terminal_worker_emits_once_then_is_skipped(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-400", role="implement", orch="WIKI-ORCH")
        await self.monitor.tick()

        # Move worker to terminal DEAD.
        self.store.transition(worker.run_id, LifecycleState.DEAD, reason="tester")
        notes = await self.monitor.tick()
        runtime = [n for n in notes if n.event_type == "runtime-transition"]
        self.assertEqual(len(runtime), 1)
        self.assertIn("idle -> dead", runtime[0].message)

        self.assertEqual(await self.monitor.tick(), [])

    async def test_unrouted_verdict_realarm_every_5min(self) -> None:
        orch = await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-500-REVIEW1", role="review", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-500-REVIEW1",
            {"state": "working", "pr": None, "step": "reviewing", "blocker": None},
        )
        await self.monitor.tick()  # seed

        # Reviewer produces a MERGE-READY verdict in its step.
        _write_status(
            self.store,
            "WIKI-500-REVIEW1",
            {
                "state": "working",
                "pr": None,
                "step": "MERGE-READY: https://gh/x/pull/9",
                "blocker": None,
            },
        )
        first = await self.monitor.tick()
        verdicts = [n for n in first if n.event_type == "unrouted-verdict"]
        self.assertEqual(len(verdicts), 1)
        self.assertIn("unrouted verdict", verdicts[0].message)

        # Immediate re-tick: no new alarm.
        self.clock.advance(60)
        second = await self.monitor.tick()
        self.assertEqual([n for n in second if n.event_type == "unrouted-verdict"], [])

        # 5 minutes later: re-alarm fires.
        self.clock.advance(300)
        third = await self.monitor.tick()
        third_v = [n for n in third if n.event_type == "unrouted-verdict"]
        self.assertEqual(len(third_v), 1)

    async def test_graph_unavailable_alarm_realarms_every_five_minutes(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-600", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-600",
            {"state": "merge-ready", "pr": "u", "step": "ready", "blocker": None},
        )

        first = await self.monitor.tick()
        self.assertEqual(
            len([n for n in first if n.event_type == "graph-unavailable"]),
            1,
        )
        self.send.calls.clear()

        self.clock.advance(120)
        early = await self.monitor.tick()
        self.assertEqual([n for n in early if n.event_type == "graph-unavailable"], [])

        self.clock.advance(200)
        second = await self.monitor.tick()
        self.assertEqual(
            len([n for n in second if n.event_type == "graph-unavailable"]), 1
        )

        self.clock.advance(1)
        mid = await self.monitor.tick()
        self.assertEqual([n for n in mid if n.event_type == "graph-unavailable"], [])

        self.clock.advance(299)
        late = await self.monitor.tick()
        self.assertEqual(
            len([n for n in late if n.event_type == "graph-unavailable"]), 1
        )

    async def test_graph_unavailable_alarm_is_ticket_level(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-700", role="implement", orch="WIKI-ORCH")
        await self._spawn("WIKI-700-REVIEW1", role="review", orch="WIKI-ORCH")
        _write_status(
            self.store,
            "WIKI-700",
            {"state": "merge-ready", "pr": "u", "step": "ready", "blocker": None},
        )
        _write_status(
            self.store,
            "WIKI-700-REVIEW1",
            {"state": "working", "pr": None, "step": "reviewing", "blocker": None},
        )

        await self.monitor.tick()  # seed
        self.clock.advance(3600)
        notes = await self.monitor.tick()
        self.assertEqual(len([n for n in notes if n.event_type == "graph-unavailable"]), 1)

    async def test_staleness_alarm_after_30_min_silent(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        worker = await self._spawn("WIKI-800", role="implement", orch="WIKI-ORCH")

        # Write status ~35 minutes ago. clock starts at 1_000_000.
        stale_mtime = self.clock.now - 2100
        _set_created_at(self.store, worker, stale_mtime - 1)
        _write_status(
            self.store,
            "WIKI-800",
            {"state": "working", "pr": None, "step": "coding", "blocker": None},
            mtime=stale_mtime,
        )

        # First tick seeds without emitting a status transition, but staleness
        # can still fire because it is not a transition detector.
        first = await self.monitor.tick()
        stale = [n for n in first if n.event_type == "staleness"]
        self.assertEqual(len(stale), 1)
        self.assertIn("silent", stale[0].message)
        self.assertIn("state=working", stale[0].message)

        # Same mtime on next tick: no duplicate alarm.
        self.clock.advance(60)
        second = await self.monitor.tick()
        self.assertEqual([n for n in second if n.event_type == "staleness"], [])

    async def test_send_failure_does_not_break_loop(self) -> None:
        await self._spawn("WIKI-ORCH", role="orchestrator", orch=None)
        await self._spawn("WIKI-900", role="implement", orch="WIKI-ORCH")
        _write_status(
            self.store, "WIKI-900", {"state": "working", "pr": None, "step": "s", "blocker": None}
        )
        await self.monitor.tick()  # seed
        self.send.calls.clear()
        self.send.fail_with = RuntimeError("adapter gone")

        _write_status(
            self.store,
            "WIKI-900",
            {"state": "merge-ready", "pr": "u", "step": "ready", "blocker": None},
        )
        notes = await self.monitor.tick()  # must not raise
        # No successful notification produced; but send_now was called.
        self.assertEqual(notes, [])
        self.assertEqual(len(self.send.calls), 1)

    async def test_missing_orch_run_swallowed(self) -> None:
        # Worker references an orchestrator agent_id that doesn't exist.
        await self._spawn("WIKI-1000", role="implement", orch="GHOST-ORCH")
        _write_status(
            self.store, "WIKI-1000", {"state": "working", "pr": None, "step": "s", "blocker": None}
        )
        await self.monitor.tick()  # seed
        _write_status(
            self.store,
            "WIKI-1000",
            {"state": "merge-ready", "pr": "u", "step": "ready", "blocker": None},
        )
        notes = await self.monitor.tick()
        self.assertEqual(notes, [])
        self.assertEqual(self.send.calls, [])


if __name__ == "__main__":
    unittest.main()
