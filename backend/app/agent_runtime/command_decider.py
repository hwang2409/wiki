"""Pure command decisions and registry-shaped projection updates."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .command_models import AgentCommand, CommandConflict, CommandError, EventSpec


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
    """Decide command events without provider or storage side effects."""

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


def apply_projection(
    state: dict[str, Any],
    events: tuple[EventSpec, ...],
) -> dict[str, Any]:
    """Apply events to the command projection without touching SQLite."""

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
