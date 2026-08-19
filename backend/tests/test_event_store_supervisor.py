from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.event_store import (
    NORMALIZER_VERSION,
    SQLiteEventStore,
    replay_raw_jsonl,
)
from backend.app.agent_runtime.fake import FixtureAdapterFactory
from backend.app.agent_runtime.provider import ProviderEvent
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor
from backend.app.agent_runtime.types import (
    EventDisposition,
    LifecycleState,
    ProviderKind,
    RunRecord,
)


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
        self.store.track_pending_user_message(
            self.record.run_id,
            "pending-1",
            "hello",
        )
        await self._apply(
            self._event(
                "turn/started",
                {"turn": {"id": "turn-1"}},
            )
        )
        await self._apply(
            self._event(
                "item/completed",
                {
                    "item": {
                        "type": "userMessage",
                        "id": "user-1",
                        "content": [{"type": "text", "text": "hello"}],
                    }
                },
            )
        )
        await self._apply(
            self._event("turn/diff/updated", {"diff": "updated"})
        )
        raw_rows = list(self.store.iter_raw_events(self.record.run_id))
        normalized_rows = list(self.store.iter_normalized_events(self.record.run_id))
        sqlite_rows = self.supervisor.event_store.view_rows(self.record.run_id)
        with self.supervisor.event_store.for_run(self.record.run_id).connection(
            read_only=True
        ) as connection:
            dispositions = connection.execute(
                "SELECT raw_seq, normalized_json, normalizer_version, created_at "
                "FROM dispositions WHERE run_id = ? ORDER BY raw_seq",
                (self.record.run_id,),
            ).fetchall()
        expected = self.supervisor.event_store._expected_view_rows(  # noqa: SLF001
            self.record.run_id,
            provider="codex",
            agent_id=self.record.agent_id,
            created_at=self.record.created_at,
            state=self.record.state.value,
            dispositions=dispositions,
        )
        for key in ("events", "patches", "cursors", "projections"):
            self.assertEqual(sqlite_rows[key], expected[key])
        self.assertEqual(sqlite_rows["dispositions"], expected["dispositions"])
        projection = sqlite_rows["projections"][0]
        record = self.store.get(self.record.run_id)
        sqlite_composer = json.loads(projection[5])
        legacy_composer = [dict(message) for message in record.composer_messages]
        for message in sqlite_composer:
            message.pop("echoed_at", None)
        for message in legacy_composer:
            message.pop("echoed_at", None)
        self.assertEqual(sqlite_composer, legacy_composer)
        self.assertEqual(int(projection[8]), record.unread_event_seq)
        self.assertEqual(json.loads(projection[6]), record.disposition_counts)
        self.assertEqual(
            json.loads(projection[3]),
            record.to_dict().get("session_meta", {}),
        )
        self.assertEqual(
            [row["raw_seq"] for row in normalized_rows],
            [row[0] for row in sqlite_rows["dispositions"]],
        )
        self.assertEqual(
            self.supervisor.event_store.materialized_raw_seqs(self.record.run_id),
            {int(row["seq"]) for row in raw_rows},
        )

    async def test_recovery_schedules_archive_backfill(self) -> None:
        with mock.patch(
            "backend.app.agent_runtime.archive_parity.backfill_headless_runs",
            return_value=[],
        ) as backfill:
            await self.supervisor.recover_on_start()
            task = self.supervisor.archive_backfill_task
            self.assertIsNotNone(task)
            await task

        backfill.assert_called_once_with(
            self.store,
            self.supervisor.event_store,
            batch_size=32,
        )

    async def test_close_releases_event_store_after_archive_backfill_failure(self) -> None:
        async def fail_backfill() -> None:
            raise RuntimeError("archive backfill failed")

        shard = self.supervisor.event_store.for_run(self.record.run_id)
        self.supervisor.archive_backfill_worker = asyncio.create_task(fail_backfill())
        with self.assertRaisesRegex(RuntimeError, "archive backfill failed"):
            await self.supervisor.close()
        self.assertTrue(shard.closed)

    async def test_archive_closes_the_run_event_store_shard(self) -> None:
        await self._apply(
            self._event("turn/started", {"turn": {"id": "turn-1"}})
        )
        shard = self.supervisor.event_store.for_run(self.record.run_id)
        await self.supervisor.archive(self.record.run_id)
        self.assertTrue(shard.closed)
        self.assertNotIn(self.record.run_id, self.supervisor.event_store._stores)  # noqa: SLF001

    async def test_close_drains_archive_backfill_after_cancellation(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def wait_backfill() -> None:
            started.set()
            await release.wait()

        shard = self.supervisor.event_store.for_run(self.record.run_id)
        self.supervisor.archive_backfill_worker = asyncio.create_task(wait_backfill())
        close_task = asyncio.create_task(self.supervisor.close())
        await started.wait()
        close_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(shard.closed)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await close_task
        self.assertTrue(shard.closed)

    async def test_legacy_append_survives_materializer_failure(self) -> None:
        adapter = self.factory(self.record)
        with mock.patch.object(
            self.supervisor.event_store,
            "materialize",
            side_effect=RuntimeError("injected materializer failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected materializer failure"):
                await self.supervisor._handle_provider_event_without_admission(
                    self.record.run_id,
                    adapter,
                    self._event("turn/started", {"turn": {"id": "turn-1"}}),
                )
        self.assertEqual(
            len(list(self.store.iter_raw_events(self.record.run_id))),
            1,
        )
        self.assertEqual(
            len(list(self.store.iter_normalized_events(self.record.run_id))),
            1,
        )

    async def test_sqlite_pipeline_write_failure_blocks_and_transitions(self) -> None:
        adapter = self.factory(self.record)
        with mock.patch.object(
            self.supervisor.event_store,
            "materialize",
            side_effect=sqlite3.OperationalError("database disk image is malformed"),
        ) as materialize:
            await adapter._events.put(  # noqa: SLF001
                self._event("turn/started", {"turn": {"id": "turn-1"}})
            )
            task = asyncio.create_task(
                self.supervisor._pump_events(self.record.run_id, adapter)
            )
            for _ in range(100):
                if self.store.get(self.record.run_id).state is LifecycleState.BLOCKED:
                    break
                await asyncio.sleep(0.01)
            await asyncio.wait_for(task, timeout=5)

        materialize.assert_called_once()
        blocked = self.store.get(self.record.run_id)
        self.assertEqual(blocked.state, LifecycleState.BLOCKED)
        self.assertEqual(
            blocked.state_reason,
            "provider event persistence failed: database disk image is malformed",
        )
        self.assertTrue(adapter.closed)

    async def test_gap_recovery_rebuilds_to_clean_replay(self) -> None:
        await self._apply(
            self._event("turn/started", {"turn": {"id": "turn-1"}})
        )
        await self._apply(
            self._event("turn/diff/updated", {"diff": "updated"})
        )
        event_store = self.supervisor.event_store.for_run(self.record.run_id)
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

    async def test_recovery_preserves_legacy_normalized_sequence(self) -> None:
        self.store.track_pending_user_message(
            self.record.run_id,
            "pending-1",
            "hello",
        )
        raw_one = self.store.append_raw(
            self.record.run_id,
            provider="codex",
            direction="provider",
            payload={
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "hello"}],
                    }
                },
            },
        )
        self.store.append_raw(
            self.record.run_id,
            provider="codex",
            direction="provider",
            payload={"method": "warning", "params": {"message": "worker"}},
        )
        self.store.append_normalized(
            self.record.run_id,
            raw_seq=2,
            disposition=EventDisposition.IGNORED,
            kind="warning",
            payload={"method": "warning", "params": {"message": "worker"}},
        )
        await self.supervisor.close()
        self.supervisor = Supervisor(self.store, self.factory)
        await self.supervisor.recover_on_start()
        projection = self.supervisor.event_store.view_rows(self.record.run_id)[
            "projections"
        ][0]
        composer = json.loads(projection[5])
        self.assertEqual(composer[0]["pending_id"], "pending-1")
        self.assertEqual(composer[0]["seq"], 2)
        normalized = list(self.store.iter_normalized_events(self.record.run_id))
        self.assertEqual(
            [(row["raw_seq"], row["seq"]) for row in normalized],
            [(2, 1), (1, 2)],
        )
        self.assertEqual(raw_one["seq"], 1)
        sqlite_rows = self.supervisor.event_store.view_rows(self.record.run_id)
        with self.supervisor.event_store.for_run(self.record.run_id).connection(
            read_only=True
        ) as connection:
            created_at_by_raw_seq = {
                int(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT raw_seq, created_at FROM dispositions "
                    "WHERE run_id = ?",
                    (self.record.run_id,),
                ).fetchall()
            }
        legacy_dispositions = []
        for row in normalized:
            normalized_json = json.dumps(
                {
                    "disposition": row["disposition"],
                    "kind": row["kind"],
                    "lifecycle_state": row["lifecycle_state"],
                    "normalized_at": row["normalized_at"],
                    "payload": row["payload"],
                    "raw_seq": row["raw_seq"],
                    "seq": row["seq"],
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            legacy_dispositions.append(
                (
                    row["raw_seq"],
                    normalized_json,
                    NORMALIZER_VERSION,
                    created_at_by_raw_seq[int(row["raw_seq"])],
                )
            )
        expected = self.supervisor.event_store._expected_view_rows(  # noqa: SLF001
            self.record.run_id,
            provider="codex",
            agent_id=self.record.agent_id,
            created_at=self.record.created_at,
            state=self.store.get(self.record.run_id).state.value,
            dispositions=legacy_dispositions,
        )
        for key in ("events", "patches", "cursors", "projections"):
            self.assertEqual(sqlite_rows[key], expected[key])
        legacy_record = self.store.get(self.record.run_id)
        sqlite_composer = json.loads(sqlite_rows["projections"][0][5])
        for message in sqlite_composer:
            message.pop("echoed_at", None)
        self.assertEqual(
            sqlite_composer,
            [
                {
                    key: value
                    for key, value in message.items()
                    if key != "echoed_at"
                }
                for message in legacy_record.composer_messages
            ],
        )
        self.assertEqual(
            sqlite_rows["projections"][0][8],
            legacy_record.unread_event_seq,
        )

    async def test_half_applied_migration_health_rebuilds_cleanly(self) -> None:
        await self._apply(
            self._event("turn/started", {"turn": {"id": "turn-1"}})
        )
        with self.supervisor.event_store.for_run(self.record.run_id).connection() as connection:
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = 2"
            )
        self.assertFalse(
            self.supervisor.event_store.run_is_healthy(self.record.run_id)
        )
        await self.supervisor._rebuild_materializer_database(self.record.run_id)  # noqa: SLF001
        self.assertTrue(
            self.supervisor.event_store.run_is_healthy(self.record.run_id)
        )

    async def test_rebuild_reopens_new_database_for_post_swap_writes(self) -> None:
        await self._apply(
            self._event("turn/started", {"turn": {"id": "turn-1"}})
        )
        target = self.supervisor.event_store.for_run(self.record.run_id)
        database_path = target.path
        old_inode = database_path.stat().st_ino

        await self.supervisor._rebuild_materializer_database(self.record.run_id)  # noqa: SLF001

        self.assertNotEqual(database_path.stat().st_ino, old_inode)
        with target.connection() as connection:
            connection.execute(
                "UPDATE run_cursors SET rebuild_state = 'ready' WHERE run_id = ?",
                (self.record.run_id,),
            )
        with sqlite3.connect(database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT rebuild_state FROM run_cursors WHERE run_id = ?",
                    (self.record.run_id,),
                ).fetchone()[0],
                "ready",
            )

    async def test_rebuild_invalidates_all_pooled_store_handles(self) -> None:
        await self._apply(
            self._event("turn/started", {"turn": {"id": "turn-1"}})
        )
        target = self.supervisor.event_store.for_run(self.record.run_id)
        database_path = target.path
        other_store = SQLiteEventStore(database_path, migrate=False)
        old_handles = []
        try:
            with self.assertRaises(sqlite3.ProgrammingError):
                with target.connection() as first_connection:
                    with other_store.connection() as second_connection:
                        old_handles.extend((first_connection, second_connection))
                        await self.supervisor._rebuild_materializer_database(
                            self.record.run_id
                        )  # noqa: SLF001
                        for connection in old_handles:
                            with self.assertRaises(sqlite3.ProgrammingError):
                                connection.execute("SELECT 1")

            with other_store.connection() as connection:
                connection.execute(
                    "UPDATE run_cursors SET rebuild_state = 'ready' WHERE run_id = ?",
                    (self.record.run_id,),
                )
            with sqlite3.connect(database_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT rebuild_state FROM run_cursors WHERE run_id = ?",
                        (self.record.run_id,),
                    ).fetchone()[0],
                    "ready",
                )
        finally:
            other_store.close()

    async def test_unhealthy_zero_event_database_rebuilds(self) -> None:
        self.supervisor.event_store.for_run(self.record.run_id).ensure_schema()
        self.supervisor.event_store.create_run(
            self.record.run_id,
            agent_id=self.record.agent_id,
            provider=self.record.provider,
            created_at=self.record.created_at,
            state=self.record.state,
        )
        with self.supervisor.event_store.for_run(self.record.run_id).connection() as connection:
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = 2"
            )
        self.assertFalse(
            self.supervisor.event_store.run_is_healthy(self.record.run_id)
        )
        await self.supervisor._normalize_orphan_raw_events()  # noqa: SLF001
        self.assertTrue(
            self.supervisor.event_store.run_is_healthy(self.record.run_id)
        )

    async def test_startup_repair_failure_is_scoped_to_one_run(self) -> None:
        other = self.store.create(
            RunRecord.new(
                agent_id="WIKI-OTHER-REPAIR",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture",
                worktree=str(self.root),
                prompt="repair test",
            )
        )
        for record in (self.record, other):
            self.supervisor.event_store.create_run(
                record.run_id,
                agent_id=record.agent_id,
                provider=record.provider,
                created_at=record.created_at,
                state=LifecycleState.WORKING,
            )
            self.store.transition(record.run_id, LifecycleState.WORKING)
        damaged_path = self.supervisor.event_store.for_run(self.record.run_id).path
        self.supervisor.event_store.close_run(self.record.run_id)
        damaged_path.write_bytes(b"damaged")
        for suffix in ("-wal", "-shm"):
            damaged_path.with_name(damaged_path.name + suffix).unlink(missing_ok=True)
        damaged_store = self.supervisor.event_store.for_run(self.record.run_id)
        with mock.patch.object(
            damaged_store,
            "replace_run_from",
            side_effect=OSError("damaged run cannot be replaced"),
        ):
            await self.supervisor._normalize_orphan_raw_events()

        self.assertEqual(
            self.store.get(self.record.run_id).state,
            LifecycleState.BLOCKED,
        )
        self.assertTrue(self.supervisor.event_store.run_is_healthy(other.run_id))

    async def test_failed_rebuild_does_not_reattach_run_and_other_run_recovers(
        self,
    ) -> None:
        other = self.store.create(
            RunRecord.new(
                agent_id="WIKI-OTHER-REATTACH",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture",
                worktree=str(self.root),
                prompt="reattach test",
            )
        )
        for record in (self.record, other):
            record.state = LifecycleState.WORKING
            record.provider_session_id = f"session-{record.run_id}"
            self.store._write_record(record)  # noqa: SLF001
            self.supervisor.event_store.create_run(
                record.run_id,
                agent_id=record.agent_id,
                provider=record.provider,
                created_at=record.created_at,
                state=LifecycleState.WORKING,
            )

        damaged_path = self.supervisor.event_store.for_run(self.record.run_id).path
        self.supervisor.event_store.close_run(self.record.run_id)
        damaged_path.write_bytes(b"damaged")
        for suffix in ("-wal", "-shm"):
            damaged_path.with_name(damaged_path.name + suffix).unlink(missing_ok=True)
        damaged_store = self.supervisor.event_store.for_run(self.record.run_id)
        with mock.patch.object(
            damaged_store,
            "replace_run_from",
            side_effect=OSError("damaged run cannot be replaced"),
        ) as replace_run:
            await self.supervisor._normalize_orphan_raw_events()  # noqa: SLF001
            results = await self.supervisor._recover_once()  # noqa: SLF001

        self.assertTrue(replace_run.called)
        failed_result = next(
            result for result in results if result["run_id"] == self.record.run_id
        )
        self.assertEqual(failed_result["action"], "block")
        self.assertNotIn(self.record.run_id, self.supervisor.adapters)
        self.assertIn(other.run_id, self.supervisor.adapters)
        self.assertTrue(self.store.get(self.record.run_id).automatic_resume_suppressed)

    async def test_corrupt_raw_boot_blocks_one_run_and_attaches_other(self) -> None:
        other = self.store.create(
            RunRecord.new(
                agent_id="WIKI-OTHER-CORRUPT-RAW",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture",
                worktree=str(self.root),
                prompt="corrupt raw test",
            )
        )
        for record in (self.record, other):
            record.state = LifecycleState.WORKING
            record.provider_session_id = f"session-{record.run_id}"
            self.store._write_record(record)  # noqa: SLF001
            self.supervisor.event_store.create_run(
                record.run_id,
                agent_id=record.agent_id,
                provider=record.provider,
                created_at=record.created_at,
                state=LifecycleState.WORKING,
            )
        await self.supervisor.close()
        self.store.raw_events_path(self.record.run_id).write_text(
            "not-json\n",
            encoding="utf-8",
        )
        self.supervisor = Supervisor(self.store, self.factory)

        results = await self.supervisor.recover_on_start()

        failed_result = next(
            result for result in results if result["run_id"] == self.record.run_id
        )
        self.assertEqual(failed_result["action"], "block")
        self.assertEqual(
            self.store.get(self.record.run_id).state,
            LifecycleState.BLOCKED,
        )
        self.assertNotIn(self.record.run_id, self.supervisor.adapters)
        self.assertIn(other.run_id, self.supervisor.adapters)

    async def test_rebuild_failure_keeps_original_database_unswapped(self) -> None:
        await self._apply(
            self._event("turn/started", {"turn": {"id": "turn-1"}})
        )
        database_path = self.supervisor.event_store.for_run(self.record.run_id).path
        with self.supervisor.event_store.for_run(self.record.run_id).connection() as connection:
            connection.execute(
                "DELETE FROM events WHERE run_id = ?",
                (self.record.run_id,),
            )
        with mock.patch(
            "backend.app.agent_runtime.event_store.os.replace",
            side_effect=OSError("injected swap failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected swap failure"):
                await self.supervisor._rebuild_materializer_database(self.record.run_id)
        with self.supervisor.event_store.for_run(self.record.run_id).connection(
            read_only=True
        ) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE run_id = ?",
                    (self.record.run_id,),
                ).fetchone()[0],
                0,
            )
        self.assertEqual(
            list(database_path.parent.glob("events-rebuild-*")),
            [],
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
        gate = asyncio.Event()
        self.supervisor.materializer_gate = gate
        original_materialize = self.supervisor.event_store.materialize
        call_count = 0

        def stalled(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            return original_materialize(*args, **kwargs)

        first_adapter = self.factory(self.record)
        second_adapter = self.factory(self.record)
        with mock.patch.object(
            self.supervisor.event_store,
            "materialize",
            side_effect=stalled,
        ):
            await first_adapter._events.put(  # noqa: SLF001
                self._event("turn/started", {"turn": {"id": "turn-1"}})
            )
            first_task = asyncio.create_task(
                self.supervisor._pump_events(self.record.run_id, first_adapter)
            )
            for _ in range(100):
                if self.store.get(self.record.run_id).raw_event_count == 1:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.store.get(self.record.run_id).raw_event_count, 1)
            await second_adapter._events.put(  # noqa: SLF001
                self._event("turn/diff/updated", {"diff": "second"})
            )
            second_task = asyncio.create_task(
                self.supervisor._pump_events(self.record.run_id, second_adapter)
            )
            await asyncio.sleep(0.05)
            self.assertEqual(call_count, 0)
            self.assertEqual(
                len(list(self.store.iter_raw_events(self.record.run_id))),
                1,
            )
            gate.set()
            await first_adapter._events.put(None)  # noqa: SLF001
            await second_adapter._events.put(None)  # noqa: SLF001
            await asyncio.wait_for(
                asyncio.gather(first_task, second_task),
                timeout=5,
            )
        self.assertEqual(call_count, 2)
        self.assertEqual(
            len(list(self.store.iter_raw_events(self.record.run_id))),
            2,
        )
        self.assertEqual(
            self.supervisor.event_store.materialized_raw_seqs(self.record.run_id),
            {1, 2},
        )
        self.assertEqual(
            self.supervisor.materializer_metrics()["queue_depth"][self.record.run_id],
            0,
        )
