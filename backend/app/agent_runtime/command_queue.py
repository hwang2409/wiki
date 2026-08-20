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
        self._recovered = False
        self._inflight: dict[
            tuple[str, str], tuple[AgentCommand, asyncio.Future[Any]]
        ] = {}
        self._recovery_agent_events: dict[str, asyncio.Event] = {}
        self._deferred: dict[str, list[AgentCommand]] = {}
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

    def _deferred_for_key(
        self, key: tuple[str, str]
    ) -> AgentCommand | None:
        for commands in self._deferred.values():
            for command in commands:
                if (command.method, command.request_id) == key:
                    return command
        return None

    def _defer(self, command: AgentCommand) -> None:
        commands = self._deferred.setdefault(command.agent_id, [])
        key = (command.method, command.request_id)
        if not any((item.method, item.request_id) == key for item in commands):
            commands.append(command)

    def _remove_deferred(self, command: AgentCommand) -> None:
        commands = self._deferred.get(command.agent_id)
        if commands is None:
            return
        key = (command.method, command.request_id)
        commands[:] = [
            item for item in commands
            if (item.method, item.request_id) != key
        ]
        if not commands:
            self._deferred.pop(command.agent_id, None)

    def _admit_recovery_head(
        self, agent_id: str
    ) -> asyncio.Future[Any] | None:
        commands = self._deferred.get(agent_id)
        if not commands or any(
            pending.agent_id == agent_id
            for pending, _future in self._inflight.values()
        ):
            return None
        command = commands[0]
        future = self._new_recovery_future(command)
        assert self.recovery_factory is not None
        self._recovery_queue.put_nowait(
            _QueuedCommand(
                command,
                self.recovery_factory(command),
                future,
                recovering=True,
            )
        )
        self._start_recovery_worker()
        return future

    async def _queue_deferred_heads(self) -> list[asyncio.Future[Any]]:
        if not self._deferred:
            return []
        if self.recovery_factory is None:
            raise CommandError("deferred command intents need a recovery executor")
        futures: list[asyncio.Future[Any]] = []
        for agent_id in tuple(self._deferred):
            head = self._deferred[agent_id][0]
            existing = self._inflight.get((head.method, head.request_id))
            if existing is not None:
                futures.append(existing[1])
                continue
            future = self._admit_recovery_head(agent_id)
            if future is not None:
                futures.append(future)
        return futures

    def _new_recovery_future(
        self, command: AgentCommand
    ) -> asyncio.Future[Any]:
        future = asyncio.get_running_loop().create_future()
        self._inflight[(command.method, command.request_id)] = (command, future)
        self._recovery_agent_events.setdefault(
            command.agent_id, asyncio.Event()
        ).clear()
        future.add_done_callback(
            lambda completed: self._finish_recovery(command, completed)
        )
        return future

    async def _ensure_recovered(self) -> list[asyncio.Future[Any]]:
        if self._recovered:
            return []
        async with self._recovery_lock:
            if self._recovered:
                return []
            pending = await asyncio.to_thread(self.log.pending)
            if pending and self.recovery_factory is None:
                raise CommandError("pending command intents need a recovery executor")
            futures: list[asyncio.Future[Any]] = []
            for command in pending:
                key = (command.method, command.request_id)
                existing = self._inflight.get(key)
                if existing is not None:
                    if not self._same_binding(command, existing[0]):
                        raise CommandConflict(
                            f"request_id {command.request_id} has a conflicting intent"
                        )
                    futures.append(existing[1])
                    continue
                deferred = self._deferred_for_key(key)
                if deferred is not None:
                    if not self._same_binding(command, deferred):
                        raise CommandConflict(
                            f"request_id {command.request_id} has a conflicting intent"
                        )
                    continue
                self._defer(command)
            self._recovered = True
            return futures

    async def recover_pending(self) -> None:
        """Replay all pending intents before startup accepts new mutations."""

        futures = await self._ensure_recovered()
        futures.extend(await self._queue_deferred_heads())
        if futures:
            results = await asyncio.gather(
                *(asyncio.shield(future) for future in futures),
                return_exceptions=True,
            )
            for future, result in zip(futures, results, strict=True):
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

    def _finish_recovery(
        self, command: AgentCommand, future: asyncio.Future[Any]
    ) -> None:
        try:
            future.result()
        except _RecoveredCommandFailure as failure:
            self._remove_deferred(command)
            self.recovery_failures.append((command, failure.error))
            self._admit_recovery_head(command.agent_id)
        except _RecoveredCommandRetry as retry:
            self._defer(command)
            self.recovery_retries.append((command, retry.error))
            return
        except BaseException as error:
            # Keep an incomplete durable intent blocked. The next recovery
            # poll retries it instead of allowing same-agent work to pass.
            retry = CommandRetryable(
                "recovery failed before completion: "
                f"{type(error).__name__}: {error}"
            )
            self._defer(command)
            self.recovery_retries.append((command, retry))
            return
        self._remove_deferred(command)
        self._admit_recovery_head(command.agent_id)
        self._open_recovery_barrier_if_ready(command.agent_id)

    def _open_recovery_barrier_if_ready(self, agent_id: str) -> None:
        if not any(
            pending.agent_id == agent_id
            for pending, _future in self._inflight.values()
        ) and not any(
            pending.agent_id == agent_id
            for commands in self._deferred.values()
            for pending in commands
        ):
            self._recovery_agent_events[agent_id].set()

    async def submit(self, command: AgentCommand, execute: CommandExecutor) -> Any:
        if self._closed:
            raise CommandError("command queue is closed")
        await self._ensure_recovered()
        key = (command.method, command.request_id)
        existing = self._inflight.get(key)
        if existing is not None:
            if not self._same_binding(command, existing[0]):
                raise CommandConflict(
                    f"request_id {command.request_id} has a conflicting command"
                )
            return await asyncio.shield(existing[1])
        deferred = self._deferred_for_key(key)
        deferred_head = self._deferred.get(command.agent_id, [None])[0]
        recovery_event = self._recovery_agent_events.get(command.agent_id)
        if (
            deferred is not deferred_head
            and recovery_event is not None
            and not recovery_event.is_set()
        ):
            await recovery_event.wait()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._inflight[key] = (command, future)
        try:
            async with self._append_lock:
                state = (
                    self.agent_state_provider(command.agent_id)
                    if self.agent_state_provider is not None
                    else self.state_provider()
                )
                intent = await asyncio.to_thread(self.log.append_intent, command, state)
            if intent.replay:
                # A receipt is terminal. It also closes any deferred retry
                # left by an earlier provider-control outage.
                self._remove_deferred(command)
                future.set_result(intent.result)
                self._inflight.pop(key, None)
                return await asyncio.shield(future)
            self._start_worker()
            await self._queue.put(_QueuedCommand(command, execute, future))
        except BaseException as exc:
            if self._inflight.get(key, (None, None))[1] is future:
                self._inflight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
        return await future

    async def _run(self, *, recovery: bool = False) -> None:
        queue = self._recovery_queue if recovery else self._queue
        while True:
            item = await queue.get()
            command, execute, future = item.command, item.execute, item.future
            retryable = False
            try:
                try:
                    await asyncio.to_thread(self.log.begin_effect, command)
                    result = await execute()
                except BaseException as exc:
                    if isinstance(exc, CommandRetryable):
                        retryable = True
                        if item.recovering:
                            self._defer(command)
                        if not future.done():
                            if item.recovering:
                                future.set_exception(_RecoveredCommandRetry(exc))
                            else:
                                future.set_exception(exc)
                    elif item.recovering and not isinstance(exc, Exception):
                        if not future.done():
                            future.set_exception(exc)
                    else:
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
                        if not future.done():
                            if item.recovering and isinstance(exc, Exception):
                                future.set_exception(_RecoveredCommandFailure(exc))
                            else:
                                future.set_exception(exc)
                else:
                    async with self._commit_lock:
                        state = (
                            self.agent_state_provider(command.agent_id)
                            if self.agent_state_provider is not None
                            else self.state_provider()
                        )
                        await asyncio.to_thread(
                            self.log.complete, command, result, state
                        )
                    if not future.done():
                        future.set_result(result)
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                key = (command.method, command.request_id)
                if self._inflight.get(key, (None, None))[1] is future:
                    self._inflight.pop(key, None)
                if retryable:
                    # A normal client retry can hit the same detached
                    # provider as recovered work. Keep it eligible for the
                    # next recovery poll without a daemon restart.
                    self._defer(command)
                else:
                    self._remove_deferred(command)
                    if not item.recovering and not retryable:
                        self._admit_recovery_head(command.agent_id)
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
