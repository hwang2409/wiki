from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.event_store import replay_raw_jsonl
from backend.app.agent_runtime.fake import FixtureAdapterFactory
from backend.app.agent_runtime.provider import ProviderEvent
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor
from backend.app.agent_runtime.types import ProviderKind, RunRecord


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


def _paths(root: Path) -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=root / "runtime",
        socket_path=root / "runtime" / "supervisor.sock",
        registry_path=root / "isolated-registry.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )


class SupervisorDualWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = RunStore(_paths(self.root))
        self.factory = FixtureAdapterFactory(FIXTURES, pid=os.getpid())
        self.supervisor = Supervisor(self.store, self.factory)
        self.record = self.store.create(
            RunRecord.new(
                agent_id="WIKI-282-TEST",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture",
                worktree=str(self.root),
                prompt="dual write test",
            )
        )

    async def asyncTearDown(self) -> None:
        await self.supervisor.close()
        self.tmp.cleanup()

    def _event(self, method: str, params: dict[str, object]) -> ProviderEvent:
        return ProviderEvent(
            ProviderKind.CODEX,
            {"method": method, "params": params},
            generation=self.record.provider_generation,
        )

    async def _apply(self, event: ProviderEvent) -> None:
        adapter = self.factory(self.record)
        await self.supervisor._handle_provider_event_without_admission(
            self.record.run_id,
            adapter,
            event,
        )

    async def test_dual_write_parity(self) -> None:
        await self._apply(
            self._event(
                "turn/started",
                {"turn": {"id": "turn-1"}},
            )
        )
        raw_rows = list(self.store.iter_raw_events(self.record.run_id))
        normalized_rows = list(self.store.iter_normalized_events(self.record.run_id))
        sqlite_rows = self.supervisor.event_store.view_rows(self.record.run_id)
        self.assertEqual(
            [row[0] for row in sqlite_rows["dispositions"]],
            [row["raw_seq"] for row in normalized_rows],
        )
        self.assertEqual(
            [json.loads(row[3]) for row in sqlite_rows["dispositions"]],
            [
                {
                    "disposition": row["disposition"],
                    "kind": row["kind"],
                    "lifecycle_state": row["lifecycle_state"],
                    "payload": row["payload"],
                    "raw_seq": row["raw_seq"],
                    "seq": row["raw_seq"],
                    "normalized_at": mock.ANY,
                }
                for row in normalized_rows
            ],
        )
        self.assertEqual(
            self.supervisor.event_store.materialized_raw_seqs(self.record.run_id),
            {int(row["seq"]) for row in raw_rows},
        )

    async def test_gap_recovery_rebuilds_to_clean_replay(self) -> None:
        await self._apply(
            self._event("turn/started", {"turn": {"id": "turn-1"}})
        )
        await self._apply(
            self._event("turn/diff/updated", {"diff": "updated"})
        )
        event_store = self.supervisor.event_store
        with event_store.connection() as connection:
            connection.execute(
                "DELETE FROM dispositions WHERE run_id = ? AND raw_seq = 1",
                (self.record.run_id,),
            )
            connection.execute(
                "DELETE FROM events WHERE run_id = ?",
                (self.record.run_id,),
            )
        await self.supervisor.close()
        self.supervisor = Supervisor(self.store, self.factory)
        await self.supervisor.recover_on_start()
        raw_path = self.store.raw_events_path(self.record.run_id)
        clean_path = self.root / "clean.sqlite3"
        clean = replay_raw_jsonl(
            raw_path,
            clean_path,
            run_id=self.record.run_id,
            agent_id=self.record.agent_id,
            provider=ProviderKind.CODEX,
            created_at=self.record.created_at,
        )
        self.assertEqual(
            self.supervisor.event_store.view_rows(self.record.run_id),
            clean.view_rows(self.record.run_id),
        )

    async def test_sse_waits_for_both_durable_outputs(self) -> None:
        original_publish = self.supervisor._publish
        observed: list[bool] = []

        async def publish(event: dict[str, object]) -> None:
            if event.get("type") == "session":
                raw_seq = self.store.get(self.record.run_id).raw_event_count
                observed.append(
                    self.supervisor.event_store.has_disposition(
                        self.record.run_id, raw_seq
                    )
                    and raw_seq
                    in {
                        int(row["raw_seq"])
                        for row in self.store.iter_normalized_events(
                            self.record.run_id
                        )
                    }
                )
            await original_publish(event)

        with mock.patch.object(self.supervisor, "_publish", side_effect=publish):
            await self._apply(
                self._event("turn/started", {"turn": {"id": "turn-1"}})
            )
        self.assertTrue(observed)
        self.assertTrue(all(observed))

    async def test_stalled_materializer_blocks_without_losing_raw_row(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_materialize = self.supervisor.event_store.materialize

        def stalled(*args: object, **kwargs: object) -> object:
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("stalled materializer was not released")
            return original_materialize(*args, **kwargs)

        adapter = self.factory(self.record)
        with mock.patch.object(
            self.supervisor.event_store,
            "materialize",
            side_effect=stalled,
        ):
            task = threading.Thread(
                target=lambda: asyncio.run(
                    self.supervisor._handle_provider_event_without_admission(
                        self.record.run_id,
                        adapter,
                        self._event("turn/started", {"turn": {"id": "turn-1"}}),
                    )
                )
            )
            task.start()
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(
                len(list(self.store.iter_raw_events(self.record.run_id))),
                1,
            )
            release.set()
            task.join(timeout=5)
            self.assertFalse(task.is_alive())
        self.assertEqual(
            self.supervisor.event_store.materialized_raw_seqs(self.record.run_id),
            {1},
        )
        self.assertEqual(
            self.supervisor.materializer_metrics()["queue_depth"][self.record.run_id],
            0,
        )
