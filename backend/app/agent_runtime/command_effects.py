"""Durable provider-effect checkpoints owned by each operation type."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .command_models import AgentCommand, CommandConflict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EffectStore:
    """Own steer, replace, and generic effect outbox tables."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        encode: Callable[[Any], str],
        validate_binding: Callable[..., None],
    ) -> None:
        self._connect = connect
        self._encode = encode
        self._validate_binding = validate_binding

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = (
            json.loads(result["result_json"])
            if result.get("result_json") is not None
            else None
        )
        return result

    def result(
        self,
        method: str,
        request_id: str,
        *,
        agent_id: str | None = None,
        command_hash: str | None = None,
    ) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM command_effects
                WHERE method = ? AND request_id = ?
                """,
                (method, request_id),
            ).fetchone()
        if row is None or row["status"] != "completed":
            return None
        if agent_id not in {None, "", row["agent_id"]}:
            raise CommandConflict(
                f"request_id {request_id} belongs to another agent"
            )
        if (
            command_hash not in {None, "", row["command_hash"]}
            and row["command_hash"] != ""
        ):
            raise CommandConflict(
                f"request_id {request_id} was used with a different payload"
            )
        return json.loads(row["result_json"]) if row["result_json"] is not None else None

    def replace_result(self, method: str, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM replace_effects WHERE method = ? AND request_id = ?",
                (method, request_id),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def validate_replace_binding(
        self,
        method: str,
        request_id: str,
        *,
        agent_id: str | None = None,
        command_hash: str | None = None,
    ) -> dict[str, Any] | None:
        result = self.replace_result(method, request_id)
        if result is None:
            return None
        if agent_id not in {None, "", result.get("agent_id")}:
            raise CommandConflict(
                f"request_id {request_id} belongs to another agent"
            )
        if (
            command_hash not in {None, "", result.get("command_hash")}
            and result.get("command_hash") != ""
        ):
            raise CommandConflict(
                f"request_id {request_id} was used with a different payload"
            )
        return result

    def begin_replace(
        self,
        *,
        method: str,
        request_id: str,
        agent_id: str,
        command_hash: str,
        old_run_id: str,
        replacement_run_id: str,
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM replace_effects WHERE method = ? AND request_id = ?",
                (method, request_id),
            ).fetchone()
            if row is not None:
                if row["agent_id"] != agent_id:
                    raise CommandConflict(
                        f"request_id {request_id} belongs to another agent"
                    )
                if row["command_hash"] != command_hash:
                    raise CommandConflict(
                        f"request_id {request_id} was used with a different payload"
                    )
                connection.commit()
                return
            now = _now()
            connection.execute(
                """
                INSERT INTO replace_effects
                (method, request_id, agent_id, command_hash, old_run_id,
                 replacement_run_id, status, result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'prepared', NULL, ?, ?)
                """,
                (
                    method,
                    request_id,
                    agent_id,
                    command_hash,
                    old_run_id,
                    replacement_run_id,
                    now,
                    now,
                ),
            )
            connection.commit()

    def update_replace(
        self,
        method: str,
        request_id: str,
        status: str,
        result: Any | None = None,
    ) -> None:
        if status not in {
            "prepared",
            "published",
            "provider_started",
            "provider_completed",
            "completed",
            "failed",
        }:
            raise ValueError("invalid replace effect status")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE replace_effects
                SET status = ?, result_json = COALESCE(?, result_json), updated_at = ?
                WHERE method = ? AND request_id = ?
                """,
                (
                    status,
                    self._encode(result) if result is not None else None,
                    _now(),
                    method,
                    request_id,
                ),
            )
            connection.commit()

    def begin(self, command: AgentCommand) -> None:
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

    def complete(
        self,
        command: AgentCommand,
        result: Any,
        *,
        command_hash: str | None = None,
    ) -> None:
        binding_hash = command_hash or command.command_hash
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM command_effects WHERE method = ? AND request_id = ?",
                (command.method, command.request_id),
            ).fetchone()
            if existing is not None:
                if existing["agent_id"] != command.agent_id:
                    raise CommandConflict(
                        f"request_id {command.request_id} belongs to another agent"
                    )
                if existing["command_hash"] not in {"", binding_hash}:
                    raise CommandConflict(
                        f"request_id {command.request_id} was used with a different payload"
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
                    binding_hash,
                    self._encode(result),
                    _now(),
                ),
            )
            connection.commit()

    def steer(
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
        return self._decode(row)

    def steer_for_pending(
        self, run_id: str, pending_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM steer_effects
                WHERE run_id = ? AND pending_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (run_id, pending_id),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def update_steer(
        self,
        method: str,
        request_id: str,
        status: str,
        result: Any | None = None,
    ) -> None:
        if status not in {"queued", "sending", "sent", "acknowledged"}:
            raise ValueError("invalid steer effect status")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steer_effects
                SET status = CASE
                        WHEN status IN ('sent', 'acknowledged')
                            AND ? <> 'acknowledged'
                        THEN status
                        WHEN status = 'sending' AND ? = 'queued'
                        THEN status
                        ELSE ?
                    END,
                    result_json = COALESCE(?, result_json), updated_at = ?
                WHERE method = ? AND request_id = ?
                """,
                (
                    status,
                    status,
                    status,
                    self._encode(result) if result is not None else None,
                    _now(),
                    method,
                    request_id,
                ),
            )
            connection.commit()

    def acknowledge_steer(self, run_id: str, pending_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steer_effects
                SET status = 'acknowledged', updated_at = ?
                WHERE run_id = ? AND pending_id = ?
                    AND status IN ('queued', 'sending', 'sent')
                """,
                (_now(), run_id, pending_id),
            )
            connection.commit()

    def mark_steer_sent(self, run_id: str, pending_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steer_effects
                SET status = 'sent', updated_at = ?
                WHERE run_id = ? AND pending_id = ?
                    AND status IN ('queued', 'sending')
                """,
                (_now(), run_id, pending_id),
            )
            connection.commit()

    def mark_steer_sending(self, run_id: str, pending_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steer_effects
                SET status = 'sending', updated_at = ?
                WHERE run_id = ? AND pending_id = ? AND status = 'queued'
                """,
                (_now(), run_id, pending_id),
            )
            connection.commit()

    def list_sending_steer_effects(self) -> list[dict[str, Any]]:
        """Return every steer effect currently at status='sending'.

        Used by recovery to reconcile effects that were mid-flight when the
        supervisor stopped: without a sweep those wedge the on-idle queue
        forever because the provider echo can never arrive from a dead
        transport (WIKI-232).
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM steer_effects
                WHERE status = 'sending'
                ORDER BY updated_at
                """
            ).fetchall()
        return [self._decode(row) for row in rows]
