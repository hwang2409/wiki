"""Keyed SQLite storage for the command read model."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any


class ProjectionStore:
    """Own only the durable per-agent command projection."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        encode: Callable[[Any], str],
    ) -> None:
        self._connect = connect
        self._encode = encode

    @staticmethod
    def read_in_transaction(
        connection: sqlite3.Connection,
        agent_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT state_json FROM command_projection WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            return {}
        return {agent_id: json.loads(row["state_json"])}

    @staticmethod
    def write_in_transaction(
        connection: sqlite3.Connection,
        agent_id: str,
        state: Mapping[str, Any],
        sequence: int,
        encode: Callable[[Any], str],
    ) -> None:
        ProjectionStore._write_agent(connection, agent_id, state, sequence, encode)

    @staticmethod
    def _write_agent(
        connection: sqlite3.Connection,
        agent_id: str,
        state: Mapping[str, Any],
        sequence: int,
        encode: Callable[[Any], str],
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
            (agent_id, encode(value), sequence),
        )

    def read(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT agent_id, state_json FROM command_projection ORDER BY agent_id"
            ).fetchall()
        return {str(row["agent_id"]): json.loads(row["state_json"]) for row in rows}

    def read_for(self, agent_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM command_projection WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return {}
        return {agent_id: json.loads(row["state_json"])}

    def seed(self, agent_id: str, state: Mapping[str, Any]) -> None:
        value = state.get(agent_id)
        if value is None:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO command_projection(agent_id, state_json, sequence)
                VALUES (?, ?, 0)
                ON CONFLICT(agent_id) DO NOTHING
                """,
                (agent_id, self._encode(value)),
            )
            connection.commit()

    def replace(self, agent_id: str, state: Mapping[str, Any]) -> None:
        with self._connect() as connection:
            self._write_agent(connection, agent_id, state, 0, self._encode)
            connection.commit()
