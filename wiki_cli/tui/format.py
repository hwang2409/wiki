"""Pure string formatting for rows, headers, footers.

Kept separate from curses so the same functions can be unit-tested and
used by the header / footer / debug dump paths without a live terminal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
import unicodedata

from .snapshot import FleetGroup, OrchRollup, WorkerRow, parse_iso


DEFAULT_COLUMNS = (
    ("ticket", 22),
    ("kind", 4),
    ("role", 10),
    ("state", 14),
    ("age", 8),
    ("step", 0),  # 0 == flex, takes remaining
)


def clean_text(text: str) -> str:
    """Remove terminal controls and render lone surrogates safely."""
    out: list[str] = []
    for char in text:
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            out.append(f"\\u{codepoint:04x}")
        elif unicodedata.category(char) == "Cc":
            continue
        else:
            out.append(char)
    return "".join(out)


def cell_width(text: str) -> int:
    """Return terminal cells using east-asian width and combining marks."""
    width = 0
    for char in clean_text(text):
        if unicodedata.category(char) in {"Cf", "Mn", "Me"}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def truncate(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` cells, ellipsizing with ``…``."""
    if width <= 0:
        return ""
    text = clean_text(text)
    if cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    target = width - cell_width("…")
    out: list[str] = []
    used = 0
    for char in text:
        char_width = cell_width(char)
        if used + char_width > target:
            break
        out.append(char)
        used += char_width
    return "".join(out) + "…"


def pad(text: str, width: int) -> str:
    if width <= 0:
        return ""
    text = clean_text(text)
    if cell_width(text) >= width:
        return truncate(text, width)
    return text + " " * (width - cell_width(text))


def format_age(seconds: float | None) -> str:
    """Compact age string: ``12s`` / ``4m`` / ``2h 14m`` / ``3d``."""
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    rm = m - h * 60
    if h < 24:
        return f"{h}h {rm:02d}m" if rm else f"{h}h"
    d = h // 24
    rh = h - d * 24
    return f"{d}d {rh}h" if rh else f"{d}d"


def state_marker(state: str | None, alarms: Sequence[str]) -> str:
    """Two-char left marker: plain-text state chip."""
    if state == "blocked" or "blocked" in alarms:
        return "!!"
    if "waiting-approval" in alarms:
        return "??"
    if "stale" in alarms:
        return "**"
    if state == "merge-ready":
        return "MR"
    if state == "working":
        return ">>"
    if state == "idle":
        return ".."
    if state == "starting":
        return ".."
    return "  "


def _pick_step(w: WorkerRow) -> str:
    if w.blocker:
        return f"blocked: {w.blocker}"
    return w.step or ""


def format_worker_row(w: WorkerRow, width: int, columns=DEFAULT_COLUMNS) -> str:
    """One flat text row for the fleet list, sized to ``width`` cells."""
    fixed_total = sum(c[1] for c in columns if c[1] > 0) + (len(columns) - 1) * 2
    # 4 = 2-char state marker + 2 spaces before the first column.
    flex_width = max(10, width - fixed_total - 4)

    marker = state_marker(w.state, w.alarms)
    kind = (w.kind or "").lower()
    role = (w.role or "").lower()
    state = (w.state or "?").lower()
    if w.review_round:
        role = f"{role} r{w.review_round}" if role else f"r{w.review_round}"
    age = format_age(w.status_age_s)
    step = _pick_step(w)

    parts: list[str] = []
    for name, col_width in columns:
        val = ""
        if name == "ticket":
            val = w.ticket
        elif name == "kind":
            val = kind
        elif name == "role":
            val = role
        elif name == "state":
            val = state
        elif name == "age":
            val = age
        elif name == "step":
            val = step
        target = col_width if col_width > 0 else flex_width
        parts.append(pad(val, target))
    return f"{marker}  " + "  ".join(parts)


def format_orch_header(rollup: OrchRollup) -> str:
    """One line summarizing an orchestrator's bucket counts."""
    return (
        f"{rollup.orch:<20}  "
        f"total {rollup.total:<2}  "
        f"working {rollup.working:<2}  "
        f"idle {rollup.idle:<2}  "
        f"mr {rollup.merge_ready:<2}  "
        f"blk {rollup.blocked:<2}  "
        f"stall {rollup.stalled_or_failed:<2}"
    )


def format_footer(hints: Sequence[tuple[str, str]], right: str = "") -> str:
    """Bottom help bar: ``key label`` pairs joined by two spaces."""
    left = "  ".join(f"{key} {label}" for key, label in hints)
    if not right:
        return left
    return left + "    " + right


def format_header(title: str, right: str = "") -> str:
    if not right:
        return title
    gap = 4
    return f"{title}{' ' * gap}{right}"


def format_generated_at(iso: str | None, now: datetime | None = None) -> str:
    dt = parse_iso(iso)
    if dt is None:
        return "?"
    ref = now or datetime.now(timezone.utc)
    delta = int((ref - dt).total_seconds())
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    return f"{delta // 3600}h"


def flatten_navigable(groups: Sequence[FleetGroup]) -> list[tuple[str, object]]:
    """Turn groups into a flat list of ``(kind, payload)`` entries.

    ``kind`` is ``"group"`` (payload = FleetGroup) or ``"worker"``
    (payload = WorkerRow). Used by both the renderer and the keymap
    reducer so selection math is shared.
    """
    out: list[tuple[str, object]] = []
    for g in groups:
        out.append(("group", g))
        for w in g.workers:
            out.append(("worker", w))
    return out
