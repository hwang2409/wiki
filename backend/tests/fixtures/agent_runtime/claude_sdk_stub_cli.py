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
    auth_status_path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "auth-status.json"
    auth_status = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "accountId": "account-a",
        "orgId": "organization-a",
        "subscriptionType": "max",
    }
    if auth_status_path.exists():
        auth_status.update(json.loads(auth_status_path.read_text(encoding="utf-8")))
    send(
        auth_status
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
permission_prompt_enabled = "--permission-prompt-tool" in sys.argv
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
        if permission_prompt_enabled:
            send(
                {
                    "type": "control_request",
                    "request_id": "permission-subprocess-1",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "mcp__wiki__read",
                        "input": {"path": "README.md"},
                        "tool_use_id": tool_id,
                        "permission_suggestions": [],
                    },
                }
            )
            for response_line in sys.stdin:
                response = json.loads(response_line)
                if response.get("type") != "control_response":
                    continue
                envelope = response.get("response") or {}
                if envelope.get("request_id") != "permission-subprocess-1":
                    continue
                result = envelope.get("response") or {}
                if result.get("behavior") != "allow":
                    send(
                        {
                            "type": "result",
                            "subtype": "error_during_execution",
                            "session_id": session_id,
                            "is_error": True,
                            "duration_ms": 1,
                            "duration_api_ms": 1,
                            "num_turns": 1,
                            "result": "permission denied",
                        }
                    )
                    raise SystemExit(1)
                break
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
