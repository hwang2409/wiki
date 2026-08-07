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

import ast
import base64
import binascii
import json
import os
import re
import threading
from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from .image_scrub import ImageScrubError, probe_normalized_dimensions
from .wiki_artifacts import (
    AUDIO_MIMES,
    ArtifactValidationError,
    IMAGE_TYPES,
    PDF_MIME,
    VIDEO_MIMES,
    VISUAL_DIFF_VARIANTS,
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
TRANSCRIPT_CACHE_VERSION = 2
MAX_EDIT_PAYLOAD = 10_000
MAX_CODEX_HARNESS_SOURCE = 100_000
MAX_CODEX_HARNESS_CALLS = 32
MAX_CODEX_BATCH_CHILD_INPUT = 600

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

_STRUCTURED_BINARY_MIMES = {
    "image": frozenset(IMAGE_TYPES),
    "pdf": frozenset({PDF_MIME}),
    "video": frozenset(VIDEO_MIMES),
    "audio": frozenset(AUDIO_MIMES),
}


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
    if name == "wait":
        return ("wait", "wait" if not tool_input else f"wait {_clip(tool_input, 54)}")
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


_JS_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_JS_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _skip_js_space(source: str, index: int) -> int:
    while index < len(source):
        if source[index].isspace():
            index += 1
        elif source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline == -1 else newline + 1
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = len(source) if end == -1 else end + 2
        else:
            break
    return index


def _js_string_end(source: str, opening: int) -> int | None:
    quote = source[opening]
    escaped = False
    for index in range(opening + 1, len(source)):
        char = source[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return index + 1
        elif char in "\r\n":
            return None
    return None


def _balanced_js_call_end(source: str, opening: int) -> int | None:
    stack = [")"]
    index = opening + 1
    pairs = {"(": ")", "[": "]", "{": "}"}
    while index < len(source):
        char = source[index]
        if char in ("'", '"'):
            index = _js_string_end(source, index)
            if index is None:
                return None
            continue
        if char == "`":
            return None
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline == -1 else newline + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                return None
            index = end + 2
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or char != stack.pop():
                return None
            if not stack:
                return index
        index += 1
    return None


def _codex_harness_calls(source: str) -> list[tuple[str, str]] | None:
    """Find tools calls without matching text inside strings or comments."""
    if len(source) > MAX_CODEX_HARNESS_SOURCE:
        return None
    calls: list[tuple[str, str]] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char in ("'", '"'):
            index = _js_string_end(source, index)
            if index is None:
                return None
            continue
        if char == "`":
            return None
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline == -1 else newline + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                return None
            index = end + 2
            continue
        if source.startswith("tools.", index) and (
            index == 0 or not (source[index - 1].isalnum() or source[index - 1] in "_$")
        ):
            name_match = _JS_IDENTIFIER.match(source, index + len("tools."))
            if name_match:
                opening = _skip_js_space(source, name_match.end())
                if opening < len(source) and source[opening] == "(":
                    end = _balanced_js_call_end(source, opening)
                    if end is None:
                        return None
                    calls.append((name_match.group(0), source[opening + 1:end].strip()))
                    if len(calls) > MAX_CODEX_HARNESS_CALLS:
                        return None
                    index = end + 1
                    continue
        index += 1
    return calls


def _decode_js_string(source: str, opening: int) -> tuple[str, int] | None:
    end = _js_string_end(source, opening)
    if end is None:
        return None
    literal = source[opening:end]
    try:
        value = json.loads(literal)
    except (TypeError, ValueError):
        try:
            value = ast.literal_eval(literal)
        except (SyntaxError, ValueError):
            return None
    return value, end if isinstance(value, str) else None


class _CodexJsArgumentParser:
    def __init__(
        self,
        source: str,
        variables: dict[str, str] | None = None,
        resolving: frozenset[str] | None = None,
    ):
        self.source = source
        self.index = 0
        # Bare identifiers get resolved against locally-declared string/object
        # literals (`const patch = "..."; tools.apply_patch(patch)`) so the
        # arg list survives the trivial variable indirection Codex loves.
        self.variables = variables or {}
        self.resolving = resolving or frozenset()

    def _space(self) -> None:
        self.index = _skip_js_space(self.source, self.index)

    def _string(self) -> str | None:
        decoded = _decode_js_string(self.source, self.index)
        if decoded is None:
            return None
        value, end = decoded
        self.index = end
        return value

    def _value(self) -> object | None:
        self._space()
        if self.index >= len(self.source) or self.source[self.index] == "`":
            return None
        if self.source[self.index] in ("'", '"'):
            return self._string()
        if self.source[self.index] == "{":
            return self._object()
        if self.source[self.index] == "[":
            return self._array()
        number = _JS_NUMBER.match(self.source, self.index)
        if number:
            self.index = number.end()
            text = number.group(0)
            return float(text) if any(c in text for c in ".eE") else int(text)
        identifier = _JS_IDENTIFIER.match(self.source, self.index)
        if identifier:
            self.index = identifier.end()
            token = identifier.group(0)
            if token == "true":
                return True
            if token == "false":
                return False
            if token == "null":
                return None
            resolved = self.variables.get(token)
            if resolved is not None and token not in self.resolving:
                # Re-parse the referenced literal in its own parser so nested
                # object/array literals resolve too — no shared cursor state.
                return _CodexJsArgumentParser(
                    resolved,
                    self.variables,
                    self.resolving | {token},
                ).parse()
            return None
        return None

    def _object(self) -> dict | None:
        self.index += 1
        result: dict = {}
        self._space()
        if self.index < len(self.source) and self.source[self.index] == "}":
            self.index += 1
            return result
        while self.index < len(self.source):
            self._space()
            if self.source[self.index] in ("'", '"'):
                key = self._string()
            else:
                key_match = _JS_IDENTIFIER.match(self.source, self.index)
                if not key_match:
                    return None
                key = key_match.group(0)
                self.index = key_match.end()
            self._space()
            if self.index >= len(self.source) or self.source[self.index] != ":":
                return None
            self.index += 1
            value = self._value()
            if value is None and not self._is_null_literal():
                return None
            result[key] = value
            self._space()
            if self.index >= len(self.source):
                return None
            if self.source[self.index] == "}":
                self.index += 1
                return result
            if self.source[self.index] != ",":
                return None
            self.index += 1
            self._space()
            if self.index < len(self.source) and self.source[self.index] == "}":
                self.index += 1
                return result
        return None

    def _array(self) -> list | None:
        self.index += 1
        result: list = []
        self._space()
        if self.index < len(self.source) and self.source[self.index] == "]":
            self.index += 1
            return result
        while self.index < len(self.source):
            value = self._value()
            if value is None and not self._is_null_literal():
                return None
            result.append(value)
            self._space()
            if self.index >= len(self.source):
                return None
            if self.source[self.index] == "]":
                self.index += 1
                return result
            if self.source[self.index] != ",":
                return None
            self.index += 1
            self._space()
            if self.index < len(self.source) and self.source[self.index] == "]":
                self.index += 1
                return result
        return None

    def _is_null_literal(self) -> bool:
        start = self.index - 4
        return (
            self.source[start:self.index] == "null"
            and (
                start <= 0
                or not (
                    self.source[start - 1].isalnum()
                    or self.source[start - 1] in "_$"
                )
            )
            and (
                self.index >= len(self.source)
                or not (
                    self.source[self.index].isalnum()
                    or self.source[self.index] in "_$"
                )
            )
        )

    def parse(self) -> object | None:
        value = self._value()
        self._space()
        if self.index != len(self.source) or not isinstance(value, (dict, str)):
            return None
        return value


def _codex_js_arguments(source: str, variables: dict[str, str] | None = None) -> dict | str | None:
    return _CodexJsArgumentParser(source, variables).parse()


# Trivial variable indirection Codex generates alongside `tools.*` calls:
#
#   const patch = "*** Begin Patch ... *** End Patch";
#   text(await tools.apply_patch(patch));
#
# The parser needs to see the literal to synthesize a semantic apply_patch
# call. We collect the source text of each assigned literal (not the parsed
# value) so the argument parser can re-scan it under its usual grammar rules.
_JS_ASSIGNMENT_HEAD = re.compile(
    r"(?:^|[;\n{])\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*",
)


def _js_literal_end(source: str, start: int) -> int | None:
    if start >= len(source):
        return None
    char = source[start]
    if char in ("'", '"'):
        return _js_string_end(source, start)
    if char in "([{":
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack = [pairs[char]]
        index = start + 1
        while index < len(source):
            ch = source[index]
            if ch in ("'", '"'):
                end = _js_string_end(source, index)
                if end is None:
                    return None
                index = end
                continue
            if ch == "`":
                return None
            if source.startswith("//", index):
                newline = source.find("\n", index + 2)
                index = len(source) if newline == -1 else newline + 1
                continue
            if source.startswith("/*", index):
                end = source.find("*/", index + 2)
                if end == -1:
                    return None
                index = end + 2
                continue
            if ch in pairs:
                stack.append(pairs[ch])
            elif ch in ")]}":
                if not stack or ch != stack.pop():
                    return None
                if not stack:
                    return index + 1
            index += 1
        return None
    return None


def _codex_extract_variables(source: str) -> dict[str, str]:
    """Map identifier -> raw literal source for locally-declared consts."""
    variables: dict[str, str] = {}
    for match in _JS_ASSIGNMENT_HEAD.finditer(source):
        name = match.group(1)
        start = match.end()
        end = _js_literal_end(source, start)
        if end is None or name in variables:
            continue
        # Only first declaration wins; later reassignments could shadow but the
        # tools.* call inline usually happens right after the first `const`.
        variables[name] = source[start:end]
    return variables


def _resolve_call_argument(source: str, variables: dict[str, str]) -> object | None:
    """Parse an arg source, resolving trailing/whole-token identifiers."""
    stripped = source.strip()
    # Bare identifier: use the variable table directly so the literal parses in
    # its own top-level scope (the argument parser only accepts dict/string).
    identifier_match = _JS_IDENTIFIER.fullmatch(stripped)
    if identifier_match:
        resolved = variables.get(stripped)
        if resolved is None:
            return None
        return _codex_js_arguments(resolved, variables)
    return _codex_js_arguments(source, variables)


def _codex_synthesized_bash(call_arguments: list[dict]) -> str | None:
    """Fold a Promise.all of exec_command calls into a shell-flavoured summary.

    All calls must be ``exec_command`` with a string ``cmd``/``command``. The
    commands join with newlines so the shared bash renderer can highlight and
    truncate the composite the same way it does any multi-line shell command.
    """
    commands: list[str] = []
    for arguments in call_arguments:
        if not isinstance(arguments, dict):
            return None
        command = arguments.get("cmd", arguments.get("command"))
        if not isinstance(command, str) or not command.strip():
            return None
        commands.append(command)
    if not commands:
        return None
    return "\n".join(commands)


def _codex_harness_fallback_input(raw_input: str) -> str:
    """Describe an undecodable wrapper without exposing its JavaScript body."""
    names: list[str] = []
    for match in re.finditer(r"\btools\.([A-Za-z_$][\w$]*)", raw_input[:MAX_CODEX_HARNESS_SOURCE]):
        name = match.group(1)
        if name not in names:
            names.append(name)
        if len(names) == 8:
            break
    if names:
        label = ", ".join(names)
        if len(names) == 8:
            label += ", …"
        return _clip(f"Codex tool wrapper could not be decoded: {label}", MAX_TOOL_IO)
    return "Codex tool wrapper could not be decoded"


def _codex_harness_tool(name: object, raw_input: object) -> dict | None:
    """Unwrap the new Codex runtime's ``const r = await tools.*(...)`` input.

    The wrapper is transport, not user intent: pull each ``tools.<name>(...)``
    call up as if the model had invoked the underlying tool directly, so the
    shared inline/block renderer gets clean bash / edit / mcp semantics rather
    than a fenced JavaScript blob to display.
    """
    if name != "exec" or not isinstance(raw_input, str):
        return None
    calls = _codex_harness_calls(raw_input)
    if not calls:
        return None
    variables = _codex_extract_variables(raw_input)
    parsed_arguments = [_resolve_call_argument(arg, variables) for _, arg in calls]
    if any(arg is None for arg in parsed_arguments):
        return None
    children: list[dict[str, str]] = []
    semantic_inputs: list[str] = []
    for (child_name, _), child_arguments in zip(calls, parsed_arguments):
        child_input = _codex_tool_input(child_name, child_arguments)
        if child_name == "exec_command" and isinstance(child_arguments, dict):
            command = child_arguments.get("cmd", child_arguments.get("command"))
            if isinstance(command, str):
                child_input = _clip(command, MAX_TOOL_IO)
        semantic_inputs.append(child_input)
        child_archetype, child_summary = classify_tool(child_name, child_input)
        children.append(
            {
                "name": _clip(child_name, 120),
                "input": _clip(child_input, MAX_CODEX_BATCH_CHILD_INPUT),
                "archetype": child_archetype,
                "summary": child_summary,
            }
        )

    function_name = _clip(calls[0][0], 120)
    primary_arguments = parsed_arguments[0]
    classified_input = semantic_inputs[0]
    display_input = classified_input
    if len(calls) > 1:
        # Homogeneous Promise.all batches of shell commands keep bash grammar
        # so the shared inline/block renderer stays compact and useful.
        if all(fname == "exec_command" for fname, _ in calls):
            combined = _codex_synthesized_bash(
                [args for args in parsed_arguments if isinstance(args, dict)]
            )
            if combined is not None:
                classified_input = _clip(combined, MAX_TOOL_IO)
                display_input = classified_input
        else:
            # Mixed batches keep every semantic sibling in the bounded parent
            # input. The child list lets the OpenCode-style row show hierarchy.
            display_input = _clip(
                "\n".join(f"{child['name']}: {child['input']}" for child in children),
                MAX_TOOL_IO,
            )
    return {
        "name": function_name,
        "input": display_input,
        "classify_input": classified_input,
        "arguments": primary_arguments,
        "batch": children if len(children) > 1 else None,
        "calls": len(calls),
    }


def _codex_output_status(value: dict) -> bool | None:
    for key in ("is_error", "isError", "failed"):
        if isinstance(value.get(key), bool):
            return not value[key]
    if "exit_code" in value:
        exit_code = value.get("exit_code")
        if isinstance(exit_code, (int, float)):
            return exit_code == 0
    return None


@dataclass(frozen=True)
class _CodexOutputDetails:
    text: str
    status: bool | None
    status_explicit: bool
    status_priority: int
    wall_time_ms: int | None


def _strip_codex_runtime_preamble(text: str) -> tuple[str, bool | None, int | None]:
    """Strip one leading Codex exec preamble and return its metadata."""
    lines = text.splitlines(keepends=True)
    if len(lines) >= 3:
        script = lines[0].strip()
        script_match = re.fullmatch(
            r"Script\s+(completed|failed|terminated)", script, re.IGNORECASE
        )
        wall_match = re.fullmatch(
            r"Wall\s+time\s+([0-9]+(?:\.[0-9]+)?)\s+seconds",
            lines[1].strip(),
            re.IGNORECASE,
        )
        if script_match and wall_match and lines[2].strip() == "Output:":
            status = script_match.group(1).lower() == "completed"
            wall_time_ms = round(float(wall_match.group(1)) * 1000)
            return "".join(lines[3:]), status, wall_time_ms
    escaped_match = re.match(
        r"^Script\s+(completed|failed|terminated)\\n"
        r"Wall\s+time\s+([0-9]+(?:\.[0-9]+)?)\s+seconds\\n"
        r"Output:\\n",
        text,
        re.IGNORECASE,
    )
    if escaped_match:
        status = escaped_match.group(1).lower() == "completed"
        wall_time_ms = round(float(escaped_match.group(2)) * 1000)
        return text[escaped_match.end() :], status, wall_time_ms
    return text, None, None


def _codex_tool_output_details(
    value: object, *, strip_runtime_preamble: bool = True
) -> _CodexOutputDetails:
    """Extract command text from Codex output envelopes and infer success."""
    if value is None:
        return _CodexOutputDetails("", None, False, 0, None)
    if isinstance(value, str):
        if strip_runtime_preamble:
            body, runtime_status, wall_time_ms = _strip_codex_runtime_preamble(value)
        else:
            body, runtime_status, wall_time_ms = value, None, None
        had_preamble = runtime_status is not None
        candidate = body.strip()
        for parser in (json.loads, ast.literal_eval):
            if not candidate.startswith(("{", "[")):
                break
            try:
                parsed = parser(candidate)
            except (ValueError, SyntaxError):
                continue
            if isinstance(parsed, (dict, list)):
                nested = _codex_tool_output_details(parsed, strip_runtime_preamble=False)
                if had_preamble or nested.status is not None or nested.text != candidate:
                    if nested.status_priority >= 3:
                        status = nested.status
                        status_priority = nested.status_priority
                    elif runtime_status is not None:
                        status = runtime_status
                        status_priority = 2
                    else:
                        status = nested.status
                        status_priority = nested.status_priority
                    return _CodexOutputDetails(
                        nested.text,
                        status,
                        status_priority >= 3,
                        status_priority,
                        wall_time_ms if wall_time_ms is not None else nested.wall_time_ms,
                    )
        if re.search(r"\bexited with code 0\b", body):
            status = runtime_status if runtime_status is not None else True
            return _CodexOutputDetails(
                body,
                status,
                runtime_status is None,
                2 if runtime_status is not None else 1,
                wall_time_ms,
            )
        match = re.search(r"\b(?:exit(?:ed)?|exit_code)\D+(\d+)\b", body)
        if match:
            status = runtime_status if runtime_status is not None else int(match.group(1)) == 0
            return _CodexOutputDetails(
                body,
                status,
                runtime_status is None,
                2 if runtime_status is not None else 1,
                wall_time_ms,
            )
        return _CodexOutputDetails(
            body,
            runtime_status,
            False,
            2 if runtime_status is not None else 0,
            wall_time_ms,
        )
    if isinstance(value, list):
        nested: list[tuple[_CodexOutputDetails, bool]] = []
        for index, item in enumerate(value):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text = item["text"]
                details = _codex_tool_output_details(
                    text,
                    strip_runtime_preamble=strip_runtime_preamble and index == 0,
                )
                # Preserve old list semantics: plain text did not infer an
                # exit status unless normalization changed that text.
                nested.append((details, details.text != text))
            else:
                nested.append(
                    (
                        _codex_tool_output_details(item, strip_runtime_preamble=False),
                        True,
                    )
                )
        status_candidates = [
            item
            for item, status_candidate in nested
            if status_candidate and item.status is not None
        ]
        if status_candidates:
            status_priority = max(item.status_priority for item in status_candidates)
            statuses = [
                item.status
                for item in status_candidates
                if item.status_priority == status_priority
            ]
            status = all(statuses)
            status_explicit = status_priority >= 3
        else:
            status = None
            status_explicit = False
            status_priority = 0
        wall_time_ms = next(
            (item.wall_time_ms for item, _ in nested if item.wall_time_ms is not None),
            None,
        )
        return _CodexOutputDetails(
            "\n".join(item.text for item, _ in nested if item.text),
            status,
            status_explicit,
            status_priority,
            wall_time_ms,
        )
    if isinstance(value, dict):
        status = _codex_output_status(value)
        status_explicit = status is not None
        status_priority = 3 if status_explicit else 0
        if "output" in value:
            nested = _codex_tool_output_details(
                value.get("output"), strip_runtime_preamble=False
            )
            return _CodexOutputDetails(
                nested.text,
                status if status_explicit else nested.status,
                status_explicit or nested.status_explicit,
                status_priority if status_explicit else nested.status_priority,
                nested.wall_time_ms,
            )
        content = value.get("content")
        if content is not None:
            nested = _codex_tool_output_details(content, strip_runtime_preamble=False)
            return _CodexOutputDetails(
                nested.text,
                status if status_explicit else nested.status,
                status_explicit or nested.status_explicit,
                status_priority if status_explicit else nested.status_priority,
                nested.wall_time_ms,
            )
        error = value.get("error")
        if error is not None:
            return _CodexOutputDetails(
                str(error), False if status is None else status, True, 3, None
            )
        return _CodexOutputDetails(
            json.dumps(value), status, status_explicit, status_priority, None
        )
    return _CodexOutputDetails(str(value), None, False, 0, None)


def _codex_tool_output(value: object) -> tuple[str, bool | None]:
    details = _codex_tool_output_details(value)
    return details.text, details.status


def _codex_completion_timestamp(
    event: dict, ts: str | None, wall_time_ms: int | None
) -> str | None:
    """Use the runtime wall time only when no result timestamp exists."""
    if ts is not None:
        return ts
    existing = (event.get("tool") or {}).get("completed_at")
    if existing is not None or wall_time_ms is None:
        return existing
    started = event.get("ts")
    if not isinstance(started, str):
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    completed = start + timedelta(milliseconds=wall_time_ms)
    return completed.isoformat().replace("+00:00", "Z")


def _structured_edit_payload(name: object, raw_input: object) -> dict | None:
    """Keep bounded edit data needed by the transcript diff renderer."""
    tool_name = str(name or "").strip().lower()
    edit_names = {"edit", "multiedit", "notebookedit", "apply_patch"}
    if tool_name not in edit_names:
        return None

    def bounded_value(key: str, value: str) -> None:
        payload[key] = value[:MAX_EDIT_PAYLOAD]
        if len(value) > MAX_EDIT_PAYLOAD:
            payload[f"{key}_truncated"] = True

    if isinstance(raw_input, str):
        if tool_name == "apply_patch" and "*** Begin Patch" in raw_input:
            payload: dict = {}
            bounded_value("patch", raw_input)
            return payload
        return None
    if not isinstance(raw_input, dict):
        return None
    payload: dict = {}
    aliases = (
        ("file_path", "file_path", "filePath", "path"),
        ("old_string", "old_string", "oldString"),
        ("new_string", "new_string", "newString"),
    )
    for output_key, *keys in aliases:
        value = next((raw_input[key] for key in keys if key in raw_input), None)
        if isinstance(value, str):
            bounded_value(output_key, value)
    replace_all = raw_input.get("replace_all", raw_input.get("replaceAll"))
    if isinstance(replace_all, bool):
        payload["replace_all"] = replace_all
    if tool_name == "apply_patch":
        patch = raw_input.get("patch", raw_input.get("diff"))
        if isinstance(patch, str):
            bounded_value("patch", patch)
    return payload or None


def _codex_add_tool_event(
    state: dict,
    name: object,
    raw_input: object,
    ts: str | None,
    call_id: object,
) -> tuple[dict | None, bool]:
    """Add one Codex call and return (event, handled-as-artifact)."""
    pending: dict = state["pending"]
    harness = _codex_harness_tool(name, raw_input)
    if harness:
        name = harness["name"]
        tool_input = harness["input"]
        classify_input = harness["classify_input"]
        structured_input = harness["arguments"]
    else:
        if name == "exec" and isinstance(raw_input, str):
            tool_input = _codex_harness_fallback_input(raw_input)
        else:
            tool_input = _codex_tool_input(str(name or ""), raw_input)
        classify_input = tool_input
        structured_input = raw_input
    name = str(name or "")
    if _is_artifact_tool(name) and call_id:
        state.setdefault("pending_artifacts", {})[call_id] = {
            "name": name,
            "input": (
                harness.get("arguments")
                if harness and isinstance(harness.get("arguments"), dict)
                else _tool_arguments(raw_input)
            ) or {},
        }
        return None, True
    archetype, summary = classify_tool(name, classify_input)
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
    edit_payload = _structured_edit_payload(name, structured_input)
    if edit_payload is not None:
        event["tool"]["edit"] = edit_payload
    if harness and harness.get("batch"):
        event["tool"]["batch"] = harness["batch"]
    _append_event(state, event)
    if call_id:
        pending[call_id] = event
    return event, False


def _codex_finish_tool_event(
    state: dict,
    event: dict | None,
    call_id: object,
    output: object,
    ts: str | None,
) -> None:
    output_details = _codex_tool_output_details(output)
    output_text = output_details.text
    output_ok = output_details.status
    if call_id and _complete_artifact(state, call_id, output_text, ts):
        return
    target = state["pending"].pop(call_id, None) if call_id else event
    if target:
        target["tool"]["output"] = _clip(output_text, MAX_TOOL_IO)
        target["tool"]["ok"] = output_ok
        target["tool"]["completed_at"] = _codex_completion_timestamp(
            target, ts, output_details.wall_time_ms
        )
        _record_tool_patch(state, target)


def _codex_mcp_output(item: dict) -> object:
    error = item.get("error")
    if error is not None:
        return {"error": error, "isError": True}
    result = item.get("result")
    if isinstance(result, dict):
        output = dict(result)
    else:
        output = {"output": result}
    if not any(key in output for key in ("is_error", "isError", "failed", "exit_code")):
        output["isError"] = item.get("status") not in (None, "completed")
    return output


def _codex_apply_mcp_tool_item(state: dict, item: dict, ts: str | None) -> bool:
    if item.get("type") != "mcpToolCall":
        return False
    server = item.get("server")
    tool = item.get("tool")
    if not isinstance(server, str) or not isinstance(tool, str):
        return False
    name = f"mcp__{server}__{tool}"
    raw_input = item.get("arguments", item.get("input", {}))
    call_id = item.get("call_id", item.get("id"))
    event, handled = _codex_add_tool_event(state, name, raw_input, ts, call_id)
    if not handled and event is None:
        return False
    _codex_finish_tool_event(state, event, call_id, _codex_mcp_output(item), ts)
    return True


def _is_artifact_tool(name: object) -> bool:
    return isinstance(name, str) and name in {
        "render_artifact",
        "wiki_artifacts__render_artifact",
        "mcp__wiki_artifacts__render_artifact",
        "mcp__wiki-artifacts__render_artifact",
    }


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
    if kind in _STRUCTURED_BINARY_MIMES:
        normalized = result.get("artifact")
        if isinstance(normalized, dict):
            artifact = dict(normalized)
            if artifact.get("kind") != kind:
                return None
        else:
            # Older providers only returned an id for image and PDF results.
            # These kinds have no media metadata that the renderer needs.
            if kind not in {"image", "pdf"}:
                return None
            default_mime = PDF_MIME if kind == "pdf" else None
            mime = payload.get("mime", default_mime)
            if mime not in _STRUCTURED_BINARY_MIMES[kind]:
                return None
            artifact = {
                "kind": kind,
                "ref": f"artifact://{artifact_id}",
                "mime": mime,
            }
    elif kind == "visual-diff":
        normalized = result.get("artifact")
        if not isinstance(normalized, dict) or normalized.get("kind") != kind:
            return None
        artifact = dict(normalized)
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
    if protocol_event is not None and not _is_artifact_tool(meta.get("name")):
        protocol_event = None
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


def _codex_tool_end_output(
    ptype: str, payload: dict
) -> tuple[str, bool | None, int | None]:
    """patch_apply_end / mcp_tool_call_end carry the authoritative tool output
    the corresponding function_call/custom_tool_call left `output: null`."""
    if ptype == "patch_apply_end":
        parts = [payload.get("stdout") or "", payload.get("stderr") or ""]
        details = _codex_tool_output_details("\n".join(p for p in parts if p))
        return details.text, bool(payload.get("success")), details.wall_time_ms
    if ptype == "mcp_tool_call_end":
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            details = _codex_tool_output_details(result)
            return details.text, details.status, details.wall_time_ms
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
                details = _codex_tool_output_details(text or json.dumps(target))
                return details.text, ok, details.wall_time_ms
            details = _codex_tool_output_details(json.dumps(target))
            return details.text, ok, details.wall_time_ms
        details = _codex_tool_output_details(target if target is not None else result)
        return details.text, ok, details.wall_time_ms
    details = _codex_tool_output_details(payload)
    return details.text, details.status, details.wall_time_ms


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
            "completed_at": tool.get("completed_at"),
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
            out, ok, wall_time_ms = _codex_tool_end_output(ptype, payload)
            if _complete_artifact(state, call_id, str(out), ts, failed=ok is False):
                _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
                return
            event = pending.pop(call_id, None)
            if event:
                event["tool"]["output"] = _clip(str(out), MAX_TOOL_IO)
                event["tool"]["ok"] = ok
                event["tool"]["completed_at"] = _codex_completion_timestamp(
                    event, ts, wall_time_ms
                )
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
        elif ptype == "mcpToolCall":
            if _codex_apply_mcp_tool_item(state, payload, ts):
                _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
            else:
                _record_row_disposition(state, EVENT_DISPOSITION_UNKNOWN)
        elif ptype in ("function_call", "custom_tool_call", "web_search_call", "tool_search_call"):
            name = payload.get("name") or ptype.replace("_call", "")
            raw_input = payload.get("arguments", payload.get("input", payload.get("action", "")))
            call_id = payload.get("call_id")
            _codex_add_tool_event(state, name, raw_input, ts, call_id)
            _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
        elif ptype in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
            call_id = payload.get("call_id")
            output_details = _codex_tool_output_details(payload.get("output"))
            output_text = output_details.text
            output_ok = output_details.status
            if _complete_artifact(state, call_id, output_text, ts):
                _record_row_disposition(state, EVENT_DISPOSITION_RENDERED)
                return
            event = pending.pop(call_id, None)
            if event:
                event["tool"]["output"] = _clip(output_text, MAX_TOOL_IO)
                event["tool"]["ok"] = output_ok
                event["tool"]["completed_at"] = _codex_completion_timestamp(
                    event, ts, output_details.wall_time_ms
                )
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
        if isinstance(item, dict) and item.get("type") == "mcpToolCall":
            if _codex_apply_mcp_tool_item(state, item, ts):
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
                edit_payload = _structured_edit_payload(name, raw_input)
                if edit_payload is not None:
                    event["tool"]["edit"] = edit_payload
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
                event["tool"]["completed_at"] = ts
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
# ---------------------------------------------------------------------------
# Cache-layer concurrency model (WIKI-244 R8 — one design, all rules here).
#
# Locks:
#   _cache_locks_guard  — ONE short-hold registry mutex. Protects the
#                         STRUCTURE and entries of all three registries:
#                         _cache (path → parse state), _cache_locks
#                         (path → _PathLockEntry incl. refcounts), and
#                         _subagent_intros (child path → intro tuple).
#   _PathLockEntry.lock — one per-path critical section, entered via
#                         _path_lock(key). Covers the FULL read-modify-write
#                         span for that transcript: the parse state dict,
#                         its agent_child_assignments map, and any intro
#                         data consulted while annotating that path.
#
# Lock ordering:
#   A thread MAY take the guard while holding a path lock (guard is a leaf).
#   A thread MUST NEVER acquire a path lock while holding the guard —
#   _path_lock releases the guard before blocking on the path lock.
#   The guard is never held across file I/O or any blocking call.
#
# Escape rule:
#   No mutable registry object may be mutated after its lock is released.
#   Parse states and their agent_child_assignments are only mutated inside
#   _path_lock(main path) — including by annotate_agent_events, which holds
#   the path lock for its whole read-modify-write span. _subagent_intros
#   entries are immutable tuples; every compound operation on the map
#   (lookup+validate+delete, evict+insert) happens inside the guard, with
#   file reads OUTSIDE the guard and a re-stat validation before publish.
#
# Identity / reset / bound (per registry):
#   _cache            — states carry device+inode; identity change or shrink
#                       rebuilds (dropping the assignment map inside);
#                       LRU-bounded at _PARSE_CACHE_MAX.
#   _cache_locks      — refcounted; an entry is only evicted at refcount 0
#                       (refcounts increment under the guard BEFORE lock
#                       acquisition, so eviction can never split one path
#                       across two lock objects); orphan entries swept.
#   _subagent_intros  — entries carry device+inode+size; replacement or
#                       shrink reloads; insertion-order bounded.
#
# Eviction points:
#   1. On state creation (under the guard, inside _read_cached_state).
#   2. On path-lock RELEASE when the refcount falls to zero (under the
#      guard, inside _path_lock's finally) — so a concurrent burst of many
#      paths trims back to the bound as soon as holders drain, and the
#      just-released key itself is evictable. Entries with holders or
#      waiters (refs > 0) are never evicted.
#
# Endpoint transaction rule (R9):
#   An endpoint's parse + agent annotation is ONE path-lock span.
#   read_session_delta / read_older_session annotate their response window
#   in-span (annotate_agents=True) — annotation never runs on a snapshot
#   after the lock was released, so no eviction or same-path replacement
#   can interleave between read and annotate. When annotation finds a
#   fresh state (empty assignment map — new, rebuilt, or evicted-and-
#   rebuilt), it first reconstructs assignments from the state's FULL
#   retained parent sequence, bounded by the event-retention window, so
#   duplicate-prompt parents keep stable one-to-one children across
#   eviction and replacement.
# ---------------------------------------------------------------------------
_PARSE_CACHE_MAX = max(8, int(os.environ.get("WIKI_PARSE_CACHE_MAX", "64")))
_cache: dict[str, dict] = {}  # path → parse state (insertion order = LRU order)
_cache_locks: dict[str, "_PathLockEntry"] = {}
_cache_locks_guard = threading.Lock()  # guards _cache/_cache_locks STRUCTURE


class _PathLockEntry:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refs = 0


@contextmanager
def _path_lock(key: str):
    # Refcount under the guard BEFORE acquiring, so eviction (which only
    # removes entries with refs == 0) can never race a thread that is about
    # to acquire the lock it just looked up.
    with _cache_locks_guard:
        entry = _cache_locks.get(key)
        if entry is None:
            entry = _PathLockEntry()
            _cache_locks[key] = entry
        entry.refs += 1
    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with _cache_locks_guard:
            entry.refs -= 1
            if entry.refs == 0:
                # Eviction point 2: trim on release so concurrent bursts of
                # many distinct paths return to the bound once holders
                # drain; the just-released key itself is fair game.
                _evict_parse_states_locked(None)


def _evict_parse_states_locked(current_key: str | None) -> None:
    # Caller holds _cache_locks_guard. Evict least-recently-used states past
    # the cap, skipping the active key and any path whose lock is in use.
    for key in list(_cache):
        if len(_cache) <= _PARSE_CACHE_MAX:
            break
        if key == current_key:
            continue
        entry = _cache_locks.get(key)
        if entry is not None and entry.refs > 0:
            continue
        _cache.pop(key, None)
        _cache_locks.pop(key, None)
    # Prune orphaned lock entries whose state is gone (evicted while the lock
    # was held, or cleared externally) once nobody references them, so the
    # lock map is bounded by the state map plus in-flight readers.
    for key in list(_cache_locks):
        if key == current_key or key in _cache:
            continue
        entry = _cache_locks[key]
        if entry.refs > 0:
            continue
        del _cache_locks[key]


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
        # parent event id → child agent id (claude only); dies with the state
        # so stale links cannot outlive a transcript reset (WIKI-244).
        "agent_child_assignments": {},
        "cache_version": TRANSCRIPT_CACHE_VERSION,
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
    with _cache_locks_guard:
        state = _cache.get(key)
        if (
            state is None
            or state.get("cache_version") != TRANSCRIPT_CACHE_VERSION
            or stat.st_size < state["offset"]
            or state.get("ino") != stat.st_ino
            or state.get("dev") != stat.st_dev
        ):
            state = _new_parse_state(fmt)
            state["ino"] = stat.st_ino
            state["dev"] = stat.st_dev
            _cache.pop(key, None)
            _cache[key] = state
            _evict_parse_states_locked(key)
        else:
            # LRU touch: re-insertion moves the key to the end.
            _cache[key] = _cache.pop(key)
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


def _annotate_window_locked(main_path: Path, state: dict, window_events: list) -> list:
    """Annotate a response window inside the caller's path-lock span.

    Endpoint transaction rule: the caller holds _path_lock(main path) and
    `state` is the CURRENT parse state — no eviction or replacement can
    interleave. If the assignment map is empty (fresh, rebuilt, or
    evicted-and-rebuilt state), reconstruct it from the state's full
    retained parent sequence first, so windowed responses keep stable
    one-to-one children even for duplicate prompts without timestamps.
    """
    store = _agent_assignment_store(main_path)
    if not store and any(
        event.get("kind") == "tool"
        and (event.get("tool") or {}).get("name") in ("Agent", "Task")
        for event in state["events"]
    ):
        # Bounded reconstruction: the retained event window (<= 2000 events).
        _annotate_agent_events_locked(main_path, state["events"], store)
    return _annotate_agent_events_locked(main_path, window_events, store)


def read_session_events(fmt: str, path: Path) -> dict:
    """Returns {events, base, tokens, dirty_from}.

    base = absolute index of events[0] (grows when the buffer trims).
    dirty_from = absolute index of the oldest tool event still awaiting its
    output — everything at/after it can mutate on a later read, so delta
    consumers must re-fetch from min(cursor, dirty_from).
    """
    key = str(path)
    with _path_lock(key):
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
    tail_events: int | None = None,
    annotate_agents: bool = False,
) -> dict:
    key = str(path)
    with _path_lock(key):
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
            # An explicit tail_events bound (e.g. the inline sub-agent trace)
            # overrides the default policy in both directions.
            if tail_events is not None:
                window_base = max(base, total - tail_events)
            else:
                window_base = max(base, total - TAIL_WINDOW_EVENTS) if tail_window else base
            # Snapshot only the event objects and mutable nested leaves;
            # avoid a recursive clone of large immutable payloads.
            reset_events = _snapshot_events(events[window_base - base :])
            if annotate_agents:
                reset_events = _annotate_window_locked(path, state, reset_events)
            return {
                "events": reset_events,
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
            rewind_events = _snapshot_events(events)
            if annotate_agents:
                rewind_events = _annotate_window_locked(path, state, rewind_events)
            return {
                "events": rewind_events,
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

        # WIKI-244 R5 (M2): a stale cursor (unmounted row, sleeping tab) can
        # trail by far more than the inline bound — clip the incremental tail
        # to the newest tail_events events, advance tail_from to the first
        # returned event, and mark the omitted middle as older history.
        # Patches referring to clipped events are dropped with them; clients
        # apply patches by event id, so patches for events they never held
        # are no-ops either way.
        clipped = False
        if (
            tail_events is not None
            and tail_from < total
            and total - tail_from > tail_events
        ):
            tail_from = total - tail_events
            clipped = True

        if tail_from < total:
            tail_slice = deepcopy(events[tail_from - base :])
            patch_map = {
                event_id: entry for event_id, entry in patch_map.items() if int(entry.get("index", total)) < tail_from
            }
        else:
            tail_slice = []

        patches = [
            {
                "id": int(entry["id"]),
                "output": entry.get("output"),
                "ok": entry.get("ok"),
                "completed_at": entry.get("completed_at"),
            }
            for entry in sorted(patch_map.values(), key=lambda item: int(item["cursor"]))
        ]
        if annotate_agents and tail_slice:
            tail_slice = _annotate_window_locked(path, state, tail_slice)
        payload = {
            "events": tail_slice,
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
        if clipped:
            payload["has_older"] = True
        return payload


def read_older_session(
    fmt: str,
    path: Path,
    before: int,
    count: int,
    *,
    annotate_agents: bool = False,
) -> dict:
    """Return the retained events immediately before an absolute event index."""
    key = str(path)
    with _path_lock(key):
        state = _read_cached_state(fmt, path, key)
        base = int(state["base"])
        events = state["events"]
        total = base + len(events)
        end = min(max(before, base), total)
        start = max(base, end - max(1, count))
        page_events = _snapshot_events(events[start - base : end - base])
        if annotate_agents:
            page_events = _annotate_window_locked(path, state, page_events)
        return {
            "events": page_events,
            "base": start,
            "has_older": start > base,
        }


# ---------------------------------------------------------------- claude subagents

# file path → (head, started_at, inode, device, size-at-read). The intro is
# only immutable while the SAME file grows in place — a replaced inode or a
# shrunken file means a different child transcript now lives at that path,
# so cached intros carry a stat fingerprint and reload on mismatch
# (WIKI-244 R6 M3). Bounded so long-lived backends do not accumulate one
# entry per child file forever.
_SUBAGENT_INTRO_CACHE_MAX = 2048
_subagent_intros: dict[str, tuple[str, str, int, int, int]] = {}


def subagents_dir(main_path: Path) -> Path:
    return main_path.parent / main_path.stem / "subagents"


def _read_intro_rows(path: Path) -> tuple[str, str]:
    head = ""
    started_at = ""
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
                if not started_at and isinstance(row.get("timestamp"), str):
                    started_at = row["timestamp"]
                if row.get("type") == "user":
                    content = (row.get("message") or {}).get("content")
                    if isinstance(content, str):
                        head = content[:120]
                    break
    except OSError:
        pass
    return head, started_at


def _subagent_intro(path: Path, stat: os.stat_result | None = None) -> tuple[str, str]:
    key = str(path)
    if stat is None:
        try:
            stat = path.stat()
        except OSError:
            return "", ""
    # Compound lookup+validate+delete under the guard (concurrency model):
    # two readers racing a replacement both take this section in turn; the
    # loser sees the entry already gone and simply falls through to re-read.
    with _cache_locks_guard:
        cached = _subagent_intros.get(key)
        if cached is not None:
            head, started_at, ino, dev, size = cached
            # Same inode growing (or unchanged) in place → the first rows
            # cannot have changed. New inode or shrink = replacement.
            if ino == stat.st_ino and dev == stat.st_dev and stat.st_size >= size:
                return head, started_at
            _subagent_intros.pop(key, None)
    # File I/O outside the guard.
    head, started_at = _read_intro_rows(path)
    if head:
        # Revalidate before publish: if the file changed identity while we
        # read it, our rows may belong to neither generation — return them
        # to this caller but do not publish to the cache.
        try:
            fresh = path.stat()
        except OSError:
            return head, started_at
        if fresh.st_ino != stat.st_ino or fresh.st_dev != stat.st_dev or fresh.st_size < stat.st_size:
            return head, started_at
        with _cache_locks_guard:
            while len(_subagent_intros) >= _SUBAGENT_INTRO_CACHE_MAX:
                _subagent_intros.pop(next(iter(_subagent_intros)))
            _subagent_intros[key] = (head, started_at, stat.st_ino, stat.st_dev, stat.st_size)
    return head, started_at


def _subagent_prompt_head(path: Path) -> str:
    return _subagent_intro(path)[0]


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
        head, started_at = _subagent_intro(path, stat)
        entries.append(
            {
                "id": path.stem.removeprefix("agent-"),
                "prompt_head": head,
                "started_at": started_at,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )
    return entries


def _agent_assignment_store(main_path: Path) -> dict:
    """Parent-id → child-id map scoped to the transcript's parse state.

    Living inside the parse-state dict means the map dies with the state:
    a file shrink, reload, or same-path replacement rebuilds the state and
    drops every assignment. Entries below the retained event base are
    pruned. Callers without parse state get an ephemeral map.

    Concurrency model: the returned dict belongs to the parse state — the
    CALLER must hold _path_lock(main path) for the whole span in which it
    reads or mutates the map (annotate_agent_events does). This helper only
    takes the registry guard for the structural lookup/prune.
    """
    key = str(main_path)
    with _cache_locks_guard:
        state = _cache.get(key)
        if state is None:
            return {}
        store = state.setdefault("agent_child_assignments", {})
        base = int(state.get("base", 0))
        if store and base:
            for event_id in [event_id for event_id in store if event_id < base]:
                del store[event_id]
        return store


def annotate_agent_events(main_path: Path, events: list, assignments: dict | None = None) -> list:
    """Attach subagent ids without mutating parser-owned cached events.

    Parents and children correlate by prompt head. Duplicate heads (retries,
    repeated exploration prompts) resolve deterministically and independently
    of the delta window that carries the parent:

    - assignments persist per parent event id (stable absolute index) inside
      the transcript's parse state, so a parent seen again in a later window
      keeps its child, while a parse-state reset (file shrink, reload) drops
      the whole map;
    - a cached assignment is re-validated against the current child list —
      if the child vanished or its prompt head no longer matches the parent,
      the stale link is dropped and re-resolved;
    - a new parent claims the first child (by start time) that no other
      parent holds AND that started at/after the parent's own timestamp —
      the timestamp anchor makes the choice identical whether duplicate
      parents arrive in one response or split across many;
    - a parent with no claimable child stays unannotated rather than reusing
      another parent's child.
    """
    if assignments is None:
        # Concurrency model: the state-backed assignment map may only be
        # read or mutated inside the path's critical section — hold it for
        # the WHOLE read-modify-write span so two concurrent annotations
        # cannot hand the same child to two different parents.
        with _path_lock(str(main_path)):
            return _annotate_agent_events_locked(
                main_path, events, _agent_assignment_store(main_path)
            )
    return _annotate_agent_events_locked(main_path, events, assignments)


def _annotate_agent_events_locked(main_path: Path, events: list, assignments: dict) -> list:
    heads: dict[str, list[dict]] | None = None
    children_by_id: dict[str, dict] = {}
    annotated = events
    for index, event in enumerate(events):
        tool = event.get("tool")
        if not tool or tool.get("name") not in ("Agent", "Task") or tool.get("agent_id"):
            continue
        prompt_head = tool.get("prompt_head")
        if not prompt_head:
            continue
        event_id = event.get("id")
        if heads is None:
            heads = {}
            children = sorted(
                (entry for entry in list_subagents(main_path) if entry["prompt_head"]),
                key=lambda entry: (entry.get("started_at") or "", entry.get("mtime") or 0),
            )
            for child in children:
                heads.setdefault(child["prompt_head"], []).append(child)
                children_by_id[child["id"]] = child
        chosen = assignments.get(event_id) if isinstance(event_id, int) else None
        if chosen is not None:
            cached_child = children_by_id.get(chosen)
            if cached_child is None or cached_child.get("prompt_head") != prompt_head:
                # Stale link: the child vanished or the parent at this id now
                # carries a different prompt (path or id reuse). Re-resolve.
                del assignments[event_id]
                chosen = None
        if chosen is None:
            candidates = heads.get(prompt_head) or []
            taken_ids = set(assignments.values())
            parent_ts = event.get("ts") or ""
            chosen = next(
                (
                    child["id"]
                    for child in candidates
                    if child["id"] not in taken_ids
                    and (not parent_ts or not child.get("started_at") or child["started_at"] >= parent_ts)
                ),
                None,
            )
            if chosen is None:
                # Clock skew fallback: no child started at/after the parent —
                # take the first unclaimed child rather than dropping the link.
                chosen = next(
                    (child["id"] for child in candidates if child["id"] not in taken_ids),
                    None,
                )
            if chosen is None:
                continue
            if isinstance(event_id, int):
                assignments[event_id] = chosen
        if annotated is events:
            annotated = list(events)
        annotated[index] = {
            **event,
            "tool": {**tool, "agent_id": chosen},
        }
    return annotated
