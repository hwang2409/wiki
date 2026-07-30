"""Session replay timeline builder for archived agent runs (WIKI-174).

Pure helpers that transform ``events.jsonl`` (and, when needed, ``raw.jsonl``)
into a compact, timestamped timeline the frontend scrubber consumes. All I/O
here is read-only and strictly bounded so a corrupted or adversarial log can't
blow out a handler's memory budget.

Read discipline (WIKI-174 round-2 review):
    * Files are opened with ``O_NOFOLLOW`` so a symlinked ``events.jsonl``
      pointing outside the runs root can't leak data.
    * We snapshot the file size at open with ``os.fstat`` and read only up to
      that byte count, so a live-append that lands the header of a new record
      before its trailing newline can't feed us a torn half-line.
    * Only ``\\n``-terminated records are yielded — the last partial line in
      the snapshot window is dropped and reconsidered on the next read.
    * Every line is capped by ``MAX_LINE_BYTES``: an oversize line is skipped
      wholesale instead of being buffered into memory.
    * Every scan is capped by a total byte budget so ``build_bookmarks`` on a
      100 MiB events.jsonl still terminates in constant peak RSS.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import Any, Iterable, Iterator

RUN_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Bookmark rendering budget: keep the count small so a scrubber's tick track
# stays legible on a 480px panel even for long runs.
MAX_BOOKMARKS = 200
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000

# Per-line hard ceiling. Real supervisor events observed on disk are ~200 B –
# 30 KiB (tool_use inputs, hook payloads). A well-formed event should never
# approach 1 MiB; anything above that is corruption or an attack and we drop
# it rather than allocate for it.
MAX_LINE_BYTES = 1 * 1024 * 1024

# Per-scan hard ceiling. The bookmark scan and raw event lookup traverse a
# full events.jsonl / raw.jsonl. This bound guarantees a peak read of
# ~64 MiB regardless of file size — with truncation surfaced to the caller so
# nothing gets silently dropped from the UI.
MAX_SCAN_BYTES = 64 * 1024 * 1024

# Chunked stream reads: 128 KiB balances syscall overhead against per-response
# RSS. Never buffer more than one chunk beyond the current partial line.
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


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_PATTERN.fullmatch(run_id))


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


def load_run_metadata(run_dir: Path) -> dict[str, Any]:
    run_path = run_dir / "run.json"
    try:
        raw = run_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReplayError(f"run.json missing for {run_dir.name}") from exc
    except OSError as exc:
        raise ReplayError(f"could not read run.json: {exc}") from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ReplayError(f"run.json is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayError("run.json must contain an object")
    return value


def build_run_summary(run_dir: Path, meta: dict[str, Any] | None = None) -> RunSummary:
    meta = meta if meta is not None else load_run_metadata(run_dir)
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
        run_id=str(meta.get("run_id") or run_dir.name),
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


@dataclass(frozen=True)
class _RawLine:
    raw: bytes


@dataclass
class _ScanStats:
    dropped_oversize: int = 0
    dropped_malformed: int = 0
    dropped_truncated_tail: bool = False
    scan_truncated: bool = False


def _open_nofollow(path: Path) -> int:
    """Open a JSONL log for read, refusing symlinks and non-regular files.

    A symlinked ``events.jsonl`` that points outside the runs root would let a
    caller who can only forge the ``run_id`` still read anything the server
    process can. ``O_NOFOLLOW`` on the final path component plus an
    ``S_ISREG`` check on the resulting fd close that off.
    """

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReplayError(f"{path.name} is not a regular file")
    except Exception:
        os.close(fd)
        raise
    return fd


def _iter_snapshot_lines(
    path: Path,
    *,
    max_scan_bytes: int = MAX_SCAN_BYTES,
    max_line_bytes: int = MAX_LINE_BYTES,
    stats: _ScanStats | None = None,
) -> Iterator[bytes]:
    """Yield newline-terminated byte lines from a bounded snapshot window.

    The producer of these files (``RunStore``) writes each event as
    ``json.dumps(...) + '\\n'`` — but the write is not one atomic syscall in
    every path. Reading past the snapshot size or accepting a line without a
    trailing ``\\n`` risks a torn read where the reader sees ``json.dumps``
    but not the newline. We snapshot ``st_size`` at open and only yield lines
    that end with ``\\n`` inside that window; anything after the last newline
    is left for the next read.
    """

    try:
        fd = _open_nofollow(path)
    except FileNotFoundError:
        return
    reader: BufferedReader | None = None
    try:
        info = os.fstat(fd)
        snapshot_size = int(info.st_size)
        reader = os.fdopen(fd, "rb", buffering=0)
        # ``fd`` now owned by ``reader``; do not close it separately.
        fd = -1
        buffer = bytearray()
        remaining = snapshot_size
        total_yielded = 0
        while remaining > 0:
            to_read = min(_STREAM_CHUNK, remaining)
            chunk = reader.read(to_read)
            if not chunk:
                break
            remaining -= len(chunk)
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                total_yielded += newline + 1
                if len(line) > max_line_bytes:
                    if stats is not None:
                        stats.dropped_oversize += 1
                    continue
                if total_yielded > max_scan_bytes:
                    if stats is not None:
                        stats.scan_truncated = True
                    return
                yield line
            # Guard against a single record that is itself larger than
            # ``max_line_bytes``: don't grow the buffer past that ceiling
            # searching for the eventual newline.
            if len(buffer) > max_line_bytes:
                # Discard everything up to the next newline in the stream.
                if stats is not None:
                    stats.dropped_oversize += 1
                buffer.clear()
                skip_remaining = remaining
                while skip_remaining > 0:
                    skip_chunk = reader.read(min(_STREAM_CHUNK, skip_remaining))
                    if not skip_chunk:
                        break
                    skip_remaining -= len(skip_chunk)
                    hit = skip_chunk.find(b"\n")
                    if hit >= 0:
                        buffer.extend(skip_chunk[hit + 1 :])
                        remaining = skip_remaining
                        break
                else:
                    remaining = 0
                if remaining == 0 and skip_remaining > 0:
                    remaining = skip_remaining
        # Any bytes still in the buffer come from an un-newline-terminated
        # tail: leave them for the next call.
        if buffer:
            if stats is not None:
                stats.dropped_truncated_tail = True
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
        elif fd >= 0:
            os.close(fd)


def _iter_json_events(
    path: Path,
    *,
    max_scan_bytes: int = MAX_SCAN_BYTES,
    max_line_bytes: int = MAX_LINE_BYTES,
    stats: _ScanStats | None = None,
) -> Iterator[dict[str, Any]]:
    for line in _iter_snapshot_lines(
        path,
        max_scan_bytes=max_scan_bytes,
        max_line_bytes=max_line_bytes,
        stats=stats,
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            if stats is not None:
                stats.dropped_malformed += 1
            continue
        if isinstance(value, dict):
            yield value


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
    # Codex ``item/completed`` covers user injections, agent replies, tool
    # execs, and reasoning. userMessage = a real steer; agentMessage carrying
    # a merge sentinel = a verdict.
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
        # Codex reports through ``params.returncode``; Claude uses
        # top-level ``exit_code``. A clean 0 is normal termination, NOT an
        # error — only mark non-zero exits so bookmarks stay useful.
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
    """Return ``(summary, text_hint)`` for a normalized event payload.

    ``text_hint`` is only populated when a message-body excerpt was extracted;
    the bookmark classifier reuses it so we don't scan the payload twice.
    """

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
        # Look in both shapes: Codex nests under ``params.returncode``, Claude
        # exposes ``exit_code`` on the payload directly.
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
    # Generic Codex method-derived kinds (``item_agentMessage_delta``,
    # ``thread_tokenUsage_updated``, etc.) — just show the kind and let the
    # payload viewer do the rest.
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


def _iter_timeline(source: Iterable[dict[str, Any]]) -> Iterator[TimelineEvent]:
    for entry in source:
        event = _timeline_event_from(entry)
        if event is not None:
            yield event


def build_timeline_window(
    events_path: Path,
    *,
    after_seq: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[TimelineEvent], int | None, _ScanStats]:
    """Return ``(events, next_after_seq, stats)`` for a bounded slice.

    ``next_after_seq`` is the last-emitted seq when the window filled, letting
    the client paginate forward without re-reading the file from byte zero.
    ``stats`` surfaces drop counters + snapshot truncation so callers can show
    when data was skipped.
    """

    stats = _ScanStats()
    if limit <= 0:
        return [], None, stats
    limit = min(limit, MAX_LIMIT)
    if after_seq < 0:
        after_seq = 0
    collected: list[TimelineEvent] = []
    last_seq: int | None = None
    for event in _iter_timeline(
        _iter_json_events(events_path, stats=stats)
    ):
        if event.seq <= after_seq:
            continue
        collected.append(event)
        last_seq = event.seq
        if len(collected) >= limit:
            return collected, last_seq, stats
    return collected, None, stats


def build_bookmarks(
    events_path: Path,
    *,
    cap: int = MAX_BOOKMARKS,
) -> tuple[list[dict[str, Any]], _ScanStats]:
    """Whole-file bookmark scan, capped so a large run stays cheap on the wire."""

    stats = _ScanStats()
    if cap <= 0:
        return [], stats
    bookmarks: list[dict[str, Any]] = []
    for event in _iter_timeline(_iter_json_events(events_path, stats=stats)):
        if event.bookmark is None:
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
            break
    return bookmarks, stats


def build_timeline_response(
    run_dir: Path,
    *,
    after_seq: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Full endpoint payload — meta + a window of events + bookmarks."""

    meta = load_run_metadata(run_dir)
    summary = build_run_summary(run_dir, meta)
    events_path = run_dir / "events.jsonl"
    events, next_seq, window_stats = build_timeline_window(
        events_path, after_seq=after_seq, limit=limit
    )
    bookmarks: list[dict[str, Any]]
    bookmark_stats: _ScanStats
    if after_seq <= 0:
        bookmarks, bookmark_stats = build_bookmarks(events_path)
    else:
        bookmarks, bookmark_stats = [], _ScanStats()
    return {
        "run": summary.as_dict(),
        "events": [event.as_dict() for event in events],
        "next_after_seq": next_seq,
        "bookmarks": bookmarks,
        "warnings": _warnings_from(window_stats, bookmark_stats),
    }


def _warnings_from(*stats: _ScanStats) -> list[str]:
    dropped_oversize = sum(s.dropped_oversize for s in stats)
    dropped_malformed = sum(s.dropped_malformed for s in stats)
    dropped_tail = any(s.dropped_truncated_tail for s in stats)
    scan_truncated = any(s.scan_truncated for s in stats)
    warnings: list[str] = []
    if dropped_oversize:
        warnings.append(f"dropped {dropped_oversize} oversized event line(s)")
    if dropped_malformed:
        warnings.append(f"skipped {dropped_malformed} malformed line(s)")
    if dropped_tail:
        warnings.append("trailing partial write ignored (torn-read guard)")
    if scan_truncated:
        warnings.append("scan hit byte budget — later events not classified")
    return warnings


def load_raw_event(run_dir: Path, seq: int) -> dict[str, Any] | None:
    """Return the raw.jsonl entry for ``seq`` without slurping the whole file."""

    if seq <= 0:
        return None
    raw_path = run_dir / "raw.jsonl"
    stats = _ScanStats()
    for entry in _iter_json_events(raw_path, stats=stats):
        entry_seq = entry.get("seq")
        if isinstance(entry_seq, int) and entry_seq == seq:
            return entry
        # raw.jsonl is monotonic in seq, so we can bail early once we pass it.
        if isinstance(entry_seq, int) and entry_seq > seq:
            return None
    return None
