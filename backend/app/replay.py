"""Session replay timeline builder for archived agent runs (WIKI-174).

Round-3 review feedback drove a fd-first rewrite:

* Every file we read is opened through ``pathwalk.open_relative_file`` against
  a pre-opened runs-root descriptor. A symlink swap at any path component
  (leaf or intermediate) fails the open, closing the check-then-open TOCTTOU
  gap that resolve-then-open guards can't cover.
* Reads always happen through the fd; the reader NEVER re-derives a path
  from a string mid-request. Once we hold the fd, we read from that inode
  regardless of what happens on disk.
* ``events.jsonl`` and ``raw.jsonl`` reads go through
  ``stream_snapshot_records`` — a single-pass, inline skip-state machine
  that counts EVERY byte read against a scan budget, keeps oversized
  records from being buffered into RAM, and preserves records after a
  skipped oversized line (round-2 lost them).
* ``run.json`` metadata is size-capped before reading — the round-2 code
  called ``Path.read_text`` with no ceiling.
* Timeline pages resume from an opaque base64url cursor that encodes the
  byte offset at which to resume ``os.lseek``. Callers no longer re-scan
  from byte zero every page.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Any, Iterator

from .pathwalk import open_relative_directory, open_relative_file

RUN_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Bookmark rendering budget: keep the count small so a scrubber's tick track
# stays legible on a 480px panel even for long runs. When we hit this we
# surface ``bookmarks_truncated`` in the response so nothing is silently
# hidden (round-3 review item 5).
MAX_BOOKMARKS = 200
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000

# Per-line hard ceiling. Real supervisor events observed on disk are ~200 B –
# 30 KiB; anything above 1 MiB is corruption or an attack and the reader
# discards it wholesale instead of buffering.
MAX_LINE_BYTES = 1 * 1024 * 1024

# Per-scan hard ceiling. Every byte physically read counts — including bytes
# consumed while skipping an oversized record — so a 100 GiB file terminates
# in constant peak RSS. Surfaced as ``scan_truncated`` in warnings.
MAX_SCAN_BYTES = 64 * 1024 * 1024

# Per-run.json read cap. The largest real ``run.json`` seen in the runtime
# directory is ~14 KiB even with a very long ``initial_prompt``. 256 KiB
# gives ample headroom without letting a corrupt or adversarial run.json
# drive unbounded metadata reads (round-3 review item 1).
MAX_RUN_JSON_BYTES = 256 * 1024

# ``_resolve_ticket_runs`` outer caps: sort the whole runs dir by mtime, then
# read at most this many run.json files to filter by ticket. Surfaced as
# ``runs_truncated`` so the UI can note when older matches were skipped.
MAX_RUN_LIST_SCAN = 500
MAX_RUN_LIST_ENTRIES = 200

# Chunked stream reads: 128 KiB balances syscall overhead against per-response
# RSS. We never buffer more than one chunk beyond the current partial record.
_STREAM_CHUNK = 128 * 1024

MERGE_READY_PATTERN = re.compile(r"\b(MERGE-READY|BLOCKED)\s*:", re.IGNORECASE)


class ReplayError(Exception):
    """Raised when a run directory is unreadable or malformed."""


@dataclass(frozen=True)
class TimelineEvent:
    seq: int
    raw_seq: int
    ts: str | None
    kind: str
    disposition: str
    lifecycle_state: str | None
    summary: str
    bookmark: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "raw_seq": self.raw_seq,
            "ts": self.ts,
            "kind": self.kind,
            "disposition": self.disposition,
            "lifecycle_state": self.lifecycle_state,
            "summary": self.summary,
            "bookmark": self.bookmark,
        }


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    agent_id: str | None
    orch_id: str | None
    role: str | None
    provider: str | None
    model: str | None
    outcome: str | None
    state: str | None
    created_at: str | None
    updated_at: str | None
    total_events: int
    initial_prompt_excerpt: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "orch_id": self.orch_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_events": self.total_events,
            "initial_prompt_excerpt": self.initial_prompt_excerpt,
        }


@dataclass
class _ScanStats:
    dropped_oversize: int = 0
    dropped_malformed: int = 0
    dropped_truncated_tail: bool = False
    scan_truncated: bool = False
    bookmarks_truncated: bool = False


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_PATTERN.fullmatch(run_id))


# ---------------------------------------------------------------------------
# Opaque byte cursor (round-3 review item 4)
# ---------------------------------------------------------------------------


def encode_cursor(byte_offset: int) -> str:
    """Encode a byte offset into an opaque URL-safe token.

    Client code MUST NOT parse this — the encoding may change. The point is
    the round-1/round-2 ``after_seq`` cursor forced the server to rescan
    from byte zero every page; a byte cursor lets us ``lseek`` straight to
    the resume point.
    """

    payload = json.dumps({"o": int(byte_offset)}, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ReplayError("invalid cursor") from exc
    if not isinstance(value, dict):
        raise ReplayError("invalid cursor")
    offset = value.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise ReplayError("invalid cursor")
    return offset


# ---------------------------------------------------------------------------
# Bounded fd-based reader (round-3 review items 1 + 2 + 3)
# ---------------------------------------------------------------------------


def _open_run_child_fd(runs_root_fd: int, run_id: str, filename: str) -> int:
    """Open ``<runs_root>/<run_id>/<filename>`` via the dir-fd walker.

    ``open_relative_file`` refuses a symlink at ANY component so a swap in
    the run dir (or in the child file) fails the open — closing the TOCTTOU
    gap in the round-2 resolve-then-open guard.
    """

    return open_relative_file(runs_root_fd, (run_id, filename))


def _fstat_regular_or_raise(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ReplayError("expected a regular file")
    return info


def stream_snapshot_records(
    fd: int,
    *,
    start_offset: int = 0,
    max_scan_bytes: int = MAX_SCAN_BYTES,
    max_line_bytes: int = MAX_LINE_BYTES,
    stats: _ScanStats,
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(end_offset, record_bytes)`` for each newline-terminated record.

    ``end_offset`` is the byte position immediately AFTER the record's
    trailing newline. Callers pass it back as ``start_offset`` on the next
    page to resume without a rescan.

    Bounds enforced (round-3 review item 2):

    * Every byte physically read from ``fd`` counts against
      ``max_scan_bytes`` — including bytes consumed while walking past an
      oversized record. An earlier version accounted only for bytes that
      were yielded, which let an oversized-line attacker bypass the budget.
    * Records over ``max_line_bytes`` are skipped by advancing through the
      stream until the terminating newline; bytes are NEVER buffered past
      the ceiling.
    * Records after a skipped oversized record are preserved — the earlier
      fast-skip loop's edge case could drop the record immediately after a
      skipped line if the newline sat at the end of the read chunk.

    The reader assumes a size snapshot: ``os.fstat`` at open time, read
    exactly that many bytes, drop any un-newline-terminated tail. That is
    the WIKI-174 round-2 torn-write guard, preserved here.
    """

    info = _fstat_regular_or_raise(fd)
    snapshot_size = int(info.st_size)
    if start_offset < 0:
        start_offset = 0
    if start_offset >= snapshot_size:
        return
    os.lseek(fd, start_offset, os.SEEK_SET)

    chunk_start = start_offset
    total_bytes_read = 0
    buffer = bytearray()
    accumulated_line_bytes = 0
    skipping_oversize = False
    remaining_in_snapshot = snapshot_size - start_offset

    while remaining_in_snapshot > 0:
        to_read = min(_STREAM_CHUNK, remaining_in_snapshot)
        chunk = os.read(fd, to_read)
        if not chunk:
            break
        remaining_in_snapshot -= len(chunk)
        total_bytes_read += len(chunk)
        if total_bytes_read > max_scan_bytes:
            stats.scan_truncated = True
            return
        offset = 0
        while offset < len(chunk):
            nl = chunk.find(b"\n", offset)
            if nl < 0:
                slice_len = len(chunk) - offset
                accumulated_line_bytes += slice_len
                if not skipping_oversize and accumulated_line_bytes > max_line_bytes:
                    # This partial record just crossed the ceiling.
                    # Drop what we've buffered and flip into skip mode; the
                    # terminating newline (which may be many chunks away)
                    # will exit skip mode.
                    stats.dropped_oversize += 1
                    skipping_oversize = True
                    buffer.clear()
                if not skipping_oversize:
                    buffer.extend(chunk[offset:])
                offset = len(chunk)
                continue

            record_end_position = chunk_start + nl + 1
            if skipping_oversize:
                # This newline terminates the oversized record. Reset and
                # keep processing the rest of the chunk — the very next
                # record is the one round-2 lost.
                skipping_oversize = False
                accumulated_line_bytes = 0
                offset = nl + 1
                continue

            slice_len = nl - offset
            accumulated_line_bytes += slice_len
            if accumulated_line_bytes > max_line_bytes:
                # Whole record fit in one chunk but the accumulated span is
                # over ceiling (edge case: partial from prior chunk + this
                # slice > ceiling AND newline came within this slice).
                stats.dropped_oversize += 1
                buffer.clear()
                accumulated_line_bytes = 0
                offset = nl + 1
                continue

            if buffer:
                buffer.extend(chunk[offset:nl])
                record = bytes(buffer)
                buffer.clear()
            else:
                record = bytes(chunk[offset:nl])
            accumulated_line_bytes = 0
            offset = nl + 1
            yield record_end_position, record

        chunk_start += len(chunk)

    if buffer or skipping_oversize:
        # Trailing bytes with no terminating newline: torn write or an
        # oversized record whose \n sits outside the snapshot window.
        stats.dropped_truncated_tail = True


def stream_json_events(
    fd: int,
    *,
    start_offset: int = 0,
    max_scan_bytes: int = MAX_SCAN_BYTES,
    max_line_bytes: int = MAX_LINE_BYTES,
    stats: _ScanStats,
) -> Iterator[tuple[int, dict[str, Any]]]:
    for end_offset, record in stream_snapshot_records(
        fd,
        start_offset=start_offset,
        max_scan_bytes=max_scan_bytes,
        max_line_bytes=max_line_bytes,
        stats=stats,
    ):
        if not record.strip():
            continue
        try:
            value = json.loads(record)
        except ValueError:
            stats.dropped_malformed += 1
            continue
        if isinstance(value, dict):
            yield end_offset, value


# ---------------------------------------------------------------------------
# run.json metadata (bounded)
# ---------------------------------------------------------------------------


def _read_bounded_metadata(runs_root_fd: int, run_id: str) -> dict[str, Any]:
    try:
        fd = _open_run_child_fd(runs_root_fd, run_id, "run.json")
    except FileNotFoundError as exc:
        raise ReplayError(f"run.json missing for {run_id}") from exc
    except OSError as exc:
        raise ReplayError(f"could not open run.json: {exc}") from exc
    try:
        info = _fstat_regular_or_raise(fd)
        if info.st_size > MAX_RUN_JSON_BYTES:
            raise ReplayError(
                f"run.json exceeds {MAX_RUN_JSON_BYTES}-byte ceiling"
            )
        chunks: list[bytes] = []
        remaining = MAX_RUN_JSON_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_RUN_JSON_BYTES:
            raise ReplayError(
                f"run.json exceeds {MAX_RUN_JSON_BYTES}-byte ceiling"
            )
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ReplayError(f"run.json is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayError("run.json must contain an object")
    return value


def _excerpt(text: str, limit: int = 120) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _first_text_block(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text
        if block_type == "tool_use":
            name = block.get("name")
            if isinstance(name, str):
                return f"tool_use: {name}"
        if block_type == "tool_result":
            inner = block.get("content")
            inner_text = _first_text_block(inner)
            if inner_text:
                marker = " (error)" if block.get("is_error") else ""
                return f"tool_result{marker}: {inner_text}"
    return None


def _run_summary_from_meta(meta: dict[str, Any], fallback_run_id: str) -> RunSummary:
    initial_prompt = meta.get("initial_prompt")
    initial_excerpt = (
        _excerpt(initial_prompt, 240)
        if isinstance(initial_prompt, str) and initial_prompt.strip()
        else None
    )
    total_events = meta.get("normalized_event_count")
    if not isinstance(total_events, int):
        total_events = meta.get("raw_event_count") or 0
    return RunSummary(
        run_id=str(meta.get("run_id") or fallback_run_id),
        agent_id=meta.get("agent_id") if isinstance(meta.get("agent_id"), str) else None,
        orch_id=meta.get("orchestrator_id") if isinstance(meta.get("orchestrator_id"), str) else None,
        role=meta.get("role") if isinstance(meta.get("role"), str) else None,
        provider=meta.get("provider") if isinstance(meta.get("provider"), str) else None,
        model=meta.get("model") if isinstance(meta.get("model"), str) else None,
        outcome=meta.get("outcome") if isinstance(meta.get("outcome"), str) else None,
        state=meta.get("state") if isinstance(meta.get("state"), str) else None,
        created_at=meta.get("created_at") if isinstance(meta.get("created_at"), str) else None,
        updated_at=meta.get("updated_at") if isinstance(meta.get("updated_at"), str) else None,
        total_events=int(total_events),
        initial_prompt_excerpt=initial_excerpt,
    )


def build_run_summary(runs_root_fd: int, run_id: str) -> RunSummary:
    meta = _read_bounded_metadata(runs_root_fd, run_id)
    return _run_summary_from_meta(meta, run_id)


# ---------------------------------------------------------------------------
# Bookmark + summary derivation (unchanged logic, extracted from round-2)
# ---------------------------------------------------------------------------


def _codex_item(payload: dict[str, Any]) -> dict[str, Any] | None:
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    return item if isinstance(item, dict) else None


def _codex_item_text(item: dict[str, Any]) -> str | None:
    content = item.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text
    summary = item.get("summary")
    if isinstance(summary, list):
        for entry in summary:
            if isinstance(entry, dict):
                text = entry.get("text") or entry.get("summary")
                if isinstance(text, str) and text.strip():
                    return text
            elif isinstance(entry, str) and entry.strip():
                return entry
    return None


def _classify_bookmark(kind: str, payload: dict[str, Any], text: str | None) -> str | None:
    if kind == "item_completed":
        item = _codex_item(payload) or {}
        item_type = item.get("type")
        if item_type == "userMessage":
            item_text = _codex_item_text(item)
            if item_text and item_text.strip():
                return "steer"
        elif item_type == "agentMessage":
            item_text = _codex_item_text(item) or text
            if item_text and MERGE_READY_PATTERN.search(item_text):
                return "verdict"
        elif item_type == "commandExecution":
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                return "error"
    if kind == "turn_completed":
        params = payload.get("params") or {}
        turn = params.get("turn") if isinstance(params, dict) else None
        if isinstance(turn, dict):
            status = turn.get("status")
            error_obj = turn.get("error")
            if status == "failed" or error_obj is not None:
                return "error"
            if status == "completed":
                return "verdict"
        return None
    if kind in {"error", "codex_error", "provider_protocol_error"}:
        return "error"
    if kind == "provider_process_exit":
        exit_code: Any = payload.get("exit_code")
        params = payload.get("params")
        if isinstance(params, dict):
            exit_code = params.get("returncode", exit_code)
        if isinstance(exit_code, int) and exit_code != 0:
            return "error"
        return None
    if kind == "claude_result":
        if payload.get("is_error"):
            return "error"
        return "verdict"
    if payload.get("is_error") is True:
        return "error"
    if kind == "claude_user":
        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            has_user_input = False
            if isinstance(content, str) and content.strip():
                has_user_input = True
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") != "tool_result":
                        has_user_input = True
                        break
            if has_user_input:
                return "steer"
    if text and MERGE_READY_PATTERN.search(text):
        return "verdict"
    return None


def _summarize_payload(kind: str, payload: dict[str, Any]) -> tuple[str, str | None]:
    if kind == "claude_assistant" or kind == "claude_user":
        message = payload.get("message")
        if isinstance(message, dict):
            text = _first_text_block(message.get("content"))
            if text:
                return _excerpt(text), text
        return kind, None
    if kind == "claude_stream_event":
        event = payload.get("event")
        if isinstance(event, dict):
            event_type = event.get("type") or "stream_event"
            block = event.get("content_block")
            if isinstance(block, dict):
                block_type = block.get("type")
                name = block.get("name")
                if block_type == "tool_use" and isinstance(name, str):
                    return f"stream {event_type}: tool_use {name}", None
                if block_type:
                    return f"stream {event_type}: {block_type}", None
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type"):
                return f"stream {event_type}: {delta.get('type')}", None
            return f"stream {event_type}", None
        return "stream_event", None
    if kind in {"claude_hook_started", "claude_hook_response"}:
        return f"hook {payload.get('hook_name') or payload.get('hook_event') or 'unknown'}", None
    if kind == "claude_status":
        status = payload.get("status") or payload.get("state") or payload.get("session_state")
        return f"status {status or 'unknown'}", None
    if kind == "claude_client_message":
        request = payload.get("request") or {}
        subtype = request.get("subtype") if isinstance(request, dict) else None
        return f"client {payload.get('type') or 'message'}{': ' + subtype if subtype else ''}", None
    if kind == "claude_control_response":
        response = payload.get("response") or {}
        subtype = response.get("subtype") if isinstance(response, dict) else None
        return f"control_response{': ' + subtype if subtype else ''}", None
    if kind == "claude_result":
        subtype = payload.get("subtype") or ""
        marker = " (error)" if payload.get("is_error") else ""
        return f"result {subtype}{marker}".strip(), None
    if kind == "claude_init":
        model = payload.get("model")
        return f"init model={model}" if model else "init", None
    if kind == "claude_thinking_tokens":
        tokens = payload.get("output_tokens") or payload.get("thinking_tokens")
        return f"thinking tokens={tokens}" if tokens else "thinking", None
    if kind == "claude_rate_limit_event":
        return "rate_limit_event", None
    if kind == "provider_process_exit":
        code: Any = payload.get("exit_code")
        params = payload.get("params")
        if isinstance(params, dict):
            code = params.get("returncode", code)
        return (
            f"provider_process_exit code={code}"
            if code is not None
            else "provider_process_exit",
            None,
        )
    if kind == "provider_stderr" or kind == "codex_stderr":
        text = payload.get("text") or payload.get("stderr") or ""
        return _excerpt(text) if isinstance(text, str) else kind, None
    if kind == "item_started" or kind == "item_completed":
        item = _codex_item(payload) or {}
        item_type = item.get("type") or "item"
        text = _codex_item_text(item)
        stem = f"codex {kind.replace('_', '/')}: {item_type}"
        if text:
            return f"{stem} — {_excerpt(text, 100)}", text
        return stem, None
    if kind == "turn_started" or kind == "turn_completed":
        params = payload.get("params") or {}
        turn = params.get("turn") if isinstance(params, dict) else {}
        if isinstance(turn, dict):
            status = turn.get("status")
            duration = turn.get("durationMs")
            bits = [f"codex turn/{kind.split('_', 1)[1]}"]
            if status:
                bits.append(str(status))
            if duration:
                bits.append(f"{int(duration)}ms")
            return " ".join(bits), None
        return f"codex turn/{kind.split('_', 1)[1]}", None
    if kind == "warning":
        params = payload.get("params") or {}
        message = params.get("message") if isinstance(params, dict) else None
        if isinstance(message, str) and message.strip():
            return f"warning: {_excerpt(message, 120)}", None
        return "warning", None
    if kind == "codex_client_message" or kind == "rpc_response":
        method = payload.get("method") or "response"
        return f"codex {method}", None
    if kind == "approval" or kind == "approval_cancelled" or kind == "approval_resolved":
        request = payload.get("request")
        subtype = request.get("subtype") if isinstance(request, dict) else None
        return f"{kind}{': ' + subtype if subtype else ''}", None
    if kind == "artifact":
        artifact = payload.get("artifact") or {}
        kind_hint = artifact.get("kind") if isinstance(artifact, dict) else None
        return f"artifact {kind_hint}" if kind_hint else "artifact", None
    return kind, None


def _timeline_event_from(entry: dict[str, Any]) -> TimelineEvent | None:
    seq = entry.get("seq")
    if not isinstance(seq, int):
        return None
    raw_seq = entry.get("raw_seq") if isinstance(entry.get("raw_seq"), int) else seq
    kind = entry.get("kind") if isinstance(entry.get("kind"), str) else "unknown"
    disposition = (
        entry.get("disposition") if isinstance(entry.get("disposition"), str) else "unknown"
    )
    lifecycle = entry.get("lifecycle_state")
    lifecycle_state = lifecycle if isinstance(lifecycle, str) else None
    ts = entry.get("normalized_at")
    ts = ts if isinstance(ts, str) else None
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    summary, text_hint = _summarize_payload(kind, payload)
    bookmark = _classify_bookmark(kind, payload, text_hint)
    return TimelineEvent(
        seq=seq,
        raw_seq=int(raw_seq),
        ts=ts,
        kind=kind,
        disposition=disposition,
        lifecycle_state=lifecycle_state,
        summary=summary,
        bookmark=bookmark,
    )


# ---------------------------------------------------------------------------
# Timeline + bookmark builders — fd-based
# ---------------------------------------------------------------------------


@dataclass
class TimelinePage:
    events: list[TimelineEvent]
    end_offset: int
    has_more: bool


def _build_timeline_page(
    fd: int,
    *,
    start_offset: int,
    limit: int,
    stats: _ScanStats,
) -> TimelinePage:
    if limit <= 0:
        return TimelinePage(events=[], end_offset=start_offset, has_more=False)
    limit = min(limit, MAX_LIMIT)
    if start_offset < 0:
        start_offset = 0
    snapshot_size = int(_fstat_regular_or_raise(fd).st_size)
    events: list[TimelineEvent] = []
    end_offset = start_offset
    for offset, entry in stream_json_events(
        fd, start_offset=start_offset, stats=stats
    ):
        end_offset = offset
        event = _timeline_event_from(entry)
        if event is None:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    has_more = end_offset < snapshot_size and not stats.scan_truncated
    return TimelinePage(events=events, end_offset=end_offset, has_more=has_more)


def _build_bookmarks(fd: int, *, cap: int, stats: _ScanStats) -> list[dict[str, Any]]:
    if cap <= 0:
        return []
    bookmarks: list[dict[str, Any]] = []
    for _offset, entry in stream_json_events(fd, start_offset=0, stats=stats):
        event = _timeline_event_from(entry)
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


def _warnings_from(*stats: _ScanStats) -> list[str]:
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
        warnings.append("scan hit byte budget — later events not classified")
    if bookmarks_truncated:
        warnings.append(f"bookmark list truncated at {MAX_BOOKMARKS}; more exist")
    return warnings


def build_timeline_response(
    runs_root_fd: int,
    run_id: str,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Full endpoint payload: meta + a window of events + bookmarks + cursor.

    Every file read happens through an fd rooted at ``runs_root_fd`` via the
    dir-fd walker, so a symlink swap anywhere along ``<run_id>/*`` fails the
    open rather than following into an attacker-chosen path.
    """

    summary = build_run_summary(runs_root_fd, run_id)
    start_offset = decode_cursor(cursor)
    window_stats = _ScanStats()
    bookmark_stats = _ScanStats()

    events_fd = _open_run_child_fd(runs_root_fd, run_id, "events.jsonl")
    try:
        page = _build_timeline_page(
            events_fd,
            start_offset=start_offset,
            limit=limit,
            stats=window_stats,
        )
    finally:
        os.close(events_fd)

    # Bookmarks scan only on the first page — re-scanning per page would
    # dominate the wire cost and give identical results.
    bookmarks: list[dict[str, Any]]
    if start_offset == 0:
        events_fd = _open_run_child_fd(runs_root_fd, run_id, "events.jsonl")
        try:
            bookmarks = _build_bookmarks(
                events_fd, cap=MAX_BOOKMARKS, stats=bookmark_stats
            )
        finally:
            os.close(events_fd)
    else:
        bookmarks = []

    next_cursor = encode_cursor(page.end_offset) if page.has_more else None
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
    """Return the raw.jsonl entry for ``seq`` — fd-based, byte-bounded."""

    if seq <= 0:
        return None
    stats = _ScanStats()
    try:
        raw_fd = _open_run_child_fd(runs_root_fd, run_id, "raw.jsonl")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        for _offset, entry in stream_json_events(raw_fd, start_offset=0, stats=stats):
            entry_seq = entry.get("seq")
            if isinstance(entry_seq, int) and entry_seq == seq:
                return entry
            if isinstance(entry_seq, int) and entry_seq > seq:
                return None
    finally:
        os.close(raw_fd)
    return None


# ---------------------------------------------------------------------------
# Ticket → runs discovery — bounded (round-3 review item 1 tail)
# ---------------------------------------------------------------------------


@dataclass
class TicketRunsListing:
    runs: list[RunSummary]
    truncated: bool = False


def _iter_root_entries(runs_root_fd: int) -> list[tuple[str, float]]:
    """Return ``(name, mtime)`` for regular subdirs of the runs root, sorted newest first.

    Symlinked entries are silently skipped: ``open_relative_directory`` will
    refuse to follow them if we did try to open them, but skipping in the
    listing keeps the per-request cost predictable.
    """

    entries: list[tuple[str, float]] = []
    root_fd = os.dup(runs_root_fd)
    try:
        with os.scandir(root_fd) as scanner:
            for entry in scanner:
                if not valid_run_id(entry.name):
                    continue
                try:
                    info = os.stat(entry.name, dir_fd=runs_root_fd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                entries.append((entry.name, info.st_mtime))
    finally:
        os.close(root_fd)
    entries.sort(key=lambda pair: pair[1], reverse=True)
    return entries


def resolve_ticket_runs(runs_root_fd: int, ticket: str) -> TicketRunsListing:
    entries = _iter_root_entries(runs_root_fd)
    matches: list[RunSummary] = []
    truncated = False
    scanned = 0
    for name, _mtime in entries:
        if scanned >= MAX_RUN_LIST_SCAN:
            truncated = True
            break
        scanned += 1
        try:
            meta = _read_bounded_metadata(runs_root_fd, name)
        except ReplayError:
            continue
        agent_id = meta.get("agent_id")
        if agent_id != ticket:
            continue
        matches.append(_run_summary_from_meta(meta, name))
        if len(matches) >= MAX_RUN_LIST_ENTRIES:
            truncated = True
            break
    return TicketRunsListing(runs=matches, truncated=truncated)


# ---------------------------------------------------------------------------
# Runs root helper (for callers in main.py)
# ---------------------------------------------------------------------------


def open_runs_root_fd(runs_root_path: str | os.PathLike[str]) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    return os.open(runs_root_path, os.O_RDONLY | no_follow | directory_flag)


def verify_run_dir_exists(runs_root_fd: int, run_id: str) -> None:
    """Cheap early-existence check that mirrors production behavior.

    ``open_relative_directory`` opens the run dir under ``runs_root_fd``
    with ``O_NOFOLLOW`` on every component. We close the fd immediately —
    the useful signal is whether the open succeeded.
    """

    try:
        fd = open_relative_directory(runs_root_fd, (run_id,))
    except FileNotFoundError as exc:
        raise ReplayError("run not found") from exc
    except OSError as exc:
        raise ReplayError(f"run not readable: {exc}") from exc
    os.close(fd)
