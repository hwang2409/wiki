"""`wiki tui` curses application.

Read-only fleet monitor built on stdlib ``curses`` + stdlib HTTP.
The heavy work — parsing dashboard payloads, computing selection, and
matching filter needles — lives in pure sibling modules so the
interactive shell here stays a thin composition layer.
"""

from __future__ import annotations

import argparse
import curses
import queue
import signal
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional

from . import client, format as fmt, keymap
from .snapshot import (
    FleetSnapshot,
    WorkerRow,
    WorkerSession,
    session_from_payload,
    snapshot_from_payload,
)


DEFAULT_BACKEND = "http://127.0.0.1:8213"
POLL_INTERVAL_S = 2.0
STALE_AFTER_S = 10.0
DETAIL_POLL_INTERVAL_S = 3.0
FALLBACK_POLL_S = 5.0


# --- Views -----------------------------------------------------------------


@dataclass
class FleetViewState:
    selection: keymap.Selection = field(default_factory=keymap.Selection)
    filter_input: str = ""

    def visible_snapshot(self, snapshot: FleetSnapshot) -> FleetSnapshot:
        return keymap.filter_snapshot(snapshot, self.selection.filter_text)


@dataclass
class DetailViewState:
    ticket: str
    session: WorkerSession | None = None
    scroll: int = 0
    follow: bool = True
    last_fetch: float = 0.0


# --- Data pump -------------------------------------------------------------


class DataPump:
    """Background thread(s) feeding the app queue with fresh snapshots."""

    def __init__(self, backend: str, out_queue: queue.Queue) -> None:
        self.backend = backend.rstrip("/")
        self.out = out_queue
        self.stop_event = threading.Event()
        self._sse_thread: Optional[client.SSEThread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._trigger = threading.Event()
        self._last_sse_ts = time.monotonic()

    def start(self) -> None:
        self._last_sse_ts = time.monotonic()
        self._sse_thread = client.start_sse_thread(
            f"{self.backend}/api/events",
            self._on_sse_event,
            self.stop_event,
        )
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="wiki-tui-poll", daemon=True
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._trigger.set()
        if self._sse_thread:
            self._sse_thread.close()
            self._sse_thread.join(timeout=1.0)
        if self._poll_thread:
            self._poll_thread.join(timeout=1.0)

    def request_refresh(self) -> None:
        self._trigger.set()

    # --- internals ---

    def _on_sse_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype in {"agents", "vault"}:
            self._last_sse_ts = time.time()
            self._trigger.set()

    def _poll_loop(self) -> None:
        # Prime immediately so the UI has data on first paint.
        self._fetch_and_publish()
        while not self.stop_event.is_set():
            elapsed = time.monotonic() - self._last_sse_ts
            wait_s = min(POLL_INTERVAL_S, max(0.0, FALLBACK_POLL_S - elapsed))
            triggered = self._trigger.wait(wait_s)
            self._trigger.clear()
            if self.stop_event.is_set():
                return
            # If SSE has been silent for FALLBACK_POLL_S while the
            # trigger did NOT fire, still refresh so we self-heal
            # when the SSE stream is dead but the UI is idle.
            if not triggered and (time.monotonic() - self._last_sse_ts) < FALLBACK_POLL_S:
                continue
            self._fetch_and_publish()

    def _fetch_and_publish(self) -> None:
        try:
            raw = client.get_json(f"{self.backend}/dashboard/data")
        except client.BackendUnavailable as exc:
            snapshot = FleetSnapshot(
                groups=(), generated_at=None, stale=True, fetch_error=str(exc)
            )
        else:
            snapshot = snapshot_from_payload(raw)
        self.out.put(("snapshot", snapshot))


class DetailPump:
    """Polls one worker's session endpoint until stopped."""

    def __init__(self, backend: str, ticket: str, out_queue: queue.Queue) -> None:
        self.backend = backend.rstrip("/")
        self.ticket = ticket
        self.out = out_queue
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name=f"wiki-tui-detail-{self.ticket}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        # first fetch fires immediately
        while not self.stop_event.is_set():
            try:
                raw = client.get_json(
                    f"{self.backend}/api/agents/{self.ticket}/session"
                )
                sess = session_from_payload(self.ticket, raw)
            except client.BackendUnavailable as exc:
                sess = WorkerSession(
                    ticket=self.ticket,
                    pr=None, state=None, step=None, blocker=None,
                    latest_verdict=None, events=(),
                    fetch_error=str(exc),
                )
            self.out.put(("session", sess))
            if self.stop_event.wait(DETAIL_POLL_INTERVAL_S):
                return


# --- Rendering -------------------------------------------------------------


BOX_H = "─"
BOX_V = "│"
TL = "┌"
TR = "┐"
BL = "└"
BR = "┘"
LEFT_T = "├"
RIGHT_T = "┤"


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Guarded addstr — curses raises on writes to the last cell."""
    if y < 0:
        return
    try:
        max_y, max_x = win.getmaxyx()
    except curses.error:
        return
    if y >= max_y or x >= max_x:
        return
    remaining = max_x - x - 1
    if remaining <= 0:
        return
    text = fmt.truncate(text, remaining)
    if not text:
        return
    try:
        win.addnstr(y, x, text, len(text), attr)
    except curses.error:
        pass


def draw_frame(win, title: str, footer: str) -> tuple[int, int, int, int]:
    """Draw the outer box; return usable region ``(top, left, bottom, right)``.

    Rows/columns returned are indices you can safely address; the frame
    lines are drawn outside that region.
    """
    max_y, max_x = win.getmaxyx()
    if max_y < 5 or max_x < 40:
        return (1, 1, max_y - 2, max_x - 2)
    win.erase()
    win.attron(curses.A_DIM)
    _safe_addstr(win, 0, 0, TL + BOX_H * (max_x - 2) + TR)
    for row in range(1, max_y - 1):
        _safe_addstr(win, row, 0, BOX_V)
        _safe_addstr(win, row, max_x - 1, BOX_V)
    _safe_addstr(win, max_y - 1, 0, BL + BOX_H * (max_x - 2) + BR)
    win.attroff(curses.A_DIM)

    # title floats on the top rule at column 2
    _safe_addstr(win, 0, 2, f" {title} ", curses.A_BOLD)

    # footer sits on the bottom rule
    if footer:
        _safe_addstr(win, max_y - 1, 2, f" {footer} ", curses.A_DIM)
    return (1, 1, max_y - 2, max_x - 2)


def render_fleet(
    win,
    snapshot: FleetSnapshot,
    view: FleetViewState,
    now: datetime,
) -> None:
    filtered = view.visible_snapshot(snapshot)
    entries = fmt.flatten_navigable(filtered.groups)
    selection = view.selection.clamp(entries)

    total_workers = filtered.total_workers()
    total_orch = len(filtered.groups)
    age = fmt.format_generated_at(snapshot.generated_at, now=now)
    stale_tag = "  (stale)" if snapshot.stale else ""
    title_right = (
        f"{total_workers} workers · {total_orch} orch"
        f" · updated {age}{stale_tag}"
    )
    title = fmt.format_header("wiki fleet", title_right)
    hints = keymap.FLEET_HINTS
    if view.selection.filter_active:
        footer = f"/{view.filter_input}_"
    else:
        footer = fmt.format_footer(hints, right=now.astimezone().strftime("%H:%M:%S"))
    top, left, bottom, right = draw_frame(win, title, footer)
    inner_width = max(1, right - left + 1)
    inner_height = max(1, bottom - top)

    if snapshot.fetch_error:
        msg = f"backend not reachable at {snapshot.fetch_error}"
        hint = "start the sidecar with `make dev` or launch Wiki.app"
        _safe_addstr(win, top + 1, left + 2, msg, curses.A_BOLD)
        _safe_addstr(win, top + 3, left + 2, hint, curses.A_DIM)
        return
    if total_workers == 0 and not snapshot.groups:
        _safe_addstr(win, top + 1, left + 2, "no workers registered", curses.A_DIM)
        return
    if total_workers == 0:
        _safe_addstr(
            win, top + 1, left + 2,
            "no workers match filter — press esc to clear",
            curses.A_DIM,
        )
        return

    # scroll window: keep selection in view
    max_rows = inner_height - 1  # leave one blank line at bottom of pane
    scroll = 0
    if selection.index >= max_rows:
        scroll = selection.index - max_rows + 1
    scroll = max(0, min(scroll, max(0, len(entries) - max_rows)))
    visible = entries[scroll : scroll + max_rows]

    for row_offset, (kind, payload) in enumerate(visible):
        row = top + row_offset
        is_selected = (scroll + row_offset) == selection.index
        if kind == "group":
            group = payload
            line = fmt.format_orch_header(group.rollup)
            attr = curses.A_BOLD | curses.A_UNDERLINE
            _safe_addstr(win, row, left + 1, fmt.pad(line, inner_width - 2), attr)
        else:
            worker = payload
            line = "  " + fmt.format_worker_row(worker, inner_width - 4)
            padded = fmt.pad(line, inner_width - 2) if fmt.cell_width(line) < inner_width - 2 else line
            attr = curses.A_REVERSE if is_selected else curses.A_NORMAL
            _safe_addstr(win, row, left + 1, padded, attr)


def render_detail(
    win,
    snapshot: FleetSnapshot,
    detail: DetailViewState,
    now: datetime,
) -> None:
    worker = snapshot.find(detail.ticket)
    session = detail.session
    state = (worker.state if worker else (session.state if session else "?")) or "?"
    role = (worker.role or "") if worker else ""
    kind = (worker.kind or "") if worker else ""
    orch = (worker.orch or "") if worker else ""
    parts = [detail.ticket]
    if orch:
        parts.append(orch)
    if kind or role:
        parts.append(f"{kind or '?'}/{role or '?'}")
    parts.append(state)
    if worker and worker.review_round:
        parts.append(f"r{worker.review_round}")
    title = fmt.format_header(" · ".join(parts))

    footer_right = now.astimezone().strftime("%H:%M:%S")
    if detail.follow:
        follow_tag = "follow ON"
    else:
        follow_tag = "follow OFF"
    footer = fmt.format_footer(keymap.DETAIL_HINTS, right=f"{follow_tag}    {footer_right}")

    top, left, bottom, right = draw_frame(win, title, footer)
    inner_width = max(1, right - left + 1)
    inner_height = max(1, bottom - top)

    row = top
    step = (worker.step if worker else (session.step if session else "")) or "—"
    pr = (worker.pr if worker else (session.pr if session else None)) or "—"
    blocker = (worker.blocker if worker else (session.blocker if session else None))
    age = fmt.format_age(worker.status_age_s) if worker else "—"
    worktree = worker.worktree if worker else None

    header_lines: list[tuple[str, str, int]] = [
        ("step   ", fmt.truncate(step, inner_width - 10), 0),
        ("pr     ", pr, 0),
        ("age    ", age, 0),
    ]
    if worktree:
        header_lines.append(("path   ", fmt.truncate(worktree, inner_width - 10), curses.A_DIM))
    if blocker:
        header_lines.append(("blocker", blocker, curses.A_BOLD))

    for label, val, attr in header_lines:
        _safe_addstr(win, row, left + 1, label, curses.A_DIM)
        _safe_addstr(win, row, left + 9, val, attr)
        row += 1

    # verdict pane
    row += 1
    _safe_addstr(win, row, left + 1, "latest verdict", curses.A_DIM | curses.A_UNDERLINE)
    row += 1
    verdict = session.latest_verdict if session else None
    if verdict:
        _safe_addstr(win, row, left + 1, fmt.truncate(verdict, inner_width - 2))
    else:
        _safe_addstr(win, row, left + 1, "—", curses.A_DIM)
    row += 2

    # transcript tail
    _safe_addstr(win, row, left + 1, "transcript (latest last)", curses.A_DIM | curses.A_UNDERLINE)
    row += 1

    tail_top = row
    tail_bottom = bottom - 1
    tail_height = max(1, tail_bottom - tail_top + 1)

    events = list(session.events) if session else []
    if session and session.fetch_error and not events:
        _safe_addstr(win, tail_top, left + 1, f"session unavailable: {session.fetch_error}", curses.A_DIM)
        return
    if not events:
        _safe_addstr(win, tail_top, left + 1, "waiting for events…", curses.A_DIM)
        return

    if detail.follow:
        window_start = max(0, len(events) - tail_height)
    else:
        window_start = max(0, min(detail.scroll, len(events) - 1))
    visible = events[window_start : window_start + tail_height]
    for i, ev in enumerate(visible):
        ts_short = _format_time_only(ev.ts)
        label = fmt.pad(ev.label, 12)
        prefix = f"{ts_short}  {label}  "
        text_room = inner_width - fmt.cell_width(prefix) - 2
        text = fmt.truncate((ev.text or "").replace("\n", " ⏎ "), max(1, text_room))
        _safe_addstr(win, tail_top + i, left + 1, prefix, curses.A_DIM)
        _safe_addstr(win, tail_top + i, left + 1 + fmt.cell_width(prefix), text)


def render_help(win) -> None:
    top, left, bottom, right = draw_frame(win, "help", "esc close")
    lines = [
        "fleet view",
        "  j / k        move worker cursor (skips group headers)",
        "  g / G        jump to first / last worker",
        "  enter        open selected worker",
        "  /            filter by ticket / orch / step (esc to clear)",
        "  r            force refresh",
        "  q / ctrl-c   quit",
        "",
        "worker detail",
        "  j / k        scroll transcript (turns off follow)",
        "  g / G        jump top / end of transcript",
        "  f            toggle follow mode",
        "  o            open PR in browser (if present)",
        "  esc          back to fleet",
        "",
        "chrome",
        "  the header shows worker + orch counts and data age",
        "  '(stale)' means the last fetch failed — polling continues",
        "  row marker: !! blocked  ?? waiting  ** stale  MR merge-ready  >> working  .. idle",
    ]
    for i, line in enumerate(lines):
        _safe_addstr(win, top + i, left + 2, line, curses.A_BOLD if i in (0, 8, 15) else 0)


def _format_time_only(iso: str | None) -> str:
    if not iso:
        return "        "
    from .snapshot import parse_iso
    dt = parse_iso(iso)
    if dt is None:
        return "        "
    return dt.astimezone().strftime("%H:%M:%S")


# --- App -------------------------------------------------------------------


class App:
    def __init__(self, backend: str) -> None:
        self.backend = backend.rstrip("/")
        self.snapshot = FleetSnapshot(groups=(), generated_at=None, stale=False)
        self.snapshot_ts = 0.0
        self.fleet = FleetViewState()
        self.detail: Optional[DetailViewState] = None
        self.help_open = False
        self.q: queue.Queue = queue.Queue()
        self.pump = DataPump(self.backend, self.q)
        self.detail_pump: Optional[DetailPump] = None
        self._quit = False

    # --- lifecycle ---

    def run(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        self.pump.start()
        try:
            while not self._quit:
                self._drain_queue()
                self._check_snapshot_stale()
                self._render(stdscr)
                self._handle_input(stdscr)
        finally:
            self.pump.stop()
            if self.detail_pump:
                self.detail_pump.stop()

    def _check_snapshot_stale(self) -> None:
        if self.snapshot_ts == 0:
            return
        if (time.time() - self.snapshot_ts) > STALE_AFTER_S and not self.snapshot.stale:
            self.snapshot = replace(self.snapshot, stale=True)

    def _drain_queue(self) -> None:
        drained = 0
        while drained < 32:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                return
            drained += 1
            if kind == "snapshot":
                self.snapshot = payload
                if not payload.fetch_error:
                    self.snapshot_ts = time.time()
            elif kind == "session" and self.detail and payload.ticket == self.detail.ticket:
                self.detail.session = payload

    def _render(self, stdscr) -> None:
        now = datetime.now(timezone.utc)
        if self.help_open:
            render_help(stdscr)
        elif self.detail:
            render_detail(stdscr, self.snapshot, self.detail, now)
        else:
            render_fleet(stdscr, self.snapshot, self.fleet, now)
        stdscr.refresh()

    # --- input ---

    def _handle_input(self, stdscr) -> None:
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            self._quit = True
            return
        if ch == -1:
            return

        if self.help_open:
            if ch in (27, ord("q"), ord("?")):
                self.help_open = False
            return

        if self.detail is not None:
            self._handle_detail_key(ch)
            return
        self._handle_fleet_key(ch)

    def _handle_fleet_key(self, ch: int) -> None:
        view = self.fleet
        if view.selection.filter_active:
            if ch == 27:
                view.selection = replace(view.selection, filter_active=False, filter_text="")
                view.filter_input = ""
                return
            if ch in (10, 13, curses.KEY_ENTER):
                view.selection = replace(
                    view.selection,
                    filter_active=False,
                    filter_text=view.filter_input.strip(),
                )
                return
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                view.filter_input = view.filter_input[:-1]
                view.selection = replace(view.selection, filter_text=view.filter_input.strip())
                return
            if 32 <= ch < 127:
                view.filter_input += chr(ch)
                view.selection = replace(view.selection, filter_text=view.filter_input.strip())
                return
            return

        if ch in (ord("q"), 3):  # 3 == ctrl-c
            self._quit = True
            return
        if ch == ord("?"):
            self.help_open = True
            return
        if ch == ord("r"):
            self.pump.request_refresh()
            return
        if ch == ord("/"):
            view.selection = replace(view.selection, filter_active=True)
            view.filter_input = view.selection.filter_text or ""
            return

        filtered = view.visible_snapshot(self.snapshot)
        entries = fmt.flatten_navigable(filtered.groups)
        if not entries:
            return
        selection = view.selection.clamp(entries)

        if ch in (curses.KEY_DOWN, ord("j")):
            view.selection = keymap.move(selection, entries, 1)
        elif ch in (curses.KEY_UP, ord("k")):
            view.selection = keymap.move(selection, entries, -1)
        elif ch == curses.KEY_NPAGE:
            view.selection = keymap.move(selection, entries, 10)
        elif ch == curses.KEY_PPAGE:
            view.selection = keymap.move(selection, entries, -10)
        elif ch == ord("g"):
            view.selection = replace(selection, index=keymap.jump_top(entries))
        elif ch == ord("G"):
            view.selection = replace(selection, index=keymap.jump_end(entries))
        elif ch in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            worker = keymap.selected_worker(view.selection, filtered)
            if worker is not None:
                self._enter_detail(worker.ticket)

    def _handle_detail_key(self, ch: int) -> None:
        detail = self.detail
        if detail is None:
            return
        if ch in (27, curses.KEY_LEFT, curses.KEY_BACKSPACE):
            self._exit_detail()
            return
        if ch == ord("q") or ch == 3:
            self._quit = True
            return
        if ch == ord("?"):
            self.help_open = True
            return
        if ch == ord("f"):
            if detail.follow:
                detail.follow = False
                if detail.session:
                    detail.scroll = max(0, len(detail.session.events) - 1)
            else:
                detail.follow = True
            if detail.follow and detail.session:
                detail.scroll = max(0, len(detail.session.events) - 1)
            return
        if ch == ord("o") and detail.session and detail.session.pr:
            try:
                webbrowser.open(detail.session.pr)
            except Exception:
                pass
            return
        if ch in (curses.KEY_DOWN, ord("j")):
            if detail.follow:
                detail.follow = False
                if detail.session:
                    detail.scroll = max(0, len(detail.session.events) - 1)
            detail.scroll += 1
        elif ch in (curses.KEY_UP, ord("k")):
            if detail.follow:
                detail.follow = False
                if detail.session:
                    detail.scroll = max(0, len(detail.session.events) - 1)
            detail.scroll = max(0, detail.scroll - 1)
        elif ch == curses.KEY_NPAGE:
            if detail.follow:
                detail.follow = False
                if detail.session:
                    detail.scroll = max(0, len(detail.session.events) - 1)
            detail.scroll += 10
        elif ch == curses.KEY_PPAGE:
            if detail.follow:
                detail.follow = False
                if detail.session:
                    detail.scroll = max(0, len(detail.session.events) - 1)
            detail.scroll = max(0, detail.scroll - 10)
        elif ch == ord("g"):
            detail.follow = False
            detail.scroll = 0
        elif ch == ord("G"):
            detail.follow = True

    def _enter_detail(self, ticket: str) -> None:
        self.detail = DetailViewState(ticket=ticket)
        self.detail_pump = DetailPump(self.backend, ticket, self.q)
        self.detail_pump.start()

    def _exit_detail(self) -> None:
        if self.detail_pump:
            self.detail_pump.stop()
            self.detail_pump = None
        self.detail = None


# --- Entrypoint ------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wiki tui",
        description="Read-only terminal fleet monitor for the local wiki sidecar.",
    )
    parser.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        help=f"Sidecar base URL (default: {DEFAULT_BACKEND})",
    )
    return parser.parse_args(argv)


def _preflight(backend: str) -> Optional[str]:
    """Return ``None`` when the sidecar answers; else a friendly reason."""
    try:
        client.get_json(f"{backend.rstrip('/')}/dashboard/data", timeout=2.0)
    except client.BackendUnavailable as exc:
        return exc.reason
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Preflight isn't a hard gate — the UI can render its own "backend
    # not reachable" state and keep polling. We only print an early
    # hint so `wiki tui` in a terminal without a sidecar gets a clean
    # message before entering curses.
    unreachable = _preflight(args.backend)
    if unreachable:
        sys.stderr.write(
            f"wiki tui: sidecar at {args.backend} unreachable — {unreachable}\n"
            f"          starting anyway; will connect when it comes up (ctrl-c to quit)\n"
        )
    app = App(args.backend)
    def _handle_shutdown(_signum, _frame) -> None:
        app._quit = True
        raise KeyboardInterrupt

    previous_handlers = {}
    for signal_name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            previous_handlers[signum] = signal.signal(signum, _handle_shutdown)
    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
