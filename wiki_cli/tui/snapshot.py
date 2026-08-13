"""Normalize ``/dashboard/data`` + ``/api/agents/{ticket}/session`` payloads.

Pure functions — no HTTP, no curses. Tests hit these directly to prove
the aggregation-consumption layer stays stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class WorkerRow:
    ticket: str
    base_ticket: str
    orch: str
    role: str | None
    kind: str | None
    model: str | None
    state: str | None
    step: str | None
    blocker: str | None
    pr: str | None
    worktree: str | None
    status_age_s: float | None
    alarms: tuple[str, ...]
    review_round: int | None


@dataclass(frozen=True)
class OrchRollup:
    orch: str
    total: int
    working: int
    idle: int
    merge_ready: int
    blocked: int
    stalled_or_failed: int
    waiting_approval: int


@dataclass(frozen=True)
class FleetGroup:
    orch: str
    rollup: OrchRollup
    workers: tuple[WorkerRow, ...]


@dataclass(frozen=True)
class FleetSnapshot:
    groups: tuple[FleetGroup, ...]
    generated_at: str | None
    stale: bool = False
    fetch_error: str | None = None

    def total_workers(self) -> int:
        return sum(len(g.workers) for g in self.groups)

    def flatten(self) -> list[tuple[FleetGroup, WorkerRow]]:
        out: list[tuple[FleetGroup, WorkerRow]] = []
        for g in self.groups:
            for w in g.workers:
                out.append((g, w))
        return out

    def find(self, ticket: str) -> WorkerRow | None:
        for _, w in self.flatten():
            if w.ticket == ticket:
                return w
        return None


@dataclass(frozen=True)
class TranscriptEvent:
    ts: str | None
    kind: str
    label: str
    text: str


@dataclass(frozen=True)
class WorkerSession:
    ticket: str
    pr: str | None
    state: str | None
    step: str | None
    blocker: str | None
    latest_verdict: str | None
    events: tuple[TranscriptEvent, ...]
    fetch_error: str | None = None


UNASSIGNED = "(unassigned)"

WORKING_ISH = {"working", "waiting-approval", "starting"}


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _pr_url(value: Any) -> str | None:
    if isinstance(value, dict):
        return _as_str(value.get("url"))
    return _as_str(value)


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_tuple_str(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(x for x in value if isinstance(x, str))


def worker_from_payload(raw: dict[str, Any]) -> WorkerRow:
    orch = _as_str(raw.get("orch")) or UNASSIGNED
    return WorkerRow(
        ticket=str(raw.get("ticket") or ""),
        base_ticket=str(raw.get("base_ticket") or raw.get("ticket") or ""),
        orch=orch,
        role=_as_str(raw.get("role")),
        kind=_as_str(raw.get("kind")),
        model=_as_str(raw.get("model")),
        state=_as_str(raw.get("state")),
        step=_as_str(raw.get("step")),
        blocker=_as_str(raw.get("blocker")),
        pr=_pr_url(raw.get("pr")),
        worktree=_as_str(raw.get("worktree")),
        status_age_s=_as_float(raw.get("status_age_s")),
        alarms=_as_tuple_str(raw.get("alarms")),
        review_round=_as_int(raw.get("review_round")),
    )


def rollup_from_payload(raw: dict[str, Any]) -> OrchRollup:
    def _n(key: str) -> int:
        v = raw.get(key)
        return v if isinstance(v, int) else 0

    return OrchRollup(
        orch=str(raw.get("orch") or UNASSIGNED),
        total=_n("total"),
        working=_n("working"),
        idle=_n("idle"),
        merge_ready=_n("merge_ready"),
        blocked=_n("blocked"),
        stalled_or_failed=_n("stalled_or_failed"),
        waiting_approval=_n("waiting_approval"),
    )


def _worker_sort_key(w: WorkerRow) -> tuple[int, int, str]:
    """Sort within a group: attention first, then ticket alpha.

    ``blocked`` / ``waiting-approval`` / ``stale`` / ``review-gap`` /
    ``unrouted-verdict`` alarms rise to the top; ``merge-ready`` next;
    everything else after.
    """
    urgent = {"blocked", "stale", "review-gap", "unrouted-verdict", "waiting-approval", "attention"}
    if any(a in urgent for a in w.alarms):
        alarm_weight = 0
    elif "merge-ready" in w.alarms:
        alarm_weight = 1
    else:
        alarm_weight = 2
    role_weight = 0 if (w.role or "implement") == "implement" else 1
    return (alarm_weight, role_weight, w.ticket)


def snapshot_from_payload(
    payload: dict[str, Any],
    *,
    stale: bool = False,
    fetch_error: str | None = None,
) -> FleetSnapshot:
    """Build a :class:`FleetSnapshot` from the ``/dashboard/data`` body.

    Missing / malformed fields produce empty groups rather than raising —
    the caller renders the "backend not running" state through
    ``fetch_error``.
    """
    if not isinstance(payload, dict):
        return FleetSnapshot(groups=(), generated_at=None, stale=stale, fetch_error=fetch_error)

    workers_raw = payload.get("workers") or []
    rollups_raw = payload.get("orchestrators") or []
    workers = [worker_from_payload(w) for w in workers_raw if isinstance(w, dict)]
    rollups = {
        r.orch: r for r in (rollup_from_payload(r) for r in rollups_raw if isinstance(r, dict))
    }

    grouped: dict[str, list[WorkerRow]] = {}
    for w in workers:
        grouped.setdefault(w.orch, []).append(w)
    for orch in rollups:
        grouped.setdefault(orch, [])

    groups: list[FleetGroup] = []
    for orch in sorted(grouped):
        rows = sorted(grouped[orch], key=_worker_sort_key)
        rollup = rollups.get(orch) or OrchRollup(
            orch=orch,
            total=len(rows),
            working=sum(1 for r in rows if r.state == "working"),
            idle=sum(1 for r in rows if r.state == "idle"),
            merge_ready=sum(1 for r in rows if r.state == "merge-ready"),
            blocked=sum(1 for r in rows if r.state == "blocked"),
            stalled_or_failed=sum(1 for r in rows if "stale" in r.alarms),
            waiting_approval=sum(1 for r in rows if "waiting-approval" in r.alarms),
        )
        groups.append(FleetGroup(orch=orch, rollup=rollup, workers=tuple(rows)))

    return FleetSnapshot(
        groups=tuple(groups),
        generated_at=_as_str(payload.get("generated_at")),
        stale=stale,
        fetch_error=fetch_error,
    )


# --- Worker session parsing ------------------------------------------------

VERDICT_MARKERS = ("MERGE-READY", "NOT-MERGE-READY", "GATE-GREEN")


def _event_label(kind: str, tool: dict[str, Any] | None) -> str:
    if kind == "tool" and isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str) and name:
            return f"tool.{name}"
        return "tool"
    return kind


def _event_text(raw: dict[str, Any]) -> str:
    text = raw.get("text")
    if isinstance(text, str) and text.strip():
        return text
    tool = raw.get("tool")
    if isinstance(tool, dict):
        summary = tool.get("summary")
        if isinstance(summary, str) and summary:
            return summary
        cmd = tool.get("input")
        if isinstance(cmd, str) and cmd:
            return cmd
    return ""


def _latest_verdict(events: list[TranscriptEvent]) -> str | None:
    for ev in reversed(events):
        if ev.kind != "assistant":
            continue
        for line in reversed(ev.text.splitlines()):
            snippet = line.strip()
            if not snippet:
                continue
            for marker in ("NOT-MERGE-READY", "MERGE-READY", "GATE-GREEN"):
                if marker in snippet:
                    return snippet[:200]
    return None


def session_from_payload(ticket: str, payload: dict[str, Any]) -> WorkerSession:
    if not isinstance(payload, dict):
        return WorkerSession(
            ticket=ticket, pr=None, state=None, step=None, blocker=None,
            latest_verdict=None, events=(),
        )
    raw_events = payload.get("events") or []
    events: list[TranscriptEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "?")
        label = _event_label(kind, raw.get("tool") if isinstance(raw.get("tool"), dict) else None)
        text = _event_text(raw)
        events.append(TranscriptEvent(
            ts=_as_str(raw.get("ts")),
            kind=kind,
            label=label,
            text=text,
        ))
    meta = payload.get("session_meta") if isinstance(payload.get("session_meta"), dict) else {}
    return WorkerSession(
        ticket=ticket,
        pr=_pr_url(payload.get("pr")),
        state=_as_str(meta.get("state")),
        step=_as_str(meta.get("step")),
        blocker=_as_str(meta.get("blocker")),
        latest_verdict=_latest_verdict(events),
        events=tuple(events),
    )


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
