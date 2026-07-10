from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def send(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


args = sys.argv[1:]
if "--session-id" in args:
    session_id = args[args.index("--session-id") + 1]
else:
    session_id = args[args.index("--resume") + 1]

log_path = Path(os.environ["FAKE_PROTOCOL_LOG"])
with log_path.open("a", encoding="utf-8") as log:
    log.write(
        json.dumps(
            {
                "argv": args,
                "tmux": os.environ.get("TMUX"),
                "tmux_pane": os.environ.get("TMUX_PANE"),
                "session_id": session_id,
            },
            separators=(",", ":"),
        )
        + "\n"
    )

config_dir = Path(os.environ["CLAUDE_CONFIG_DIR"])
transcript = config_dir / "projects" / "isolated-worktree" / f"{session_id}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
pending_permission = False

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
    event_type = value.get("type")
    if event_type == "control_request":
        request_id = value["request_id"]
        request = value.get("request") or {}
        subtype = request.get("subtype")
        send(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {"pid": os.getpid()} if subtype == "initialize" else {},
                },
            }
        )
        if subtype == "interrupt":
            send(
                {
                    "type": "result",
                    "subtype": "interrupted",
                    "session_id": session_id,
                    "is_error": True,
                    "result": "interrupted",
                }
            )
        elif subtype == "end_session":
            raise SystemExit(0)
    elif event_type == "user":
        content = value.get("message", {}).get("content") or []
        text = " ".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
        send(
            {
                "type": "user",
                "session_id": session_id,
                "uuid": "replay-1",
                "isReplay": True,
                "message": value["message"],
            }
        )
        send(
            {
                "type": "system",
                "subtype": "session_state_changed",
                "session_id": session_id,
                "state": "running",
            }
        )
        if "approval" in text:
            pending_permission = True
            send(
                {
                    "type": "control_request",
                    "request_id": "permission-1",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "Bash",
                        "display_name": "Bash",
                        "input": {"command": "true"},
                        "tool_use_id": "toolu_fixture",
                    },
                }
            )
        else:
            send(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": session_id,
                    "is_error": False,
                    "result": "fixture complete",
                }
            )
    elif event_type == "control_response" and pending_permission:
        pending_permission = False
        send(
            {
                "type": "result",
                "subtype": "success",
                "session_id": session_id,
                "is_error": False,
                "result": "permission handled",
            }
        )
