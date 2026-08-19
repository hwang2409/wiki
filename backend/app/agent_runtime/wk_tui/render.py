"""Pure Rich renderers for wk lane events."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.text import Text

from .. import normalizer

MAX_LINE = 240
_CODEX_ERROR_METHODS = {
    "error",
    "codex.provider_error",
    "provider/protocolError",
    "provider/processExited",
    "wk.codex.tool_policy_unavailable",
}
_CODEX_RENDERED_METHODS = normalizer._CODEX_RENDERED_METHODS
_CODEX_SUMMARIZED_METHODS = normalizer._CODEX_SUMMARIZED_METHODS
_CODEX_IGNORED_METHODS = normalizer._CODEX_IGNORED_METHODS
_CODEX_APPROVAL_METHODS = normalizer._CODEX_APPROVAL_METHODS
_CLAUDE_IGNORED_TYPES = normalizer._CLAUDE_IGNORED_TYPES
_CODEX_DELTA_METHODS = {
    method
    for method in _CODEX_RENDERED_METHODS | _CODEX_SUMMARIZED_METHODS
    if method.startswith("item/")
    and (
        method.endswith("Delta")
        or method.endswith("delta")
        or method.endswith("summaryPartAdded")
        or method.endswith("terminalInteraction")
    )
}
_CODEX_SILENT_METHODS = _CODEX_DELTA_METHODS | {
    "item/userMessage",
    "thread/closed",
}
_NATIVE_TOOL_TYPES = {"commandExecution", "fileChange", "mcpToolCall"}


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
    return Text(
        f"[raw] {json.dumps(raw, default=str, sort_keys=True)}",
        style="dim",
    )


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


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _claude_blocks(
    message: object, event_kind: str, rendered: list[RenderableType]
) -> Sequence[object]:
    if not isinstance(message, Mapping):
        return ()
    content = message.get("content")
    if content is None:
        return ()
    if not _is_sequence(content):
        rendered.append(_line("[event] ", {"type": event_kind, "content": content}, "dim"))
        return ()
    return content


def _codex_error_line(method: str, params: Mapping[str, Any]) -> Text:
    if method == "provider/protocolError":
        prefix = "protocol error"
        detail = params.get("error") or params.get("message") or params
    elif method == "provider/processExited":
        prefix = "process exited"
        detail = params.get("error") or params.get("message") or params
    elif method == "turn/completed":
        prefix = "turn failed"
        turn = params.get("turn")
        detail = turn.get("error") if isinstance(turn, Mapping) else None
        detail = detail or turn or params
    else:
        detail = params.get("error") or params
        prefix = params.get("error_class") or "error"
    return Text(f"[error] {prefix}: {_one_line(detail)}", style="bold red")


def _failed_codex_turn(params: Mapping[str, Any]) -> bool:
    turn = params.get("turn")
    if not isinstance(turn, Mapping):
        return False
    status = str(turn.get("status") or "").lower()
    return status == "failed" or turn.get("error") is not None


def _native_tool_name(item: Mapping[str, Any]) -> str:
    item_type = str(item.get("type") or "tool")
    if item_type == "mcpToolCall":
        server = item.get("server") or "?"
        tool = item.get("tool") or "?"
        return f"{item_type} {server}.{tool}"
    return item_type


def _native_tool_input(item: Mapping[str, Any]) -> object:
    item_type = item.get("type")
    if item_type == "commandExecution":
        return item.get("command", item.get("cmd", item.get("input", {})))
    if item_type == "fileChange":
        return item.get("changes", item.get("input", item.get("patch", {})))
    if item_type == "webSearch":
        return item.get("query", item.get("input", {}))
    if item_type == "imageView":
        return item.get("path", item.get("input", {}))
    if item_type in {"collabToolCall", "collabAgentToolCall"}:
        return item.get("input", item.get("arguments", item.get("action", {})))
    return item.get("arguments", item.get("input", {}))


def _native_tool_result(item: Mapping[str, Any]) -> object:
    item_type = item.get("type")
    if item_type == "commandExecution":
        result = item.get("aggregatedOutput", item.get("output"))
        if result is None and (item.get("stdout") is not None or item.get("stderr") is not None):
            result = "\n".join(
                str(part) for part in (item.get("stdout"), item.get("stderr")) if part
            )
        return result
    if item_type == "fileChange":
        return item.get("output", item.get("result", item.get("error")))
    return item.get("error") or item.get("result")


def _native_tool_status(item: Mapping[str, Any]) -> tuple[str, bool]:
    if item.get("error") is not None:
        return "error", True
    result = item.get("result")
    if isinstance(result, Mapping) and result.get("isError") is True:
        return "error", True
    status = item.get("status")
    if isinstance(status, str) and status:
        if status.lower() in {
            "failed",
            "error",
            "declined",
            "cancelled",
            "canceled",
            "interrupted",
        }:
            return status, True
    exit_code = item.get("exit_code", item.get("exitCode"))
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return "error", True
    success = item.get("success")
    if isinstance(success, bool):
        return ("success" if success else "error"), not success
    if isinstance(status, str) and status:
        return status, False
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return ("success" if exit_code == 0 else "error"), exit_code != 0
    return "completed", False


def _native_tool_start_line(item: Mapping[str, Any]) -> Text:
    return Text.assemble(
        ("[tool] ", "dim"),
        (_native_tool_name(item), "bold cyan"),
        (" status=started input=", "dim"),
        (_one_line(_native_tool_input(item)), "dim"),
    )


_IGNORED_CODEX_ITEM_START = object()
_CODEX_ITEM_STARTED_RENDERERS = {
    "commandExecution": _native_tool_start_line,
    "fileChange": _native_tool_start_line,
    "mcpToolCall": _native_tool_start_line,
    "dynamicToolCall": _native_tool_start_line,
    "webSearch": _native_tool_start_line,
    "collabToolCall": _native_tool_start_line,
    "collabAgentToolCall": _native_tool_start_line,
    "imageView": _native_tool_start_line,
    "agentMessage": _IGNORED_CODEX_ITEM_START,
    "reasoning": _IGNORED_CODEX_ITEM_START,
    "userMessage": _IGNORED_CODEX_ITEM_START,
}


def _native_tool_result_line(item: Mapping[str, Any]) -> Text:
    status, error = _native_tool_status(item)
    style = "bold red" if error else "dim green"
    return Text.assemble(
        ("[tool result] ", "dim"),
        (status, style),
        (f" {_native_tool_name(item)} status={status} result=", style),
        (_one_line(_native_tool_result(item)), style),
    )


def render_claude(
    raw: Mapping[str, Any], verbose: bool = False
) -> list[RenderableType]:
    """Render one raw Claude event into Rich renderables."""

    kind = str(raw.get("type") or "")
    rendered: list[RenderableType] = []
    if kind in _CLAUDE_IGNORED_TYPES:
        pass
    elif kind == "system":
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
        for block in _claude_blocks(message, kind, rendered):
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
        for block in _claude_blocks(message, kind, rendered):
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
    if method in _CODEX_ERROR_METHODS or (
        method == "turn/completed" and _failed_codex_turn(params)
    ):
        rendered.append(_codex_error_line(method, params))
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
    elif method in _CODEX_APPROVAL_METHODS or method == "serverRequest/resolved":
        rendered.append(_line("[approval] ", params, "yellow"))
    elif method == "item/started":
        item = params.get("item")
        item_type = item.get("type") if isinstance(item, Mapping) else None
        item_renderer = (
            _CODEX_ITEM_STARTED_RENDERERS.get(item_type)
            if isinstance(item_type, str)
            else None
        )
        if item_renderer is _IGNORED_CODEX_ITEM_START:
            pass
        elif item_renderer is not None and isinstance(item, Mapping):
            rendered.append(item_renderer(item))
        elif item is None:
            rendered.append(_line("[codex] ", method, "dim"))
        else:
            rendered.append(_line("[event] ", method or dict(raw), "dim"))
    elif method == "item/completed":
        item = params.get("item") if isinstance(params.get("item"), Mapping) else {}
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type in _NATIVE_TOOL_TYPES:
            rendered.append(_native_tool_result_line(item))
        elif item_type == "agentMessage":
            text = str(item.get("text") or "")
            if text:
                rendered.append(Markdown(text, code_theme="monokai"))
        elif item_type == "reasoning":
            summary_value = item.get("summary")
            if summary_value is not None and not _is_sequence(summary_value):
                rendered.append(_line("[item] ", item, "dim"))
                return _append_verbose(rendered, raw, verbose)
            summary = " ".join(str(part) for part in summary_value or [])
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
    elif method == "turn/moderationMetadata" and normalizer._codex_moderation_is_warning(
        params
    ):
        rendered.append(_line("[warning] ", params, "yellow"))
    elif method in _CODEX_SILENT_METHODS or method in _CODEX_SUMMARIZED_METHODS:
        pass
    elif method in _CODEX_IGNORED_METHODS:
        pass
    elif method in _CODEX_RENDERED_METHODS:
        rendered.append(_line("[codex] ", method, "dim"))
    else:
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
