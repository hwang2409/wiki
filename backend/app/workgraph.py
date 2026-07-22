"""Per-ticket workgraph.json writer + composite health (graph-engineering D2).

The orchestrator is the sole writer: every spawn/steer/verdict/archive action
appends a typed edge via ``wiki graph append``, which validates the payload,
recomputes ``composite_health``, and atomically rewrites the hot copy under
``/tmp/agent-status`` plus a durable snapshot under ``~/.wiki/workgraphs`` on
meaningful appends (spawn / verdict / archive).

Schema bodies mirror the graph-engineering spec (sections 1.2-1.5 + 2.2).
When the D1 ``schemas/`` directory exists in the repo (WIKI-162), those files
are preferred; the embedded copies keep the writer usable before D1 lands and
outside a repo checkout.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_DIR = Path(os.environ.get("WIKI_AGENT_STATUS_DIR") or "/tmp/agent-status")
SNAPSHOT_DIR = Path(
    os.environ.get("WIKI_WORKGRAPH_SNAPSHOT_DIR") or Path.home() / ".wiki" / "workgraphs"
)
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

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

DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})$"
)


class WorkgraphError(ValueError):
    """Validation or IO failure while appending to a workgraph."""


# --- schemas (verbatim from vault/wiki/specs/graph-engineering.md 1.2-1.5) ---

FINDING_SCHEMA: dict[str, Any] = {
    "$id": "https://wiki.henry/schemas/finding.json",
    "type": "object",
    "required": [
        "id",
        "severity",
        "title",
        "observed",
        "why_wrong",
        "do_instead",
        "source_worker",
        "source_sha",
        "created_at",
    ],
    "properties": {
        "id": {"type": "string", "pattern": "^F-[a-z0-9]{6}$"},
        "severity": {"enum": ["BLOCKING", "HIGH", "MEDIUM", "LOW", "INFO"]},
        "title": {"type": "string", "maxLength": 140},
        "file": {"type": "string"},
        "line": {"type": "integer", "minimum": 1},
        "observed": {"type": "string"},
        "why_wrong": {"type": "string"},
        "do_instead": {"type": "string"},
        "constraint": {"type": "string"},
        "source_worker": {"type": "string"},
        "source_kind": {"enum": ["review", "sim", "thermo", "audit", "canary", "eval", "human"]},
        "source_sha": {"type": "string", "pattern": "^[a-f0-9]{7,40}$"},
        "linked_findings": {"type": "array", "items": {"type": "string"}},
        "resolved_by": {"type": ["string", "null"]},
        "created_at": {"type": "string", "format": "date-time"},
        "sla_deadline": {"type": ["string", "null"], "format": "date-time"},
    },
    "additionalProperties": False,
}

VERDICT_SCHEMA: dict[str, Any] = {
    "$id": "https://wiki.henry/schemas/verdict.json",
    "type": "object",
    "required": ["worker", "sha", "state", "findings", "created_at"],
    "properties": {
        "worker": {"type": "string"},
        "sha": {"type": "string"},
        "state": {"enum": ["MERGE-READY", "NOT-MERGE-READY", "NO-GO", "INSUFFICIENT-CONTEXT"]},
        "findings": {"type": "array", "items": {"$ref": "finding.json"}},
        "summary": {"type": "string", "maxLength": 500},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "additionalProperties": False,
}

STEER_SCHEMA: dict[str, Any] = {
    "$id": "https://wiki.henry/schemas/steer.json",
    "type": "object",
    "required": ["target_worker", "mode", "findings", "created_at"],
    "properties": {
        "target_worker": {"type": "string"},
        "mode": {"enum": ["now", "on-idle"]},
        "findings": {"type": "array", "items": {"$ref": "finding.json"}, "minItems": 1},
        "preamble": {"type": "string"},
        "constraint_bundle": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "additionalProperties": False,
}

EDGE_SCHEMA: dict[str, Any] = {
    "$id": "https://wiki.henry/schemas/edge.json",
    "type": "object",
    "required": ["kind", "from", "to", "created_at"],
    "properties": {
        "kind": {"enum": list(EDGE_KINDS)},
        "from": {"type": "string"},
        "to": {"type": "string"},
        "payload": {"type": "object"},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "additionalProperties": False,
}

WORKGRAPH_SCHEMA: dict[str, Any] = {
    "$id": "https://wiki.henry/schemas/workgraph.json",
    "type": "object",
    "required": [
        "ticket",
        "orch",
        "template",
        "created_at",
        "updated_at",
        "nodes",
        "edges",
        "composite_health",
    ],
    "properties": {
        "ticket": {"type": "string"},
        "orch": {"type": "string"},
        "template": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "kind", "label"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string"},
                    "label": {"type": "string"},
                    "worker_id": {"type": "string"},
                    "sha": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "edges": {"type": "array", "items": {"$ref": "edge.json"}},
        "composite_health": {
            "type": "object",
            "required": [
                "state",
                "open_findings",
                "blocking",
                "slowest_node_stall_seconds",
                "iteration_count",
            ],
            "properties": {
                "state": {"type": "string"},
                "open_findings": {"type": "integer", "minimum": 0},
                "blocking": {"type": "integer", "minimum": 0},
                "slowest_node_stall_seconds": {"type": "number", "minimum": 0},
                "iteration_count": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

# Per-kind payload requirements. steer/verdict payloads are the full
# Steer/Verdict objects; the rest mirror the $defs in the D1 edge schema
# (schemas/edge.schema.json) so behavior is identical whether that file is
# present or the embedded copies are in use.
PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {
    "spawn": {
        "type": "object",
        "required": ["ticket", "role", "model", "worktree", "request_id"],
        "properties": {
            "ticket": {"type": "string"},
            "role": {"type": "string"},
            "model": {"type": "string"},
            "effort": {"type": "string"},
            "worktree": {"type": "string"},
            "request_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "steer": STEER_SCHEMA,
    "verdict": VERDICT_SCHEMA,
    "archive": {
        "type": "object",
        "required": ["outcome", "ended_at"],
        "properties": {
            "outcome": {"type": "string"},
            "ended_at": {"type": "string", "format": "date-time"},
        },
        "additionalProperties": False,
    },
    "handoff": {
        "type": "object",
        "required": ["from_session", "to_session", "worktree", "pr_url", "remaining_summary"],
        "properties": {
            "from_session": {"type": "string"},
            "to_session": {"type": "string"},
            "worktree": {"type": "string"},
            "pr_url": {"type": ["string", "null"]},
            "remaining_summary": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "monitor_alarm": {
        "type": "object",
        "required": ["alarm_kind", "message", "watchlist_row"],
        "properties": {
            "alarm_kind": {"type": "string"},
            "message": {"type": "string"},
            "watchlist_row": {"oneOf": [{"type": "string"}, {"type": "object"}]},
        },
        "additionalProperties": False,
    },
    "capability_grant": {
        "type": "object",
        "required": ["capability", "granted_by"],
        "properties": {
            "capability": {"type": "string"},
            "granted_by": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "escalation": {
        "type": "object",
        "required": ["reason", "prior_findings", "target"],
        "properties": {
            "reason": {"type": "string"},
            "prior_findings": {"type": "array", "items": {"$ref": "finding.json"}},
            "target": {"enum": ["henry", "orchestrator"]},
        },
        "additionalProperties": False,
    },
}

# edge.schema.json $defs names for the literal payload shapes.
_PAYLOAD_DEF_NAMES = {
    "spawn": "spawnPayload",
    "archive": "archivePayload",
    "handoff": "handoffPayload",
    "monitor_alarm": "monitorAlarmPayload",
    "capability_grant": "capabilityGrantPayload",
    "escalation": "escalationPayload",
}


def payload_schema_for(edge_kind: str) -> dict[str, Any] | None:
    """Prefer the D1 schemas/ files: steer/verdict schemas directly, other
    payload shapes from edge.schema.json $defs; embedded copies otherwise."""
    if edge_kind in {"steer", "verdict"}:
        return load_schema(edge_kind)
    def_name = _PAYLOAD_DEF_NAMES.get(edge_kind)
    if def_name is None:
        return None
    defs = load_schema("edge").get("$defs")
    if isinstance(defs, dict) and isinstance(defs.get(def_name), dict):
        return defs[def_name]
    return PAYLOAD_SCHEMAS[edge_kind]

_EMBEDDED_SCHEMAS: dict[str, dict[str, Any]] = {
    "finding": FINDING_SCHEMA,
    "verdict": VERDICT_SCHEMA,
    "steer": STEER_SCHEMA,
    "edge": EDGE_SCHEMA,
    "workgraph": WORKGRAPH_SCHEMA,
}


def load_schema(name: str) -> dict[str, Any]:
    """D1 schemas/ file when present, embedded spec copy otherwise."""
    path = SCHEMA_DIR / f"{name}.schema.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return _EMBEDDED_SCHEMAS[name]


# --- minimal JSON Schema subset validator ---------------------------------
# Covers exactly the keywords the spec schemas use: type (incl. unions), enum,
# required, properties, additionalProperties: false, pattern, maxLength,
# minimum, minItems, items, oneOf (any-match), $ref (sibling schema by file
# name), format (date-time only). Unknown keywords (allOf/if/then in the D1
# edge schema) are ignored — per-kind payload checks come from
# payload_schema_for() instead. Deliberately not a general validator.

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve_ref(ref: str) -> dict[str, Any]:
    name = ref.rsplit("/", 1)[-1].removesuffix(".json").removesuffix(".schema")
    return load_schema(name)


def validate_instance(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return validate_instance(instance, _resolve_ref(ref), path)

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        if not any(not validate_instance(instance, sub, path) for sub in one_of if isinstance(sub, dict)):
            errors.append(f"{path}: matches no oneOf variant")
            return errors

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(t, lambda _v: True)(instance) for t in types):
            errors.append(f"{path}: expected type {'/'.join(types)}")
            return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
        return errors

    if isinstance(instance, str):
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, instance):
            errors.append(f"{path}: {instance!r} does not match {pattern}")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(instance) > max_length:
            errors.append(f"{path}: longer than maxLength {max_length}")
        if schema.get("format") == "date-time" and not DATE_TIME_RE.match(instance):
            errors.append(f"{path}: {instance!r} is not an RFC 3339 date-time")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if minimum is not None and instance < minimum:
            errors.append(f"{path}: {instance} below minimum {minimum}")

    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(instance) < min_items:
            errors.append(f"{path}: fewer than minItems {min_items}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(validate_instance(item, item_schema, f"{path}[{index}]"))

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate_instance(value, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {key!r}")

    return errors


def validate_payload(edge_kind: str, payload: Any) -> list[str]:
    schema = payload_schema_for(edge_kind)
    if schema is None:
        return [f"$: unknown edge kind {edge_kind!r}"]
    return validate_instance(payload, schema, "$.payload")


# --- graph mutation --------------------------------------------------------


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
        node = {
            "id": node_id,
            "kind": payload.get("role") or "worker",
            "label": label,
            "worker_id": str(worker_ticket),
        }
        return node
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

    payload_errors = validate_payload(edge_kind, payload)
    if payload_errors:
        raise WorkgraphError(
            f"payload does not satisfy the {edge_kind!r} schema:\n  " + "\n  ".join(payload_errors)
        )

    graph = load_workgraph(ticket, status_dir)
    if graph is None:
        if not orch:
            raise WorkgraphError(
                f"no workgraph for {ticket} yet; pass --orch to create one"
            )
        graph = create_workgraph(ticket, orch, template)

    now_ts = time.time() if now_ts is None else now_ts
    stamp = (
        datetime.fromtimestamp(now_ts, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    known = _node_ids(graph)
    graph_orch = str(graph.get("orch") or orch or "orch")
    for role, node_id in (("from", from_node), ("to", to_node)):
        if node_id not in known:
            graph.setdefault("nodes", []).append(
                _infer_node(node_id, edge_kind, role, payload, graph_orch)
            )
            known.add(node_id)

    edge = {
        "kind": edge_kind,
        "from": from_node,
        "to": to_node,
        "payload": payload,
        "created_at": stamp,
    }
    edge_errors = validate_instance(edge, load_schema("edge"), "$.edge")
    if edge_errors:
        raise WorkgraphError("edge does not satisfy the edge schema:\n  " + "\n  ".join(edge_errors))

    graph.setdefault("edges", []).append(edge)
    graph["updated_at"] = stamp
    graph["composite_health"] = compute_composite_health(graph, now_ts)

    graph_errors = validate_instance(graph, load_schema("workgraph"), "$")
    if graph_errors:
        raise WorkgraphError(
            "workgraph does not satisfy the workgraph schema:\n  " + "\n  ".join(graph_errors)
        )

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
