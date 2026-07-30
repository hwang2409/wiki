"""Public response builders: timeline pages + raw event lookup.

Composes the low-level reader/cursor primitives with the classification
layer. Keeps the FastAPI endpoint one-line-per-route.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import errors as e
from .classification import timeline_event_from
from .cursor import decode_cursor, encode_cursor
from .errors import ReplayError
from .metadata import build_run_summary
from .models import ScanStats, TimelinePage
from .reader import SnapshotReader, _open_run_child_fd


def _build_timeline_page(
    fd: int,
    *,
    start_offset: int,
    start_skipping: bool,
    limit: int,
    stats: ScanStats,
    max_scan_bytes: int | None = None,
    max_line_bytes: int | None = None,
) -> TimelinePage:
    if limit <= 0:
        return TimelinePage(
            events=[],
            end_offset=start_offset,
            end_skipping=start_skipping,
            has_more=False,
        )
    limit = min(limit, e.MAX_LIMIT)
    # Resolve budgets at call time so ``mock.patch.object(replay, "X", n)``
    # actually reaches this function.
    if max_scan_bytes is None:
        max_scan_bytes = e.MAX_SCAN_BYTES
    if max_line_bytes is None:
        max_line_bytes = e.MAX_LINE_BYTES
    reader = SnapshotReader(
        fd,
        start_offset=start_offset,
        start_skipping=start_skipping,
        max_scan_bytes=max_scan_bytes,
        max_line_bytes=max_line_bytes,
        stats=stats,
    )
    events = []
    end_offset = start_offset
    end_skipping = start_skipping
    reached_limit = False
    for offset, record in reader.records():
        try:
            entry = json.loads(record)
        except ValueError:
            stats.dropped_malformed += 1
            continue
        if not isinstance(entry, dict):
            continue
        event = timeline_event_from(entry)
        if event is None:
            continue
        events.append(event)
        end_offset = offset
        end_skipping = False
        if len(events) >= limit:
            reached_limit = True
            break
    if not reached_limit:
        end_offset, end_skipping = reader.resume_state()
    has_more = end_offset < reader.snapshot_size
    return TimelinePage(
        events=events,
        end_offset=end_offset,
        end_skipping=end_skipping,
        has_more=has_more,
    )


def _build_bookmarks(
    fd: int,
    *,
    cap: int,
    stats: ScanStats,
    max_scan_bytes: int | None = None,
    max_line_bytes: int | None = None,
) -> list[dict[str, Any]]:
    if cap <= 0:
        return []
    if max_scan_bytes is None:
        max_scan_bytes = e.MAX_SCAN_BYTES
    if max_line_bytes is None:
        max_line_bytes = e.MAX_LINE_BYTES
    reader = SnapshotReader(
        fd,
        start_offset=0,
        start_skipping=False,
        max_scan_bytes=max_scan_bytes,
        max_line_bytes=max_line_bytes,
        stats=stats,
    )
    bookmarks: list[dict[str, Any]] = []
    for _offset, record in reader.records():
        try:
            entry = json.loads(record)
        except ValueError:
            stats.dropped_malformed += 1
            continue
        if not isinstance(entry, dict):
            continue
        event = timeline_event_from(entry)
        if event is None or event.bookmark is None:
            continue
        bookmarks.append(
            {
                "seq": event.seq,
                "kind": event.bookmark,
                "ts": event.ts,
                "summary": event.summary,
                "event_kind": event.kind,
            }
        )
        if len(bookmarks) >= cap:
            stats.bookmarks_truncated = True
            break
    return bookmarks


def _warnings_from(*stats: ScanStats) -> list[str]:
    dropped_oversize = sum(s.dropped_oversize for s in stats)
    dropped_malformed = sum(s.dropped_malformed for s in stats)
    dropped_tail = any(s.dropped_truncated_tail for s in stats)
    scan_truncated = any(s.scan_truncated for s in stats)
    bookmarks_truncated = any(s.bookmarks_truncated for s in stats)
    warnings: list[str] = []
    if dropped_oversize:
        warnings.append(f"dropped {dropped_oversize} oversized event line(s)")
    if dropped_malformed:
        warnings.append(f"skipped {dropped_malformed} malformed line(s)")
    if dropped_tail:
        warnings.append("trailing partial write ignored (torn-read guard)")
    if scan_truncated:
        warnings.append("scan budget reached — continuing via cursor")
    if bookmarks_truncated:
        warnings.append(f"bookmark list truncated at {e.MAX_BOOKMARKS}; more exist")
    return warnings


def build_timeline_response(
    runs_root_fd: int,
    run_id: str,
    *,
    cursor: str | None = None,
    limit: int = e.DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Full endpoint payload: meta + a window of events + bookmarks + cursor.

    The cursor is signed and bound to ``run_id`` (see ``cursor.decode_cursor``);
    a tampered or wrong-run cursor 400s. A well-formed cursor pointing past
    ``snapshot_size`` also 400s in ``SnapshotReader.__init__``.
    """

    summary = build_run_summary(runs_root_fd, run_id)
    start_offset, start_skipping = decode_cursor(cursor, run_id=run_id)
    window_stats = ScanStats()
    bookmark_stats = ScanStats()

    events_fd = _open_run_child_fd(runs_root_fd, run_id, "events.jsonl")
    try:
        page = _build_timeline_page(
            events_fd,
            start_offset=start_offset,
            start_skipping=start_skipping,
            limit=limit,
            stats=window_stats,
        )
    finally:
        os.close(events_fd)

    # Bookmarks only on the first page.
    bookmarks: list[dict[str, Any]]
    if start_offset == 0 and not start_skipping:
        events_fd = _open_run_child_fd(runs_root_fd, run_id, "events.jsonl")
        try:
            bookmarks = _build_bookmarks(
                events_fd, cap=e.MAX_BOOKMARKS, stats=bookmark_stats
            )
        finally:
            os.close(events_fd)
    else:
        bookmarks = []

    next_cursor = (
        encode_cursor(page.end_offset, run_id=run_id, skipping=page.end_skipping)
        if page.has_more
        else None
    )
    return {
        "run": summary.as_dict(),
        "events": [event.as_dict() for event in page.events],
        "next_cursor": next_cursor,
        "has_more": page.has_more,
        "bookmarks": bookmarks,
        "bookmarks_truncated": bookmark_stats.bookmarks_truncated,
        "warnings": _warnings_from(window_stats, bookmark_stats),
    }


def load_raw_event(runs_root_fd: int, run_id: str, seq: int) -> dict[str, Any] | None:
    """Return the raw.jsonl entry for ``seq`` — fd-based, byte-bounded, resumable."""

    if seq <= 0:
        return None
    start_offset = 0
    start_skipping = False
    while True:
        stats = ScanStats()
        try:
            raw_fd = _open_run_child_fd(runs_root_fd, run_id, "raw.jsonl")
        except ReplayError:
            return None
        found: dict[str, Any] | None = None
        past = False
        try:
            reader = SnapshotReader(
                raw_fd,
                start_offset=start_offset,
                start_skipping=start_skipping,
                max_scan_bytes=e.MAX_SCAN_BYTES,
                max_line_bytes=e.MAX_LINE_BYTES,
                stats=stats,
            )
            resume_offset = start_offset
            resume_skipping = start_skipping
            for offset, record in reader.records():
                try:
                    entry = json.loads(record)
                except ValueError:
                    stats.dropped_malformed += 1
                    continue
                if not isinstance(entry, dict):
                    continue
                entry_seq = entry.get("seq")
                if isinstance(entry_seq, int):
                    if entry_seq == seq:
                        found = entry
                        break
                    if entry_seq > seq:
                        past = True
                        break
            if found is None and not past:
                resume_offset, resume_skipping = reader.resume_state()
        finally:
            os.close(raw_fd)
        if found is not None:
            return found
        if past:
            return None
        if not stats.scan_truncated:
            return None
        if resume_offset <= start_offset and not (resume_skipping and not start_skipping):
            return None
        start_offset = resume_offset
        start_skipping = resume_skipping
