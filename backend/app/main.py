from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import accounts, github_pr, tokens, transcripts, uistate, vaultops
from .frontend_static import mount_frontend_static


ROOT_DIR = Path(os.environ.get("WIKI_REPO_DIR", Path(__file__).resolve().parents[2])).resolve()
VAULT_DIR = Path(os.environ.get("WIKI_VAULT_DIR", ROOT_DIR / "vault")).resolve()
MAX_NOTE_BYTES = 2_000_000

VAULT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Wiki API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class NoteSummary(BaseModel):
    id: str
    path: str
    title: str
    excerpt: str
    updated_at: datetime
    note_type: str | None = None
    meta_updated: str | None = None


class Note(NoteSummary):
    content: str


class NoteCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=160)
    path: str | None = Field(default=None, max_length=260)
    content: str = Field(default="", max_length=MAX_NOTE_BYTES)


class NoteUpdate(BaseModel):
    content: str = Field(..., max_length=MAX_NOTE_BYTES)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "untitled"


def normalize_note_path(raw_path: str | None, *, fallback_title: str | None = None) -> str:
    candidate = (raw_path or "").strip().replace("\\", "/")
    if not candidate:
        if not fallback_title:
            raise HTTPException(status_code=400, detail="Note path is required")
        candidate = slugify(fallback_title)

    if candidate.endswith("/"):
        candidate = f"{candidate}{slugify(fallback_title or 'untitled')}"

    path = PurePosixPath(candidate)
    if path.is_absolute():
        raise HTTPException(status_code=400, detail="Note paths must be relative")

    if path.suffix.lower() != ".md":
        path = path.with_suffix(".md")

    unsafe_parts = {"", ".", ".."}
    if any(part in unsafe_parts for part in path.parts):
        raise HTTPException(status_code=400, detail="Note path contains unsafe segments")

    if any(part.startswith(".") for part in path.parts):
        raise HTTPException(status_code=400, detail="Hidden note paths are not supported")

    return path.as_posix()


def resolve_note_path(note_path: str) -> Path:
    normalized = normalize_note_path(note_path)
    target = (VAULT_DIR / normalized).resolve()
    try:
        target.relative_to(VAULT_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Note path escapes the vault") from exc

    return target


def unique_note_path(raw_path: str | None, title: str) -> str:
    normalized = normalize_note_path(raw_path, fallback_title=title)
    target = resolve_note_path(normalized)
    if not target.exists():
        return normalized

    parent = PurePosixPath(normalized).parent
    stem = PurePosixPath(normalized).stem
    for index in range(2, 1000):
        candidate_name = f"{stem}-{index}.md"
        candidate = candidate_name if parent.as_posix() == "." else f"{parent.as_posix()}/{candidate_name}"
        if not resolve_note_path(candidate).exists():
            return candidate

    raise HTTPException(status_code=409, detail="Could not find an available note path")


def read_note(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="Note is not valid UTF-8") from exc


def note_id_for(path: Path) -> str:
    return path.relative_to(VAULT_DIR).as_posix()


def extract_title(content: str, fallback_path: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            if title:
                return title

    fallback = Path(fallback_path).stem.replace("-", " ").replace("_", " ").strip()
    return fallback.title() if fallback else "Untitled"


def excerpt_for(content: str) -> str:
    content = re.sub(r"\n*```wiki-styles\n[\s\S]*?\n```\s*$", "", content, flags=re.IGNORECASE)
    content = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", content)
    content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    text = " ".join(lines)
    text = re.sub(r"[>*_`#[\]()!]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 180:
        return f"{text[:177].rstrip()}..."
    return text


def updated_at_for(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def frontmatter_fields(content: str) -> dict[str, str]:
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return {}


def to_summary(path: Path) -> NoteSummary:
    content = read_note(path)
    note_id = note_id_for(path)
    fields = frontmatter_fields(content)
    return NoteSummary(
        id=note_id,
        path=note_id,
        title=extract_title(content, note_id),
        excerpt=excerpt_for(content),
        updated_at=updated_at_for(path),
        note_type=fields.get("type"),
        meta_updated=fields.get("updated"),
    )


def to_note(path: Path) -> Note:
    summary = to_summary(path)
    return Note(**summary.model_dump(), content=read_note(path))


def iter_note_files() -> list[Path]:
    files: list[Path] = []
    for path in VAULT_DIR.rglob("*.md"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(VAULT_DIR)
        except ValueError:
            continue
        files.append(resolved)

    return sorted(files, key=lambda note: note.stat().st_mtime, reverse=True)


def normalize_content(title: str, content: str) -> str:
    body = content.rstrip()
    if not body:
        body = f"# {title.strip()}\n\n"
    elif not body.lstrip().startswith(("#", "---")):
        body = f"# {title.strip()}\n\n{body}\n"
    else:
        body = f"{body}\n"

    if len(body.encode("utf-8")) > MAX_NOTE_BYTES:
        raise HTTPException(status_code=413, detail="Note is too large")

    return body


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


WIKILINK_PATTERN = re.compile(r"\[\[([^\][|\n]+?)(?:\|[^\][\n]*)?\]\]")
CODE_FENCE_PATTERN = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
INLINE_CODE_PATTERN = re.compile(r"`[^`\n]*`")
SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


def strip_code(content: str) -> str:
    """Remove fenced blocks and inline code so quoted link syntax isn't indexed."""
    return INLINE_CODE_PATTERN.sub("", CODE_FENCE_PATTERN.sub("", content))


class ActivityFile(BaseModel):
    path: str
    status: str


class ActivityCommit(BaseModel):
    sha: str
    date: str
    message: str
    files: list[ActivityFile]


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT_DIR), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"git failed: {result.stderr.strip()[:200]}")
    return result.stdout


@app.get("/api/activity", response_model=list[ActivityCommit])
def activity(limit: int = 50) -> list[ActivityCommit]:
    limit = max(1, min(limit, 200))
    out = run_git(
        "log",
        f"-{limit}",
        "--pretty=%x1e%H%x1f%aI%x1f%s",
        "--name-status",
        "--",
        "vault",
    )

    commits: list[ActivityCommit] = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, body = chunk.partition("\n")
        try:
            sha, date, message = head.split("\x1f")
        except ValueError:
            continue
        files: list[ActivityFile] = []
        for line in body.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                files.append(ActivityFile(path=parts[-1], status=parts[0][:1]))
        commits.append(ActivityCommit(sha=sha, date=date, message=message, files=files))
    return commits


@app.get("/api/activity/{sha}")
def activity_diff(sha: str) -> dict[str, str]:
    if not SHA_PATTERN.fullmatch(sha):
        raise HTTPException(status_code=400, detail="Bad commit sha")
    patch = run_git("show", sha, "--format=%s", "--patch", "--", "vault")
    return {"patch": patch}


class NoteLinks(BaseModel):
    outgoing: list[str]
    incoming: list[str]
    unresolved: list[str]


AGENT_REGISTRY_PATH = Path("/tmp/agent-registry.json")
AGENT_STATUS_DIR = Path("/tmp/agent-status")
AGENT_ARCHIVE_DIR = Path.home() / "me" / "fun" / "agent-archive"
ARCHIVE_TS_PATTERN = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$")
ANSI_PATTERN = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI incl. space-intermediate forms (e.g. ESC[0 q)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-_]"  # bare two-char escapes
)
# Cursor jumps back to column start = same redraw semantics as \r.
CURSOR_JUMP_PATTERN = re.compile(r"\x1b\[\d*[GD]")
TICKET_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
SPAWN_TICKET_PATTERN = re.compile(r"^[A-Z0-9-]+$")
ORCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
CDX_MODELS = {"gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"}
CC_MODELS = {"opus", "sonnet"}
WORKER_ROLES = {"plan", "implement", "review"}
REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
MAX_SPAWN_PROMPT_BYTES = 100_000
MAX_ORCH_GOAL_BYTES = 20_000
ORCH_KICKOFF_TEMPLATE = """FIRST, self-register this Claude Code session before anything else:
`~/me/fun/wiki/wiki agent orch {orch_id} --window "$(tmux display-message -p -t "$TMUX_PANE" '#{{window_id}}')"`

You are the MASTERMIND ORCHESTRATOR for this project per the `tmux-ticket-codex` / `tmux-ticket-claude` skills.
Your job is to spawn tmux workers for bounded tickets/tasks, monitor their status files and pane logs, steer them back on track, and independently gate PRs before merge.
Every worker you spawn must register with `--orch {orch_id}` so the wiki can map them back to you.

Before spawning anything, read this protocol note in full:
`~/me/fun/wiki/vault/tools/orchestrator-worker-protocol.md`

After you self-register and read the protocol:
{goal_instruction}
"""


def tmux_live_windows() -> set[str]:
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", "#{window_id}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.split("\n") if line.strip()}
    except (OSError, subprocess.TimeoutExpired):
        return set()


def tmux_has_window_named(name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", "#{window_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        return any(line.strip() == name for line in result.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_agent_status(ticket: str) -> dict | None:
    path = AGENT_STATUS_DIR / f"{ticket}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_mtime"] = path.stat().st_mtime
        return data
    except (OSError, ValueError):
        return None


def _archive_role(session_dir: Path) -> str | None:
    prompts = list(session_dir.glob("*-prompt.md"))
    if not prompts:
        return None
    try:
        head = prompts[0].read_text(encoding="utf-8", errors="replace")[:300]
    except OSError:
        return None
    if "PLANNING worker" in head:
        return "plan"
    if "IMPLEMENTATION worker" in head or "autonomous worker" in head:
        return "implement"
    return None


def _archive_sessions(ticket_dir: Path) -> list[tuple[datetime, Path]]:
    sessions = []
    for session_dir in ticket_dir.iterdir():
        match = ARCHIVE_TS_PATTERN.fullmatch(session_dir.name)
        if not session_dir.is_dir() or not match:
            continue
        y, mo, d, h, mi, s = map(int, match.groups())
        sessions.append((datetime(y, mo, d, h, mi, s).astimezone(), session_dir))
    return sorted(sessions, reverse=True)


def list_archived(limit: int = 20) -> list[dict]:
    if not AGENT_ARCHIVE_DIR.is_dir():
        return []
    entries = []
    for ticket_dir in AGENT_ARCHIVE_DIR.iterdir():
        if not ticket_dir.is_dir() or not TICKET_PATTERN.fullmatch(ticket_dir.name):
            continue
        for archived_at, session_dir in _archive_sessions(ticket_dir):
            status = {}
            try:
                status = json.loads((session_dir / "final-status.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            meta = {}
            try:
                meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            worker = meta.get("worker") or {}
            kind = worker.get("kind")
            if not kind:
                if any(session_dir.glob("cdx-*")):
                    kind = "cdx"
                elif any(session_dir.glob("cc-*")):
                    kind = "cc"
            entries.append(
                {
                    "ticket": ticket_dir.name,
                    "archived_at": archived_at.isoformat(),
                    "kind": kind,
                    "role": worker.get("role") or _archive_role(session_dir),
                    "model": worker.get("model"),
                    "outcome": meta.get("outcome"),
                    "state": status.get("state"),
                    "pr": status.get("pr"),
                    "step": status.get("step"),
                }
            )
    entries.sort(key=lambda e: e["archived_at"], reverse=True)
    return entries[:limit]


def _archive_hint(ticket: str) -> tuple[str | None, str | None, Path | None]:
    """(kind, spawned_at-ish iso, session dir) from the newest archive of a ticket."""
    ticket_dir = AGENT_ARCHIVE_DIR / ticket
    if not ticket_dir.is_dir():
        return (None, None, None)
    sessions = _archive_sessions(ticket_dir)
    if not sessions:
        return (None, None, None)
    archived_at, session_dir = sessions[0]
    kind = "cdx" if any(session_dir.glob("cdx-*")) else "cc" if any(session_dir.glob("cc-*")) else None
    return (kind, archived_at.isoformat(), session_dir)


@app.get("/api/agents")
def agents() -> dict[str, object]:
    registry: dict = {}
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass

    live_windows = tmux_live_windows()
    now = datetime.now(tz=timezone.utc).timestamp()
    workers = []
    seen_tickets = set()

    for ticket, entry in sorted(registry.items()):
        if ticket.startswith("_"):
            continue
        current = entry.get("current") or {}
        status = read_agent_status(ticket)
        seen_tickets.add(ticket)
        workers.append(
            {
                "ticket": ticket,
                "registered": True,
                "window": current.get("window"),
                "window_alive": current.get("window") in live_windows,
                "kind": current.get("kind"),
                "role": current.get("role"),
                "model": current.get("model"),
                "worktree": current.get("worktree"),
                "log": current.get("log"),
                "orch": current.get("orch"),
                "session": current.get("session"),
                "spawned_at": current.get("spawned_at"),
                "history": entry.get("history", []),
                "state": (status or {}).get("state"),
                "pr": (status or {}).get("pr"),
                "step": (status or {}).get("step"),
                "blocker": (status or {}).get("blocker"),
                "status_age_seconds": (
                    int(now - status["_mtime"]) if status else None
                ),
            }
        )

    # Status files without a registry entry — skill drift, surface flagged.
    if AGENT_STATUS_DIR.is_dir():
        for path in sorted(AGENT_STATUS_DIR.glob("*.json")):
            ticket = path.stem
            if ticket in seen_tickets:
                continue
            status = read_agent_status(ticket)
            workers.append(
                {
                    "ticket": ticket,
                    "registered": False,
                    "window": None,
                    "window_alive": False,
                    "kind": None,
                    "role": None,
                    "model": None,
                    "worktree": None,
                    "log": None,
                    "orch": None,
                    "session": None,
                    "spawned_at": None,
                    "history": [],
                    "state": (status or {}).get("state"),
                    "pr": (status or {}).get("pr"),
                    "step": (status or {}).get("step"),
                    "blocker": (status or {}).get("blocker"),
                    "status_age_seconds": (
                        int(now - status["_mtime"]) if status else None
                    ),
                }
            )

    orchestrators = []
    for orch_id, orch in sorted((registry.get("_orchestrators") or {}).items()):
        transcript = orch.get("transcript")
        orchestrators.append(
            {
                "id": orch_id,
                "window": orch.get("window"),
                "window_alive": orch.get("window") in live_windows,
                "cwd": orch.get("cwd"),
                "spawned_at": orch.get("spawned_at"),
                "transcript_exists": bool(transcript and Path(transcript).is_file()),
            }
        )

    return {"workers": workers, "orchestrators": orchestrators, "archived": list_archived()}


@app.get("/api/agents/{ticket}/pr")
def agent_pr(ticket: str) -> dict[str, object]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    return github_pr.get_pr_payload(ticket)


@app.post("/api/agents/{ticket}/pr/approve")
def agent_pr_approve(ticket: str) -> dict[str, str]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    return github_pr.approve_pr(ticket)


def capture_pane_tail(window: str, lines: int) -> str | None:
    """Rendered pane text via tmux — tmux resolves cursor-jump/CR redraws itself."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", window],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    cleaned: list[str] = []
    blank_run = 0
    for line in result.stdout.split("\n"):
        line = line.rstrip()
        if not line:
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        cleaned.append(line)
    return "\n".join(cleaned).strip("\n")


@app.get("/api/agents/{ticket}/log")
def agent_log(ticket: str, lines: int = 200) -> dict[str, str]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    lines = max(10, min(lines, 1000))

    registry: dict = {}
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass

    current = (registry.get(ticket) or {}).get("current") or {}
    window = current.get("window")
    if window and re.fullmatch(r"@\d+", window) and window in tmux_live_windows():
        tail = capture_pane_tail(window, lines)
        if tail is not None:
            return {"path": f"tmux {window}", "tail": tail}

    candidates: list[Path] = []
    log_hint = current.get("log")
    if log_hint:
        candidates.append(Path(log_hint))
    for prefix in ("cdx", "cc"):
        candidates.extend(Path("/tmp").glob(f"{prefix}-{ticket}*.log"))

    existing = [p for p in candidates if p.is_file()]
    if not existing:
        raise HTTPException(status_code=404, detail="No log found")
    newest = max(existing, key=lambda p: p.stat().st_mtime)

    return {"path": str(newest), "tail": clean_pane_log(newest, lines)}


def clean_pane_log(path: Path, lines: int = 500) -> str:
    """ANSI/CR cleanup of a raw pipe-pane log, tail-bytes read (logs reach 50MB+)."""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 400_000))
        raw = f.read().decode("utf-8", errors="replace")
    text = ANSI_PATTERN.sub("", CURSOR_JUMP_PATTERN.sub("\r", raw))
    cleaned: list[str] = []
    blank_run = 0
    for line in text.split("\n"):
        # Carriage-return redraws: the last segment is what the terminal shows.
        line = line.split("\r")[-1].rstrip()
        # TUI redraw junk: lines that are only box-drawing/separator glyphs.
        if line and not re.search(r"[\w#>$\[\]()]", line):
            continue
        if not line:
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        cleaned.append(line)
    return "\n".join(cleaned[-lines:]).strip("\n")


_session_paths: dict[str, tuple[str, Path]] = {}  # ticket → (fmt, path), successes only


@app.get("/api/agents/{ticket}/session")
def agent_session(ticket: str, after: int = 0) -> dict[str, object]:
    """Delta protocol: `after` = client's absolute event cursor. Response events
    start at `from` = min(after, oldest-still-mutating event); client splices."""
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")

    registry: dict = {}
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    orch = (registry.get("_orchestrators") or {}).get(ticket)
    if orch and orch.get("transcript"):
        path = Path(orch["transcript"])
        if path.is_file():
            result = transcripts.read_session_events("claude", path)
            base = result["base"]
            events = result["events"]
            transcripts.annotate_agent_events(path, events)
            total = base + len(events)
            start = max(base, min(max(after, 0), result["dirty_from"], total))
            return {
                "format": "claude",
                "path": str(path),
                "tokens": result["tokens"],
                "tasks": result.get("tasks") or [],
                "pr": result.get("pr"),
                "from": start,
                "total": total,
                "events": events[start - base :],
                "subagents": _active_subagents(path),
                "working": _transcript_working(path, ticket),
            }
        raise HTTPException(status_code=404, detail="Orchestrator transcript missing")

    current = (registry.get(ticket) or {}).get("current") or {}
    kind = current.get("kind")
    spawned_at = current.get("spawned_at")
    registry_session_id = current.get("session_id") if isinstance(current.get("session_id"), str) else None
    archive_dir: Path | None = None
    if not current:
        kind, spawned_at, archive_dir = _archive_hint(ticket)

    found = _session_paths.get(ticket)
    if found is None or not found[1].is_file():
        found = transcripts.find_session(kind, ticket, spawned_at, registry_session_id)
        if found:
            _session_paths[ticket] = found
    if found is None:
        # Native transcript gone (cleanup) — fall back to the archived pane log.
        if archive_dir is not None:
            logs = sorted(archive_dir.glob("*.log"), key=lambda p: p.stat().st_size, reverse=True)
            if logs:
                tail = clean_pane_log(logs[0])
                return {
                    "format": "pane-log",
                    "path": str(logs[0]),
                    "tokens": None,
                    "from": 0,
                    "total": 1,
                    "events": [{"kind": "terminal", "ts": None, "text": tail}],
                }
        raise HTTPException(status_code=404, detail="No session transcript found")

    fmt, path = found
    result = transcripts.read_session_events(fmt, path)
    base = result["base"]
    events = result["events"]
    if fmt == "claude":
        transcripts.annotate_agent_events(path, events)
    total = base + len(events)
    start = max(base, min(max(after, 0), result["dirty_from"], total))
    return {
        "format": fmt,
        "path": str(path),
        "tokens": result["tokens"],
        "tasks": result.get("tasks") or [],
        "pr": result.get("pr"),
        "from": start,
        "total": total,
        "events": events[start - base :],
        "subagents": _active_subagents(path) if fmt == "claude" else [],
        "working": _transcript_working(path, ticket),
    }


def _transcript_working(path: Path, ticket: str | None = None) -> bool:
    """Pane spinner is authoritative (transcript writes gap during long tool calls);
    mtime is the fallback when there's no live window."""
    if ticket:
        window = resolve_window(ticket)
        if window:
            pane = capture_pane_tail(window, 20)
            if pane is not None:
                return pane_is_working(pane)
    try:
        return (datetime.now(tz=timezone.utc).timestamp() - path.stat().st_mtime) < 30
    except OSError:
        return False


def _active_subagents(main_path: Path) -> list[dict]:
    """Subagent files with fresh mtime = running (covers background agents whose
    Agent tool call returns immediately)."""
    now = datetime.now(tz=timezone.utc).timestamp()
    return [
        {
            "id": entry["id"],
            "active": (now - entry["mtime"]) < 45,
            "head": entry["prompt_head"][:60],
        }
        for entry in transcripts.list_subagents(main_path)
    ]


SUBAGENT_ID_PATTERN = re.compile(r"^[a-f0-9]{8,24}$")


def _resolve_main_transcript(ticket: str) -> Path | None:
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
        orch = (registry.get("_orchestrators") or {}).get(ticket)
    except (OSError, ValueError):
        orch = None
    if orch and orch.get("transcript"):
        path = Path(orch["transcript"])
        return path if path.is_file() else None
    found = _session_paths.get(ticket)
    return found[1] if found and found[0] == "claude" and found[1].is_file() else None


@app.get("/api/agents/{ticket}/subagents/{agent_id}/session")
def subagent_session(ticket: str, agent_id: str, after: int = 0) -> dict[str, object]:
    if not TICKET_PATTERN.fullmatch(ticket) or not SUBAGENT_ID_PATTERN.fullmatch(agent_id):
        raise HTTPException(status_code=400, detail="Bad id")
    main_path = _resolve_main_transcript(ticket)
    if main_path is None:
        raise HTTPException(status_code=404, detail="No claude transcript for this agent")
    path = transcripts.subagents_dir(main_path) / f"agent-{agent_id}.jsonl"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No such subagent")
    result = transcripts.read_session_events("claude-sub", path)
    base = result["base"]
    events = result["events"]
    total = base + len(events)
    start = max(base, min(max(after, 0), result["dirty_from"], total))
    return {
        "format": "claude",
        "path": str(path),
        "tokens": result["tokens"],
        "tasks": result.get("tasks") or [],
        "pr": result.get("pr"),
        "from": start,
        "total": total,
        "events": events[start - base :],
    }


UPLOAD_DIR = Path("/tmp/wiki-uploads")
IMAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+\.(png|jpg|jpeg|gif|webp)$")


class UploadIn(BaseModel):
    media_type: str = Field(..., pattern="^image/(png|jpeg|gif|webp)$")
    data: str = Field(..., max_length=20_000_000)  # base64


@app.post("/api/upload")
def upload_image(body: UploadIn) -> dict[str, str]:
    import base64
    import uuid

    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}[
        body.media_type
    ]
    try:
        blob = base64.b64decode(body.data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Bad base64") from exc
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}.{ext}"
    (UPLOAD_DIR / name).write_bytes(blob)
    return {"path": str(UPLOAD_DIR / name), "url": f"/api/uploads/{name}"}


def _serve_image(directory: Path, name: str) -> FileResponse:
    if not IMAGE_NAME_PATTERN.fullmatch(name):
        raise HTTPException(status_code=400, detail="Bad name")
    target = directory / name
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)


@app.get("/api/uploads/{name}")
def get_upload(name: str) -> FileResponse:
    return _serve_image(UPLOAD_DIR, name)


@app.get("/api/transcript-images/{name}")
def get_transcript_image(name: str) -> FileResponse:
    return _serve_image(transcripts.TRANSCRIPT_IMAGE_DIR, name)


SKILLS_DIR = Path.home() / ".claude" / "skills"
COMMANDS_DIR = Path.home() / ".claude" / "commands"
_skills_cache: tuple[float, list[dict]] | None = None


@app.get("/api/skills")
def list_skills() -> dict[str, object]:
    global _skills_cache
    now = datetime.now(tz=timezone.utc).timestamp()
    if _skills_cache and now - _skills_cache[0] < 60:
        return {"skills": _skills_cache[1]}
    skills: list[dict] = []
    if SKILLS_DIR.is_dir():
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            manifest = skill_dir / "SKILL.md"
            if not manifest.is_file():
                continue
            description = ""
            try:
                for line in manifest.read_text(encoding="utf-8").split("\n")[:12]:
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip()[:140]
                        break
            except OSError:
                pass
            skills.append({"name": skill_dir.name, "description": description})
    if COMMANDS_DIR.is_dir():
        for command in sorted(COMMANDS_DIR.glob("*.md")):
            skills.append({"name": command.stem, "description": ""})
    _skills_cache = (now, skills)
    return {"skills": skills}


@app.get("/api/tokens")
async def get_tokens(
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    bucket: str = "hour",
    cli: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    """Token-usage series over local codex/claude session files.

    Query params: from (ISO, inclusive), to (ISO, exclusive), bucket=hour|day,
    cli=codex|claude, model=<exact>. `from` is a Python keyword so we bind it
    via alias.
    """
    return await asyncio.to_thread(
        tokens.query_nonblocking,
        from_ts=from_ts,
        to_ts=to_ts,
        bucket=bucket,
        cli=cli,
        model=model,
    )


MSG_QUEUE_PATH = Path("/tmp/wiki-msg-queue.json")
# codex: "• Working (26m 28s • esc to interrupt)" · claude: "✽ Leavening… (4m 26s · ↓ 6.0k tokens)"
SPINNER_PATTERN = re.compile(r"esc to interrupt|\(\d+m\s\d+s\b|\(\d+s\b")


def pane_is_working(pane: str) -> bool:
    return bool(SPINNER_PATTERN.search(pane))


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    mode: str = Field(default="now", pattern="^(now|on-idle)$")


class SpawnWorkerIn(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=80)
    kind: str = Field(..., min_length=2, max_length=8)
    role: str = Field(..., min_length=4, max_length=16)
    model: str = Field(..., min_length=2, max_length=64)
    effort: str | None = Field(default=None, max_length=16)
    workdir: str = Field(..., min_length=1, max_length=4096)
    orch: str | None = Field(default=None, max_length=100)
    prompt: str = Field(..., min_length=1, max_length=100_000)


class SpawnOrchestratorIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=100)
    workdir: str = Field(..., min_length=1, max_length=4096)
    model: str = Field(..., min_length=2, max_length=32)
    goal: str = Field(default="", max_length=20_000)


def _read_queue() -> dict[str, list[dict]]:
    try:
        data = json.loads(MSG_QUEUE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_queue(queue: dict[str, list[dict]]) -> None:
    queue = {k: v for k, v in queue.items() if v}
    tmp = MSG_QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=1), encoding="utf-8")
    tmp.rename(MSG_QUEUE_PATH)


def _read_agent_registry() -> dict:
    try:
        data = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_existing_dir(raw_path: str, *, field_name: str) -> Path:
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} does not exist") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"{field_name} must be a directory")
    return resolved


def run_checked(args: list[str], *, timeout: int, label: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError as exc:
        raise RuntimeError(f"{label}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label}: timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{label}: {detail[:240]}")
    return result


def resolve_window(ticket: str) -> str | None:
    """Live tmux window for a worker ticket or orchestrator id."""
    registry = _read_agent_registry()
    window = ((registry.get(ticket) or {}).get("current") or {}).get("window") or (
        (registry.get("_orchestrators") or {}).get(ticket) or {}
    ).get("window")
    if not window or not re.fullmatch(r"@\d+", window):
        return None
    return window if window in tmux_live_windows() else None


def deliver_message(window: str, text: str) -> None:
    """Protocol input channel: literal text, pause (composer paste-detection), Enter,
    then verify submitted — text still sitting in the composer gets a bare Enter."""
    subprocess.run(["tmux", "send-keys", "-t", window, "-l", text], timeout=5, check=False)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)
    time.sleep(2)
    pane = capture_pane_tail(window, 30) or ""
    if text[:60] in pane.replace("\n", " "):
        subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)


def accept_claude_trust_prompt(window: str, *, timeout_seconds: float = 8.0) -> None:
    """New project directories can trigger Claude's trust prompt before the kickoff prompt runs."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pane = capture_pane_tail(window, 40) or ""
        if "Quick safety check" not in pane and (
            "? for shortcuts" in pane or "-- INSERT --" in pane or "bypass permissions on" in pane
        ):
            return
        if "Quick safety check" in pane and "Yes, I trust this folder" in pane:
            subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)
            time.sleep(0.5)
            return
        time.sleep(0.25)


@app.post("/api/agents/spawn")
def spawn_agent(body: SpawnWorkerIn) -> dict[str, str]:
    ticket = body.ticket.strip()
    if not ticket or not SPAWN_TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Ticket must be uppercase letters, numbers, or dashes")

    kind = body.kind.strip()
    if kind not in {"cdx", "cc"}:
        raise HTTPException(status_code=400, detail="Kind must be cdx or cc")

    role = body.role.strip()
    if role not in WORKER_ROLES:
        raise HTTPException(status_code=400, detail="Role must be plan, implement, or review")

    model = body.model.strip()
    allowed_models = CDX_MODELS if kind == "cdx" else CC_MODELS
    if model not in allowed_models:
        raise HTTPException(status_code=400, detail="Model is not allowed for this worker kind")

    effort = (body.effort or "").strip() or None
    if kind == "cdx":
        if effort not in REASONING_EFFORTS:
            raise HTTPException(status_code=400, detail="Reasoning effort is required for Codex workers")
    elif effort is not None:
        raise HTTPException(status_code=400, detail="Claude workers do not accept reasoning effort")

    prompt = body.prompt
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Kickoff prompt is required")
    if len(prompt.encode("utf-8")) >= MAX_SPAWN_PROMPT_BYTES:
        raise HTTPException(status_code=400, detail="Kickoff prompt must be smaller than 100KB")

    workdir_path = resolve_existing_dir(body.workdir, field_name="Working directory")

    registry = _read_agent_registry()
    orch = (body.orch or "").strip()
    if orch:
        if not ORCH_ID_PATTERN.fullmatch(orch):
            raise HTTPException(status_code=400, detail="Orchestrator id is invalid")
        if orch not in (registry.get("_orchestrators") or {}):
            raise HTTPException(status_code=400, detail="Orchestrator id is not registered")

    current = (registry.get(ticket) or {}).get("current") or {}
    live_window = current.get("window")
    if isinstance(live_window, str) and live_window in tmux_live_windows():
        raise HTTPException(status_code=409, detail=f"{ticket} already has a live worker window")

    prompt_path = Path("/tmp") / f"{kind}-{ticket}-prompt.md"
    log_path = Path("/tmp") / f"{kind}-{ticket}.log"
    status_path = AGENT_STATUS_DIR / f"{ticket}.json"
    AGENT_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        status_path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)
        prompt_path.write_text(prompt if prompt.endswith("\n") else f"{prompt}\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not prepare worker files") from exc

    prompt_shell = shlex.quote(str(prompt_path))
    model_shell = shlex.quote(model)
    if kind == "cdx":
        effort_shell = shlex.quote(effort or "")
        command = (
            f'codex --yolo -m {model_shell} -c model_reasoning_effort={effort_shell} '
            f'"$(cat {prompt_shell})"'
        )
    else:
        command = f'claude --model {model_shell} --dangerously-skip-permissions "$(cat {prompt_shell})"'

    window: str | None = None
    try:
        created = run_checked(
            [
                "tmux",
                "new-window",
                "-dP",
                "-F",
                "#{window_id}",
                "-n",
                f"{kind}:{ticket}",
                "-c",
                str(workdir_path),
                command,
            ],
            timeout=10,
            label="tmux new-window failed",
        )
        window = created.stdout.strip()
        if not re.fullmatch(r"@\d+", window):
            raise RuntimeError(f"tmux new-window failed: unexpected window id {window!r}")

        run_checked(
            ["tmux", "pipe-pane", "-t", window, "-o", f"cat >> {shlex.quote(str(log_path))}"],
            timeout=5,
            label="tmux pipe-pane failed",
        )

        register_args = [
            str(ROOT_DIR / "wiki"),
            "agent",
            "register",
            ticket,
            "--window",
            window,
            "--kind",
            kind,
            "--role",
            role,
            "--model",
            model,
            "--worktree",
            str(workdir_path),
            "--log",
            str(log_path),
        ]
        if orch:
            register_args.extend(["--orch", orch])
        run_checked(register_args, timeout=10, label="wiki agent register failed")
    except RuntimeError as exc:
        if window:
            subprocess.run(["tmux", "kill-window", "-t", window], timeout=5, check=False)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"window": window, "log": str(log_path), "prompt_path": str(prompt_path)}


@app.post("/api/agents/spawn-orchestrator")
def spawn_orchestrator(body: SpawnOrchestratorIn, background: BackgroundTasks) -> dict[str, str]:
    orch_id = body.id.strip()
    if not ORCH_ID_PATTERN.fullmatch(orch_id):
        raise HTTPException(
            status_code=400,
            detail="Orchestrator id must start with a letter or number and only use letters, numbers, dashes, or underscores",
        )

    model = body.model.strip()
    if model not in CC_MODELS:
        raise HTTPException(status_code=400, detail="Model must be opus or sonnet")

    goal = body.goal.strip()
    if len(goal.encode("utf-8")) >= MAX_ORCH_GOAL_BYTES:
        raise HTTPException(status_code=400, detail="Initial goal must stay under 20KB")

    workdir_path = resolve_existing_dir(body.workdir, field_name="Project directory")

    registry = _read_agent_registry()
    live_windows = tmux_live_windows()
    if tmux_has_window_named(f"orch:{orch_id}"):
        raise HTTPException(status_code=409, detail=f"orch:{orch_id} already exists as a live tmux window")
    existing = (registry.get("_orchestrators") or {}).get(orch_id)
    if existing:
        existing_window = existing.get("window")
        if isinstance(existing_window, str) and existing_window in live_windows:
            raise HTTPException(status_code=409, detail=f"{orch_id} already has a live orchestrator window")
        raise HTTPException(
            status_code=409,
            detail=f"{orch_id} is already registered as an orchestrator; run `wiki agent orch-done {orch_id}` first",
        )

    goal_instruction = (
        f"Pursue this initial goal immediately:\n\n{goal}\n"
        if goal
        else "Print exactly one line: READY: orchestrator registered and awaiting instructions.\nThen wait for Henry to steer you via the wiki composer."
    )
    prompt = ORCH_KICKOFF_TEMPLATE.format(orch_id=orch_id, goal_instruction=goal_instruction)

    prompt_path = Path("/tmp") / f"cc-orch-{orch_id}-prompt.md"
    log_path = Path("/tmp") / f"cc-orch-{orch_id}.log"
    try:
        log_path.unlink(missing_ok=True)
        prompt_path.unlink(missing_ok=True)
        prompt_path.write_text(prompt if prompt.endswith("\n") else f"{prompt}\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not prepare orchestrator files") from exc

    prompt_shell = shlex.quote(str(prompt_path))
    model_shell = shlex.quote(model)
    command = f'claude --model {model_shell} --dangerously-skip-permissions "$(cat {prompt_shell})"'

    window: str | None = None
    try:
        created = run_checked(
            [
                "tmux",
                "new-window",
                "-dP",
                "-F",
                "#{window_id}",
                "-n",
                f"orch:{orch_id}",
                "-c",
                str(workdir_path),
                command,
            ],
            timeout=10,
            label="tmux new-window failed",
        )
        window = created.stdout.strip()
        if not re.fullmatch(r"@\d+", window):
            raise RuntimeError(f"tmux new-window failed: unexpected window id {window!r}")

        run_checked(
            ["tmux", "pipe-pane", "-t", window, "-o", f"cat >> {shlex.quote(str(log_path))}"],
            timeout=5,
            label="tmux pipe-pane failed",
        )
        background.add_task(accept_claude_trust_prompt, window)
    except RuntimeError as exc:
        if window:
            subprocess.run(["tmux", "kill-window", "-t", window], timeout=5, check=False)
        try:
            prompt_path.unlink(missing_ok=True)
            log_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "window": window,
        "log": str(log_path),
        "prompt_path": str(prompt_path),
        "note": "orchestrator launching — will appear when it self-registers",
    }


@app.post("/api/agents/{ticket}/message")
def agent_message(ticket: str, body: MessageIn, background: BackgroundTasks) -> dict[str, object]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    window = resolve_window(ticket)
    if window is None:
        raise HTTPException(status_code=409, detail="No live tmux window for this agent")
    if body.mode == "on-idle":
        queue = _read_queue()
        queue.setdefault(ticket, []).append(
            {"text": body.text, "queued_at": datetime.now(tz=timezone.utc).isoformat()}
        )
        _write_queue(queue)
        return {"status": "queued", "position": len(queue[ticket])}
    # Delivery has load-bearing sleeps (paste-pause + submit-verify) — don't block the response.
    background.add_task(deliver_message, window, body.text)
    return {"status": "sent"}


@app.get("/api/agents/{ticket}/queue")
def agent_queue(ticket: str) -> dict[str, object]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    return {"messages": _read_queue().get(ticket, [])}


@app.delete("/api/agents/{ticket}/queue/{index}")
def agent_queue_delete(ticket: str, index: int) -> dict[str, object]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    queue = _read_queue()
    messages = queue.get(ticket, [])
    if not 0 <= index < len(messages):
        raise HTTPException(status_code=404, detail="No such queued message")
    messages.pop(index)
    _write_queue(queue)
    return {"messages": messages}


_idle_counts: dict[str, int] = {}


async def message_dispatcher() -> None:
    """Deliver on-idle messages once the target pane shows no spinner twice in a row."""
    while True:
        await asyncio.sleep(2.5)
        try:
            queue = _read_queue()
            if not any(queue.values()):
                _idle_counts.clear()
                continue
            for ticket, messages in list(queue.items()):
                if not messages:
                    continue
                window = resolve_window(ticket)
                if window is None:
                    continue  # window gone — hold until it returns or user cancels
                pane = await asyncio.to_thread(capture_pane_tail, window, 40)
                if pane is None or pane_is_working(pane):
                    _idle_counts[ticket] = 0
                    continue
                _idle_counts[ticket] = _idle_counts.get(ticket, 0) + 1
                if _idle_counts[ticket] < 2:
                    continue
                message = messages.pop(0)
                _write_queue(queue)
                _idle_counts[ticket] = 0
                await asyncio.to_thread(deliver_message, window, message["text"])
        except Exception:
            continue  # dispatcher must never die


# ---------------------------------------------------------------------------
# Agent event broker: watchdog + manual endpoints push, /api/events streams.
# ---------------------------------------------------------------------------
_event_subscribers: set[asyncio.Queue[dict]] = set()


async def publish_agent_event(event: dict) -> None:
    dead: list[asyncio.Queue[dict]] = []
    for queue_ in list(_event_subscribers):
        try:
            queue_.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(queue_)
    for queue_ in dead:
        _event_subscribers.discard(queue_)


def _subscribe_agent_events() -> asyncio.Queue[dict]:
    subscriber: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)
    _event_subscribers.add(subscriber)
    return subscriber


@app.on_event("startup")
async def _start_dispatcher() -> None:
    asyncio.create_task(message_dispatcher())
    asyncio.create_task(accounts.watchdog_loop(publish_agent_event))
    asyncio.create_task(tokens.refresh_in_background())


def vault_snapshot() -> dict[str, float]:
    snapshot = {
        str(path): path.stat().st_mtime
        for path in VAULT_DIR.rglob("*.md")
        if ".obsidian" not in path.parts
    }
    # Track commits too (activity feed freshness): HEAD, branch refs, packed-refs.
    git_dir = ROOT_DIR / ".git"
    extras = [git_dir / "HEAD", git_dir / "packed-refs", *(git_dir / "refs" / "heads").glob("*")]
    # Agent state: registry + per-ticket status files.
    extras.append(AGENT_REGISTRY_PATH)
    extras.append(MSG_QUEUE_PATH)
    if AGENT_STATUS_DIR.is_dir():
        extras.extend(AGENT_STATUS_DIR.glob("*.json"))
    for candidate in extras:
        try:
            snapshot[str(candidate)] = candidate.stat().st_mtime
        except OSError:
            continue
    return snapshot


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream():
        subscriber = _subscribe_agent_events()
        snapshot = vault_snapshot()
        idle_ticks = 0
        yield "retry: 2000\n\n"
        try:
            while True:
                await asyncio.sleep(1)
                while True:
                    try:
                        evt = subscriber.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    yield f"data: {json.dumps(evt)}\n\n"
                current = vault_snapshot()
                if current != snapshot:
                    changed = [
                        path
                        for path in current
                        if snapshot.get(path) != current[path]
                    ] + [path for path in snapshot if path not in current]
                    snapshot = current
                    paths = []
                    for path in changed:
                        try:
                            paths.append(Path(path).relative_to(VAULT_DIR).as_posix())
                        except ValueError:
                            paths.append("git")
                    yield f"data: {json.dumps({'type': 'vault', 'paths': sorted(set(paths))[:20]})}\n\n"
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    if idle_ticks >= 15:
                        idle_ticks = 0
                        yield ": ping\n\n"
        finally:
            _event_subscribers.discard(subscriber)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/links", response_model=dict[str, NoteLinks])
def links() -> dict[str, NoteLinks]:
    contents = {note_id_for(path): read_note(path) for path in iter_note_files()}

    resolver: dict[str, str] = {}
    for note_id, content in contents.items():
        lowered = note_id.lower()
        resolver[lowered] = note_id
        resolver[lowered.removesuffix(".md")] = note_id
        resolver[Path(note_id).stem.lower()] = note_id
        resolver.setdefault(extract_title(content, note_id).lower(), note_id)

    outgoing: dict[str, list[str]] = {note_id: [] for note_id in contents}
    unresolved: dict[str, list[str]] = {note_id: [] for note_id in contents}
    incoming: dict[str, list[str]] = {note_id: [] for note_id in contents}

    for note_id, content in contents.items():
        for match in WIKILINK_PATTERN.finditer(strip_code(content)):
            target = match.group(1).strip().lower().removesuffix(".md")
            resolved = resolver.get(target)
            if resolved and resolved != note_id:
                if resolved not in outgoing[note_id]:
                    outgoing[note_id].append(resolved)
                    incoming[resolved].append(note_id)
            elif not resolved and target not in unresolved[note_id]:
                unresolved[note_id].append(target)

    return {
        note_id: NoteLinks(
            outgoing=outgoing[note_id],
            incoming=sorted(set(incoming[note_id])),
            unresolved=unresolved[note_id],
        )
        for note_id in contents
    }


@app.get("/api/notes", response_model=list[NoteSummary])
def list_notes(q: str | None = None) -> list[NoteSummary]:
    files = iter_note_files()
    if q:
        needle = q.lower()
        files = [
            path
            for path in files
            if needle in note_id_for(path).lower() or needle in read_note(path).lower()
        ]
    return [to_summary(path) for path in files]


@app.get("/api/notes/{note_path:path}", response_model=Note)
def get_note(note_path: str) -> Note:
    path = resolve_note_path(note_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Note not found")
    return to_note(path)


@app.post("/api/notes", response_model=Note, status_code=status.HTTP_201_CREATED)
def create_note(payload: NoteCreate) -> Note:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")

    note_path = unique_note_path(payload.path, title)
    target = resolve_note_path(note_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(normalize_content(title, payload.content), encoding="utf-8")
    return to_note(target)


class RenameRequest(BaseModel):
    path: str = Field(..., max_length=260)
    new_path: str = Field(..., max_length=260)


@app.post("/api/rename")
def rename(payload: RenameRequest) -> dict[str, object]:
    try:
        changed = vaultops.rename_note(VAULT_DIR, payload.path, payload.new_path)
    except vaultops.VaultOpError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": changed[0], "changed": changed}


@app.delete("/api/notes/{note_path:path}")
def delete_note(note_path: str) -> dict[str, object]:
    try:
        changed = vaultops.delete_note(VAULT_DIR, note_path)
    except vaultops.VaultOpError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": changed[0], "changed": changed}


@app.put("/api/notes/{note_path:path}", response_model=Note)
def update_note(note_path: str, payload: NoteUpdate) -> Note:
    target = resolve_note_path(note_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Note not found")

    content = payload.content.rstrip()
    if len(content.encode("utf-8")) > MAX_NOTE_BYTES:
        raise HTTPException(status_code=413, detail="Note is too large")

    target.write_text(f"{content}\n", encoding="utf-8")
    return to_note(target)


class AccountRotateIn(BaseModel):
    account: str | None = Field(default=None, max_length=120)


@app.get("/api/accounts")
def get_accounts() -> dict[str, object]:
    return accounts.snapshot()


@app.post("/api/accounts/rotate")
async def rotate_account(body: AccountRotateIn) -> dict[str, object]:
    state = await asyncio.to_thread(accounts.read_state)
    state = await asyncio.to_thread(accounts.ensure_state_initialized, state)

    force_target = (body.account or "").strip() or None
    if force_target and force_target not in await asyncio.to_thread(accounts.list_available_accounts):
        raise HTTPException(status_code=400, detail="unknown account")

    try:
        result = await accounts.rotate_locked(
            state=state,
            force_target=force_target,
        )
    except accounts.RotationDebouncedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except accounts.NoEligibleAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except accounts.RotationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    event = {
        "type": "codex_rotation",
        "from": result.outgoing,
        "to": result.incoming,
        "revived": result.revived,
        "failed": result.failed,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    await publish_agent_event(event)
    return {
        "from": result.outgoing,
        "to": result.incoming,
        "revived": result.revived,
        "failed": result.failed,
    }


app.include_router(uistate.router)

mount_frontend_static(app)
