#!/usr/bin/env python3
"""Replay frames recorded from codex-cli 0.147.0 at the stdio boundary."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


TOOLS = {"read", "write", "edit", "bash", "gate", "status"}
THREAD_ID = "01a0027e-08de-7c40-9c71-46d2c364c51f"
TURN_ID = "01a0027e-099c-7753-a546-2a6883f3617f"
ITEM_ID = "exec-1c3ccf04-6827-4b3f-b582-24ffa15eb9ae"
REASONING_ID = "rs_072e39cf388bc953016a7f9d359de4819492f61f837942ffee"


def send(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def home() -> Path:
    return Path(os.environ["CODEX_HOME"])


def account() -> dict[str, Any]:
    value = "account-a organization-a"
    path = home() / "auth-status.txt"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
    parts = value.split()
    account_id = parts[0] if parts else "account-a"
    organization_id = parts[1] if len(parts) > 1 else "organization-a"
    return {
        "type": "chatgpt",
        "planType": "pro",
        "email": account_id,
        "id": account_id,
        "accountId": account_id,
        "organizationId": organization_id,
    }


def thread_value(thread_id: str, transcript: Path, cwd: str) -> dict[str, Any]:
    return {
        "id": thread_id,
        "extra": None,
        "sessionId": thread_id,
        "forkedFromId": None,
        "parentThreadId": None,
        "preview": "",
        "ephemeral": False,
        "section": None,
        "sectionEnteredAt": None,
        "historyMode": "legacy",
        "modelProvider": "openai",
        "createdAt": 1786748209,
        "updatedAt": 1786748209,
        "recencyAt": 1786748209,
        "status": {"type": "idle"},
        "path": str(transcript),
        "cwd": cwd,
        "cliVersion": "0.147.0",
        "source": "vscode",
        "canAcceptDirectInput": True,
        "threadSource": None,
        "agentNickname": None,
        "agentRole": None,
        "gitInfo": None,
        "name": None,
        "turns": [],
    }


def thread_result(thread_id: str, transcript: Path, cwd: str) -> dict[str, Any]:
    return {
        "thread": thread_value(thread_id, transcript, cwd),
        "model": "gpt-5.6-terra",
        "modelProvider": "openai",
        "serviceTier": "priority",
        "cwd": cwd,
        "runtimeWorkspaceRoots": [cwd],
        "instructionSources": [],
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": (
            {"type": "dangerFullAccess"}
            if (home() / "unsafe-sandbox").exists()
            else {"type": "readOnly", "networkAccess": False}
        ),
        "activePermissionProfile": None,
        "reasoningEffort": "xhigh",
        "multiAgentMode": "explicitRequestOnly",
    }


def trust_project(cwd: str) -> None:
    config = home() / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    marker = f'[projects."{cwd}"]'
    if marker not in existing:
        separator = "\n" if existing and not existing.endswith("\n") else ""
        config.write_text(
            f'{existing}{separator}\n{marker}\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )


def emit_turn(thread_id: str, turn_id: str, call_id: str) -> None:
    tool = "gate" if (home() / "gate-call").exists() else "read"
    arguments = {"pr": "230"} if tool == "gate" else {"path": "README.md"}
    send({"method": "thread/status/changed", "params": {"threadId": thread_id, "status": {"type": "active", "activeFlags": []}}})
    send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id, "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": None, "startedAt": 1786748209, "completedAt": None, "durationMs": None}}})
    send({"method": "item/started", "params": {"item": {"type": "userMessage", "id": "user-message-1", "clientId": None, "content": [{"type": "text", "text": "Call wiki.read now with path README.md. Do not answer until it returns.", "text_elements": []}]}, "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1786748212074}})
    send({"method": "item/completed", "params": {"item": {"type": "userMessage", "id": "user-message-1", "clientId": None, "content": [{"type": "text", "text": "Call wiki.read now with path README.md. Do not answer until it returns.", "text_elements": []}]}, "threadId": thread_id, "turnId": turn_id, "completedAtMs": 1786748212074}})
    send({"method": "item/started", "params": {"item": {"type": "reasoning", "id": REASONING_ID, "summary": [], "content": []}, "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1786748213715}})
    send({"method": "item/reasoning/summaryPartAdded", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": REASONING_ID, "summaryIndex": 0}})
    send({"method": "item/reasoning/summaryTextDelta", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": REASONING_ID, "delta": "**Planning single README read**", "summaryIndex": 0}})
    send({"method": "item/completed", "params": {"item": {"type": "reasoning", "id": REASONING_ID, "summary": ["**Planning single README read**"], "content": []}, "threadId": thread_id, "turnId": turn_id, "completedAtMs": 1786748215069}})
    item = {"type": "dynamicToolCall", "id": call_id, "namespace": "wiki", "tool": tool, "arguments": arguments, "status": "inProgress", "contentItems": None, "success": None, "durationMs": None}
    send({"method": "item/started", "params": {"item": item, "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1786748215466}})
    send({"id": 0, "method": "item/tool/call", "params": {"threadId": thread_id, "turnId": turn_id, "callId": call_id, "namespace": "wiki", "tool": tool, "arguments": arguments}})
    response: dict[str, Any] = {}
    while response.get("id") != 0:
        response = json.loads(sys.stdin.readline())
        if response.get("method") == "account/read":
            send({"id": response.get("id"), "result": {"account": account()}})
    result = response.get("result", {})
    if not (home() / "drop-tool-result").exists():
        completed = dict(item)
        completed.update({"status": "completed", "contentItems": result.get("contentItems", []), "success": result.get("success", False), "durationMs": 68})
        send({"method": "item/completed", "params": {"item": completed, "threadId": thread_id, "turnId": turn_id, "completedAtMs": 1786748215535}})
    send({"method": "item/started", "params": {"item": {"type": "agentMessage", "id": "agent-message-1", "text": "", "phase": "final_answer", "memoryCitation": None}, "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1786748216876}})
    send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "agent-message-1", "text": "", "phase": "final_answer", "memoryCitation": None}, "threadId": thread_id, "turnId": turn_id, "completedAtMs": 1786748217032}})
    send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}}})
    send({"method": "thread/status/changed", "params": {"threadId": thread_id, "status": {"type": "idle"}}})


transcript = home() / "sessions" / f"rollout-{THREAD_ID}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
transcript_handle = transcript.open("a", encoding="utf-8")

try:
    turn_number = 0
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        with (home() / "transport.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{method}\n")
        request_id = request.get("id")
        params = request.get("params") or {}
        if method == "initialize":
            send({"id": request_id, "result": {"userAgent": "wiki-wk-recording-gpt56/0.147.0 (Mac OS; arm64)"}})
        elif method == "account/read":
            send({"id": request_id, "result": {"account": account()}})
        elif method == "thread/start":
            dynamic_tools = params.get("dynamicTools")
            (home() / "thread-start-sandbox.json").write_text(
                json.dumps(params.get("sandboxPolicy"), separators=(",", ":")),
                encoding="utf-8",
            )
            namespace = dynamic_tools[0] if isinstance(dynamic_tools, list) and len(dynamic_tools) == 1 else {}
            tools = namespace.get("tools") if isinstance(namespace, dict) else None
            names = {item.get("name") for item in tools or [] if isinstance(item, dict)}
            if namespace.get("type") != "namespace" or namespace.get("name") != "wiki" or names != TOOLS:
                send({"id": request_id, "error": {"code": -32000, "message": "dynamicTools ownership proof failed"}})
            else:
                cwd = str(params.get("cwd", Path.cwd()))
                trust_project(cwd)
                send({"id": request_id, "result": thread_result(THREAD_ID, transcript, cwd)})
                send({"method": "thread/started", "params": {"thread": thread_value(THREAD_ID, transcript, cwd), "emittedAtMs": 1786748209563}})
        elif method in {"thread/resume", "thread/read"}:
            cwd = str(params.get("cwd", Path.cwd()))
            send({"id": request_id, "result": thread_result(str(params.get("threadId", THREAD_ID)), transcript, cwd)})
        elif method == "turn/start":
            turn_number += 1
            turn_id = f"wk-turn-{turn_number}"
            turn_input = params.get("input")
            if isinstance(turn_input, list) and turn_input and isinstance(turn_input[0], dict):
                with (home() / "turn-prompts.log").open("a", encoding="utf-8") as handle:
                    handle.write(f"{turn_input[0].get('text', '')}\n")
            if (home() / "approval").exists():
                send({"id": 71, "method": "item/commandExecution/requestApproval", "params": {"threadId": THREAD_ID, "turnId": turn_id, "itemId": ITEM_ID, "reason": "fixture approval", "command": ["wiki", "gate"]}})
                approval = json.loads(sys.stdin.readline())
                if approval.get("id") != 71:
                    raise RuntimeError("approval response id did not match")
                send({"method": "serverRequest/resolved", "params": {"threadId": THREAD_ID, "requestId": 71}})
            send({"id": request_id, "result": {"turn": {"id": turn_id, "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": None, "startedAt": None, "completedAt": None, "durationMs": None}}})
            emit_turn(THREAD_ID, turn_id, f"{ITEM_ID}-{turn_number}")
        elif method == "turn/interrupt":
            send({"id": request_id, "result": {}})
            send({"method": "turn/completed", "params": {"threadId": THREAD_ID, "turn": {"id": "wk-turn-interrupted", "status": "interrupted"}}})
        elif method == "turn/steer":
            send({"id": request_id, "result": {"turnId": params.get("expectedTurnId")}})
        elif method == "thread/archive":
            send({"id": request_id, "result": {}})
            send({"method": "thread/archived", "params": {"threadId": THREAD_ID}})
        else:
            send({"id": request_id, "result": {}})
finally:
    transcript_handle.close()
