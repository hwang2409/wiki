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

from backend.app.agent_runtime.client import (
    SupervisorClient,
    SupervisorRemoteError,
    SupervisorUnavailable,
)
from backend.app.agent_runtime.codex import CodexAppServerAdapter
from backend.app.agent_runtime.fake import CodexFixtureAdapter, FixtureAdapterFactory
from backend.app.agent_runtime.process import ProviderProcessIdentity
from backend.app.agent_runtime.protocol import UnixSupervisorServer
from backend.app.agent_runtime.provider import (
    AdapterStatus,
    ProviderEvent,
    StartRequest,
)
from backend.app.agent_runtime.store import RunStore, RuntimePaths, StoreConflict
from backend.app.agent_runtime.supervisor import Supervisor, resolve_safe_worktree
from backend.app.agent_runtime.types import LifecycleState, ProviderKind, RunRecord


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


class StartFailureAdapter(CodexFixtureAdapter):
    async def start(self, request: StartRequest) -> AdapterStatus:
        await self._events.put(  # noqa: SLF001 - failure-drain fixture
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "error",
                    "params": {"message": "fixture start failure", "willRetry": False},
                },
                generation=self.snapshot().generation + 1,
            )
        )
        raise RuntimeError("fixture start failure")


class ResumeFailureAdapter(CodexFixtureAdapter):
    def __init__(self, *args, attempts: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.attempts = attempts

    async def resume(self, session_id: str) -> AdapterStatus:
        self.attempts.append(session_id)
        await self._events.put(  # noqa: SLF001 - failure-drain fixture
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "error",
                    "params": {"message": "fixture resume failure", "willRetry": False},
                },
                generation=self.snapshot().generation + 1,
            )
        )
        raise RuntimeError("fixture resume failure")


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

    def _codex_process_factory(self, env: dict[str, str]):
        async def identity(
            pid: int | None,
            _provider: ProviderKind,
            _session_id: str | None,
            *,
            reported_path: str | None = None,
        ) -> ProviderProcessIdentity | None:
            if pid is None or reported_path is None:
                return None
            return ProviderProcessIdentity(pid, reported_path)

        def factory(record: RunRecord):
            return CodexAppServerAdapter(
                record,
                command=(
                    sys.executable,
                    "-u",
                    str(FIXTURES / "fake_codex_app_server.py"),
                ),
                env=env,
                request_timeout=2,
                identity_resolver=identity,
            )

        return factory

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
        self.assertEqual(
            event, {"type": "agents", "tickets": ["WIKI-42"], "surface": "agents"}
        )

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

    async def test_codex_question_before_turn_response_can_be_answered(self) -> None:
        await self.supervisor.close()
        protocol_log = self.root / "live-order-protocol.jsonl"
        transcript_dir = self.root / "live-order-transcripts"
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.root / "home"),
                "CODEX_HOME": str(self.root / "codex-home"),
                "FAKE_PROTOCOL_LOG": str(protocol_log),
                "FAKE_CODEX_TRANSCRIPT_DIR": str(transcript_dir),
                "FAKE_CODEX_APPROVAL": "1",
                "TMUX": "must-not-leak",
                "TMUX_PANE": "%9999",
            }
        )

        self.supervisor = Supervisor(self.store, self._codex_process_factory(env))
        events = self.supervisor.subscribe()
        start_task = asyncio.create_task(
            self.supervisor.start_run(
                agent_id="WIKI-QUESTION",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="Ask the fixture question",
            )
        )
        run_id: str | None = None
        for _ in range(200):
            run_id = self.store.current_run_id("WIKI-QUESTION")
            if run_id and self.store.get(run_id).pending_requests:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(run_id)
        assert run_id is not None
        started = await asyncio.wait_for(start_task, timeout=2)
        self.assertEqual(started.provider_session_id, "thread-1")
        pending = self.store.get(run_id).pending_requests
        self.assertEqual(pending["int:0"]["request_id"], 0)
        self.assertEqual(
            pending["int:0"]["request_kind"],
            "item/tool/requestUserInput",
        )

        await self.supervisor.respond(
            run_id,
            0,
            {"answers": {"wiki_surface": {"answers": ["Agents page"]}}},
        )
        self.assertEqual(self.store.get(run_id).pending_requests, {})
        self.assertGreater(started.raw_event_count, 0)
        surfaces: set[str] = set()
        for _ in range(8):
            event = await asyncio.wait_for(events.get(), timeout=2)
            surface = event.get("surface")
            if isinstance(surface, str):
                surfaces.add(surface)
            if "session" in surfaces:
                break
        self.assertIn("session", surfaces)
        self.supervisor.unsubscribe(events)

    async def test_background_codex_turn_rejection_is_durably_blocked(self) -> None:
        await self.supervisor.close()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.root / "failure-home"),
                "CODEX_HOME": str(self.root / "failure-codex-home"),
                "FAKE_PROTOCOL_LOG": str(self.root / "failure-protocol.jsonl"),
                "FAKE_CODEX_TRANSCRIPT_DIR": str(self.root / "failure-transcripts"),
                "FAKE_TURN_START_ERROR": "1",
                "TMUX": "must-not-leak",
                "TMUX_PANE": "%9999",
            }
        )
        self.supervisor = Supervisor(self.store, self._codex_process_factory(env))
        started = await self.supervisor.start_run(
            agent_id="WIKI-TURN-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Reject this turn after accepting the session",
        )
        for _ in range(200):
            failed = self.store.get(started.run_id)
            if failed.state is LifecycleState.BLOCKED:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(failed.state, LifecycleState.BLOCKED)
        self.assertTrue(
            any(
                event["payload"].get("params", {}).get("operation") == "turn/start"
                for event in self.store.read_raw_events(started.run_id)
            )
        )

    async def test_event_driven_delivery_failure_keeps_message_and_provider(
        self,
    ) -> None:
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
        self.store.queue_message(record.run_id, "do not lose this")
        with mock.patch.object(
            adapter,
            "send_on_idle",
            side_effect=RuntimeError("fixture delivery failed"),
        ):
            await self.supervisor._handle_provider_event(  # noqa: SLF001
                record.run_id,
                adapter,
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "thread/status/changed",
                        "params": {"status": {"type": "idle"}},
                    },
                ),
            )
        persisted = self.store.get(record.run_id)
        queued = persisted.queued_messages
        self.assertEqual([item["text"] for item in queued], ["do not lose this"])
        self.assertEqual(
            persisted.state_reason,
            "queued message delivery failed: fixture delivery failed",
        )
        self.assertIs(self.supervisor.adapters[record.run_id], adapter)
        self.assertFalse(getattr(adapter, "closed", False))
        self.assertNotIn(record.run_id, self.supervisor.pipeline_failures)

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
        self.assertEqual(
            self.store.read_normalized_events(record.run_id)[-1]["disposition"],
            "unknown",
        )

    async def test_start_failure_events_are_drained_before_run_is_marked_dead(
        self,
    ) -> None:
        await self.supervisor.close()

        def factory(_record: RunRecord):
            return StartFailureAdapter(
                FIXTURES / "codex_app_server_success.jsonl",
                FIXTURES / "codex_app_server_control.jsonl",
                pid=987_654,
            )

        self.supervisor = Supervisor(self.store, factory, pid_alive=lambda _pid: False)
        with self.assertRaisesRegex(RuntimeError, "fixture start failure"):
            await self.supervisor.start_run(
                agent_id="WIKI-START-FAIL",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="Fail after emitting a provider event",
            )
        run_id = self.store.current_run_id("WIKI-START-FAIL")
        assert run_id is not None
        failed = self.store.get(run_id)
        self.assertEqual(failed.state, LifecycleState.DEAD)
        self.assertTrue(
            any(
                event["payload"].get("method") == "error"
                for event in self.store.read_raw_events(run_id)
            )
        )

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
        with mock.patch.object(
            adapter, "replace", wraps=adapter.replace
        ) as provider_replace:
            replacement = await self.supervisor.replace(
                old.run_id,
                "Replacement ticket WIKI-42 prompt",
            )
        provider_replace.assert_awaited_once_with(
            "Replacement ticket WIKI-42 prompt", None
        )
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
        self.assertEqual(
            {event["generation"] for event in self.store.read_raw_events(old.run_id)},
            {1},
        )
        self.assertEqual(
            {
                event["generation"]
                for event in self.store.read_raw_events(replacement.run_id)
            },
            {2},
        )
        self.assertEqual(old_methods.count("thread/started"), 1)
        self.assertEqual(replacement_methods.count("thread/started"), 1)

        recovery = await self.supervisor.recover_on_start()
        old_result = next(item for item in recovery if item["run_id"] == old.run_id)
        self.assertEqual(old_result["action"], "skip")

    async def test_recovery_resumes_only_current_working_and_idle_with_dead_pid(
        self,
    ) -> None:
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
            self.assertEqual(
                by_id[records["waiting-approval"].run_id]["action"], "block"
            )
            self.assertEqual(by_id[records["dead"].run_id]["action"], "skip")
            self.assertEqual(by_id[records["completed"].run_id]["action"], "skip")
            self.assertEqual(
                store.get(records["working"].run_id).state, LifecycleState.IDLE
            )
            self.assertEqual(
                store.get(records["idle"].run_id).state, LifecycleState.IDLE
            )
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

    async def test_recovery_rechecks_live_orphan_then_resumes_exact_session(
        self,
    ) -> None:
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
            with self.assertRaisesRegex(StoreConflict, "refusing duplicate resume"):
                await supervisor.resume_run(record.run_id)
            with self.assertRaisesRegex(StoreConflict, "refusing false stop"):
                await supervisor.stop(record.run_id)
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

    async def test_failed_automatic_resume_does_not_loop_and_manual_retry_works(
        self,
    ) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-RESUME-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.WORKING
        record.provider_session_id = "session-retry"
        record.provider_pid = 987_654
        self.store.create(record)
        attempts: list[str] = []

        def factory(_record: RunRecord):
            if not attempts:
                return ResumeFailureAdapter(
                    FIXTURES / "codex_app_server_success.jsonl",
                    FIXTURES / "codex_app_server_control.jsonl",
                    pid=987_654,
                    attempts=attempts,
                )
            return CodexFixtureAdapter(
                FIXTURES / "codex_app_server_success.jsonl",
                FIXTURES / "codex_app_server_control.jsonl",
                pid=987_654,
            )

        self.supervisor = Supervisor(self.store, factory, pid_alive=lambda _pid: False)
        first = await self.supervisor.recover_on_start()
        self.assertEqual(first[0]["action"], "block")
        blocked = self.store.get(record.run_id)
        self.assertEqual(blocked.recovery_from_state, LifecycleState.WORKING)
        self.assertTrue(blocked.automatic_resume_suppressed)
        self.assertEqual(attempts, ["session-retry"])
        self.assertTrue(
            any(
                event["payload"].get("method") == "error"
                for event in self.store.read_raw_events(record.run_id)
            )
        )

        second = await self.supervisor.recover_on_start()
        self.assertEqual(second[0]["action"], "block")
        self.assertEqual(attempts, ["session-retry"])

        resumed = await self.supervisor.resume_run(record.run_id)
        self.assertEqual(resumed.state, LifecycleState.IDLE)
        self.assertFalse(resumed.automatic_resume_suppressed)

    async def test_stable_automatic_resume_clears_crash_loop_guard(self) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-RESUME-STABLE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.IDLE
        record.provider_session_id = "session-stable"
        record.provider_pid = 987_654
        self.store.create(record)
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: False,
            recovery_stability_seconds=0,
        )

        first = await self.supervisor.recover_on_start()
        self.assertEqual(first[0]["action"], "resume")
        self.assertTrue(self.store.get(record.run_id).automatic_resume_suppressed)

        second = await self.supervisor.recover_on_start()
        self.assertEqual(second[0]["action"], "retain")
        stable = self.store.get(record.run_id)
        self.assertFalse(stable.automatic_resume_suppressed)
        self.assertIsNone(stable.automatic_resume_guarded_at)

    async def test_recovery_retains_healthy_attachment_despite_stale_pid(self) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )
        record = await self.supervisor.start_run(
            agent_id="WIKI-ATTACHED",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-ATTACHED",
        )
        adapter = self.supervisor.adapters[record.run_id]

        recovery = await self.supervisor.recover_on_start()

        result = next(item for item in recovery if item["run_id"] == record.run_id)
        self.assertEqual(result["action"], "retain")
        self.assertIs(self.supervisor.adapters[record.run_id], adapter)
        with self.assertRaisesRegex(StoreConflict, "already has an attached"):
            await self.supervisor.resume_run(record.run_id)

    async def test_raw_persistence_failure_closes_and_blocks_without_recovery(
        self,
    ) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-PERSIST",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-PERSIST",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        adapter = self.supervisor.adapters[record.run_id]
        self.assertIsInstance(adapter, CodexFixtureAdapter)
        assert isinstance(adapter, CodexFixtureAdapter)

        with mock.patch.object(
            self.store,
            "append_raw",
            side_effect=OSError("fixture fsync failed"),
        ):
            await adapter.respond("fixture-request", {"answer": "fixture"})
            for _ in range(200):
                if record.run_id in self.supervisor.pipeline_failures:
                    break
                await asyncio.sleep(0.01)

        self.assertTrue(adapter.closed)
        self.assertNotIn(record.run_id, self.supervisor.adapters)
        self.assertFalse(
            any(key[0] == id(adapter) for key in self.supervisor.event_routes)
        )
        blocked = self.store.get(record.run_id)
        self.assertEqual(blocked.state, LifecycleState.BLOCKED)
        self.assertEqual(
            blocked.state_reason,
            "provider event persistence failed: fixture fsync failed",
        )

        recovery = await self.supervisor.recover_on_start()
        result = next(item for item in recovery if item["run_id"] == record.run_id)
        self.assertEqual(result["action"], "block")
        self.assertNotIn(record.run_id, self.supervisor.adapters)

    async def test_replace_serializes_send_and_interrupt_against_old_run(self) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-RACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original WIKI-RACE prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        agent_lock = self.supervisor._run_lock(old.run_id)  # noqa: SLF001
        replace_started = asyncio.Event()
        allow_replace = asyncio.Event()
        original_replace = adapter.replace

        async def delayed_replace(prompt: str, model: str | None = None):
            replace_started.set()
            await allow_replace.wait()
            return await original_replace(prompt, model)

        with (
            mock.patch.object(adapter, "replace", side_effect=delayed_replace),
            mock.patch.object(
                adapter, "send_now", wraps=adapter.send_now
            ) as provider_send,
            mock.patch.object(
                adapter,
                "send_on_idle",
                wraps=adapter.send_on_idle,
            ) as provider_send_on_idle,
            mock.patch.object(
                adapter, "interrupt", wraps=adapter.interrupt
            ) as provider_interrupt,
        ):
            replace_task = asyncio.create_task(
                self.supervisor.replace(old.run_id, "Replacement WIKI-RACE prompt")
            )
            await asyncio.wait_for(replace_started.wait(), timeout=2)
            self.store.queue_message(
                old.run_id,
                "must not reach replacement from idle queue",
            )
            send_task = asyncio.create_task(
                self.supervisor.send_now(old.run_id, "must not reach replacement")
            )
            interrupt_task = asyncio.create_task(self.supervisor.interrupt(old.run_id))
            queued_delivery_task = asyncio.create_task(
                self.supervisor._deliver_next_queued(old.run_id, adapter)  # noqa: SLF001
            )
            await asyncio.sleep(0)
            self.assertFalse(send_task.done())
            self.assertFalse(interrupt_task.done())
            self.assertFalse(queued_delivery_task.done())

            allow_replace.set()
            replacement = await asyncio.wait_for(replace_task, timeout=2)
            control_results = await asyncio.gather(
                send_task,
                interrupt_task,
                return_exceptions=True,
            )
            await queued_delivery_task

        self.assertTrue(
            all(isinstance(item, StoreConflict) for item in control_results)
        )
        provider_send.assert_not_awaited()
        provider_send_on_idle.assert_not_awaited()
        provider_interrupt.assert_not_awaited()
        self.assertNotEqual(replacement.run_id, old.run_id)
        self.assertIs(self.supervisor.adapters[replacement.run_id], adapter)
        self.assertIs(
            self.supervisor._run_lock(replacement.run_id),  # noqa: SLF001
            agent_lock,
        )
        self.assertEqual(len(self.store.get(old.run_id).queued_messages), 1)

    async def test_slow_provider_control_does_not_block_other_runs(self) -> None:
        first = await self.supervisor.start_run(
            agent_id="WIKI-SLOW",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-SLOW",
        )
        second = await self.supervisor.start_run(
            agent_id="WIKI-FAST",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-FAST",
        )
        first_adapter = self.supervisor.adapters[first.run_id]
        first_send = first_adapter.send_now
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()

        async def delayed_send(message: str):
            slow_started.set()
            await release_slow.wait()
            return await first_send(message)

        with mock.patch.object(first_adapter, "send_now", side_effect=delayed_send):
            slow_task = asyncio.create_task(
                self.supervisor.send_now(first.run_id, "slow control")
            )
            await asyncio.wait_for(slow_started.wait(), timeout=2)
            fast_result = await asyncio.wait_for(
                self.supervisor.send_now(second.run_id, "independent control"),
                timeout=0.5,
            )
            release_slow.set()
            slow_result = await asyncio.wait_for(slow_task, timeout=2)

        self.assertEqual(fast_result, {"status": "sent"})
        self.assertEqual(slow_result, {"status": "sent"})

    async def test_store_replace_failure_blocks_old_run_and_clears_live_routes(
        self,
    ) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-STORE-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original WIKI-STORE-FAIL prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        attempted: dict[str, RunRecord] = {}

        def fail_registry_commit(_old_run_id: str, replacement: RunRecord):
            attempted["replacement"] = replacement
            raise OSError("fixture registry commit failed")

        with mock.patch.object(
            self.store,
            "replace",
            side_effect=fail_registry_commit,
        ):
            with self.assertRaisesRegex(OSError, "registry commit failed"):
                await self.supervisor.replace(
                    old.run_id,
                    "Replacement WIKI-STORE-FAIL prompt",
                )

        blocked = self.store.get(old.run_id)
        self.assertEqual(blocked.state, LifecycleState.DEAD)
        self.assertEqual(
            blocked.state_reason,
            "replacement metadata commit failed: fixture registry commit failed",
        )
        self.assertIsNone(blocked.provider_pid)
        self.assertIsNone(blocked.active_turn_id)
        self.assertEqual(self.store.current_run_id(old.agent_id), old.run_id)
        self.assertNotIn(old.run_id, self.supervisor.adapters)
        self.assertNotIn(old.run_id, self.supervisor.event_tasks)
        self.assertFalse(
            any(key[0] == id(adapter) for key in self.supervisor.event_routes)
        )
        self.assertEqual((await adapter.status()).state, LifecycleState.DEAD)
        replacement = attempted["replacement"]
        self.assertNotEqual(replacement.state, LifecycleState.STARTING)
        self.assertIsNotNone(replacement.provider_session_id)
        self.assertGreater(replacement.provider_generation, 0)
        self.assertIsNotNone(replacement.provider_pid)

    async def test_provider_replace_failure_drains_event_and_keeps_old_identity(
        self,
    ) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-PROVIDER-REPLACE-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="original prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        self.assertIsInstance(adapter, CodexFixtureAdapter)
        assert isinstance(adapter, CodexFixtureAdapter)

        async def fail_replace(_prompt: str, _model: str | None = None):
            await adapter._events.put(  # noqa: SLF001 - failure-drain fixture
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "error",
                        "params": {
                            "message": "fixture replacement failure",
                            "willRetry": False,
                        },
                    },
                    generation=old.provider_generation,
                )
            )
            raise RuntimeError("fixture replacement failure")

        with mock.patch.object(adapter, "replace", side_effect=fail_replace):
            with self.assertRaisesRegex(RuntimeError, "replacement failure"):
                await self.supervisor.replace(old.run_id, "replacement prompt")

        failed = self.store.get(old.run_id)
        self.assertEqual(failed.state, LifecycleState.BLOCKED)
        self.assertEqual(failed.provider_session_id, old.provider_session_id)
        self.assertEqual(failed.transcript_path, old.transcript_path)
        raw_events = self.store.read_raw_events(old.run_id)
        self.assertTrue(
            any(event["payload"].get("method") == "error" for event in raw_events),
            raw_events,
        )
        self.assertNotIn(old.run_id, self.supervisor.adapters)

    async def test_dead_current_run_can_be_replaced_without_resurrecting_it(
        self,
    ) -> None:
        await self.supervisor.close()
        dead = RunRecord.new(
            agent_id="WIKI-DEAD-REPLACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="failed original",
        )
        dead.state = LifecycleState.DEAD
        dead.provider_session_id = "dead-session"
        self.store.create(dead)
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )

        replacement = await self.supervisor.replace(
            dead.run_id,
            "fresh replacement prompt",
        )

        archived = self.store.get(dead.run_id)
        self.assertEqual(archived.state, LifecycleState.DEAD)
        self.assertEqual(archived.replaced_by_run_id, replacement.run_id)
        self.assertNotEqual(replacement.provider_session_id, "dead-session")
        self.assertEqual(
            self.store.current_run_id(dead.agent_id),
            replacement.run_id,
        )

    async def test_explicit_stop_detaches_then_allows_fresh_replacement(self) -> None:
        original = await self.supervisor.start_run(
            agent_id="WIKI-STOP-REPLACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="original prompt",
        )
        stopped = await self.supervisor.stop(original.run_id)
        self.assertEqual(stopped.state, LifecycleState.DEAD)
        self.assertNotIn(original.run_id, self.supervisor.adapters)

        replacement = await self.supervisor.replace(
            original.run_id,
            "replacement after stop",
        )
        self.assertNotEqual(replacement.run_id, original.run_id)
        self.assertIn(replacement.run_id, self.supervisor.adapters)

    async def test_failed_stop_control_remains_terminal_and_never_revives(self) -> None:
        original = await self.supervisor.start_run(
            agent_id="WIKI-STOP-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[original.run_id]

        with (
            mock.patch.object(
                adapter,
                "stop",
                side_effect=RuntimeError("fixture stop rejection"),
            ),
            self.assertRaisesRegex(RuntimeError, "fixture stop rejection"),
        ):
            await self.supervisor.stop(original.run_id)

        stopped = self.store.get(original.run_id)
        self.assertEqual(stopped.state, LifecycleState.DEAD)
        self.assertEqual(
            stopped.state_reason,
            "provider stop failed: fixture stop rejection",
        )
        self.assertNotIn(original.run_id, self.supervisor.adapters)
        recovery = await self.supervisor.recover_on_start()
        result = next(item for item in recovery if item["run_id"] == original.run_id)
        self.assertEqual(result["action"], "skip")

    async def test_failed_archive_control_remains_completed(self) -> None:
        original = await self.supervisor.start_run(
            agent_id="WIKI-ARCHIVE-FAIL",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[original.run_id]

        with (
            mock.patch.object(
                adapter,
                "archive",
                side_effect=RuntimeError("fixture archive rejection"),
            ),
            self.assertRaisesRegex(RuntimeError, "fixture archive rejection"),
        ):
            await self.supervisor.archive(original.run_id)

        archived = self.store.get(original.run_id)
        self.assertEqual(archived.state, LifecycleState.COMPLETED)
        self.assertEqual(
            archived.state_reason,
            "provider archive failed: fixture archive rejection",
        )
        self.assertNotIn(original.run_id, self.supervisor.adapters)

    async def test_codex_fake_exercises_start_steer_and_targeted_interrupt(
        self,
    ) -> None:
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
        self.assertEqual(
            adapter.replayed_methods[-3:],
            ["turn/start", "turn/steer", "turn/interrupt"],
        )

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
        self.assertEqual(
            self.store.get(record.run_id).state, LifecycleState.INTERRUPTED
        )

    async def test_unexpected_stream_end_preserves_state_for_exact_resume(self) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )
        record = await self.supervisor.start_run(
            agent_id="WIKI-STREAM",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-STREAM",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        adapter = self.supervisor.adapters[record.run_id]
        await adapter.close()
        for _ in range(200):
            if record.run_id not in self.supervisor.adapters:
                break
            await asyncio.sleep(0.01)
        self.assertNotIn(record.run_id, self.supervisor.adapters)
        interrupted = self.store.get(record.run_id)
        self.assertEqual(interrupted.state, LifecycleState.IDLE)
        self.assertEqual(interrupted.state_reason, "provider event stream ended")

        recovery = await self.supervisor.recover_on_start()
        result = next(item for item in recovery if item["run_id"] == record.run_id)
        self.assertEqual(result["action"], "resume")
        self.assertIn(record.run_id, self.supervisor.adapters)
        guarded = self.store.get(record.run_id)
        self.assertTrue(guarded.automatic_resume_suppressed)
        self.assertIsNotNone(guarded.automatic_resume_guarded_at)

        resumed_adapter = self.supervisor.adapters[record.run_id]
        await resumed_adapter.close()
        for _ in range(200):
            if record.run_id not in self.supervisor.adapters:
                break
            await asyncio.sleep(0.01)
        crash_loop_check = await self.supervisor.recover_on_start()
        retry = next(
            item for item in crash_loop_check if item["run_id"] == record.run_id
        )
        self.assertEqual(retry["action"], "block")
        self.assertNotIn(record.run_id, self.supervisor.adapters)

    def test_safe_worktree_refuses_home_relative_missing(self) -> None:
        self.assertEqual(
            resolve_safe_worktree(str(self.worktree)), self.worktree.resolve()
        )
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

    async def test_client_round_trip_message_contract_and_event_subscription(
        self,
    ) -> None:
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
        self.assertEqual(
            event, {"type": "agents", "tickets": ["WIKI-42"], "surface": "agents"}
        )
        self.assertNotIn("initial_prompt", started)

        sent = await asyncio.to_thread(
            self.client.send_message, "WIKI-42", "now", "now"
        )
        self.assertEqual(sent, {"status": "sent"})
        queued = await asyncio.to_thread(
            self.client.send_message,
            "WIKI-42",
            "later",
            "on-idle",
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["messages"][0]["text"], "later")
        listed_queue = await asyncio.to_thread(self.client.queue, "WIKI-42")
        self.assertEqual(listed_queue, {"messages": queued["messages"]})
        emptied = await asyncio.to_thread(
            self.client.delete_queued,
            "WIKI-42",
            0,
        )
        self.assertEqual(emptied, {"messages": []})
        with self.assertRaises(SupervisorRemoteError) as missing:
            await asyncio.to_thread(self.client.delete_queued, "WIKI-42", 0)
        self.assertEqual(missing.exception.error_type, "RunNotFound")

        runs = await asyncio.to_thread(self.client.request, "run/list")
        current = next(
            run for run in runs["runs"] if run["run_id"] == started["run_id"]
        )
        self.assertTrue(current["control_attached"])
        self.assertTrue(current["provider_alive"])
        await stream.aclose()

    async def test_server_accepts_existing_prompt_contract_above_default_reader_limit(
        self,
    ) -> None:
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
                self.assertEqual(
                    registry["WIKI-42"]["current"]["run_id"], started["run_id"]
                )
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stderr:
                    process.stderr.close()
            self.assertFalse(paths.socket_path.exists())
            self.assertFalse(paths.pid_path.exists())


if __name__ == "__main__":
    unittest.main()
