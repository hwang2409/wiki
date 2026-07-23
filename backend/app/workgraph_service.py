"""Workgraph telemetry for canonical agent actions (graph-engineering D2.3/2.6).

Every successful canonical action — spawn, steer, archive — routes through
this module, whether it arrived via the ``wiki agent`` CLI, the MCP agent
tools, or the app UI (they all converge on the backend endpoints). Node IDs
are stable and derived from agent identity: ``orch:<orch-id>`` for the acting
orchestrator, the agent id itself for workers.

Agent operations are primary; the graph is telemetry. A failed append is
logged and swallowed — it must never fail the underlying action.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from uuid import uuid4

from wiki_cli import graph_lint

from . import workgraph

log = logging.getLogger("wiki.workgraph")

# Worker agent ids extend the base ticket with a role suffix
# (WIKI-163-REVIEW1, PHO-14060-PLAN2, ...); the workgraph lives on the base
# ticket so all of a ticket's agents share one graph.
_ROLE_SUFFIX = re.compile(r"-(?:REVIEW|PLAN|SIM)\d*$", re.IGNORECASE)

# Fallback orchestrator identity for actions taken directly by Henry in the
# app UI, where no orchestrator id accompanies the request.
DEFAULT_ACTOR = "henry"


def base_ticket(agent_id: str) -> str:
    return _ROLE_SUFFIX.sub("", agent_id)


def orch_node_id(orch: str) -> str:
    return f"orch:{orch}"


def _record(
    ticket: str,
    edge_kind: str,
    from_node: str,
    to_node: str,
    payload: dict,
    orch: str,
    status_dir: Path | None,
) -> None:
    try:
        workgraph.append_edge(
            ticket,
            edge_kind,
            from_node,
            to_node,
            payload,
            orch=orch,
            status_dir=status_dir,
        )
    except Exception:
        log.exception(
            "workgraph %s append failed for %s (the agent action itself succeeded)",
            edge_kind,
            ticket,
        )


def record_spawn(
    *,
    agent_id: str,
    orch: str | None,
    role: str,
    model: str,
    effort: str | None,
    worktree: str,
    request_id: str | None,
    status_dir: Path | None = None,
) -> None:
    actor = orch or DEFAULT_ACTOR
    payload = {
        "ticket": agent_id,
        "role": role,
        "model": model,
        "worktree": worktree,
        "request_id": request_id or str(uuid4()),
    }
    if effort:
        payload["effort"] = effort
    _record(
        base_ticket(agent_id),
        "spawn",
        orch_node_id(actor),
        agent_id,
        payload,
        actor,
        status_dir,
    )


def record_steer(
    *,
    agent_id: str,
    orch: str | None,
    mode: str,
    text: str,
    source: str | None,
    request_id: str | None,
    status_dir: Path | None = None,
) -> None:
    actor = orch or DEFAULT_ACTOR
    payload = graph_lint.compose_steer_document(
        agent_id,
        mode,
        text,
        source_worker=source or DEFAULT_ACTOR,
        request_id=request_id or str(uuid4()),
    )
    _record(
        base_ticket(agent_id),
        "steer",
        orch_node_id(actor),
        agent_id,
        payload,
        actor,
        status_dir,
    )


def record_archive(
    *,
    agent_id: str,
    orch: str | None,
    outcome: str | None,
    status_dir: Path | None = None,
) -> None:
    actor = orch or DEFAULT_ACTOR
    payload = {"outcome": outcome or "archived", "ended_at": workgraph.now_iso()}
    _record(
        base_ticket(agent_id),
        "archive",
        orch_node_id(actor),
        agent_id,
        payload,
        actor,
        status_dir,
    )
