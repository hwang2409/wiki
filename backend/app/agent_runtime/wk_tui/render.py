"""Pure Rich renderers for wk lane events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.text import Text

MAX_LINE = 240
_CODEX_ERROR_METHODS = {
    "error",
    "codex.provider_error",
    "wk.codex.tool_policy_unavailable",
}


def _one_line(value: object, limit: int = MAX_LINE) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, default=str, sort_keys=True)
    )
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _line(prefix: str, body: object, style: str) -> Text:
    return Text.assemble((prefix, "dim"), (_one_line(body), style))


def _raw_line(raw: Mapping[str, Any]) -> Text:
    return Text(f"[raw] {_one_line(raw)}", style="dim")


def _append_verbose(
    rendered: list[RenderableType], raw: Mapping[str, Any], verbose: bool
) -> list[RenderableType]:
    if verbose:
        rendered.append(_raw_line(raw))
    return rendered


def _tool_line(name: object, arguments: object) -> Text:
    return Text.assemble(
        ("[tool] ", "dim"),
        (_one_line(name or "?"), "bold cyan"),
        (" ", "dim"),
        (_one_line(arguments or {}), "dim"),
    )


def _tool_result_line(detail: object, *, error: bool) -> Text:
    style = "bold red" if error else "dim green"
    status = "error" if error else "ok"
    return Text.assemble(
        ("[tool result] ", "dim"),
        (status, style),
        (f" {_one_line(detail)}", style),
    )


def _turn_line(status: object, duration: object = None) -> Text:
    suffix = f" ({duration} ms)" if duration is not None else ""
    return Text(f"[turn] {_one_line(status or '?')}{suffix}", style="dim")


def render_claude(
    raw: Mapping[str, Any], verbose: bool = False
) -> list[RenderableType]:
    """Render one raw Claude event into Rich renderables."""

    kind = str(raw.get("type") or "")
    rendered: list[RenderableType] = []
    if kind == "system":
        subtype = str(raw.get("subtype") or "")
        if subtype == "init":
            rendered.append(
                _line(
                    "[system] ",
                    f"session {raw.get('session_id') or '?'} started",
                    "cyan",
                )
            )
        else:
            rendered.append(_line("[system] ", subtype or dict(raw), "dim"))
    elif kind == "assistant":
        message = raw.get("message")
        content = message.get("content") if isinstance(message, Mapping) else []
        for block in content or []:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "")
                if text:
                    rendered.append(Markdown(text, code_theme="monokai"))
            elif block_type == "thinking":
                thinking = _one_line(block.get("thinking") or "")
                rendered.append(Text(f"[thinking] {thinking}", style="dim"))
            elif block_type == "tool_use":
                rendered.append(_tool_line(block.get("name"), block.get("input")))
    elif kind == "user":
        message = raw.get("message")
        content = message.get("content") if isinstance(message, Mapping) else []
        for block in content or []:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                rendered.append(
                    _tool_result_line(
                        block.get("content"),
                        error=bool(block.get("is_error")),
                    )
                )
    elif kind == "result":
        status = raw.get("subtype") or ("error" if raw.get("is_error") else "ok")
        rendered.append(_turn_line(status, raw.get("duration_ms", "?")))
    elif kind == "control_request":
        rendered.append(_line("[approval] ", raw.get("request") or {}, "yellow"))
    elif kind == "provider_error":
        detail = (
            f"{raw.get('error_class') or 'error'}: {_one_line(raw.get('error') or '')}"
        )
        rendered.append(Text(f"[error] {detail}", style="bold red"))
    elif kind != "stream_event":
        rendered.append(_line("[event] ", kind or dict(raw), "dim"))
    return _append_verbose(rendered, raw, verbose)


def render_codex(raw: Mapping[str, Any], verbose: bool = False) -> list[RenderableType]:
    """Render one raw Codex App Server event into Rich renderables."""

    method = str(raw.get("method") or "")
    params = raw.get("params") if isinstance(raw.get("params"), Mapping) else {}
    rendered: list[RenderableType] = []
    if method in _CODEX_ERROR_METHODS:
        detail = params.get("error") or params
        prefix = params.get("error_class") or "error"
        rendered.append(
            Text(f"[error] {prefix}: {_one_line(detail)}", style="bold red")
        )
    elif method == "thread/started":
        rendered.append(Text("[thread] started", style="cyan"))
    elif method == "turn/started":
        rendered.append(Text("[turn] started", style="dim"))
    elif method == "turn/completed":
        turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
        duration = turn.get(
            "duration_ms",
            turn.get("durationMs", params.get("duration_ms", params.get("durationMs"))),
        )
        rendered.append(_turn_line(turn.get("status"), duration))
    elif method == "item/tool/call":
        rendered.append(
            _tool_line(
                f"{params.get('namespace') or '?'}.{params.get('tool') or '?'}",
                params.get("arguments"),
            )
        )
    elif method == "item/completed":
        item = params.get("item") if isinstance(params.get("item"), Mapping) else {}
        item_type = item.get("type")
        if item_type == "agentMessage":
            text = str(item.get("text") or "")
            if text:
                rendered.append(Markdown(text, code_theme="monokai"))
        elif item_type == "reasoning":
            summary = " ".join(str(part) for part in item.get("summary") or [])
            if summary:
                rendered.append(Text(f"[thinking] {_one_line(summary)}", style="dim"))
        elif item_type == "dynamicToolCall":
            success = bool(item.get("success"))
            rendered.append(
                Text(
                    f"[tool result] {item.get('tool')} success={item.get('success')}",
                    style="dim green" if success else "bold red",
                )
            )
        elif item_type != "userMessage":
            rendered.append(_line("[item] ", item_type or dict(item), "dim"))
    elif method == "thread/status/changed":
        status = (
            params.get("status") if isinstance(params.get("status"), Mapping) else {}
        )
        rendered.append(_line("[status] ", status.get("type") or "?", "dim"))
    elif not (method.startswith("item/") or method.startswith("thread/")):
        rendered.append(_line("[event] ", method or dict(raw), "dim"))
    return _append_verbose(rendered, raw, verbose)


def _render_raw(raw: Mapping[str, Any], verbose: bool = False) -> list[RenderableType]:
    if raw.get("type") == "wk_ledger":
        return []
    if "method" in raw:
        return render_codex(raw, verbose=verbose)
    return render_claude(raw, verbose=verbose)


def render_item(item: Mapping[str, Any], verbose: bool = False) -> list[RenderableType]:
    """Render a lane event envelope into Rich renderables."""

    raw = item.get("raw")
    if isinstance(raw, Mapping):
        return _render_raw(raw, verbose=verbose)
    if "method" in item or "type" in item:
        return _render_raw(item, verbose=verbose)
    return []


def render_event(
    item: Mapping[str, Any], verbose: bool = False
) -> list[RenderableType]:
    """Render either a raw provider event or a lane event envelope."""

    return render_item(item, verbose=verbose)
