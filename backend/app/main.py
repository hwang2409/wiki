from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import transcripts, vaultops


ROOT_DIR = Path(__file__).resolve().parents[2]
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
    archive_dir: Path | None = None
    if not current:
        kind, spawned_at, archive_dir = _archive_hint(ticket)

    found = _session_paths.get(ticket)
    if found is None or not found[1].is_file():
        found = transcripts.find_session(kind, ticket, spawned_at)
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


MSG_QUEUE_PATH = Path("/tmp/wiki-msg-queue.json")
# codex: "• Working (26m 28s • esc to interrupt)" · claude: "✽ Leavening… (4m 26s · ↓ 6.0k tokens)"
SPINNER_PATTERN = re.compile(r"esc to interrupt|\(\d+m\s\d+s\b|\(\d+s\b")


def pane_is_working(pane: str) -> bool:
    return bool(SPINNER_PATTERN.search(pane))


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    mode: str = Field(default="now", pattern="^(now|on-idle)$")


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


def resolve_window(ticket: str) -> str | None:
    """Live tmux window for a worker ticket or orchestrator id."""
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    window = ((registry.get(ticket) or {}).get("current") or {}).get("window") or (
        (registry.get("_orchestrators") or {}).get(ticket) or {}
    ).get("window")
    if not window or not re.fullmatch(r"@\d+", window):
        return None
    return window if window in tmux_live_windows() else None


def deliver_message(window: str, text: str) -> None:
    """Protocol input channel: literal text, pause (composer paste-detection), Enter,
    then verify submitted — text still sitting in the composer gets a bare Enter."""
    import time

    subprocess.run(["tmux", "send-keys", "-t", window, "-l", text], timeout=5, check=False)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)
    time.sleep(2)
    pane = capture_pane_tail(window, 30) or ""
    if text[:60] in pane.replace("\n", " "):
        subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)


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


@app.on_event("startup")
async def _start_dispatcher() -> None:
    asyncio.create_task(message_dispatcher())


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
        snapshot = vault_snapshot()
        idle_ticks = 0
        yield "retry: 2000\n\n"
        while True:
            await asyncio.sleep(1)
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
