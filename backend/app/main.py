from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import accounts, agent_replace, github_pr, terminal, tokens, transcripts, uistate, vaultops
from .agent_runtime.client import (
    SupervisorClient,
    SupervisorRemoteError,
    SupervisorUnavailable,
)
from .agent_runtime.store import RuntimePaths
from .frontend_static import mount_frontend_static


ROOT_DIR = Path(os.environ.get("WIKI_REPO_DIR", Path(__file__).resolve().parents[2])).resolve()
VAULT_DIR = Path(os.environ.get("WIKI_VAULT_DIR", ROOT_DIR / "vault")).resolve()
MAX_NOTE_BYTES = 2_000_000

VAULT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Wiki API")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=terminal.TRUSTED_HOSTS)
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


AGENT_REGISTRY_PATH = Path(os.environ.get("WIKI_AGENT_REGISTRY_PATH") or "/tmp/agent-registry.json")
AGENT_STATUS_DIR = Path(os.environ.get("WIKI_AGENT_STATUS_DIR") or "/tmp/agent-status")
AGENT_ARCHIVE_DIR = Path(
    os.environ.get("WIKI_AGENT_ARCHIVE_DIR") or Path.home() / "me" / "fun" / "agent-archive"
)
AGENT_TMP_DIR = Path(os.environ.get("WIKI_AGENT_TMP_DIR") or "/tmp")
SUPERVISOR_CLIENT = SupervisorClient(RuntimePaths.from_env())
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
HEADLESS_ORCH_KICKOFF_TEMPLATE = """You are the MASTERMIND ORCHESTRATOR `{orch_id}` for this project.
Wiki's durable supervisor has already registered this provider session. Do not self-register and do not use tmux as an agent runtime or control plane.

Delegate bounded tickets/tasks through Wiki's supervisor-backed controls, monitor durable lifecycle/status/event data, steer runs through Wiki, and independently gate PRs before merge. Every worker you create must use orchestrator id `{orch_id}` so Wiki preserves the group.

Before spawning anything, read this protocol note in full:
`~/me/fun/wiki/vault/tools/orchestrator-worker-protocol.md`

Where that note still describes tmux mechanics, preserve the workflow invariant but use Wiki's headless supervisor controls instead.

After reading the protocol:
{goal_instruction}
"""


def valid_agent_id(value: str) -> bool:
    return bool(TICKET_PATTERN.fullmatch(value) or ORCH_ID_PATTERN.fullmatch(value))


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

    legacy_windows: set[str] = set()
    headless_ids: set[str] = set()
    for ticket, entry in registry.items():
        if ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if not isinstance(current, dict):
            continue
        if _is_headless(current):
            headless_ids.add(current["run_id"])
        elif isinstance(current.get("window"), str):
            legacy_windows.add(current["window"])
    legacy_windows.update(
        orch.get("window")
        for orch in (registry.get("_orchestrators") or {}).values()
        if isinstance(orch, dict) and isinstance(orch.get("window"), str)
    )
    live_windows = tmux_live_windows() if legacy_windows else set()
    runtime_by_id: dict[str, dict] = {}
    supervisor_health: dict[str, object] = {
        "status": "not-needed" if not headless_ids else "unavailable",
        "runs": len(headless_ids),
    }
    if headless_ids:
        try:
            runtime_result = _supervisor_request("run/list")
            runtime_rows = (
                runtime_result.get("runs", [])
                if isinstance(runtime_result, dict)
                else []
            )
            runtime_by_id = {
                row["run_id"]: row
                for row in runtime_rows
                if isinstance(row, dict)
                and isinstance(row.get("run_id"), str)
                and row["run_id"] in headless_ids
            }
            supervisor_health["status"] = "ready"
            supervisor_health["pid"] = runtime_result.get("pid")
        except (HTTPException, SupervisorUnavailable, SupervisorRemoteError) as exc:
            supervisor_health["detail"] = str(exc.detail if isinstance(exc, HTTPException) else exc)
    now = datetime.now(tz=timezone.utc).timestamp()
    workers = []
    orchestrators = []
    seen_tickets = set()

    for ticket, entry in sorted(registry.items()):
        if ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current") or {}
        if not isinstance(current, dict):
            continue
        headless = _is_headless(current)
        runtime = runtime_by_id.get(current.get("run_id"), current) if headless else {}
        runtime_state = runtime.get("state") if headless else None
        control_attached = bool(runtime.get("control_attached")) if headless else False
        status = read_agent_status(ticket)
        seen_tickets.add(ticket)
        window_alive = (
            control_attached if headless else current.get("window") in live_windows
        )
        if current.get("role") == "orchestrator":
            transcript = current.get("transcript")
            orchestrators.append(
                {
                    "id": ticket,
                    "window": current.get("window"),
                    "window_alive": window_alive,
                    "run_id": current.get("run_id"),
                    "runtime_state": runtime_state,
                    "control_attached": control_attached,
                    "provider_session_id": current.get("provider_session_id"),
                    "provider_pid": runtime.get("provider_pid") if headless else None,
                    "cwd": current.get("worktree") or current.get("cwd"),
                    "model": current.get("model"),
                    "spawned_at": current.get("spawned_at"),
                    "transcript_exists": bool(
                        isinstance(transcript, str) and Path(transcript).is_file()
                    ),
                    "log": current.get("log"),
                }
            )
            continue
        workers.append(
            {
                "ticket": ticket,
                "registered": True,
                "window": current.get("window"),
                "window_alive": window_alive,
                "run_id": current.get("run_id"),
                "runtime_state": runtime_state,
                "control_attached": control_attached,
                "provider_session_id": current.get("provider_session_id"),
                "provider_pid": runtime.get("provider_pid") if headless else None,
                "kind": current.get("kind"),
                "role": current.get("role"),
                "model": current.get("model"),
                "worktree": current.get("worktree"),
                "log": current.get("log"),
                "orch": current.get("orch"),
                "session": current.get("session"),
                "spawned_at": current.get("spawned_at"),
                "history": entry.get("history", []),
                "state": (status or {}).get("state") or runtime_state,
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

    for orch_id, orch in sorted((registry.get("_orchestrators") or {}).items()):
        if not isinstance(orch, dict):
            continue
        transcript = orch.get("transcript")
        orchestrators.append(
            {
                "id": orch_id,
                "window": orch.get("window"),
                "window_alive": orch.get("window") in live_windows,
                "run_id": None,
                "runtime_state": None,
                "control_attached": False,
                "provider_session_id": orch.get("session_id"),
                "provider_pid": None,
                "cwd": orch.get("cwd"),
                "model": orch.get("model"),
                "spawned_at": orch.get("spawned_at"),
                "transcript_exists": bool(transcript and Path(transcript).is_file()),
                "log": orch.get("log"),
            }
        )

    return {
        "workers": workers,
        "orchestrators": orchestrators,
        "archived": list_archived(),
        "supervisor": supervisor_health,
    }


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


def _tail_text_file(path: Path, lines: int) -> str:
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 400_000))
        raw = handle.read().decode("utf-8", errors="replace")
    return "\n".join(raw.splitlines()[-lines:])


@app.get("/api/agents/{ticket}/log")
def agent_log(ticket: str, lines: int = 200) -> dict[str, str]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    lines = max(10, min(lines, 1000))

    registry: dict = {}
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass

    current = (registry.get(ticket) or {}).get("current") or {}
    if isinstance(current, dict) and _is_headless(current):
        log_hint = current.get("log")
        if not isinstance(log_hint, str) or not Path(log_hint).is_file():
            raise HTTPException(status_code=404, detail="No supervisor event log found")
        path = Path(log_hint)
        return {"path": str(path), "tail": _tail_text_file(path, lines)}
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


def _direct_transcript_session(path: Path) -> tuple[str, Path] | None:
    if not path.is_file():
        return None
    fmt = transcripts.detect_session_format(path) or "codex"
    return (fmt, path)


def _provider_events(
    agent_id: str,
    *,
    after_seq: int = 0,
    limit: int = 200,
    include_raw: bool = False,
) -> dict[str, object] | None:
    resolved = _registry_agent(_read_agent_registry(), agent_id)
    if resolved is None or not _is_headless(resolved[2]):
        return None
    result = _supervisor_request(
        "events/read",
        {
            "agent_id": resolved[0],
            "after_seq": after_seq,
            "limit": limit,
            "include_raw": include_raw,
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("events"), list):
        raise HTTPException(
            status_code=502,
            detail="Agent supervisor returned a bad event inspector response",
        )
    return dict(result)


@app.get("/api/agents/{agent_id}/events")
def agent_provider_events(
    agent_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    include_raw: bool = Query(False),
) -> dict[str, object]:
    if not valid_agent_id(agent_id):
        raise HTTPException(status_code=400, detail="Bad agent id")
    result = _provider_events(
        agent_id,
        after_seq=after_seq,
        limit=limit,
        include_raw=include_raw,
    )
    if result is None:
        raise HTTPException(
            status_code=409,
            detail="Provider event inspection is available after headless migration",
        )
    return result


def _session_delta_payload(
    fmt: str,
    path: Path,
    *,
    cursor: int,
    client_path: str | None = None,
    ticket: str | None = None,
    include_subagents: bool = False,
    include_queue: bool = False,
) -> dict[str, object]:
    effective_cursor = 0 if client_path is not None and client_path != str(path) else cursor
    result = transcripts.read_session_delta(fmt, path, effective_cursor)
    events = result["events"]
    if fmt == "claude":
        transcripts.annotate_agent_events(path, events)
    payload: dict[str, object] = {
        "version": 2,
        "format": fmt,
        "path": str(path),
        "tokens": result["tokens"],
        "tasks": result.get("tasks") or [],
        "pr": result.get("pr"),
        "session_meta": result.get("session_meta") or {},
        "dispositions": result.get("dispositions") or {"rendered": 0, "summarized": 0, "ignored": 0, "unknown": 0},
        "base": result["base"],
        "cursor": result["cursor"],
        "tail_from": result["tail_from"],
        "events": events,
        "patches": result.get("patches") or [],
        "working": _transcript_working(path, ticket),
    }
    if include_subagents:
        payload["subagents"] = _active_subagents(path)
    if include_queue and ticket and valid_agent_id(ticket):
        payload["queue"] = _queue_messages(ticket)
    if ticket:
        provider_inspector = _provider_events(ticket, limit=50)
        if provider_inspector is not None:
            payload["provider_inspector"] = provider_inspector
    return payload


@app.get("/api/agents/{ticket}/session")
def agent_session(
    ticket: str,
    cursor: int = Query(0, ge=0),
    client_path: str | None = Query(None, alias="path"),
) -> dict[str, object]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")

    registry: dict = {}
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    orch = (registry.get("_orchestrators") or {}).get(ticket)
    if orch and orch.get("transcript"):
        found = _direct_transcript_session(Path(orch["transcript"]))
        if found is not None:
            fmt, path = found
            return _session_delta_payload(
                fmt,
                path,
                cursor=cursor,
                client_path=client_path,
                ticket=ticket,
                include_subagents=fmt == "claude",
                include_queue=True,
            )
        raise HTTPException(status_code=404, detail="Orchestrator transcript missing")

    current = (registry.get(ticket) or {}).get("current") or {}
    kind = current.get("kind")
    spawned_at = current.get("spawned_at")
    registry_session_id = current.get("session_id") if isinstance(current.get("session_id"), str) else None
    archive_dir: Path | None = None
    if not current:
        kind, spawned_at, archive_dir = _archive_hint(ticket)

    found = None
    if isinstance(current, dict) and _is_headless(current):
        transcript_hint = current.get("transcript")
        if isinstance(transcript_hint, str):
            found = _direct_transcript_session(Path(transcript_hint))
            if found is not None:
                _session_paths[ticket] = found
    if found is None:
        found = _session_paths.get(ticket)
    if found is None or not found[1].is_file():
        found = transcripts.find_session(kind, ticket, spawned_at, registry_session_id, current.get("worktree"))
        if found:
            _session_paths[ticket] = found
    if found is None:
        if isinstance(current, dict) and _is_headless(current):
            provider_inspector = _provider_events(ticket, limit=50)
            if provider_inspector is None:
                raise HTTPException(
                    status_code=503,
                    detail="Supervisor event inspector is unavailable",
                )
            return {
                "version": 2,
                "format": "provider-events",
                "path": f"provider://{current['run_id']}",
                "tokens": None,
                "tasks": [],
                "pr": None,
                "session_meta": {},
                "dispositions": {
                    "rendered": 0,
                    "summarized": 0,
                    "ignored": 0,
                    "unknown": 0,
                },
                "base": 0,
                "cursor": 0,
                "tail_from": 0,
                "events": [],
                "patches": [],
                "subagents": [],
                "queue": _queue_messages(ticket),
                "working": _transcript_working(Path(current.get("log") or "."), ticket),
                "provider_inspector": provider_inspector,
            }
        # Native transcript gone (cleanup) — fall back to the archived pane log.
        if archive_dir is not None:
            logs = sorted(archive_dir.glob("*.log"), key=lambda p: p.stat().st_size, reverse=True)
            if logs:
                tail = clean_pane_log(logs[0])
                return {
                    "version": 2,
                    "format": "pane-log",
                    "path": str(logs[0]),
                    "tokens": None,
                    "tasks": [],
                    "pr": None,
                    "session_meta": {},
                    "dispositions": {"rendered": 1, "summarized": 0, "ignored": 0, "unknown": 0},
                    "base": 0,
                    "cursor": 1,
                    "tail_from": 0,
                    "events": [{"id": 0, "kind": "terminal", "ts": None, "text": tail, "disposition": "rendered"}],
                    "patches": [],
                    "subagents": [],
                    "queue": [],
                    "working": False,
                }
        raise HTTPException(status_code=404, detail="No session transcript found")

    fmt, path = found
    return _session_delta_payload(
        fmt,
        path,
        cursor=cursor,
        client_path=client_path,
        ticket=ticket,
        include_subagents=fmt == "claude",
        include_queue=True,
    )


def _transcript_working(path: Path, ticket: str | None = None) -> bool:
    """Use supervisor lifecycle, then legacy pane spinner, then transcript mtime."""
    if ticket:
        resolved = _registry_agent(_read_agent_registry(), ticket)
        if resolved is not None and _is_headless(resolved[2]):
            return resolved[2].get("state") in {
                "starting",
                "working",
                "waiting-approval",
            }
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
    registry: dict = {}
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
        orch = (registry.get("_orchestrators") or {}).get(ticket)
    except (OSError, ValueError):
        orch = None
    if orch and orch.get("transcript"):
        found = _direct_transcript_session(Path(orch["transcript"]))
        if found and found[0] == "claude":
            return found[1]
        return None
    resolved = _registry_agent(registry if isinstance(registry, dict) else {}, ticket)
    if resolved is not None and _is_headless(resolved[2]):
        transcript_hint = resolved[2].get("transcript")
        if isinstance(transcript_hint, str):
            found = _direct_transcript_session(Path(transcript_hint))
            if found and found[0] == "claude":
                return found[1]
    found = _session_paths.get(ticket)
    return found[1] if found and found[0] == "claude" and found[1].is_file() else None


@app.get("/api/agents/{ticket}/subagents/{agent_id}/session")
def subagent_session(
    ticket: str,
    agent_id: str,
    cursor: int = Query(0, ge=0),
    client_path: str | None = Query(None, alias="path"),
) -> dict[str, object]:
    if not valid_agent_id(ticket) or not SUBAGENT_ID_PATTERN.fullmatch(agent_id):
        raise HTTPException(status_code=400, detail="Bad id")
    main_path = _resolve_main_transcript(ticket)
    if main_path is None:
        raise HTTPException(status_code=404, detail="No claude transcript for this agent")
    path = transcripts.subagents_dir(main_path) / f"agent-{agent_id}.jsonl"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No such subagent")
    return _session_delta_payload("claude-sub", path, cursor=cursor, client_path=client_path)


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


MSG_QUEUE_PATH = Path(os.environ.get("WIKI_MSG_QUEUE_PATH") or "/tmp/wiki-msg-queue.json")
# codex: "• Working (26m 28s • esc to interrupt)" · claude: "✽ Leavening… (4m 26s · ↓ 6.0k tokens)"
SPINNER_PATTERN = re.compile(r"esc to interrupt|\(\d+m\s\d+s\b|\(\d+s\b")


def pane_is_working(pane: str) -> bool:
    return bool(SPINNER_PATTERN.search(pane))


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    mode: str = Field(default="now", pattern="^(now|on-idle)$")


class AgentRespondIn(BaseModel):
    request_id: str | int
    response: dict[str, Any]


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


def _registry_agent(
    registry: dict,
    agent_id: str,
) -> tuple[str, dict, dict] | None:
    """Resolve one worker/headless orchestrator without guessing across entries."""

    for candidate in (agent_id, agent_id.upper()):
        entry = registry.get(candidate)
        if not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if isinstance(current, dict):
            return candidate, entry, current
    return None


def _is_headless(current: dict) -> bool:
    return isinstance(current.get("run_id"), str) and bool(current["run_id"])


def _supervisor_request(method: str, params: dict | None = None) -> Any:
    """Call the durable supervisor and preserve useful HTTP error classes."""

    try:
        SUPERVISOR_CLIENT.ensure_running()
        return SUPERVISOR_CLIENT.request(method, params)
    except SupervisorUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Agent supervisor is unavailable: {exc}",
        ) from exc
    except SupervisorRemoteError as exc:
        status_code = {
            "RunNotFound": 404,
            "ValueError": 400,
            "StoreConflict": 409,
            "ProviderBusy": 409,
            "ProviderProcessError": 409,
            "ProviderProtocolError": 409,
        }.get(exc.error_type, 502)
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


def _headless_queue(ticket: str) -> list[dict[str, Any]]:
    result = _supervisor_request("run/queue", {"agent_id": ticket})
    if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
        raise HTTPException(status_code=502, detail="Agent supervisor returned a bad queue")
    return [dict(message) for message in result["messages"] if isinstance(message, dict)]


def _queue_messages(ticket: str) -> list[dict]:
    resolved = _registry_agent(_read_agent_registry(), ticket)
    if resolved is not None and _is_headless(resolved[2]):
        return _headless_queue(resolved[0])
    return list(_read_queue().get(ticket, []))


def resolve_existing_dir(raw_path: str, *, field_name: str) -> Path:
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} does not exist") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"{field_name} must be a directory")
    return resolved


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


def _headless_replacement_prompt(agent_id: str, current: dict) -> str:
    status_path = AGENT_STATUS_DIR / f"{agent_id}.json"
    role = current.get("role") or "worker"
    return f"""You are the replacement {role} for {agent_id}.
Your prior provider session was {current.get("provider_session_id") or "not recorded"}.

Recover context from:
- prior transcript: {current.get("transcript") or "not resolved"}
- raw provider events: {current.get("log") or "not recorded"}
- status file: {status_path}

Preserve the same identity, role, worktree, orchestrator grouping, PR gates, and status-file contract. Re-read the current ticket/PR state, update the status file before long operations, then continue from the last durable step.
"""


def _control_headless_agent(agent_id: str, action: str) -> dict[str, object]:
    raw_id = agent_id.strip()
    if not raw_id or not valid_agent_id(raw_id):
        raise HTTPException(status_code=400, detail="Bad agent id")
    resolved = _registry_agent(_read_agent_registry(), raw_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="No registered agent")
    resolved_id, _, current = resolved
    if not _is_headless(current):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents must be migrated before lifecycle control",
        )
    result = _supervisor_request(
        f"run/{action}",
        {"agent_id": resolved_id},
    )
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail="Agent supervisor returned a bad lifecycle response",
        )
    return dict(result)


@app.post("/api/agents/{agent_id}/interrupt")
def interrupt_agent(agent_id: str) -> dict[str, object]:
    return _control_headless_agent(agent_id, "interrupt")


@app.post("/api/agents/{agent_id}/resume")
def resume_agent(agent_id: str) -> dict[str, object]:
    return _control_headless_agent(agent_id, "resume")


@app.post("/api/agents/{agent_id}/stop")
def stop_agent(agent_id: str) -> dict[str, object]:
    return _control_headless_agent(agent_id, "stop")


@app.post("/api/agents/{agent_id}/archive")
def archive_agent(agent_id: str) -> dict[str, object]:
    return _control_headless_agent(agent_id, "archive")


@app.post("/api/agents/{agent_id}/respond")
def respond_to_agent(agent_id: str, body: AgentRespondIn) -> dict[str, object]:
    raw_id = agent_id.strip()
    if not raw_id or not valid_agent_id(raw_id):
        raise HTTPException(status_code=400, detail="Bad agent id")
    if isinstance(body.request_id, bool):
        raise HTTPException(status_code=400, detail="Bad provider request id")
    if len(json.dumps(body.response).encode("utf-8")) > MAX_SPAWN_PROMPT_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Provider response must be smaller than 100KB",
        )
    resolved = _registry_agent(_read_agent_registry(), raw_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="No registered agent")
    resolved_id, _, current = resolved
    if not _is_headless(current):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents cannot accept provider responses",
        )
    result = _supervisor_request(
        "run/respond",
        {
            "agent_id": resolved_id,
            "request_id": body.request_id,
            "response": body.response,
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail="Agent supervisor returned a bad provider response",
        )
    return dict(result)


@app.post("/api/agents/{agent_id}/replace")
def replace_agent(agent_id: str) -> dict[str, object]:
    raw_id = agent_id.strip()
    if not raw_id or not (
        TICKET_PATTERN.fullmatch(raw_id) or ORCH_ID_PATTERN.fullmatch(raw_id)
    ):
        raise HTTPException(status_code=400, detail="Bad agent id")
    registry = _read_agent_registry()
    resolved = _registry_agent(registry, raw_id)
    if resolved is None or not _is_headless(resolved[2]):
        return agent_replace.replace_agent(agent_id)

    resolved_id, _, current = resolved
    result = _supervisor_request(
        "run/replace",
        {
            "run_id": current["run_id"],
            "prompt": _headless_replacement_prompt(resolved_id, current),
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Agent supervisor returned a bad run")
    refreshed = _registry_agent(_read_agent_registry(), resolved_id)
    registration = refreshed[2] if refreshed is not None else result
    return {
        "id": resolved_id,
        "type": "orchestrator" if current.get("role") == "orchestrator" else "worker",
        "window": None,
        "run_id": result.get("run_id"),
        "log": registration.get("log"),
        "prompt_path": None,
        "model": result.get("model"),
        "registration": registration,
    }


@app.post("/api/agents/spawn")
def spawn_agent(body: SpawnWorkerIn) -> dict[str, object]:
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
        headless_orch = _registry_agent(registry, orch)
        if orch not in (registry.get("_orchestrators") or {}) and not (
            headless_orch is not None
            and headless_orch[2].get("role") == "orchestrator"
            and _is_headless(headless_orch[2])
        ):
            raise HTTPException(status_code=400, detail="Orchestrator id is not registered")

    current = (registry.get(ticket) or {}).get("current") or {}
    if isinstance(current, dict) and _is_headless(current):
        raise HTTPException(
            status_code=409,
            detail=f"{ticket} already has a supervisor-owned run; use Replace",
        )
    live_window = current.get("window")
    if isinstance(live_window, str) and live_window in tmux_live_windows():
        raise HTTPException(status_code=409, detail=f"{ticket} already has a live worker window")

    status_path = AGENT_STATUS_DIR / f"{ticket}.json"
    AGENT_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        status_path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not reset worker status") from exc

    result = _supervisor_request(
        "run/start",
        {
            "agent_id": ticket,
            "provider": "codex" if kind == "cdx" else "claude",
            "role": role,
            "model": model,
            "effort": effort,
            "worktree": str(workdir_path),
            "prompt": prompt,
            "orchestrator_id": orch or None,
            "migrate_legacy": bool(current),
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Agent supervisor returned a bad run")
    refreshed = _registry_agent(_read_agent_registry(), ticket)
    registration = refreshed[2] if refreshed is not None else {}
    return {
        "window": None,
        "run_id": result.get("run_id"),
        "log": registration.get("log"),
        "prompt_path": None,
    }


@app.post("/api/agents/spawn-orchestrator")
def spawn_orchestrator(body: SpawnOrchestratorIn) -> dict[str, object]:
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
    normal_entry = registry.get(orch_id)
    normal_current = (
        normal_entry.get("current") if isinstance(normal_entry, dict) else None
    )
    if isinstance(normal_current, dict):
        detail = (
            f"{orch_id} already has a supervisor-owned run; use Replace"
            if _is_headless(normal_current)
            else f"{orch_id} is already registered as a legacy worker"
        )
        raise HTTPException(status_code=409, detail=detail)

    legacy_orchestrators = registry.get("_orchestrators")
    legacy_orchestrator = (
        legacy_orchestrators.get(orch_id)
        if isinstance(legacy_orchestrators, dict)
        else None
    )
    if legacy_orchestrator is not None and not isinstance(legacy_orchestrator, dict):
        raise HTTPException(status_code=409, detail=f"{orch_id} has an invalid legacy registration")
    migrate_legacy = isinstance(legacy_orchestrator, dict)
    if isinstance(legacy_orchestrator, dict):
        legacy_window = legacy_orchestrator.get("window")
        if (
            isinstance(legacy_window, str)
            and re.fullmatch(r"@\d+", legacy_window)
            and legacy_window in tmux_live_windows()
        ):
            raise HTTPException(
                status_code=409,
                detail=f"{orch_id} already has a live legacy orchestrator window",
            )

    goal_instruction = (
        f"Pursue this initial goal immediately:\n\n{goal}\n"
        if goal
        else "Print exactly one line: READY: orchestrator registered and awaiting instructions.\nThen wait for Henry to steer you via the wiki composer."
    )
    prompt = HEADLESS_ORCH_KICKOFF_TEMPLATE.format(
        orch_id=orch_id,
        goal_instruction=goal_instruction,
    )
    result = _supervisor_request(
        "run/start",
        {
            "agent_id": orch_id,
            "provider": "claude",
            "role": "orchestrator",
            "model": model,
            "effort": None,
            "worktree": str(workdir_path),
            "prompt": prompt,
            "orchestrator_id": None,
            "migrate_legacy": migrate_legacy,
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Agent supervisor returned a bad run")
    refreshed = _registry_agent(_read_agent_registry(), orch_id)
    registration = refreshed[2] if refreshed is not None else {}

    return {
        "window": None,
        "run_id": result.get("run_id"),
        "log": registration.get("log"),
        "prompt_path": None,
        "note": "orchestrator registered under the durable supervisor",
    }


@app.post("/api/agents/{ticket}/message")
def agent_message(ticket: str, body: MessageIn, background: BackgroundTasks) -> dict[str, object]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    resolved = _registry_agent(_read_agent_registry(), ticket)
    if resolved is not None and _is_headless(resolved[2]):
        method = "run/send_now" if body.mode == "now" else "run/send_on_idle"
        result = _supervisor_request(
            method,
            {"agent_id": resolved[0], "text": body.text},
        )
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=502,
                detail="Agent supervisor returned a bad message response",
            )
        return dict(result)
    window = resolve_window(ticket)
    if window is None:
        raise HTTPException(status_code=409, detail="No live tmux window for this agent")
    if body.mode == "on-idle":
        queue = _read_queue()
        queue.setdefault(ticket, []).append(
            {"text": body.text, "queued_at": datetime.now(tz=timezone.utc).isoformat()}
        )
        _write_queue(queue)
        background.add_task(
            publish_agent_event,
            {"type": "session", "ticket": ticket, "surface": "queue"},
        )
        return {"status": "queued", "position": len(queue[ticket]), "messages": queue[ticket]}
    # Delivery has load-bearing sleeps (paste-pause + submit-verify) — don't block the response.
    background.add_task(deliver_message, window, body.text)
    return {"status": "sent"}


@app.get("/api/agents/{ticket}/queue")
def agent_queue(ticket: str) -> dict[str, object]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    resolved = _registry_agent(_read_agent_registry(), ticket)
    if resolved is not None and _is_headless(resolved[2]):
        return {"messages": _headless_queue(resolved[0])}
    return {"messages": _read_queue().get(ticket, [])}


@app.delete("/api/agents/{ticket}/queue/{index}")
def agent_queue_delete(ticket: str, index: int) -> dict[str, object]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    resolved = _registry_agent(_read_agent_registry(), ticket)
    if resolved is not None and _is_headless(resolved[2]):
        result = _supervisor_request(
            "run/queue/delete",
            {"agent_id": resolved[0], "index": index},
        )
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            raise HTTPException(
                status_code=502,
                detail="Agent supervisor returned a bad queue response",
            )
        return {"messages": list(result["messages"])}
    queue = _read_queue()
    messages = queue.get(ticket, [])
    if not 0 <= index < len(messages):
        raise HTTPException(status_code=404, detail="No such queued message")
    messages.pop(index)
    _write_queue(queue)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(publish_agent_event({"type": "session", "ticket": ticket, "surface": "queue"}))
    except RuntimeError:
        pass
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
                await publish_agent_event({"type": "session", "ticket": ticket, "surface": "queue"})
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


async def supervisor_event_bridge() -> None:
    """Forward supervisor events without wrapping the established SSE dictionaries."""

    while True:
        try:
            async for event in SUPERVISOR_CLIENT.subscribe_events():
                ticket = event.get("ticket")
                tickets = event.get("tickets")
                changed = {
                    value
                    for value in (
                        [ticket] if isinstance(ticket, str) else []
                    )
                    + (tickets if isinstance(tickets, list) else [])
                    if isinstance(value, str)
                }
                _invalidate_session_paths(changed)
                await publish_agent_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The backend may start before the detached daemon. Spawn/control
            # requests autostart it; this bridge reconnects without owning it.
            pass
        await asyncio.sleep(1)


async def agent_runtime_dispatchers() -> None:
    await asyncio.gather(message_dispatcher(), supervisor_event_bridge())


@app.on_event("startup")
async def _start_dispatcher() -> None:
    terminal.refresh_boot_token()
    asyncio.create_task(agent_runtime_dispatchers())
    asyncio.create_task(accounts.watchdog_loop(publish_agent_event))
    asyncio.create_task(tokens.refresh_in_background())


@app.on_event("shutdown")
async def _stop_terminals() -> None:
    await asyncio.to_thread(terminal.TERMINAL_MANAGER.close_all)


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
    if AGENT_STATUS_DIR.is_dir():
        extras.extend(AGENT_STATUS_DIR.glob("*.json"))
    for candidate in extras:
        try:
            snapshot[str(candidate)] = candidate.stat().st_mtime
        except OSError:
            continue
    return snapshot


def _read_registry_snapshot() -> dict:
    try:
        data = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _changed_registry_tickets(previous: dict, current: dict) -> set[str]:
    changed: set[str] = set()
    previous_workers = {k: v for k, v in previous.items() if k != "_orchestrators"}
    current_workers = {k: v for k, v in current.items() if k != "_orchestrators"}
    for ticket in set(previous_workers) | set(current_workers):
        if previous_workers.get(ticket) != current_workers.get(ticket):
            changed.add(ticket)
    previous_orchs = previous.get("_orchestrators") or {}
    current_orchs = current.get("_orchestrators") or {}
    for ticket in set(previous_orchs) | set(current_orchs):
        if previous_orchs.get(ticket) != current_orchs.get(ticket):
            changed.add(ticket)
    return changed


def _invalidate_session_paths(changed: set[str]) -> None:
    for ticket in changed:
        _session_paths.pop(ticket, None)


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream():
        subscriber = _subscribe_agent_events()
        snapshot = vault_snapshot()
        registry_snapshot = _read_registry_snapshot()
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
                    vault_paths: list[str] = []
                    git_changed = False
                    agents_dirty = False
                    changed_tickets: set[str] = set()
                    for path in changed:
                        candidate = Path(path)
                        if candidate == AGENT_REGISTRY_PATH:
                            agents_dirty = True
                            current_registry = _read_registry_snapshot()
                            changed_tickets.update(_changed_registry_tickets(registry_snapshot, current_registry))
                            registry_snapshot = current_registry
                            continue
                        if candidate.parent == AGENT_STATUS_DIR:
                            agents_dirty = True
                            if TICKET_PATTERN.fullmatch(candidate.stem):
                                changed_tickets.add(candidate.stem)
                            continue
                        try:
                            vault_paths.append(candidate.relative_to(VAULT_DIR).as_posix())
                        except ValueError:
                            git_changed = True
                    _invalidate_session_paths(changed_tickets)
                    if vault_paths or git_changed:
                        paths = sorted(set(vault_paths + (["git"] if git_changed else [])))[:20]
                        yield f"data: {json.dumps({'type': 'vault', 'paths': paths})}\n\n"
                    if agents_dirty:
                        yield f"data: {json.dumps({'type': 'agents', 'tickets': sorted(changed_tickets)[:20], 'surface': 'agents'})}\n\n"
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
        "failed_reasons": result.failed_reasons,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    await publish_agent_event(event)
    return {
        "from": result.outgoing,
        "to": result.incoming,
        "revived": result.revived,
        "failed": result.failed,
        "failed_reasons": result.failed_reasons,
    }


app.include_router(terminal.router)
app.include_router(uistate.router)

mount_frontend_static(app)
