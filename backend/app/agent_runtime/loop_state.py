"""Ticket-level merge-ready loop state derived from workgraph edges.

Chrome on the wiki-app agent view surfaces the review-round cadence:

- ``round`` — count of review workers spawned so far.
- ``cap`` — iteration cap from the graph, its template, or the module default.
- ``danger`` — ``normal`` while ``round < 5``, ``warning`` at 5-6, ``danger``
  at 7-8 (matches the spec-cap tiers in the WIKI-172 kickoff).
- ``unrouted_verdict_count`` — non-MERGE-READY verdicts whose reviewer has no
  subsequent steer routing their findings back to the implementer.
- ``plateau_length`` — trailing verdicts sharing the same first-finding
  signature (dedupe by title, then first line of the observed body).
- ``latest_verdict`` — a small dict describing the last verdict edge: state,
  reviewer worker id, findings signature, routed-at (ISO string) if any, and
  the top blocking / highest-severity finding title.
- ``history`` — one entry per round in commit order, each pairing the spawn
  event with its verdict, routed-at timestamp, and archive outcome. Feeds the
  ticket-view "history" expander so review activity is inspectable without
  re-loading the whole graph.

The chrome consumes this alongside :func:`~backend.app.workgraph.current_health`
via the existing ``/api/agents/{ticket}/workgraph`` endpoint — no new load.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .graph_health import iteration_cap_for


DANGER_NORMAL = "normal"
DANGER_WARNING = "warning"
DANGER_DANGER = "danger"

# Kickoff spec: subdued N<4, warning N=5..6, danger N=7..8. N==4 rides with
# normal (still in the healthy band, next spawn tips into warning).
_WARNING_ROUND = 5
_DANGER_ROUND = 7


@dataclass(frozen=True)
class LoopHistoryEntry:
    """One review-round entry paired to the ticket-view history expander."""

    round: int
    reviewer: str | None
    spawned_at: str | None
    verdict_state: str | None
    verdict_at: str | None
    routed_at: str | None
    archived_at: str | None
    top_finding: str | None
    finding_signature: str | None


@dataclass(frozen=True)
class LoopState:
    """Merge-ready loop state for one ticket."""

    round: int
    cap: int
    danger: str
    unrouted_verdict_count: int
    plateau_length: int
    latest_verdict: dict[str, Any] | None
    history: list[LoopHistoryEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "cap": self.cap,
            "danger": self.danger,
            "unrouted_verdict_count": self.unrouted_verdict_count,
            "plateau_length": self.plateau_length,
            "latest_verdict": self.latest_verdict,
            "history": [entry.__dict__ for entry in self.history],
        }


def _edges(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]


def _nodes_by_id(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        node["id"]: node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }


def _is_review_spawn(edge: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> bool:
    if edge.get("kind") != "spawn":
        return False
    payload = edge.get("payload")
    role = payload.get("role") if isinstance(payload, dict) else None
    if role == "review":
        return True
    # Belt-and-braces: some spawn edges are recorded without payload.role;
    # fall back to the inferred node kind (spec 2.3 sets node.kind="review"
    # from verdict edges, and workers explicitly created with kind=review).
    target = nodes.get(str(edge.get("to")))
    if isinstance(target, dict) and target.get("kind") == "review":
        return True
    return False


def _finding_signature(finding: dict[str, Any]) -> str:
    """Signature used to detect plateaus — dedupe by title then observed head."""

    title = finding.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip().casefold()
    observed = finding.get("observed")
    if isinstance(observed, str):
        first = observed.strip().splitlines()[0] if observed.strip() else ""
        if first:
            return first.strip().casefold()
    fid = finding.get("id")
    return str(fid) if isinstance(fid, str) else ""


def _verdict_findings(edge: dict[str, Any]) -> list[dict[str, Any]]:
    payload = edge.get("payload")
    if not isinstance(payload, dict):
        return []
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return []
    return [finding for finding in findings if isinstance(finding, dict)]


def _top_finding(findings: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer the first BLOCKING finding, fall back to the first entry."""

    ordered = list(findings)
    for finding in ordered:
        if finding.get("severity") == "BLOCKING":
            return finding
    return ordered[0] if ordered else None


def _verdict_signature(edge: dict[str, Any]) -> str | None:
    """Signature used for plateau detection on the trailing verdict streak."""

    top = _top_finding(_verdict_findings(edge))
    if top is None:
        return None
    return _finding_signature(top) or None


def _plateau_length(verdicts: list[dict[str, Any]]) -> int:
    if not verdicts:
        return 0
    latest_sig = _verdict_signature(verdicts[-1])
    if not latest_sig:
        return 1 if verdicts else 0
    length = 1
    for edge in reversed(verdicts[:-1]):
        if _verdict_signature(edge) == latest_sig:
            length += 1
        else:
            break
    return length


def _danger_tier(round_: int) -> str:
    if round_ >= _DANGER_ROUND:
        return DANGER_DANGER
    if round_ >= _WARNING_ROUND:
        return DANGER_WARNING
    return DANGER_NORMAL


def _route_time_for(
    verdict_edge: dict[str, Any],
    later_edges: list[dict[str, Any]],
) -> str | None:
    """Return the routed-at of the earliest steer that carries this verdict."""

    reviewer = None
    payload = verdict_edge.get("payload")
    if isinstance(payload, dict):
        candidate = payload.get("worker")
        if isinstance(candidate, str):
            reviewer = candidate
    if reviewer is None:
        reviewer = verdict_edge.get("from") if isinstance(verdict_edge.get("from"), str) else None
    if not reviewer:
        return None
    for edge in later_edges:
        if edge.get("kind") != "steer":
            continue
        steer_payload = edge.get("payload")
        if not isinstance(steer_payload, dict):
            continue
        if steer_payload.get("source_worker") != reviewer:
            continue
        created = edge.get("created_at")
        if isinstance(created, str):
            return created
        return None
    return None


def _archive_time_for(
    reviewer: str | None,
    later_edges: list[dict[str, Any]],
) -> str | None:
    if not reviewer:
        return None
    for edge in later_edges:
        if edge.get("kind") != "archive":
            continue
        if edge.get("to") != reviewer:
            continue
        created = edge.get("created_at")
        return created if isinstance(created, str) else None
    return None


def derive_loop_state(
    graph: dict[str, Any],
    *,
    iteration_cap: int | None = None,
) -> LoopState:
    """Compute :class:`LoopState` from an already-loaded workgraph.

    ``iteration_cap`` overrides the graph/template cap (kept as a hook for the
    endpoint layer; production callers pass ``None`` and inherit the cap the
    monitor uses).
    """

    edges = _edges(graph)
    nodes = _nodes_by_id(graph)
    cap = iteration_cap if iteration_cap and iteration_cap > 0 else iteration_cap_for(graph)

    spawn_events: list[tuple[int, dict[str, Any]]] = [
        (index, edge)
        for index, edge in enumerate(edges)
        if _is_review_spawn(edge, nodes)
    ]
    verdict_edges = [edge for edge in edges if edge.get("kind") == "verdict"]

    unrouted = 0
    for index, edge in enumerate(edges):
        if edge.get("kind") != "verdict":
            continue
        payload = edge.get("payload") if isinstance(edge.get("payload"), dict) else {}
        state = payload.get("state")
        if state == "MERGE-READY":
            continue
        later = edges[index + 1 :]
        if _route_time_for(edge, later) is None:
            unrouted += 1

    plateau_length = _plateau_length(verdict_edges)

    latest_verdict_payload: dict[str, Any] | None = None
    if verdict_edges:
        last = verdict_edges[-1]
        last_index = None
        for index, edge in enumerate(edges):
            if edge is last:
                last_index = index
                break
        later = edges[last_index + 1 :] if last_index is not None else []
        top = _top_finding(_verdict_findings(last))
        payload = last.get("payload") if isinstance(last.get("payload"), dict) else {}
        latest_verdict_payload = {
            "state": payload.get("state"),
            "reviewer": payload.get("worker") or last.get("from"),
            "created_at": last.get("created_at"),
            "routed_at": _route_time_for(last, later),
            "top_finding": {
                "id": top.get("id") if top else None,
                "title": top.get("title") if top else None,
                "severity": top.get("severity") if top else None,
            }
            if top
            else None,
            "signature": _verdict_signature(last),
            "findings_count": len(_verdict_findings(last)),
        }

    history: list[LoopHistoryEntry] = []
    for round_number, (spawn_index, spawn_edge) in enumerate(spawn_events, start=1):
        reviewer = str(spawn_edge.get("to")) if spawn_edge.get("to") else None
        # First verdict at or after the spawn whose reviewer matches; if the
        # workgraph didn't label verdict.payload.worker, fall back to the
        # from-node reference.
        matched_verdict: dict[str, Any] | None = None
        matched_index: int | None = None
        for edge_index, edge in enumerate(edges[spawn_index + 1 :], start=spawn_index + 1):
            if edge.get("kind") != "verdict":
                continue
            payload = edge.get("payload") if isinstance(edge.get("payload"), dict) else {}
            worker = payload.get("worker") or edge.get("from")
            if worker == reviewer:
                matched_verdict = edge
                matched_index = edge_index
                break
        verdict_state = None
        verdict_at = None
        routed_at = None
        top_finding_title = None
        signature = None
        if matched_verdict is not None:
            verdict_payload = (
                matched_verdict.get("payload")
                if isinstance(matched_verdict.get("payload"), dict)
                else {}
            )
            verdict_state = verdict_payload.get("state")
            verdict_at = matched_verdict.get("created_at")
            later = edges[(matched_index or 0) + 1 :]
            routed_at = _route_time_for(matched_verdict, later)
            top = _top_finding(_verdict_findings(matched_verdict))
            if top is not None:
                title = top.get("title")
                top_finding_title = title if isinstance(title, str) else None
            signature = _verdict_signature(matched_verdict)
        history.append(
            LoopHistoryEntry(
                round=round_number,
                reviewer=reviewer,
                spawned_at=spawn_edge.get("created_at")
                if isinstance(spawn_edge.get("created_at"), str)
                else None,
                verdict_state=verdict_state,
                verdict_at=verdict_at,
                routed_at=routed_at,
                archived_at=_archive_time_for(reviewer, edges[spawn_index + 1 :]),
                top_finding=top_finding_title,
                finding_signature=signature,
            )
        )

    round_ = len(spawn_events)
    return LoopState(
        round=round_,
        cap=cap,
        danger=_danger_tier(round_),
        unrouted_verdict_count=unrouted,
        plateau_length=plateau_length,
        latest_verdict=latest_verdict_payload,
        history=history,
    )
