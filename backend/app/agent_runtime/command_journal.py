"""Durable event, intent, receipt, and request-index storage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .command_decider import apply_projection, decide
from .command_models import (
    AgentCommand,
    CommandConflict,
    CommandIntent,
    CommandReceipt,
    restore_receipt_error,
)
from .command_projection import ProjectionStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommandJournal:
    """Own transactions for command events, intents, receipts, and indexes."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        encode: Callable[[Any], str],
        projection: ProjectionStore,
        validate_binding: Callable[..., None],
    ) -> None:
        self._connect = connect
        self._encode = encode
        self._projection = projection
        self._validate_binding = validate_binding

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
        return self._projection.read_in_transaction(connection, command.agent_id)

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
            raise restore_receipt_error(receipt)

    def validate_receipt_binding(
        self,
        method: str,
        request_id: str,
        *,
        agent_id: str | None = None,
        command_hash: str | None = None,
    ) -> CommandReceipt | None:
        receipt = self.receipt(method, request_id)
        if receipt is not None:
            if agent_id not in {None, "", receipt.agent_id}:
                raise CommandConflict(
                    f"request_id {request_id} belongs to another agent"
                )
            if command_hash not in {None, "", receipt.command_hash}:
                raise CommandConflict(
                    f"request_id {request_id} was used with a different payload"
                )
        return receipt

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
        with self._connect() as connection:
            for table in (
                "command_receipts",
                "command_intents",
                "command_effects",
                "steer_effects",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE method = ? AND request_id = ?",
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

    def append_intent(
        self,
        command: AgentCommand,
        state: Mapping[str, Any],
    ) -> CommandIntent:
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
                    raise restore_receipt_error(receipt)
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
                        self._encode(dict(event.payload)),
                        now,
                    ),
                )
                sequence = int(cursor.lastrowid)
            projected = apply_projection(command_state, events)
            self._projection.write_in_transaction(
                connection,
                command.agent_id,
                projected,
                sequence,
                self._encode,
            )
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
                    self._encode(self._command_json(command)),
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
                    self._encode(result),
                    now,
                ),
            )
            sequence = int(cursor.lastrowid)
            self._projection.write_in_transaction(
                connection,
                command.agent_id,
                state,
                sequence,
                self._encode,
            )
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
                    self._encode(result),
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
                    self._encode({"type": type(exc).__name__, "message": str(exc)}),
                    now,
                ),
            )
            sequence = int(cursor.lastrowid)
            self._projection.write_in_transaction(
                connection,
                command.agent_id,
                state,
                sequence,
                self._encode,
            )
            error_type = type(exc).__name__
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
                    int(command.payload.get("implicit_request_id") is True),
                    error_type,
                    type(exc).__module__,
                    type(exc).__qualname__,
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
