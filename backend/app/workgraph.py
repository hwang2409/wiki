"""Per-ticket workgraph.json writer + composite health (graph-engineering D2).

The orchestrator is the sole writer: every spawn/steer/verdict/archive action
appends a typed edge via ``wiki graph append``, which validates the edge
(including its kind-specific payload) against the canonical D1 schemas,
recomputes ``composite_health``, and atomically rewrites the hot copy under
``/tmp/agent-status`` plus a durable snapshot under ``~/.wiki/workgraphs`` on
meaningful appends (spawn / verdict / archive).

Validation delegates to ``wiki_cli.graph_lint`` (WIKI-162), which loads the
versioned ``schemas/`` files in both source checkouts and PyInstaller bundles.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wiki_cli import graph_lint

STATUS_DIR = Path(os.environ.get("WIKI_AGENT_STATUS_DIR") or "/tmp/agent-status")
SNAPSHOT_DIR = Path(
    os.environ.get("WIKI_WORKGRAPH_SNAPSHOT_DIR") or Path.home() / ".wiki" / "workgraphs"
)

EDGE_KINDS = (
    "spawn",
    "steer",
    "verdict",
    "archive",
    "handoff",
    "monitor_alarm",
    "capability_grant",
    "escalation",
)
# Appends that also write a durable ~/.wiki snapshot (spec 2.1).
SNAPSHOT_EDGE_KINDS = {"spawn", "verdict", "archive"}
STALL_ALARM_SECONDS = 1800
DEFAULT_ITERATION_CAP = 8


class WorkgraphError(ValueError):
    """Validation or IO failure while appending to a workgraph."""


def _validate(document: dict[str, Any], schema_name: str, what: str) -> None:
    try:
        violations = graph_lint.validate_document(document, schema_name)
    except graph_lint.GraphLintError as exc:
        raise WorkgraphError(str(exc)) from exc
    if violations:
        raise WorkgraphError(
            f"{what} does not satisfy the {schema_name} schema:\n  "
            + "\n  ".join(f"{pointer}: {message}" for pointer, message in violations)
        )


# --- graph IO --------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def hot_path(ticket: str, status_dir: Path | None = None) -> Path:
    return (status_dir or STATUS_DIR) / f"{ticket}.workgraph.json"


def load_workgraph(ticket: str, status_dir: Path | None = None) -> dict[str, Any] | None:
    path = hot_path(ticket, status_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def newest_snapshot_path(ticket: str, snapshot_dir: Path | None = None) -> Path | None:
    directory = snapshot_dir or SNAPSHOT_DIR
    best: tuple[int, Path] | None = None
    if not directory.is_dir():
        return None
    for path in directory.glob(f"{ticket}-*.workgraph.json"):
        stem = path.name.removesuffix(".workgraph.json")
        epoch_text = stem[len(ticket) + 1 :]
        if not epoch_text.isdigit():
            continue
        epoch = int(epoch_text)
        if best is None or epoch > best[0]:
            best = (epoch, path)
    return best[1] if best else None


def load_snapshot(ticket: str, snapshot_dir: Path | None = None) -> dict[str, Any] | None:
    path = newest_snapshot_path(ticket, snapshot_dir)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), suffix=".workgraph-tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(data, handle, indent=1)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).rename(path)


# --- graph mutation --------------------------------------------------------


def _node_ids(graph: dict[str, Any]) -> set[str]:
    return {node.get("id") for node in graph.get("nodes", []) if isinstance(node, dict)}


def _infer_node(
    node_id: str, edge_kind: str, role: str, payload: dict[str, Any], orch: str
) -> dict[str, Any]:
    """Auto-add shape for a node first referenced by this edge (spec 2.3)."""
    if role == "from" and edge_kind in {"spawn", "steer", "archive"}:
        return {"id": node_id, "kind": "orchestrator", "label": f"{orch} orch"}
    if role == "to" and edge_kind == "spawn":
        worker_ticket = payload.get("ticket") or node_id
        model = payload.get("model")
        label = f"{worker_ticket} {model}" if model else str(worker_ticket)
        return {
            "id": node_id,
            "kind": payload.get("role") or "worker",
            "label": label,
            "worker_id": str(worker_ticket),
        }
    if role == "to" and edge_kind in {"verdict", "handoff", "escalation", "monitor_alarm"}:
        return {"id": node_id, "kind": "orchestrator", "label": f"{orch} orch"}
    return {"id": node_id, "kind": "worker", "label": node_id}


def collect_findings(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Latest instance per finding id, in chronological edge order."""
    findings: dict[str, dict[str, Any]] = {}
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        payload = edge.get("payload")
        if not isinstance(payload, dict):
            continue
        for finding in payload.get("findings", []) or []:
            if isinstance(finding, dict) and isinstance(finding.get("id"), str):
                findings[finding["id"]] = finding
    return findings


def _archived_node_ids(graph: dict[str, Any]) -> set[str]:
    return {
        edge.get("to")
        for edge in graph.get("edges", [])
        if isinstance(edge, dict) and edge.get("kind") == "archive"
    }


def _live_worker_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    archived = _archived_node_ids(graph)
    return [
        node
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
        and node.get("kind") not in {"orchestrator", "monitor"}
        and node.get("id") not in archived
    ]


def compute_composite_health(graph: dict[str, Any], now_ts: float | None = None) -> dict[str, Any]:
    now_ts = time.time() if now_ts is None else now_ts
    edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]

    findings = collect_findings(graph)
    open_findings = [f for f in findings.values() if not f.get("resolved_by")]
    blocking = [f for f in open_findings if f.get("severity") == "BLOCKING"]
    iteration_count = sum(1 for edge in edges if edge.get("kind") == "verdict")

    # Stall = time since the last edge touching each live worker node; report
    # the worst offender so the watchlist can threshold on one number.
    slowest = 0
    for node in _live_worker_nodes(graph):
        node_id = node.get("id")
        last = None
        for edge in edges:
            if node_id in (edge.get("from"), edge.get("to")):
                ts = _parse_ts(edge.get("created_at"))
                if ts is not None and (last is None or ts > last):
                    last = ts
        if last is not None:
            slowest = max(slowest, int(now_ts - last))

    live_workers = _live_worker_nodes(graph)
    verdict_states = [
        (edge.get("payload") or {}).get("state")
        for edge in edges
        if edge.get("kind") == "verdict"
    ]
    has_workers = any(
        isinstance(node, dict) and node.get("kind") not in {"orchestrator", "monitor"}
        for node in graph.get("nodes", [])
    )
    if has_workers and not live_workers:
        state = "archived"
    elif verdict_states and verdict_states[-1] == "MERGE-READY" and not blocking:
        state = "merge-ready"
    elif any(edge.get("kind") in {"verdict", "steer"} for edge in edges):
        state = "iterating"
    else:
        state = "spawned"

    return {
        "state": state,
        "open_findings": len(open_findings),
        "blocking": len(blocking),
        "slowest_node_stall_seconds": max(0, slowest),
        "iteration_count": iteration_count,
    }


def create_workgraph(
    ticket: str, orch: str, template: str | None = None, created_at: str | None = None
) -> dict[str, Any]:
    created = created_at or now_iso()
    return {
        "ticket": ticket,
        "orch": orch,
        # The workgraph schema requires template; <orch>.implement matches the
        # D3 selector fallback until templates are picked explicitly.
        "template": template or f"{orch}.implement",
        "created_at": created,
        "updated_at": created,
        "nodes": [],
        "edges": [],
        "composite_health": {
            "state": "spawned",
            "open_findings": 0,
            "blocking": 0,
            "slowest_node_stall_seconds": 0,
            "iteration_count": 0,
        },
    }


def append_edge(
    ticket: str,
    edge_kind: str,
    from_node: str,
    to_node: str,
    payload: dict[str, Any],
    *,
    orch: str | None = None,
    template: str | None = None,
    status_dir: Path | None = None,
    snapshot_dir: Path | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Validate, append, recompute health, atomic-write hot + snapshot copies."""
    if edge_kind not in EDGE_KINDS:
        raise WorkgraphError(f"unknown edge kind {edge_kind!r}; expected one of {EDGE_KINDS}")
    if not isinstance(payload, dict):
        raise WorkgraphError("payload must be a JSON object")

    now_ts = time.time() if now_ts is None else now_ts
    stamp = (
        datetime.fromtimestamp(now_ts, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    edge = {
        "kind": edge_kind,
        "from": from_node,
        "to": to_node,
        "payload": payload,
        "created_at": stamp,
    }
    # The edge schema's per-kind conditionals validate the payload here.
    _validate(edge, "edge", f"{edge_kind} edge")

    graph = load_workgraph(ticket, status_dir)
    if graph is None:
        if not orch:
            raise WorkgraphError(f"no workgraph for {ticket} yet; pass --orch to create one")
        graph = create_workgraph(ticket, orch, template)

    known = _node_ids(graph)
    graph_orch = str(graph.get("orch") or orch or "orch")
    for role, node_id in (("from", from_node), ("to", to_node)):
        if node_id not in known:
            graph.setdefault("nodes", []).append(
                _infer_node(node_id, edge_kind, role, payload, graph_orch)
            )
            known.add(node_id)

    graph.setdefault("edges", []).append(edge)
    graph["updated_at"] = stamp
    graph["composite_health"] = compute_composite_health(graph, now_ts)

    _validate(graph, "workgraph", "workgraph")

    atomic_write_json(hot_path(ticket, status_dir), graph)
    snapshot_path: Path | None = None
    if edge_kind in SNAPSHOT_EDGE_KINDS:
        directory = snapshot_dir or SNAPSHOT_DIR
        # Millisecond epoch: scripted sequences append faster than 1/s and the
        # newest-snapshot lookup needs distinct, ordered names.
        snapshot_path = directory / f"{ticket}-{int(now_ts * 1000)}.workgraph.json"
        atomic_write_json(snapshot_path, graph)

    graph["_snapshot_path"] = str(snapshot_path) if snapshot_path else None
    return graph


# --- composite-health watchlist checks (spec 2.5) --------------------------


def health_alarms(
    graph: dict[str, Any],
    *,
    iteration_cap: int = DEFAULT_ITERATION_CAP,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    health = compute_composite_health(graph, now_ts)
    alarms: list[dict[str, Any]] = []

    blocking = health.get("blocking", 0)
    live_reviewers = [
        node for node in _live_worker_nodes(graph) if node.get("kind") == "review"
    ]
    if blocking and not live_reviewers:
        alarms.append(
            {
                "check": "blocking_no_reviewer",
                "message": f"{blocking} BLOCKING finding(s) open with no live reviewer",
            }
        )

    stall = health.get("slowest_node_stall_seconds", 0)
    if stall > STALL_ALARM_SECONDS:
        alarms.append(
            {
                "check": "node_stall",
                "message": f"slowest node stalled {stall}s (> {STALL_ALARM_SECONDS}s)",
            }
        )

    iterations = health.get("iteration_count", 0)
    if iterations > iteration_cap:
        alarms.append(
            {
                "check": "iteration_cap",
                "message": f"iteration_count {iterations} exceeds cap {iteration_cap}",
            }
        )
    return alarms
