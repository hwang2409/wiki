#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "wiki32_isolated_backend.py"
PYTHON = ROOT / ".venv" / "bin" / "python"


def choose_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def write_rows(path: Path, rows: list[dict], mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def codex_user(message: str, timestamp: str) -> dict:
    return {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {"type": "user_message", "message": message},
    }


def codex_assistant(message: str, timestamp: str) -> dict:
    return {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {"type": "agent_message", "message": message},
    }


def codex_tool_call(call_id: str, timestamp: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": timestamp,
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "name": "exec_command",
            "arguments": "echo hi",
        },
    }


def codex_tool_output(call_id: str, timestamp: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": timestamp,
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": "done\nexited with code 0",
        },
    }


def wait_for_health(base_url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {base_url}/health")


def fetch_json(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def build_session(result: dict) -> dict:
    return {
        "path": result["path"],
        "cursor": result["cursor"],
        "base": result["base"],
        "events": result["events"],
    }


def apply_patches(events: list[dict], patches: list[dict]) -> list[dict]:
    by_id = {event["id"]: index for index, event in enumerate(events)}
    next_events = events
    changed = False
    for patch in patches:
        index = by_id.get(patch["id"])
        if index is None:
            continue
        event = next_events[index]
        tool = event.get("tool")
        if not tool:
            continue
        if tool.get("output") == patch.get("output") and tool.get("ok") == patch.get("ok"):
            continue
        if not changed:
            next_events = list(next_events)
            changed = True
        next_events[index] = {
            **event,
            "tool": {
                **tool,
                "output": patch.get("output"),
                "ok": patch.get("ok"),
            },
        }
    return next_events


def merge_session(current: dict | None, result: dict) -> dict:
    if current is None or current["path"] != result["path"] or result["cursor"] < current["cursor"]:
        return build_session(result)

    base = current["base"]
    events = current["events"]
    if result["base"] > base:
        trim = result["base"] - base
        if trim >= len(events):
            return build_session(result)
        events = events[trim:]
        base = result["base"]

    client_end = base + len(events)
    if result["tail_from"] < base or result["tail_from"] > client_end:
        return build_session(result)

    merged = events[: result["tail_from"] - base] + result["events"]
    merged = apply_patches(merged, result["patches"])
    return {
        "path": result["path"],
        "cursor": result["cursor"],
        "base": base,
        "events": merged,
    }


def normalize_events(events: list[dict]) -> list[tuple[int, str, str | None]]:
    normalized = []
    for event in events:
        tool = event.get("tool") or {}
        normalized.append((event["id"], event["text"], tool.get("output")))
    return normalized


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wiki32-live-watch-") as tmp:
        root = Path(tmp)
        status_dir = root / "status"
        sessions_dir = root / "sessions"
        registry = root / "agent-registry.json"
        queue = root / "wiki-msg-queue.json"
        transcript = root / "wiki32-main.jsonl"
        status_dir.mkdir()
        sessions_dir.mkdir()
        registry.write_text(
            json.dumps(
                {
                    "_orchestrators": {
                        "WIKI-32": {
                            "window": "@9999",
                            "spawned_at": "2026-07-09T00:00:00Z",
                            "transcript": str(transcript),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        queue.write_text("{}", encoding="utf-8")
        write_rows(transcript, [codex_user("watch start", "2026-07-09T00:00:00Z")], mode="w")

        port = choose_port()
        base_url = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [
                str(PYTHON),
                str(WRAPPER),
                "--port",
                str(port),
                "--registry",
                str(registry),
                "--status-dir",
                str(status_dir),
                "--queue",
                str(queue),
                "--codex-sessions-dir",
                str(sessions_dir),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def writer() -> None:
            start = time.monotonic()
            for index in range(36):
                target = start + (index + 1) * 5
                while time.monotonic() < target:
                    time.sleep(0.1)
                timestamp = f"2026-07-09T00:{(index + 1) // 12:02d}:{((index + 1) * 5) % 60:02d}Z"
                if index == 10:
                    write_rows(transcript, [codex_tool_call("call-live", timestamp)])
                elif index == 11:
                    write_rows(transcript, [codex_tool_output("call-live", timestamp)])
                else:
                    write_rows(transcript, [codex_assistant(f"live-{index}", timestamp)])

        thread = threading.Thread(target=writer, daemon=True)

        try:
            wait_for_health(base_url)
            thread.start()
            session = None
            cursor = 0
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                result = fetch_json(f"{base_url}/api/agents/WIKI-32/session?cursor={cursor}")
                session = merge_session(session, result)
                cursor = int(result["cursor"])
                time.sleep(2.5)
            thread.join(timeout=10)
            result = fetch_json(f"{base_url}/api/agents/WIKI-32/session?cursor={cursor}")
            session = merge_session(session, result)
            cursor = int(result["cursor"])

            final_full = fetch_json(f"{base_url}/api/agents/WIKI-32/session?cursor=0")
            final_model = build_session(final_full)
            observed = normalize_events(session["events"] if session else [])
            expected = normalize_events(final_model["events"])
            if observed != expected:
                raise SystemExit(
                    json.dumps(
                        {
                            "ok": False,
                            "observed_count": len(observed),
                            "expected_count": len(expected),
                        },
                        indent=2,
                    )
                )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "durationSeconds": 180,
                        "observedCount": len(observed),
                        "expectedCount": len(expected),
                    },
                    indent=2,
                )
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
