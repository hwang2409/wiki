"""Native CLI session transcripts (codex/claude JSONL) → normalized event stream.

Formats are unversioned internals — parsers are defensive, unknown rows are skipped.
Normalized event:
  {"kind": "user"|"assistant"|"thinking"|"tool", "ts": str|None, "text": str,
   "tool": {"name", "input", "output", "ok"} (kind=tool only)}
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
KICKOFF_TICKET_PATTERN = re.compile(r"Linear ticket ([A-Z]+-\d+)\b")

MAX_TEXT = 80_000
MAX_TOOL_IO = 3_000

TRANSCRIPT_IMAGE_DIR = Path("/tmp/wiki-transcript-images")

_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


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


def _codex_session_cwd(path: Path) -> str | None:
    """cwd from the session_meta line (first row of every rollout)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            row = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    payload = row.get("payload") or {}
    return payload.get("cwd")


def find_codex_session(ticket: str, spawned_at: str | None) -> Path | None:
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
    slug = ticket.lower()
    matches = [
        p
        for p in candidates
        if _codex_kickoff_ticket(p) == ticket
        or PurePosixPath(_codex_session_cwd(p) or "").name == slug
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


def find_claude_session(ticket: str, spawned_at: str | None) -> Path | None:
    """Worktree project dirs embed the ticket slug; kickoff prompt disambiguates the rest."""
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


def find_session(kind: str | None, ticket: str, spawned_at: str | None) -> tuple[str, Path] | None:
    if kind == "cc":
        path = find_claude_session(ticket, spawned_at)
        return ("claude", path) if path else None
    if kind == "cdx":
        path = find_codex_session(ticket, spawned_at)
        return ("codex", path) if path else None
    for fmt, finder in (("codex", find_codex_session), ("claude", find_claude_session)):
        path = finder(ticket, spawned_at)
        if path:
            return (fmt, path)
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


def _codex_apply(state: dict, row: dict) -> None:
    events: list = state["events"]
    pending: dict = state["pending"]  # call_id → event (awaiting output)
    ts = row.get("timestamp")
    rtype = row.get("type")
    payload = row.get("payload") or {}
    ptype = payload.get("type")

    if rtype == "event_msg":
        if ptype == "user_message":
            events.append({"kind": "user", "ts": ts, "text": _clip(payload.get("message") or "", MAX_TEXT)})
        elif ptype == "agent_message":
            events.append({"kind": "assistant", "ts": ts, "text": _clip(payload.get("message") or "", MAX_TEXT)})
        elif ptype == "token_count":
            info = payload.get("info") or {}
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            if total:
                state["tokens"] = total
        elif ptype == "context_compacted":
            events.append({"kind": "thinking", "ts": ts, "text": "context compacted"})
    elif rtype == "response_item":
        if ptype == "reasoning":
            summary = payload.get("summary") or []
            text = " ".join(
                s.get("text", "") for s in summary if isinstance(s, dict)
            ).strip()
            events.append({"kind": "thinking", "ts": ts, "text": _clip(text, MAX_TEXT)})
        elif ptype in ("function_call", "custom_tool_call", "web_search_call", "tool_search_call"):
            name = payload.get("name") or ptype.replace("_call", "")
            raw_input = payload.get("arguments", payload.get("input", payload.get("action", "")))
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
            events.append(event)
            call_id = payload.get("call_id")
            if call_id:
                pending[call_id] = event
        elif ptype in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
            event = pending.pop(payload.get("call_id"), None)
            if event:
                output = payload.get("output")
                if isinstance(output, dict):
                    output = output.get("content") or json.dumps(output)
                event["tool"]["output"] = _clip(str(output or ""), MAX_TOOL_IO)
                event["tool"]["ok"] = "exited with code 0" in str(output or "") or None


# ---------------------------------------------------------------- claude parser


def _xml_tag(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", text)
    return match.group(1).strip() if match else None


def _claude_user_event(text: str, ts: str | None) -> dict | None:
    stripped = text.strip()
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
    parent = row.get("parentUuid")
    prev = state.get("last_user")
    if (
        event["kind"] == "user"
        and prev
        and parent
        and prev["parent"] == parent
        and prev["index"] == len(events) - 1
    ):
        events[-1] = event
        state["tail_replaced"] = True
    else:
        events.append(event)
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
    events: list = state["events"]
    pending: dict = state["pending"]  # tool_use id → event
    rtype = row.get("type")
    if rtype not in ("user", "assistant"):
        return
    if row.get("isSidechain") and not state.get("sidechain_ok"):
        return
    ts = row.get("timestamp")
    message = row.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        if rtype == "user" and content.strip():
            for event in _claude_user_events(content, ts):
                _append_claude_user(state, event, row)
        return
    if not isinstance(content, list):
        return
    if rtype == "user" and any(
        isinstance(b, dict) and b.get("type") in ("text", "image") for b in content
    ):
        assembled = _assemble_user_content(content)
        if assembled.strip():
            for event in _claude_user_events(assembled, ts):
                _append_claude_user(state, event, row)
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
                    events.append({"kind": "assistant", "ts": ts, "text": _clip(text, MAX_TEXT)})
        elif btype == "thinking":
            events.append({"kind": "thinking", "ts": ts, "text": _clip(block.get("thinking") or "", MAX_TEXT)})

        elif btype == "tool_use":
            name = block.get("name") or "tool"
            raw_input = block.get("input")
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
            events.append(event)
            if block.get("id"):
                pending[block["id"]] = event
        elif btype == "tool_result":
            event = pending.pop(block.get("tool_use_id"), None)
            if event:
                result = block.get("content")
                if isinstance(result, list):
                    result = "\n".join(
                        b.get("text", "") for b in result if isinstance(b, dict) and b.get("type") == "text"
                    )
                event["tool"]["output"] = _clip(str(result or ""), MAX_TOOL_IO)
                event["tool"]["ok"] = not block.get("is_error")


# ---------------------------------------------------------------- incremental cache

_APPLY = {"codex": _codex_apply, "claude": _claude_apply, "claude-sub": _claude_apply}
_cache: dict[str, dict] = {}  # path → parse state; wiped on reload, rebuilt lazily


def read_session_events(fmt: str, path: Path) -> dict:
    """Returns {events, base, tokens, dirty_from}.

    base = absolute index of events[0] (grows when the buffer trims).
    dirty_from = absolute index of the oldest tool event still awaiting its
    output — everything at/after it can mutate on a later read, so delta
    consumers must re-fetch from min(cursor, dirty_from).
    """
    key = str(path)
    stat = path.stat()
    state = _cache.get(key)
    if state is None or stat.st_size < state["offset"]:
        state = {
            "offset": 0,
            "buffer": "",
            "events": [],
            "pending": {},
            "tokens": None,
            "base": 0,
            "last_user": None,
            "tail_replaced": False,
            "sidechain_ok": fmt == "claude-sub",
        }
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
            state["base"] += len(state["events"]) - 2000
            state["events"] = state["events"][-2000:]
            kept = set(map(id, state["events"]))
            state["pending"] = {
                call_id: event for call_id, event in state["pending"].items() if id(event) in kept
            }
    total = state["base"] + len(state["events"])
    dirty_from = total
    pending_events = set(map(id, state["pending"].values()))
    if pending_events:
        for i, event in enumerate(state["events"]):
            if id(event) in pending_events:
                dirty_from = state["base"] + i
                break
    if state.get("tail_replaced") and state["events"]:
        dirty_from = min(dirty_from, total - 1)
        state["tail_replaced"] = False
    return {
        "events": state["events"],
        "base": state["base"],
        "tokens": state["tokens"],
        "dirty_from": dirty_from,
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


def annotate_agent_events(main_path: Path, events: list) -> None:
    """Attach subagent ids to Agent tool events by prompt-head match."""
    heads: dict[str, str] | None = None
    for event in events:
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
            tool["agent_id"] = agent_id
