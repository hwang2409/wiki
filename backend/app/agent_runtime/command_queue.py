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
        self._worker: asyncio.Task[None] | None = None
        self._append_lock = asyncio.Lock()
        self._commit_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovered = False
        self._inflight: dict[
            tuple[str, str], tuple[AgentCommand, asyncio.Future[Any]]
        ] = {}
        self._recovery_commands: dict[asyncio.Future[Any], AgentCommand] = {}
        self._deferred: dict[tuple[str, str], AgentCommand] = {}
        self.recovery_failures: list[tuple[AgentCommand, Exception]] = []
        self.recovery_retries: list[tuple[AgentCommand, CommandRetryable]] = []
        self._closed = False

    def _start_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(), name="global-command-reactor"
            )

    @staticmethod
    def _same_binding(left: AgentCommand, right: AgentCommand) -> bool:
        return left.agent_id == right.agent_id and left.command_hash == right.command_hash

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
            loop = asyncio.get_running_loop()
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
                future: asyncio.Future[Any] = loop.create_future()
                self._inflight[key] = (command, future)
                self._recovery_commands[future] = command
                futures.append(future)
                assert self.recovery_factory is not None
                await self._queue.put(
                    _QueuedCommand(
                        command,
                        self.recovery_factory(command),
                        future,
                        recovering=True,
                    )
                )
            self._recovered = True
            if pending:
                self._start_worker()
            return futures

    async def recover_pending(self) -> None:
        """Replay all pending intents before startup accepts new mutations."""

        futures = await self._ensure_recovered()
        if self._deferred:
            if self.recovery_factory is None:
                raise CommandError("deferred command intents need a recovery executor")
            loop = asyncio.get_running_loop()
            for key, command in list(self._deferred.items()):
                if key in self._inflight:
                    continue
                future: asyncio.Future[Any] = loop.create_future()
                self._inflight[key] = (command, future)
                self._recovery_commands[future] = command
                futures.append(future)
                await self._queue.put(
                    _QueuedCommand(command, self.recovery_factory(command), future, True)
                )
            if futures:
                self._start_worker()
        if futures:
            results = await asyncio.gather(
                *(asyncio.shield(future) for future in futures),
                return_exceptions=True,
            )
            for future, result in zip(futures, results, strict=True):
                if not isinstance(result, BaseException):
                    continue
                if isinstance(result, _RecoveredCommandFailure):
                    command = self._recovery_commands.get(future)
                    if command is None:
                        raise CommandError("recovered command binding was lost")
                    self.recovery_failures.append((command, result.error))
                    continue
                if isinstance(result, _RecoveredCommandRetry):
                    command = self._recovery_commands.get(future)
                    if command is None:
                        raise CommandError("recovered command binding was lost")
                    self._deferred[(command.method, command.request_id)] = command
                    self.recovery_retries.append((command, result.error))
                    continue
                if isinstance(
                    result,
                    (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                ):
                    raise result
                if isinstance(result, sqlite3.DatabaseError):
                    raise result
                raise result

    async def submit(self, command: AgentCommand, execute: CommandExecutor) -> Any:
        if self._closed:
            raise CommandError("command queue is closed")
        await self._ensure_recovered()
        key = (command.method, command.request_id)
        self._deferred.pop(key, None)
        existing = self._inflight.get(key)
        if existing is not None:
            if not self._same_binding(command, existing[0]):
                raise CommandConflict(
                    f"request_id {command.request_id} has a conflicting command"
                )
            return await asyncio.shield(existing[1])
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

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            command, execute, future = item.command, item.execute, item.future
            retryable = False
            try:
                try:
                    await asyncio.to_thread(self.log.begin_effect, command)
                    result = await execute()
                except BaseException as exc:
                    if isinstance(exc, CommandRetryable):
                        retryable = True
                        if not future.done():
                            if item.recovering:
                                future.set_exception(_RecoveredCommandRetry(exc))
                            else:
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
                if not retryable:
                    self._deferred.pop(key, None)
                self._queue.task_done()

    async def close(self) -> None:
        self._closed = True
        await self._queue.join()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
