from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.fake import FixtureAdapterFactory
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor
from backend.app.agent_runtime.types import (
    EventDisposition,
    LifecycleState,
    ProviderKind,
    RunRecord,
)


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


class AutoArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.worktree = root / "worktree"
        self.worktree.mkdir()
        self.paths = RuntimePaths(
            runtime_dir=root / "runtime",
            socket_path=root / "runtime" / "supervisor.sock",
            registry_path=root / "registry.json",
            archive_dir=root / "archive",
            status_dir=root / "status",
        )
        self.store = RunStore(self.paths)
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            auto_archive_grace_seconds=600,
        )

    async def asyncTearDown(self) -> None:
        await self.supervisor.close()
        self.tmp.cleanup()

    def _run(
        self, ticket: str, *, auto_archive: bool, state: LifecycleState
    ) -> RunRecord:
        record = RunRecord.new(
            agent_id=ticket,
            provider=ProviderKind.CODEX,
            role="review" if auto_archive else "implement",
            auto_archive=auto_archive,
            model="fixture",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = state
        self.store.create(record)
        return record

    def _verdict_status(self, ticket: str) -> None:
        self.paths.status_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.status_dir / f"{ticket}.json").write_text(
            json.dumps(
                {
                    "state": "merge-ready",
                    "pr": "https://github.com/example/repo/pull/1",
                    "step": "VERDICT: MERGE-READY",
                    "blocker": None,
                }
            ),
            encoding="utf-8",
        )

    def _output(self, run_id: str) -> None:
        raw = self.store.append_raw(
            run_id,
            provider="codex",
            direction="provider",
            payload={
                "method": "item/completed",
                "params": {"item": {"type": "text", "text": "verdict"}},
            },
        )
        self.store.append_normalized(
            run_id,
            raw_seq=int(raw["seq"]),
            disposition=EventDisposition.RENDERED,
            kind="item_completed",
            payload={"text": "verdict"},
        )

    async def test_spawn_defaults_review_to_one_shot(self) -> None:
        review = await self.supervisor.start_run(
            agent_id="WIKI-DEFAULT-REVIEW",
            provider=ProviderKind.CODEX,
            role="review",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="review",
        )
        implement = await self.supervisor.start_run(
            agent_id="WIKI-DEFAULT-IMPLEMENT",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="implement",
        )
        audit = await self.supervisor.start_run(
            agent_id="WIKI-EXPLICIT-AUDIT",
            provider=ProviderKind.CODEX,
            role="audit",
            auto_archive=True,
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="audit",
        )

        self.assertTrue(review.auto_archive)
        self.assertFalse(implement.auto_archive)
        self.assertTrue(audit.auto_archive)

    async def test_unviewed_verdict_archives_after_grace_with_persistence(self) -> None:
        record = self._run(
            "WIKI-AUTO-GRACE", auto_archive=True, state=LifecycleState.COMPLETED
        )
        self._output(record.run_id)
        self._verdict_status(record.agent_id)

        await self.supervisor.recover_on_start()
        self.assertTrue(self.store.run_dir(record.run_id).is_dir())
        raw_before = self.store.raw_events_path(record.run_id).read_bytes()
        events_before = self.store.normalized_events_path(record.run_id).read_bytes()

        candidate = self.store.get(record.run_id)
        candidate.auto_archive_terminal_at = "2000-01-01T00:00:00+00:00"
        self.store._write_record(candidate)  # noqa: SLF001 - test clock control
        self.supervisor.auto_archive_grace_seconds = 0
        events = self.supervisor.subscribe()
        with mock.patch.object(
            self.supervisor, "_archive", wraps=self.supervisor._archive
        ) as archive:
            results = await self.supervisor.recover_on_start()
        archive.assert_awaited_once_with(record.run_id, outcome="closed")

        self.assertEqual(results[-1]["reason"], "grace elapsed")
        self.assertFalse(self.store.run_dir(record.run_id).exists())
        archived = list((self.paths.archive_dir / record.agent_id).glob("*"))
        self.assertEqual(len(archived), 1)
        self.assertTrue((archived[0] / "raw.jsonl").is_file())
        self.assertTrue((archived[0] / "events.jsonl").is_file())
        self.assertEqual((archived[0] / "raw.jsonl").read_bytes(), raw_before)
        self.assertEqual((archived[0] / "events.jsonl").read_bytes(), events_before)
        notification = await asyncio.wait_for(events.get(), timeout=1)
        while notification.get("type") != "notification":
            notification = await asyncio.wait_for(events.get(), timeout=1)
        self.assertEqual(
            notification["message"],
            "auto-archived WIKI-AUTO-GRACE (one-shot, grace elapsed)",
        )

    async def test_viewed_verdict_archives_before_grace(self) -> None:
        record = self._run(
            "WIKI-AUTO-VIEWED", auto_archive=True, state=LifecycleState.IDLE
        )
        self._output(record.run_id)
        self._verdict_status(record.agent_id)
        await self.supervisor.recover_on_start()
        self.assertTrue(self.store.run_dir(record.run_id).is_dir())

        viewed = await self.supervisor.mark_viewed(record.run_id, requested_seq=1)

        self.assertEqual(viewed.outcome, "closed")
        self.assertFalse(self.store.run_dir(record.run_id).exists())

    async def test_implementer_never_auto_archives(self) -> None:
        record = self._run(
            "WIKI-AUTO-IMPLEMENT",
            auto_archive=False,
            state=LifecycleState.COMPLETED,
        )
        self._verdict_status(record.agent_id)
        self.supervisor.auto_archive_grace_seconds = 0

        await self.supervisor.recover_on_start()

        self.assertTrue(self.store.run_dir(record.run_id).is_dir())
        self.assertEqual(self.store.get(record.run_id).state, LifecycleState.COMPLETED)

    async def test_manual_archive_is_idempotent_after_auto_archive(self) -> None:
        record = self._run(
            "WIKI-AUTO-RACE", auto_archive=True, state=LifecycleState.COMPLETED
        )
        self.supervisor.auto_archive_grace_seconds = 0
        first = await self.supervisor.recover_on_start()
        self.assertEqual(first[-1]["action"], "auto-archive")

        replay = await self.supervisor.dispatch(
            "run/archive",
            {"agent_id": record.agent_id, "outcome": "closed"},
        )

        self.assertEqual(replay["outcome"], "closed")
        self.assertEqual(replay["run_id"], record.run_id)

    async def test_manual_archive_prevents_policy_archive(self) -> None:
        record = self._run(
            "WIKI-MANUAL-FIRST", auto_archive=True, state=LifecycleState.COMPLETED
        )
        await self.supervisor.archive(record.run_id, outcome="closed")

        self.supervisor.auto_archive_grace_seconds = 0
        self.assertEqual(await self.supervisor.recover_on_start(), [])


if __name__ == "__main__":
    unittest.main()
