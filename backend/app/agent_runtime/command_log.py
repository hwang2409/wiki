"""Durable command ordering for supervisor agent operations.

The provider is an external side effect. This module records its intent
before execution and its result after execution. The sqlite database is the
durable receipt and event source for retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
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


def _canonical_command_hash(command: "AgentCommand") -> str:
    payload = dict(command.payload)
    identity_payload = payload.get("command_hash_payload")
    if isinstance(identity_payload, Mapping):
        payload = dict(identity_payload)
    value = json.dumps(
        {
            "method": command.method,
            "agent_id": command.agent_id,
            "payload": payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentCommand:
    """A typed mutation accepted by the supervisor command queue."""

    method: str
    agent_id: str
    request_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def command_hash(self) -> str:
        return _canonical_command_hash(self)

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
    error_module: str | None = None
    error_qualname: str | None = None
    agent_id: str | None = None
    command_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_type is None


def _restore_receipt_error(receipt: CommandReceipt) -> BaseException:
    module_name = receipt.error_module
    qualname = receipt.error_qualname or receipt.error_type
    if module_name and qualname:
        try:
            value: Any = importlib.import_module(module_name)
            for part in qualname.split("."):
                value = getattr(value, part)
            if isinstance(value, type) and issubclass(value, BaseException):
                return value(receipt.error_message or "command failed")
        except (ImportError, AttributeError, TypeError, ValueError):
            pass
    return CommandReceiptError(receipt.error_message or "command failed")


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
                    command_hash TEXT NOT NULL DEFAULT '',
                    implicit INTEGER NOT NULL DEFAULT 0,
                    result_run_id TEXT,
                    result_json TEXT,
                    error_type TEXT,
                    error_module TEXT,
                    error_qualname TEXT,
                    error_message TEXT,
                    event_sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(method, request_id)
                );
                CREATE TABLE IF NOT EXISTS command_intents (
                    method TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT '',
                    command_hash TEXT NOT NULL DEFAULT '',
                    implicit INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS command_effects (
                    method TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    command_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(method, request_id)
                );
                CREATE TABLE IF NOT EXISTS steer_effects (
                    method TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    command_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    pending_id TEXT,
                    message TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(method, request_id)
                );
                CREATE INDEX IF NOT EXISTS steer_effects_pending_idx
                    ON steer_effects(run_id, pending_id);
                CREATE TABLE IF NOT EXISTS start_requests (
                    request_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    implicit INTEGER NOT NULL DEFAULT 0,
                    committed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS archived_start_requests (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    session_path TEXT NOT NULL
                );
                """
            )
            # The first implementation shipped before the command binding
            # columns existed. Keep existing local runtimes readable.
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(command_receipts)"
                ).fetchall()
            }
            for name, definition in {
                "command_hash": "TEXT NOT NULL DEFAULT ''",
                "implicit": "INTEGER NOT NULL DEFAULT 0",
                "result_run_id": "TEXT",
                "error_module": "TEXT",
                "error_qualname": "TEXT",
            }.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE command_receipts ADD COLUMN {name} {definition}"
                    )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(command_intents)"
                ).fetchall()
            }
            for name, definition in {
                "agent_id": "TEXT NOT NULL DEFAULT ''",
                "command_hash": "TEXT NOT NULL DEFAULT ''",
                "implicit": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE command_intents ADD COLUMN {name} {definition}"
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
            error_module=row["error_module"],
            error_qualname=row["error_qualname"],
            agent_id=row["agent_id"],
            command_hash=row["command_hash"],
        )

    @staticmethod
    def _validate_binding(
        command: AgentCommand,
        *,
        agent_id: str | None,
        command_hash: str | None,
    ) -> None:
        if agent_id not in {None, "", command.agent_id}:
            raise CommandConflict(
                f"request_id {command.request_id} belongs to another agent"
            )
        if command_hash not in {None, "", command.command_hash}:
            raise CommandConflict(
                f"request_id {command.request_id} was used with a different payload"
            )

    @staticmethod
    def _result_run_id(result: Any) -> str | None:
        if isinstance(result, Mapping) and isinstance(result.get("run_id"), str):
            return result["run_id"]
        return None

    def _projection_for_command(
        self,
        connection: sqlite3.Connection,
        command: AgentCommand,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        if command.agent_id in state:
            return {command.agent_id: state.get(command.agent_id)}
        row = connection.execute(
            "SELECT state_json FROM command_projection WHERE agent_id = ?",
            (command.agent_id,),
        ).fetchone()
        if row is not None:
            value = json.loads(row["state_json"])
            return {command.agent_id: value}
        return {}

    @staticmethod
    def _write_agent_projection(
        connection: sqlite3.Connection,
        agent_id: str,
        state: Mapping[str, Any],
        sequence: int,
    ) -> None:
        value = state.get(agent_id)
        if value is None:
            connection.execute(
                "DELETE FROM command_projection WHERE agent_id = ?", (agent_id,)
            )
            return
        connection.execute(
            """
            INSERT INTO command_projection(agent_id, state_json, sequence)
            VALUES (?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                state_json = excluded.state_json,
                sequence = excluded.sequence
            """,
            (agent_id, CommandLog._json(value), sequence),
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

    def raise_receipt(self, method: str, request_id: str) -> None:
        receipt = self.receipt(method, request_id)
        if receipt is not None and not receipt.ok:
            raise _restore_receipt_error(receipt)

    def effect_result(self, method: str, request_id: str) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, result_json FROM command_effects
                WHERE method = ? AND request_id = ?
                """,
                (method, request_id),
            ).fetchone()
        if row is None or row["status"] != "completed":
            return None
        return json.loads(row["result_json"]) if row["result_json"] is not None else None

    def begin_effect(self, command: AgentCommand) -> None:
        """Persist the reactor checkpoint before an external effect starts."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM command_effects WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if row is not None:
                self._validate_binding(
                    command,
                    agent_id=row["agent_id"],
                    command_hash=row["command_hash"],
                )
                if row["status"] == "completed":
                    connection.commit()
                    return
            connection.execute(
                """
                INSERT INTO command_effects
                (method, request_id, agent_id, command_hash, status, result_json, created_at)
                VALUES (?, ?, ?, ?, 'started', NULL, ?)
                ON CONFLICT(method, request_id) DO UPDATE SET
                    status = CASE
                        WHEN command_effects.status = 'completed'
                        THEN command_effects.status
                        ELSE 'started'
                    END
                """,
                (
                    command.method,
                    command.request_id,
                    command.agent_id,
                    command.command_hash,
                    _now(),
                ),
            )
            connection.commit()

    def steer_effect(
        self,
        *,
        method: str,
        request_id: str,
        agent_id: str,
        command_hash: str,
        run_id: str,
        pending_id: str,
        message: str,
        mode: str,
    ) -> dict[str, Any]:
        """Create or read one durable steer delivery record."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM steer_effects WHERE method = ? AND request_id = ?",
                (method, request_id),
            ).fetchone()
            if row is None:
                now = _now()
                connection.execute(
                    """
                    INSERT INTO steer_effects
                    (method, request_id, agent_id, command_hash, run_id, pending_id,
                     message, mode, status, result_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?)
                    """,
                    (
                        method,
                        request_id,
                        agent_id,
                        command_hash,
                        run_id,
                        pending_id,
                        message,
                        mode,
                        now,
                        now,
                    ),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM steer_effects WHERE method = ? AND request_id = ?",
                    (method, request_id),
                ).fetchone()
            else:
                if row["agent_id"] not in {None, "", agent_id}:
                    raise CommandConflict(
                        f"request_id {request_id} belongs to another agent"
                    )
                if command_hash and row["command_hash"] not in {"", command_hash}:
                    raise CommandConflict(
                        f"request_id {request_id} was used with a different payload"
                    )
                connection.commit()
        assert row is not None
        result = dict(row)
        result["result"] = (
            json.loads(result["result_json"])
            if result.get("result_json") is not None
            else None
        )
        return result

    def update_steer_effect(
        self,
        method: str,
        request_id: str,
        status: str,
        result: Any | None = None,
    ) -> None:
        if status not in {"queued", "sent", "acknowledged"}:
            raise ValueError("invalid steer effect status")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steer_effects
                SET status = ?, result_json = COALESCE(?, result_json), updated_at = ?
                WHERE method = ? AND request_id = ?
                """,
                (
                    status,
                    self._json(result) if result is not None else None,
                    _now(),
                    method,
                    request_id,
                ),
            )
            connection.commit()

    def acknowledge_steer_for_pending(self, run_id: str, pending_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steer_effects
                SET status = 'acknowledged', updated_at = ?
                WHERE run_id = ? AND pending_id = ? AND status IN ('queued', 'sent')
                """,
                (_now(), run_id, pending_id),
            )
            connection.commit()

    def mark_steer_sent_for_pending(self, run_id: str, pending_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steer_effects
                SET status = 'sent', updated_at = ?
                WHERE run_id = ? AND pending_id = ? AND status = 'queued'
                """,
                (_now(), run_id, pending_id),
            )
            connection.commit()

    def complete_effect(self, command: AgentCommand, result: Any) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM command_effects WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if existing is not None:
                self._validate_binding(
                    command,
                    agent_id=existing["agent_id"],
                    command_hash=existing["command_hash"],
                )
            connection.execute(
                """
                INSERT INTO command_effects
                (method, request_id, agent_id, command_hash, status, result_json, created_at)
                VALUES (?, ?, ?, ?, 'completed', ?, ?)
                ON CONFLICT(method, request_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    command_hash = excluded.command_hash,
                    status = 'completed',
                    result_json = excluded.result_json
                """,
                (
                    command.method,
                    command.request_id,
                    command.agent_id,
                    command.command_hash,
                    self._json(result),
                    _now(),
                ),
            )
            connection.commit()

    def register_start_request(
        self,
        request_id: str,
        agent_id: str,
        run_id: str,
        *,
        implicit: bool = False,
        committed: bool = False,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO start_requests(request_id, agent_id, run_id, implicit, committed)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    run_id = excluded.run_id,
                    implicit = excluded.implicit,
                    committed = excluded.committed
                """,
                (request_id, agent_id, run_id, int(implicit), int(committed)),
            )
            connection.commit()

    def commit_start_request(self, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE start_requests SET committed = 1 WHERE request_id = ?",
                (request_id,),
            )
            connection.commit()

    def remove_start_request(self, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM start_requests WHERE request_id = ?", (request_id,)
            )
            connection.commit()

    def archive_start_request(
        self, request_id: str, run_id: str, session_path: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO archived_start_requests(request_id, run_id, session_path)
                VALUES (?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    session_path = excluded.session_path
                """,
                (request_id, run_id, session_path),
            )
            connection.commit()

    def archived_start_request(self, request_id: str) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id, session_path FROM archived_start_requests "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": str(row["run_id"]),
            "session_path": str(row["session_path"]),
        }

    def start_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM start_requests WHERE request_id = ? AND committed = 1",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_id": str(row["request_id"]),
            "agent_id": str(row["agent_id"]),
            "run_id": str(row["run_id"]),
            "implicit": bool(row["implicit"]),
        }

    def forget_implicit_for_run(self, run_id: str) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT method, request_id FROM command_receipts "
                "WHERE implicit = 1 AND result_run_id = ?",
                (run_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "DELETE FROM command_receipts WHERE method = ? AND request_id = ?",
                    (row["method"], row["request_id"]),
                )
                connection.execute(
                    "DELETE FROM command_effects WHERE method = ? AND request_id = ?",
                    (row["method"], row["request_id"]),
                )
                connection.execute(
                    "DELETE FROM steer_effects WHERE method = ? AND request_id = ?",
                    (row["method"], row["request_id"]),
                )
            connection.execute("DELETE FROM start_requests WHERE run_id = ?", (run_id,))
            connection.commit()

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
            connection.execute(
                "DELETE FROM command_effects WHERE method = ? AND request_id = ?",
                (method, request_id),
            )
            connection.execute(
                "DELETE FROM steer_effects WHERE method = ? AND request_id = ?",
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

    def projection_for(self, agent_id: str) -> dict[str, Any]:
        """Read one projected agent without scanning unrelated rows."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM command_projection WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return {}
        return {agent_id: json.loads(row["state_json"])}

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
                self._validate_binding(
                    command,
                    agent_id=receipt.agent_id,
                    command_hash=receipt.command_hash,
                )
                if not receipt.ok:
                    raise _restore_receipt_error(receipt)
                return CommandIntent(command, (), replay=True, result=receipt.result)
            existing = connection.execute(
                "SELECT * FROM command_intents WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if existing is not None:
                self._validate_binding(
                    command,
                    agent_id=existing["agent_id"],
                    command_hash=existing["command_hash"],
                )
                return CommandIntent(command, (), pending=True)
            command_state = self._projection_for_command(connection, command, state)
            events = decide(command, command_state)
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
            projected = _apply_projection(command_state, events)
            self._write_agent_projection(connection, command.agent_id, projected, sequence)
            connection.execute(
                """
                INSERT INTO command_intents
                (method, request_id, agent_id, command_hash, implicit,
                 command_json, intent_sequence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.method,
                    command.request_id,
                    command.agent_id,
                    command.command_hash,
                    int(command.payload.get("implicit_request_id") is True),
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
                existing_receipt = self._receipt(existing)
                self._validate_binding(
                    command,
                    agent_id=existing_receipt.agent_id,
                    command_hash=existing_receipt.command_hash,
                )
                connection.commit()
                return existing_receipt
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
            self._write_agent_projection(connection, command.agent_id, state, sequence)
            implicit = int(command.payload.get("implicit_request_id") is True)
            result_run_id = self._result_run_id(result)
            connection.execute(
                """
                INSERT INTO command_receipts
                (method, request_id, agent_id, command_hash, implicit, result_run_id,
                 result_json, error_type, error_module, error_qualname,
                 error_message, event_sequence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    command.method,
                    command.request_id,
                    command.agent_id,
                    command.command_hash,
                    implicit,
                    result_run_id,
                    self._json(result),
                    sequence,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM command_intents WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            )
            if command.method == "run/start" and result_run_id is not None:
                connection.execute(
                    """
                    INSERT INTO start_requests(request_id, agent_id, run_id, implicit, committed)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(request_id) DO UPDATE SET
                        agent_id = excluded.agent_id,
                        run_id = excluded.run_id,
                        implicit = excluded.implicit,
                        committed = 1
                    """,
                    (
                        command.request_id,
                        command.agent_id,
                        result_run_id,
                        implicit,
                    ),
                )
            connection.commit()
        return CommandReceipt(
            command.method,
            command.request_id,
            result=result,
            agent_id=command.agent_id,
            command_hash=command.command_hash,
        )

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
                "SELECT * FROM command_receipts WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if existing is not None:
                existing_receipt = self._receipt(existing)
                self._validate_binding(
                    command,
                    agent_id=existing_receipt.agent_id,
                    command_hash=existing_receipt.command_hash,
                )
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
            self._write_agent_projection(connection, command.agent_id, state, sequence)
            error_type = type(exc).__name__
            error_module = type(exc).__module__
            error_qualname = type(exc).__qualname__
            implicit = int(command.payload.get("implicit_request_id") is True)
            connection.execute(
                """
                INSERT INTO command_receipts
                (method, request_id, agent_id, command_hash, implicit, result_run_id,
                 result_json, error_type, error_module, error_qualname,
                 error_message, event_sequence, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.method,
                    command.request_id,
                    command.agent_id,
                    command.command_hash,
                    implicit,
                    error_type,
                    error_module,
                    error_qualname,
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
AgentStateProvider = Callable[[str], Mapping[str, Any]]
RecoveryExecutorFactory = Callable[[AgentCommand], CommandExecutor]


@dataclass
class _QueuedCommand:
    command: AgentCommand
    execute: CommandExecutor
    future: asyncio.Future[Any]


class CommandQueue:
    """Run durable provider effects in one global FIFO reactor."""

    def __init__(
        self,
        log: CommandLog,
        state_provider: Callable[[], Mapping[str, Any]],
        agent_state_provider: AgentStateProvider | None = None,
        recovery_factory: RecoveryExecutorFactory | None = None,
    ):
        self.log = log
        self.state_provider = state_provider
        self.agent_state_provider = agent_state_provider
        self.recovery_factory = recovery_factory
        self._queue: asyncio.Queue[_QueuedCommand] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._append_lock = asyncio.Lock()
        self._commit_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovered = False
        self._inflight: dict[
            tuple[str, str], tuple[AgentCommand, asyncio.Future[Any]]
        ] = {}
        self._closed = False

    def _start_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(),
                name="global-command-reactor",
            )

    @staticmethod
    def _same_binding(left: AgentCommand, right: AgentCommand) -> bool:
        return (
            left.agent_id == right.agent_id
            and left.command_hash == right.command_hash
        )

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
                futures.append(future)
                assert self.recovery_factory is not None
                await self._queue.put(
                    _QueuedCommand(command, self.recovery_factory(command), future)
                )
            self._recovered = True
            if pending:
                self._start_worker()
            return futures

    async def recover_pending(self) -> None:
        """Replay all pending intents before startup accepts new mutations."""

        futures = await self._ensure_recovered()
        if futures:
            await asyncio.gather(*(asyncio.shield(future) for future in futures))

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
                intent = await asyncio.to_thread(
                    self.log.append_intent, command, state
                )
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
            try:
                try:
                    await asyncio.to_thread(self.log.begin_effect, command)
                    result = await execute()
                except BaseException as exc:
                    async with self._commit_lock:
                        state = (
                            self.agent_state_provider(command.agent_id)
                            if self.agent_state_provider is not None
                            else self.state_provider()
                        )
                        await asyncio.to_thread(self.log.fail, command, exc, state)
                        if command.payload.get("implicit_request_id") is True:
                            await asyncio.to_thread(
                                self.log.forget, command.method, command.request_id
                            )
                    if not future.done():
                        future.set_exception(exc)
                else:
                    async with self._commit_lock:
                        state = (
                            self.agent_state_provider(command.agent_id)
                            if self.agent_state_provider is not None
                            else self.state_provider()
                        )
                        await asyncio.to_thread(self.log.complete, command, result, state)
                    if not future.done():
                        future.set_result(result)
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                key = (command.method, command.request_id)
                if self._inflight.get(key, (None, None))[1] is future:
                    self._inflight.pop(key, None)
                self._queue.task_done()

    async def close(self) -> None:
        self._closed = True
        await self._queue.join()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
