from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

from . import backend_runtime


class AgentToolError(RuntimeError):
    pass


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_agents",
        "description": (
            "List Wiki supervisor workers and orchestrators with durable runtime/status "
            "state. Use this instead of inspecting tmux, process tables, or registry files."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    {
        "name": "read_agent",
        "description": (
            "Read one agent snapshot, including run id, lifecycle state, status-file "
            "state, PR, current step, blocker, provider identity, and worktree."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {"id": {"type": "string", "minLength": 1}},
        },
    },
    {
        "name": "read_agent_events",
        "description": (
            "Read durable normalized supervisor/provider events for gate diagnosis and "
            "progress inspection. Cursors are normalized event sequence numbers."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "after_seq": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                "include_raw": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "read_agent_pr",
        "description": (
            "Read the Wiki GitHub PR snapshot for a ticket. Use `wiki gate` for the "
            "authoritative merge-ready check including checks and review threads."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {"id": {"type": "string", "minLength": 1}},
        },
    },
    {
        "name": "spawn_agent",
        "description": (
            "Spawn one supervisor-owned worker. Always pass this orchestrator's id in "
            "orch. request_id is generated when omitted; reuse an explicit value on retry."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ticket", "kind", "role", "model", "workdir", "prompt", "orch"],
            "properties": {
                "ticket": {"type": "string", "minLength": 1},
                "kind": {"enum": ["cc", "cdx"]},
                "role": {"enum": ["plan", "implement", "review"]},
                "model": {"type": "string", "minLength": 1},
                "effort": {"enum": ["minimal", "low", "medium", "high", "xhigh"]},
                "workdir": {"type": "string", "minLength": 1},
                "prompt": {"type": "string", "minLength": 1},
                "orch": {"type": "string", "minLength": 1},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
            },
        },
    },
    {
        "name": "steer_agent",
        "description": (
            "Send a supervisor-native message now or once the run is idle. request_id "
            "is generated when omitted; reuse an explicit value on retry. The turn is "
            "tagged as a synthetic supervisor-steer source so the receiving session "
            "renders it as a system marker rather than a Henry-authored message; pass "
            "an explicit source (e.g. 'mastermind') to override that label."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "message"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "message": {"type": "string", "minLength": 1, "maxLength": 4000},
                "mode": {"enum": ["now", "on-idle"], "default": "now"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "source": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[A-Za-z0-9_.:-]+$",
                    "default": "supervisor-steer",
                },
            },
        },
    },
    {
        "name": "replace_agent",
        "description": "Replace a supervisor-owned worker or orchestrator without using tmux.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "kind": {"enum": ["cc", "cdx"]},
                "model": {"type": "string", "minLength": 1},
                "effort": {"enum": ["minimal", "low", "medium", "high", "xhigh"]},
            },
        },
    },
    {
        "name": "archive_agent",
        "description": (
            "Archive a terminal run with its outcome and release its provider process. "
            "Call only after the protocol's gate/wrap-up conditions are satisfied."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "outcome"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "outcome": {"enum": ["merged", "closed", "abandoned"]},
            },
        },
    },
    {
        "name": "next_review",
        "description": (
            "Run the merge-ready gate and start the next pinned PR reviewer in one "
            "idempotent operation. The reviewer stays grouped under this orchestrator."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ticket", "pr_number", "expected_sha"],
            "properties": {
                "ticket": {"type": "string", "minLength": 1},
                "pr_number": {"type": "integer", "minimum": 1},
                "expected_sha": {"type": "string", "minLength": 1, "maxLength": 64},
                "reviewer_kind": {"enum": ["cc", "cdx"], "default": "cdx"},
                "reviewer_model": {"type": "string", "minLength": 1, "default": "gpt-5.6-sol"},
                "reviewer_effort": {
                    "enum": ["minimal", "low", "medium", "high", "xhigh"],
                    "default": "high",
                },
                "prompt_template": {"type": "string", "maxLength": 100000},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
            },
        },
    },
]


def _arguments(
    arguments: Any,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> dict[str, Any]:
    if os.environ.get("WIKI_AGENT_ROLE") != "orchestrator":
        raise AgentToolError("agent operations require an orchestrator runtime")
    if not isinstance(arguments, dict):
        raise AgentToolError("tool input must be an object")
    missing = required - arguments.keys()
    if missing:
        raise AgentToolError(f"missing required field: {sorted(missing)[0]}")
    extra = arguments.keys() - required - optional
    if extra:
        raise AgentToolError(f"unknown field: {sorted(extra)[0]}")
    return dict(arguments)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AgentToolError(f"{field} must be a non-empty string")
    return value


def _backend_api(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    configured = os.environ.get("WIKI_BACKEND_URL")
    if not configured:
        raise AgentToolError("WIKI_BACKEND_URL is missing from the runtime")
    try:
        return backend_runtime.request_json(
            configured,
            method,
            path,
            payload,
            timeout=15,
        )
    except (backend_runtime.BackendRequestError, ValueError) as exc:
        raise AgentToolError(f"Wiki backend request failed: {exc}") from exc


def list_agents(arguments: Any) -> dict[str, Any]:
    _arguments(arguments, required=set())
    return _backend_api("GET", "/api/agents")


def read_agent(arguments: Any) -> dict[str, Any]:
    values = _arguments(arguments, required={"id"})
    agent_id = _string(values["id"], "id")
    payload = _backend_api("GET", "/api/agents")
    wanted = agent_id.upper()
    for kind, key, rows in (
        ("worker", "ticket", payload.get("workers")),
        ("orchestrator", "id", payload.get("orchestrators")),
    ):
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            found = row.get(key)
            if isinstance(found, str) and found.upper() == wanted:
                return {**row, "type": kind}
    raise AgentToolError(f"agent {agent_id} was not found")


def read_agent_events(arguments: Any) -> dict[str, Any]:
    values = _arguments(
        arguments,
        required={"id"},
        optional={"after_seq", "limit", "include_raw"},
    )
    agent_id = _string(values.pop("id"), "id")
    query = urlencode(
        {
            "after_seq": values.get("after_seq", 0),
            "limit": values.get("limit", 200),
            "include_raw": str(values.get("include_raw", False)).lower(),
        }
    )
    return _backend_api(
        "GET",
        f"/api/agents/{quote(agent_id, safe='')}/events?{query}",
    )


def read_agent_pr(arguments: Any) -> dict[str, Any]:
    values = _arguments(arguments, required={"id"})
    agent_id = _string(values["id"], "id")
    return _backend_api("GET", f"/api/agents/{quote(agent_id, safe='')}/pr")


def spawn_agent(arguments: Any) -> dict[str, Any]:
    values = _arguments(
        arguments,
        required={"ticket", "kind", "role", "model", "workdir", "prompt", "orch"},
        optional={"effort", "request_id"},
    )
    values.setdefault("effort", None)
    values.setdefault("request_id", str(uuid4()))
    return _backend_api("POST", "/api/agents/spawn", values)


def steer_agent(arguments: Any) -> dict[str, Any]:
    from wiki_cli import graph_lint

    values = _arguments(
        arguments,
        required={"id", "message"},
        optional={"mode", "request_id", "source"},
    )
    agent_id = _string(values.pop("id"), "id")
    message = _string(values.pop("message"), "message")
    source = values.get("source")
    if source is not None and not isinstance(source, str):
        raise AgentToolError("source must be a string")
    mode = values.get("mode", "now")
    request_id = values.get("request_id") or str(uuid4())
    typed_steer = graph_lint.compose_steer_document(
        agent_id,
        mode,
        message,
        source_worker=source or "supervisor-steer",
        request_id=request_id,
    )
    try:
        violations = graph_lint.validate_document(typed_steer, "steer")
    except graph_lint.GraphLintError as exc:
        raise AgentToolError(f"could not validate Steer: {exc}") from exc
    if violations:
        formatted = "; ".join(f"{pointer}: {error}" for pointer, error in violations)
        raise AgentToolError(f"invalid Steer: {formatted}")
    payload: dict[str, Any] = {
        "text": message,
        "mode": mode,
        "request_id": request_id,
        # Orchestrator-initiated steers are system messages, not Henry — the
        # frontend renders them as marker rows rather than user bubbles.
        "source": source or "supervisor-steer",
    }
    return _backend_api(
        "POST",
        f"/api/agents/{quote(agent_id, safe='')}/message",
        payload,
    )


def replace_agent(arguments: Any) -> dict[str, Any]:
    values = _arguments(
        arguments,
        required={"id"},
        optional={"kind", "model", "effort"},
    )
    agent_id = _string(values.pop("id"), "id")
    return _backend_api(
        "POST",
        f"/api/agents/{quote(agent_id, safe='')}/replace",
        values,
    )


def archive_agent(arguments: Any) -> dict[str, Any]:
    values = _arguments(arguments, required={"id", "outcome"})
    agent_id = _string(values.pop("id"), "id")
    return _backend_api(
        "POST",
        f"/api/agents/{quote(agent_id, safe='')}/archive",
        values,
    )


def next_review(arguments: Any) -> dict[str, Any]:
    values = _arguments(
        arguments,
        required={"ticket", "pr_number", "expected_sha"},
        optional={
            "reviewer_kind",
            "reviewer_model",
            "reviewer_effort",
            "prompt_template",
            "request_id",
        },
    )
    values.setdefault("reviewer_kind", "cdx")
    values.setdefault("reviewer_model", "gpt-5.6-sol")
    values.setdefault("reviewer_effort", "high")
    values.setdefault("request_id", str(uuid4()))
    orch = os.environ.get("WIKI_AGENT_ID")
    if not orch:
        raise AgentToolError("WIKI_AGENT_ID is missing from the orchestrator runtime")
    values["orch"] = orch
    return _backend_api("POST", "/api/agents/next-review", values)


TOOL_HANDLERS = {
    "list_agents": list_agents,
    "read_agent": read_agent,
    "read_agent_events": read_agent_events,
    "read_agent_pr": read_agent_pr,
    "spawn_agent": spawn_agent,
    "steer_agent": steer_agent,
    "replace_agent": replace_agent,
    "archive_agent": archive_agent,
    "next_review": next_review,
}


def tool_result(request_id: Any, name: str, arguments: Any) -> dict[str, Any]:
    try:
        payload = TOOL_HANDLERS[name](arguments)
    except (AgentToolError, TypeError, ValueError) as exc:
        result = {
            "content": [{"type": "text", "text": f"Wiki runtime operation failed: {exc}"}],
            "isError": True,
        }
    else:
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            "structuredContent": payload,
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
