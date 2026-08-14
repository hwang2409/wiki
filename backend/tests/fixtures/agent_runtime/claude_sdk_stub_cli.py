#!/usr/bin/env python3
"""Small Claude Code process fixture for the real SDK subprocess transport."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


TOOLS = [
    "mcp__wiki__read",
    "mcp__wiki__write",
    "mcp__wiki__edit",
    "mcp__wiki__bash",
    "mcp__wiki__gate",
    "mcp__wiki__status",
]


def send(value: dict[str, object]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


if sys.argv[1:4] == ["auth", "status", "--json"]:
    send(
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
        }
    )
    raise SystemExit(0)

capture = os.environ.get("WK_ENV_CAPTURE")
capture_path = Path(capture) if capture else Path(os.environ["CLAUDE_CONFIG_DIR"]) / "env-capture.json"
if capture_path:
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_text(
        json.dumps(dict(os.environ), sort_keys=True), encoding="utf-8"
    )

session_id = "sdk-subprocess-session"
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "control_request":
        request_id = str(message["request_id"])
        request = message.get("request") or {}
        send(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {"session_id": session_id}
                    if request.get("subtype") == "initialize"
                    else {},
                },
            }
        )
        if request.get("subtype") == "initialize":
            send(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": session_id,
                    "apiKeySource": "none",
                    "tools": TOOLS,
                    "hooks": [],
                    "setting_sources": [],
                }
            )
    elif message.get("type") == "user":
        tool_id = "tool-subprocess-1"
        send(
            {
                "type": "assistant",
                "session_id": session_id,
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "mcp__wiki__read",
                            "input": {"path": "README.md"},
                        }
                    ],
                },
            }
        )
        if os.environ.get("WK_DROP_TOOL_RESULT") != "1":
            send(
                {
                    "type": "user",
                    "session_id": session_id,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": "fixture read",
                                "is_error": False,
                            }
                        ],
                    },
                }
            )
        send(
            {
                "type": "result",
                "subtype": "success",
                "session_id": session_id,
                "is_error": False,
                "duration_ms": 1,
                "duration_api_ms": 1,
                "num_turns": 1,
                "result": "fixture complete",
            }
        )
