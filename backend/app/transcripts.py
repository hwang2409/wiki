"""Native CLI session transcripts (codex/claude JSONL) → normalized event stream.

Formats are unversioned internals — parsers are defensive, and each source row
is bucketed as rendered, summarized, intentionally ignored, or unknown.
Normalized event:
  {"kind": "user"|"assistant"|"thinking"|"tool"|"tasks"|"interrupt"|"pr"|
            "marker"|"image"|"question"|"artifact",
   "ts": str|None, "text": str, "disposition": "rendered"|"summarized"|
                                                "intentionally_ignored"|"unknown",
   "tool": {"name", "input", "output", "ok"} (kind=tool only),
   "tasks": [{id, subject, status, blockedBy}] (kind=tasks only),
   "pr":   {number, url}                      (kind=pr only),
   "marker": str                              (kind=marker only),
   "encrypted": bool                          (thinking only),
   "question": {
       "prompt": str, "header": str|None, "options": [str, ...],
       "multi_select": bool, "answered_option": int|None,
       "answered_options": [int, ...], "custom_reply": str|None
   }                                          (question only)}
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from .wiki_artifacts import (
    ArtifactValidationError,
    _validate_text_payload,
    artifact_from_codex_mcp_tool_result,
    artifact_from_text,
    sentinel_text,
)

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
KICKOFF_TICKET_PATTERN = re.compile(r"(?:Linear )?ticket ([A-Z]+-\d+)\b")

MAX_TEXT = 80_000
MAX_TOOL_IO = 3_000
MAX_CHANGE_LOG = 4_096

try:
    TAIL_WINDOW_EVENTS = max(1, int(os.environ.get("WIKI_TRANSCRIPT_TAIL_WINDOW", "500")))
except ValueError:
    TAIL_WINDOW_EVENTS = 500

TRANSCRIPT_IMAGE_DIR = Path("/tmp/wiki-transcript-images")

_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

EVENT_DISPOSITION_RENDERED = "rendered"
EVENT_DISPOSITION_SUMMARIZED = "summarized"
EVENT_DISPOSITION_IGNORED = "intentionally_ignored"
EVENT_DISPOSITION_UNKNOWN = "unknown"


def cache_image(media_type: str, b64_data: str) -> str | None:
    """Persist a transcript-embedded image, return its served filename. Content-hashed — idempotent."""
    import base64
    import hashlib

    ext = _IMAGE_EXT.get(media_type)
    if not ext or len(b64_data) > 15_000_000:
        return None
    name = f"{hashlib.sha1(b64_data[:4096].encode() ) .hexdigest()[:20]}-{len(b64_data)}.{ext}"
    target = TRANSCRIPT_IMAGE_DIR / name
    if not target.exists():
        try:
            TRANSCRIPT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(b64_data))
        except (OSError, ValueError):
            return None
    return name


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} chars truncated]"


def _disposition_counts_key(disposition: str) -> str:
    return "ignored" if disposition == EVENT_DISPOSITION_IGNORED else disposition


def _record_row_disposition(state: dict, disposition: str) -> None:
    counts = state.setdefault(
        "dispositions",
        {"rendered": 0, "summarized": 0, "ignored": 0, "unknown": 0},
    )
    key = _disposition_counts_key(disposition)
    counts[key] = int(counts.get(key, 0)) + 1


def _format_duration_ms(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    total_seconds = max(0, int(round(float(value) / 1000)))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    return f"{seconds}s"


def _marker_event(
    ts: str | None,
    text: str,
    marker: str,
    *,
    disposition: str = EVENT_DISPOSITION_RENDERED,
) -> dict:
    return {
        "kind": "marker",
        "ts": ts,
        "text": text,
        "marker": marker,
        "disposition": disposition,
    }


# ---------------------------------------------------------------- discovery


def _codex_kickoff_ticket(path: Path) -> str | None:
    """Ticket named in the first user_message ("You are a ... worker for Linear ticket X")."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get("payload") or {}
                if row.get("type") == "event_msg" and payload.get("type") == "user_message":
                    match = KICKOFF_TICKET_PATTERN.search(payload.get("message") or "")
                    return match.group(1) if match else None
    except OSError:
        return None
    return None


def detect_session_format(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rtype = row.get("type")
                if rtype in {"event_msg", "response_item", "turn_context"}:
                    return "codex"
                if rtype in {"user", "assistant", "system", "attachment", "pr-link"}:
                    return "claude"
    except OSError:
        return None
    return None


def _codex_session_meta(path: Path) -> dict | None:
    """session_meta payload (first row of every rollout), defensively parsed."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            row = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    payload = row.get("payload") or {}
    return payload if isinstance(payload, dict) else None


def _codex_session_cwd(path: Path) -> str | None:
    meta = _codex_session_meta(path)
    if not meta:
        return None
    cwd = meta.get("cwd")
    return cwd if isinstance(cwd, str) else None


def _codex_session_id(path: Path) -> str | None:
    meta = _codex_session_meta(path)
    if not meta:
        return None
    sid = meta.get("id")
    return sid if isinstance(sid, str) else None


def find_codex_session(
    ticket: str,
    spawned_at: str | None,
    session_id: str | None = None,
    worktree: str | None = None,
) -> Path | None:
    """Newest session whose kickoff prompt names the ticket, started at/after spawn.

    Fallback: match by worktree cwd (dir name embeds the ticket slug). `codex
    resume` writes a NEW rollout whose history is replayed as response_items —
    the kickoff prompt is invisible to the user_message scan — so resumed
    sessions are only findable by cwd. Newest mtime wins either way.
    """
    try:
        spawn = datetime.fromisoformat(spawned_at) if spawned_at else None
    except ValueError:
        spawn = None
    days: list[datetime] = []
    base = spawn or datetime.now(tz=timezone.utc)
    for delta in (0, 1, -1):  # spawn day, plus neighbors for tz/midnight edges
        days.append(base + timedelta(days=delta))
    # Resumes can happen days after spawn — always include today.
    days.append(datetime.now(tz=timezone.utc))
    candidates: list[Path] = []
    seen_days: set[str] = set()
    for day in days:
        key = f"{day.year:04d}/{day.month:02d}/{day.day:02d}"
        if key in seen_days:
            continue
        seen_days.add(key)
        day_dir = CODEX_SESSIONS_DIR / key
        if day_dir.is_dir():
            candidates.extend(day_dir.glob("rollout-*.jsonl"))
    # Registry-tracked id beats discovery. Locate the anchor rollout by id;
    # then prefer any newer rollout in the same cwd (`codex resume` writes a
    # NEW rollout with a NEW id, so a chain of resumes appears as a sequence
    # of rollouts in one cwd — the app should render the newest of that chain).
    if session_id:
        anchor: Path | None = None
        for p in candidates:
            if _codex_session_id(p) == session_id:
                anchor = p
                break
        if anchor is not None:
            # `codex resume <id>` REUSES the anchor rollout file (verified live
            # 2026-07-08 via open file handles) — the exact-id file IS the live
            # session. Chaining to "newer rollout, same cwd" here cross-bled
            # tickets whose sessions share a cwd (workers spawned at repo
            # root): 13227's view streamed 13251's session. Exact match wins;
            # the same-cwd chain remains ONLY as the fallback below for
            # anchors that vanished (old resume---last lineages).
            try:
                anchor.stat()
                return anchor
            except OSError:
                pass
            anchor_cwd = _codex_session_cwd(anchor)
            ranked: list[tuple[float, Path]] = []
            if anchor_cwd:
                for p in candidates:
                    if p == anchor or _codex_session_cwd(p) != anchor_cwd:
                        continue
                    try:
                        ranked.append((p.stat().st_mtime, p))
                    except OSError:
                        continue
            if ranked:
                return max(ranked, key=lambda pair: pair[0])[1]
            return None

    slug = ticket.lower()
    # worktree = exact registry-recorded path; needed when the worktree dir
    # name does not embed the ticket slug (e.g. PR-keyed workers running in
    # branch-named worktrees) AND the kickoff prompt lacks "Linear ticket X".
    matches = [
        p
        for p in candidates
        if _codex_kickoff_ticket(p) == ticket
        or PurePosixPath(_codex_session_cwd(p) or "").name == slug
        or (worktree and _codex_session_cwd(p) == worktree)
    ]
    if not matches:
        return None
    if spawn:
        # Prefer sessions started after this spawn (plan→implement chains reuse the ticket).
        after = [p for p in matches if p.stat().st_mtime >= spawn.timestamp() - 300]
        if after:
            matches = after
    return max(matches, key=lambda p: p.stat().st_mtime)


def _claude_kickoff_ticket(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "user":
                    continue
                content = (row.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                if isinstance(content, str):
                    match = KICKOFF_TICKET_PATTERN.search(content)
                    return match.group(1) if match else None
    except OSError:
        return None
    return None


def find_claude_session(ticket: str, spawned_at: str | None, session_id: str | None = None) -> Path | None:
    """Worktree project dirs embed the ticket slug; kickoff prompt disambiguates the rest."""
    if session_id:
        matches = list(CLAUDE_PROJECTS_DIR.glob(f"**/{session_id}.jsonl"))
        if matches:
            return max(matches, key=lambda p: p.stat().st_mtime)
    slug = ticket.lower()
    dirs = [d for d in CLAUDE_PROJECTS_DIR.glob(f"*{slug}*") if d.is_dir()]
    candidates: list[Path] = []
    for d in dirs:
        candidates.extend(d.glob("*.jsonl"))
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        spawn = datetime.fromisoformat(spawned_at) if spawned_at else None
    except ValueError:
        spawn = None
    cutoff = spawn.timestamp() - 300 if spawn else 0
    recent = [
        p
        for d in CLAUDE_PROJECTS_DIR.glob("*")
        if d.is_dir()
        for p in d.glob("*.jsonl")
        if p.stat().st_mtime >= cutoff
    ]
    matches = [p for p in recent if _claude_kickoff_ticket(p) == ticket]
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def find_session(
    kind: str | None,
    ticket: str,
    spawned_at: str | None,
    session_id: str | None = None,
    worktree: str | None = None,
) -> tuple[str, Path] | None:
    if kind == "cc":
        path = find_claude_session(ticket, spawned_at, session_id)
        return ("claude", path) if path else None
    if kind == "cdx":
        path = find_codex_session(ticket, spawned_at, session_id, worktree)
        return ("codex", path) if path else None
    path = find_codex_session(ticket, spawned_at, session_id, worktree)
    if path:
        return ("codex", path)
    path = find_claude_session(ticket, spawned_at, session_id)
    if path:
        return ("claude", path)
    return None


# ---------------------------------------------------------------- archetype classifier

_READ_HEADS = {"sed", "cat", "nl", "head", "tail", "less", "bat", "od"}
_SEARCH_HEADS = {"rg", "grep", "ugrep", "find", "fd", "ag"}
_VALIDATE_HEADS = {"bazel", "pnpm", "npm", "yarn", "ruff", "ty", "pytest", "cargo", "tsc", "mypy", "eslint", "vitest", "jest", "go"}
_INFRA_HEADS = {"docker", "psql", "curl", "tmux", "kubectl", "terraform", "aws", "gcloud"}
_WAIT_HEADS = {"sleep", "ps", "wait"}
_TICKET_TOOLS = {
    "get_issue", "save_issue", "list_issues", "list_comments", "save_comment",
    "get_project", "list_issue_statuses", "create_comment",
}
_FILE_LINE_PATTERN = re.compile(r"sed -n '?(\d+),(\d+)p'?\s+(\S+)")
_QUOTED_PATTERN = re.compile(r"""["']([^"']{1,60})["']""")
_PATCH_FILE_PATTERN = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (\S+)")


def _strip_wrappers(cmd: str) -> str:
    cmd = cmd.strip()
    while True:
        match = re.match(r"^cd\s+\S+\s*&&\s*", cmd)
        if not match:
            return cmd
        cmd = cmd[match.end():]


def classify_tool(name: str, tool_input: str) -> tuple[str, str]:
    archetype, summary = _classify_tool(name, tool_input)
    return archetype, " ".join(summary.split())[:100]


def _classify_tool(name: str, tool_input: str) -> tuple[str, str]:
    """(archetype, one-line human summary) — best-effort, falls back to raw."""
    mcp = re.match(r"mcp__[\w-]+__(\w+)$", name)
    if mcp:
        name = mcp.group(1)
    if name == "apply_patch":
        files = _PATCH_FILE_PATTERN.findall(tool_input)
        short = ", ".join(PurePosixPath(f).name for f in files[:4])
        if len(files) > 4:
            short += f" +{len(files) - 4}"
        return ("edit", f"edit {short}" if short else "apply patch")
    if name == "write_stdin":
        return ("wait", "waiting on terminal" if '"chars": ""' in tool_input or not tool_input else "input → terminal")
    if name in _TICKET_TOOLS:
        ticket = re.search(r"[A-Z]{2,}-\d+", tool_input)
        return ("ticket", f"{name}{' ' + ticket.group(0) if ticket else ''}")
    if name == "update_plan":
        return ("plan", "updated plan")
    if name == "view_image":
        return ("read", f"view image {PurePosixPath(tool_input.strip()).name}" if tool_input else "view image")
    if name in ("web_search", "web_search_call", "WebSearch", "WebFetch"):
        return ("search", f"web search: {tool_input[:60]}")
    # Claude-native tools
    if name in ("Read", "NotebookRead"):
        return ("read", f"read {PurePosixPath(tool_input.strip()).name}" if tool_input else "read")
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return ("edit", f"edit {PurePosixPath(tool_input.strip()).name}" if tool_input else "edit")
    if name in ("Grep", "Glob"):
        return ("search", f"{name.lower()} {_clip(tool_input, 60)}")
    if name == "Monitor":
        return ("monitor", f"monitor: {_clip(tool_input, 70)}")
    if name == "TaskStop":
        task = re.search(r'"task_id":\s*"([^"]+)"', tool_input)
        return ("monitor", f"stop monitor {task.group(1)}" if task else "stop monitor")
    if name == "Agent":
        return ("agent", f"agent: {_clip(tool_input, 70)}")
    if name == "AskUserQuestion":
        question = re.search(r'"question":\s*"([^"]{1,80})', tool_input)
        return ("ask", f"ask: {question.group(1)}" if question else "ask henry")
    if name == "Skill":
        skill = re.search(r'"skill":\s*"([^"]+)"', tool_input)
        return ("agent", f"skill {skill.group(1)}" if skill else f"skill {_clip(tool_input, 50)}")

    if name not in ("exec_command", "shell", "exec", "local_shell", "Bash"):
        return ("tool", f"{name} {tool_input[:60]}".strip())

    cmd = _strip_wrappers(tool_input)
    head = cmd.split(None, 1)[0].rsplit("/", 1)[-1] if cmd else ""
    rest = cmd.split(None, 1)[1] if " " in cmd else ""

    if "send-keys" in cmd:
        target = re.search(r"-t\s+(\S+)", cmd)
        # Steer text often staged in a var: STEER="..."; tmux send-keys ... "$STEER"
        payload = re.search(r"""\bSTEER=["']([^"']{1,80})""", cmd) or re.search(
            r"""send-keys[^"']*["'](?!\$)([^"']{1,80})""", cmd
        )
        label = f"steer {target.group(1)}" if target else "steer"
        return ("steer", f"{label}: {payload.group(1)}" if payload else label)

    if "/tmp/agent-status/" in cmd and '"state' in cmd.replace("\\", ""):
        state = re.search(r'\\?"state\\?":\s*\\?"(\w[\w-]*)', cmd)
        return ("status", f"status → {state.group(1)}" if state else "status write")

    if head in _READ_HEADS:
        match = _FILE_LINE_PATTERN.search(cmd)
        if match:
            start, end, path = match.groups()
            return ("read", f"read {PurePosixPath(path).name}:{start}-{end}")
        paths = [t for t in cmd.split() if "/" in t or "." in t]
        return ("read", f"read {PurePosixPath(paths[-1]).name}" if paths else _clip(cmd, 80))
    if head in _SEARCH_HEADS:
        quoted = _QUOTED_PATTERN.search(rest)
        return ("search", f"{head} {quoted.group(1)!r}" if quoted else _clip(cmd, 80))
    if head == "git":
        sub = rest.split(None, 1)[0] if rest else ""
        if sub == "-C":
            parts = rest.split(None, 3)
            sub = parts[2] if len(parts) > 2 else ""
        msg = re.search(r"-m\s+[\"']([^\"']{1,60})", cmd)
        return ("git", f"git {sub}" + (f": {msg.group(1)}" if msg else ""))
    if head == "gh":
        return ("github", _clip(re.sub(r"\s+--json\b.*", "", cmd), 80))
    if head in _VALIDATE_HEADS or "pytest" in cmd.split()[0:2]:
        return ("validate", _clip(cmd, 80))
    if head in _WAIT_HEADS:
        return ("wait", _clip(cmd, 60))
    if head in _INFRA_HEADS:
        return ("infra", _clip(cmd, 80))
    if head in ("python", "python3", "node") and ("-c" in cmd or "<<" in cmd):
        return ("run", f"{head} inline script")
    return ("run", _clip(cmd, 80))


# ---------------------------------------------------------------- disposition policy


_CLAUDE_IGNORED_TYPES = {
    "ai-title": "Claude auto-titles duplicate visible transcript context.",
    "file-history-snapshot": "Backup manifests are large implementation metadata, not session content.",
    "last-prompt": "Truncated prompt storage is bookkeeping, not a transcript event.",
    "mode": "Repeated mode rows are ambient metadata without user-visible state.",
    "queue-operation": "Queue bookkeeping is internal plumbing noise.",
    "result": "Completion sentinels carry no additional session detail.",
    "started": "Bootstrap sentinels only record session start.",
}

_CODEX_IGNORED_TYPES = {
    "compacted": "Compaction bookkeeping is redundant with the rendered compacted marker.",
    "inter_agent_communication_metadata": "Cross-thread transport metadata is internal-only plumbing.",
    "session_meta": "Session headers duplicate stable metadata already shown elsewhere.",
    "thread_settings_applied": "Thread settings are verbose startup metadata with no incremental transcript value.",
    "turn_context": "Turn context rows are parser bookkeeping, not user-visible activity.",
    "world_state": "World-state snapshots are large internal state dumps.",
}


# ---------------------------------------------------------------- codex parser


def _codex_tool_input(name: str, arguments: object) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)  # arguments is a JSON-string — double-parse
        except ValueError:
            return _clip(arguments, MAX_TOOL_IO)
    if isinstance(arguments, dict):
        for key in ("cmd", "command", "input", "query", "pattern", "path", "file_path", "description"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return _clip(value, MAX_TOOL_IO)
            if isinstance(value, list):
                return _clip(" ".join(str(v) for v in value), MAX_TOOL_IO)
        return _clip(json.dumps(arguments), MAX_TOOL_IO)
    return _clip(str(arguments), MAX_TOOL_IO)


def _is_artifact_tool(name: object) -> bool:
    return isinstance(name, str) and (
        name == "render_artifact" or name.endswith("__render_artifact")
    )


def _tool_arguments(arguments: object) -> dict | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    return dict(arguments) if isinstance(arguments, dict) else None


def _artifact_event(protocol_event: dict, ts: str | None) -> dict:
    artifact = protocol_event.get("artifact") or {}
    title = protocol_event.get("title")
    kind = artifact.get("kind") or "artifact"
    return {
        "kind": "artifact",
        "ts": protocol_event.get("ts") or ts,
        "text": title or kind,
        "artifact_id": protocol_event.get("id"),
        "title": title,
        "caption": protocol_event.get("caption"),
        "artifact": artifact,
    }


def _append_artifact_event(
    state: dict,
    protocol_event: dict,
    ts: str | None,
) -> bool:
    artifact_id = protocol_event.get("id")
    artifact_ids: set[str] = state.setdefault("artifact_ids", set())
    if artifact_id in artifact_ids:
        return False
    artifact_ids.add(artifact_id)
    _append_event(state, _artifact_event(protocol_event, ts))
    return True


def _artifact_tool_status(
    meta: dict,
    output: str,
    ts: str | None,
    *,
    ok: bool,
    summary: str,
) -> dict:
    raw_input = meta.get("input") or {}
    return {
        "kind": "tool",
        "ts": ts,
        "text": "",
        "tool": {
            "name": meta.get("name") or "render_artifact",
            "input": _clip(json.dumps(raw_input), MAX_TOOL_IO),
            "output": _clip(output, MAX_TOOL_IO),
            "ok": ok,
            "archetype": "tool",
            "summary": summary,
        },
    }


def _failed_artifact_tool(meta: dict, output: str, ts: str | None) -> dict:
    return _artifact_tool_status(
        meta,
        output,
        ts,
        ok=False,
        summary="render_artifact rejected",
    )


def _unparseable_artifact_tool(meta: dict, output: str, ts: str | None) -> dict:
    return _artifact_tool_status(
        meta,
        output,
        ts,
        ok=True,
        summary="render_artifact completed without a parseable artifact",
    )


def _codex_mcp_tool_result_text(item: dict) -> str:
    result = item.get("result")
    if not isinstance(result, dict):
        return str(item.get("error") or result or "")
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
    return str(item.get("error") or result.get("error") or json.dumps(result))


def _is_codex_render_artifact_call(item: object) -> bool:
    return (
        isinstance(item, dict)
        and item.get("type") == "mcpToolCall"
        and item.get("server") == "wiki_artifacts"
        and item.get("tool") == "render_artifact"
    )


def _artifact_from_structured_result(meta: dict, output: str) -> dict | None:
    try:
        result = json.loads(output)
    except ValueError:
        return None
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    artifact_id = result.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return None
    raw_input = meta.get("input")
    if not isinstance(raw_input, dict):
        return None
    kind = raw_input.get("kind")
    payload = raw_input.get("payload")
    if not isinstance(kind, str) or not isinstance(payload, dict):
        return None
    if kind == "image":
        artifact = {
            "kind": "image",
            "ref": f"artifact://{artifact_id}",
            "mime": payload.get("mime"),
        }
    else:
        try:
            validated_payload = _validate_text_payload(kind, payload)
        except ArtifactValidationError:
            return None
        artifact = {**validated_payload, "kind": kind}
    protocol_event = {"kind": "artifact", "id": artifact_id, "artifact": artifact}
    for field, limit in (("title", 200), ("caption", 500)):
        if field in raw_input:
            value = raw_input[field]
            if not isinstance(value, str) or len(value) > limit:
                return None
            protocol_event[field] = value
    return artifact_from_text(sentinel_text(protocol_event))


def _structured_artifact_result_failed(output: str) -> bool:
    try:
        result = json.loads(output)
    except ValueError:
        return False
    return isinstance(result, dict) and result.get("ok") is False


def _complete_artifact(
    state: dict,
    call_id: object,
    output: str,
    ts: str | None,
    *,
    failed: bool = False,
) -> bool:
    meta = state.get("pending_artifacts", {}).pop(call_id, None)
    if meta is None:
        return False
    failed = failed or _structured_artifact_result_failed(output)
    protocol_event = None if failed else artifact_from_text(output)
    if protocol_event is None and not failed:
        protocol_event = _artifact_from_structured_result(meta, output)
    if protocol_event is not None:
        _append_artifact_event(state, protocol_event, ts)
    else:
        _append_event(
            state,
            _failed_artifact_tool(meta, output, ts)
            if failed
            else _unparseable_artifact_tool(meta, output, ts),
        )
    return True


def _codex_tool_end_output(ptype: str, payload: dict) -> tuple[str, bool | None]:
    """patch_apply_end / mcp_tool_call_end carry the authoritative tool output
    the corresponding function_call/custom_tool_call left `output: null`."""
    if ptype == "patch_apply_end":
        parts = [payload.get("stdout") or "", payload.get("stderr") or ""]
        return "\n".join(p for p in parts if p), bool(payload.get("success"))
    if ptype == "mcp_tool_call_end":
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            return str(result), None
        ok_side, err_side = result.get("Ok"), result.get("Err")
        target = ok_side if ok_side is not None else err_side
        ok = ok_side is not None and err_side is None
        if isinstance(target, dict):
            content = target.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                return text or json.dumps(target), ok
            return json.dumps(target), ok
        return str(target if target is not None else result), ok
    return json.dumps(payload), None


def _dedupe_pair(state: dict, source: str, role: str, text: str) -> bool:
    """Pair-consumption dedupe. Codex live sessions emit the SAME turn as
    both `event_msg` user_message/agent_message AND `response_item`/message.
    Naive set-based dedup drops legitimate repeats ("Continue" x89 in one
    audited rollout).

    Model: each rendered message grants one suppress-credit for its twin
    (opposite source). The next twin arrival consumes the credit and is
    skipped. Bidirectional — either source can arrive first (audit measured
    41 event_msg-first vs 2 response_item-first orderings). No credit ⇒
    render normally (a true repeat from the same source, or an unpaired one).
    """
    key = (source, role, text.strip()[:400])
    credits: dict = state.setdefault("dedupe_credits", {})
    if credits.get(key, 0) > 0:
        credits[key] -= 1
        if credits[key] == 0:
            del credits[key]
        return True
    twin_source = "response_item" if source == "event_msg" else "event_msg"
    twin_key = (twin_source, role, text.strip()[:400])
    credits[twin_key] = credits.get(twin_key, 0) + 1
    if len(credits) > 2000:
        for old in list(credits.keys())[:1000]:
            del credits[old]
    return False


def _record_change(state: dict, change: dict) -> None:
    cursor = int(state.get("cursor", 0)) + 1
    state["cursor"] = cursor
    entry = {"cursor": cursor, **change}
    changes: list[dict] = state.setdefault("changes", [])
    changes.append(entry)
    if len(changes) > MAX_CHANGE_LOG:
        del changes[: len(changes) - MAX_CHANGE_LOG]


def _append_event(state: dict, event: dict) -> dict:
    event.setdefault("disposition", EVENT_DISPOSITION_RENDERED)
    event_id = int(state.get("next_event_id", state.get("base", 0) + len(state.get("events", []))))
    event["id"] = event_id
    state["next_event_id"] = event_id + 1
    state["events"].append(event)
    _record_change(state, {"kind": "tail", "from": event_id})
    return event


def _replace_event_at(state: dict, index: int, event: dict) -> dict:
    current = state["events"][index]
    event["id"] = current["id"]
    state["events"][index] = event
    _record_change(state, {"kind": "tail", "from": int(event["id"])})
    return event


def _mark_tail_changed(state: dict, index: int) -> None:
    event = state["events"][index]
    _record_change(state, {"kind": "tail", "from": int(event["id"])})


def _record_tool_patch(state: dict, event: dict) -> None:
    tool = event.get("tool") or {}
    _record_change(
        state,
        {
            "kind": "patch",
            "id": int(event["id"]),
            "index": int(event["id"]),
            "output": tool.get("output"),
            "ok": tool.get("ok"),
        },
    )


def _codex_background_event(payload: dict, ts: str | None) -> dict | None:
    ptype = payload.get("type")
    if ptype == "task_started":
        mode = payload.get("collaboration_mode_kind")
        text = "task started"
        if isinstance(mode, str) and mode:
            text += f" · {mode}"
        return _marker_event(ts, text, "task_started")
    if ptype == "task_complete":
        text = "task complete"
        duration = _format_duration_ms(payload.get("duration_ms"))
        if duration:
            text += f" · {duration}"
        last = payload.get("last_agent_message")
        if isinstance(last, str) and last.strip():
            first = last.strip().splitlines()[0]
            text += f": {_clip(first, 220)}"
        return _marker_event(ts, _clip(text, 400), "task_complete")
    if ptype == "sub_agent_activity":
        kind = payload.get("kind") or "activity"
        path = PurePosixPath(payload.get("agent_path") or "").name
        label = f"subagent {kind}"
        if path:
            label += f" · {path}"
        return _marker_event(ts, label, "subagent")
    return None


def _codex_apply(state: dict, row: dict) -> None:
    pending: dict = state["pending"]  # call_id → event (awaiting output)
    ts = row.get("timestamp")
    rtype = row.get("type")
    payload = row.get("payload") or {}
    ptype = payload.get("type")

    if rtype in _CODEX_IGNORED_TYPES:
        _record_row_disposition(state, EVENT_DISPOSITION_IGNORED)
        return

    if rtype == "event_msg":
        if ptype == "thread_settings_applied":
            _record_row_disposition(state, EVENT_DISPOSITION_IGNORED)
            return
        if ptype == "user_message":
            text = _clip(payload.get("message") or "", MAX_TEXT)
            if text and not _dedupe_pair(state, "event_msg", "user", text):
                _append_event(state, {"kind": "user", "ts": ts, "text": text})
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype == "agent_message":
            text = _clip(payload.get("message") or "", MAX_TEXT)
            if text and not _dedupe_pair(state, "event_msg", "assistant", text):
                _append_event(state, {"kind": "assistant", "ts": ts, "text": text})
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype == "token_count":
            info = payload.get("info") or {}
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            if total:
                state["tokens"] = total
            _record_row_disposition(state, EVENT_DISPOSITION_SUMMARIZED)
        elif ptype == "context_compacted":
            _append_event(state, {"kind": "thinking", "ts": ts, "text": "context compacted"})
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype == "turn_aborted":
            reason = payload.get("reason") or "aborted"
            duration_ms = payload.get("duration_ms")
            text = f"turn aborted ({reason})"
            if isinstance(duration_ms, (int, float)) and duration_ms:
                text += f" · {int(duration_ms // 1000)}s"
            _append_event(state, {"kind": "interrupt", "ts": ts, "text": text})
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype in ("task_started", "task_complete", "sub_agent_activity"):
            marker = _codex_background_event(payload, ts)
            if marker:
                _append_event(state, marker)
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype in ("patch_apply_end", "mcp_tool_call_end", "web_search_end"):
            call_id = payload.get("call_id")
            out, ok = _codex_tool_end_output(ptype, payload)
            if _complete_artifact(state, call_id, str(out), ts, failed=ok is False):
                _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
                return
            event = pending.pop(call_id, None)
            if event:
                event["tool"]["output"] = _clip(str(out), MAX_TOOL_IO)
                event["tool"]["ok"] = ok
                _record_tool_patch(state, event)
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        else:
            _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
    elif rtype == "response_item":
        if ptype == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                _record_row_disposition(state, EVENT_DISPOSITION_IGNORED)
                return  # developer role = injected instructions, skip
            parts: list[str] = []
            for block in payload.get("content") or []:
                if isinstance(block, dict):
                    text_field = block.get("text")
                    if isinstance(text_field, str) and text_field:
                        parts.append(text_field)
            text = "\n".join(parts).strip()
            if text and not _dedupe_pair(state, "response_item", role, text):
                _append_event(state, {"kind": role, "ts": ts, "text": _clip(text, MAX_TEXT)})
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype == "reasoning":
            summary = payload.get("summary") or []
            text = " ".join(
                s.get("text", "") for s in summary if isinstance(s, dict)
            ).strip()
            _append_event(
                state,
                {
                    "kind": "thinking",
                    "ts": ts,
                    "text": _clip(text, MAX_TEXT),
                    "encrypted": isinstance(payload.get("encrypted_content"), str),
                },
            )
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype in ("function_call", "custom_tool_call", "web_search_call", "tool_search_call"):
            name = payload.get("name") or ptype.replace("_call", "")
            raw_input = payload.get("arguments", payload.get("input", payload.get("action", "")))
            call_id = payload.get("call_id")
            if _is_artifact_tool(name) and call_id:
                state.setdefault("pending_artifacts", {})[call_id] = {
                    "name": name,
                    "input": _tool_arguments(raw_input) or {},
                }
                _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
                return
            tool_input = _codex_tool_input(name, raw_input)
            archetype, summary = classify_tool(name, tool_input)
            event = {
                "kind": "tool",
                "ts": ts,
                "text": "",
                "tool": {
                    "name": name,
                    "input": tool_input,
                    "output": None,
                    "ok": None,
                    "archetype": archetype,
                    "summary": summary,
                },
            }
            _append_event(state, event)
            if call_id:
                pending[call_id] = event
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
            call_id = payload.get("call_id")
            output = payload.get("output")
            if isinstance(output, dict):
                output = output.get("content") or json.dumps(output)
            output_text = str(output or "")
            if _complete_artifact(state, call_id, output_text, ts):
                _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
                return
            event = pending.pop(call_id, None)
            if event:
                event["tool"]["output"] = _clip(output_text, MAX_TOOL_IO)
                event["tool"]["ok"] = "exited with code 0" in output_text or None
                _record_tool_patch(state, event)
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        else:
            _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
    else:
        _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)


_SYNTHETIC_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _validated_normalized_source(payload: object) -> str | None:
    """Read `source` off a normalized-event payload with the same validation
    used at ingest (matches supervisor._validated_source). Archived events
    are historical: reject rather than persist unrecognized shapes."""

    if not isinstance(payload, dict):
        return None
    value = payload.get("source")
    if not isinstance(value, str) or not _SYNTHETIC_SOURCE_PATTERN.match(value):
        return None
    return value


_PENDING_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _validated_normalized_pending_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("pending_id")
    if not isinstance(value, str) or not _PENDING_ID_PATTERN.match(value):
        return None
    return value


def _stamp_last_user_event_source(
    state: dict,
    source: str | None,
    pending_id: str | None = None,
) -> None:
    """Attach a validated synthetic source (and durable pending_id, when
    present) to the most recent user event so archived-session parsers
    preserve the marker-row rendering that the live composer_messages
    surface produces."""

    if not source and not pending_id:
        return
    events = state.get("events") or []
    for event in reversed(events):
        if event.get("kind") == "user":
            if source:
                event["source"] = source
            if pending_id:
                event["pending_id"] = pending_id
            return


def _normalized_disposition(row: dict) -> str:
    disposition = row.get("disposition")
    if disposition == "ignored":
        return EVENT_DISPOSITION_IGNORED
    if disposition in {
        EVENT_DISPOSITION_RENDERED,
        EVENT_DISPOSITION_SUMMARIZED,
        EVENT_DISPOSITION_UNKNOWN,
    }:
        return disposition
    return EVENT_DISPOSITION_UNKNOWN


def _apply_normalized_payload(
    state: dict,
    row: dict,
    apply: Callable[[dict, dict], None],
    payload_row: dict | None,
) -> None:
    """Apply a provider payload while keeping archive normalization counts.

    Native transcript parsers also classify their input rows. Archived
    events.jsonl already contains the authoritative classification, so keep
    parser side effects (events, patches, tokens) but count each envelope once.
    """
    counts = dict(state.get("dispositions") or {})
    if payload_row is not None:
        apply(state, payload_row)
    state["dispositions"] = counts
    _record_row_disposition(state, _normalized_disposition(row))


def _codex_normalized_apply(state: dict, row: dict) -> None:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _apply_normalized_payload(state, row, _codex_apply, None)
        return
    if row.get("kind") == "artifact" and payload.get("kind") == "artifact":
        _append_artifact_event(state, payload, row.get("normalized_at"))
        _record_row_disposition(state, _normalized_disposition(row))
        return
    method = payload.get("method")
    params = payload.get("params")
    params = params if isinstance(params, dict) else {}
    ts = row.get("normalized_at")
    native_row: dict | None = None

    if method == "rawResponseItem/completed":
        item = params.get("item")
        if isinstance(item, dict):
            native_row = {"type": "response_item", "timestamp": ts, "payload": item}
    elif method == "item/completed":
        # App-server also emits rawResponseItem/completed for rich content.
        # These two message forms are retained as a defensive fallback and the
        # native parser's pair-credit dedupe removes the app-server twin.
        item = params.get("item")
        artifact = artifact_from_codex_mcp_tool_result(item)
        if artifact is not None:
            _append_artifact_event(state, artifact, ts)
            _record_row_disposition(state, _normalized_disposition(row))
            return
        if _is_codex_render_artifact_call(item):
            completed = (
                item.get("status") == "completed"
                and item.get("error") is None
                and not (
                    isinstance(item.get("result"), dict)
                    and item["result"].get("isError") is True
                )
            )
            _append_event(
                state,
                _artifact_tool_status(
                    {
                        "name": "render_artifact",
                        "input": _tool_arguments(item.get("arguments")) or {},
                    },
                    _codex_mcp_tool_result_text(item),
                    ts,
                    ok=completed,
                    summary=(
                        "render_artifact completed without a parseable artifact"
                        if completed
                        else "render_artifact rejected"
                    ),
                ),
            )
            _record_row_disposition(state, _normalized_disposition(row))
            return
        if isinstance(item, dict) and item.get("type") == "userMessage":
            text = "\n".join(
                str(block.get("text"))
                for block in item.get("content") or []
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ).strip()
            native_row = {
                "type": "event_msg",
                "timestamp": ts,
                "payload": {"type": "user_message", "message": text},
            }
        elif isinstance(item, dict) and item.get("type") == "agentMessage":
            native_row = {
                "type": "event_msg",
                "timestamp": ts,
                "payload": {
                    "type": "agent_message",
                    "message": str(item.get("text") or ""),
                },
            }
    elif method == "thread/tokenUsage/updated":
        usage = params.get("tokenUsage")
        if isinstance(usage, dict):
            native_row = {
                "type": "event_msg",
                "timestamp": ts,
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": usage.get("total") or {}},
                },
            }
    elif method == "context/compacted":
        native_row = {
            "type": "event_msg",
            "timestamp": ts,
            "payload": {"type": "context_compacted"},
        }
    elif method == "turn/completed":
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("status") == "interrupted":
            native_row = {
                "type": "event_msg",
                "timestamp": ts,
                "payload": {
                    "type": "turn_aborted",
                    "reason": "interrupted",
                    "duration_ms": turn.get("durationMs"),
                },
            }

    _apply_normalized_payload(state, row, _codex_apply, native_row)
    _stamp_last_user_event_source(
        state,
        _validated_normalized_source(payload),
        _validated_normalized_pending_id(payload),
    )


def _claude_normalized_apply(state: dict, row: dict) -> None:
    payload = row.get("payload")
    native_row = dict(payload) if isinstance(payload, dict) else None
    if native_row is not None and not native_row.get("timestamp"):
        native_row["timestamp"] = row.get("normalized_at")
    _apply_normalized_payload(state, row, _claude_apply, native_row)
    _stamp_last_user_event_source(
        state,
        _validated_normalized_source(payload),
        _validated_normalized_pending_id(payload),
    )


# ---------------------------------------------------------------- claude parser


def _claude_progress_text(row: dict) -> str:
    data = row.get("data")
    if isinstance(data, dict):
        nested = data.get("message")
        if isinstance(nested, dict):
            content = nested.get("content")
            if isinstance(content, str) and content.strip():
                return _clip(content.strip(), MAX_TEXT)
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                if parts:
                    return _clip("\n".join(parts), MAX_TEXT)
        prompt = data.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return _clip(prompt.strip(), MAX_TEXT)
        kind = data.get("type")
        if isinstance(kind, str) and kind:
            return f"progress · {kind}"
    return "progress"


def _claude_system_text(row: dict) -> tuple[str, str] | None:
    subtype = row.get("subtype")
    if subtype == "api_error":
        error_raw = row.get("error")
        error = error_raw if isinstance(error_raw, dict) else {}
        detail = row.get("content") or error.get("formatted") or error.get("message")
        retry = _format_duration_ms(row.get("retryInMs"))
        text = f"API error: {detail}" if detail else "API error"
        if retry:
            text += f" · retry in {retry}"
        return ("api_error", _clip(str(text), 400))
    if subtype == "compact_boundary":
        return ("compact_boundary", _clip(str(row.get("content") or "conversation compacted"), 400))
    if subtype == "scheduled_task_fire":
        return ("scheduled_task_fire", _clip(str(row.get("content") or "scheduled task fired"), 400))
    if subtype == "stop_hook_summary":
        parts = [f"stop hook · {int(row.get('hookCount') or 0)} hook{'s' if int(row.get('hookCount') or 0) != 1 else ''}"]
        level = row.get("level")
        if isinstance(level, str) and level:
            parts.append(level)
        if row.get("preventedContinuation"):
            parts.append("blocked continuation")
        return ("stop_hook_summary", " · ".join(parts))
    if subtype == "turn_duration":
        duration = _format_duration_ms(row.get("durationMs")) or "0s"
        count = row.get("messageCount")
        text = f"turn duration · {duration}"
        if isinstance(count, int):
            text += f" · {count} messages"
        return ("turn_duration", text)
    if subtype == "informational":
        return ("informational", _clip(str(row.get("content") or "informational"), 400))
    if subtype == "local_command":
        name = _xml_tag(str(row.get("content") or ""), "command-name") or "local command"
        args = _xml_tag(str(row.get("content") or ""), "command-args") or ""
        return ("local_command", f"{name} {args}".strip())
    if subtype == "away_summary":
        return ("away_summary", _clip(str(row.get("content") or "away summary"), 400))
    return None


def _claude_init_data(row: dict) -> dict:
    return {
        "claude_code_version": row.get("claude_code_version"),
        "model": row.get("model"),
        "output_style": row.get("output_style"),
        "cwd": row.get("cwd"),
        "mcp_servers": row.get("mcp_servers") if isinstance(row.get("mcp_servers"), list) else [],
        "agents": row.get("agents") if isinstance(row.get("agents"), list) else [],
        "memory_paths": row.get("memory_paths") if isinstance(row.get("memory_paths"), list) else [],
        "fast_mode_state": row.get("fast_mode_state"),
    }


def _claude_task_data(row: dict) -> dict:
    return {
        "status": row.get("status"),
        "summary": row.get("summary"),
        "output_file": row.get("output_file"),
        "task_id": row.get("task_id"),
        "tool_use_id": row.get("tool_use_id"),
    }


def _claude_retry_data(row: dict) -> dict:
    return {
        "attempt": row.get("attempt"),
        "max_retries": row.get("max_retries"),
        "error": row.get("error"),
        "error_status": row.get("error_status"),
        "retry_delay_ms": row.get("retry_delay_ms"),
    }


def _claude_rate_limit_data(row: dict) -> dict:
    info = row.get("rate_limit_info")
    info = info if isinstance(info, dict) else {}
    return {
        "status": info.get("status", row.get("status")),
        "rateLimitType": info.get("rateLimitType", row.get("rateLimitType")),
        "isUsingOverage": info.get("isUsingOverage", row.get("isUsingOverage")),
        "overageStatus": info.get("overageStatus", row.get("overageStatus")),
        "overageDisabledReason": info.get("overageDisabledReason", row.get("overageDisabledReason")),
        "resetsAt": info.get("resetsAt", row.get("resetsAt")),
    }


def _claude_task_patch(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, list):
        return {}
    result: dict = {}
    for operation in value:
        if not isinstance(operation, dict):
            continue
        path = operation.get("path")
        if not isinstance(path, str):
            continue
        key = path.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
        if key and operation.get("op") in {"add", "replace"}:
            result[key] = operation.get("value")
    return result


def _claude_permission_text(row: dict) -> str:
    mode = row.get("permissionMode") or "unknown"
    label = str(mode)
    if label == "bypassPermissions":
        label = "bypass permissions"
    return f"permissions · {label}"


def build_question_events(
    ts: str | None,
    questions: list[dict],
    tool_use_id: str,
) -> list[dict]:
    events: list[dict] = []
    for entry in questions:
        if not isinstance(entry, dict):
            continue
        options = [
            str(option.get("label") or "")
            for option in (entry.get("options") or [])
            if isinstance(option, dict) and str(option.get("label") or "")
        ]
        events.append(
            {
                "kind": "question",
                "ts": ts,
                "text": str(entry.get("question") or "").strip(),
                "tool_use_id": tool_use_id,
                "question": {
                    "tool_use_id": tool_use_id,
                    "prompt": str(entry.get("question") or "").strip(),
                    "header": str(entry.get("header") or "").strip() or None,
                    "options": options,
                    "multi_select": entry.get("multiSelect") is True,
                    "answered_option": None,
                    "answered_options": [],
                    "custom_reply": None,
                },
            }
        )
    return events


def _emit_question_events(state: dict, ts: str | None, questions: list[dict], tool_use_id: str) -> None:
    pending_questions: dict = state.setdefault("pending_questions", {})
    refs: list[dict] = []
    for event in build_question_events(ts, questions, tool_use_id):
        refs.append(_append_event(state, event))
    if refs:
        pending_questions[tool_use_id] = refs


def _coerce_question_answer_value(value: object) -> str | list[str] | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, list):
        parts = [
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
        return parts or None
    if isinstance(value, dict):
        direct = value.get("answer")
        coerced = _coerce_question_answer_value(direct)
        if coerced is not None:
            return coerced
        nested = value.get("answers")
        return _coerce_question_answer_value(nested)
    return None


def _question_answer_map(
    refs: list[dict],
    result: str,
    structured_answers: object,
) -> dict[str, str | list[str]]:
    prompts = [
        str((event.get("question") or {}).get("prompt") or "").strip()
        for event in refs
    ]
    answers_by_prompt: dict[str, str | list[str]] = {}
    if isinstance(structured_answers, dict):
        for key, raw_value in structured_answers.items():
            answer = _coerce_question_answer_value(raw_value)
            if answer is None:
                continue
            prompt: str | None = None
            if isinstance(key, str) and key in prompts:
                prompt = key
            elif isinstance(key, str) and key.isdigit():
                index = int(key)
                if 0 <= index < len(prompts):
                    prompt = prompts[index]
            if prompt:
                answers_by_prompt[prompt] = answer
    if answers_by_prompt:
        return answers_by_prompt
    prefix = "Your questions have been answered: "
    suffix = ". You can now continue with these answers in mind."
    body = result.strip()
    if body.startswith(prefix):
        body = body[len(prefix):]
    if body.endswith(suffix):
        body = body[: -len(suffix)]
    cursor = 0
    for idx, prompt in enumerate(prompts):
        marker = f'"{prompt}"='
        start = body.find(marker, cursor)
        if start < 0:
            continue
        value_start = start + len(marker)
        next_start = len(body)
        for next_prompt in prompts[idx + 1 :]:
            candidate = body.find(f', "{next_prompt}"=', value_start)
            if candidate >= 0:
                next_start = candidate
                break
        raw_value = body[value_start:next_start].strip().rstrip(",")
        if raw_value.startswith('"') and raw_value.endswith('"') and len(raw_value) >= 2:
            raw_value = raw_value[1:-1]
        raw_value = raw_value.strip()
        if raw_value:
            answers_by_prompt[prompt] = raw_value
        cursor = next_start
    return answers_by_prompt


def _question_answer_values(
    raw_value: str | list[str],
    options: list[str],
    multi_select: bool,
) -> list[str]:
    if isinstance(raw_value, list):
        return raw_value
    if not multi_select or raw_value in options:
        return [raw_value]
    # Claude Code records multi-select answers as a comma-space separated
    # string. Prefer an exact option match above so labels containing commas
    # remain intact when only that option was selected.
    return [part.strip() for part in raw_value.split(", ") if part.strip()]


def _apply_question_answers(
    state: dict,
    tool_use_id: str,
    result: str,
    structured_answers: object = None,
) -> None:
    refs = state.get("pending_questions", {}).pop(tool_use_id, None)
    if not refs:
        return
    answers_by_prompt = _question_answer_map(refs, result, structured_answers)
    changed_indices: list[int] = []
    for event in refs:
        question_meta = event.get("question") or {}
        raw_value = answers_by_prompt.get(str(question_meta.get("prompt") or ""))
        if raw_value is None:
            continue
        options = question_meta.get("options") or []
        multi_select = question_meta.get("multi_select") is True
        values = _question_answer_values(raw_value, options, multi_select)
        answered_options = [
            index for index, label in enumerate(options) if label in values
        ]
        custom_values = [value for value in values if value not in options]
        question_meta["answered_options"] = answered_options
        question_meta["answered_option"] = (
            answered_options[0]
            if not multi_select and len(answered_options) == 1
            else None
        )
        question_meta["custom_reply"] = ", ".join(custom_values) or None
        changed_indices.append(int(event["id"]) - int(state.get("base", 0)))
    for changed_index in changed_indices:
        if 0 <= changed_index < len(state["events"]):
            _mark_tail_changed(state, changed_index)


def _tool_reference_event(ts: str | None, tool_name: str) -> dict:
    return _marker_event(ts, f"tool reference · {tool_name}", "tool_reference")


def _render_claude_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif block.get("type") == "tool_reference":
                name = block.get("tool_name") or "unknown tool"
                parts.append(f"Tool reference: {name}")
        return "\n".join(parts)
    return json.dumps(content) if content is not None else ""


def _emit_tasks(state: dict, ts: str | None) -> None:
    """Append a `kind:"tasks"` event when the status vector changes. The event
    embeds a deep copy of the task list so later deltas can't retroactively
    mutate it (delta protocol relies on event objects being immutable once emitted)."""
    tasks = state.get("tasks") or []
    key = tuple((t["id"], t.get("status", "pending")) for t in tasks)
    if key == state.get("tasks_key"):
        return
    state["tasks_key"] = key
    snap = [dict(t) for t in tasks]
    counts = {"completed": 0, "in_progress": 0, "pending": 0}
    for task in snap:
        counts[task.get("status", "pending")] = counts.get(task.get("status", "pending"), 0) + 1
    total = len(snap)
    summary = (
        f"{total} task{'s' if total != 1 else ''} "
        f"({counts['completed']} done, {counts['in_progress']} in progress, {counts['pending']} open)"
    )
    _append_event(state, {"kind": "tasks", "ts": ts, "text": summary, "tasks": snap})


def _apply_task_delta(state: dict, row: dict, task_meta: dict, ts: str | None) -> None:
    """Apply a TaskCreate or TaskUpdate result between task_reminder snapshots."""
    tasks: list = state.setdefault("tasks", [])
    tur = row.get("toolUseResult") or {}
    if task_meta.get("kind") == "create":
        info = tur.get("task") or {}
        tid = info.get("id")
        subject = info.get("subject") or task_meta.get("subject") or ""
        if tid is None:
            return
        active_form = task_meta.get("activeForm")
        if active_form:
            state.setdefault("task_activeform", {})[subject] = active_form
        for task in tasks:
            if task["id"] == str(tid):
                task["subject"] = subject
                _emit_tasks(state, ts)
                return
        new_task = {"id": str(tid), "subject": subject, "status": "pending", "blockedBy": []}
        if active_form:
            new_task["activeForm"] = active_form
        tasks.append(new_task)
        _emit_tasks(state, ts)
        return
    # update
    tid = tur.get("taskId") or task_meta.get("taskId")
    status = (tur.get("statusChange") or {}).get("to") or task_meta.get("status")
    if tid is None or not status:
        return
    for task in tasks:
        if task["id"] == str(tid):
            task["status"] = status
            _emit_tasks(state, ts)
            return


def _xml_tag(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", text)
    return match.group(1).strip() if match else None


_BASH_TAG_PATTERN = re.compile(r"<(bash-input|bash-stdout|bash-stderr)>([\s\S]*?)</\1>")


def _bash_event(ts: str | None, parts: dict[str, str]) -> dict:
    shell = {
        "input": _clip(parts.get("input", ""), MAX_TEXT),
        "stdout": _clip(parts.get("stdout", ""), MAX_TEXT),
        "stderr": _clip(parts.get("stderr", ""), MAX_TEXT),
    }
    text = shell["input"] or shell["stdout"] or shell["stderr"]
    return {"kind": "bash", "ts": ts, "text": text, "bash": shell}


def _claude_user_event(text: str, ts: str | None) -> dict | None:
    stripped = text.strip()
    if stripped.startswith("[Request interrupted"):
        return {"kind": "interrupt", "ts": ts, "text": _clip(stripped, 400)}
    if stripped.startswith("<task-notification>"):
        summary = _xml_tag(stripped, "summary")
        event = _xml_tag(stripped, "event")
        status = _xml_tag(stripped, "status")
        parts = [p for p in (summary, event) if p]
        label = " — ".join(parts) or "task notification"
        if status and status not in label:
            label = f"[{status}] {label}"
        return {"kind": "notification", "ts": ts, "text": _clip(label, MAX_TEXT)}
    if stripped.startswith(("<local-command-stdout", "<local-command-caveat")):
        return None  # slash-command plumbing noise
    if "<command-name>" in stripped:
        name = _xml_tag(stripped, "command-name") or ""
        args = _xml_tag(stripped, "command-args") or ""
        return {"kind": "command", "ts": ts, "text": f"{name} {args}".strip()}
    # Injected reminders wrap real prompts — drop the wrapper, keep the human text.
    cleaned = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", stripped).strip()
    bash_matches = list(_BASH_TAG_PATTERN.finditer(cleaned))
    if bash_matches and not _BASH_TAG_PATTERN.sub("", cleaned).strip():
        parts: dict[str, str] = {}
        for match in bash_matches:
            key = match.group(1).removeprefix("bash-")
            value = match.group(2).strip()
            if key in parts and value:
                parts[key] = f"{parts[key]}\n{value}".strip()
            else:
                parts[key] = value
        return _bash_event(ts, parts)
    if not cleaned:
        return None
    return {"kind": "user", "ts": ts, "text": _clip(cleaned, MAX_TEXT)}


_WIKI_IMAGES_PATTERN = re.compile(r"\[images?: ([^\]]+)\]")
IMG_TOKEN_OPEN = "\u27e6img:"
IMG_TOKEN_CLOSE = "\u27e7"


def _img_token(url: str) -> str:
    return f"{IMG_TOKEN_OPEN}{url}{IMG_TOKEN_CLOSE}"


def _claude_user_events(text: str, ts: str | None) -> list[dict]:
    """User text → events. Wiki-composer image refs ([image: /tmp/wiki-uploads/x.png])
    become inline ⟦img:url⟧ tokens the renderer swaps for chips — in place, not appended."""

    def rewrite(match: re.Match) -> str:
        tokens = []
        for raw in match.group(1).split():
            path = PurePosixPath(raw)
            if str(path.parent) == "/tmp/wiki-uploads":
                tokens.append(_img_token(f"/api/uploads/{path.name}"))
        return " ".join(tokens) if tokens else match.group(0)

    rewritten = _WIKI_IMAGES_PATTERN.sub(rewrite, text)
    event = _claude_user_event(rewritten, ts)
    return [event] if event else []


def _append_claude_user(state: dict, event: dict, row: dict) -> None:
    """Edited/resent prompts fork the tree: sibling user rows share parentUuid.
    Claude Code shows only the newest branch — replace the stale draft in place."""
    events: list = state["events"]
    if event["kind"] == "bash":
        prev = state.get("last_bash")
        parent = row.get("parentUuid")
        if prev and parent and prev["row_uuid"] == parent and prev["index"] == len(events) - 1:
            current = events[-1]
            shell = current.setdefault("bash", {})
            next_shell = event.get("bash") or {}
            for key in ("input", "stdout", "stderr"):
                if next_shell.get(key):
                    shell[key] = next_shell[key]
            current["text"] = shell.get("input") or shell.get("stdout") or shell.get("stderr") or ""
            state["last_bash"] = {"row_uuid": row.get("uuid"), "index": len(events) - 1}
            _mark_tail_changed(state, len(events) - 1)
            return
        _append_event(state, event)
        state["last_bash"] = {"row_uuid": row.get("uuid"), "index": len(events) - 1}
        return
    parent = row.get("parentUuid")
    prev = state.get("last_user")
    if (
        event["kind"] == "user"
        and prev
        and parent
        and prev["parent"] == parent
        and prev["index"] == len(events) - 1
    ):
        _replace_event_at(state, len(events) - 1, event)
    else:
        _append_event(state, event)
    if event["kind"] == "user":
        state["last_user"] = {"parent": parent, "index": len(events) - 1}


_IMAGE_MARKER_PATTERN = re.compile(r"\[Image #\d+\]")


def _assemble_user_content(content: list) -> str:
    """Interleave text + image blocks: terminal-pasted [Image #N] markers become
    inline ⟦img⟧ tokens; markerless images token in at their block position."""
    urls: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                name = cache_image(source.get("media_type") or "", source.get("data") or "")
                urls.append(f"/api/transcript-images/{name}" if name else "")
    queue = [u for u in urls if u]
    all_text = " ".join(
        block.get("text") or ""
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    use_markers = bool(_IMAGE_MARKER_PATTERN.search(all_text))
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            chunk = block.get("text") or ""
            if use_markers:
                chunk = _IMAGE_MARKER_PATTERN.sub(
                    lambda m: _img_token(queue.pop(0)) if queue else m.group(0), chunk
                )
            parts.append(chunk)
        elif btype == "image" and not use_markers and queue:
            parts.append(_img_token(queue.pop(0)))
    for url in queue:  # more images than markers — don't drop them
        parts.append(_img_token(url))
    return "\n".join(p for p in parts if p.strip())


def _claude_apply(state: dict, row: dict) -> None:
    pending: dict = state["pending"]  # tool_use id → event
    rtype = row.get("type")
    ts = row.get("timestamp")

    if rtype == "rate_limit_event":
        rate_limit = _claude_rate_limit_data(row)
        state.setdefault("session_meta", {})["rate_limit"] = rate_limit
        if rate_limit.get("status") != "allowed":
            _append_event(
                state,
                {
                    "kind": "claude_rate_limit",
                    "ts": ts,
                    "text": str(rate_limit.get("status") or "rate limit"),
                    "claude_rate_limit": rate_limit,
                },
            )
        _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        return

    if rtype == "pr-link":
        number = row.get("prNumber")
        url = row.get("prUrl")
        if number and url:
            info = {"number": number, "url": url}
            if state.get("pr") != info:
                state["pr"] = info
                _append_event(state, {"kind": "pr", "ts": ts, "text": f"PR #{number}", "pr": info})
        _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        return

    if rtype in _CLAUDE_IGNORED_TYPES:
        _record_row_disposition(state, EVENT_DISPOSITION_IGNORED)
        return

    if rtype == "custom-title":
        title = row.get("customTitle")
        if isinstance(title, str) and title.strip():
            state.setdefault("session_meta", {})["custom_title"] = title.strip()
        _record_row_disposition(state, EVENT_DISPOSITION_SUMMARIZED)
        return

    if rtype == "agent-name":
        agent_name = row.get("agentName")
        if isinstance(agent_name, str) and agent_name.strip():
            state.setdefault("session_meta", {})["agent_name"] = agent_name.strip()
        _record_row_disposition(state, EVENT_DISPOSITION_SUMMARIZED)
        return

    if rtype == "permission-mode":
        _append_event(state, _marker_event(ts, _claude_permission_text(row), "permission-mode"))
        _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        return

    if rtype == "progress":
        _append_event(state, _marker_event(ts, _claude_progress_text(row), "progress"))
        _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        return

    if rtype == "system":
        subtype = row.get("subtype")
        if subtype == "thinking_tokens":
            estimated = row.get("estimated_tokens")
            delta = row.get("estimated_tokens_delta")
            current = state.get("thinking_tokens", 0)
            if isinstance(estimated, (int, float)):
                current = int(estimated)
            elif isinstance(delta, (int, float)):
                current += int(delta)
            state["thinking_tokens"] = max(0, current)
            state.setdefault("session_meta", {})["thinking_tokens"] = {
                "total": state["thinking_tokens"],
                "estimated_tokens": estimated,
                "estimated_tokens_delta": delta,
            }
            _record_row_disposition(state, EVENT_DISPOSITION_SUMMARIZED)
            return
        if subtype == "init":
            init = _claude_init_data(row)
            model = str(init.get("model") or "claude")
            cwd = str(init.get("cwd") or "")
            text = f"session started: {model}"
            if cwd:
                text += f" in {cwd}"
            _append_event(
                state,
                {
                    "kind": "claude_init",
                    "ts": ts,
                    "text": text,
                    "claude_init": init,
                },
            )
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
            return
        if subtype == "task_notification":
            task = _claude_task_data(row)
            event = {
                "kind": "claude_task",
                "ts": ts,
                "text": str(task.get("summary") or "task notification"),
                "claude_task": task,
            }
            _append_event(state, event)
            task_id = task.get("task_id")
            if task_id is not None:
                state.setdefault("claude_task_events", {})[str(task_id)] = event
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
            return
        if subtype == "task_updated":
            patch = _claude_task_patch(row.get("patch"))
            task_id = row.get("task_id") or patch.get("task_id")
            event = state.setdefault("claude_task_events", {}).get(str(task_id)) if task_id is not None else None
            if event is not None:
                task = event.setdefault("claude_task", {})
                task.update(patch)
                status = task.get("status") or "updated"
                summary = task.get("summary") or "task updated"
                event["text"] = f"{status}: {summary}"
                _mark_tail_changed(state, int(event["id"]) - int(state.get("base", 0)))
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
            return
        if subtype == "api_retry":
            retry = _claude_retry_data(row)
            _append_event(
                state,
                {
                    "kind": "claude_api_retry",
                    "ts": ts,
                    "text": str(retry.get("error_status") or retry.get("error") or "api retry"),
                    "claude_api_retry": retry,
                },
            )
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
            return
        rendered = _claude_system_text(row)
        if rendered:
            subtype, text = rendered
            _append_event(state, _marker_event(ts, text, subtype))
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        else:
            _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
        return

    if rtype == "attachment":
        attachment = row.get("attachment") or {}
        if attachment.get("type") != "task_reminder":
            _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
            return
        content = attachment.get("content")
        if not isinstance(content, list):
            _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
            return
        new_tasks: list = []
        for entry in content:
            if not isinstance(entry, dict) or entry.get("id") is None:
                continue
            new_tasks.append({
                "id": str(entry["id"]),
                "subject": entry.get("subject") or "",
                "status": entry.get("status") or "pending",
                "blockedBy": entry.get("blockedBy") or [],
            })
        active_form_map = state.get("task_activeform") or {}
        for task in new_tasks:
            active_form = active_form_map.get(task["subject"])
            if active_form:
                task["activeForm"] = active_form
        state["tasks"] = new_tasks
        _emit_tasks(state, ts)
        _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        return

    if rtype not in ("user", "assistant"):
        _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
        return
    if rtype == "user" and row.get("isMeta"):
        _record_row_disposition(state, EVENT_DISPOSITION_IGNORED)
        return  # injected context wrapping — not a real user message
    if row.get("isSidechain") and not state.get("sidechain_ok"):
        _record_row_disposition(state, EVENT_DISPOSITION_IGNORED)
        return
    message = row.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        if rtype == "user" and content.strip():
            for event in _claude_user_events(content, ts):
                _append_claude_user(state, event, row)
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        else:
            _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
        return
    if not isinstance(content, list):
        _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
        return
    if rtype == "user" and any(
        isinstance(b, dict) and b.get("type") in ("text", "image") for b in content
    ):
        assembled = _assemble_user_content(content)
        if assembled.strip():
            for event in _claude_user_events(assembled, ts):
                _append_claude_user(state, event, row)
    rendered = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                if rtype == "user":
                    pass  # handled by _assemble_user_content above
                else:
                    _append_event(state, {"kind": "assistant", "ts": ts, "text": _clip(text, MAX_TEXT)})
                    rendered = True
        elif btype == "thinking":
            _append_event(
                state,
                {"kind": "thinking", "ts": ts, "text": _clip(block.get("thinking") or "", MAX_TEXT)},
            )
            rendered = True

        elif btype == "tool_use":
            name = block.get("name") or "tool"
            raw_input = block.get("input")
            block_id = block.get("id")
            if name == "AskUserQuestion" and isinstance(raw_input, dict) and block_id:
                _emit_question_events(state, ts, raw_input.get("questions") or [], block_id)
                rendered = True
            elif _is_artifact_tool(name) and block_id:
                state.setdefault("pending_artifacts", {})[block_id] = {
                    "name": name,
                    "input": _tool_arguments(raw_input) or {},
                }
                rendered = True
            else:
                tool_input = _codex_tool_input(name, raw_input)
                archetype, summary = classify_tool(name, tool_input)
                event = {
                    "kind": "tool",
                    "ts": ts,
                    "text": "",
                    "tool": {
                        "name": name,
                        "input": tool_input,
                        "output": None,
                        "ok": None,
                        "archetype": archetype,
                        "summary": summary,
                    },
                }
                if name in ("Agent", "Task") and isinstance(raw_input, dict):
                    prompt = raw_input.get("prompt")
                    if isinstance(prompt, str):
                        event["tool"]["prompt_head"] = prompt[:120]
                _append_event(state, event)
                rendered = True
                if block_id:
                    pending[block_id] = event
                    if name == "TaskCreate" and isinstance(raw_input, dict):
                        state.setdefault("task_inputs", {})[block_id] = {
                            "kind": "create",
                            "subject": raw_input.get("subject") or "",
                            "activeForm": raw_input.get("activeForm"),
                        }
                    elif name == "TaskUpdate" and isinstance(raw_input, dict):
                        state.setdefault("task_inputs", {})[block_id] = {
                            "kind": "update",
                            "taskId": raw_input.get("taskId"),
                            "status": raw_input.get("status"),
                        }
        elif btype == "tool_result":
            tool_use_id = block.get("tool_use_id")
            result_text = _render_claude_result_text(block.get("content"))
            if tool_use_id and isinstance(block.get("content"), str):
                tool_use_result = row.get("toolUseResult")
                if not isinstance(tool_use_result, dict):
                    tool_use_result = row.get("tool_use_result")
                structured_answers = (
                    tool_use_result.get("answers")
                    if isinstance(tool_use_result, dict)
                    else None
                )
                _apply_question_answers(
                    state,
                    tool_use_id,
                    block.get("content") or "",
                    structured_answers,
                )
                rendered = True
            if _complete_artifact(
                state,
                tool_use_id,
                result_text,
                ts,
                failed=bool(block.get("isError") or block.get("is_error")),
            ):
                rendered = True
                continue
            event = pending.pop(tool_use_id, None)
            if event:
                event["tool"]["output"] = _clip(result_text, MAX_TOOL_IO)
                event["tool"]["ok"] = not block.get("is_error")
                _record_tool_patch(state, event)
                rendered = True
            task_meta = state.get("task_inputs", {}).pop(tool_use_id, None) if tool_use_id else None
            if task_meta:
                _apply_task_delta(state, row, task_meta, ts)
                rendered = True
            for result_block in block.get("content") or [] if isinstance(block.get("content"), list) else []:
                if isinstance(result_block, dict) and result_block.get("type") == "tool_reference":
                    name = str(result_block.get("tool_name") or "unknown tool")
                    _append_event(state, _tool_reference_event(ts, name))
                    rendered = True
        elif btype == "tool_reference":
            name = str(block.get("tool_name") or "unknown tool")
            _append_event(state, _tool_reference_event(ts, name))
            rendered = True
        elif btype == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                name = cache_image(source.get("media_type") or "", source.get("data") or "")
                if name:
                    _append_event(state, {"kind": "image", "ts": ts, "text": f"/api/transcript-images/{name}"})
                    rendered = True
    _record_row_disposition(
        state,
        EVENT_DISPOSITION_RENDERED if rendered or (rtype == "user" and any(
            isinstance(b, dict) and b.get("type") in ("text", "image") for b in content
        )) else EVENT_DISPOSITION_UNKNOWN,
    )


# ---------------------------------------------------------------- incremental cache

_APPLY = {
    "codex": _codex_apply,
    "claude": _claude_apply,
    "claude-sub": _claude_apply,
    "codex-normalized": _codex_normalized_apply,
    "claude-normalized": _claude_normalized_apply,
}
_cache: dict[str, dict] = {}  # path → parse state; wiped on reload, rebuilt lazily
_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_guard = threading.Lock()


def _cache_lock_for(key: str) -> threading.Lock:
    with _cache_locks_guard:
        lock = _cache_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _cache_locks[key] = lock
        return lock


def _new_parse_state(fmt: str) -> dict:
    return {
        "offset": 0,
        "buffer": "",
        "events": [],
        "pending": {},
        "pending_artifacts": {},
        "pending_questions": {},
        "tokens": None,
        "base": 0,
        "next_event_id": 0,
        "cursor": 0,
        "changes": [],
        "last_user": None,
        "last_bash": None,
        "sidechain_ok": fmt == "claude-sub",
        "tasks": [],
        "tasks_key": (),
        "pr": None,
        "session_meta": {},
        "task_inputs": {},
        "task_activeform": {},
        "claude_task_events": {},
        "thinking_tokens": 0,
        "artifact_ids": set(),
        "dedupe_credits": {},
        "dispositions": {"rendered": 0, "summarized": 0, "ignored": 0, "unknown": 0},
    }


def _prune_change_log(state: dict) -> None:
    base = int(state["base"])
    state["changes"] = [
        change
        for change in state.get("changes", [])
        if (
            change.get("kind") == "tail"
            and int(change.get("from", base)) >= base
        )
        or (
            change.get("kind") == "patch"
            and int(change.get("index", base)) >= base
        )
    ]


def _read_cached_state(fmt: str, path: Path, key: str) -> dict:
    stat = path.stat()
    state = _cache.get(key)
    if state is None or stat.st_size < state["offset"]:
        state = _new_parse_state(fmt)
        _cache[key] = state
    if stat.st_size > state["offset"]:
        apply = _APPLY[fmt]
        with path.open(encoding="utf-8", errors="replace") as f:
            f.seek(state["offset"])
            chunk = state["buffer"] + f.read()
            state["offset"] = f.tell()
        lines = chunk.split("\n")
        state["buffer"] = lines.pop()  # trailing partial line waits for next read
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            try:
                apply(state, row)
            except (KeyError, TypeError, AttributeError):
                continue
        if len(state["events"]) > 2000:
            trim = len(state["events"]) - 2000
            state["base"] += trim
            state["events"] = state["events"][-2000:]
            kept = set(map(id, state["events"]))
            state["pending"] = {
                call_id: event for call_id, event in state["pending"].items() if id(event) in kept
            }
            _prune_change_log(state)
            state["last_bash"] = None
    return state


def read_session_events(fmt: str, path: Path) -> dict:
    """Returns {events, base, tokens, dirty_from}.

    base = absolute index of events[0] (grows when the buffer trims).
    dirty_from = absolute index of the oldest tool event still awaiting its
    output — everything at/after it can mutate on a later read, so delta
    consumers must re-fetch from min(cursor, dirty_from).
    """
    key = str(path)
    with _cache_lock_for(key):
        state = _read_cached_state(fmt, path, key)
        total = state["base"] + len(state["events"])
        dirty_from = total
        pending_events = set(map(id, state["pending"].values()))
        if pending_events:
            for i, event in enumerate(state["events"]):
                if id(event) in pending_events:
                    dirty_from = state["base"] + i
                    break
        return {
            "events": deepcopy(state["events"]),
            "base": state["base"],
            "tokens": state["tokens"],
            "tasks": deepcopy(state.get("tasks") or []),
            "pr": deepcopy(state.get("pr")),
            "session_meta": deepcopy(state.get("session_meta") or {}),
            "dispositions": dict(state.get("dispositions") or {}),
            "dirty_from": dirty_from,
            "cursor": state.get("cursor", 0),
        }


def _snapshot_events(events: list[dict]) -> list[dict]:
    """Copy event objects and the nested leaves parsers mutate in place."""
    snapshot: list[dict] = []
    for event in events:
        copied = dict(event)
        for key in ("tool", "question"):
            value = event.get(key)
            if isinstance(value, dict):
                copied[key] = dict(value)
        snapshot.append(copied)
    return snapshot


def read_session_delta(
    fmt: str,
    path: Path,
    cursor: int = 0,
    *,
    tail_window: bool = True,
) -> dict:
    key = str(path)
    with _cache_lock_for(key):
        state = _read_cached_state(fmt, path, key)
        base = int(state["base"])
        events = state["events"]
        current_cursor = int(state.get("cursor", 0))
        total = base + len(events)
        changes: list[dict] = list(state.get("changes", []))
        tasks = deepcopy(state.get("tasks") or [])
        pr = deepcopy(state.get("pr"))

        full_reset = cursor <= 0 or cursor > current_cursor
        if not full_reset:
            if not changes:
                full_reset = cursor != current_cursor
            else:
                first_cursor = int(changes[0]["cursor"])
                if cursor < first_cursor - 1:
                    full_reset = True

        if full_reset:
            window_base = max(base, total - TAIL_WINDOW_EVENTS) if tail_window else base
            return {
                # Snapshot only the event objects and mutable nested leaves;
                # avoid a recursive clone of large immutable payloads.
                "events": _snapshot_events(events[window_base - base :]),
                "base": window_base,
                "tokens": state["tokens"],
                "tasks": tasks,
                "pr": pr,
                "session_meta": deepcopy(state.get("session_meta") or {}),
                "dispositions": dict(state.get("dispositions") or {}),
                "cursor": current_cursor,
                "tail_from": window_base,
                "patches": [],
                "has_older": window_base > base,
            }

        changed = [entry for entry in changes if int(entry["cursor"]) > cursor]
        tail_from = total
        patch_map: dict[int, dict] = {}
        for entry in changed:
            if entry.get("kind") == "tail":
                tail_from = min(tail_from, int(entry.get("from", total)))
            elif entry.get("kind") == "patch":
                patch_map[int(entry["id"])] = entry

        if tail_from < base:
            return {
                "events": _snapshot_events(events),
                "base": base,
                "tokens": state["tokens"],
                "tasks": tasks,
                "pr": pr,
                "session_meta": deepcopy(state.get("session_meta") or {}),
                "dispositions": dict(state.get("dispositions") or {}),
                "cursor": current_cursor,
                "tail_from": base,
                "patches": [],
                "has_older": False,
            }

        if tail_from < total:
            tail_events = deepcopy(events[tail_from - base :])
            patch_map = {
                event_id: entry for event_id, entry in patch_map.items() if int(entry.get("index", total)) < tail_from
            }
        else:
            tail_events = []

        patches = [
            {
                "id": int(entry["id"]),
                "output": entry.get("output"),
                "ok": entry.get("ok"),
            }
            for entry in sorted(patch_map.values(), key=lambda item: int(item["cursor"]))
        ]
        return {
            "events": tail_events,
            "base": base,
            "tokens": state["tokens"],
            "tasks": tasks,
            "pr": pr,
            "session_meta": deepcopy(state.get("session_meta") or {}),
            "dispositions": dict(state.get("dispositions") or {}),
            "cursor": current_cursor,
            "tail_from": tail_from,
            "patches": patches,
        }


def read_older_session(
    fmt: str,
    path: Path,
    before: int,
    count: int,
) -> dict:
    """Return the retained events immediately before an absolute event index."""
    key = str(path)
    with _cache_lock_for(key):
        state = _read_cached_state(fmt, path, key)
        base = int(state["base"])
        events = state["events"]
        total = base + len(events)
        end = min(max(before, base), total)
        start = max(base, end - max(1, count))
        return {
            "events": _snapshot_events(events[start - base : end - base]),
            "base": start,
            "has_older": start > base,
        }


# ---------------------------------------------------------------- claude subagents

_subagent_heads: dict[str, str] = {}  # file path → first-user-prompt head (immutable once written)


def subagents_dir(main_path: Path) -> Path:
    return main_path.parent / main_path.stem / "subagents"


def _subagent_prompt_head(path: Path) -> str:
    key = str(path)
    cached = _subagent_heads.get(key)
    if cached is not None:
        return cached
    head = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") == "user":
                    content = (row.get("message") or {}).get("content")
                    if isinstance(content, str):
                        head = content[:120]
                    break
    except OSError:
        pass
    if head:
        _subagent_heads[key] = head
    return head


def list_subagents(main_path: Path) -> list[dict]:
    directory = subagents_dir(main_path)
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("agent-*.jsonl")):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "id": path.stem.removeprefix("agent-"),
                "prompt_head": _subagent_prompt_head(path),
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )
    return entries


def annotate_agent_events(main_path: Path, events: list) -> list:
    """Attach subagent ids without mutating parser-owned cached events."""
    heads: dict[str, str] | None = None
    annotated = events
    for index, event in enumerate(events):
        tool = event.get("tool")
        if not tool or tool.get("name") not in ("Agent", "Task") or tool.get("agent_id"):
            continue
        prompt_head = tool.get("prompt_head")
        if not prompt_head:
            continue
        if heads is None:
            heads = {e["prompt_head"]: e["id"] for e in list_subagents(main_path) if e["prompt_head"]}
        agent_id = heads.get(prompt_head)
        if agent_id:
            if annotated is events:
                annotated = list(events)
            annotated[index] = {
                **event,
                "tool": {**tool, "agent_id": agent_id},
            }
    return annotated
