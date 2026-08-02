"""Durable command ordering and storage composition for agent operations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .command_decider import decide as _decide
from .command_effects import EffectStore
from .command_queue import CommandQueue
from .command_journal import CommandJournal
from .command_models import (
    AgentCommand,
    CommandConflict,
    CommandReceipt,
    CommandRetryable,
    CommandError as _CommandError,
    CommandReceiptError as _CommandReceiptError,
)
from .command_projection import ProjectionStore

CommandError = _CommandError
CommandReceiptError = _CommandReceiptError
decide = _decide

__all__ = [
    "AgentCommand",
    "CommandConflict",
    "CommandError",
    "CommandLog",
    "CommandQueue",
    "CommandReceipt",
    "CommandReceiptError",
    "CommandRetryable",
    "decide",
]


class CommandLog:
    """Compose typed journal, projection, and provider-effect stores."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()
        self.path.chmod(0o600)
        self.projections = ProjectionStore(self._connect, self._json)
        self.journal = CommandJournal(
            self._connect,
            self._json,
            self.projections,
            self._validate_binding,
        )
        self.effects = EffectStore(self._connect, self._json, self._validate_binding)

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
                CREATE TABLE IF NOT EXISTS replace_effects (
                    method TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    command_hash TEXT NOT NULL,
                    old_run_id TEXT NOT NULL,
                    replacement_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
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
            receipt_columns = {
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
                if name not in receipt_columns:
                    connection.execute(
                        f"ALTER TABLE command_receipts ADD COLUMN {name} {definition}"
                    )
            intent_columns = {
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
                if name not in intent_columns:
                    connection.execute(
                        f"ALTER TABLE command_intents ADD COLUMN {name} {definition}"
                    )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

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

    def receipt(self, method: str, request_id: str):
        return self.journal.receipt(method, request_id)

    def raise_receipt(self, method: str, request_id: str) -> None:
        self.journal.raise_receipt(method, request_id)

    def validate_receipt_binding(
        self,
        method: str,
        request_id: str,
        *,
        agent_id: str | None = None,
        command_hash: str | None = None,
    ):
        return self.journal.validate_receipt_binding(
            method,
            request_id,
            agent_id=agent_id,
            command_hash=command_hash,
        )

    def effect_result(
        self,
        method: str,
        request_id: str,
        *,
        agent_id: str | None = None,
        command_hash: str | None = None,
    ) -> Any | None:
        return self.effects.result(
            method,
            request_id,
            agent_id=agent_id,
            command_hash=command_hash,
        )

    def replace_effect(
        self,
        method: str,
        request_id: str,
        *,
        agent_id: str | None = None,
        command_hash: str | None = None,
    ) -> dict[str, Any] | None:
        return self.effects.validate_replace_binding(
            method,
            request_id,
            agent_id=agent_id,
            command_hash=command_hash,
        )

    def begin_replace_effect(self, **kwargs: str) -> None:
        self.effects.begin_replace(**kwargs)

    def update_replace_effect(
        self,
        method: str,
        request_id: str,
        status: str,
        result: Any | None = None,
    ) -> None:
        self.effects.update_replace(method, request_id, status, result)

    def begin_effect(self, command: AgentCommand) -> None:
        self.effects.begin(command)

    def steer_effect(self, **kwargs: str) -> dict[str, Any]:
        return self.effects.steer(**kwargs)

    def update_steer_effect(
        self,
        method: str,
        request_id: str,
        status: str,
        result: Any | None = None,
    ) -> None:
        self.effects.update_steer(method, request_id, status, result)

    def steer_effect_for_pending(
        self, run_id: str, pending_id: str
    ) -> dict[str, Any] | None:
        return self.effects.steer_for_pending(run_id, pending_id)

    def steer_effect_for_request(
        self, method: str, request_id: str
    ) -> dict[str, Any] | None:
        return self.effects.steer_for_request(method, request_id)

    def acknowledge_steer_for_pending(self, run_id: str, pending_id: str) -> None:
        self.effects.acknowledge_steer(run_id, pending_id)

    def mark_steer_sent_for_pending(self, run_id: str, pending_id: str) -> None:
        self.effects.mark_steer_sent(run_id, pending_id)

    def mark_steer_sending_for_pending(self, run_id: str, pending_id: str) -> None:
        self.effects.mark_steer_sending(run_id, pending_id)

    def revert_steer_sending_to_queued_for_pending(
        self, run_id: str, pending_id: str
    ) -> None:
        self.effects.revert_steer_sending_to_queued(run_id, pending_id)

    def sending_steer_effects(self) -> list[dict[str, Any]]:
        return self.effects.list_sending_steer_effects()

    def complete_effect(
        self,
        command: AgentCommand,
        result: Any,
        *,
        command_hash: str | None = None,
    ) -> None:
        self.effects.complete(command, result, command_hash=command_hash)

    def register_start_request(self, *args: Any, **kwargs: Any) -> None:
        self.journal.register_start_request(*args, **kwargs)

    def commit_start_request(self, request_id: str) -> None:
        self.journal.commit_start_request(request_id)

    def remove_start_request(self, request_id: str) -> None:
        self.journal.remove_start_request(request_id)

    def archive_start_request(
        self, request_id: str, run_id: str, session_path: str
    ) -> None:
        self.journal.archive_start_request(request_id, run_id, session_path)

    def archived_start_request(self, request_id: str) -> dict[str, str] | None:
        return self.journal.archived_start_request(request_id)

    def start_request(self, request_id: str) -> dict[str, Any] | None:
        return self.journal.start_request(request_id)

    def forget_implicit_for_run(self, run_id: str) -> None:
        self.journal.forget_implicit_for_run(run_id)

    def known(self, method: str, request_id: str) -> bool:
        return self.journal.known(method, request_id)

    def forget(self, method: str, request_id: str) -> None:
        self.journal.forget(method, request_id)

    def pending(self) -> list[AgentCommand]:
        return self.journal.pending()

    def projection(self) -> dict[str, Any]:
        return self.projections.read()

    def projection_for(self, agent_id: str) -> dict[str, Any]:
        return self.projections.read_for(agent_id)

    def seed_projection(self, agent_id: str, state: Mapping[str, Any]) -> None:
        self.projections.seed(agent_id, state)

    def replace_projection(self, agent_id: str, state: Mapping[str, Any]) -> None:
        self.projections.replace(agent_id, state)

    def append_intent(self, command: AgentCommand, state: Mapping[str, Any]):
        return self.journal.append_intent(command, state)

    def complete(
        self,
        command: AgentCommand,
        result: Any,
        state: Mapping[str, Any],
        *,
        event_type: str | None = None,
    ):
        return self.journal.complete(command, result, state, event_type=event_type)

    def fail(
        self,
        command: AgentCommand,
        exc: BaseException,
        state: Mapping[str, Any],
    ) -> None:
        self.journal.fail(command, exc, state)

    def events(self, *, method: str | None = None) -> list[dict[str, Any]]:
        return self.journal.events(method=method)
