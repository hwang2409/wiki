"""Workgraph telemetry for canonical agent actions (graph-engineering D2.3/2.6).

Every successful canonical action — spawn, steer, archive — routes through
this module, whether it arrived via the ``wiki agent`` CLI, the MCP agent
tools, or the app UI (they all converge on the backend endpoints). Node IDs
are stable and derived from agent identity: ``orch:<orch-id>`` for the acting
orchestrator, the agent id itself for workers.

Agent operations are primary; the graph is telemetry. Appends are delivered
through a bounded keyed outbox so a blocked filesystem write can never stall
an already-successful agent action response: ``record_*`` enqueue and return
immediately, delivery keeps exactly one append in flight per ticket (per-
ticket order preserved) while distinct tickets deliver concurrently, and
failures — append errors or a full outbox — are logged and swallowed. The
FastAPI lifespan owns the outbox lifecycle: shutdown drains with a bound and
logs every undelivered item. The CLI path (``wiki graph append``) stays
synchronous; only the backend service path is asynchronous.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import Future
from collections import deque
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
    """Bounded keyed delivery of graph appends off the request path.

    Appends are keyed by ticket: exactly one delivery is in flight per ticket
    (a per-ticket drain thread preserves order), while distinct tickets
    deliver concurrently — one ticket's held append lock can never stall the
    whole fleet's telemetry or force unrelated edges out of the bounded
    buffer. ``submit`` never blocks: once ``max_pending`` undelivered items
    accumulate, further edges are dropped and the loss logged (agent
    operations are primary; the graph is telemetry). The FastAPI lifespan
    owns ``start``/``stop``; ``stop`` drains with a bound and logs every
    undelivered item so a backend restart can never silently discard
    accepted writes.
    """

    def __init__(self, max_pending: int = 256) -> None:
        self._cond = threading.Condition()
        self._queues: dict[
            str, deque[tuple[str, Callable[[], None], Future[None] | None]]
        ] = {}
        self._active: set[str] = set()  # tickets with a live drain thread
        self._delivering: dict[str, str] = {}  # ticket -> edge kind in flight
        self._pending = 0
        self._max_pending = max_pending
        self._closed = False

    def start(self) -> None:
        with self._cond:
            self._closed = False

    def submit(
        self,
        ticket: str,
        edge_kind: str,
        deliver: Callable[[], None],
        *,
        ack: Future[None] | None = None,
    ) -> bool:
        with self._cond:
            if self._closed:
                log.error(
                    "workgraph outbox stopped; dropping %s edge for %s "
                    "(the agent action itself succeeded)",
                    edge_kind,
                    ticket,
                )
                if ack is not None:
                    ack.set_exception(RuntimeError("workgraph outbox is stopped"))
                return False
            if self._pending >= self._max_pending:
                log.error(
                    "workgraph outbox full; dropping %s edge for %s "
                    "(the agent action itself succeeded)",
                    edge_kind,
                    ticket,
                )
                if ack is not None:
                    ack.set_exception(RuntimeError("workgraph outbox is full"))
                return False
            self._queues.setdefault(ticket, deque()).append((edge_kind, deliver, ack))
            self._pending += 1
            if ticket not in self._active:
                self._active.add(ticket)
                threading.Thread(
                    target=self._drain_ticket,
                    args=(ticket,),
                    name=f"workgraph-outbox-{ticket}",
                    daemon=True,
                ).start()
        return True

    def _drain_ticket(self, ticket: str) -> None:
        while True:
            with self._cond:
                ticket_queue = self._queues.get(ticket)
                if not ticket_queue:
                    self._queues.pop(ticket, None)
                    self._active.discard(ticket)
                    self._cond.notify_all()
                    return
                edge_kind, deliver, ack = ticket_queue.popleft()
                self._delivering[ticket] = edge_kind
            try:
                deliver()
                if ack is not None:
                    ack.set_result(None)
            except Exception as exc:
                if ack is not None:
                    ack.set_exception(exc)
                log.exception(
                    "workgraph %s append failed for %s (the agent action itself succeeded)",
                    edge_kind,
                    ticket,
                )
            finally:
                with self._cond:
                    del self._delivering[ticket]
                    self._pending -= 1
                    self._cond.notify_all()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every submitted edge has been delivered."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout)

    def stop(self, timeout: float = 5.0) -> bool:
        """Refuse new work, drain with a bound, log anything undelivered."""
        with self._cond:
            self._closed = True
        if self.flush(timeout):
            return True
        with self._cond:
            for ticket, ticket_queue in self._queues.items():
                for edge_kind, _deliver, ack in ticket_queue:
                    log.error(
                        "workgraph outbox shutdown: undelivered %s edge for %s",
                        edge_kind,
                        ticket,
                    )
                    if ack is not None:
                        ack.set_exception(RuntimeError("workgraph outbox stopped"))
            for ticket, edge_kind in self._delivering.items():
                log.error(
                    "workgraph outbox shutdown: %s edge for %s still delivering "
                    "at timeout",
                    edge_kind,
                    ticket,
                )
            # Drop queued work so drain threads exit; in-flight deliveries are
            # daemon threads and die with the process.
            self._pending -= sum(len(q) for q in self._queues.values())
            self._queues.clear()
            self._cond.notify_all()
        return False


OUTBOX = _TelemetryOutbox()


def start_outbox() -> None:
    OUTBOX.start()


def stop_outbox(timeout: float = 5.0) -> bool:
    return OUTBOX.stop(timeout)


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
    now_ts: float | None = None,
    wait_for_delivery: bool = False,
) -> Future[None] | None:
    ack: Future[None] | None = Future() if wait_for_delivery else None

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
            now_ts=now_ts,
        )

    OUTBOX.submit(ticket, edge_kind, deliver, ack=ack)
    return ack


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


def record_escalation(
    *,
    ticket: str,
    orch: str | None,
    reason: str,
    prior_findings: list[dict],
    target: str,
    request_id: str | None = None,
    status_dir: Path | None = None,
    now_ts: float | None = None,
    wait_for_delivery: bool = False,
) -> Future[None] | None:
    """Queue a typed monitor escalation through the canonical graph writer."""

    actor = orch or DEFAULT_ACTOR
    return _record(
        base_ticket(ticket),
        "escalation",
        "monitor:fleet",
        orch_node_id(actor),
        {
            "reason": reason,
            "prior_findings": prior_findings,
            "target": target,
        },
        actor,
        status_dir,
        request_id=request_id,
        now_ts=now_ts,
        wait_for_delivery=wait_for_delivery,
    )
