"""Typed FIFO execution for durable supervisor command intents."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .command_models import (
    AgentCommand,
    CommandConflict,
    CommandError,
    CommandRetryable,
)
from .command_interfaces import CommandExecutor, CommandPersistence


AgentStateProvider = Callable[[str], Mapping[str, Any]]
RecoveryExecutorFactory = Callable[[AgentCommand], CommandExecutor]


@dataclass
class _QueuedCommand:
    command: AgentCommand
    execute: CommandExecutor
    future: asyncio.Future[Any]
    recovering: bool = False


class _RecoveredCommandFailure(Exception):
    """An ordinary recovered command failure already stored as a receipt."""

    def __init__(self, error: Exception):
        super().__init__(str(error))
        self.error = error


class _RecoveredCommandRetry(Exception):
    """A recovered command stayed pending until provider control returns."""

    def __init__(self, error: CommandRetryable):
        super().__init__(str(error))
        self.error = error


class PerAgentQueueOwner:
    """Own one agent's deferred queue and every admission transition."""

    def __init__(self) -> None:
        self._commands: list[AgentCommand] = []
        self._admitted: tuple[AgentCommand, asyncio.Future[Any]] | None = None
        self._inflight: dict[
            tuple[str, str], tuple[AgentCommand, asyncio.Future[Any]]
        ] = {}
        self._promoted_futures: list[asyncio.Future[Any]] = []
        self._barrier = asyncio.Event()
        self._barrier.set()

    @staticmethod
    def _key(command: AgentCommand) -> tuple[str, str]:
        return command.method, command.request_id

    def find(self, key: tuple[str, str]) -> AgentCommand | None:
        return next(
            (command for command in self._commands if self._key(command) == key),
            None,
        )

    def inflight(self, key: tuple[str, str]) -> tuple[AgentCommand, asyncio.Future[Any]] | None:
        return self._inflight.get(key)

    def defer(self, command: AgentCommand) -> None:
        if self.find(self._key(command)) is not None:
            return
        if not self._commands:
            self._barrier.clear()
        self._commands.append(command)

    def admit_head(self) -> tuple[AgentCommand, asyncio.Future[Any]] | None:
        if not self._commands or self._admitted is not None:
            return None
        future = asyncio.get_running_loop().create_future()
        command = self._commands[0]
        self._admitted = (command, future)
        self._inflight[self._key(command)] = (command, future)
        self._barrier.clear()
        return command, future

    def promote_next(
        self,
        recovery_factory: RecoveryExecutorFactory,
        enqueue: Callable[[_QueuedCommand], None],
    ) -> tuple[AgentCommand, asyncio.Future[Any]] | None:
        admission = self.admit_head()
        if admission is None:
            return None
        command, future = admission
        try:
            execute = recovery_factory(command)
            enqueue(_QueuedCommand(command, execute, future, recovering=True))
        except BaseException as error:
            self.rollback_promotion(error)
            raise
        self._promoted_futures.append(future)
        return admission

    def rollback_promotion(self, reason: BaseException) -> None:
        del reason
        admission = self._admitted
        if admission is None:
            return
        command, future = admission
        self._admitted = None
        self._inflight.pop(self._key(command), None)
        if not future.done():
            future.cancel()
        self._barrier.clear()

    def finalize_terminal(self, command: AgentCommand, outcome: str) -> bool:
        """Finalize one admitted head and report whether a successor remains."""

        key = self._key(command)
        if not self._commands or self._key(self._commands[0]) != key:
            return False
        try:
            if outcome in {"retry", "receipt_error"}:
                return False
            self._commands.pop(0)
            return bool(self._commands)
        finally:
            if self._admitted is not None and self._key(self._admitted[0]) == key:
                self._admitted = None
            self._inflight.pop(key, None)
            if self._commands:
                self._barrier.clear()
            else:
                self._barrier.set()

    def release_for_retry(self, command: AgentCommand) -> None:
        key = self._key(command)
        if self._admitted is not None and self._key(self._admitted[0]) == key:
            self._admitted = None
        self._inflight.pop(key, None)
        self._barrier.clear()

    def register_live(
        self, command: AgentCommand, future: asyncio.Future[Any]
    ) -> None:
        self._inflight[self._key(command)] = (command, future)

    def finish_live(self, command: AgentCommand, future: asyncio.Future[Any]) -> None:
        key = self._key(command)
        if self._inflight.get(key, (None, None))[1] is future:
            self._inflight.pop(key, None)

    async def wait_for_turn(self, key: tuple[str, str]) -> None:
        if self._commands and self._key(self._commands[0]) != key:
            await self._barrier.wait()

    def barrier_open(self) -> bool:
        return self._barrier.is_set()

    def queued_request_ids(self) -> tuple[str, ...]:
        return tuple(command.request_id for command in self._commands)

    def head(self) -> AgentCommand | None:
        return self._commands[0] if self._commands else None

    def admitted_request_id(self) -> str | None:
        return self._admitted[0].request_id if self._admitted is not None else None

    def admitted_future(self) -> asyncio.Future[Any] | None:
        return self._admitted[1] if self._admitted is not None else None

    def recovery_idle(self) -> bool:
        return not self._commands and self._admitted is None

    def take_promoted_futures(self) -> list[asyncio.Future[Any]]:
        futures = self._promoted_futures
        self._promoted_futures = []
        return futures


class CommandQueue:
    """Run durable provider effects in one global FIFO reactor."""

    def __init__(
        self,
        log: CommandPersistence,
        state_provider: Callable[[], Mapping[str, Any]],
        agent_state_provider: AgentStateProvider | None = None,
        recovery_factory: RecoveryExecutorFactory | None = None,
        failure_state_provider: AgentStateProvider | None = None,
    ):
        self.log = log
        self.state_provider = state_provider
        self.agent_state_provider = agent_state_provider
        self.recovery_factory = recovery_factory
        self.failure_state_provider = failure_state_provider
        self._queue: asyncio.Queue[_QueuedCommand] = asyncio.Queue()
        self._recovery_queue: asyncio.Queue[_QueuedCommand] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._recovery_worker: asyncio.Task[None] | None = None
        self._append_lock = asyncio.Lock()
        self._commit_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._pending_loaded = False
        self._agent_recovery: dict[str, PerAgentQueueOwner] = {}
        self.recovery_failures: list[tuple[AgentCommand, Exception]] = []
        self.recovery_retries: list[tuple[AgentCommand, CommandRetryable]] = []
        self._closed = False

    def _start_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(), name="global-command-reactor"
            )

    def _start_recovery_worker(self) -> None:
        if self._recovery_worker is None or self._recovery_worker.done():
            self._recovery_worker = asyncio.create_task(
                self._run(recovery=True), name="recovered-command-reactor"
            )

    @staticmethod
    def _same_binding(left: AgentCommand, right: AgentCommand) -> bool:
        return left.agent_id == right.agent_id and left.command_hash == right.command_hash

    def _agent_state(self, agent_id: str) -> PerAgentQueueOwner:
        return self._agent_recovery.setdefault(agent_id, PerAgentQueueOwner())

    def defer_for_recovery(self, command: AgentCommand) -> None:
        self._agent_state(command.agent_id).defer(command)

    def recovery_queue(self, agent_id: str) -> tuple[str, ...]:
        return self._agent_state(agent_id).queued_request_ids()

    def recovery_admitted(self, agent_id: str) -> str | None:
        return self._agent_state(agent_id).admitted_request_id()

    def recovery_barrier_open(self, agent_id: str) -> bool:
        return self._agent_state(agent_id).barrier_open()

    def recovery_ready(self) -> bool:
        """Report whether every recovered agent queue is fully drained."""

        return all(state.recovery_idle() for state in self._agent_recovery.values())

    def _admit_recovery_head(
        self, agent_id: str
    ) -> asyncio.Future[Any] | None:
        state = self._agent_state(agent_id)
        if self.recovery_factory is None:
            raise CommandError("deferred command intents need a recovery executor")
        admission = state.promote_next(
            self.recovery_factory,
            self._recovery_queue.put_nowait,
        )
        if admission is None:
            return None
        command, future = admission
        future.add_done_callback(
            lambda completed: self._finish_recovery(command, completed)
        )
        self._start_recovery_worker()
        return future

    async def _queue_deferred_heads(self) -> list[asyncio.Future[Any]]:
        if not self._agent_recovery:
            return []
        if self.recovery_factory is None:
            raise CommandError("deferred command intents need a recovery executor")
        futures: list[asyncio.Future[Any]] = []
        for agent_id, state in tuple(self._agent_recovery.items()):
            existing = state.admitted_future()
            if existing is not None:
                futures.append(existing)
                continue
            future = self._admit_recovery_head(agent_id)
            if future is not None:
                futures.append(future)
        return futures

    async def _ensure_recovered(self) -> list[asyncio.Future[Any]]:
        if self._pending_loaded:
            return []
        async with self._recovery_lock:
            if self._pending_loaded:
                return []
            pending = await asyncio.to_thread(self.log.pending)
            if pending and self.recovery_factory is None:
                raise CommandError("pending command intents need a recovery executor")
            futures: list[asyncio.Future[Any]] = []
            for command in pending:
                key = (command.method, command.request_id)
                owner = self._agent_state(command.agent_id)
                inflight = owner.inflight(key)
                if inflight is not None:
                    existing, future = inflight
                    if not self._same_binding(command, existing):
                        raise CommandConflict(
                            f"request_id {command.request_id} has a conflicting intent"
                        )
                    futures.append(future)
                    continue
                existing = owner.find(key)
                if existing is not None:
                    if not self._same_binding(command, existing):
                        raise CommandConflict(
                            f"request_id {command.request_id} has a conflicting intent"
                        )
                    continue
                self.defer_for_recovery(command)
            self._pending_loaded = True
            return futures

    async def recover_pending(self) -> None:
        """Replay all pending intents before startup accepts new mutations."""

        futures = await self._ensure_recovered()
        known = set(futures)
        for future in await self._queue_deferred_heads():
            if future not in known:
                futures.append(future)
                known.add(future)
        for state in self._agent_recovery.values():
            state.take_promoted_futures()
        while futures:
            current = futures
            futures = []
            results = await asyncio.gather(
                *(asyncio.shield(future) for future in current),
                return_exceptions=True,
            )
            for future, result in zip(current, results, strict=True):
                if not isinstance(result, BaseException):
                    continue
                if isinstance(result, (_RecoveredCommandFailure, _RecoveredCommandRetry)):
                    continue
                if isinstance(
                    result,
                    (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                ):
                    raise result
                if isinstance(result, sqlite3.DatabaseError):
                    raise result
                raise result
            for state in self._agent_recovery.values():
                futures.extend(state.take_promoted_futures())

    def _finish_recovery(
        self, command: AgentCommand, future: asyncio.Future[Any]
    ) -> None:
        try:
            future.result()
        except _RecoveredCommandFailure as failure:
            self.recovery_failures.append((command, failure.error))
        except _RecoveredCommandRetry as retry:
            self.recovery_retries.append((command, retry.error))
        except BaseException as error:
            retry = CommandRetryable(
                "recovery failed before completion: "
                f"{type(error).__name__}: {error}"
            )
            self.recovery_retries.append((command, retry))

    def _transition(self, command: AgentCommand, outcome: str) -> None:
        state = self._agent_state(command.agent_id)
        if outcome == "retry":
            if state.find((command.method, command.request_id)) is None:
                state.defer(command)
            state.release_for_retry(command)
            return
        if outcome == "receipt_error" and state.find(
            (command.method, command.request_id)
        ) is None:
            state.defer(command)
        if not state.finalize_terminal(command, outcome):
            return
        successor = state.head()
        if successor is None:
            return
        try:
            self._admit_recovery_head(command.agent_id)
        except BaseException as error:
            self.recovery_retries.append(
                (
                    successor,
                    CommandRetryable(
                        "recovery promotion failed: "
                        f"{type(error).__name__}: {error}"
                    ),
                )
            )

    async def submit(self, command: AgentCommand, execute: CommandExecutor) -> Any:
        if self._closed:
            raise CommandError("command queue is closed")
        await self._ensure_recovered()
        key = (command.method, command.request_id)
        owner = self._agent_state(command.agent_id)
        existing = owner.inflight(key)
        if existing is not None:
            if not self._same_binding(command, existing[0]):
                raise CommandConflict(
                    f"request_id {command.request_id} has a conflicting command"
                )
            return await asyncio.shield(existing[1])
        queued_request_ids = owner.queued_request_ids()
        if queued_request_ids:
            await owner.wait_for_turn(key)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        owner.register_live(command, future)
        try:
            async with self._append_lock:
                current_state = (
                    self.agent_state_provider(command.agent_id)
                    if self.agent_state_provider is not None
                    else self.state_provider()
                )
                intent = await asyncio.to_thread(
                    self.log.append_intent, command, current_state
                )
            if intent.replay:
                # A receipt is terminal. It also closes any deferred retry
                # left by an earlier provider-control outage.
                self._transition(command, "success")
                future.set_result(intent.result)
                owner.finish_live(command, future)
                return await asyncio.shield(future)
            self._start_worker()
            await self._queue.put(_QueuedCommand(command, execute, future))
        except BaseException as exc:
            owner.finish_live(command, future)
            if not future.done():
                future.set_exception(exc)
        return await future

    async def _run(self, *, recovery: bool = False) -> None:
        queue = self._recovery_queue if recovery else self._queue
        while True:
            item = await queue.get()
            command, execute, future = item.command, item.execute, item.future
            outcome = "success"

            def receipt_error(error: BaseException) -> None:
                nonlocal outcome
                outcome = "receipt_error"
                if future.done():
                    return
                if item.recovering:
                    retry = CommandRetryable(
                        "command receipt could not be persisted: "
                        f"{type(error).__name__}: {error}"
                    )
                    future.set_exception(_RecoveredCommandRetry(retry))
                else:
                    future.set_exception(error)

            try:
                try:
                    await asyncio.to_thread(self.log.begin_effect, command)
                    result = await execute()
                except BaseException as exc:
                    if isinstance(exc, CommandRetryable):
                        outcome = "retry"
                        if not future.done():
                            if item.recovering:
                                future.set_exception(_RecoveredCommandRetry(exc))
                            else:
                                future.set_exception(exc)
                    elif item.recovering and not isinstance(exc, Exception):
                        outcome = "retry"
                        if not future.done():
                            future.set_exception(exc)
                    else:
                        try:
                            async with self._commit_lock:
                                state = (
                                    self.failure_state_provider(command.agent_id)
                                    if self.failure_state_provider is not None
                                    else self.agent_state_provider(command.agent_id)
                                    if self.agent_state_provider is not None
                                    else self.state_provider()
                                )
                                await asyncio.to_thread(self.log.fail, command, exc, state)
                                if command.payload.get("implicit_request_id") is True:
                                    await asyncio.to_thread(
                                        self.log.forget, command.method, command.request_id
                                    )
                        except BaseException as receipt_failure:
                            receipt_error(receipt_failure)
                        else:
                            outcome = "failure"
                            if not future.done():
                                if item.recovering and isinstance(exc, Exception):
                                    future.set_exception(_RecoveredCommandFailure(exc))
                                else:
                                    future.set_exception(exc)
                else:
                    try:
                        async with self._commit_lock:
                            state = (
                                self.agent_state_provider(command.agent_id)
                                if self.agent_state_provider is not None
                                else self.state_provider()
                            )
                            await asyncio.to_thread(
                                self.log.complete, command, result, state
                            )
                    except BaseException as receipt_failure:
                        receipt_error(receipt_failure)
                    else:
                        if not future.done():
                            future.set_result(result)
            except BaseException as exc:
                if item.recovering:
                    outcome = "retry"
                if not future.done():
                    future.set_exception(exc)
            finally:
                try:
                    self._transition(command, outcome)
                finally:
                    self._agent_state(command.agent_id).finish_live(command, future)
                    queue.task_done()

    async def close(self) -> None:
        self._closed = True
        await self._queue.join()
        await self._recovery_queue.join()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        if self._recovery_worker is not None:
            self._recovery_worker.cancel()
            await asyncio.gather(self._recovery_worker, return_exceptions=True)
        self._recovery_worker = None
