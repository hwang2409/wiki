from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.event_store import (
    NORMALIZER_VERSION,
    SQLiteEventStore,
    replay_raw_jsonl,
)
from backend.app.agent_runtime.fake import FixtureAdapterFactory
from backend.app.agent_runtime.normalizer import normalize_provider_event
from backend.app.agent_runtime.provider import ProviderEvent
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import _BoundedPersistenceWriter, Supervisor
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
        persistence = self.supervisor.materializer_metrics()["persistence"]
        self.assertGreaterEqual(persistence["count"], 3)
        self.assertEqual(persistence["queue_depth"], 0)
        self.assertEqual(
            set(persistence["latency_seconds"]),
            {"p50", "p95", "p99"},
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
        materialize_started = threading.Event()
        release_materialize = threading.Event()
        original_materialize = self.supervisor.event_store.materialize
        call_count = 0

        def stalled(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            materialize_started.set()
            release_materialize.wait(timeout=10)
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
            await asyncio.to_thread(materialize_started.wait, 10)
            self.assertEqual(self.store.get(self.record.run_id).raw_event_count, 1)
            await second_adapter._events.put(  # noqa: SLF001
                self._event("turn/diff/updated", {"diff": "second"})
            )
            second_task = asyncio.create_task(
                self.supervisor._pump_events(self.record.run_id, second_adapter)
            )
            await asyncio.sleep(0.05)
            self.assertEqual(call_count, 1)
            self.assertEqual(
                len(list(self.store.iter_raw_events(self.record.run_id))),
                1,
            )
            release_materialize.set()
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

    async def test_writer_preserves_per_run_order_across_concurrent_burst(self) -> None:
        records = [
            self.store.create(
                RunRecord.new(
                    agent_id=f"WIKI-358-ORDER-{index}",
                    provider=ProviderKind.CODEX,
                    role="implement",
                    model="fixture",
                    worktree=str(self.root),
                    prompt="writer ordering test",
                )
            )
            for index in range(3)
        ]
        adapters = [self.factory(record) for record in records]
        raw_committed: set[tuple[str, int]] = set()
        ordering_lock = threading.Lock()
        original_append_raw = self.store.append_raw
        original_append_normalized = self.store.append_normalized

        def append_raw(*args: object, **kwargs: object) -> dict[str, object]:
            row = original_append_raw(*args, **kwargs)  # type: ignore[arg-type]
            with ordering_lock:
                raw_committed.add((str(args[0]), int(row["seq"])))
            return row

        def append_normalized(*args: object, **kwargs: object) -> dict[str, object]:
            run_id = str(args[0])
            raw_seq = int(kwargs["raw_seq"])
            with ordering_lock:
                self.assertIn((run_id, raw_seq), raw_committed)
            return original_append_normalized(  # type: ignore[arg-type]
                *args,
                **kwargs,
            )

        self.supervisor.expected_stream_ends.update(id(adapter) for adapter in adapters)
        try:
            with (
                mock.patch.object(self.store, "append_raw", side_effect=append_raw),
                mock.patch.object(
                    self.store,
                    "append_normalized",
                    side_effect=append_normalized,
                ),
            ):
                tasks = []
                for record, adapter in zip(records, adapters):
                    for index in range(100):
                        await adapter._events.put(  # noqa: SLF001
                            ProviderEvent(
                                ProviderKind.CODEX,
                                {
                                    "method": "turn/diff/updated",
                                    "params": {"diff": f"{record.run_id}-{index}"},
                                },
                                generation=record.provider_generation,
                            )
                        )
                    await adapter._events.put(None)  # noqa: SLF001
                    tasks.append(
                        asyncio.create_task(
                            self.supervisor._pump_events(record.run_id, adapter)
                        )
                    )
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
        finally:
            self.supervisor.expected_stream_ends.difference_update(
                id(adapter) for adapter in adapters
            )

        for record in records:
            raw = list(self.store.iter_raw_events(record.run_id))
            normalized = list(self.store.iter_normalized_events(record.run_id))
            self.assertEqual([int(row["seq"]) for row in raw], list(range(1, 101)))
            self.assertEqual(
                [int(row["raw_seq"]) for row in normalized],
                list(range(1, 101)),
            )
            self.assertEqual(
                self.supervisor.event_store.materialized_raw_seqs(record.run_id),
                set(range(1, 101)),
            )

    async def test_writer_preserves_order_for_concurrent_same_run_submissions(
        self,
    ) -> None:
        observed: list[str] = []
        original_append_raw = self.store.append_raw

        def append_raw(*args: object, **kwargs: object) -> dict[str, object]:
            payload = kwargs["payload"]
            assert isinstance(payload, dict)
            params = payload["params"]
            assert isinstance(params, dict)
            observed.append(str(params["diff"]))
            time.sleep(0.005)
            return original_append_raw(*args, **kwargs)  # type: ignore[arg-type]

        events = [
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "turn/diff/updated",
                    "params": {"diff": f"same-run-{index}"},
                },
                generation=self.record.provider_generation,
            )
            for index in range(8)
        ]
        with mock.patch.object(self.store, "append_raw", side_effect=append_raw):
            await asyncio.gather(
                *(
                    self.supervisor._persist_event_bundle_async(
                        self.record,
                        event,
                        normalize_provider_event(
                            event.provider,
                            event.payload,
                            direction=event.direction,
                        ),
                        event.payload["params"],
                    )
                    for event in events
                )
            )

        self.assertEqual(observed, [f"same-run-{index}" for index in range(8)])
        self.assertEqual(
            [int(row["raw_seq"]) for row in self.store.iter_normalized_events(
                self.record.run_id
            )],
            list(range(1, 9)),
        )

    async def test_event_write_cannot_overwrite_concurrent_transition(self) -> None:
        write_started = threading.Event()
        release_write = threading.Event()
        transition_finished = threading.Event()
        original_write = self.store._write_record  # noqa: SLF001

        def blocked_write(record: RunRecord) -> None:
            write_started.set()
            release_write.wait(timeout=10)
            original_write(record)

        def append() -> None:
            self.store.append_raw(
                self.record.run_id,
                provider="codex",
                direction="server",
                payload={"method": "turn/started"},
            )

        def transition() -> None:
            self.store.transition(self.record.run_id, LifecycleState.BLOCKED)
            transition_finished.set()

        with mock.patch.object(self.store, "_write_record", side_effect=blocked_write):
            append_thread = threading.Thread(target=append)
            append_thread.start()
            self.assertTrue(write_started.wait(timeout=10))
            transition_thread = threading.Thread(target=transition)
            transition_thread.start()
            self.assertFalse(transition_finished.wait(timeout=0.05))
            release_write.set()
            append_thread.join(timeout=10)
            transition_thread.join(timeout=10)

        self.assertTrue(transition_finished.is_set())
        record = self.store.get(self.record.run_id)
        self.assertEqual(record.state, LifecycleState.BLOCKED)
        self.assertEqual(record.raw_event_count, 1)

    async def test_event_write_cannot_overwrite_concurrent_viewed_cursor(self) -> None:
        self.store.append_raw(
            self.record.run_id,
            provider="codex",
            direction="server",
            payload={"method": "turn/started"},
        )
        write_started = threading.Event()
        release_write = threading.Event()
        viewed_finished = threading.Event()
        original_write = self.store._write_record  # noqa: SLF001

        def blocked_write(record: RunRecord) -> None:
            write_started.set()
            release_write.wait(timeout=10)
            original_write(record)

        def append_normalized() -> None:
            self.store.append_normalized(
                self.record.run_id,
                raw_seq=1,
                disposition=EventDisposition.RENDERED,
                kind="turn_started",
                payload={},
            )

        def mark_viewed() -> None:
            self.store.mark_viewed(self.record.run_id)
            viewed_finished.set()

        with mock.patch.object(self.store, "_write_record", side_effect=blocked_write):
            append_thread = threading.Thread(target=append_normalized)
            append_thread.start()
            self.assertTrue(write_started.wait(timeout=10))
            viewed_thread = threading.Thread(target=mark_viewed)
            viewed_thread.start()
            self.assertFalse(viewed_finished.wait(timeout=0.05))
            release_write.set()
            append_thread.join(timeout=10)
            viewed_thread.join(timeout=10)

        self.assertTrue(viewed_finished.is_set())
        record = self.store.get(self.record.run_id)
        self.assertEqual(record.normalized_event_count, 1)
        self.assertEqual(record.last_viewed_seq, 1)

    async def test_model_changed_event_uses_writer_and_awaits_durability(self) -> None:
        started = threading.Event()
        release = threading.Event()
        original_append_raw = self.store.append_raw

        def blocked_append_raw(*args: object, **kwargs: object) -> dict[str, object]:
            started.set()
            release.wait(timeout=10)
            return original_append_raw(*args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(
            self.store, "append_raw", side_effect=blocked_append_raw
        ):
            task = asyncio.create_task(
                self.supervisor._append_model_changed_event(
                    self.record,
                    old_model="fixture",
                    new_model="fixture-2",
                    trigger="test",
                )
            )
            await asyncio.to_thread(started.wait, 10)
            self.assertFalse(task.done())
            self.assertEqual(
                (await self.supervisor.dispatch("ping", {}))["status"],
                "ok",
            )
            release.set()
            await asyncio.wait_for(task, timeout=10)

        raw = list(self.store.iter_raw_events(self.record.run_id))
        normalized = list(self.store.iter_normalized_events(self.record.run_id))
        self.assertEqual(raw[0]["provider"], "supervisor")
        self.assertEqual(normalized[0]["kind"], "model_changed")

    async def test_startup_events_use_normal_writer_without_dropping_tail(self) -> None:
        adapter = self.factory(self.record)
        events = [
            self._event("turn/started", {"turn": {"id": "startup-1"}}),
            self._event("turn/diff/updated", {"diff": "startup-2"}),
            self._event("turn/diff/updated", {"diff": "startup-3"}),
        ]
        for event in events:
            await adapter._events.put(event)  # noqa: SLF001
        await adapter._events.put(None)  # noqa: SLF001
        self.supervisor.expected_stream_ends.add(id(adapter))
        try:
            self.supervisor._attach_adapter(self.record.run_id, adapter)
            await asyncio.wait_for(
                self.supervisor.event_tasks[self.record.run_id], timeout=10
            )
        finally:
            self.supervisor.expected_stream_ends.discard(id(adapter))

        self.assertEqual(self.store.get(self.record.run_id).raw_event_count, 3)
        self.assertEqual(
            [row["raw_seq"] for row in self.store.iter_normalized_events(
                self.record.run_id
            )],
            [1, 2, 3],
        )

    async def test_writer_close_drains_admitted_capacity_waiters(self) -> None:
        writer = _BoundedPersistenceWriter(
            workers=1,
            queue_size=0,
            per_run_queue_size=1,
        )
        started = threading.Event()
        release = threading.Event()
        completed: list[int] = []

        def blocked(value: int) -> int:
            started.set()
            release.wait(timeout=10)
            completed.append(value)
            return value

        first = asyncio.create_task(writer.submit("run", blocked, 1))
        await asyncio.to_thread(started.wait, 10)
        second = asyncio.create_task(writer.submit("run", blocked, 2))
        await asyncio.sleep(0)
        close_task = asyncio.create_task(writer.close())
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeError):
            await writer.submit("run", blocked, 3)
        self.assertFalse(close_task.done())
        release.set()
        self.assertEqual(await asyncio.wait_for(first, timeout=10), 1)
        self.assertEqual(await asyncio.wait_for(second, timeout=10), 2)
        await asyncio.wait_for(close_task, timeout=10)
        self.assertEqual(completed, [1, 2])

    async def test_startup_event_persistence_failure_blocks_run(self) -> None:
        adapter = self.factory(self.record)
        with mock.patch.object(
            self.supervisor.event_store,
            "materialize",
            side_effect=sqlite3.OperationalError("startup materializer failure"),
        ):
            await adapter._events.put(  # noqa: SLF001
                self._event("turn/started", {"turn": {"id": "startup"}})
            )
            self.supervisor._attach_adapter(self.record.run_id, adapter)
            task = self.supervisor.event_tasks[self.record.run_id]
            await asyncio.wait_for(task, timeout=5)

        blocked = self.store.get(self.record.run_id)
        self.assertEqual(blocked.state, LifecycleState.BLOCKED)
        self.assertEqual(
            blocked.state_reason,
            "provider event persistence failed: startup materializer failure",
        )

    async def test_writer_keeps_ping_responsive_during_blocked_burst(self) -> None:
        records = [
            self.store.create(
                RunRecord.new(
                    agent_id=f"WIKI-358-PING-{index}",
                    provider=ProviderKind.CODEX,
                    role="implement",
                    model="fixture",
                    worktree=str(self.root),
                    prompt="writer responsiveness test",
                )
            )
            for index in range(3)
        ]
        adapters = [self.factory(record) for record in records]
        materialize_started = threading.Event()
        release_materialize = threading.Event()
        original_materialize = self.supervisor.event_store.materialize

        def blocked_materialize(*args: object, **kwargs: object) -> object:
            materialize_started.set()
            release_materialize.wait(timeout=10)
            return original_materialize(*args, **kwargs)  # type: ignore[arg-type]

        self.supervisor.expected_stream_ends.update(id(adapter) for adapter in adapters)
        tasks: list[asyncio.Task[None]] = []
        try:
            with mock.patch.object(
                self.supervisor.event_store,
                "materialize",
                side_effect=blocked_materialize,
            ):
                for record, adapter in zip(records, adapters):
                    for index in range(40):
                        await adapter._events.put(  # noqa: SLF001
                            ProviderEvent(
                                ProviderKind.CODEX,
                                {
                                    "method": "turn/diff/updated",
                                    "params": {"diff": f"burst-{index}"},
                                },
                                generation=record.provider_generation,
                            )
                        )
                    await adapter._events.put(None)  # noqa: SLF001
                    tasks.append(
                        asyncio.create_task(
                            self.supervisor._pump_events(record.run_id, adapter)
                        )
                    )
                await asyncio.to_thread(materialize_started.wait, 10)
                started = time.perf_counter()
                await asyncio.wait_for(
                    self.supervisor.dispatch("ping", {}),
                    timeout=3,
                )
                self.assertLess(time.perf_counter() - started, 1)
        finally:
            release_materialize.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.supervisor.expected_stream_ends.difference_update(
                id(adapter) for adapter in adapters
            )

    async def test_writer_failure_blocks_only_the_event_run(self) -> None:
        other = self.store.create(
            RunRecord.new(
                agent_id="WIKI-358-FAILURE-OTHER",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture",
                worktree=str(self.root),
                prompt="failure isolation test",
            )
        )
        failing_adapter = self.factory(self.record)
        other_adapter = self.factory(other)
        original_materialize = self.supervisor.event_store.materialize

        def fail_one_run(*args: object, **kwargs: object) -> object:
            if str(args[0]) == self.record.run_id:
                raise sqlite3.OperationalError("injected writer failure")
            return original_materialize(*args, **kwargs)  # type: ignore[arg-type]

        self.supervisor.expected_stream_ends.add(id(other_adapter))
        with mock.patch.object(
            self.supervisor.event_store,
            "materialize",
            side_effect=fail_one_run,
        ):
            await failing_adapter._events.put(  # noqa: SLF001
                self._event("turn/started", {"turn": {"id": "failed"}})
            )
            await failing_adapter._events.put(None)  # noqa: SLF001
            for index in range(3):
                await other_adapter._events.put(  # noqa: SLF001
                    ProviderEvent(
                        ProviderKind.CODEX,
                        {
                            "method": "turn/diff/updated",
                            "params": {"diff": f"other-{index}"},
                        },
                        generation=other.provider_generation,
                    )
                )
            await other_adapter._events.put(None)  # noqa: SLF001
            await asyncio.wait_for(
                asyncio.gather(
                    self.supervisor._pump_events(
                        self.record.run_id,
                        failing_adapter,
                    ),
                    self.supervisor._pump_events(other.run_id, other_adapter),
                ),
                timeout=10,
            )
        self.supervisor.expected_stream_ends.discard(id(other_adapter))

        failed = self.store.get(self.record.run_id)
        self.assertEqual(failed.state, LifecycleState.BLOCKED)
        self.assertEqual(
            failed.state_reason,
            "provider event persistence failed: injected writer failure",
        )
        self.assertEqual(
            len(list(self.store.iter_normalized_events(other.run_id))),
            3,
        )
        self.assertEqual(len(list(self.store.iter_raw_events(other.run_id))), 3)

    async def test_writer_backpressure_waits_and_recovers(self) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(self.store, self.factory)
        self.supervisor.persistence_writer = _BoundedPersistenceWriter(
            workers=1,
            queue_size=0,
            per_run_queue_size=1,
        )
        other = self.store.create(
            RunRecord.new(
                agent_id="WIKI-358-BACKPRESSURE-OTHER",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture",
                worktree=str(self.root),
                prompt="backpressure test",
            )
        )
        first_adapter = self.factory(self.record)
        other_adapter = self.factory(other)
        materialize_started = threading.Event()
        release_materialize = threading.Event()
        original_materialize = self.supervisor.event_store.materialize
        original_submit = self.supervisor.persistence_writer.submit
        other_submit_entered = asyncio.Event()

        def blocked_materialize(*args: object, **kwargs: object) -> object:
            materialize_started.set()
            release_materialize.wait(timeout=10)
            return original_materialize(*args, **kwargs)  # type: ignore[arg-type]

        async def observe_submit(
            run_id: str,
            function: object,
            *args: object,
            **kwargs: object,
        ) -> object:
            if run_id == other.run_id:
                other_submit_entered.set()
            return await original_submit(  # type: ignore[arg-type]
                run_id,
                function,
                *args,
                **kwargs,
            )

        self.supervisor.expected_stream_ends.update(
            {id(first_adapter), id(other_adapter)}
        )
        try:
            with (
                mock.patch.object(
                    self.supervisor.event_store,
                    "materialize",
                    side_effect=blocked_materialize,
                ),
                mock.patch.object(
                    self.supervisor.persistence_writer,
                    "submit",
                    side_effect=observe_submit,
                ),
            ):
                await first_adapter._events.put(  # noqa: SLF001
                    self._event("turn/started", {"turn": {"id": "first"}})
                )
                await other_adapter._events.put(  # noqa: SLF001
                    self._event("turn/started", {"turn": {"id": "other"}})
                )
                await first_adapter._events.put(None)  # noqa: SLF001
                await other_adapter._events.put(None)  # noqa: SLF001
                first_task = asyncio.create_task(
                    self.supervisor._pump_events(self.record.run_id, first_adapter)
                )
                await asyncio.to_thread(materialize_started.wait, 10)
                other_task = asyncio.create_task(
                    self.supervisor._pump_events(other.run_id, other_adapter)
                )
                await other_submit_entered.wait()
                self.assertEqual(self.store.get(other.run_id).raw_event_count, 0)
                self.assertEqual(
                    self.supervisor.materializer_metrics()["persistence"][
                        "queue_depth"
                    ],
                    1,
                )
                persistence = self.supervisor.materializer_metrics()["persistence"]
                self.assertEqual(persistence["running"], 1)
                self.assertEqual(persistence["queued"], 0)
                self.assertGreaterEqual(persistence["admission_waiting"], 1)
                release_materialize.set()
                await asyncio.wait_for(
                    asyncio.gather(first_task, other_task),
                    timeout=10,
                )
        finally:
            release_materialize.set()
            self.supervisor.expected_stream_ends.difference_update(
                {id(first_adapter), id(other_adapter)}
            )
        self.assertEqual(self.store.get(other.run_id).raw_event_count, 1)
        self.assertEqual(self.store.get(other.run_id).normalized_event_count, 1)
        self.assertEqual(
            self.supervisor.materializer_metrics()["persistence"]["queue_depth"],
            0,
        )

    async def test_shutdown_drains_accepted_writer_jobs(self) -> None:
        started = threading.Event()
        release = threading.Event()
        original_append_raw = self.store.append_raw

        def blocked_append_raw(*args: object, **kwargs: object) -> dict[str, object]:
            started.set()
            release.wait(timeout=10)
            return original_append_raw(*args, **kwargs)  # type: ignore[arg-type]

        close_entered = asyncio.Event()
        original_close = self.supervisor.persistence_writer.close

        async def observe_close() -> None:
            close_entered.set()
            await original_close()

        with (
            mock.patch.object(self.store, "append_raw", side_effect=blocked_append_raw),
            mock.patch.object(
                self.supervisor.persistence_writer,
                "close",
                side_effect=observe_close,
            ),
        ):
            write_task = asyncio.create_task(
                self.supervisor._append_raw_event_async(
                    self.record.run_id,
                    provider="codex",
                    direction="provider",
                    payload={"method": "fixture/write"},
                    generation=1,
                    received_at="2026-08-19T00:00:00Z",
                )
            )
            await asyncio.to_thread(started.wait, 10)
            close_task = asyncio.create_task(self.supervisor.close())
            await close_entered.wait()
            self.assertFalse(close_task.done())
            release.set()
            raw = await asyncio.wait_for(write_task, timeout=10)
            await asyncio.wait_for(close_task, timeout=10)

        self.assertEqual(raw["seq"], 1)
        self.assertEqual(len(list(self.store.iter_raw_events(self.record.run_id))), 1)

    async def test_shutdown_drains_queued_sqlite_work_before_close(self) -> None:
        other = self.store.create(
            RunRecord.new(
                agent_id="WIKI-358-SHUTDOWN-OTHER",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture",
                worktree=str(self.root),
                prompt="queued sqlite shutdown test",
            )
        )
        started = threading.Event()
        release = threading.Event()
        materialized: list[str] = []
        original_materialize = self.supervisor.event_store.materialize

        def blocked_materialize(*args: object, **kwargs: object) -> object:
            run_id = str(args[0])
            materialized.append(run_id)
            if not started.is_set():
                started.set()
                release.wait(timeout=10)
            return original_materialize(*args, **kwargs)  # type: ignore[arg-type]

        first_event = self._event("turn/started", {"turn": {"id": "first"}})
        second_event = ProviderEvent(
            ProviderKind.CODEX,
            {
                "method": "turn/started",
                "params": {"turn": {"id": "second"}},
            },
            generation=other.provider_generation,
        )
        patcher = mock.patch.object(
            self.supervisor.event_store,
            "materialize",
            side_effect=blocked_materialize,
        )
        patcher.start()
        try:
            first_task = asyncio.create_task(
                self.supervisor._persist_event_bundle_async(
                    self.record,
                    first_event,
                    normalize_provider_event(
                        first_event.provider,
                        first_event.payload,
                        direction=first_event.direction,
                    ),
                    first_event.payload["params"],
                )
            )
            await asyncio.to_thread(started.wait, 10)
            second_task = asyncio.create_task(
                self.supervisor._persist_event_bundle_async(
                    other,
                    second_event,
                    normalize_provider_event(
                        second_event.provider,
                        second_event.payload,
                        direction=second_event.direction,
                    ),
                    second_event.payload["params"],
                )
            )
            await asyncio.sleep(0)
            close_task = asyncio.create_task(self.supervisor.close())
            await asyncio.sleep(0)
            self.assertFalse(close_task.done())
            release.set()
            await asyncio.wait_for(
                asyncio.gather(first_task, second_task, close_task),
                timeout=10,
            )
        finally:
            release.set()
            patcher.stop()
        self.assertEqual(set(materialized), {self.record.run_id, other.run_id})


    async def test_writer_keeps_run_status_responsive_during_blocked_burst(
        self,
    ) -> None:
        records = [
            self.store.create(
                RunRecord.new(
                    agent_id=f"WIKI-358-STATUS-{index}",
                    provider=ProviderKind.CODEX,
                    role="implement",
                    model="fixture",
                    worktree=str(self.root),
                    prompt="store-read responsiveness test",
                )
            )
            for index in range(3)
        ]
        adapters = [self.factory(record) for record in records]
        materialize_started = threading.Event()
        release_materialize = threading.Event()
        original_materialize = self.supervisor.event_store.materialize

        def blocked_materialize(*args: object, **kwargs: object) -> object:
            materialize_started.set()
            release_materialize.wait(timeout=10)
            return original_materialize(*args, **kwargs)  # type: ignore[arg-type]

        self.supervisor.expected_stream_ends.update(id(adapter) for adapter in adapters)
        tasks: list[asyncio.Task[None]] = []
        try:
            with mock.patch.object(
                self.supervisor.event_store,
                "materialize",
                side_effect=blocked_materialize,
            ):
                for record, adapter in zip(records, adapters):
                    for index in range(40):
                        await adapter._events.put(  # noqa: SLF001
                            ProviderEvent(
                                ProviderKind.CODEX,
                                {
                                    "method": "turn/diff/updated",
                                    "params": {"diff": f"status-burst-{index}"},
                                },
                                generation=record.provider_generation,
                            )
                        )
                    await adapter._events.put(None)  # noqa: SLF001
                    tasks.append(
                        asyncio.create_task(
                            self.supervisor._pump_events(record.run_id, adapter)
                        )
                    )
                await asyncio.to_thread(materialize_started.wait, 10)
                # Store-touching dispatches must stay responsive while writer
                # threads persist: materialization holds no store lock, and the
                # JSONL appends the lock does cover are bounded.
                started = time.perf_counter()
                status = await asyncio.wait_for(
                    self.supervisor.dispatch(
                        "run/status", {"agent_id": records[0].agent_id}
                    ),
                    timeout=3,
                )
                self.assertLess(time.perf_counter() - started, 1)
                self.assertEqual(status["run_id"], records[0].run_id)
        finally:
            release_materialize.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.supervisor.expected_stream_ends.difference_update(
                id(adapter) for adapter in adapters
            )
