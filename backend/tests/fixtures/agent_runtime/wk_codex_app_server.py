#!/usr/bin/env python3
"""Documented JSONL Codex App Server fixture at the process boundary."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


TOOLS = {"read", "write", "edit", "bash", "gate", "status"}


def send(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def codex_home() -> Path:
    return Path(os.environ["CODEX_HOME"])


def account() -> dict[str, Any]:
    value = "account-a organization-a"
    path = codex_home() / "auth-status.txt"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
    parts = value.split()
    return {
        "type": "chatgpt",
        "planType": "pro",
        "email": parts[0] if parts else "account-a",
        "accountId": parts[0] if parts else "account-a",
        "organizationId": parts[1] if len(parts) > 1 else "organization-a",
    }


def thread_result(thread_id: str, transcript: Path) -> dict[str, Any]:
    return {
        "thread": {
            "id": thread_id,
            "path": str(transcript),
            "status": {"type": "idle"},
        }
    }


def emit_turn(thread_id: str, turn_id: str, call_id: str = "tool-1") -> None:
    tool = "gate" if (codex_home() / "gate-call").exists() else "read"
    arguments = {"pr": "230"} if tool == "gate" else {"path": "README.md"}
    send({"method": "thread/status/changed", "params": {"threadId": thread_id, "status": {"type": "active"}}})
    send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress"}}})
    item = {"type": "dynamicToolCall", "id": "wk-tool-1", "name": tool, "status": "inProgress"}
    send({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn_id, "item": item}})
    send(
        {
            "id": 60,
            "method": "item/tool/call",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "callId": call_id,
                "namespace": "wiki",
                "tool": tool,
                "arguments": arguments,
            },
        }
    )
    response = {}
    while response.get("id") != 60:
        response_line = sys.stdin.readline()
        response = json.loads(response_line)
        if response.get("method") == "account/read":
            send({"id": response.get("id"), "result": {"account": account()}})
    if response.get("id") != 60:
        raise RuntimeError("dynamic tool response id did not match")
    if not (codex_home() / "drop-tool-result").exists():
        completed = dict(item)
        completed.update({"status": "completed", "contentItems": response.get("result", {}).get("contentItems", []), "success": response.get("result", {}).get("success", False)})
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "item": completed}})
    send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "item": {"type": "agentMessage", "id": "agent-message-1", "text": "fixture response", "status": "completed"}}})
    send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}}})
    send({"method": "thread/status/changed", "params": {"threadId": thread_id, "status": {"type": "idle"}}})


thread_id = "wk-thread-1"
turn_number = 0
transcript = codex_home() / "sessions" / f"rollout-{thread_id}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
transcript_handle = transcript.open("a", encoding="utf-8")

try:
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        (codex_home() / "transport.log").open("a", encoding="utf-8").write(f"{method}\n")
        request_id = request.get("id")
        params = request.get("params") or {}
        if method == "initialize":
            send({"id": request_id, "result": {"userAgent": "wk-codex-fixture/1"}})
        elif method == "account/read":
            send({"id": request_id, "result": {"account": account()}})
        elif method == "thread/start":
            dynamic_tools = params.get("dynamicTools")
            names = {item.get("name") for item in dynamic_tools or [] if isinstance(item, dict)}
            if names != TOOLS or len(names) != len(dynamic_tools or []):
                send({"id": request_id, "error": {"code": -32000, "message": "dynamicTools ownership proof failed"}})
            else:
                send({"id": request_id, "result": thread_result(thread_id, transcript)})
        elif method == "thread/resume":
            send({"id": request_id, "result": thread_result(str(params["threadId"]), transcript)})
        elif method == "thread/read":
            send({"id": request_id, "result": thread_result(thread_id, transcript)})
        elif method == "turn/start":
            turn_number += 1
            turn_id = f"wk-turn-{turn_number}"
            if (codex_home() / "approval").exists():
                send({"id": 71, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "wk-approval-1", "reason": "fixture approval", "command": ["wiki", "gate"]}})
                approval_line = sys.stdin.readline()
                approval_response = json.loads(approval_line)
                if approval_response.get("id") != 71:
                    raise RuntimeError("approval response id did not match")
                send({"method": "serverRequest/resolved", "params": {"threadId": thread_id, "requestId": 71}})
            send({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            emit_turn(thread_id, turn_id)
        elif method == "turn/interrupt":
            send({"id": request_id, "result": {}})
            send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": "wk-turn-interrupted", "status": "interrupted"}}})
        elif method == "turn/steer":
            send({"id": request_id, "result": {"turnId": params.get("expectedTurnId")}})
        elif method == "thread/archive":
            send({"id": request_id, "result": {}})
            send({"method": "thread/archived", "params": {"threadId": thread_id}})
        else:
            send({"id": request_id, "result": {}})
finally:
    transcript_handle.close()
