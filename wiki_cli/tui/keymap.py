"""Keymap + navigation reducer.

Kept pure so the interaction contract (what each key does, cursor
clamping, filter matching) can be tested without a curses screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from .format import flatten_navigable
from .snapshot import FleetGroup, FleetSnapshot, WorkerRow


FLEET_HINTS: tuple[tuple[str, str], ...] = (
    ("j/k", "move"),
    ("g/G", "top/end"),
    ("enter", "open"),
    ("/", "filter"),
    ("r", "refresh"),
    ("?", "help"),
    ("q", "quit"),
)

DETAIL_HINTS: tuple[tuple[str, str], ...] = (
    ("j/k", "scroll"),
    ("g/G", "top/end"),
    ("f", "follow"),
    ("o", "open pr"),
    ("esc", "back"),
    ("q", "quit"),
)


@dataclass(frozen=True)
class Selection:
    """Cursor state for the fleet view: index over navigable entries."""

    index: int = 0
    filter_text: str = ""
    filter_active: bool = False

    def clamp(self, entries: Sequence[tuple[str, object]]) -> "Selection":
        if not entries:
            return replace(self, index=0)
        idx = max(0, min(self.index, len(entries) - 1))
        return replace(self, index=idx)


def matches_filter(worker: WorkerRow, needle: str) -> bool:
    if not needle:
        return True
    hay = " ".join(
        x for x in (
            worker.ticket, worker.orch, worker.role, worker.kind,
            worker.state, worker.step, worker.blocker,
        ) if x
    ).lower()
    return needle.lower() in hay


def filter_snapshot(snapshot: FleetSnapshot, needle: str) -> FleetSnapshot:
    """Drop workers that do not match ``needle``; drop empty groups."""
    if not needle:
        return snapshot
    groups: list[FleetGroup] = []
    for g in snapshot.groups:
        kept = tuple(w for w in g.workers if matches_filter(w, needle))
        if kept:
            groups.append(FleetGroup(orch=g.orch, rollup=g.rollup, workers=kept))
    return FleetSnapshot(
        groups=tuple(groups),
        generated_at=snapshot.generated_at,
        stale=snapshot.stale,
        fetch_error=snapshot.fetch_error,
    )


def move(selection: Selection, entries: Sequence[tuple[str, object]], delta: int) -> Selection:
    """Move cursor by ``delta``, skipping ``group`` entries when possible."""
    if not entries:
        return selection.clamp(entries)
    if delta == 0:
        return selection.clamp(entries)
    idx = selection.index
    step = 1 if delta > 0 else -1
    remaining = abs(delta)
    while remaining > 0:
        next_idx = idx + step
        if next_idx < 0 or next_idx >= len(entries):
            break
        idx = next_idx
        if entries[idx][0] == "worker":
            remaining -= 1
    if entries[idx][0] == "group":
        for probe in range(idx + step, len(entries) if step > 0 else -1, step):
            if 0 <= probe < len(entries) and entries[probe][0] == "worker":
                idx = probe
                break
    return replace(selection, index=idx).clamp(entries)


def jump_top(entries: Sequence[tuple[str, object]]) -> int:
    for i, (kind, _) in enumerate(entries):
        if kind == "worker":
            return i
    return 0


def jump_end(entries: Sequence[tuple[str, object]]) -> int:
    for i in range(len(entries) - 1, -1, -1):
        if entries[i][0] == "worker":
            return i
    return max(0, len(entries) - 1)


def selected_worker(
    selection: Selection, snapshot: FleetSnapshot
) -> WorkerRow | None:
    entries = flatten_navigable(snapshot.groups)
    if not entries:
        return None
    idx = min(selection.index, len(entries) - 1)
    kind, payload = entries[idx]
    if kind == "worker" and isinstance(payload, WorkerRow):
        return payload
    return None
