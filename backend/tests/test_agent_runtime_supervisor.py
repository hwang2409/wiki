from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.client import SupervisorClient, SupervisorUnavailable
from backend.app.agent_runtime.fake import CodexFixtureAdapter, FixtureAdapterFactory
from backend.app.agent_runtime.protocol import UnixSupervisorServer
from backend.app.agent_runtime.provider import ProviderEvent
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor, resolve_safe_worktree
from backend.app.agent_runtime.types import LifecycleState, ProviderKind, RunRecord


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


def _paths(root: Path) -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=root / "runtime",
        socket_path=root / "runtime" / "supervisor.sock",
        registry_path=root / "isolated-registry.json",
    )


async def _wait_for_events(store: RunStore, run_id: str, minimum: int) -> RunRecord:
    for _ in range(200):
        record = store.get(run_id)
        if record.raw_event_count >= minimum:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {minimum} raw events")


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self) -> None:
        await self.supervisor.close()
        self.tmp.cleanup()

    async def test_mixed_fake_fleet_persists_raw_before_normalized(self) -> None:
        codex = await self.supervisor.start_run(
            agent_id="WIKI-CODEX",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-CODEX",
            orchestrator_id="wiki-dev",
        )
        claude = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-CLAUDE",
            orchestrator_id="wiki-dev",
        )
        codex = await _wait_for_events(self.store, codex.run_id, 10)
        claude = await _wait_for_events(self.store, claude.run_id, 10)

        self.assertGreater(codex.normalized_event_count, 0)
        self.assertEqual(codex.raw_event_count, codex.normalized_event_count)
        self.assertGreater(claude.normalized_event_count, 0)
        self.assertEqual(claude.raw_event_count, claude.normalized_event_count)
        self.assertGreater(codex.disposition_counts["rendered"], 0)
        self.assertGreater(codex.disposition_counts["ignored"], 0)
        self.assertGreater(claude.disposition_counts["rendered"], 0)

    async def test_message_contract_and_sse_event_shape_remain_stable(self) -> None:
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-42",
        )
        event = await asyncio.wait_for(queue.get(), timeout=2)
        self.assertEqual(event, {"type": "agents", "tickets": ["WIKI-42"], "surface": "agents"})

        sent = await self.supervisor.send_now(record.run_id, "steer now")
        self.assertEqual(sent, {"status": "sent"})
        queued = await self.supervisor.send_on_idle(record.run_id, "send after idle")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["position"], 1)
        self.assertEqual(queued["messages"][0]["text"], "send after idle")

        session_events = []
        for _ in range(40):
            event = await asyncio.wait_for(queue.get(), timeout=2)
            session_events.append(event)
            if {item.get("surface") for item in session_events} >= {"session", "queue"}:
                break
        self.assertIn(
            {"type": "session", "ticket": "WIKI-42", "surface": "session"},
            session_events,
        )
        self.assertIn(
            {"type": "session", "ticket": "WIKI-42", "surface": "queue"},
            session_events,
        )
        self.supervisor.unsubscribe(queue)

    async def test_on_idle_delivery_failure_keeps_durable_message(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-42",
        )
        adapter = self.supervisor.adapters[record.run_id]
        with mock.patch.object(
            adapter,
            "send_on_idle",
            side_effect=RuntimeError("fixture delivery failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                await self.supervisor.send_on_idle(record.run_id, "do not lose this")
        queued = self.store.get(record.run_id).queued_messages
        self.assertEqual([item["text"] for item in queued], ["do not lose this"])

    async def test_normalization_failure_keeps_raw_event_and_run_alive(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-42",
        )
        record = await _wait_for_events(self.store, record.run_id, 10)
        before_raw = record.raw_event_count
        before_normalized = record.normalized_event_count
        adapter = self.supervisor.adapters[record.run_id]
        with mock.patch(
            "backend.app.agent_runtime.supervisor.normalize_provider_event",
            side_effect=RuntimeError("fixture normalizer failure"),
        ):
            await self.supervisor._handle_provider_event(  # noqa: SLF001 - ordering contract test
                record.run_id,
                adapter,
                ProviderEvent(ProviderKind.CODEX, {"method": "fixture/unknown"}),
            )
        record = self.store.get(record.run_id)
        self.assertEqual(record.raw_event_count, before_raw + 1)
        self.assertEqual(record.normalized_event_count, before_normalized + 1)
        self.assertEqual(record.state, LifecycleState.IDLE)
        self.assertEqual(self.store.read_normalized_events(record.run_id)[-1]["disposition"], "unknown")

    async def test_replace_creates_new_run_and_stale_run_never_recovers(self) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original ticket WIKI-42 prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        with mock.patch.object(adapter, "replace", wraps=adapter.replace) as provider_replace:
            replacement = await self.supervisor.replace(
                old.run_id,
                "Replacement ticket WIKI-42 prompt",
            )
        provider_replace.assert_awaited_once_with("Replacement ticket WIKI-42 prompt", None)
        self.assertNotEqual(replacement.run_id, old.run_id)
        old = self.store.get(old.run_id)
        self.assertEqual(old.replaced_by_run_id, replacement.run_id)
        self.assertEqual(old.outcome, "handoff")
        self.assertNotIn(old.run_id, self.supervisor.adapters)
        self.assertNotIn(old.run_id, self.supervisor.event_tasks)
        self.assertIs(self.supervisor.adapters[replacement.run_id], adapter)
        await _wait_for_events(self.store, old.run_id, 10)
        await _wait_for_events(self.store, replacement.run_id, 10)
        old_methods = [
            event["payload"].get("method")
            for event in self.store.read_raw_events(old.run_id)
        ]
        replacement_methods = [
            event["payload"].get("method")
            for event in self.store.read_raw_events(replacement.run_id)
        ]
        self.assertEqual(old_methods.count("thread/started"), 1)
        self.assertEqual(replacement_methods.count("thread/started"), 1)

        recovery = await self.supervisor.recover_on_start()
        old_result = next(item for item in recovery if item["run_id"] == old.run_id)
        self.assertEqual(old_result["action"], "skip")

    async def test_recovery_resumes_only_current_working_and_idle_with_dead_pid(self) -> None:
        await self.supervisor.close()
        store = RunStore(self.paths)
        records: dict[str, RunRecord] = {}
        for index, state in enumerate(
            (
                LifecycleState.WORKING,
                LifecycleState.IDLE,
                LifecycleState.STARTING,
                LifecycleState.WAITING_APPROVAL,
                LifecycleState.DEAD,
                LifecycleState.COMPLETED,
            ),
            start=1,
        ):
            worktree = self.root / f"recovery-{index}"
            worktree.mkdir()
            record = RunRecord.new(
                agent_id=f"WIKI-R{index}",
                provider=ProviderKind.CODEX if index % 2 else ProviderKind.CLAUDE,
                role="implement",
                model="fixture",
                worktree=str(worktree),
                prompt="fixture",
            )
            record.state = state
            record.provider_session_id = f"session-{index}"
            record.provider_pid = 999_000 + index
            store.create(record)
            records[state.value] = record

        supervisor = Supervisor(
            store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: False,
        )
        try:
            results = await supervisor.recover_on_start()
            by_id = {item["run_id"]: item for item in results}
            self.assertEqual(by_id[records["working"].run_id]["action"], "resume")
            self.assertEqual(by_id[records["idle"].run_id]["action"], "resume")
            self.assertEqual(by_id[records["starting"].run_id]["action"], "block")
            self.assertEqual(by_id[records["waiting-approval"].run_id]["action"], "block")
            self.assertEqual(by_id[records["dead"].run_id]["action"], "skip")
            self.assertEqual(by_id[records["completed"].run_id]["action"], "skip")
            self.assertEqual(store.get(records["working"].run_id).state, LifecycleState.IDLE)
            self.assertEqual(store.get(records["idle"].run_id).state, LifecycleState.IDLE)
            self.assertEqual(
                store.get(records["waiting-approval"].run_id).state,
                LifecycleState.BLOCKED,
            )
            self.assertEqual(
                store.get(records["starting"].run_id).state,
                LifecycleState.BLOCKED,
            )
        finally:
            await supervisor.close()
        # Keep tearDown from closing the already-closed original twice.
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))

    async def test_recovery_rechecks_live_orphan_then_resumes_exact_session(self) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-ORPHAN",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.WORKING
        record.provider_session_id = "session-exact"
        record.provider_pid = 424_242
        self.store.create(record)
        alive = {"value": True}
        supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: alive["value"],
        )
        try:
            first = await supervisor.recover_on_start()
            self.assertEqual(first[0]["action"], "block")
            blocked = self.store.get(record.run_id)
            self.assertEqual(blocked.state, LifecycleState.BLOCKED)
            self.assertEqual(blocked.recovery_from_state, LifecycleState.WORKING)

            alive["value"] = False
            second = await supervisor.recover_on_start()
            self.assertEqual(second[0]["action"], "resume")
            resumed = self.store.get(record.run_id)
            self.assertEqual(resumed.state, LifecycleState.IDLE)
            self.assertEqual(resumed.provider_session_id, "session-exact")
            self.assertIsNone(resumed.recovery_from_state)
            self.assertIn(record.run_id, supervisor.adapters)
        finally:
            await supervisor.close()
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))

    async def test_codex_fake_exercises_start_steer_and_targeted_interrupt(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-42",
        )
        adapter = self.supervisor.adapters[record.run_id]
        self.assertIsInstance(adapter, CodexFixtureAdapter)
        assert isinstance(adapter, CodexFixtureAdapter)
        started = await self.supervisor.send_now(record.run_id, "begin a long turn")
        self.assertEqual(started, {"status": "sent"})
        status = await adapter.status()
        self.assertEqual(status.state, LifecycleState.WORKING)
        self.assertIsNotNone(status.active_turn_id)

        await self.supervisor.send_now(record.run_id, "steer the active turn")
        await self.supervisor.interrupt(record.run_id)
        self.assertEqual(adapter.replayed_methods[-3:], ["turn/start", "turn/steer", "turn/interrupt"])

    async def test_claude_interruption_result_stays_interrupted(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-CLAUDE",
        )
        record = await _wait_for_events(self.store, record.run_id, 10)
        before = record.raw_event_count
        await self.supervisor.interrupt(record.run_id)
        await _wait_for_events(self.store, record.run_id, before + 1)
        self.assertEqual(self.store.get(record.run_id).state, LifecycleState.INTERRUPTED)

    def test_safe_worktree_refuses_home_relative_missing(self) -> None:
        self.assertEqual(resolve_safe_worktree(str(self.worktree)), self.worktree.resolve())
        with self.assertRaisesRegex(ValueError, "absolute"):
            resolve_safe_worktree("relative/path")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            resolve_safe_worktree(str(self.root / "missing"))
        with self.assertRaisesRegex(ValueError, r"\$HOME"):
            resolve_safe_worktree(str(Path.home()))


class UnixClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.paths = _paths(self.root)
        self.store = RunStore(self.paths)
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))
        self.server = UnixSupervisorServer(self.supervisor, self.paths.socket_path)
        await self.server.start()
        self.client = SupervisorClient(self.paths, timeout=2)

    async def asyncTearDown(self) -> None:
        await self.server.close()
        await self.supervisor.close()
        self.tmp.cleanup()

    async def test_client_round_trip_message_contract_and_event_subscription(self) -> None:
        ping = await asyncio.to_thread(self.client.ping)
        self.assertEqual(ping["status"], "ok")
        self.assertEqual(self.paths.socket_path.stat().st_mode & 0o777, 0o600)

        stream = self.client.subscribe_events()
        next_event = asyncio.ensure_future(anext(stream))
        for _ in range(100):
            if self.supervisor.subscribers:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.supervisor.subscribers)
        started = await asyncio.to_thread(
            self.client.request,
            "run/start",
            {
                "agent_id": "WIKI-42",
                "provider": "codex",
                "role": "implement",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "Work on ticket WIKI-42",
            },
        )
        event = await asyncio.wait_for(next_event, timeout=3)
        self.assertEqual(event, {"type": "agents", "tickets": ["WIKI-42"], "surface": "agents"})
        self.assertNotIn("initial_prompt", started)

        sent = await asyncio.to_thread(self.client.send_message, "WIKI-42", "now", "now")
        self.assertEqual(sent, {"status": "sent"})
        queued = await asyncio.to_thread(
            self.client.send_message,
            "WIKI-42",
            "later",
            "on-idle",
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["messages"][0]["text"], "later")
        await stream.aclose()

    async def test_server_accepts_existing_prompt_contract_above_default_reader_limit(self) -> None:
        started = await asyncio.to_thread(
            self.client.request,
            "run/start",
            {
                "agent_id": "WIKI-LARGE",
                "provider": "codex",
                "role": "implement",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "x" * 70_000,
            },
        )
        self.assertEqual(started["agent_id"], "WIKI-LARGE")

    async def test_socket_refuses_to_replace_regular_file(self) -> None:
        await self.server.close()
        self.paths.socket_path.write_text("not a socket", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "non-socket"):
            await self.server.start()

    async def test_second_server_refuses_to_unlink_active_socket(self) -> None:
        second = UnixSupervisorServer(self.supervisor, self.paths.socket_path)
        with self.assertRaisesRegex(RuntimeError, "already active"):
            await second.start()
        self.assertTrue(self.paths.socket_path.exists())


class DaemonProcessTests(unittest.TestCase):
    def test_daemon_process_owns_fake_run_without_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            worktree = root / "worktree"
            worktree.mkdir()
            env = os.environ.copy()
            env["TMUX"] = ""
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "backend.app.agent_runtime.daemon",
                    "--runtime-dir",
                    str(paths.runtime_dir),
                    "--socket",
                    str(paths.socket_path),
                    "--registry",
                    str(paths.registry_path),
                    "--fake-fixture-dir",
                    str(FIXTURES),
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            client = SupervisorClient(paths, timeout=1)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        self.assertEqual(client.ping()["status"], "ok")
                        break
                    except SupervisorUnavailable:
                        if time.monotonic() >= deadline:
                            stderr = (
                                process.stderr.read()
                                if process.poll() is not None and process.stderr
                                else "process still running without a socket"
                            )
                            self.fail(f"daemon did not start: {stderr}")
                        time.sleep(0.05)
                started = client.request(
                    "run/start",
                    {
                        "agent_id": "WIKI-42",
                        "provider": "codex",
                        "role": "implement",
                        "model": "fixture-codex",
                        "effort": "high",
                        "worktree": str(worktree),
                        "prompt": "Work on ticket WIKI-42",
                    },
                )
                self.assertEqual(started["state"], "idle")
                self.assertTrue(paths.registry_path.is_file())
                registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
                self.assertEqual(registry["WIKI-42"]["current"]["run_id"], started["run_id"])
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stderr:
                    process.stderr.close()
            self.assertFalse(paths.socket_path.exists())
            self.assertFalse(paths.pid_path.exists())


if __name__ == "__main__":
    unittest.main()
