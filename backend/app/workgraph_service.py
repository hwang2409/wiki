"""Workgraph telemetry for canonical agent actions (graph-engineering D2.3/2.6).

Every successful canonical action — spawn, steer, archive — routes through
this module, whether it arrived via the ``wiki agent`` CLI, the MCP agent
tools, or the app UI (they all converge on the backend endpoints). Node IDs
are stable and derived from agent identity: ``orch:<orch-id>`` for the acting
orchestrator, the agent id itself for workers.

Agent operations are primary; the graph is telemetry. Appends are delivered
through a bounded single-worker outbox so a blocked filesystem write can never
stall an already-successful agent action response: ``record_*`` enqueue and
return immediately, delivery preserves per-ticket order (one global FIFO
worker), and failures — append errors or a full outbox — are logged and
swallowed. The CLI path (``wiki graph append``) stays synchronous; only the
backend service path is asynchronous.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from pathlib import Path
from typing import Callable
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


class _TelemetryOutbox:
    """Bounded, serialized delivery of graph appends off the request path.

    One daemon worker drains a global FIFO, which preserves per-ticket order
    by construction. ``submit`` never blocks: a full queue drops the edge and
    logs the loss (agent operations are primary; the graph is telemetry).
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._queue: queue.Queue[tuple[str, str, Callable[[], None]]] = queue.Queue(
            maxsize=maxsize
        )
        self._idle = threading.Condition()
        self._pending = 0
        self._start_lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def _ensure_worker(self) -> None:
        with self._start_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._drain, name="workgraph-outbox", daemon=True
                )
                self._worker.start()

    def submit(self, ticket: str, edge_kind: str, deliver: Callable[[], None]) -> bool:
        self._ensure_worker()
        with self._idle:
            self._pending += 1
        try:
            self._queue.put_nowait((ticket, edge_kind, deliver))
        except queue.Full:
            with self._idle:
                self._pending -= 1
                self._idle.notify_all()
            log.error(
                "workgraph outbox full; dropping %s edge for %s "
                "(the agent action itself succeeded)",
                edge_kind,
                ticket,
            )
            return False
        return True

    def _drain(self) -> None:
        while True:
            ticket, edge_kind, deliver = self._queue.get()
            try:
                deliver()
            except Exception:
                log.exception(
                    "workgraph %s append failed for %s (the agent action itself succeeded)",
                    edge_kind,
                    ticket,
                )
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every submitted edge has been delivered (tests only)."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)


OUTBOX = _TelemetryOutbox()


def flush_outbox(timeout: float = 5.0) -> bool:
    return OUTBOX.flush(timeout)


def _record(
    ticket: str,
    edge_kind: str,
    from_node: str,
    to_node: str,
    payload: dict,
    orch: str,
    status_dir: Path | None,
    request_id: str | None = None,
) -> None:
    def deliver() -> None:
        workgraph.append_edge(
            ticket,
            edge_kind,
            from_node,
            to_node,
            payload,
            orch=orch,
            status_dir=status_dir,
            request_id=request_id,
        )

    OUTBOX.submit(ticket, edge_kind, deliver)


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
    operation_id = request_id or str(uuid4())
    payload = {
        "ticket": agent_id,
        "role": role,
        "model": model,
        "worktree": worktree,
        "request_id": operation_id,
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
        request_id=operation_id,
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
    # The supervisor's exact request id rides on the edge itself: replay
    # dedupe must not depend on digest prefixes derived from the body text.
    operation_id = request_id or str(uuid4())
    payload = graph_lint.compose_steer_document(
        agent_id,
        mode,
        text,
        source_worker=source or DEFAULT_ACTOR,
        request_id=operation_id,
    )
    _record(
        base_ticket(agent_id),
        "steer",
        orch_node_id(actor),
        agent_id,
        payload,
        actor,
        status_dir,
        request_id=operation_id,
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
