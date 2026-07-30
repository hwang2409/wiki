"""Kind-aware summary + bookmark classification.

Kept isolated from the reader so the parsing rules can evolve without
churning the byte-level plumbing.
"""

from __future__ import annotations

import re
from typing import Any

from .models import TimelineEvent


MERGE_READY_PATTERN = re.compile(r"\b(MERGE-READY|BLOCKED)\s*:", re.IGNORECASE)


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


def classify_bookmark(kind: str, payload: dict[str, Any], text: str | None) -> str | None:
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


def summarize_payload(kind: str, payload: dict[str, Any]) -> tuple[str, str | None]:
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
        return (
            f"hook {payload.get('hook_name') or payload.get('hook_event') or 'unknown'}",
            None,
        )
    if kind == "claude_status":
        status = payload.get("status") or payload.get("state") or payload.get("session_state")
        return f"status {status or 'unknown'}", None
    if kind == "claude_client_message":
        request = payload.get("request") or {}
        subtype = request.get("subtype") if isinstance(request, dict) else None
        return (
            f"client {payload.get('type') or 'message'}{': ' + subtype if subtype else ''}",
            None,
        )
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


def timeline_event_from(entry: dict[str, Any]) -> TimelineEvent | None:
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
    summary, text_hint = summarize_payload(kind, payload)
    bookmark = classify_bookmark(kind, payload, text_hint)
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
