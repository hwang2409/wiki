"""Session replay timeline builder for archived agent runs (WIKI-174).

Round-4 review feedback closed the pagination story completely:

* ``_SnapshotReader`` is a class that tracks its own resumable state — byte
  position AND mid-oversized-record skip flag. When we hit the scan budget
  mid-scan, the caller reads ``resume_state`` and encodes both fields into
  the next cursor. The next request re-enters skip mode where it left off,
  so no event in an arbitrarily-large ``events.jsonl`` is permanently
  unreachable.
* ``has_more`` is now purely ``end_offset < snapshot_size``. The round-3
  logic ANDed it with ``not scan_truncated``, so a budget cut mid-scan
  reported ``has_more=false`` even though data was still ahead.
* All bounds — ``MAX_SCAN_BYTES``, ``MAX_LINE_BYTES``, ``MAX_BOOKMARKS``,
  ``MAX_RUN_JSON_BYTES``, ``MAX_RUN_LIST_ENTRIES`` — resolve at CALL time
  via module-attribute lookup, so ``mock.patch.object(replay, "X", small)``
  actually exercises small budgets in tests. Round-3 tests silently ran at
  production budgets because the def-time defaults were already captured.
* Child files open with ``O_NONBLOCK`` and are refused unless ``S_ISREG``.
  A FIFO named ``run.json`` used to block ``fstat`` indefinitely before
  the reader could check the mode.
* ``ReplayError`` carries a ``status_code`` so endpoint mapping is one
  line — no more brittle "not found" substring probes.

Every file is opened through ``pathwalk.open_relative_file`` rooted at a
pre-opened runs-root fd; that closes the check-then-open TOCTTOU gap the
round-2 resolve-then-open guard couldn't cover.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, Iterator

from .pathwalk import open_relative_directory, open_relative_file

RUN_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Bookmark rendering budget: keep the count small so a scrubber's tick track
# stays legible on a 480px panel even for long runs. Truncation is surfaced
# via ``bookmarks_truncated`` so nothing is silently hidden.
MAX_BOOKMARKS = 200
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000

# Per-line hard ceiling. Real supervisor events observed on disk are ~200 B –
# 30 KiB; anything above 1 MiB is corruption or an attack and the reader
# discards it wholesale instead of buffering.
MAX_LINE_BYTES = 1 * 1024 * 1024

# Per-scan hard ceiling. Every byte physically read counts — including bytes
# consumed while skipping an oversized record — so a 100 GiB file terminates
# in constant peak RSS. Pagination resumes across scan-truncated pages via
# the ``skipping`` field of the cursor, so no event is unreachable.
MAX_SCAN_BYTES = 64 * 1024 * 1024

# Per-run.json read cap. The largest real ``run.json`` seen in the runtime
# directory is ~14 KiB even with a very long ``initial_prompt``. 256 KiB
# gives ample headroom without letting a corrupt or adversarial run.json
# drive unbounded metadata reads.
MAX_RUN_JSON_BYTES = 256 * 1024

# ``resolve_ticket_runs`` outer caps: sort by mtime, read at most this many
# run.json files to filter by ticket. Surfaced as ``runs_truncated``.
MAX_RUN_LIST_SCAN = 500
MAX_RUN_LIST_ENTRIES = 200

# Chunked stream reads: 128 KiB balances syscall overhead against per-response
# RSS. We never buffer more than one chunk beyond the current partial record.
_STREAM_CHUNK = 128 * 1024

MERGE_READY_PATTERN = re.compile(r"\b(MERGE-READY|BLOCKED)\s*:", re.IGNORECASE)


class ReplayError(Exception):
    """Raised when a run directory is unreadable, malformed, or refused.

    ``status_code`` maps directly to the endpoint HTTP status. 404 for
    "run isn't here" (including symlink-swapped or FIFO-substituted
    children), 400 for client-supplied garbage, 500 otherwise.
    """

    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


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
# Opaque byte cursor + skip-state
# ---------------------------------------------------------------------------


def encode_cursor(byte_offset: int, *, skipping: bool = False) -> str:
    """Encode a resumable position (byte offset + skip flag) into an opaque token.

    Client code MUST NOT parse this — the encoding may change. Round-4 added
    the ``skipping`` flag so a scan-budget cut inside an oversized record
    can resume mid-skip instead of re-buffering the whole record on the
    next request.
    """

    payload_dict: dict[str, Any] = {"o": int(byte_offset)}
    if skipping:
        payload_dict["s"] = True
    payload = json.dumps(payload_dict, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[int, bool]:
    if not cursor:
        return 0, False
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ReplayError("invalid cursor", status_code=400) from exc
    if not isinstance(value, dict):
        raise ReplayError("invalid cursor", status_code=400)
    offset = value.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise ReplayError("invalid cursor", status_code=400)
    skipping = bool(value.get("s", False))
    return int(offset), skipping


# ---------------------------------------------------------------------------
# fd-based bounded reader
# ---------------------------------------------------------------------------


def _open_run_child_fd(runs_root_fd: int, run_id: str, filename: str) -> int:
    """Open ``<runs_root>/<run_id>/<filename>`` via the dir-fd walker.

    ``open_relative_file`` refuses a symlink at ANY component. We pass
    ``O_NONBLOCK`` on the final open so a FIFO or device swapped in for a
    regular file returns immediately instead of blocking the request in the
    kernel. After the open we ``fstat`` and refuse anything that isn't a
    regular file — a FIFO opened with O_NONBLOCK would still let us read
    junk, and a device could give the client arbitrary system state.
    """

    o_nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        fd = open_relative_file(runs_root_fd, (run_id, filename), extra_final_flags=o_nonblock)
    except FileNotFoundError as exc:
        raise ReplayError(f"{filename} not found for run {run_id}", status_code=404) from exc
    except OSError as exc:
        # ELOOP (symlink refused) and every other open error collapse to
        # 404 so callers can't distinguish "swap detected" from "missing"
        # via response codes.
        raise ReplayError(
            f"{filename} not accessible for run {run_id}: {exc}",
            status_code=404,
        ) from exc
    try:
        info = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise ReplayError(
            f"could not stat {filename}: {exc}",
            status_code=404,
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ReplayError(
            f"{filename} is not a regular file",
            status_code=404,
        )
    return fd


class _SnapshotReader:
    """Byte-bounded, resumable line reader for a size-snapshotted fd.

    The reader owns its own state (``position``, ``skipping``, buffered
    partial-record bytes) so a caller that stops mid-stream can extract a
    ``resume_state`` and hand it into the next reader instance via the
    cursor. That is what makes pagination survive a scan-budget cut in the
    middle of an oversized record — round-3 lost bytes and reported
    ``has_more=false`` at that point.
    """

    def __init__(
        self,
        fd: int,
        *,
        start_offset: int,
        start_skipping: bool,
        max_scan_bytes: int,
        max_line_bytes: int,
        stats: _ScanStats,
    ) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReplayError("expected a regular file", status_code=404)
        self.fd = fd
        self.snapshot_size = int(info.st_size)
        self.max_scan_bytes = max_scan_bytes
        self.max_line_bytes = max_line_bytes
        self.stats = stats
        self.buffer = bytearray()
        self.accumulated_line_bytes = 0
        self.skipping = bool(start_skipping)
        clamped_offset = start_offset
        if clamped_offset < 0:
            clamped_offset = 0
        if clamped_offset > self.snapshot_size:
            clamped_offset = self.snapshot_size
        self.position = clamped_offset
        if self.position > 0:
            os.lseek(fd, self.position, os.SEEK_SET)
        self._total_bytes_read = 0

    def resume_state(self) -> tuple[int, bool]:
        """Where the caller should resume + whether mid-oversized-record.

        If the reader stopped mid-normal-record (buffered partial bytes but
        no newline yet), rewind past those bytes so the resume re-reads
        them as one atomic unit. If it stopped mid-oversized-skip, keep
        the current position and set the skip flag — the next reader
        instance discards bytes until the terminating newline.
        """

        if not self.skipping and self.buffer:
            return (self.position - len(self.buffer), False)
        return (self.position, self.skipping)

    def records(self) -> Iterator[tuple[int, bytes]]:
        while self.position < self.snapshot_size:
            # Cap the read size against remaining budget so we can't over-read
            # by (chunk_size - 1) bytes past the ceiling in a single syscall.
            budget_remaining = self.max_scan_bytes - self._total_bytes_read
            if budget_remaining <= 0:
                self.stats.scan_truncated = True
                return
            to_read = min(
                _STREAM_CHUNK,
                self.snapshot_size - self.position,
                budget_remaining,
            )
            chunk = os.read(self.fd, to_read)
            if not chunk:
                break
            chunk_start = self.position
            self.position += len(chunk)
            self._total_bytes_read += len(chunk)
            offset = 0
            while offset < len(chunk):
                nl = chunk.find(b"\n", offset)
                if nl < 0:
                    slice_len = len(chunk) - offset
                    self.accumulated_line_bytes += slice_len
                    if not self.skipping and self.accumulated_line_bytes > self.max_line_bytes:
                        self.stats.dropped_oversize += 1
                        self.skipping = True
                        self.buffer.clear()
                    if not self.skipping:
                        self.buffer.extend(chunk[offset:])
                    offset = len(chunk)
                    continue
                record_end_position = chunk_start + nl + 1
                if self.skipping:
                    self.skipping = False
                    self.accumulated_line_bytes = 0
                    offset = nl + 1
                    continue
                slice_len = nl - offset
                self.accumulated_line_bytes += slice_len
                if self.accumulated_line_bytes > self.max_line_bytes:
                    self.stats.dropped_oversize += 1
                    self.buffer.clear()
                    self.accumulated_line_bytes = 0
                    offset = nl + 1
                    continue
                if self.buffer:
                    self.buffer.extend(chunk[offset:nl])
                    record = bytes(self.buffer)
                    self.buffer.clear()
                else:
                    record = bytes(chunk[offset:nl])
                self.accumulated_line_bytes = 0
                offset = nl + 1
                yield record_end_position, record
            # After yielding every complete record from this chunk, check
            # whether the budget is exhausted BEFORE we read the next chunk.
            # This lets ``max_scan_bytes`` truncate cleanly on record
            # boundaries when possible, rather than mid-chunk.
            if self._total_bytes_read >= self.max_scan_bytes:
                # Only mark truncated if there is actually data left ahead;
                # otherwise natural EOF is the reason we're stopping.
                if self.position < self.snapshot_size:
                    self.stats.scan_truncated = True
                return
        if self.buffer or self.skipping:
            self.stats.dropped_truncated_tail = True


# ---------------------------------------------------------------------------
# run.json metadata (bounded)
# ---------------------------------------------------------------------------


def _read_bounded_metadata(runs_root_fd: int, run_id: str) -> dict[str, Any]:
    cap = MAX_RUN_JSON_BYTES
    fd = _open_run_child_fd(runs_root_fd, run_id, "run.json")
    try:
        info = os.fstat(fd)
        if info.st_size > cap:
            raise ReplayError(
                f"run.json exceeds {cap}-byte ceiling",
                status_code=413,
            )
        chunks: list[bytes] = []
        remaining = cap + 1
        while remaining > 0:
            chunk = os.read(fd, min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > cap:
            raise ReplayError(
                f"run.json exceeds {cap}-byte ceiling",
                status_code=413,
            )
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ReplayError(f"run.json is not valid JSON: {exc}", status_code=500) from exc
    if not isinstance(value, dict):
        raise ReplayError("run.json must contain an object", status_code=500)
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
# Bookmark + summary derivation
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
# Timeline + bookmark builders
# ---------------------------------------------------------------------------


@dataclass
class TimelinePage:
    events: list[TimelineEvent]
    end_offset: int
    end_skipping: bool
    has_more: bool


def _build_timeline_page(
    fd: int,
    *,
    start_offset: int,
    start_skipping: bool,
    limit: int,
    stats: _ScanStats,
    max_scan_bytes: int | None = None,
    max_line_bytes: int | None = None,
) -> TimelinePage:
    if limit <= 0:
        return TimelinePage(events=[], end_offset=start_offset, end_skipping=start_skipping, has_more=False)
    limit = min(limit, MAX_LIMIT)
    # Resolve budgets at call time so ``mock.patch.object(replay, "X", n)``
    # in tests actually reaches this function — the round-3 tests silently
    # ran at 64 MiB because default-arg captures happened at def time.
    if max_scan_bytes is None:
        max_scan_bytes = MAX_SCAN_BYTES
    if max_line_bytes is None:
        max_line_bytes = MAX_LINE_BYTES
    reader = _SnapshotReader(
        fd,
        start_offset=start_offset,
        start_skipping=start_skipping,
        max_scan_bytes=max_scan_bytes,
        max_line_bytes=max_line_bytes,
        stats=stats,
    )
    events: list[TimelineEvent] = []
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
        event = _timeline_event_from(entry)
        if event is None:
            continue
        events.append(event)
        end_offset = offset
        end_skipping = False
        if len(events) >= limit:
            reached_limit = True
            break
    if not reached_limit:
        # Reader exited on its own — either EOF, torn tail, or scan budget.
        # Grab its authoritative resume state.
        end_offset, end_skipping = reader.resume_state()
    # ``has_more`` is now purely based on file position — the round-3
    # ANDing with ``not scan_truncated`` marked a budget-cut page as
    # "done" even though data remained ahead.
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
    stats: _ScanStats,
    max_scan_bytes: int | None = None,
    max_line_bytes: int | None = None,
) -> list[dict[str, Any]]:
    if cap <= 0:
        return []
    if max_scan_bytes is None:
        max_scan_bytes = MAX_SCAN_BYTES
    if max_line_bytes is None:
        max_line_bytes = MAX_LINE_BYTES
    reader = _SnapshotReader(
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
        warnings.append("scan budget reached — continuing via cursor")
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
    """Full endpoint payload: meta + a window of events + bookmarks + cursor."""

    summary = build_run_summary(runs_root_fd, run_id)
    start_offset, start_skipping = decode_cursor(cursor)
    window_stats = _ScanStats()
    bookmark_stats = _ScanStats()

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

    # Bookmarks only on the first page (start_offset == 0 AND not
    # mid-skip). Later pages return an empty list; the client keeps the
    # first-page bookmarks around.
    bookmarks: list[dict[str, Any]]
    if start_offset == 0 and not start_skipping:
        events_fd = _open_run_child_fd(runs_root_fd, run_id, "events.jsonl")
        try:
            bookmarks = _build_bookmarks(
                events_fd, cap=MAX_BOOKMARKS, stats=bookmark_stats
            )
        finally:
            os.close(events_fd)
    else:
        bookmarks = []

    next_cursor = (
        encode_cursor(page.end_offset, skipping=page.end_skipping)
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
    """Return the raw.jsonl entry for ``seq`` — fd-based, byte-bounded, resumable.

    Round-3 stopped at the scan budget with an event past 64 MiB
    unreachable; we now loop across resume cursors so any event with a
    valid seq is eventually returnable.
    """

    if seq <= 0:
        return None
    start_offset = 0
    start_skipping = False
    while True:
        stats = _ScanStats()
        try:
            raw_fd = _open_run_child_fd(runs_root_fd, run_id, "raw.jsonl")
        except ReplayError:
            return None
        found: dict[str, Any] | None = None
        past = False
        try:
            reader = _SnapshotReader(
                raw_fd,
                start_offset=start_offset,
                start_skipping=start_skipping,
                max_scan_bytes=MAX_SCAN_BYTES,
                max_line_bytes=MAX_LINE_BYTES,
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
            # Reader reached EOF without finding seq.
            return None
        if resume_offset <= start_offset and not (resume_skipping and not start_skipping):
            # No forward progress — bail rather than loop.
            return None
        start_offset = resume_offset
        start_skipping = resume_skipping


# ---------------------------------------------------------------------------
# Ticket → runs discovery
# ---------------------------------------------------------------------------


@dataclass
class TicketRunsListing:
    runs: list[RunSummary]
    truncated: bool = False


def _iter_root_entries(runs_root_fd: int) -> list[tuple[str, float]]:
    """Return ``(name, mtime)`` for non-symlinked subdirs of the runs root."""

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
# Runs root helper
# ---------------------------------------------------------------------------


def open_runs_root_fd(runs_root_path: str | os.PathLike[str]) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    return os.open(runs_root_path, os.O_RDONLY | no_follow | directory_flag)


def verify_run_dir_exists(runs_root_fd: int, run_id: str) -> None:
    """Cheap early-existence check that mirrors production behavior."""

    try:
        fd = open_relative_directory(runs_root_fd, (run_id,))
    except FileNotFoundError as exc:
        raise ReplayError("run not found", status_code=404) from exc
    except OSError as exc:
        raise ReplayError(f"run not readable: {exc}", status_code=404) from exc
    os.close(fd)
