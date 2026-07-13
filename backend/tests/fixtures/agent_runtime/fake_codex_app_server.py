from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def send(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


log_path = Path(os.environ["FAKE_PROTOCOL_LOG"])
args = sys.argv[1:]
approval = os.environ.get("FAKE_CODEX_APPROVAL") == "1"
artifact_tool = os.environ.get("FAKE_ARTIFACT_TOOL") == "1"
turn_start_error = os.environ.get("FAKE_TURN_START_ERROR") == "1"
thread_counter = 0
thread_id: str | None = None
active_turn: str | None = None
pending_turn_request_id: str | int | None = None
open_files = []

with log_path.open("a", encoding="utf-8") as log:
    log.write(
        json.dumps(
            {
                "tmux": os.environ.get("TMUX"),
                "tmux_pane": os.environ.get("TMUX_PANE"),
            },
            separators=(",", ":"),
        )
        + "\n"
    )

if os.environ.get("FAKE_OVERSIZED") == "1":
    sys.stdout.write(json.dumps({"oversized": "x" * (5 * 1024 * 1024)}) + "\n")
    sys.stdout.flush()
if os.environ.get("FAKE_OVERSIZED_STDERR") == "1":
    sys.stderr.write("x" * (5 * 1024 * 1024) + "\n")
    sys.stderr.flush()
if os.environ.get("FAKE_EOF_LIVE") == "1":
    os.close(sys.stdout.fileno())
    time.sleep(30)

for raw_line in sys.stdin:
    value = json.loads(raw_line)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(value, separators=(",", ":")) + "\n")
    method = value.get("method")
    request_id = value.get("id")
    params = value.get("params") or {}
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake-codex/1"}})
    elif method == "thread/start":
        thread_counter += 1
        thread_id = f"thread-{thread_counter}"
        transcript = (
            Path(os.environ["FAKE_CODEX_TRANSCRIPT_DIR"]) / f"rollout-{thread_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        handle = transcript.open("a", encoding="utf-8")
        open_files.append(handle)
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": thread_id,
                        "sessionId": thread_id,
                        "path": str(transcript),
                        "status": {"type": "idle"},
                    },
                    "model": params.get("model"),
                },
            }
        )
    elif method == "thread/resume":
        thread_id = params["threadId"]
        transcript = (
            Path(os.environ["FAKE_CODEX_TRANSCRIPT_DIR"]) / f"rollout-{thread_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        handle = transcript.open("a", encoding="utf-8")
        open_files.append(handle)
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": thread_id,
                        "sessionId": thread_id,
                        "path": str(transcript),
                        "status": {"type": "idle"},
                    }
                },
            }
        )
    elif method == "turn/start":
        active_turn = f"turn-{thread_id}"
        if approval:
            pending_turn_request_id = request_id
        elif not turn_start_error:
            send(
                {
                    "id": request_id,
                    "result": {"turn": {"id": active_turn, "status": "inProgress"}},
                }
            )
        send(
            {
                "method": "thread/status/changed",
                "params": {"threadId": thread_id, "status": {"type": "active"}},
            }
        )
        send(
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": active_turn, "status": "inProgress"},
                },
            }
        )
        if approval:
            send(
                {
                    "method": "thread/status/changed",
                    "params": {
                        "threadId": thread_id,
                        "status": {
                            "type": "active",
                            "activeFlags": ["waitingOnUserInput"],
                        },
                    },
                }
            )
            send(
                {
                    "id": 0,
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "threadId": thread_id,
                        "turnId": active_turn,
                        "itemId": "call_fixture_user_input",
                        "questions": [
                            {
                                "id": "wiki_surface",
                                "header": "Surface",
                                "question": (
                                    "Which Wiki surface should show supervisor health?"
                                ),
                                "isOther": True,
                                "isSecret": False,
                                "options": [
                                    {
                                        "label": "Agents page",
                                        "description": (
                                            "Show supervisor health on the Agents page."
                                        ),
                                    },
                                    {
                                        "label": "Session page",
                                        "description": (
                                            "Show supervisor health on the Session page."
                                        ),
                                    },
                                ],
                            }
                        ],
                        "autoResolutionMs": None,
                    },
                }
            )
            approval = False
        elif turn_start_error:
            send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": "fixture turn/start rejection",
                    },
                }
            )
            turn_start_error = False
        elif artifact_tool and "render_artifact" in json.dumps(params):
            from datetime import datetime, timezone
            from uuid import uuid4

            registered = any(
                argument.startswith("mcp_servers.wiki_artifacts.command=")
                for argument in args
            )
            if not registered:
                raise RuntimeError("wiki_artifacts MCP config missing from Codex argv")
            artifact_id = str(uuid4())
            call_id = "call_render_artifact"
            event = {
                "kind": "artifact",
                "id": artifact_id,
                "artifact": {"kind": "mermaid", "source": "graph TD; A-->B"},
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            output = f"<<wiki-artifact:v1>>{json.dumps(event, separators=(',', ':'))}<<end>>"
            tool_item = {
                "type": "mcpToolCall",
                "id": call_id,
                "server": "wiki_artifacts",
                "tool": "render_artifact",
                "arguments": {
                    "kind": "mermaid",
                    "payload": {"source": "graph TD; A-->B"},
                },
                "status": "completed",
                "result": {"content": [{"type": "text", "text": output}]},
            }
            send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": active_turn,
                        "item": tool_item,
                    },
                }
            )
            if open_files:
                open_files[-1].write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "payload": {
                                "type": "function_call",
                                "name": "wiki_artifacts__render_artifact",
                                "call_id": call_id,
                                "arguments": tool_item["arguments"],
                            },
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                open_files[-1].write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "payload": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": output,
                            },
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                open_files[-1].flush()
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": active_turn, "status": "completed"},
                    },
                }
            )
            send(
                {
                    "method": "thread/status/changed",
                    "params": {"threadId": thread_id, "status": {"type": "idle"}},
                }
            )
            active_turn = None
    elif method == "turn/steer":
        send({"id": request_id, "result": {"turnId": active_turn}})
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
        send(
            {
                "method": "thread/status/changed",
                "params": {"threadId": thread_id, "status": {"type": "idle"}},
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": active_turn, "status": "interrupted", "error": None},
                },
            }
        )
        active_turn = None
    elif method == "thread/read":
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": thread_id,
                        "path": str(
                            Path(os.environ["FAKE_CODEX_TRANSCRIPT_DIR"])
                            / f"rollout-{thread_id}.jsonl"
                        ),
                        "status": {"type": "active" if active_turn else "idle"},
                    }
                },
            }
        )
    elif method == "thread/archive":
        send({"id": request_id, "result": {}})
        send({"method": "thread/archived", "params": {"threadId": thread_id}})
        active_turn = None
    elif method is None and request_id == 0:
        answers = value.get("result", {}).get("answers", {})
        if not all(
            isinstance(answer, dict) and isinstance(answer.get("answers"), list)
            for answer in answers.values()
        ):
            raise RuntimeError("invalid requestUserInput response shape")
        if pending_turn_request_id is not None:
            send(
                {
                    "id": pending_turn_request_id,
                    "result": {"turn": {"id": active_turn, "status": "inProgress"}},
                }
            )
            pending_turn_request_id = None
        send(
            {
                "method": "serverRequest/resolved",
                "params": {"threadId": thread_id, "requestId": 0},
            }
        )
        send(
            {
                "method": "thread/status/changed",
                "params": {"threadId": thread_id, "status": {"type": "active"}},
            }
        )

for handle in open_files:
    handle.close()
