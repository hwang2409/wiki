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
from backend.app import workgraph_service
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
        self.archive_edge_patcher = mock.patch.object(
            workgraph_service, "record_archive"
        )
        self.archive_edge_patcher.start()

    async def asyncTearDown(self) -> None:
        await self.supervisor.close()
        self.archive_edge_patcher.stop()
        self.tmp.cleanup()

    def _run(
        self,
        ticket: str,
        *,
        auto_archive: bool,
        state: LifecycleState,
        role: str | None = None,
    ) -> RunRecord:
        record = RunRecord.new(
            agent_id=ticket,
            provider=ProviderKind.CODEX,
            role=role or ("review" if auto_archive else "implement"),
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
                    "step": "MERGE-READY: https://github.com/example/repo/pull/1",
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

    async def test_start_api_rejects_one_shot_plan_and_implement_roles(self) -> None:
        for role in ("plan", "implement"):
            with self.assertRaises(ValueError):
                await self.supervisor.start_run(
                    agent_id=f"WIKI-API-{role.upper()}",
                    provider=ProviderKind.CODEX,
                    role=role,
                    auto_archive=True,
                    model="fixture-codex",
                    worktree=str(self.worktree),
                    prompt=role,
                )

            with self.assertRaises(ValueError):
                await self.supervisor.dispatch(
                    "run/start",
                    {
                        "agent_id": f"WIKI-DISPATCH-{role.upper()}",
                        "provider": "codex",
                        "role": role,
                        "auto_archive": True,
                        "model": "fixture-codex",
                        "worktree": str(self.worktree),
                        "prompt": role,
                    },
                )

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

        viewed = await self.supervisor.mark_viewed(record.run_id, requested_seq=2)

        self.assertEqual(viewed.outcome, "closed")
        self.assertFalse(self.store.run_dir(record.run_id).exists())

    async def test_implementer_never_auto_archives(self) -> None:
        record = self._run(
            "WIKI-AUTO-IMPLEMENT",
            auto_archive=True,
            state=LifecycleState.COMPLETED,
            role="implement",
        )
        self._verdict_status(record.agent_id)
        self.supervisor.auto_archive_grace_seconds = 0

        await self.supervisor.recover_on_start()

        self.assertTrue(self.store.run_dir(record.run_id).is_dir())
        self.assertEqual(self.store.get(record.run_id).state, LifecycleState.COMPLETED)

    async def test_verdict_seq_requires_new_non_unread_event_after_prior_view(self) -> None:
        record = self._run(
            "WIKI-AUTO-NEW-EVENT",
            auto_archive=True,
            state=LifecycleState.IDLE,
        )
        self._output(record.run_id)
        await self.supervisor.mark_viewed(record.run_id, requested_seq=1)

        raw = self.store.append_raw(
            record.run_id,
            provider="supervisor",
            direction="internal",
            payload={"type": "provider_process_exit", "source": "supervisor"},
        )
        self.store.append_normalized(
            record.run_id,
            raw_seq=int(raw["seq"]),
            disposition=EventDisposition.RENDERED,
            kind="provider_process_exit",
            payload={"source": "supervisor"},
        )
        self._verdict_status(record.agent_id)

        await self.supervisor.recover_on_start()

        current = self.store.get(record.run_id)
        self.assertEqual(current.auto_archive_verdict_seq, 2)
        self.assertEqual(current.last_viewed_seq, 1)
        self.assertTrue(self.store.run_dir(record.run_id).is_dir())

        viewed = await self.supervisor.mark_viewed(record.run_id, requested_seq=2)
        self.assertEqual(viewed.outcome, "closed")

    async def test_verdict_prefixes_are_case_insensitive_and_log_text_is_ignored(self) -> None:
        for index, step in enumerate(
            (
                "MERGE-READY: clean",
                "not-merge-ready: findings",
                "context\nNEEDS-FIXES: revise",
            )
        ):
            record = self._run(
                f"WIKI-AUTO-PREFIX-{index}",
                auto_archive=True,
                state=LifecycleState.IDLE,
            )
            self.paths.status_dir.mkdir(parents=True, exist_ok=True)
            (self.paths.status_dir / f"{record.agent_id}.json").write_text(
                json.dumps(
                    {
                        "state": "merge-ready" if index == 0 else "blocked",
                        "step": step,
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNotNone(self.supervisor._auto_archive_signal(record))

        ignored = self._run(
            "WIKI-AUTO-LOG-TEXT",
            auto_archive=True,
            state=LifecycleState.IDLE,
        )
        self.store.provider_log_path(ignored.run_id).write_text(
            "MERGE-READY: quoted kickoff text", encoding="utf-8"
        )
        self.paths.status_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.status_dir / f"{ignored.agent_id}.json").write_text(
            json.dumps({"state": "working", "step": "still running"}),
            encoding="utf-8",
        )
        self.assertIsNone(self.supervisor._auto_archive_signal(ignored))

    async def test_auto_and_manual_archive_have_matching_registry_and_graph_state(self) -> None:
        async def archive_in_mode(label: str, *, auto_archive: bool) -> tuple[dict, dict]:
            root = Path(self.tmp.name) / label
            worktree = root / "worktree"
            worktree.mkdir(parents=True)
            paths = RuntimePaths(
                runtime_dir=root / "runtime",
                socket_path=root / "runtime" / "supervisor.sock",
                registry_path=root / "registry.json",
                archive_dir=root / "archive",
                status_dir=root / "status",
            )
            store = RunStore(paths)
            supervisor = Supervisor(
                store,
                FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
                auto_archive_grace_seconds=0,
            )
            record = RunRecord.new(
                agent_id="WIKI-AUTO-PARITY",
                provider=ProviderKind.CODEX,
                role="review",
                auto_archive=auto_archive,
                model="fixture",
                worktree=str(worktree),
                prompt="fixture",
                run_id="00000000-0000-4000-8000-000000000275",
            )
            record.state = LifecycleState.COMPLETED
            store.create(record)
            with mock.patch.object(workgraph_service, "record_archive") as edge:
                if auto_archive:
                    await supervisor.recover_on_start()
                else:
                    await supervisor.archive(record.run_id, outcome="closed")
                edge.assert_called_once()
                edge_state = edge.call_args.kwargs
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            await supervisor.close()
            return registry, edge_state

        manual_registry, manual_edge = await archive_in_mode(
            "manual", auto_archive=False
        )
        auto_registry, auto_edge = await archive_in_mode(
            "auto", auto_archive=True
        )
        self.assertEqual(manual_registry, auto_registry)
        manual_edge.pop("status_dir")
        auto_edge.pop("status_dir")
        self.assertEqual(manual_edge, auto_edge)

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
