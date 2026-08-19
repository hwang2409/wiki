from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import pytest

pytest.importorskip("rich")

from rich.console import Console
from rich.markdown import Markdown
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

    thinking = render_item(
        _item(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "thinking", "thinking": "private"}]
                },
            }
        )
    )
    assert isinstance(thinking[0], Text)
    assert str(thinking[0].style) == "dim"


def test_claude_markdown_renders_nested_fences() -> None:
    nested = "Here is a sample:\n\n````markdown\n```python\nprint('ok')\n```\n````"
    rendered = render_item(
        _item(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": nested}]},
            }
        )
    )

    assert isinstance(rendered[0], Markdown)
    assert "print('ok')" in _capture(rendered)


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
        (
            {
                "method": "provider/protocolError",
                "params": {"error": "invalid frame"},
            },
            "[error] protocol error: invalid frame",
        ),
        (
            {
                "method": "provider/processExited",
                "params": {"returncode": 137},
            },
            "[error] process exited",
        ),
        (
            {
                "method": "turn/completed",
                "params": {"turn": {"status": "failed", "error": "timed out"}},
            },
            "[error] turn failed: timed out",
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
                "method": "item/started",
                "params": {
                    "item": {"type": "commandExecution", "command": "echo hi"}
                },
            },
            "[tool] commandExecution status=started input=echo hi",
        ),
        (
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "command": "echo hi",
                        "status": "completed",
                        "aggregatedOutput": "hi",
                    }
                },
            },
            "[tool result] completed commandExecution status=completed result=hi",
        ),
        (
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "fileChange",
                        "changes": {"README.md": "updated"},
                    }
                },
            },
            "[tool] fileChange status=started input=",
        ),
        (
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "fileChange",
                        "status": "completed",
                        "output": "updated README.md",
                    }
                },
            },
            "[tool result] completed fileChange status=completed result=updated README.md",
        ),
        (
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "server": "filesystem",
                        "tool": "list_dir",
                        "arguments": {"path": "/tmp"},
                    }
                },
            },
            "[tool] mcpToolCall filesystem.list_dir status=started input=",
        ),
        (
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "server": "filesystem",
                        "tool": "list_dir",
                        "status": "completed",
                        "result": {"content": [{"text": "a.txt"}]},
                    }
                },
            },
            "[tool result] completed mcpToolCall filesystem.list_dir status=completed result=",
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
        ({"method": "item/started", "params": {}}, "[event] item/started"),
        ({"method": "thread/closed", "params": {}}, ""),
        ({"method": "custom/event", "params": {}}, "[event] custom/event"),
        (
            {"method": "item/novel", "params": {"value": 1}},
            "[event] item/novel",
        ),
        (
            {"method": "thread/novel", "params": {"value": 1}},
            "[event] thread/novel",
        ),
    ]

    for raw, expected in cases:
        assert expected in _capture(render_item(_item(raw)))

    assert render_item(_item({"method": "item/userMessage", "params": {}})) == []
    for item_type in ("agentMessage", "reasoning", "userMessage"):
        assert (
            render_item(
                _item({"method": "item/started", "params": {"item": {"type": item_type}}})
            )
            == []
        )


def test_known_codex_noise_is_ignored_but_unknown_methods_are_visible() -> None:
    ignored_methods = (
        "item/agentMessage/delta",
        "item/commandExecution/outputDelta",
        "item/commandExecution/terminalInteraction",
        "item/delta",
        "item/dynamicToolCall/outputDelta",
        "item/fileChange/outputDelta",
        "item/mcpToolCall/outputDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "hook/completed",
        "hook/started",
    )

    for method in ignored_methods:
        assert render_item(_item({"method": method, "params": {}})) == []
    assert "[event] item/started" in _capture(
        render_item(_item({"method": "item/started", "params": {"item": {"type": "new"}}}))
    )


def test_native_tool_lines_include_inputs_and_results() -> None:
    file_start = _capture(
        render_item(
            _item(
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "type": "fileChange",
                            "changes": {"README.md": "updated"},
                        }
                    },
                }
            )
        )
    )
    mcp_start = _capture(
        render_item(
            _item(
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "type": "mcpToolCall",
                            "server": "filesystem",
                            "tool": "list_dir",
                            "arguments": {"path": "/tmp"},
                        }
                    },
                }
            )
        )
    )
    mcp_result = _capture(
        render_item(
            _item(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "mcpToolCall",
                            "server": "filesystem",
                            "tool": "list_dir",
                            "status": "completed",
                            "result": {"content": [{"text": "a.txt"}]},
                        }
                    },
                }
            )
        )
    )

    assert "README.md" in file_start
    assert "updated" in file_start
    assert "/tmp" in mcp_start
    assert "a.txt" in mcp_result


@pytest.mark.parametrize(
    ("item_update", "status_text"),
    [
        ({"status": "canceled"}, "canceled"),
        ({"status": "declined"}, "declined"),
        ({"status": "interrupted"}, "interrupted"),
        ({"exit_code": 1}, "error"),
    ],
)
def test_native_tool_failure_states_use_error_style(
    item_update: Mapping[str, Any], status_text: str
) -> None:
    item = {"type": "commandExecution", "command": "echo hi", **item_update}
    rendered = render_item(
        _item({"method": "item/completed", "params": {"item": item}})
    )

    assert isinstance(rendered[0], Text)
    assert status_text in rendered[0].plain
    offset = rendered[0].plain.index(status_text)
    style = next(
        span.style
        for span in rendered[0].spans
        if span.start <= offset < span.end
    )
    assert str(style) == "bold red"


def test_codex_errors_use_error_style() -> None:
    cases = [
        {"method": "provider/protocolError", "params": {"error": "bad"}},
        {"method": "provider/processExited", "params": {"returncode": 1}},
        {
            "method": "turn/completed",
            "params": {"turn": {"status": "failed", "error": "bad"}},
        },
    ]

    for raw in cases:
        rendered = render_item(_item(raw))
        assert isinstance(rendered[0], Text)
        assert str(rendered[0].style) == "bold red"


def test_malformed_content_and_reasoning_render_fallbacks() -> None:
    claude = render_item(
        _item(
            {
                "type": "assistant",
                "message": {"content": 12},
            }
        )
    )
    reasoning = render_item(
        _item(
            {
                "method": "item/completed",
                "params": {"item": {"type": "reasoning", "summary": 12}},
            }
        )
    )

    assert "[event]" in _capture(claude)
    assert "[item]" in _capture(reasoning)


def test_verbose_adds_complete_raw_json_after_output() -> None:
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
    payload = json.loads(renderables[1].plain.removeprefix("[raw] "))
    assert payload == raw

    assert "[raw]" in _capture(
        render_item(_item({"type": "stream_event"}), verbose=True)
    )
    assert render_item({"raw": {"type": "wk_ledger"}, "event": {}}, verbose=True) == []
