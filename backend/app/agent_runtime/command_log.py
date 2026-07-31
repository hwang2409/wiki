"""Durable command ordering for supervisor agent operations.

The provider is an external side effect. This module records its intent
before execution and its result after execution. The sqlite database is the
durable receipt and event source for retries.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4


class CommandError(RuntimeError):
    """Base class for command-log failures."""


class CommandConflict(CommandError):
    """The command is not valid for the current projection."""


class CommandReceiptError(CommandError):
    """A prior command attempt recorded an error receipt."""


@dataclass(frozen=True)
class AgentCommand:
    """A typed mutation accepted by the supervisor command queue."""

    method: str
    agent_id: str
    request_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: str(uuid4()))

    @classmethod
    def spawn(
        cls,
        *,
        agent_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> AgentCommand:
        return cls("run/start", agent_id, request_id, dict(payload))

    @classmethod
    def steer(
        cls,
        *,
        agent_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> AgentCommand:
        method = str(payload.get("method") or "run/send_now")
        if method not in {"run/send_now", "run/send_on_idle"}:
            raise ValueError("steer method must be run/send_now or run/send_on_idle")
        return cls(method, agent_id, request_id, dict(payload))

    @classmethod
    def archive(
        cls,
        *,
        agent_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> AgentCommand:
        return cls("run/archive", agent_id, request_id, dict(payload))

    @classmethod
    def replace(
        cls,
        *,
        agent_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> AgentCommand:
        return cls("run/replace", agent_id, request_id, dict(payload))


@dataclass(frozen=True)
class EventSpec:
    event_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CommandIntent:
    command: AgentCommand
    events: tuple[EventSpec, ...]
    replay: bool = False
    pending: bool = False
    result: Any = None


@dataclass(frozen=True)
class CommandReceipt:
    method: str
    request_id: str
    result: Any = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_type is None


def _current_projection(
    state: Mapping[str, Any], agent_id: str
) -> Mapping[str, Any] | None:
    value = state.get(agent_id)
    if not isinstance(value, Mapping):
        return None
    current = value.get("current")
    return current if isinstance(current, Mapping) else None


def _require_current(
    state: Mapping[str, Any], command: AgentCommand
) -> Mapping[str, Any]:
    current = _current_projection(state, command.agent_id)
    if current is None or not current.get("run_id"):
        raise CommandConflict(f"{command.agent_id} has no current run")
    return current


def decide(
    command: AgentCommand,
    state: Mapping[str, Any],
) -> tuple[EventSpec, ...]:
    """Purely decide command events from the current registry projection."""

    payload = dict(command.payload)
    current = _current_projection(state, command.agent_id)
    if command.method == "run/start":
        if current is not None and current.get("run_id"):
            raise CommandConflict(f"{command.agent_id} already has a current run")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise CommandError("spawn command requires a run_id")
        return (
            EventSpec(
                "run/start_requested",
                {
                    "agent_id": command.agent_id,
                    "run_id": run_id,
                    "request_id": command.request_id,
                },
            ),
        )
    current = _require_current(state, command)
    requested_run_id = payload.get("run_id")
    if requested_run_id is not None and requested_run_id != current.get("run_id"):
        raise CommandConflict(f"{command.agent_id} run is no longer current")
    run_id = str(requested_run_id or current["run_id"])
    if command.method in {"run/send_now", "run/send_on_idle"}:
        return (
            EventSpec(
                "run/steer_requested",
                {
                    "agent_id": command.agent_id,
                    "run_id": run_id,
                    "mode": "now" if command.method == "run/send_now" else "on-idle",
                    "request_id": command.request_id,
                },
            ),
        )
    if command.method == "run/archive":
        return (
            EventSpec(
                "run/archive_requested",
                {
                    "agent_id": command.agent_id,
                    "run_id": run_id,
                    "request_id": command.request_id,
                },
            ),
        )
    if command.method == "run/replace":
        replacement_id = payload.get("replacement_run_id")
        if not isinstance(replacement_id, str) or not replacement_id:
            raise CommandError("replace command requires replacement_run_id")
        return (
            EventSpec(
                "run/replace_requested",
                {
                    "agent_id": command.agent_id,
                    "run_id": run_id,
                    "replacement_run_id": replacement_id,
                    "request_id": command.request_id,
                },
            ),
        )
    raise CommandError(f"unsupported agent command: {command.method}")


def _apply_projection(
    state: dict[str, Any],
    events: tuple[EventSpec, ...],
) -> dict[str, Any]:
    """Apply command events to a small registry-shaped projection."""

    projected = json.loads(json.dumps(state))
    for event in events:
        payload = dict(event.payload)
        agent_id = str(payload["agent_id"])
        entry = projected.get(agent_id)
        if not isinstance(entry, dict):
            entry = {"history": []}
            projected[agent_id] = entry
        current = entry.get("current")
        if event.event_type == "run/start_requested":
            entry["current"] = {
                "ticket": agent_id,
                "run_id": payload["run_id"],
                "state": "starting",
                "start_request_id": payload["request_id"],
            }
        elif event.event_type == "run/replace_requested":
            entry["current"] = {
                **(dict(current) if isinstance(current, dict) else {}),
                "run_id": payload["replacement_run_id"],
                "state": "starting",
                "replaces_run_id": payload["run_id"],
            }
        elif event.event_type == "run/archive_completed":
            projected.pop(agent_id, None)
    return projected


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommandLog:
    """SQLite event log, receipt store, and command intent journal."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS command_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS command_events_request_idx
                    ON command_events(method, request_id, sequence);
                CREATE TABLE IF NOT EXISTS command_receipts (
                    method TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    result_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    event_sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(method, request_id)
                );
                CREATE TABLE IF NOT EXISTS command_intents (
                    method TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    intent_sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(method, request_id)
                );
                CREATE TABLE IF NOT EXISTS command_projection (
                    agent_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    sequence INTEGER NOT NULL
                );
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _command_json(command: AgentCommand) -> dict[str, Any]:
        return {
            "method": command.method,
            "agent_id": command.agent_id,
            "request_id": command.request_id,
            "payload": dict(command.payload),
            "command_id": command.command_id,
        }

    @staticmethod
    def _receipt(row: sqlite3.Row) -> CommandReceipt:
        result = None
        if row["result_json"] is not None:
            result = json.loads(row["result_json"])
        return CommandReceipt(
            method=str(row["method"]),
            request_id=str(row["request_id"]),
            result=result,
            error_type=row["error_type"],
            error_message=row["error_message"],
        )

    @staticmethod
    def _remove_stale_projections(
        connection: sqlite3.Connection,
        state: Mapping[str, Any],
    ) -> None:
        agent_ids = [agent_id for agent_id in state if not agent_id.startswith("_")]
        if not agent_ids:
            connection.execute("DELETE FROM command_projection")
            return
        marks = ",".join("?" for _ in agent_ids)
        connection.execute(
            f"DELETE FROM command_projection WHERE agent_id NOT IN ({marks})",
            agent_ids,
        )

    def receipt(self, method: str, request_id: str) -> CommandReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM command_receipts WHERE method = ? AND request_id = ?",
                (method, request_id),
            ).fetchone()
        return self._receipt(row) if row is not None else None

    def known(self, method: str, request_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM command_receipts
                    WHERE method = ? AND request_id = ?
                    UNION ALL
                    SELECT 1 FROM command_intents
                    WHERE method = ? AND request_id = ?
                    LIMIT 1
                    """,
                    (method, request_id, method, request_id),
                ).fetchone()
                is not None
            )

    def forget(self, method: str, request_id: str) -> None:
        """Drop a replay receipt when an implicit operation is reusable."""

        with self._connect() as connection:
            connection.execute(
                "DELETE FROM command_receipts WHERE method = ? AND request_id = ?",
                (method, request_id),
            )
            connection.execute(
                "DELETE FROM command_intents WHERE method = ? AND request_id = ?",
                (method, request_id),
            )
            connection.commit()

    def pending(self) -> list[AgentCommand]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT command_json FROM command_intents ORDER BY intent_sequence"
            ).fetchall()
        return [
            AgentCommand(
                method=str(value["method"]),
                agent_id=str(value["agent_id"]),
                request_id=str(value["request_id"]),
                payload=dict(value.get("payload") or {}),
                command_id=str(value["command_id"]),
            )
            for row in rows
            for value in [json.loads(row["command_json"])]
        ]

    def projection(self) -> dict[str, Any]:
        """Read the durable registry-shaped command projection."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT agent_id, state_json FROM command_projection ORDER BY agent_id"
            ).fetchall()
        return {str(row["agent_id"]): json.loads(row["state_json"]) for row in rows}

    def append_intent(
        self,
        command: AgentCommand,
        state: Mapping[str, Any],
    ) -> CommandIntent:
        """Append an intent in one transaction with its initial projection."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt_row = connection.execute(
                "SELECT * FROM command_receipts WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if receipt_row is not None:
                receipt = self._receipt(receipt_row)
                if not receipt.ok:
                    raise CommandReceiptError(receipt.error_message or "command failed")
                return CommandIntent(command, (), replay=True, result=receipt.result)
            existing = connection.execute(
                "SELECT 1 FROM command_intents WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if existing is not None:
                return CommandIntent(command, (), pending=True)
            events = decide(command, state)
            sequence = 0
            now = _now()
            for event in events:
                cursor = connection.execute(
                    """
                    INSERT INTO command_events
                    (command_id, request_id, method, agent_id, phase,
                     event_type, payload_json, created_at)
                    VALUES (?, ?, ?, ?, 'intent', ?, ?, ?)
                    """,
                    (
                        command.command_id,
                        command.request_id,
                        command.method,
                        command.agent_id,
                        event.event_type,
                        self._json(dict(event.payload)),
                        now,
                    ),
                )
                sequence = int(cursor.lastrowid)
            projected = _apply_projection(dict(state), events)
            self._remove_stale_projections(connection, projected)
            for agent_id, value in projected.items():
                if agent_id.startswith("_"):
                    continue
                connection.execute(
                    """
                    INSERT INTO command_projection(agent_id, state_json, sequence)
                    VALUES (?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        sequence = excluded.sequence
                    """,
                    (agent_id, self._json(value), sequence),
                )
            connection.execute(
                """
                INSERT INTO command_intents
                (method, request_id, command_json, intent_sequence, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    command.method,
                    command.request_id,
                    self._json(self._command_json(command)),
                    sequence,
                    now,
                ),
            )
            connection.commit()
        return CommandIntent(command, events)

    def complete(
        self,
        command: AgentCommand,
        result: Any,
        state: Mapping[str, Any],
        *,
        event_type: str | None = None,
    ) -> CommandReceipt:
        """Append the completion, project state, and receipt atomically."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM command_receipts WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._receipt(existing)
            now = _now()
            cursor = connection.execute(
                """
                INSERT INTO command_events
                (command_id, request_id, method, agent_id, phase,
                 event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, 'result', ?, ?, ?)
                """,
                (
                    command.command_id,
                    command.request_id,
                    command.method,
                    command.agent_id,
                    event_type or f"{command.method.replace('/', '_')}_completed",
                    self._json(result),
                    now,
                ),
            )
            sequence = int(cursor.lastrowid)
            self._remove_stale_projections(connection, state)
            for agent_id, value in state.items():
                if agent_id.startswith("_"):
                    continue
                connection.execute(
                    """
                    INSERT INTO command_projection(agent_id, state_json, sequence)
                    VALUES (?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        sequence = excluded.sequence
                    """,
                    (agent_id, self._json(value), sequence),
                )
            connection.execute(
                """
                INSERT INTO command_receipts
                (method, request_id, agent_id, result_json, error_type,
                 error_message, event_sequence, created_at)
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    command.method,
                    command.request_id,
                    command.agent_id,
                    self._json(result),
                    sequence,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM command_intents WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            )
            connection.commit()
        return CommandReceipt(command.method, command.request_id, result=result)

    def fail(
        self,
        command: AgentCommand,
        exc: BaseException,
        state: Mapping[str, Any],
    ) -> None:
        """Persist a failed attempt so retries cannot repeat its side effect."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM command_receipts WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return
            now = _now()
            cursor = connection.execute(
                """
                INSERT INTO command_events
                (command_id, request_id, method, agent_id, phase,
                 event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, 'result', ?, ?, ?)
                """,
                (
                    command.command_id,
                    command.request_id,
                    command.method,
                    command.agent_id,
                    f"{command.method.replace('/', '_')}_failed",
                    self._json({"type": type(exc).__name__, "message": str(exc)}),
                    now,
                ),
            )
            sequence = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO command_receipts
                (method, request_id, agent_id, result_json, error_type,
                 error_message, event_sequence, created_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    command.method,
                    command.request_id,
                    command.agent_id,
                    type(exc).__name__,
                    str(exc),
                    sequence,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM command_intents WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            )
            connection.commit()

    def events(self, *, method: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM command_events"
        args: tuple[Any, ...] = ()
        if method is not None:
            query += " WHERE method = ?"
            args = (method,)
        query += " ORDER BY sequence"
        with self._connect() as connection:
            rows = connection.execute(query, args).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "command_id": row["command_id"],
                "request_id": row["request_id"],
                "method": row["method"],
                "agent_id": row["agent_id"],
                "phase": row["phase"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


CommandExecutor = Callable[[], Awaitable[Any]]


class CommandQueue:
    """A single async worker that serializes command side effects."""

    def __init__(
        self,
        log: CommandLog,
        state_provider: Callable[[], Mapping[str, Any]],
    ):
        self.log = log
        self.state_provider = state_provider
        self._queue: asyncio.Queue[
            tuple[AgentCommand, CommandExecutor, asyncio.Future[Any]]
        ] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    async def submit(self, command: AgentCommand, execute: CommandExecutor) -> Any:
        if self._closed:
            raise CommandError("command queue is closed")
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="agent-command-queue")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put((command, execute, future))
        return await future

    async def _run(self) -> None:
        while True:
            command, execute, future = await self._queue.get()
            try:
                intent = await asyncio.to_thread(
                    self.log.append_intent, command, self.state_provider()
                )
                if intent.replay:
                    if not future.done():
                        future.set_result(intent.result)
                    continue
                try:
                    result = await execute()
                except BaseException as exc:
                    await asyncio.to_thread(
                        self.log.fail, command, exc, self.state_provider()
                    )
                    if command.payload.get("implicit_request_id") is True:
                        await asyncio.to_thread(
                            self.log.forget, command.method, command.request_id
                        )
                    if not future.done():
                        future.set_exception(exc)
                else:
                    await asyncio.to_thread(
                        self.log.complete, command, result, self.state_provider()
                    )
                    if not future.done():
                        future.set_result(result)
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        self._closed = True
        worker = self._worker
        if worker is None:
            return
        if not self._queue.empty():
            await self._queue.join()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self._worker = None
