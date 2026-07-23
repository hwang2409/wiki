"""Per-ticket workgraph.json writer + composite health (graph-engineering D2).

The orchestrator is the sole writer: every spawn/steer/verdict/archive action
appends a typed edge (via the canonical agent endpoints or ``wiki graph
append``), which validates the edge (including its kind-specific payload)
against the canonical D1 schemas, recomputes ``composite_health``, and
atomically rewrites the durable snapshot under ``~/.wiki/workgraphs`` followed
by the hot copy under ``/tmp/agent-status``.

Durability contract for ``append_edge``:

- A per-ticket cross-process lock (``<ticket>.workgraph.lock`` next to the hot
  file) is held from load/validation through both commits, so concurrent
  backend requests and CLI appends serialize instead of last-writer-winning
  away each other's edges.
- Both output files are staged as temp files first; the snapshot commits
  BEFORE the hot pointer, so a failure can never leave a live edge without a
  durable copy. Temp files are removed on every error path and IO failures
  surface as ``WorkgraphError``.
- The hot file is the authoritative last commit, and the commit protocol is
  crash-consistent: if a writer dies between the snapshot rename and the hot
  rename, the next append (under the same lock, before allocating a new
  revision) detects the orphan snapshot — its edges strictly extend the hot
  graph's — promotes it into the append base, and republishes hot from it.
  The orphan's committed edge can therefore never be shadowed by a newer
  revision or dropped from the recovered history, and retrying the failed
  append dedupes against the promoted base. Replays are detected by stable
  operation key across the full edge history — the edge-level ``request_id``
  (the supervisor's exact idempotency key, mode-scoped for steer edges since
  ``now``/``on-idle`` are distinct supervisor methods), with legacy fallbacks
  to the spawn payload's ``request_id`` or the steer finding's request-digest
  fields — and exact newest-edge equality for keyless kinds. A replayed
  append mutates nothing.
- Snapshot names carry a per-ticket monotonic revision assigned while the
  ticket lock is held, so recovery ordering follows commit order — never wall
  time, which is sampled before the lock and can invert under contention.
  Epoch-ns, pid, and a per-process counter keep names collision-proof.
- An existing-but-unreadable/invalid hot file fails closed
  (``WorkgraphCorruptError``); it is never silently replaced. Recovery is
  explicit via ``recover_from_snapshot`` / ``wiki graph recover``.

Validation delegates to ``wiki_cli.graph_lint`` (WIKI-162), which loads the
versioned ``schemas/`` files in both source checkouts and PyInstaller bundles.
"""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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

# One injected clock for every stall/health computation so the renderer, the
# health endpoint, and the CLI agree on "now" (and tests can freeze it).
CLOCK: Callable[[], float] = time.time


class WorkgraphError(ValueError):
    """Validation or IO failure while appending to a workgraph."""


class WorkgraphCorruptError(WorkgraphError):
    """An existing hot workgraph file cannot be read or parsed."""


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


def _stamp(now_ts: float) -> str:
    return (
        datetime.fromtimestamp(now_ts, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def now_iso() -> str:
    return _stamp(CLOCK())


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def hot_path(ticket: str, status_dir: Path | None = None) -> Path:
    return (status_dir or STATUS_DIR) / f"{ticket}.workgraph.json"


@contextlib.contextmanager
def _ticket_lock(ticket: str, status_dir: Path | None = None):
    """Cross-process mutex serializing every mutation of one ticket's graph.

    Backend requests and ``wiki graph append`` share the same lockfile, so a
    read-modify-write can never interleave with another writer's commit.
    """
    directory = status_dir or STATUS_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / f"{ticket}.workgraph.lock", "a+b")
    except OSError as exc:
        raise WorkgraphError(f"could not open workgraph lock for {ticket}: {exc}") from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_workgraph(ticket: str, status_dir: Path | None = None) -> dict[str, Any] | None:
    """Return the hot graph, ``None`` if absent, or fail closed if damaged.

    A missing file is a normal state (no graph yet). An existing file that
    cannot be read or parsed raises ``WorkgraphCorruptError`` so a caller can
    never mistake a damaged graph for a missing one and overwrite its edge
    history.
    """
    path = hot_path(ticket, status_dir)
    recovery_hint = f"inspect it or run `wiki graph recover {ticket}` to restore the newest snapshot"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkgraphCorruptError(
            f"hot workgraph {path} exists but cannot be read ({exc}); {recovery_hint}"
        ) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise WorkgraphCorruptError(
            f"hot workgraph {path} is not valid JSON ({exc}); {recovery_hint}"
        ) from exc
    if not isinstance(data, dict):
        raise WorkgraphCorruptError(
            f"hot workgraph {path} is not a JSON object; {recovery_hint}"
        )
    return data


# Per-process counter suffix for snapshot names: combined with epoch-ns and
# pid, two appends can never produce the same name even at an identical
# (injected or same-instant) clock reading.
_SNAPSHOT_SEQ = itertools.count()


def _snapshot_name(ticket: str, revision: int, now_ts: float) -> str:
    return (
        f"{ticket}-r{revision}-{int(now_ts * 1_000_000_000)}"
        f"-{os.getpid()}-{next(_SNAPSHOT_SEQ)}.workgraph.json"
    )


def _snapshot_sort_key(suffix: str) -> tuple[int, int, int, int] | None:
    """(revision, epoch-ns, pid, seq) — revision dominates.

    Legacy pre-revision names sort as revision 0, so any revisioned snapshot
    outranks every legacy one regardless of embedded wall time.
    """
    parts = suffix.split("-")
    if len(parts) == 4 and parts[0][:1] == "r":
        if parts[0][1:].isdigit() and all(part.isdigit() for part in parts[1:]):
            return (int(parts[0][1:]), int(parts[1]), int(parts[2]), int(parts[3]))
        return None
    if not parts or not all(part.isdigit() for part in parts):
        return None
    if len(parts) == 1:  # legacy millisecond names sort against epoch-ns names
        return (0, int(parts[0]) * 1_000_000, 0, 0)
    if len(parts) == 3:  # legacy epoch-ns names, pre-revision
        return (0, int(parts[0]), int(parts[1]), int(parts[2]))
    return None


def _latest_snapshot_revision(ticket: str, directory: Path) -> int:
    if not directory.is_dir():
        return 0
    best = 0
    for path in directory.glob(f"{ticket}-*.workgraph.json"):
        stem = path.name.removesuffix(".workgraph.json")
        key = _snapshot_sort_key(stem[len(ticket) + 1 :])
        if key is not None:
            best = max(best, key[0])
    return best


def newest_snapshot_path(ticket: str, snapshot_dir: Path | None = None) -> Path | None:
    directory = snapshot_dir or SNAPSHOT_DIR
    best: tuple[tuple[int, int, int], Path] | None = None
    if not directory.is_dir():
        return None
    for path in directory.glob(f"{ticket}-*.workgraph.json"):
        stem = path.name.removesuffix(".workgraph.json")
        key = _snapshot_sort_key(stem[len(ticket) + 1 :])
        if key is None:
            continue
        if best is None or key > best[0]:
            best = (key, path)
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


def _stage_json(directory: Path, text: str) -> Path:
    """Write serialized graph text to a temp file in ``directory``.

    The temp path is captured before any write so it can be unlinked on every
    unsuccessful exit — a failed write or close must not leak a staging file.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=str(directory), suffix=".workgraph-tmp", delete=False, encoding="utf-8"
        )
    except OSError as exc:
        raise WorkgraphError(f"could not stage workgraph write under {directory}: {exc}") from exc
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(text)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise WorkgraphError(f"could not stage workgraph write under {directory}: {exc}") from exc
    return temp


def _serialize(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1) + "\n"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    temp = _stage_json(path.parent, _serialize(data))
    try:
        temp.rename(path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise WorkgraphError(f"could not commit workgraph write to {path}: {exc}") from exc


def recover_from_snapshot(
    ticket: str,
    status_dir: Path | None = None,
    snapshot_dir: Path | None = None,
) -> dict[str, str | None]:
    """Explicitly restore the hot file from the newest durable snapshot.

    The damaged hot file (if any) is preserved as ``<hot>.corrupt-<epoch>``,
    never deleted.
    """
    snapshot_path = newest_snapshot_path(ticket, snapshot_dir)
    if snapshot_path is None:
        directory = snapshot_dir or SNAPSHOT_DIR
        raise WorkgraphError(f"no snapshot found for {ticket} under {directory}")
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkgraphError(f"snapshot {snapshot_path} is unusable: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkgraphError(f"snapshot {snapshot_path} is not a JSON object")
    _validate(data, "workgraph", f"snapshot {snapshot_path.name}")

    hot = hot_path(ticket, status_dir)
    backup: Path | None = None
    with _ticket_lock(ticket, status_dir):
        if hot.exists() or hot.is_symlink():
            backup = hot.with_name(f"{hot.name}.corrupt-{int(CLOCK() * 1000)}")
            try:
                hot.rename(backup)
            except OSError as exc:
                raise WorkgraphError(f"could not preserve damaged hot file {hot}: {exc}") from exc
        atomic_write_json(hot, data)
    return {"snapshot": str(snapshot_path), "backup": str(backup) if backup else None}


# --- graph mutation --------------------------------------------------------


def _node_ids(graph: dict[str, Any]) -> set[str]:
    return {node.get("id") for node in graph.get("nodes", []) if isinstance(node, dict)}


# Explicit (edge kind -> from/to endpoint node kind) mapping for nodes first
# seen through that edge. "spawned" resolves to the spawn payload's role so an
# implement/review/plan worker lands with its real kind; monitor and review
# sources must never default to "worker" or they corrupt composite health.
_ENDPOINT_NODE_KINDS: dict[str, tuple[str, str]] = {
    "spawn": ("orchestrator", "spawned"),
    "steer": ("orchestrator", "worker"),
    "verdict": ("review", "orchestrator"),
    "archive": ("orchestrator", "worker"),
    "handoff": ("worker", "worker"),
    "monitor_alarm": ("monitor", "orchestrator"),
    "capability_grant": ("orchestrator", "orchestrator"),
    "escalation": ("monitor", "orchestrator"),
}


def _infer_node(
    node_id: str, edge_kind: str, role: str, payload: dict[str, Any], orch: str
) -> dict[str, Any]:
    """Auto-add shape for a node first referenced by this edge (spec 2.3)."""
    kind = _ENDPOINT_NODE_KINDS[edge_kind][0 if role == "from" else 1]
    if kind == "orchestrator":
        return {"id": node_id, "kind": "orchestrator", "label": f"{orch} orch"}
    if kind == "monitor":
        return {"id": node_id, "kind": "monitor", "label": f"{node_id} monitor"}
    if kind == "spawned":
        worker_ticket = payload.get("ticket") or node_id
        model = payload.get("model")
        label = f"{worker_ticket} {model}" if model else str(worker_ticket)
        return {
            "id": node_id,
            "kind": payload.get("role") or "worker",
            "label": label,
            "worker_id": str(worker_ticket),
        }
    if edge_kind == "verdict" and role == "from":
        worker_id = payload.get("worker")
        worker_id = worker_id if isinstance(worker_id, str) and worker_id else node_id
        return {"id": node_id, "kind": "review", "label": worker_id, "worker_id": worker_id}
    worker_id = node_id
    if edge_kind == "steer" and role == "to":
        target = payload.get("target_worker")
        if isinstance(target, str) and target:
            worker_id = target
    return {"id": node_id, "kind": kind, "label": node_id, "worker_id": worker_id}


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
    now_ts = CLOCK() if now_ts is None else now_ts
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


def _edge_op_key(edge: object) -> str | None:
    """Stable operation key for supervisor-idempotent edge kinds.

    The edge-level ``request_id`` — the supervisor's exact idempotency key,
    persisted as operation metadata — dominates when present: it is
    body-independent, so a replay with altered text still dedupes and two
    distinct operations can never collide. Steer keys also carry the payload
    ``mode``: ``now`` and ``on-idle`` are distinct supervisor methods
    (``run/send_now`` vs ``run/send_on_idle``), so one request id used once
    per mode is two operations and must record two edges. Older edges
    without an edge-level id fall back to the spawn payload's
    ``request_id``, then to the legacy steer digest fields
    (``id``/``source_sha`` prefixes of one request-id+message digest).
    Kinds without any request identity return ``None`` and dedupe by exact
    newest-edge equality.
    """
    if not isinstance(edge, dict):
        return None
    kind = edge.get("kind")
    payload = edge.get("payload")
    payload = payload if isinstance(payload, dict) else None
    mode = payload.get("mode") if payload else None
    request_id = edge.get("request_id")
    if isinstance(request_id, str) and request_id:
        if kind == "steer":
            return f"steer:{mode}:{request_id}"
        return f"{kind}:{request_id}"
    if payload is None:
        return None
    if kind == "spawn":
        payload_request_id = payload.get("request_id")
        if isinstance(payload_request_id, str) and payload_request_id:
            return f"spawn:{payload_request_id}"
        return None
    if kind == "steer":
        findings = payload.get("findings")
        first = findings[0] if isinstance(findings, list) and findings else None
        if isinstance(first, dict):
            finding_id = first.get("id")
            sha = first.get("source_sha")
            if isinstance(finding_id, str) and finding_id and isinstance(sha, str) and sha:
                return f"steer:{mode}:{finding_id}:{sha}"
    return None


def _orphan_snapshot_graph(
    ticket: str, hot_graph: dict[str, Any] | None, snapshot_dir: Path
) -> dict[str, Any] | None:
    """Return the newest snapshot iff it strictly extends the hot graph.

    Commit order is snapshot first, hot second: a crash between the two
    renames leaves the newest snapshot carrying a committed edge the hot
    graph lacks. The caller (holding the ticket lock) promotes that orphan
    into the append base BEFORE allocating the next revision — otherwise the
    next append would commit revision N+1 from the stale hot graph and
    recovery would silently drop the orphan's edge from the durable history.

    Promotion requires the hot edge list to be a strict prefix of the
    snapshot's (the only shape the crash window can produce; a missing hot
    file counts as the empty prefix) and the snapshot to be schema-valid.
    Anything else — equal/older snapshots (the normal state), unreadable or
    invalid files (nothing recoverable to promote), divergent histories —
    returns ``None`` and the hot graph stays authoritative.
    """
    path = newest_snapshot_path(ticket, snapshot_dir)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    snapshot_edges = data.get("edges")
    if not isinstance(snapshot_edges, list) or not snapshot_edges:
        return None
    hot_edges = hot_graph.get("edges", []) if hot_graph is not None else []
    if not isinstance(hot_edges, list):
        return None
    if len(snapshot_edges) <= len(hot_edges):
        return None
    if snapshot_edges[: len(hot_edges)] != hot_edges:
        return None
    try:
        _validate(data, "workgraph", f"orphan snapshot {path.name}")
    except WorkgraphError:
        return None
    return data


def _is_replay(graph: dict[str, Any], edge: dict[str, Any]) -> bool:
    edges = graph.get("edges")
    if not isinstance(edges, list) or not edges:
        return False
    # Keyed kinds dedupe across the FULL history: a replay can arrive after
    # other edges have landed, so newest-edge equality is not enough.
    key = _edge_op_key(edge)
    if key is not None:
        return any(_edge_op_key(prior) == key for prior in edges)
    last = edges[-1]
    return isinstance(last, dict) and all(
        last.get(key) == edge.get(key) for key in ("kind", "from", "to", "payload")
    )


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
    request_id: str | None = None,
) -> dict[str, Any]:
    """Validate, append, recompute health, commit snapshot then hot copy.

    ``request_id`` is the supervisor's exact idempotency key for this
    operation; when given it is persisted on the edge and becomes the replay
    dedupe key across the full edge history.
    """
    if edge_kind not in EDGE_KINDS:
        raise WorkgraphError(f"unknown edge kind {edge_kind!r}; expected one of {EDGE_KINDS}")
    if not isinstance(payload, dict):
        raise WorkgraphError("payload must be a JSON object")

    now_ts = CLOCK() if now_ts is None else now_ts
    stamp = _stamp(now_ts)
    edge = {
        "kind": edge_kind,
        "from": from_node,
        "to": to_node,
        "payload": payload,
        "created_at": stamp,
    }
    if request_id:
        edge["request_id"] = request_id
    # The edge schema's per-kind conditionals validate the payload here,
    # before any graph state is loaded or mutated.
    _validate(edge, "edge", f"{edge_kind} edge")

    # The cross-process lock spans load through both commits: another writer
    # can never interleave between our read and our rename.
    hot = hot_path(ticket, status_dir)
    directory = snapshot_dir or SNAPSHOT_DIR
    with _ticket_lock(ticket, status_dir):
        graph = load_workgraph(ticket, status_dir)
        if graph is not None:
            try:
                _validate(graph, "workgraph", "existing workgraph")
            except WorkgraphError as exc:
                raise WorkgraphCorruptError(
                    f"existing hot workgraph for {ticket} is invalid and will not be replaced "
                    f"({exc}); run `wiki graph recover {ticket}` to restore the newest snapshot"
                ) from exc
        # Crash reconciliation (still under the lock): if a previous commit
        # died between the snapshot and hot renames, promote the orphan
        # snapshot into the append base — and republish hot immediately, so
        # the store is consistent even if this append replays or fails.
        orphan = _orphan_snapshot_graph(ticket, graph, directory)
        if orphan is not None:
            graph = orphan
            atomic_write_json(hot, graph)
        if graph is None:
            if not orch:
                raise WorkgraphError(f"no workgraph for {ticket} yet; pass --orch to create one")
            graph = create_workgraph(ticket, orch, template, created_at=stamp)
        elif _is_replay(graph, edge):
            graph["_snapshot_path"] = None
            graph["_replayed"] = True
            return graph

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

        # Stage everything, then commit the durable snapshot BEFORE the hot
        # pointer: a live edge must never exist without its durable copy.
        serialized = _serialize(graph)
        snapshot_path: Path | None = None
        staged: list[tuple[Path, Path]] = []
        if edge_kind in SNAPSHOT_EDGE_KINDS:
            # Revision is assigned while HOLDING the ticket lock, so it follows
            # commit order even when a slower writer sampled an older now_ts
            # before the lock. Recovery orders by revision, never wall time.
            revision = _latest_snapshot_revision(ticket, directory) + 1
            snapshot_path = directory / _snapshot_name(ticket, revision, now_ts)
            staged.append((_stage_json(directory, serialized), snapshot_path))
        try:
            staged.append((_stage_json(hot.parent, serialized), hot))
            for temp, target in staged:
                try:
                    temp.rename(target)
                except OSError as exc:
                    raise WorkgraphError(
                        f"could not commit workgraph write to {target}: {exc}"
                    ) from exc
        except WorkgraphError:
            for temp, _target in staged:
                temp.unlink(missing_ok=True)
            raise

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


def current_health(
    graph: dict[str, Any],
    *,
    iteration_cap: int = DEFAULT_ITERATION_CAP,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Health + alarms computed from one clock read.

    Every consumer (renderer endpoint, health endpoint, CLI) goes through this
    so the same query at the same instant gives the same answer.
    """
    now_ts = CLOCK() if now_ts is None else now_ts
    return {
        "computed_at": _stamp(now_ts),
        "health": compute_composite_health(graph, now_ts),
        "alarms": health_alarms(graph, iteration_cap=iteration_cap, now_ts=now_ts),
    }
