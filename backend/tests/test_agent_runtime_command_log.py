from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from backend.app.agent_runtime.command_log import (
    AgentCommand,
    CommandConflict,
    CommandLog,
    CommandQueue,
    decide,
)
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.types import ProviderKind, RunRecord


class CommandLogTests(unittest.TestCase):
    def test_decider_is_pure_and_rejects_duplicate_spawn(self) -> None:
        state: dict[str, object] = {}
        command = AgentCommand.spawn(
            agent_id="WIKI-219",
            request_id="spawn-1",
            payload={"run_id": "run-1"},
        )

        events = decide(command, state)

        self.assertEqual(events[0].event_type, "run/start_requested")
        self.assertEqual(state, {})
        with self.assertRaises(CommandConflict):
            decide(command, {"WIKI-219": {"current": {"run_id": "run-1"}}})

    def test_receipt_replay_has_one_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = CommandLog(Path(tmp) / "command-log.sqlite3")
            command = AgentCommand.spawn(
                agent_id="WIKI-219",
                request_id="spawn-1",
                payload={"run_id": "run-1"},
            )
            log.append_intent(command, {})
            log.complete(
                command,
                {"run_id": "run-1"},
                {"WIKI-219": {"current": {"run_id": "run-1"}}},
            )

            replay = log.append_intent(command, {})

            self.assertTrue(replay.replay)
            self.assertEqual(replay.result, {"run_id": "run-1"})
            self.assertEqual(len(log.events(method="run/start")), 2)
            self.assertEqual(len(log.pending()), 0)

    def test_queue_totally_orders_provider_effects(self) -> None:
        async def run() -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                state = {"WIKI-219": {"current": {"run_id": "run-1"}}}
                queue = CommandQueue(log, lambda: state)
                order: list[str] = []

                async def effect(name: str) -> dict[str, str]:
                    order.append(f"start:{name}")
                    await asyncio.sleep(0)
                    order.append(f"end:{name}")
                    return {"name": name}

                first = AgentCommand.steer(
                    agent_id="WIKI-219",
                    request_id="steer-1",
                    payload={"method": "run/send_now", "run_id": "run-1"},
                )
                second = AgentCommand.steer(
                    agent_id="WIKI-219",
                    request_id="steer-2",
                    payload={"method": "run/send_now", "run_id": "run-1"},
                )
                results = await asyncio.gather(
                    queue.submit(first, lambda: effect("one")),
                    queue.submit(second, lambda: effect("two")),
                )
                await queue.close()
                self.assertEqual(results, [{"name": "one"}, {"name": "two"}])
                return order

        self.assertEqual(
            asyncio.run(run()),
            ["start:one", "end:one", "start:two", "end:two"],
        )

    def test_restart_aborts_uncommitted_start_and_restores_preimage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths(
                runtime_dir=root / "runtime",
                socket_path=root / "runtime" / "supervisor.sock",
                registry_path=root / "registry.json",
                archive_dir=root / "archive",
                status_dir=root / "status",
            )
            paths.status_dir.mkdir(parents=True)
            status_path = paths.status_dir / "WIKI-219.json"
            status_path.write_text('{"state":"working"}\n', encoding="utf-8")
            paths.registry_path.write_text(
                json.dumps({"_orchestrators": {"WIKI-219": {"kind": "cc"}}}),
                encoding="utf-8",
            )
            store = RunStore(paths)
            record = RunRecord.new(
                agent_id="WIKI-219",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                worktree=str(root),
                prompt="Implement WIKI-219",
                start_request_id="spawn-1",
            )
            store.create(record, migrate_legacy=True, transactional_start=True)
            self.assertTrue(record.start_transaction)

            restarted = RunStore(paths)

            self.assertEqual(restarted.list_runs(), [])
            self.assertEqual(
                json.loads(paths.registry_path.read_text(encoding="utf-8")),
                {"_orchestrators": {"WIKI-219": {"kind": "cc"}}},
            )
            self.assertEqual(
                status_path.read_text(encoding="utf-8"), '{"state":"working"}\n'
            )

    def test_start_request_is_durable_in_run_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths(
                runtime_dir=root / "runtime",
                socket_path=root / "runtime" / "supervisor.sock",
                registry_path=root / "registry.json",
                archive_dir=root / "archive",
                status_dir=root / "status",
            )
            store = RunStore(paths)
            record = RunRecord.new(
                agent_id="WIKI-219",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                worktree=str(root),
                prompt="Implement WIKI-219",
                start_request_id="spawn-1",
            )
            store.create(record)

            restarted = RunStore(paths)

            found = restarted.find_start_request("spawn-1")
            self.assertIsNotNone(found)
            self.assertEqual(found.run_id if found else None, record.run_id)


if __name__ == "__main__":
    unittest.main()
