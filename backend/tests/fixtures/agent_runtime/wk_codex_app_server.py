#!/usr/bin/env python3
"""Small real-process Codex App Server fixture for the wk lane tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


TOOLS = [
    "mcp__wiki__read",
    "mcp__wiki__write",
    "mcp__wiki__edit",
    "mcp__wiki__bash",
    "mcp__wiki__gate",
    "mcp__wiki__status",
]


def send(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def codex_home() -> Path:
    return Path(os.environ["CODEX_HOME"])


def policy() -> dict[str, Any]:
    path = codex_home() / "policy.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("policy fixture must be an object")
        return value
    return {"owner": "wiki", "tools": TOOLS, "nativeToolsDisabled": True}


def auth_status() -> None:
    status_path = codex_home() / "auth-status.txt"
    identity = status_path.read_text(encoding="utf-8").strip() if status_path.exists() else "account-a organization-a"
    account, _, organization = identity.partition(" ")
    print(f"Logged in using ChatGPT account={account} organization={organization}")


def thread_result(thread_id: str, transcript: Path) -> dict[str, Any]:
    return {
        "thread": {
            "id": thread_id,
            "sessionId": thread_id,
            "path": str(transcript),
            "status": {"type": "idle"},
        },
        "toolPolicy": policy(),
    }


def emit_turn(thread_id: str, turn_id: str) -> None:
    send(
        {
            "method": "thread/status/changed",
            "params": {"threadId": thread_id, "status": {"type": "active"}},
        }
    )
    send(
        {
            "method": "turn/started",
            "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress"}},
        }
    )
    item = {
        "type": "mcpToolCall",
        "id": "wk-tool-1",
        "server": "wiki",
        "tool": "read",
        "arguments": {"path": "README.md"},
        "status": "inProgress",
    }
    send(
        {
            "method": "item/started",
            "params": {"threadId": thread_id, "turnId": turn_id, "item": item},
        }
    )
    if not (codex_home() / "drop-tool-result").exists():
        completed_item = dict(item)
        completed_item.update(
            {
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "fixture read"}]},
            }
        )
        send(
            {
                "method": "item/completed",
                "params": {"threadId": thread_id, "turnId": turn_id, "item": completed_item},
            }
        )
    send(
        {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "id": "agent-message-1",
                    "text": "fixture response",
                    "status": "completed",
                },
            },
        }
    )
    send(
        {
            "method": "turn/completed",
            "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}},
        }
    )
    send(
        {
            "method": "thread/status/changed",
            "params": {"threadId": thread_id, "status": {"type": "idle"}},
        }
    )


if len(sys.argv) >= 3 and sys.argv[1:3] == ["login", "status"]:
    auth_status()
    raise SystemExit(0)

thread_id = "wk-thread-1"
turn_number = 0
transcript = codex_home() / "sessions" / f"rollout-{thread_id}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
transcript_handle = transcript.open("a", encoding="utf-8")

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "wk-codex-fixture/1"}})
    elif method == "thread/start":
        send({"id": request_id, "result": thread_result(thread_id, transcript)})
    elif method == "thread/resume":
        send({"id": request_id, "result": thread_result(str(params["threadId"]), transcript)})
    elif method == "thread/read":
        result = thread_result(thread_id, transcript)
        result["thread"]["status"] = {"type": "idle"}
        send({"id": request_id, "result": result})
    elif method == "turn/start":
        turn_number += 1
        turn_id = f"wk-turn-{turn_number}"
        if (codex_home() / "approval").exists():
            send(
                {
                    "id": 71,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": "wk-approval-1",
                        "reason": "fixture approval",
                        "command": ["wiki", "gate"],
                    },
                }
            )
            response_line = sys.stdin.readline()
            response = json.loads(response_line)
            if response.get("id") != 71:
                raise RuntimeError("approval response id did not match")
            send(
                {
                    "method": "serverRequest/resolved",
                    "params": {"threadId": thread_id, "requestId": 71},
                }
            )
        send({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        emit_turn(thread_id, turn_id)
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
        send(
            {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": {"id": "wk-turn-interrupted", "status": "interrupted"}},
            }
        )
    elif method == "turn/steer":
        send({"id": request_id, "result": {"turnId": params.get("expectedTurnId")}})
    elif method == "thread/archive":
        send({"id": request_id, "result": {}})
        send({"method": "thread/archived", "params": {"threadId": thread_id}})
    else:
        send({"id": request_id, "result": {}})

transcript_handle.close()
