"""WIKI-161: send_now/send_on_idle carry a `source` field that flows into
pending_user_messages and composer_messages so the frontend can render
synthetic-source user turns as marker rows without changing LLM semantics."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.app.agent_runtime.fake import FixtureAdapterFactory
from backend.app.agent_runtime.provider import ProviderEvent
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import (
    Supervisor,
    _validated_source,
)
from backend.app.agent_runtime.types import EventDisposition, ProviderKind


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


def _paths(root: Path) -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=root / "runtime",
        socket_path=root / "runtime" / "supervisor.sock",
        registry_path=root / "isolated-registry.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )


async def _wait_for_events(store: RunStore, run_id: str, minimum: int) -> None:
    for _ in range(200):
        record = store.get(run_id)
        if record.raw_event_count >= minimum:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {minimum} raw events")


class SendNowSourceTests(unittest.IsolatedAsyncioTestCase):
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

    async def _start_claude(self, ticket: str) -> str:
        record = await self.supervisor.start_run(
            agent_id=ticket,
            provider=ProviderKind.CLAUDE,
            role="orchestrator",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt=f"Work on ticket {ticket}",
        )
        await _wait_for_events(self.store, record.run_id, 5)
        return record.run_id

    async def _echo_user_turn(self, run_id: str, text: str) -> None:
        adapter = self.supervisor.adapters[run_id]
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                    },
                },
            ),
        )

    async def _wait_for_composer_message(self, run_id: str) -> list[dict[str, str]]:
        for _ in range(200):
            messages = self.store.get(run_id).composer_messages
            if messages:
                return messages
            await asyncio.sleep(0.01)
        raise AssertionError("no composer message ever surfaced")

    async def test_send_now_without_source_leaves_composer_untagged(self) -> None:
        run_id = await self._start_claude("WIKI-161-A")
        pending_id = str(uuid4())
        result = await self.supervisor.send_now(
            run_id,
            "hi from henry",
            pending_id,
        )
        self.assertEqual(result["status"], "sent")
        pending = self.store.get(run_id).pending_user_messages
        self.assertEqual(len(pending), 1)
        self.assertNotIn("source", pending[0])
        await self._echo_user_turn(run_id, "hi from henry")
        composer_messages = await self._wait_for_composer_message(run_id)
        self.assertNotIn("source", composer_messages[0])

    async def test_send_now_with_source_persists_and_flows_to_composer(self) -> None:
        run_id = await self._start_claude("WIKI-161-B")
        pending_id = str(uuid4())
        result = await self.supervisor.send_now(
            run_id,
            "[fleet] WIKI-1234 merged",
            pending_id,
            source="fleet-monitor",
        )
        self.assertEqual(result["status"], "sent")
        pending = self.store.get(run_id).pending_user_messages
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source"], "fleet-monitor")
        self.assertEqual(pending[0]["text"], "[fleet] WIKI-1234 merged")

        await self._echo_user_turn(run_id, "[fleet] WIKI-1234 merged")
        composer_messages = await self._wait_for_composer_message(run_id)
        self.assertEqual(composer_messages[0]["source"], "fleet-monitor")
        self.assertEqual(composer_messages[0]["pending_id"], pending_id)
        self.assertEqual(self.store.get(run_id).pending_user_messages, [])

    async def test_send_now_source_without_pending_id_mints_one(self) -> None:
        run_id = await self._start_claude("WIKI-161-C")
        result = await self.supervisor.send_now(
            run_id,
            "supervisor-steer body",
            source="supervisor-steer",
        )
        self.assertEqual(result["status"], "sent")
        pending = self.store.get(run_id).pending_user_messages
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source"], "supervisor-steer")
        # Pending id must be minted so the source can be correlated back to the
        # composer_messages record when the provider echoes the turn.
        self.assertTrue(pending[0]["pending_id"])

    async def test_send_on_idle_persists_source_on_queued_message(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-161-D",
            provider=ProviderKind.CODEX,
            role="orchestrator",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-161-D",
        )
        run_id = record.run_id
        # Occupy the run so send_on_idle stays queued instead of delivering.
        await self.supervisor.send_now(run_id, "start a long turn")
        result = await self.supervisor.send_on_idle(
            run_id,
            "queued fleet ping",
            source="fleet-monitor",
        )
        self.assertEqual(result["status"], "queued")
        queued = self.store.get(run_id).queued_messages
        self.assertEqual(queued[0]["source"], "fleet-monitor")

    async def test_source_validation_rejects_bad_payloads(self) -> None:
        with self.assertRaises(ValueError):
            _validated_source("")
        with self.assertRaises(ValueError):
            _validated_source("has spaces")
        with self.assertRaises(ValueError):
            _validated_source("x" * 65)
        with self.assertRaises(ValueError):
            _validated_source(123)
        self.assertIsNone(_validated_source(None))
        self.assertEqual(_validated_source("fleet-monitor"), "fleet-monitor")
        self.assertEqual(_validated_source("mastermind"), "mastermind")

    async def test_supervisor_run_send_now_jsonrpc_accepts_source(self) -> None:
        run_id = await self._start_claude("WIKI-161-E")
        result = await self.supervisor.dispatch(
            "run/send_now",
            {
                "run_id": run_id,
                "text": "rpc-tagged body",
                "source": "mastermind",
            },
        )
        self.assertEqual(result["status"], "sent")
        pending = self.store.get(run_id).pending_user_messages
        self.assertEqual(pending[0]["source"], "mastermind")

    async def test_supervisor_run_send_now_rejects_bad_source(self) -> None:
        run_id = await self._start_claude("WIKI-161-F")
        with self.assertRaises(ValueError):
            await self.supervisor.dispatch(
                "run/send_now",
                {
                    "run_id": run_id,
                    "text": "attempted attack",
                    "source": "has spaces",
                },
            )

    async def test_unread_event_seq_only_advances_on_worker_output(self) -> None:
        """WIKI-161 review R6+R2R3: the unread dot must light only when the
        worker produces new output. Sourced supervisor turns AND unsourced
        outbound client_message journals AND user echoes (whether Henry-
        typed or synthetic) AND turn/lifecycle rows must all leave the
        counter alone; only assistant/tool/etc. output advances it."""

        run_id = await self._start_claude("WIKI-161-H")
        baseline_unread = self.store.get(run_id).unread_event_seq

        # 1. Outbound client_message row (adapters journal these before any
        # provider response) must NOT advance unread.
        pre_outbound = self.store.get(run_id)
        self.store.append_normalized(
            run_id,
            raw_seq=pre_outbound.raw_event_count,
            disposition=EventDisposition.IGNORED,
            kind="claude_client_message",
            payload={"type": "user", "message": {"role": "user"}},
        )
        after_outbound = self.store.get(run_id)
        self.assertEqual(
            after_outbound.normalized_event_count,
            pre_outbound.normalized_event_count + 1,
        )
        self.assertEqual(after_outbound.unread_event_seq, pre_outbound.unread_event_seq)

        # 2. Sourced user echo (supervisor-steer / fleet-monitor wake) — no
        # advance either.
        pre_sourced = self.store.get(run_id)
        self.store.append_normalized(
            run_id,
            raw_seq=pre_sourced.raw_event_count,
            disposition=EventDisposition.RENDERED,
            kind="claude_user",
            payload={
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "[fleet] wake up"}],
                },
                "source": "fleet-monitor",
            },
        )
        after_sourced = self.store.get(run_id)
        self.assertEqual(after_sourced.unread_event_seq, pre_sourced.unread_event_seq)

        # 3. Turn/lifecycle boundary (turn_started) — no advance.
        pre_lifecycle = self.store.get(run_id)
        self.store.append_normalized(
            run_id,
            raw_seq=pre_lifecycle.raw_event_count,
            disposition=EventDisposition.RENDERED,
            kind="turn_started",
            payload={"method": "turn/started"},
        )
        after_lifecycle = self.store.get(run_id)
        self.assertEqual(
            after_lifecycle.unread_event_seq, pre_lifecycle.unread_event_seq
        )

        # 4. Henry-typed user echo (unsourced) also does not advance unread —
        # Henry just sent it, so it can't be "unread" for him.
        pre_henry = self.store.get(run_id)
        self.store.append_normalized(
            run_id,
            raw_seq=pre_henry.raw_event_count,
            disposition=EventDisposition.RENDERED,
            kind="claude_user",
            payload={
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi henry"}],
                },
            },
        )
        after_henry = self.store.get(run_id)
        self.assertEqual(after_henry.unread_event_seq, pre_henry.unread_event_seq)

        # 5. Only genuine worker output (assistant message here) advances.
        pre_worker = self.store.get(run_id)
        self.store.append_normalized(
            run_id,
            raw_seq=pre_worker.raw_event_count,
            disposition=EventDisposition.RENDERED,
            kind="claude_assistant",
            payload={"text": "worker response"},
        )
        after_worker = self.store.get(run_id)
        self.assertEqual(
            after_worker.unread_event_seq,
            pre_worker.normalized_event_count + 1,
        )
        # Baseline started at whatever start_run produced; the assistant
        # message is the first genuinely unread-worthy event we appended.
        self.assertGreater(after_worker.unread_event_seq, baseline_unread)

    async def test_unread_event_seq_rebuilt_from_normalized_jsonl(self) -> None:
        """WIKI-161 R2 R3: a crash between normalized JSONL fsync and run.json
        replace currently repairs ``normalized_event_count`` but must also
        repair the new ``unread_event_seq`` — otherwise the on-disk value
        stays stale and the unread dot lights wrong on restart."""

        run_id = await self._start_claude("WIKI-161-J")
        # Two synthetic + one assistant. Only the assistant is unread-worthy.
        pre = self.store.get(run_id)
        self.store.append_normalized(
            run_id,
            raw_seq=pre.raw_event_count,
            disposition=EventDisposition.RENDERED,
            kind="claude_user",
            payload={
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "[steer] focus"}],
                },
                "source": "supervisor-steer",
            },
        )
        pre = self.store.get(run_id)
        self.store.append_normalized(
            run_id,
            raw_seq=pre.raw_event_count,
            disposition=EventDisposition.RENDERED,
            kind="claude_assistant",
            payload={"text": "worker output"},
        )
        expected_unread = self.store.get(run_id).unread_event_seq

        # Simulate a crash after fsync but before run.json replace: reset
        # the field on disk and reload via a fresh RunStore reconcile.
        record = self.store.get(run_id)
        record.unread_event_seq = 0
        self.store._write_record(record)  # noqa: SLF001

        fresh = RunStore(self.paths)
        rebuilt = fresh.get(run_id)
        self.assertEqual(rebuilt.unread_event_seq, expected_unread)

    async def test_pending_track_failure_releases_dedupe_and_pending(self) -> None:
        """Regression for WIKI-161 review: pre-acceptance failures in the
        pending metadata write left the dedupe key durably claimed, so any
        retry with the same key returned ``deduplicated`` — the synthetic
        turn was silently dropped and the orchestrator never woke."""

        run_id = await self._start_claude("WIKI-161-G")
        dedupe_key = "fleet:wiki-161-g:merge-ready"

        real_track = self.store.track_pending_user_message
        boom_calls = {"n": 0}

        def track_that_fails_once(*args: Any, **kwargs: Any):
            boom_calls["n"] += 1
            if boom_calls["n"] == 1:
                raise OSError("simulated disk failure")
            return real_track(*args, **kwargs)

        self.store.track_pending_user_message = track_that_fails_once  # type: ignore[method-assign]
        try:
            with self.assertRaises(OSError):
                await self.supervisor.send_now(
                    run_id,
                    "[fleet] retry me",
                    dedupe_key=dedupe_key,
                    source="fleet-monitor",
                )
            # Rollback: dedupe claim released, pending row discarded.
            record = self.store.get(run_id)
            self.assertNotIn(dedupe_key, record.message_dedupe_keys)
            self.assertEqual(record.pending_user_messages, [])
        finally:
            self.store.track_pending_user_message = real_track  # type: ignore[method-assign]

        # Retry now reaches the adapter and completes normally.
        result = await self.supervisor.send_now(
            run_id,
            "[fleet] retry me",
            dedupe_key=dedupe_key,
            source="fleet-monitor",
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["dedupe_key"], dedupe_key)
        pending = self.store.get(run_id).pending_user_messages
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source"], "fleet-monitor")


    async def test_pending_track_post_commit_failure_rolls_back_and_retries_cleanly(
        self,
    ) -> None:
        """WIKI-161 review R2 HIGH: track_pending_user_message()'s atomic
        os.replace may commit and a follow-up chmod/directory-fsync may then
        raise. The pending row is durable at that point, so the rollback
        MUST unconditionally discard by pending_id — otherwise the retry
        lands a second pending row and the stale first row can consume the
        retry's provider echo. Adapter is spied to prove the retry actually
        reached the provider (status alone would not distinguish a real
        send from a deduplicated short-circuit)."""

        run_id = await self._start_claude("WIKI-161-I")
        dedupe_key = "fleet:wiki-161-i:merge-ready"

        real_track = self.store.track_pending_user_message

        def track_that_commits_then_raises(
            passed_run_id: str,
            pending_id: str,
            text: str,
            source: str | None = None,
        ):
            # Simulate the post-commit code path: the durable write lands
            # (so the pending row is on disk) and then a follow-up filesystem
            # step (chmod / dir fsync / stat) raises.
            real_track(passed_run_id, pending_id, text, source=source)
            raise OSError("simulated post-commit fs failure")

        self.store.track_pending_user_message = (  # type: ignore[method-assign]
            track_that_commits_then_raises
        )
        try:
            with self.assertRaises(OSError):
                await self.supervisor.send_now(
                    run_id,
                    "[fleet] partial-commit path",
                    dedupe_key=dedupe_key,
                    source="fleet-monitor",
                )
        finally:
            self.store.track_pending_user_message = (  # type: ignore[method-assign]
                real_track
            )

        record = self.store.get(run_id)
        self.assertNotIn(dedupe_key, record.message_dedupe_keys)
        # The whole point of the fix: no orphan pending row after post-commit
        # failure — otherwise the retry lands a second row and echoes swap.
        self.assertEqual(record.pending_user_messages, [])

        adapter = self.supervisor.adapters[run_id]
        real_send = adapter.send_now
        send_calls: list[str] = []

        async def send_spy(text: str):
            send_calls.append(text)
            return await real_send(text)

        adapter.send_now = send_spy  # type: ignore[assignment]
        try:
            result = await self.supervisor.send_now(
                run_id,
                "[fleet] partial-commit path",
                dedupe_key=dedupe_key,
                source="fleet-monitor",
            )
        finally:
            adapter.send_now = real_send  # type: ignore[assignment]

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["dedupe_key"], dedupe_key)
        # Adapter spy confirms the retry actually flowed to the provider
        # instead of short-circuiting on the still-claimed dedupe key.
        self.assertEqual(send_calls, ["[fleet] partial-commit path"])
        pending = self.store.get(run_id).pending_user_messages
        self.assertEqual(
            len(pending),
            1,
            f"exactly one pending row expected after retry, got {pending}",
        )
        self.assertEqual(pending[0]["source"], "fleet-monitor")
        self.assertEqual(pending[0]["text"], "[fleet] partial-commit path")


if __name__ == "__main__":
    unittest.main()
