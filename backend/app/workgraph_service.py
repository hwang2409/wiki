"""Workgraph telemetry for canonical agent actions (graph-engineering D2.3/2.6).

Every successful canonical action — spawn, steer, archive — routes through
this module, whether it arrived via the ``wiki agent`` CLI, the MCP agent
tools, or the app UI (they all converge on the backend endpoints). Node IDs
are stable and derived from agent identity: ``orch:<orch-id>`` for the acting
orchestrator, the agent id itself for workers.

Agent operations are primary; the graph is telemetry. Most appends are
delivered through a bounded keyed outbox. Supervisor archive edges use the
synchronous path and recover from committed archive sessions after restart.
The FastAPI lifespan owns the outbox lifecycle. The CLI path (``wiki graph
append``) stays synchronous.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import Future
from collections import deque
from pathlib import Path
from typing import Callable
from uuid import uuid4

from wiki_cli import graph_lint

from .agent_runtime.archive_protocol import archive_is_committed
from .agent_runtime.ticket import base_ticket
from . import workgraph

log = logging.getLogger("wiki.workgraph")

# Fallback orchestrator identity for actions taken directly by Henry in the
# app UI, where no orchestrator id accompanies the request.
DEFAULT_ACTOR = "henry"


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
    findings: list[dict] | None = None,
    constraint_bundle: str | None = None,
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
        findings=findings,
        constraint_bundle=constraint_bundle,
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


def record_verdict(
    *,
    ticket: str,
    reviewer: str,
    orch: str | None,
    payload: dict,
    request_id: str,
    status_dir: Path | None = None,
    wait_for_delivery: bool = False,
) -> Future[None] | None:
    """Persist a parsed reviewer verdict before autopilot acts on it."""

    actor = orch or DEFAULT_ACTOR
    return _record(
        base_ticket(ticket),
        "verdict",
        reviewer,
        orch_node_id(actor),
        payload,
        actor,
        status_dir,
        request_id=request_id,
        wait_for_delivery=wait_for_delivery,
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


def record_archive_sync(
    *,
    agent_id: str,
    orch: str | None,
    outcome: str | None,
    run_id: str,
    ended_at: str | None = None,
    status_dir: Path | None = None,
) -> dict:
    """Append an archive edge before the supervisor reports archive success.

    This path does not use the FastAPI-owned process-local outbox. The
    committed archive is also a durable recovery source for interrupted edge
    writes.
    """

    actor = orch or DEFAULT_ACTOR
    payload = {
        "outcome": outcome or "archived",
        "ended_at": ended_at or workgraph.now_iso(),
    }
    return workgraph.append_edge(
        base_ticket(agent_id),
        "archive",
        orch_node_id(actor),
        agent_id,
        payload,
        orch=actor,
        status_dir=status_dir,
        request_id=f"archive:{run_id}",
    )


def reconcile_archive_edges(
    archive_dir: Path,
    *,
    status_dir: Path | None = None,
) -> int:
    """Replay archive edges missing after a supervisor exit.

    Archive sessions are committed before this process can lose an edge
    write. Replaying by run id is safe because workgraph deduplicates it.
    """

    repaired = 0
    for session_dir in sorted(archive_dir.glob("*/*")):
        if not archive_is_committed(session_dir):
            continue
        try:
            run = json.loads((session_dir / "run.json").read_text(encoding="utf-8"))
            meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(run, dict) or not isinstance(meta, dict):
            continue
        role = run.get("role")
        agent_id = run.get("agent_id")
        run_id = run.get("run_id")
        if role not in {"plan", "implement", "review"}:
            continue
        if not all(isinstance(value, str) and value for value in (agent_id, run_id)):
            continue
        try:
            record_archive_sync(
                agent_id=agent_id,
                orch=run.get("orchestrator_id")
                if isinstance(run.get("orchestrator_id"), str)
                else None,
                outcome=run.get("outcome")
                if isinstance(run.get("outcome"), str)
                else None,
                run_id=run_id,
                ended_at=meta.get("ended_at")
                if isinstance(meta.get("ended_at"), str)
                else None,
                status_dir=status_dir,
            )
        except Exception:
            log.exception("could not reconcile archive workgraph edge for %s", agent_id)
        else:
            repaired += 1
    return repaired


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
