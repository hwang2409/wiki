from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.command_log import (
    AgentCommand,
    CommandConflict,
    CommandLog,
    CommandQueue,
    CommandRetryable,
    decide,
)
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.types import ProviderKind, RunRecord


async def _raise_runtime_error(message: str) -> None:
    raise RuntimeError(message)


async def _return_result(result: dict[str, str]) -> dict[str, str]:
    return result


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

    def test_receipt_and_effect_replay_bind_agent_and_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = CommandLog(Path(tmp) / "command-log.sqlite3")
            command = AgentCommand.spawn(
                agent_id="WIKI-A",
                request_id="spawn-bound",
                payload={"run_id": "run-a"},
            )
            log.append_intent(command, {})
            log.complete(
                command,
                {"run_id": "run-a"},
                {"WIKI-A": {"current": {"run_id": "run-a"}}},
            )
            with self.assertRaises(CommandConflict):
                log.append_intent(
                    AgentCommand.spawn(
                        agent_id="WIKI-B",
                        request_id="spawn-bound",
                        payload={"run_id": "run-b"},
                    ),
                    {},
                )
            with self.assertRaises(CommandConflict):
                log.append_intent(
                    AgentCommand.spawn(
                        agent_id="WIKI-A",
                        request_id="spawn-bound",
                        payload={"run_id": "run-other"},
                    ),
                    {},
                )

            effect_command = AgentCommand(
                "run/archive",
                "WIKI-A",
                "archive-bound",
                {"run_id": "run-a"},
            )
            log.complete_effect(effect_command, {"run_id": "run-a"})
            with self.assertRaises(CommandConflict):
                log.effect_result(
                    "run/archive",
                    "archive-bound",
                    agent_id="WIKI-B",
                    command_hash=effect_command.command_hash,
                )
            with self.assertRaises(CommandConflict):
                log.effect_result(
                    "run/archive",
                    "archive-bound",
                    agent_id="WIKI-A",
                    command_hash=AgentCommand(
                        "run/archive",
                        "WIKI-A",
                        "archive-bound",
                        {"run_id": "run-other"},
                    ).command_hash,
                )

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

    def test_queue_preserves_global_order_across_agents(self) -> None:
        async def run() -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                state = {
                    "WIKI-A": {"current": {"run_id": "run-a"}},
                    "WIKI-B": {"current": {"run_id": "run-b"}},
                }
                queue = CommandQueue(log, lambda: state)
                order: list[str] = []

                async def effect(name: str) -> dict[str, str]:
                    order.append(f"start:{name}")
                    await asyncio.sleep(0)
                    order.append(f"end:{name}")
                    return {"name": name}

                first = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="steer-a",
                    payload={"method": "run/send_now", "run_id": "run-a"},
                )
                second = AgentCommand.steer(
                    agent_id="WIKI-B",
                    request_id="steer-b",
                    payload={"method": "run/send_now", "run_id": "run-b"},
                )
                await asyncio.gather(
                    queue.submit(first, lambda: effect("a")),
                    queue.submit(second, lambda: effect("b")),
                )
                await queue.close()
                return order

        self.assertEqual(
            asyncio.run(run()),
            ["start:a", "end:a", "start:b", "end:b"],
        )

    def test_inflight_conflict_checks_agent_and_payload(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                state = {
                    "WIKI-A": {"current": {"run_id": "run-a"}},
                    "WIKI-B": {"current": {"run_id": "run-b"}},
                }
                queue = CommandQueue(log, lambda: state)
                started = asyncio.Event()
                release = asyncio.Event()

                async def effect() -> dict[str, str]:
                    started.set()
                    await release.wait()
                    return {"status": "sent"}

                first = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="same-request",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-a",
                        "text": "one",
                    },
                )
                conflicting = AgentCommand.steer(
                    agent_id="WIKI-B",
                    request_id="same-request",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-b",
                        "text": "two",
                    },
                )
                first_task = asyncio.create_task(queue.submit(first, effect))
                await started.wait()
                with self.assertRaises(CommandConflict):
                    await queue.submit(conflicting, effect)
                release.set()
                await first_task
                await queue.close()

        asyncio.run(run())

    def test_pending_intents_replay_in_durable_order(self) -> None:
        async def run() -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                first = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="spawn-a",
                    payload={"run_id": "run-a"},
                )
                second = AgentCommand.spawn(
                    agent_id="WIKI-B",
                    request_id="spawn-b",
                    payload={"run_id": "run-b"},
                )
                log.append_intent(first, {})
                log.append_intent(second, {})
                queue_order: list[str] = []

                def factory(command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        queue_order.append(command.request_id)
                        return {"run_id": str(command.payload["run_id"])}

                    return effect

                queue = CommandQueue(log, lambda: {}, recovery_factory=factory)
                await queue.recover_pending()
                await queue.close()
                assert log.pending() == []
                return queue_order

        self.assertEqual(asyncio.run(run()), ["spawn-a", "spawn-b"])

    def test_failed_recovery_receipt_does_not_block_healthy_recovery(self) -> None:
        async def run() -> tuple[list[str], list[tuple[str, str]]]:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                failed = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="spawn-failed",
                    payload={"run_id": "run-failed"},
                )
                healthy = AgentCommand.spawn(
                    agent_id="WIKI-B",
                    request_id="spawn-healthy",
                    payload={"run_id": "run-healthy"},
                )
                log.append_intent(failed, {})
                log.append_intent(healthy, {})
                completed: list[str] = []

                def factory(command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        if command.request_id == failed.request_id:
                            raise RuntimeError("provider failed during recovery")
                        completed.append(command.request_id)
                        return {"run_id": str(command.payload["run_id"])}

                    return effect

                queue = CommandQueue(log, lambda: {}, recovery_factory=factory)
                await queue.recover_pending()
                await queue.close()
                receipt = log.receipt("run/start", failed.request_id)
                assert receipt is not None
                return completed, [
                    (item[0].request_id, str(item[1]))
                    for item in queue.recovery_failures
                ]

        completed, failures = asyncio.run(run())
        self.assertEqual(completed, ["spawn-healthy"])
        self.assertEqual(failures, [("spawn-failed", "provider failed during recovery")])

    def test_unexpected_recovery_failure_keeps_same_agent_barrier_closed(self) -> None:
        async def run() -> None:
            class UnexpectedRecoveryFailure(BaseException):
                pass

            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                pending = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="spawn-unexpected",
                    payload={"run_id": "run-a"},
                )
                log.append_intent(pending, {})
                attempts = 0

                def factory(command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        nonlocal attempts
                        attempts += 1
                        if attempts == 1:
                            raise UnexpectedRecoveryFailure("recovery interrupted")
                        return {"run_id": str(command.payload["run_id"])}

                    return effect

                queue = CommandQueue(log, lambda: {}, recovery_factory=factory)
                with self.assertRaises(UnexpectedRecoveryFailure):
                    await queue.recover_pending()
                self.assertEqual([item.request_id for item in log.pending()], [pending.request_id])

                next_start = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="spawn-next",
                    payload={"run_id": "run-next"},
                )
                blocked = asyncio.create_task(
                    queue.submit(next_start, lambda: _return_result({"status": "started"}))
                )
                await asyncio.sleep(0)
                self.assertFalse(blocked.done())
                blocked.cancel()
                await asyncio.gather(blocked, return_exceptions=True)

                await queue.recover_pending()
                self.assertEqual(
                    await queue.submit(
                        next_start,
                        lambda: _return_result({"status": "started"}),
                    ),
                    {"status": "started"},
                )
                await queue.close()

        asyncio.run(run())

    def test_recovered_barriers_open_per_agent_before_other_recovery_finishes(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                first = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="spawn-a-old",
                    payload={"run_id": "run-a-old"},
                )
                second = AgentCommand.spawn(
                    agent_id="WIKI-B",
                    request_id="spawn-b-old",
                    payload={"run_id": "run-b-old"},
                )
                log.append_intent(first, {})
                log.append_intent(second, {})
                first_done = asyncio.Event()
                second_started = asyncio.Event()
                release_second = asyncio.Event()

                def factory(command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        if command.agent_id == "WIKI-A":
                            first_done.set()
                        else:
                            second_started.set()
                            await release_second.wait()
                        return {"run_id": str(command.payload["run_id"])}

                    return effect

                queue = CommandQueue(log, lambda: {}, recovery_factory=factory)
                recovery = asyncio.create_task(queue.recover_pending())
                await second_started.wait()
                await first_done.wait()

                next_start = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="spawn-a-new",
                    payload={"run_id": "run-a-new"},
                )
                self.assertEqual(
                    await queue.submit(
                        next_start,
                        lambda: _return_result({"status": "started"}),
                    ),
                    {"status": "started"},
                )
                self.assertFalse(recovery.done())
                release_second.set()
                await recovery
                await queue.close()

        asyncio.run(run())

    def test_deferred_admission_failure_restores_intent_and_barrier(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                deferred = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="spawn-deferred",
                    payload={"run_id": "run-deferred"},
                )
                log.append_intent(deferred, {})
                attempts = 0

                def factory(command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        nonlocal attempts
                        attempts += 1
                        if attempts == 1:
                            raise CommandRetryable("provider control is unavailable")
                        return {"run_id": str(command.payload["run_id"])}

                    return effect

                queue = CommandQueue(log, lambda: {}, recovery_factory=factory)
                await queue.recover_pending()
                self.assertIn(
                    deferred.request_id,
                    queue.recovery_queue(deferred.agent_id),
                )

                admission_failure = RuntimeError("command log is unavailable")
                replacement = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id=deferred.request_id,
                    payload={"run_id": "run-deferred"},
                )
                with mock.patch.object(
                    log,
                    "append_intent",
                    side_effect=admission_failure,
                ):
                    with self.assertRaisesRegex(RuntimeError, "command log is unavailable"):
                        await queue.submit(
                            replacement,
                            lambda: _return_result({"status": "started"}),
                        )
                self.assertEqual(
                    [item.request_id for item in log.pending()],
                    [deferred.request_id],
                )
                self.assertFalse(queue.recovery_barrier_open("WIKI-A"))

                next_start = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="spawn-after-deferred",
                    payload={"run_id": "run-next"},
                )
                blocked = asyncio.create_task(
                    queue.submit(next_start, lambda: _return_result({"status": "started"}))
                )
                await asyncio.sleep(0)
                self.assertFalse(blocked.done())

                await queue.recover_pending()
                self.assertEqual(
                    await blocked,
                    {"status": "started"},
                )
                await queue.close()

        asyncio.run(run())

    def test_deferred_retries_keep_same_agent_order(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                first = AgentCommand(
                    "run/send_now",
                    agent_id="WIKI-A",
                    request_id="send-a-old",
                    payload={"run_id": "run-a-old"},
                )
                second = AgentCommand(
                    "run/send_now",
                    agent_id="WIKI-A",
                    request_id="send-a-next",
                    payload={"run_id": "run-a-old"},
                )
                effects: list[str] = []
                first_started = asyncio.Event()
                release_first = asyncio.Event()
                second_started = asyncio.Event()

                def factory(command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        if command.request_id == first.request_id:
                            first_started.set()
                            await release_first.wait()
                            effects.append("a")
                        else:
                            second_started.set()
                            effects.append("b")
                        return {"run_id": str(command.payload["run_id"])}

                    return effect

                queue = CommandQueue(log, lambda: {}, recovery_factory=factory)
                queue.defer_for_recovery(first)
                queue.defer_for_recovery(second)
                recovery = asyncio.create_task(queue.recover_pending())
                await asyncio.wait_for(first_started.wait(), timeout=2)
                self.assertFalse(second_started.is_set())
                release_first.set()
                await recovery
                await asyncio.wait_for(second_started.wait(), timeout=2)
                self.assertEqual(effects, ["a", "b"])
                await queue.close()

        asyncio.run(run())

    def test_startup_retry_promotes_only_the_next_recovery_head(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                first = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="send-a-old",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-a",
                        "text": "first",
                    },
                )
                second = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="send-a-next",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-a",
                        "text": "second",
                    },
                )
                state = {"WIKI-A": {"current": {"run_id": "run-a"}}}
                log.append_intent(first, state)
                log.append_intent(second, state)
                attempts = 0
                effects: list[str] = []
                second_started = asyncio.Event()
                release_second = asyncio.Event()

                def factory(command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        nonlocal attempts
                        if command.request_id == first.request_id:
                            attempts += 1
                            if attempts == 1:
                                raise CommandRetryable("provider is detached")
                        else:
                            second_started.set()
                            await release_second.wait()
                        effects.append(command.request_id)
                        return {"status": "sent"}

                    return effect

                queue = CommandQueue(log, lambda: {}, recovery_factory=factory)
                await queue.recover_pending()
                self.assertEqual(effects, [])
                self.assertEqual(
                    queue.recovery_queue("WIKI-A"),
                    (first.request_id, second.request_id),
                )
                self.assertIsNone(queue.recovery_admitted("WIKI-A"))

                recovery = asyncio.create_task(queue.recover_pending())
                await second_started.wait()
                self.assertEqual(effects, [first.request_id])
                self.assertFalse(recovery.done())
                self.assertEqual(queue.recovery_admitted("WIKI-A"), second.request_id)
                self.assertEqual(
                    queue.recovery_queue("WIKI-A"),
                    (second.request_id,),
                )

                release_second.set()
                await recovery
                self.assertEqual(effects, [first.request_id, second.request_id])
                self.assertEqual(log.pending(), [])
                await queue.close()

        asyncio.run(run())

    def test_deferred_start_blocks_same_agent_until_recovery_finishes(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                old = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="send-a-old",
                    payload={"run_id": "run-a-old"},
                )
                log.append_intent(old, {})
                old_started = asyncio.Event()
                release_old = asyncio.Event()

                async def recover_old() -> dict[str, str]:
                    old_started.set()
                    await release_old.wait()
                    return {"run_id": "run-a-old"}

                queue = CommandQueue(
                    log,
                    lambda: {},
                    recovery_factory=lambda _command: recover_old,
                )
                new = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="send-a-new",
                    payload={"run_id": "run-a-new"},
                )
                blocked = asyncio.create_task(
                    queue.submit(
                        new,
                        lambda: _return_result({"run_id": "run-a-new"}),
                    )
                )
                await asyncio.sleep(0)
                self.assertFalse(blocked.done())
                recovery = asyncio.create_task(queue.recover_pending())
                await old_started.wait()
                self.assertFalse(blocked.done())
                release_old.set()
                await recovery
                self.assertEqual(
                    await asyncio.wait_for(blocked, timeout=5),
                    {"run_id": "run-a-new"},
                )
                await queue.close()

        asyncio.run(run())

    def test_barrier_uses_method_and_request_id_composite_key(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                first = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="shared-request",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-a",
                        "text": "first",
                    },
                )
                second = AgentCommand.archive(
                    agent_id="WIKI-A",
                    request_id="shared-request",
                    payload={"run_id": "run-a"},
                )
                first_started = asyncio.Event()
                release_first = asyncio.Event()

                async def recover_first() -> dict[str, str]:
                    first_started.set()
                    await release_first.wait()
                    return {"status": "sent"}

                async def recover_second() -> dict[str, str]:
                    return {"status": "archived"}

                def factory(command: AgentCommand):
                    return recover_first if command is first else recover_second

                queue = CommandQueue(log, lambda: {}, recovery_factory=factory)
                queue.defer_for_recovery(first)
                queue.defer_for_recovery(second)
                recovery = asyncio.create_task(queue.recover_pending())
                await first_started.wait()

                blocked = asyncio.create_task(
                    queue.submit(
                        second,
                        lambda: _return_result({"status": "wrong-path"}),
                    )
                )
                await asyncio.sleep(0)
                self.assertFalse(blocked.done())
                release_first.set()
                await recovery
                self.assertEqual(await blocked, {"status": "archived"})
                await queue.close()

        asyncio.run(run())

    def test_receipt_error_keeps_recovered_head_and_barrier_closed(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                old = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="receipt-error-old",
                    payload={"run_id": "run-a"},
                )
                log.append_intent(old, {})

                async def recover() -> dict[str, str]:
                    return {"status": "started"}

                queue = CommandQueue(
                    log,
                    lambda: {},
                    recovery_factory=lambda _command: recover,
                )

                with mock.patch.object(
                    log,
                    "complete",
                    side_effect=RuntimeError("receipt write failed"),
                ):
                    await queue.recover_pending()

                self.assertEqual(
                    [item.request_id for item in log.pending()],
                    [old.request_id],
                )
                self.assertEqual(
                    queue.recovery_queue(old.agent_id),
                    (old.request_id,),
                )
                self.assertIsNone(queue.recovery_admitted(old.agent_id))
                self.assertFalse(queue.recovery_barrier_open(old.agent_id))

                newer = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="receipt-error-new",
                    payload={"run_id": "run-a-new"},
                )
                blocked = asyncio.create_task(
                    queue.submit(
                        newer,
                        lambda: _return_result({"status": "started"}),
                    )
                )
                await asyncio.sleep(0)
                self.assertFalse(blocked.done())
                blocked.cancel()
                await asyncio.gather(blocked, return_exceptions=True)
                await queue.close()

        asyncio.run(run())

    def test_promotion_factory_error_rolls_back_successor_admission(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                first = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="promotion-first",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-first",
                        "text": "first",
                    },
                )
                second = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="promotion-second",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-first",
                        "text": "second",
                    },
                )
                state = {"WIKI-A": {"current": {"run_id": "run-first"}}}
                log.append_intent(first, state)
                log.append_intent(second, state)
                second_attempts = 0

                def factory(command: AgentCommand):
                    nonlocal second_attempts
                    if command.request_id == second.request_id:
                        second_attempts += 1
                        if second_attempts == 1:
                            raise RuntimeError("factory failed during promotion")

                    async def recover() -> dict[str, str]:
                        return {"status": "sent"}

                    return recover

                queue = CommandQueue(log, lambda: state, recovery_factory=factory)
                await queue.recover_pending()
                self.assertEqual(
                    queue.recovery_queue(first.agent_id),
                    (second.request_id,),
                )
                self.assertIsNone(queue.recovery_admitted(first.agent_id))
                self.assertFalse(queue.recovery_barrier_open(first.agent_id))
                self.assertEqual(queue._recovery_queue.qsize(), 0)  # noqa: SLF001

                await queue.recover_pending()
                self.assertEqual(log.pending(), [])
                self.assertTrue(queue.recovery_barrier_open(first.agent_id))
                await queue.close()

        asyncio.run(run())

    def test_terminal_recovery_paths_open_empty_agent_barrier(self) -> None:
        async def run() -> None:
            for outcome in ("failure", "success", "timeout"):
                with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                    log = CommandLog(Path(tmp) / "command-log.sqlite3")
                    old = AgentCommand.steer(
                        agent_id="WIKI-A",
                        request_id=f"send-{outcome}",
                        payload={
                            "method": "run/send_now",
                            "run_id": "run-a",
                            "text": outcome,
                        },
                    )
                    state = {"WIKI-A": {"current": {"run_id": "run-a"}}}
                    log.append_intent(old, state)

                    async def recover_old() -> dict[str, str]:
                        if outcome == "failure":
                            raise RuntimeError("recovery failed")
                        if outcome == "timeout":
                            raise asyncio.TimeoutError("recovery timed out")
                        return {"status": "sent"}

                    queue = CommandQueue(
                        log,
                        lambda: state,
                        recovery_factory=lambda _command: recover_old,
                    )
                    await queue.recover_pending()
                    self.assertTrue(queue.recovery_barrier_open("WIKI-A"))
                    new = AgentCommand.steer(
                        agent_id="WIKI-A",
                        request_id=f"next-{outcome}",
                        payload={
                            "method": "run/send_now",
                            "run_id": "run-a",
                            "text": "next",
                        },
                    )
                    self.assertEqual(
                        await queue.submit(
                            new,
                            lambda: _return_result({"status": "sent"}),
                        ),
                        {"status": "sent"},
                    )
                    await queue.close()

        asyncio.run(run())

    def test_failed_start_restores_authoritative_projection_for_corrected_command(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                queue = CommandQueue(
                    log,
                    lambda: log.projection(),
                    agent_state_provider=log.projection_for,
                    failure_state_provider=lambda agent_id: {agent_id: None},
                )
                failed = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="start-failed",
                    payload={"run_id": "run-failed"},
                )
                with self.assertRaisesRegex(RuntimeError, "before commit"):
                    await queue.submit(
                        failed,
                        lambda: _raise_runtime_error("provider failed before commit"),
                    )
                self.assertEqual(log.projection_for("WIKI-A"), {})

                corrected = AgentCommand.spawn(
                    agent_id="WIKI-A",
                    request_id="start-corrected",
                    payload={"run_id": "run-corrected"},
                )
                result = await queue.submit(
                    corrected,
                    lambda: _return_result({"run_id": "run-corrected"}),
                )
                self.assertEqual(result, {"run_id": "run-corrected"})
                self.assertEqual(
                    log.projection_for("WIKI-A")["WIKI-A"]["current"]["run_id"],
                    "run-corrected",
                )
                await queue.close()

        asyncio.run(run())

    def test_failed_replace_restores_authoritative_projection_for_corrected_command(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                old = {
                    "WIKI-A": {
                        "current": {"run_id": "run-old", "state": "working"}
                    }
                }
                log.seed_projection("WIKI-A", old)
                queue = CommandQueue(
                    log,
                    lambda: log.projection(),
                    agent_state_provider=log.projection_for,
                    failure_state_provider=lambda _agent_id: old,
                )
                failed = AgentCommand.replace(
                    agent_id="WIKI-A",
                    request_id="replace-failed",
                    payload={
                        "run_id": "run-old",
                        "replacement_run_id": "run-failed",
                    },
                )
                with self.assertRaisesRegex(RuntimeError, "before commit"):
                    await queue.submit(
                        failed,
                        lambda: _raise_runtime_error("provider failed before commit"),
                    )
                self.assertEqual(log.projection_for("WIKI-A"), old)

                corrected = AgentCommand.replace(
                    agent_id="WIKI-A",
                    request_id="replace-corrected",
                    payload={
                        "run_id": "run-old",
                        "replacement_run_id": "run-corrected",
                    },
                )
                result = await queue.submit(
                    corrected,
                    lambda: _return_result({"run_id": "run-corrected"}),
                )
                self.assertEqual(result, {"run_id": "run-corrected"})
                self.assertEqual(
                    log.projection_for("WIKI-A")["WIKI-A"]["current"]["run_id"],
                    "run-corrected",
                )
                await queue.close()

        asyncio.run(run())

    def test_retryable_recovery_retains_intent_until_provider_control_returns(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                command = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="steer-retryable",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-a",
                        "text": "deliver once",
                    },
                )
                log.append_intent(
                    command,
                    {"WIKI-A": {"current": {"run_id": "run-a"}}},
                )
                available = False
                calls = 0

                def factory(_command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        nonlocal calls
                        calls += 1
                        if not available:
                            raise CommandRetryable("provider control is detached")
                        return {"status": "sent"}

                    return effect

                queue = CommandQueue(
                    log,
                    lambda: {},
                    recovery_factory=factory,
                )
                await queue.recover_pending()
                self.assertEqual(log.pending(), [command])
                self.assertIsNone(log.receipt(command.method, command.request_id))

                available = True
                await queue.recover_pending()
                self.assertEqual(log.pending(), [])
                receipt = log.receipt(command.method, command.request_id)
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt.result if receipt else None, {"status": "sent"})
                self.assertEqual(calls, 2)
                await queue.close()

        asyncio.run(run())

    def test_repeat_retryable_submission_is_recovered_without_restart(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                log = CommandLog(Path(tmp) / "command-log.sqlite3")
                command = AgentCommand.steer(
                    agent_id="WIKI-A",
                    request_id="steer-repeat-retryable",
                    payload={
                        "method": "run/send_now",
                        "run_id": "run-a",
                        "text": "deliver once",
                    },
                )
                log.append_intent(
                    command,
                    {"WIKI-A": {"current": {"run_id": "run-a"}}},
                )
                available = False
                deliveries = 0

                def factory(_command: AgentCommand):
                    async def effect() -> dict[str, str]:
                        nonlocal deliveries
                        if not available:
                            raise CommandRetryable("provider control is detached")
                        deliveries += 1
                        return {"status": "sent"}

                    return effect

                queue = CommandQueue(
                    log,
                    lambda: {},
                    recovery_factory=factory,
                )
                await queue.recover_pending()

                with self.assertRaises(CommandRetryable):
                    await queue.submit(command, factory(command))

                available = True
                await queue.recover_pending()

                self.assertEqual(deliveries, 1)
                self.assertEqual(log.pending(), [])
                receipt = log.receipt(command.method, command.request_id)
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt.result if receipt else None, {"status": "sent"})
                await queue.close()

        asyncio.run(run())

    def test_steer_outbox_reconciles_delivery_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = CommandLog(Path(tmp) / "command-log.sqlite3")
            row = log.steer_effect(
                method="run/send_now",
                request_id="steer-1",
                agent_id="WIKI-A",
                command_hash="hash-1",
                run_id="run-a",
                pending_id="pending-1",
                message="hello",
                mode="now",
            )
            self.assertEqual(row["status"], "queued")
            log.mark_steer_sending_for_pending("run-a", "pending-1")
            log.mark_steer_sent_for_pending("run-a", "pending-1")
            log.update_steer_effect(
                "run/send_now", "steer-1", "sent", {"status": "sent"}
            )
            replay = log.steer_effect(
                method="run/send_now",
                request_id="steer-1",
                agent_id="WIKI-A",
                command_hash="hash-1",
                run_id="run-a",
                pending_id="pending-1",
                message="hello",
                mode="now",
            )
            self.assertEqual(replay["status"], "sent")
            self.assertEqual(replay["result"], {"status": "sent"})
            log.acknowledge_steer_for_pending("run-a", "pending-1")
            self.assertEqual(
                log.steer_effect(
                    method="run/send_now",
                    request_id="steer-1",
                    agent_id="WIKI-A",
                    command_hash="hash-1",
                    run_id="run-a",
                    pending_id="pending-1",
                    message="hello",
                    mode="now",
                )["status"],
                "acknowledged",
            )
            with self.assertRaises(CommandConflict):
                log.steer_effect(
                    method="run/send_now",
                    request_id="steer-1",
                    agent_id="WIKI-B",
                    command_hash="hash-1",
                    run_id="run-b",
                    pending_id="pending-2",
                    message="other",
                    mode="now",
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
