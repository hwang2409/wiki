from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from rich.console import Console
from rich.text import Text

from backend.app.agent_runtime.wk_tui.render import render_event, render_item


def _item(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"raw": raw, "event": {}}


def _capture(renderables: Iterable[object]) -> str:
    console = Console(record=True, width=100)
    for renderable in renderables:
        console.print(renderable)
    return console.export_text()


def test_claude_events_render_markdown_and_lifecycle() -> None:
    raw_events = [
        {"type": "system", "subtype": "init", "session_id": "session-1"},
        {"type": "system", "subtype": "hook_started"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "## answer\n\n```python\nprint('ok')\n```",
                    },
                    {"type": "thinking", "thinking": "checking the input"},
                    {
                        "type": "tool_use",
                        "name": "read",
                        "input": {"path": "README.md"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "file contents",
                        "is_error": False,
                    },
                    {
                        "type": "tool_result",
                        "content": "exit code 17",
                        "is_error": True,
                    },
                ]
            },
        },
        {"type": "result", "subtype": "success", "duration_ms": 12},
        {"type": "control_request", "request": {"question": "continue?"}},
        {"type": "provider_error", "error_class": "WkError", "error": "boom"},
        {"type": "stream_event"},
        {"type": "unseen", "value": 1},
    ]

    text = "\n".join(_capture(render_item(_item(raw))) for raw in raw_events)

    assert "session session-1 started" in text
    assert "hook_started" in text
    assert "answer" in text
    assert "print('ok')" in text
    assert "[thinking] checking the input" in text
    assert "[tool] read" in text
    assert "[tool result] ok" in text
    assert "[tool result] error" in text
    assert "[turn] success (12 ms)" in text
    assert "[approval]" in text
    assert "[error] WkError: boom" in text
    assert "[event] unseen" in text
    assert _capture(render_item(_item({"type": "stream_event"}))) == ""


def test_raw_events_can_render_without_an_envelope() -> None:
    assert "answer" in _capture(
        render_event(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "answer"}]},
            }
        )
    )
    assert "started" in _capture(render_item({"method": "thread/started"}))


def test_codex_events_render_every_branch() -> None:
    cases = [
        ({"method": "error", "params": {"error": "boom"}}, "[error] error: boom"),
        (
            {"method": "codex.provider_error", "params": {"error": "down"}},
            "[error] error: down",
        ),
        (
            {
                "method": "wk.codex.tool_policy_unavailable",
                "params": {"error": "blocked"},
            },
            "blocked",
        ),
        ({"method": "thread/started", "params": {}}, "[thread] started"),
        ({"method": "turn/started", "params": {}}, "[turn] started"),
        (
            {
                "method": "turn/completed",
                "params": {"turn": {"status": "completed", "durationMs": 4}},
            },
            "[turn] completed (4 ms)",
        ),
        (
            {
                "method": "item/tool/call",
                "params": {
                    "namespace": "wiki",
                    "tool": "read",
                    "arguments": {"path": "README.md"},
                },
            },
            "[tool] wiki.read",
        ),
        (
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "done"}},
            },
            "done",
        ),
        (
            {
                "method": "item/completed",
                "params": {
                    "item": {"type": "reasoning", "summary": ["checking", "work"]}
                },
            },
            "[thinking] checking work",
        ),
        (
            {
                "method": "item/completed",
                "params": {
                    "item": {"type": "dynamicToolCall", "tool": "read", "success": True}
                },
            },
            "[tool result] read success=True",
        ),
        (
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "dynamicToolCall",
                        "tool": "write",
                        "success": False,
                    }
                },
            },
            "[tool result] write success=False",
        ),
        (
            {
                "method": "item/completed",
                "params": {"item": {"type": "other", "id": "x"}},
            },
            "[item] other",
        ),
        (
            {"method": "thread/status/changed", "params": {"status": {"type": "idle"}}},
            "[status] idle",
        ),
        ({"method": "item/started", "params": {}}, ""),
        ({"method": "thread/closed", "params": {}}, ""),
        ({"method": "custom/event", "params": {}}, "[event] custom/event"),
    ]

    for raw, expected in cases:
        assert expected in _capture(render_item(_item(raw)))

    assert render_item(_item({"method": "item/userMessage", "params": {}})) == []


def test_verbose_adds_truncated_raw_dump_after_output() -> None:
    raw = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "answer"}]},
        "large": "x" * 400,
    }

    renderables = render_item(_item(raw), verbose=True)
    assert len(renderables) == 2
    text = _capture(renderables)
    assert text.index("answer") < text.index("[raw]")
    assert '"large":' in text
    assert isinstance(renderables[1], Text)
    assert len(renderables[1].plain.removeprefix("[raw] ")) <= 243

    assert "[raw]" in _capture(
        render_item(_item({"type": "stream_event"}), verbose=True)
    )
    assert render_item({"raw": {"type": "wk_ledger"}, "event": {}}, verbose=True) == []
