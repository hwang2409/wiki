"""Session replay timeline builder for archived agent runs (WIKI-174).

Pure helpers that transform ``events.jsonl`` (and, when needed, ``raw.jsonl``)
into a compact, timestamped timeline the frontend scrubber consumes. All I/O
here is read-only and bounded so a 100k-event ``raw.jsonl`` never blows out a
handler's memory budget: we stream JSON lines, skip anything below the caller's
cursor, and cap the response window with ``limit``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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


def _iter_json_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Stream JSON objects from a JSONL file, skipping malformed trailing lines.

    The supervisor's crash-repair pass truncates the last partial line on the
    next restart, so we mirror that leniency here: if a mid-stream line fails to
    parse we skip it rather than raising — the caller is showing history, not
    building the durable ledger.
    """

    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def _classify_bookmark(kind: str, payload: dict[str, Any], text: str | None) -> str | None:
    if kind == "provider_process_exit":
        return "error"
    if kind == "claude_result":
        if payload.get("is_error"):
            return "error"
        # A completed result is the natural verdict frame.
        return "verdict"
    if payload.get("is_error") is True:
        return "error"
    if kind.endswith("_error") or kind.endswith("_stderr"):
        return "error"
    if kind == "claude_user":
        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            # Anything that ISN'T only a tool_result block is user-injected
            # steering (composer message, orchestrator user echo, etc.). Pure
            # tool_result deliveries are just protocol chatter.
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
        code = payload.get("exit_code")
        return f"provider_process_exit code={code}" if code is not None else "provider_process_exit", None
    if kind == "provider_stderr":
        text = payload.get("text") or payload.get("stderr") or ""
        return _excerpt(text) if isinstance(text, str) else "provider_stderr", None
    if kind.startswith("codex_") or kind == "codex_client_message":
        method = payload.get("method")
        if isinstance(method, str):
            return f"codex {method}", None
        return kind, None
    if kind == "approval" or kind == "approval_cancelled":
        subtype = None
        request = payload.get("request")
        if isinstance(request, dict):
            subtype = request.get("subtype")
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
) -> tuple[list[TimelineEvent], int | None]:
    """Return ``(events, next_after_seq)`` for a bounded slice of the timeline.

    ``next_after_seq`` is the last-emitted seq when the window filled, letting
    the client paginate forward without re-reading the file from byte zero. It
    is ``None`` when the stream ended within the window.
    """

    if limit <= 0:
        return [], None
    limit = min(limit, MAX_LIMIT)
    if after_seq < 0:
        after_seq = 0
    collected: list[TimelineEvent] = []
    last_seq: int | None = None
    for event in _iter_timeline(_iter_json_lines(events_path)):
        if event.seq <= after_seq:
            continue
        collected.append(event)
        last_seq = event.seq
        if len(collected) >= limit:
            return collected, last_seq
    return collected, None


def build_bookmarks(events_path: Path, *, cap: int = MAX_BOOKMARKS) -> list[dict[str, Any]]:
    """Whole-file bookmark scan, capped so a 100k-event run stays cheap on the wire."""

    if cap <= 0:
        return []
    bookmarks: list[dict[str, Any]] = []
    for event in _iter_timeline(_iter_json_lines(events_path)):
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
    return bookmarks


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
    events, next_seq = build_timeline_window(
        events_path, after_seq=after_seq, limit=limit
    )
    bookmarks = build_bookmarks(events_path) if after_seq <= 0 else []
    return {
        "run": summary.as_dict(),
        "events": [event.as_dict() for event in events],
        "next_after_seq": next_seq,
        "bookmarks": bookmarks,
    }


def load_raw_event(run_dir: Path, seq: int) -> dict[str, Any] | None:
    """Return the raw.jsonl entry for ``seq`` without slurping the whole file."""

    if seq <= 0:
        return None
    raw_path = run_dir / "raw.jsonl"
    for entry in _iter_json_lines(raw_path):
        entry_seq = entry.get("seq")
        if isinstance(entry_seq, int) and entry_seq == seq:
            return entry
        # raw.jsonl is monotonic in seq, so we can bail early once we pass it.
        if isinstance(entry_seq, int) and entry_seq > seq:
            return None
    return None
