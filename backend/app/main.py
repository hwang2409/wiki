from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from . import (
    account_notices,
    accounts,
    backend_runtime,
    context_prelude,
    dashboard,
    dashboard_page,
    github_pr,
    github_preview,
    installed_fonts,
    knowledge,
    palette,
    provider_health,
    replay,
    screencast,
    terminal,
    tokens,
    transcripts,
    uistate,
    vaultops,
    workgraph,
    workgraph_service,
)
from .agent_models import (
    default_model_for_kind,
    is_model_allowed,
    list_model_options,
    model_ids_for_kind,
)
from .agent_runtime.client import (
    SupervisorClient,
    SupervisorRemoteError,
    SupervisorUnavailable,
    replacement_prompt,
)
from .agent_runtime.command_log import AgentCommand
from .agent_runtime import costs
from .agent_runtime import graph_health
from .agent_runtime.loop_state import derive_loop_state
from .agent_runtime.archive_protocol import archive_is_committed
from .agent_runtime.event_store import SQLiteEventStore, runtime_event_db_path
from .agent_runtime.store import RuntimePaths
from .agent_runtime.ticket import (
    base_ticket,
    parse_reviewer_id,
    reviewer_id as canonical_reviewer_id,
    reviewer_id_candidates,
)
from .agent_runtime.unknown_kind_telemetry import UnknownKindTelemetry
from .agent_runtime.version import RUNTIME_FINGERPRINT
from .frontend_static import mount_frontend_static
from .next_review_schema import NextReviewIn
from .rebase_schema import RebaseDirtyPrIn


ROOT_DIR = Path(os.environ.get("WIKI_REPO_DIR", Path(__file__).resolve().parents[2])).resolve()
VAULT_DIR = Path(os.environ.get("WIKI_VAULT_DIR", ROOT_DIR / "vault")).resolve()
# File API paths are relative to the repository root. Note paths remain
# vault-relative because the note API is rooted at VAULT_DIR.
FILES_ROOT = ROOT_DIR
MAX_NOTE_BYTES = 2_000_000
MAX_FILE_BYTES = 2_000_000
MAX_FILE_TREE_ENTRIES = 10_000
VAULT_ASSET_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
IGNORED_FILE_PARTS = {
    ".git",
    ".obsidian",
    ".codex",
    "__pycache__",
    ".venv",
    "dist",
    "node_modules",
    "target",
}

VAULT_DIR.mkdir(parents=True, exist_ok=True)

PROVIDER_HEALTH = provider_health.ProviderHealthTracker()
ACCOUNT_NOTICES = account_notices.AccountNoticeStore()
UNKNOWN_KIND_TELEMETRY: UnknownKindTelemetry | None = None
logger = logging.getLogger(__name__)


_REBASE_RECORDING_NOTIFIER: Callable[[str, str, str], None] | None = None


def install_rebase_recording_notifier(
    sender: Callable[[str, str, str], None] | None,
) -> None:
    """Route rebase-bot notifications to a recorder instead of the live channel.

    Tests install a recorder before invoking rebase flows so they can assert
    deliveries; passing ``None`` restores the default live path.  The recorder
    takes priority over the pytest safety guard, so a test that installs a
    recorder gets real observations of every delivery.  The recorder
    receives the stable ``delivery_id`` as its third argument so tests can
    verify the id propagates all the way through.
    """

    global _REBASE_RECORDING_NOTIFIER
    _REBASE_RECORDING_NOTIFIER = sender


def _rebase_bot_notification_sender(
    target: str, text: str, delivery_id: str = ""
) -> None:
    """Send one rebase result through the configured agent message path.

    ``delivery_id`` becomes ``MessageIn.dedupe_key`` so the receiving
    orchestrator inbox drops duplicates from the outbox's bounded retry
    loop.  Without this key threaded through, a transient sender failure
    would silently stack N copies of the same rebase result.
    """

    recorder = _REBASE_RECORDING_NOTIFIER
    if recorder is not None:
        recorder(target, text, delivery_id)
        return
    # Fallback safety: if no recorder is installed and pytest is running,
    # drop the message rather than paging live operators from a test.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    agent_message(
        target,
        MessageIn(
            text=text,
            mode="now",
            source="rebase-bot",
            dedupe_key=delivery_id or None,
        ),
        BackgroundTasks(),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global UNKNOWN_KIND_TELEMETRY, _MAIN_EVENT_LOOP
    _MAIN_EVENT_LOOP = asyncio.get_running_loop()
    runtime_paths = RuntimePaths.from_env()
    from .agent_runtime import rebase_bot

    rebase_bot.resume_pending_jobs(notify=_rebase_bot_notification_sender)
    UNKNOWN_KIND_TELEMETRY = UnknownKindTelemetry(runtime_paths)
    configured_backend = os.environ.get("WIKI_BACKEND_URL")
    if configured_backend:
        backend_runtime.publish_backend_url(configured_backend)
    # Stamp the WIKI-147 unread-dot deploy cutoff BEFORE any request handling
    # and snapshot per-run viewed baselines for every pre-deploy run so events
    # arriving between startup and the first /api/agents call still render as
    # unread (WIKI-147 R5 B1).
    _ensure_deploy_timestamp()
    _snapshot_startup_baselines()
    terminal.refresh_boot_token()
    # Prime the tracker so the first /api/providers/health request is populated.
    try:
        await PROVIDER_HEALTH.refresh_async()
    except Exception:
        # Health is optional; a broken provider CLI or probe must not prevent
        # the backend from starting.
        pass
    # Warm the palette artifact view off the event loop.  SQLite is the index;
    # do not parse every events.jsonl before the first Cmd-K keystroke.
    threading.Thread(
        target=lambda: palette.collect_artifact_items_from_index(_sqlite_event_store()),
        name="palette-artifact-warm",
        daemon=True,
    ).start()
    # Lifespan owns the workgraph outbox: accepted telemetry writes are
    # drained on shutdown instead of dying with a daemon thread.
    workgraph_service.start_outbox()
    dispatcher_task, watchdog_task, token_task = await _start_dispatcher()
    cost_task = asyncio.create_task(costs.background_loop(), name="wiki-cost-aggregator")
    knowledge_task = asyncio.create_task(
        knowledge.background_index_loop(
            knowledge.KnowledgePaths.from_env(
                runtime_dir=runtime_paths.runtime_dir,
                archive_dir=runtime_paths.archive_dir,
                vault_dir=VAULT_DIR,
            )
        ),
        name="wiki-knowledge-indexer",
    )
    provider_health_task = asyncio.create_task(
        provider_health.probe_loop(PROVIDER_HEALTH),
        name="wiki-provider-health-probe",
    )
    unknown_kind_telemetry_stop = asyncio.Event()
    unknown_kind_telemetry_task = asyncio.create_task(
        UNKNOWN_KIND_TELEMETRY.periodic_loop(unknown_kind_telemetry_stop),
        name="wiki-unknown-kind-telemetry",
    )
    try:
        yield
    finally:
        unknown_kind_telemetry_stop.set()
        dispatcher_task.cancel()
        watchdog_task.cancel()
        token_task.cancel()
        cost_task.cancel()
        knowledge_task.cancel()
        provider_health_task.cancel()
        unknown_kind_telemetry_task.cancel()
        await asyncio.gather(
            dispatcher_task,
            watchdog_task,
            token_task,
            cost_task,
            knowledge_task,
            provider_health_task,
            unknown_kind_telemetry_task,
            return_exceptions=True,
        )
        # Bounded drain: every accepted workgraph write is delivered or
        # logged as undelivered before the process exits.
        await asyncio.to_thread(workgraph_service.stop_outbox)
        await asyncio.to_thread(terminal.TERMINAL_MANAGER.close_all)
        UNKNOWN_KIND_TELEMETRY = None


# --- Wiki.app origin secret (WIKI-148 round 6 — Path B) ---------------------
# Per-startup random secret proving a request came from the Wiki.app main
# process. Sidecars receive it through their private process environment.
# Launchd backends serve it through a private runtime socket for the Tauri
# process. It never travels through stdout or a daemon log.
_WIKI_APP_SECRET_HOLDER: dict[str, str] = {
    "value": os.environ.get("WIKI_APP_SECRET") or secrets.token_urlsafe(32)
}
_WIKI_APP_SECRET_MARKER = "[[WIKI_APP_SECRET_BOOT]]="


def wiki_app_secret() -> str:
    return _WIKI_APP_SECRET_HOLDER["value"]


def set_wiki_app_secret(value: str) -> None:
    """Testing hook — override the minted secret for pytest fixtures."""

    _WIKI_APP_SECRET_HOLDER["value"] = value


def wiki_app_secret_boot_line() -> str:
    """Return the legacy marker for compatibility with older test callers."""

    return f"{_WIKI_APP_SECRET_MARKER}{wiki_app_secret()}"


def require_wiki_app_origin(
    x_wiki_app_secret: str | None = Header(default=None, alias="X-Wiki-App-Secret"),
) -> None:
    """FastAPI dependency — reject composer callers without the origin secret.

    Constant-time compare against the module-level secret. Absent header,
    empty header, or mismatch all return 403. This is the ONLY authentication
    on composer endpoints in round 6 — the earlier client-supplied token was
    fundamentally spoofable (a worker could fetch it from the public token
    endpoint by orch id) and is removed entirely.
    """

    expected = wiki_app_secret()
    supplied = (x_wiki_app_secret or "").strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=403,
            detail=(
                "Composer endpoints require the Wiki.app origin secret. "
                "The Wiki.app webview supplies it automatically via the "
                "Tauri invoke bridge; direct HTTP callers (worker CLI "
                "sessions, curl) cannot obtain it."
            ),
        )


app = FastAPI(title="Wiki API", lifespan=lifespan)
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


class AssetMeta(BaseModel):
    width: int
    height: int
    media_type: str
    preview_base64: str | None = None
    # Source file mtime in integer ms; lets the client persist the preview in
    # an IndexedDB thumbnail cache and invalidate on file change (WIKI-200).
    mtime_ms: int | None = None


class Note(NoteSummary):
    content: str
    asset_meta: dict[str, AssetMeta] = Field(default_factory=dict)


class NoteCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=160)
    path: str | None = Field(default=None, max_length=260)
    content: str = Field(default="", max_length=MAX_NOTE_BYTES)


class NoteUpdate(BaseModel):
    content: str = Field(..., max_length=MAX_NOTE_BYTES)


class FileSummary(BaseModel):
    path: str
    size: int
    updated_at: datetime


class FileContent(BaseModel):
    path: str
    size: int
    content: str | None = None
    binary: bool = False
    error: str | None = None


class FileTree(BaseModel):
    files: list[FileSummary]
    truncated: bool = False


class Workspace(BaseModel):
    id: str
    root: str
    live: bool


class WorkspaceList(BaseModel):
    workspaces: list[Workspace]


@dataclass(frozen=True)
class WorkspaceCandidate:
    workspace: Workspace
    root: Path
    device: int
    inode: int


@dataclass(frozen=True)
class WorkspaceResolution:
    workspace: Workspace
    root: Path
    root_fd: int


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


def is_ignored_file_parts(parts: tuple[str, ...]) -> bool:
    return any(part.startswith(".") or part in IGNORED_FILE_PARTS for part in parts)


def file_not_found() -> None:
    raise HTTPException(status_code=404, detail="File not found")


def validate_file_path(raw_path: str) -> PurePosixPath:
    candidate = raw_path.strip().replace("\\", "/")
    if "\x00" in candidate or not candidate:
        file_not_found()
    raw_parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        file_not_found()
    try:
        path = PurePosixPath(candidate)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or is_ignored_file_parts(path.parts)
        ):
            file_not_found()
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError, TypeError):
        file_not_found()
    return path


def resolve_file_path(raw_path: str, file_root: Path) -> tuple[Path, str]:
    path = validate_file_path(raw_path)
    try:
        file_root = file_root.resolve()
        target = (file_root / Path(*path.parts)).resolve()
        relative_target = target.relative_to(file_root)
        if is_ignored_file_parts(relative_target.parts):
            file_not_found()
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError, TypeError):
        file_not_found()
    return target, path.as_posix()


def resolve_vault_asset_path(raw_path: str) -> tuple[Path, str, str]:
    candidate = raw_path.strip().replace("\\", "/")
    if "\x00" in candidate or not candidate:
        file_not_found()
    raw_parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        file_not_found()
    try:
        path = PurePosixPath(candidate)
        media_type = VAULT_ASSET_MEDIA_TYPES.get(path.suffix.lower())
        if (
            path.is_absolute()
            or media_type is None
            or any(part in {"", ".", ".."} for part in path.parts)
            or is_ignored_file_parts(path.parts)
        ):
            file_not_found()

        vault_root = VAULT_DIR.resolve()
        target = (vault_root / Path(*path.parts)).resolve()
        relative_target = target.relative_to(vault_root)
        if is_ignored_file_parts(relative_target.parts):
            file_not_found()
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError, TypeError):
        file_not_found()
    return target, path.as_posix(), media_type


from .pathwalk import open_relative_directory, open_relative_file, open_root_directory  # noqa: E402


def iter_repo_files(file_root: Path, root_fd: int | None = None) -> tuple[list[Path], bool]:
    if root_fd is not None:
        return iter_repo_files_from_fd(file_root, root_fd)

    files: list[Path] = []
    file_root = file_root.resolve()
    directories = [file_root]
    truncated = False
    while directories:
        directory = directories.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            relative_parts = Path(entry.path).relative_to(file_root).parts
            if is_ignored_file_parts(relative_parts):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=root_fd is None):
                    continue
                if root_fd is not None:
                    relative_path = Path(entry.path).relative_to(file_root)
                    try:
                        fd = open_relative_file(root_fd, relative_path.parts)
                        try:
                            if not stat.S_ISREG(os.fstat(fd).st_mode):
                                continue
                        finally:
                            os.close(fd)
                    except OSError:
                        continue
                else:
                    resolved = Path(entry.path).resolve()
                    resolved_relative = resolved.relative_to(file_root)
                    if is_ignored_file_parts(resolved_relative.parts):
                        continue
            except (OSError, RuntimeError, ValueError):
                continue
            if len(files) >= MAX_FILE_TREE_ENTRIES:
                truncated = True
                return files, truncated
            files.append(Path(entry.path))
    files.sort(key=lambda candidate: candidate.relative_to(file_root).as_posix())
    return files, truncated


def iter_repo_files_from_fd(file_root: Path, root_fd: int) -> tuple[list[Path], bool]:
    files: list[Path] = []
    directories: list[tuple[str, ...]] = [()]
    truncated = False
    no_follow = getattr(os, "O_NOFOLLOW", 0)

    while directories:
        directory_parts = directories.pop()
        try:
            directory_fd = open_relative_directory(root_fd, directory_parts)
        except OSError as exc:
            if exc.errno in {errno.EMFILE, errno.ENFILE}:
                return files, True
            continue
        try:
            with os.scandir(directory_fd) as scanner:
                entries = sorted(scanner, key=lambda entry: entry.name)
            for entry in entries:
                relative_parts = directory_parts + (entry.name,)
                if is_ignored_file_parts(relative_parts):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(relative_parts)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    file_fd = os.open(
                        entry.name,
                        os.O_RDONLY | no_follow,
                        dir_fd=directory_fd,
                    )
                    try:
                        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                            continue
                    finally:
                        os.close(file_fd)
                except OSError as exc:
                    if exc.errno in {errno.EMFILE, errno.ENFILE}:
                        return files, True
                    continue
                if len(files) >= MAX_FILE_TREE_ENTRIES:
                    truncated = True
                    return files, truncated
                files.append(file_root.joinpath(*relative_parts))
        except OSError as exc:
            if exc.errno in {errno.EMFILE, errno.ENFILE}:
                return files, True
            continue
        finally:
            os.close(directory_fd)

    files.sort(key=lambda candidate: candidate.relative_to(file_root).as_posix())
    return files, truncated


def opened_file_is_safe(fd: int, target: Path, file_root: Path, root_fd: int | None = None) -> bool:
    descriptor_stat = os.fstat(fd)
    if not stat.S_ISREG(descriptor_stat.st_mode):
        return False
    try:
        relative_target = target.relative_to(file_root)
        if (
            any(part in {"", ".", ".."} for part in relative_target.parts)
            or is_ignored_file_parts(relative_target.parts)
        ):
            return False
    except ValueError:
        return False

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    verification_root_fd: int | None = None
    current_fd: int | None = None
    try:
        if root_fd is None:
            verification_root_fd = os.open(file_root, os.O_RDONLY | no_follow | directory_flag)
        else:
            verification_root_fd = os.dup(root_fd)
        current_fd = verification_root_fd
        for index, component in enumerate(relative_target.parts):
            is_final = index == len(relative_target.parts) - 1
            flags = os.O_RDONLY | no_follow | (0 if is_final else directory_flag)
            next_fd = os.open(component, flags, dir_fd=current_fd)
            if current_fd != verification_root_fd:
                os.close(current_fd)
            current_fd = next_fd
        if current_fd is None:
            return False
        opened_stat = os.fstat(current_fd)
        return (opened_stat.st_dev, opened_stat.st_ino) == (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        )
    except (OSError, RuntimeError, ValueError):
        return False
    finally:
        if current_fd is not None and current_fd != verification_root_fd:
            os.close(current_fd)
        if verification_root_fd is not None:
            os.close(verification_root_fd)


def read_open_file(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


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


_IMAGE_EXTENSION_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg)(?:[?#]|$)", re.IGNORECASE)
# CommonMark image link: `![alt](destination[ "title"])`. The destination
# may be wrapped in `<>` (allowing spaces) or a bare token that runs until
# whitespace or the closing paren. Any following title is stripped.
_MARKDOWN_IMAGE_RE = re.compile(
    r"""
    !\[[^\]]*\]                # ![alt]
    \(                         # opening paren
    \s*                        # optional leading whitespace
    (?:
        <(?P<angle>[^>\n]*)>   # angle-bracketed path (may contain spaces)
        |
        (?P<bare>[^\s()]+)     # bare path — no whitespace, no parens
    )
    (?:\s+
        (?:"[^"]*"             # "title"
         | '[^']*'             # 'title'
         | \([^)]*\)           # (title)
        )
    )?
    \s*
    \)
    """,
    re.VERBOSE,
)
_OBSIDIAN_EMBED_RE = re.compile(r"!\[\[([^\][|]+?)(?:\|[^\][]*)?\]\]")


def _extract_note_image_paths(content: str, note_path: str) -> list[str]:
    """Return de-duplicated vault-relative paths for every image the note
    references. Skips external URLs and anything without an image extension.

    Handles CommonMark features the previous regex missed: link titles
    (`![](path "title")`), angle-bracketed paths with spaces
    (`![](<my image.png>)`), and percent-encoded characters (`%20`).
    """
    if not content:
        return []
    from urllib.parse import unquote

    candidates: list[str] = []
    seen: set[str] = set()

    def push(candidate: str) -> None:
        if candidate in seen:
            return
        seen.add(candidate)
        candidates.append(candidate)

    def note_relative(target: str) -> list[str]:
        # Split on the LITERAL query / fragment delimiters first — decoding
        # before splitting would treat `hero%23draft.png` or
        # `hero%3Fdraft.png` as if they carried a real `#` or `?`, silently
        # dropping the actual filename tail.
        without_query = target.split("?", 1)[0].split("#", 1)[0]
        try:
            stripped = unquote(without_query).strip()
        except (UnicodeDecodeError, ValueError):
            stripped = without_query.strip()
        if not stripped or stripped.startswith("/"):
            return []
        if re.match(r"^[a-z][a-z0-9+.-]*:", stripped, re.IGNORECASE):
            return []
        if stripped.startswith("//"):
            return []
        if not _IMAGE_EXTENSION_RE.search(stripped):
            return []
        cleaned = stripped.replace("\\", "/")
        parts = [segment for segment in cleaned.split("/") if segment not in ("", ".")]
        note_dir = note_path.rsplit("/", 1)[0] if "/" in note_path else ""
        base_parts = [segment for segment in note_dir.split("/") if segment]
        results: list[str] = []
        for base in ([base_parts] if base_parts else []) + [[]]:
            stack = list(base)
            good = True
            for part in parts:
                if part == "..":
                    if not stack:
                        good = False
                        break
                    stack.pop()
                else:
                    stack.append(part)
            if good and stack:
                results.append("/".join(stack))
        return results

    for match in _MARKDOWN_IMAGE_RE.finditer(content):
        raw = match.group("angle") or match.group("bare") or ""
        for candidate in note_relative(raw):
            push(candidate)
    for match in _OBSIDIAN_EMBED_RE.finditer(content):
        for candidate in note_relative(match.group(1)):
            push(candidate)
    return candidates


def _collect_note_asset_meta(content: str, note_path: str) -> dict[str, AssetMeta]:
    """Resolve each image reference in the note to `AssetMeta`, silently
    dropping anything that resolves outside the vault or fails to decode."""
    result: dict[str, AssetMeta] = {}
    for candidate in _extract_note_image_paths(content, note_path):
        try:
            raw, media_type, mtime_ms = _read_vault_asset_bytes(candidate)
        except HTTPException:
            continue
        if media_type not in _VAULT_ASSET_RESIZE_MIMES:
            continue
        payload = _asset_meta_for(raw, media_type, mtime_ms)
        if payload is None:
            continue
        result[candidate] = AssetMeta(**payload)
    return result


def to_note(path: Path) -> Note:
    summary = to_summary(path)
    content = read_note(path)
    asset_meta = _collect_note_asset_meta(content, summary.path)
    return Note(**summary.model_dump(), content=content, asset_meta=asset_meta)


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
def health(request: Request = None) -> dict[str, object]:  # type: ignore[assignment]
    request_nonce = request.headers.get("X-Wiki-Daemon-Nonce") if request else None
    payload: dict[str, object] = {
        "status": "ok",
        "daemon_managed": os.environ.get("WIKI_BACKEND_DAEMON") == "launchd",
        "backend_fingerprint": RUNTIME_FINGERPRINT,
        "process_id": os.getpid(),
    }
    if request_nonce is not None:
        payload["daemon_proof"] = hmac.new(
            wiki_app_secret().encode("utf-8"),
            request_nonce.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    return payload


def _unknown_kind_telemetry_service() -> UnknownKindTelemetry:
    global UNKNOWN_KIND_TELEMETRY
    if UNKNOWN_KIND_TELEMETRY is None:
        UNKNOWN_KIND_TELEMETRY = UnknownKindTelemetry(RuntimePaths.from_env())
    return UNKNOWN_KIND_TELEMETRY


@app.post("/api/agent-runtime/unknown-kind-telemetry/run")
async def run_unknown_kind_telemetry() -> dict[str, object]:
    """Run the weekly unknown-provider-kind sweep now."""

    try:
        return await asyncio.to_thread(_unknown_kind_telemetry_service().run_once)
    except Exception as exc:
        logger.exception("manual unknown-kind telemetry sweep failed")
        raise HTTPException(status_code=500, detail="telemetry sweep failed") from exc


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


@app.get("/api/gh/preview")
def gh_preview_card(
    request: Request,
    url: str = Query(..., min_length=1, max_length=300),
) -> Response:
    try:
        payload, etag = github_preview.get_github_preview(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except github_preview.GitHubPreviewFetchError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})

    headers = {
        "Cache-Control": f"private, max-age={github_preview.PREVIEW_CACHE_TTL_SECONDS}",
        "ETag": etag,
    }
    if github_preview.etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


class NoteLinks(BaseModel):
    outgoing: list[str]
    incoming: list[str]
    unresolved: list[str]


AGENT_REGISTRY_PATH = Path(os.environ.get("WIKI_AGENT_REGISTRY_PATH") or "/tmp/agent-registry.json")
AGENT_STATUS_DIR = Path(os.environ.get("WIKI_AGENT_STATUS_DIR") or "/tmp/agent-status")
_DEFAULT_RUNTIME_DIR = Path(
    os.environ.get("WIKI_AGENT_RUNTIME_DIR") or Path.home() / ".wiki" / "agent-runtime"
)
AGENT_RUNTIME_DIR = _DEFAULT_RUNTIME_DIR
AGENT_RUNS_DIR = Path(os.environ.get("WIKI_AGENT_RUNS_DIR") or AGENT_RUNTIME_DIR / "runs")
AGENT_VIEWED_PATH = Path(
    os.environ.get("WIKI_AGENT_VIEWED_PATH") or AGENT_RUNTIME_DIR / "agent-viewed.json"
)
AGENT_DEPLOY_MARKER_PATH = Path(
    os.environ.get("WIKI_AGENT_DEPLOY_MARKER_PATH")
    or AGENT_RUNTIME_DIR / "deploy-timestamp.txt"
)
# Populated at supervisor startup by :func:`_ensure_deploy_timestamp` and used
# by :func:`_resolve_viewed_fields` to classify legacy vs post-deploy runs.
_DEPLOY_TIMESTAMP: str | None = None
AGENT_ARCHIVE_DIR = Path(
    os.environ.get("WIKI_AGENT_ARCHIVE_DIR") or Path.home() / "me" / "fun" / "agent-archive"
)
AGENT_TMP_DIR = Path(os.environ.get("WIKI_AGENT_TMP_DIR") or "/tmp")
SUPERVISOR_CLIENT = SupervisorClient(RuntimePaths.from_env())

# These switches deliberately stay separate.  A route can move to the
# materialized view only after its adapter has proved safe in production.
_SQLITE_READ_ROUTES = {
    "session": ("WIKI_SQLITE_READ_SESSION", "session_adapter"),
    "delta": ("WIKI_SQLITE_READ_DELTA", "delta_adapter"),
}
_SQLITE_READ_FLAG_ENV = {
    route: flag for route, (flag, _adapter) in _SQLITE_READ_ROUTES.items()
}


@dataclass(frozen=True)
class SQLiteSourceKey:
    """Typed identity for one materialized read source."""

    source_class: str
    run_id: str
    rebuild_generation: int

    def format(self) -> str:
        return (
            f"sqlite://{self.source_class}/{self.run_id}/"
            f"rebuild-{self.rebuild_generation}"
        )

    @classmethod
    def parse(cls, value: str) -> "SQLiteSourceKey":
        match = re.fullmatch(
            r"sqlite://(?P<source_class>[^/]+)/(?P<run_id>[^/]+)/"
            r"rebuild-(?P<generation>[0-9]+)",
            value,
        )
        if match is None:
            raise ValueError(f"invalid SQLite source key: {value}")
        return cls(
            source_class=match.group("source_class"),
            run_id=match.group("run_id"),
            rebuild_generation=int(match.group("generation")),
        )


def _sqlite_read_enabled(route: str) -> bool:
    """Return whether one independently releasable SQLite read is enabled."""

    value = os.environ.get(_SQLITE_READ_ROUTES[route][0], "")
    return value.lower() in {"1", "true", "yes", "on"}


def _sqlite_event_store() -> SQLiteEventStore:
    return SQLiteEventStore(runtime_event_db_path(AGENT_RUNTIME_DIR), migrate=False)
RUN_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
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


def _normalize_kind(value: object) -> str | None:
    if value == "cc" or value == "claude":
        return "cc"
    if value == "cdx" or value == "codex":
        return "cdx"
    return None


def _normalize_provider(value: object) -> str | None:
    if value == "claude" or value == "cc":
        return "claude"
    if value == "codex" or value == "cdx":
        return "codex"
    return None


def _provider_for_kind(kind: str | None) -> str | None:
    if kind == "cc":
        return "claude"
    if kind == "cdx":
        return "codex"
    return None


def _kind_for_format(fmt: str | None) -> str | None:
    if fmt and fmt.startswith("claude"):
        return "cc"
    if fmt == "codex":
        return "cdx"
    return None


def _session_identity(
    entry: dict[str, Any] | None,
    *,
    fmt: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    model = None
    kind = _kind_for_format(fmt)
    provider = _provider_for_kind(kind)
    if isinstance(entry, dict):
        raw_model = entry.get("model")
        if isinstance(raw_model, str) and raw_model.strip():
            model = raw_model.strip()
        kind = _normalize_kind(entry.get("kind")) or kind or _normalize_kind(entry.get("provider"))
        provider = _normalize_provider(entry.get("provider")) or _provider_for_kind(kind)
    return (model, kind, provider)


def _session_identity_from_provider_inspector(
    provider_inspector: dict[str, object] | None,
    fallback: tuple[str | None, str | None, str | None],
) -> tuple[str | None, str | None, str | None]:
    model, kind, provider = fallback
    if not isinstance(provider_inspector, dict):
        return fallback
    raw_model = provider_inspector.get("model")
    if model is None and isinstance(raw_model, str) and raw_model.strip():
        model = raw_model.strip()
    kind = kind or _normalize_kind(provider_inspector.get("kind")) or _normalize_kind(
        provider_inspector.get("provider")
    )
    provider = provider or _normalize_provider(provider_inspector.get("provider")) or _provider_for_kind(kind)
    return (model, kind, provider)


def _validation_detail(exc: ValidationError) -> str:
    messages = []
    for error in exc.errors():
        message = error.get("msg")
        if isinstance(message, str):
            messages.append(message)
    return "; ".join(messages) or "Bad request body"


def _coerce_request_model(body: object, model_type: type[BaseModel]) -> BaseModel:
    if isinstance(body, model_type):
        return body
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")
    try:
        return model_type.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_validation_detail(exc)) from exc


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
    for candidate in reviewer_id_candidates(ticket):
        path = AGENT_STATUS_DIR / f"{candidate}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_mtime"] = path.stat().st_mtime
            return data
        except (OSError, ValueError):
            continue
    return None


def _agent_status_paths() -> Iterator[Path]:
    """Yield worker status files, excluding workgraph sidecars."""
    if not AGENT_STATUS_DIR.is_dir():
        return
    for path in sorted(AGENT_STATUS_DIR.glob("*.json")):
        if path.name.endswith(".workgraph.json"):
            continue
        yield path


ViewedEntry = dict[str, Any]  # {"seq": int, "at": str}


def _viewed_entry(value: Any) -> ViewedEntry | None:
    if not isinstance(value, dict):
        return None
    seq = value.get("seq")
    at = value.get("at")
    if not isinstance(seq, int) or seq < 0:
        return None
    if not isinstance(at, str):
        return None
    return {"seq": seq, "at": at}


@contextmanager
def _viewed_lock(exclusive: bool) -> Iterator[Any]:
    """Serialize read-modify-write cycles on the viewed store.

    A separate lock file (never truncated) keeps the lock intact while
    ``agent-viewed.json`` is atomically replaced.
    """

    AGENT_VIEWED_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = AGENT_VIEWED_PATH.with_suffix(AGENT_VIEWED_PATH.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_viewed_map_locked() -> dict[str, ViewedEntry]:
    """Caller MUST already hold the viewed lock."""

    try:
        data = json.loads(AGENT_VIEWED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, ViewedEntry] = {}
    for run_id, entry in data.items():
        if not isinstance(run_id, str) or run_id.startswith("_"):
            continue
        if not RUN_ID_PATTERN.fullmatch(run_id):
            continue
        parsed = _viewed_entry(entry)
        if parsed is None:
            continue
        result[run_id] = parsed
    return result


def _read_viewed_map() -> dict[str, ViewedEntry]:
    with _viewed_lock(exclusive=False):
        return _read_viewed_map_locked()


def _write_viewed_map_locked(data: dict[str, Any]) -> None:
    """Caller MUST already hold the viewed lock (exclusive)."""

    AGENT_VIEWED_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{AGENT_VIEWED_PATH.name}.",
        dir=AGENT_VIEWED_PATH.parent,
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, AGENT_VIEWED_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _isoformat_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _load_run_freshness(run_id: str | None) -> tuple[str | None, int | None]:
    """Read the durable ``updated_at`` + unread event count for a run.

    Returns ``(None, None)`` when the run has no supervisor record — legacy
    tmux workers, drift entries, or archived runs whose ``runs/<id>/run.json``
    was removed. Never falls back to status-file ``mtime`` (WIKI-147 R2 B2).
    """

    if not run_id or not RUN_ID_PATTERN.fullmatch(run_id):
        return None, None
    run_path = AGENT_RUNS_DIR / run_id / "run.json"
    try:
        payload = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    updated_at = payload.get("updated_at")
    # The unread-dot cursor skips synthetic supervisor events. Viewed cursors
    # use the normalized event sequence loaded separately below.
    seq = payload.get("unread_event_seq")
    if not isinstance(seq, int):
        seq = payload.get("normalized_event_count")
    if not isinstance(seq, int):
        seq = 0
    if not isinstance(updated_at, str):
        updated_at = None
    if not isinstance(seq, int):
        seq = None
    return updated_at, seq


def _load_run_normalized_seq(run_id: str | None) -> int | None:
    """Read the normalized event sequence used by viewed cursors."""

    if not run_id or not RUN_ID_PATTERN.fullmatch(run_id):
        return None
    run_path = AGENT_RUNS_DIR / run_id / "run.json"
    try:
        payload = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    seq = payload.get("normalized_event_count")
    return seq if isinstance(seq, int) else None


def _run_exists(run_id: str) -> bool:
    return (AGENT_RUNS_DIR / run_id / "run.json").is_file()


def _ensure_deploy_timestamp() -> str:
    """Return the wiki-supervisor deploy cutoff, initializing the marker if absent.

    Called from the FastAPI lifespan on boot so the cutoff is stamped BEFORE any
    request handling. Runs created before this timestamp are classified as
    "seen already" (pre-deploy backfill); runs created at/after render unread on
    first paint. Persisted to a marker file so restarts keep the same cutoff —
    a reboot must not silently re-classify runs.
    """

    global _DEPLOY_TIMESTAMP
    if _DEPLOY_TIMESTAMP is not None:
        return _DEPLOY_TIMESTAMP
    try:
        existing = AGENT_DEPLOY_MARKER_PATH.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        existing = ""
    if existing:
        _DEPLOY_TIMESTAMP = existing
        return existing
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    AGENT_DEPLOY_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{AGENT_DEPLOY_MARKER_PATH.name}.",
        dir=AGENT_DEPLOY_MARKER_PATH.parent,
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(now_iso)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, AGENT_DEPLOY_MARKER_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    _DEPLOY_TIMESTAMP = now_iso
    return now_iso


def _load_run_created_at(run_id: str) -> str | None:
    """Return ``created_at`` from ``runs/<id>/run.json`` or ``None``."""

    if not RUN_ID_PATTERN.fullmatch(run_id):
        return None
    try:
        payload = json.loads((AGENT_RUNS_DIR / run_id / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    created_at = payload.get("created_at")
    return created_at if isinstance(created_at, str) else None


def _baseline_path(run_id: str) -> Path:
    return AGENT_RUNS_DIR / run_id / "viewed-baseline.json"


_BASELINE_LOCKS: dict[str, threading.Lock] = {}
_BASELINE_LOCKS_GUARD = threading.Lock()


def _baseline_lock(run_id: str) -> threading.Lock:
    with _BASELINE_LOCKS_GUARD:
        lock = _BASELINE_LOCKS.get(run_id)
        if lock is None:
            lock = threading.Lock()
            _BASELINE_LOCKS[run_id] = lock
        return lock


def _load_run_baseline(run_id: str) -> tuple[str | None, int | None]:
    """Return ``(baseline_at, baseline_seq)`` for a pre-deploy run, if written."""

    try:
        payload = json.loads(_baseline_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    seq = payload.get("seq")
    at = payload.get("at")
    if not isinstance(seq, int) or seq < 0:
        return None, None
    if not isinstance(at, str):
        return None, None
    return at, seq


def _ensure_run_baseline(
    run_id: str,
    latest_at: str,
    latest_seq: int,
) -> tuple[str, int]:
    """Freeze a per-run viewed baseline on FIRST observation and return it.

    Idempotent: once ``runs/<id>/viewed-baseline.json`` exists, subsequent calls
    return the persisted value verbatim — future events with seq > baseline_seq
    then render as unread (WIKI-147 R4 B1). Writes atomically via a tempfile +
    ``os.replace`` so concurrent readers never see a half-written sidecar
    (WIKI-147 R5 B1). Concurrent first-writers are serialized on a per-run
    ``threading.Lock`` so exactly one writer freezes the snapshot; late-arrivers
    observe the persisted value on recheck and no-op (WIKI-147 R6 B1).
    """

    existing_at, existing_seq = _load_run_baseline(run_id)
    if existing_seq is not None and existing_at is not None:
        return existing_at, existing_seq
    with _baseline_lock(run_id):
        existing_at, existing_seq = _load_run_baseline(run_id)
        if existing_seq is not None and existing_at is not None:
            return existing_at, existing_seq
        path = _baseline_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"at": latest_at, "seq": latest_seq}, sort_keys=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return latest_at, latest_seq


def _snapshot_startup_baselines() -> None:
    """Freeze per-run viewed baselines for every pre-deploy run at boot.

    Invoked from the FastAPI lifespan BEFORE requests are served so events
    that land between startup and the first ``/api/agents`` call still render
    as unread — without this, request-time lazy baselining would classify
    those events as already-viewed. Runs created at/after the deploy cutoff
    keep a NULL baseline and their events render as unread naturally
    (WIKI-147 R5 B1).
    """

    if not AGENT_RUNS_DIR.is_dir():
        return
    deploy_at = _ensure_deploy_timestamp()
    try:
        entries = list(AGENT_RUNS_DIR.iterdir())
    except OSError:
        return
    for entry in entries:
        run_id = entry.name
        if not RUN_ID_PATTERN.fullmatch(run_id) or not entry.is_dir():
            continue
        if _baseline_path(run_id).is_file():
            continue
        created_at = _load_run_created_at(run_id)
        if created_at is not None and created_at >= deploy_at:
            continue
        latest_at, latest_seq = _load_run_freshness(run_id)
        if latest_at is None or latest_seq is None:
            continue
        try:
            _ensure_run_baseline(run_id, latest_at, latest_seq)
        except OSError:
            continue


def _resolve_viewed_fields(
    run_id: str | None,
    viewed_map: dict[str, ViewedEntry],
    current: dict[str, Any] | None = None,
) -> tuple[str | None, int | None, str | None, int | None]:
    """Return ``(latest_event_at, latest_event_seq, last_viewed_at, last_viewed_seq)``.

    Order of precedence for the viewed pair:
      1. Supervisor-owned entry in ``current`` (user marked the row viewed).
      2. Legacy entry in ``viewed_map`` (user marked the row viewed).
      3. Pre-deploy classification: the run was created before this supervisor's
         deploy cutoff (or the run predates the ``created_at`` schema) — freeze
         a per-run baseline at the first-observed durable seq so legacy sessions
         don't show a dot after upgrade AND future events land as unread.
         WIKI-147 R4 B1.
      4. Post-deploy new session (created_at >= deploy cutoff) — leave ``None``
         so any recorded event renders as unread.
    """

    latest_at, latest_seq = _load_run_freshness(run_id)
    if not run_id or latest_at is None or latest_seq is None:
        return latest_at, latest_seq, None, None
    if isinstance(current, dict):
        stored_seq = current.get("last_viewed_seq")
        stored_at = current.get("last_viewed_at")
        if isinstance(stored_seq, int) and stored_seq >= 0 and isinstance(stored_at, str):
            return latest_at, latest_seq, stored_at, stored_seq
    entry = viewed_map.get(run_id)
    if entry is not None:
        return latest_at, latest_seq, entry.get("at"), entry.get("seq")
    created_at = _load_run_created_at(run_id)
    deploy_at = _ensure_deploy_timestamp()
    if created_at is None or created_at < deploy_at:
        baseline_at, baseline_seq = _ensure_run_baseline(run_id, latest_at, latest_seq)
        return latest_at, latest_seq, baseline_at, baseline_seq
    return latest_at, latest_seq, None, None


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
        if (
            not session_dir.is_dir()
            or not match
            or not archive_is_committed(session_dir)
        ):
            continue
        y, mo, d, h, mi, s = map(int, match.groups())
        sessions.append((datetime(y, mo, d, h, mi, s).astimezone(), session_dir))
    return sorted(sessions, reverse=True)


def _archive_orch_hints() -> tuple[dict[str, str], set[str]]:
    """Build archive grouping hints without opening archived session bodies."""

    registry = _read_agent_registry()
    hints: dict[str, str] = {}
    orchestrators = {
        str(orch)
        for orch in (registry.get("_orchestrators") or {})
        if isinstance(orch, str)
    }
    # Seed buckets from the same orchestrator inventory exposed by list_agents:
    # both headless registry keys and entries explicitly registered with the
    # orchestrator role must get their own archive window, even with no live
    # workers carrying an ``orch`` field.
    for orch_id, _entry, _is_headless in _registered_orchestrators(registry):
        orchestrators.add(orch_id)
    for ticket, entry in registry.items():
        if not isinstance(ticket, str) or not isinstance(entry, dict):
            continue
        rows = [entry.get("current")]
        history = entry.get("history")
        if isinstance(history, list):
            rows.extend(history)
        for row in rows:
            if not isinstance(row, dict):
                continue
            orch = row.get("orch") or row.get("orchestrator_id")
            if isinstance(orch, str) and orch:
                hints[ticket] = orch
                orchestrators.add(orch)
                break

    # Headless archives are normally keyed with the same project prefix as
    # their orchestrator (WIKI -> wiki, PHO -> phoebe). This is only a cheap
    # grouping hint; the archived body remains authoritative after selection.
    for ticket in set(hints) | set(orchestrators):
        prefix = ticket.split("-", 1)[0].lower()
        matches = [
            orch
            for orch in orchestrators
            if orch.lower().startswith(prefix) or prefix.startswith(orch.lower())
        ]
        if len(matches) == 1:
            hints.setdefault(ticket, matches[0])
    return hints, orchestrators


def _bounded_archive_candidates(
    limit_per_orch: int,
    *,
    latest_per_ticket: bool,
) -> list[tuple[str, datetime, Path]]:
    """Select archive paths before reading any final-status or meta bodies."""

    hints, orchestrators = _archive_orch_hints()
    buckets: dict[str, list[tuple[tuple[float, int, str], str, datetime, Path]]] = {}
    if not AGENT_ARCHIVE_DIR.is_dir():
        return []
    for ticket_dir in AGENT_ARCHIVE_DIR.iterdir():
        if not ticket_dir.is_dir() or not TICKET_PATTERN.fullmatch(ticket_dir.name):
            continue
        sessions = _archive_sessions(ticket_dir)
        if latest_per_ticket and sessions:
            sessions = sessions[:1]
        orch = hints.get(ticket_dir.name)
        if orch is None:
            prefix = ticket_dir.name.split("-", 1)[0].lower()
            matches = [
                candidate
                for candidate in orchestrators
                if candidate.lower().startswith(prefix) or prefix.startswith(candidate.lower())
            ]
            orch = matches[0] if len(matches) == 1 else "unassigned"
        bucket = buckets.setdefault(orch, [])
        for archived_at, session_dir in sessions:
            try:
                mtime_ns = session_dir.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            key = (mtime_ns, archived_at.timestamp(), session_dir.name)
            bucket.append((key, ticket_dir.name, archived_at, session_dir))
        bucket.sort(key=lambda item: item[0], reverse=True)
        del bucket[limit_per_orch:]

    selected = [
        (ticket, archived_at, session_dir)
        for bucket in buckets.values()
        for _key, ticket, archived_at, session_dir in bucket
    ]
    selected.sort(key=lambda item: (item[1], item[2].name), reverse=True)
    return selected


def list_archived(
    limit: int | None = 20,
    *,
    latest_per_ticket: bool = False,
    limit_per_orch: int | None = None,
) -> list[dict]:
    """Archived sessions sorted by archived_at desc.

    latest_per_ticket=True dedups per ticket BEFORE `limit`, so the returned
    list surfaces every ticket rather than being truncated to a fixed
    session window. `limit=None` disables the cap. `limit_per_orch` keeps a
    bounded recent window for every orchestrator instead of applying one
    global cap.
    """
    if not AGENT_ARCHIVE_DIR.is_dir():
        return []
    entries: list[dict] = []
    if limit_per_orch is not None:
        candidates = _bounded_archive_candidates(
            limit_per_orch,
            latest_per_ticket=latest_per_ticket,
        )
    else:
        candidates = [
            (ticket_dir.name, archived_at, session_dir)
            for ticket_dir in AGENT_ARCHIVE_DIR.iterdir()
            if ticket_dir.is_dir() and TICKET_PATTERN.fullmatch(ticket_dir.name)
            for archived_at, session_dir in (
                _archive_sessions(ticket_dir)[:1]
                if latest_per_ticket
                else _archive_sessions(ticket_dir)
            )
        ]
    for ticket, archived_at, session_dir in candidates:
        status = _read_json_object(session_dir / "final-status.json")
        meta = _read_json_object(session_dir / "meta.json")
        try:
            run_value = json.loads((session_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            run_value = {}
        run = run_value if isinstance(run_value, dict) else {}
        worker = meta.get("worker") or {}
        kind = worker.get("kind")
        if not kind:
            if any(session_dir.glob("cdx-*")):
                kind = "cdx"
            elif any(session_dir.glob("cc-*")):
                kind = "cc"
        entries.append(
            {
                "ticket": ticket,
                "archived_at": archived_at.isoformat(),
                "run_id": run.get("run_id") if isinstance(run.get("run_id"), str) else None,
                "kind": kind,
                "role": worker.get("role") or _archive_role(session_dir),
                "orch": worker.get("orch"),
                "model": worker.get("model"),
                "outcome": meta.get("outcome"),
                "state": status.get("state"),
                "pr": status.get("pr"),
                "step": status.get("step"),
            }
        )
    entries.sort(key=lambda e: e["archived_at"], reverse=True)
    if limit_per_orch is not None:
        return entries
    if limit is None:
        return entries
    return entries[:limit]


def _archive_hint(
    ticket: str,
    archived_at: str | None = None,
    run_id: str | None = None,
) -> tuple[str | None, str | None, Path | None]:
    """(kind, spawned_at iso, session dir) for a ticket's archive.

    Without ``archived_at`` returns the newest archive (unchanged behavior).
    With ``archived_at`` and ``run_id`` returns the exact committed archive,
    or (None, None, None) for a stale or mismatched identifier.

    ``archived_at`` and ``run_id`` are the archive discriminators used by the
    session routes.
    """

    ticket_dir = AGENT_ARCHIVE_DIR / ticket
    if not ticket_dir.is_dir():
        return (None, None, None)
    sessions = _archive_sessions(ticket_dir)
    if not sessions:
        return (None, None, None)
    if archived_at is not None or run_id is not None:
        if archived_at is None or run_id is None:
            return (None, None, None)
        resolved = _archive_session_for_run_id(run_id)
        if resolved is None or resolved.parent.name != ticket:
            return (None, None, None)
        for candidate_at, session_dir in sessions:
            if candidate_at.isoformat() == archived_at and session_dir == resolved:
                kind = (
                    "cdx"
                    if any(session_dir.glob("cdx-*"))
                    else "cc"
                    if any(session_dir.glob("cc-*"))
                    else None
                )
                return (kind, candidate_at.isoformat(), session_dir)
        return (None, None, None)
    archived_at_dt, session_dir = sessions[0]
    kind = "cdx" if any(session_dir.glob("cdx-*")) else "cc" if any(session_dir.glob("cc-*")) else None
    return (kind, archived_at_dt.isoformat(), session_dir)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _archive_runtime_identity(
    archive_dir: Path,
    *,
    fallback_kind: str | None,
) -> tuple[dict[str, Any], str | None, str | None, str | None]:
    run = _read_json_object(archive_dir / "run.json")
    meta = _read_json_object(archive_dir / "meta.json")
    worker = meta.get("worker")
    entry = {**run, **(worker if isinstance(worker, dict) else {})}
    model, kind, provider = _session_identity(entry)
    kind = kind or fallback_kind
    provider = provider or _provider_for_kind(kind)
    return entry, model, kind, provider


def _supervisor_pid_is_alive() -> bool:
    """Avoid a queued supervisor RPC when annotating a registry snapshot."""

    try:
        raw_pid = SUPERVISOR_CLIENT.paths.pid_path.read_text(encoding="utf-8").strip()
        pid = int(raw_pid)
    except (AttributeError, OSError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _registered_orchestrators(registry: dict) -> list[tuple[str, dict, bool]]:
    """Return registered orchestrators as (id, entry, is_headless)."""

    entries: list[tuple[str, dict, bool]] = []
    seen_ids: set[str] = set()
    for ticket, entry in sorted(registry.items()):
        if ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if not isinstance(current, dict) or current.get("role") != "orchestrator":
            continue
        entries.append((ticket, current, _is_headless(current)))
        seen_ids.add(ticket)
    for orch_id, entry in sorted((registry.get("_orchestrators") or {}).items()):
        if orch_id in seen_ids or not isinstance(entry, dict):
            continue
        entries.append((orch_id, entry, False))
    return entries


def _derive_workspace_candidates() -> list[WorkspaceCandidate]:
    candidates: list[WorkspaceCandidate] = []
    seen_ids: set[str] = set()

    def add_candidate(workspace_id: str, raw_root: object, live: bool) -> None:
        if not isinstance(raw_root, str) or not workspace_id or workspace_id in seen_ids:
            return
        try:
            root = Path(raw_root).expanduser().resolve()
            root_stat = root.stat()
            if not stat.S_ISDIR(root_stat.st_mode):
                return
        except (OSError, RuntimeError, TypeError, ValueError):
            return
        seen_ids.add(workspace_id)
        candidates.append(
            WorkspaceCandidate(
                workspace=Workspace(id=workspace_id, root=str(root), live=live),
                root=root,
                device=root_stat.st_dev,
                inode=root_stat.st_ino,
            )
        )

    own_root = FILES_ROOT.resolve()
    add_candidate("wiki", str(own_root), True)
    registry = _read_agent_registry()
    orchestrators = _registered_orchestrators(registry)
    headless_entries = [entry for _id, entry, headless in orchestrators if headless]
    supervisor_alive = _supervisor_pid_is_alive() if headless_entries else False
    legacy_windows = {
        entry.get("window")
        for _id, entry, headless in orchestrators
        if not headless and isinstance(entry.get("window"), str)
    }
    live_windows = tmux_live_windows() if legacy_windows else set()
    for workspace_id, entry, headless in orchestrators:
        cwd = entry.get("worktree") or entry.get("cwd")
        if headless:
            live = supervisor_alive and entry.get("control_attached") is True
        else:
            live = entry.get("window") in live_windows
        add_candidate(workspace_id, cwd, live)

    candidates_by_root: dict[Path, list[WorkspaceCandidate]] = {}
    for candidate in candidates:
        candidates_by_root.setdefault(candidate.root, []).append(candidate)
    selected: list[WorkspaceCandidate] = []
    for root, grouped in candidates_by_root.items():
        if root == own_root:
            canonical = next(candidate for candidate in grouped if candidate.workspace.id == "wiki")
            selected.append(canonical)
        else:
            selected.append(min(grouped, key=lambda candidate: (not candidate.workspace.live, candidate.workspace.id)))
    return selected


def derive_workspaces() -> list[Workspace]:
    """Derive the server-side workspace allowlist from live runtime state."""

    return [candidate.workspace for candidate in _derive_workspace_candidates()]


def resolve_workspace(workspace_id: str) -> WorkspaceResolution:
    if not workspace_id or "/" in workspace_id or "\\" in workspace_id:
        raise HTTPException(status_code=404, detail="Unknown workspace")
    candidate = next(
        (item for item in _derive_workspace_candidates() if item.workspace.id == workspace_id),
        None,
    )
    if candidate is None or not candidate.workspace.live:
        raise HTTPException(status_code=404, detail=f"Workspace not found or inactive: {workspace_id}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(candidate.root, flags)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode) or (root_stat.st_dev, root_stat.st_ino) != (
            candidate.device,
            candidate.inode,
        ):
            os.close(root_fd)
            raise HTTPException(status_code=404, detail=f"Workspace changed while resolving: {workspace_id}")
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"Workspace not found or inactive: {workspace_id}") from exc
    return WorkspaceResolution(workspace=candidate.workspace, root=candidate.root, root_fd=root_fd)


@app.get("/api/workspaces", response_model=WorkspaceList)
def list_workspaces() -> WorkspaceList:
    return WorkspaceList(workspaces=derive_workspaces())


@app.get("/api/agents")
def agents(include_history: bool = False) -> dict[str, object]:
    registry: dict = {}
    registry_refreshed_at: str | None = None
    # Notice reconciliation compares against live registry identity, but the
    # registry file may be transiently missing (backend/supervisor startup
    # race), unreadable, or a non-object payload. On any of those failure
    # shapes we MUST leave the notice store alone — reconciling from {}
    # would treat every worker-scoped notice as archived and permanently
    # delete durable operator guidance.
    registry_loaded = False
    try:
        loaded = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            registry = loaded
            registry_loaded = True
            registry_refreshed_at = datetime.fromtimestamp(
                AGENT_REGISTRY_PATH.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat()
    except (OSError, ValueError):
        pass
    # Consume the durable legacy-migration marker before capturing the notice
    # revision. If the backend crashed between supervisor commit and the spawn
    # route's synchronous _publish_codex_worker_replaced call, the ticket-only
    # Codex notice would remain forever without this self-heal. The publish
    # is idempotent (apply_event returns False when there is nothing to
    # clear), so a marked ticket that already had its notice cleared incurs
    # no work and does not emit a refresh event. Running before we capture
    # notice_revision keeps the revision guard aligned with a snapshot that
    # already reflects the self-heal.
    if registry_loaded:
        for ticket, entry in registry.items():
            if ticket.startswith("_") or not isinstance(entry, dict):
                continue
            current = entry.get("current")
            if not isinstance(current, dict):
                continue
            if current.get("replaced_legacy_provider") == "codex":
                _publish_codex_worker_replaced(ticket)
    # Capture the notice revision BEFORE reading the registry. A failure
    # event that lands between the registry snapshot and the reconcile
    # call would otherwise carry a run_id or ticket that live_runs does
    # not know about, and reconcile_with_live would delete the fresh
    # notice. Passing this revision to reconcile makes it a no-op when
    # something landed after the snapshot; the next refresh reconciles
    # from a paired pair.
    notice_revision = ACCOUNT_NOTICES.revision

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
    supervisor_alive = _supervisor_pid_is_alive() if headless_ids else False
    supervisor_health: dict[str, object] = {
        "status": (
            "not-needed"
            if not headless_ids
            else "snapshot"
            if supervisor_alive
            else "degraded"
        ),
        "runs": len(headless_ids),
    }
    if headless_ids:
        # This route must remain responsive while run/start awaits a provider.
        # The runtime atomically projects its durable state into this registry,
        # so listing intentionally never waits on the supervisor socket.
        supervisor_health["liveness"] = "snapshot" if supervisor_alive else "unavailable"
        supervisor_health["snapshot_refreshed_at"] = registry_refreshed_at
        if not supervisor_alive:
            supervisor_health["detail"] = "supervisor PID is absent or not running"
    now = datetime.now(tz=timezone.utc).timestamp()
    viewed_map = _read_viewed_map()
    workers = []
    orchestrators = []
    seen_tickets: set[str] = set()
    # Live-run identity for notice reconciliation. Provider identity matters
    # when a ticket moves from Codex to Claude during replacement.
    live_runs: dict[str, str | None] = {}
    live_providers: dict[str, str | None] = {}

    for ticket, entry in sorted(registry.items()):
        if ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current") or {}
        if not isinstance(current, dict):
            continue
        headless = _is_headless(current)
        runtime = current if headless else {}
        runtime_state = runtime.get("state") if headless else None
        control_attached = (
            supervisor_alive and runtime.get("control_attached") is True
            if headless
            else False
        )
        status = read_agent_status(ticket)
        seen_tickets.add(ticket)
        live_runs[ticket] = current.get("run_id") if isinstance(current.get("run_id"), str) else None
        current_kind = _normalize_kind(current.get("kind"))
        live_providers[ticket] = _normalize_provider(current.get("provider")) or _provider_for_kind(current_kind)
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
                    "state_reason": runtime.get("state_reason") if headless else None,
                    "control_attached": control_attached,
                    "provider_session_id": current.get("provider_session_id"),
                    "provider_pid": runtime.get("provider_pid") if headless else None,
                    "cwd": current.get("worktree") or current.get("cwd"),
                    "kind": current.get("kind"),
                    "model": current.get("model"),
                    "effort": current.get("effort"),
                    "spawned_at": current.get("spawned_at"),
                    "transcript_exists": bool(
                        isinstance(transcript, str) and Path(transcript).is_file()
                    ),
                    "log": current.get("log"),
                }
            )
            continue
        (
            latest_event_at,
            latest_event_seq,
            last_viewed_at,
            last_viewed_seq,
        ) = _resolve_viewed_fields(current.get("run_id"), viewed_map, current)
        workers.append(
            {
                "ticket": ticket,
                "registered": True,
                "window": current.get("window"),
                "window_alive": window_alive,
                "run_id": current.get("run_id"),
                "runtime_state": runtime_state,
                "state_reason": runtime.get("state_reason") if headless else None,
                "control_attached": control_attached,
                "provider_session_id": current.get("provider_session_id"),
                "provider_pid": runtime.get("provider_pid") if headless else None,
                "kind": current.get("kind"),
                "role": current.get("role"),
                "auto_archive": current.get("auto_archive"),
                "model": current.get("model"),
                "desired_model": current.get("desired_model"),
                "effort": current.get("effort"),
                "worktree": current.get("worktree"),
                "log": current.get("log"),
                "orch": current.get("orch"),
                "session": current.get("session"),
                "spawned_at": current.get("spawned_at"),
                **({"history": entry.get("history", [])} if include_history else {}),
                "state": (status or {}).get("state") or runtime_state,
                "pr": (status or {}).get("pr"),
                "step": (status or {}).get("step"),
                "blocker": (status or {}).get("blocker"),
                "status_age_seconds": (
                    int(now - status["_mtime"]) if status else None
                ),
                "latest_event_at": latest_event_at,
                "latest_event_seq": latest_event_seq,
                "last_viewed_at": last_viewed_at,
                "last_viewed_seq": last_viewed_seq,
            }
        )

    # Status files without a registry entry — skill drift, surface flagged.
    if AGENT_STATUS_DIR.is_dir():
        for path in _agent_status_paths():
            ticket = path.stem
            if ticket in seen_tickets:
                continue
            status = read_agent_status(ticket)
            # Drift entries have no run_id -> no durable freshness signal
            # and no unread indicator (matches "derive from durable runtime
            # events" contract; WIKI-147 R2 B2).
            (
                latest_event_at,
                latest_event_seq,
                last_viewed_at,
                last_viewed_seq,
            ) = _resolve_viewed_fields(None, viewed_map)
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
                    **({"history": []} if include_history else {}),
                    "state": (status or {}).get("state"),
                    "pr": (status or {}).get("pr"),
                    "step": (status or {}).get("step"),
                    "blocker": (status or {}).get("blocker"),
                    "status_age_seconds": (
                        int(now - status["_mtime"]) if status else None
                    ),
                    "latest_event_at": latest_event_at,
                    "latest_event_seq": latest_event_seq,
                    "last_viewed_at": last_viewed_at,
                    "last_viewed_seq": last_viewed_seq,
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
                "state_reason": None,
                "control_attached": False,
                "provider_session_id": orch.get("session_id"),
                "provider_pid": None,
                "cwd": orch.get("cwd"),
                "kind": orch.get("kind") or "cc",
                "model": orch.get("model"),
                "effort": orch.get("effort"),
                "spawned_at": orch.get("spawned_at"),
                "transcript_exists": bool(transcript and Path(transcript).is_file()),
                "log": orch.get("log"),
            }
        )

    # Reconcile worker-scoped notices against the live registry. Only run
    # when the registry loaded as a valid object — a missing / unreadable /
    # non-object registry would otherwise pass {} here and permanently
    # delete every notice. Replace and archive flows publish only
    # session/agents events, which the notice store correctly ignores;
    # without this reconciliation, banners for removed tickets would remain
    # forever. WIKI-228 will drive this from durable supervisor events
    # instead of an ambient snapshot check.
    if registry_loaded:
        ACCOUNT_NOTICES.reconcile_with_live(
            live_runs,
            live_providers=live_providers,
            expected_revision=notice_revision,
        )

    return {
        "workers": workers,
        "orchestrators": orchestrators,
        "archived": list_archived(),
        "supervisor": supervisor_health,
        "account_notices": ACCOUNT_NOTICES.snapshot(),
    }


class MarkViewedBody(BaseModel):
    seq: int | None = None


@app.post("/api/agents/runs/{run_id}/viewed")
def mark_run_viewed(run_id: str, body: MarkViewedBody | None = None) -> dict[str, Any]:
    """Server-authoritative "mark viewed" keyed by durable run id.

    Body ``seq`` is the frontend's observed ``latest_event_seq``. The server
    only accepts it if the run's current durable seq is at least that high,
    and never overwrites a stored ``seq`` with a lower one (monotonic).

    Returns 404 for run ids that do not exist in the runtime store — this
    is the WIKI-147 R2 H1 contract.
    """

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Bad run id")
    if not _run_exists(run_id):
        raise HTTPException(status_code=404, detail="Unknown run id")

    current_at, current_unread_seq = _load_run_freshness(run_id)
    current_seq = _load_run_normalized_seq(run_id)
    if current_at is None or current_seq is None:
        raise HTTPException(status_code=404, detail="Run has no durable state")

    requested_seq = body.seq if body and body.seq is not None else current_seq
    if requested_seq < 0:
        raise HTTPException(status_code=400, detail="seq must be non-negative")
    # Client cannot claim to have seen events the server has not observed.
    accepted_seq = min(requested_seq, current_seq)

    # Headless runs persist viewed state in the supervisor-owned run record.
    # The file-backed path below remains for legacy and isolated test runs.
    if AGENT_RUNS_DIR.resolve() == SUPERVISOR_CLIENT.paths.runs_dir.resolve():
        result = _supervisor_request(
            "run/mark_viewed",
            {"run_id": run_id, "seq": accepted_seq},
        )
        if isinstance(result, dict):
            return {
                "run_id": run_id,
                "last_viewed_at": result.get("last_viewed_at"),
                "last_viewed_seq": result.get("last_viewed_seq"),
                "latest_event_at": result.get("updated_at", current_at),
                "latest_event_seq": result.get(
                    "unread_event_seq", current_unread_seq
                ),
            }

    with _viewed_lock(exclusive=True):
        viewed_map = _read_viewed_map_locked()
        prior = viewed_map.get(run_id)
        # Monotonic: never overwrite a newer seq with an older one.
        if prior is not None and prior.get("seq", 0) >= accepted_seq:
            return {
                "run_id": run_id,
                "last_viewed_at": prior["at"],
                "last_viewed_seq": prior["seq"],
                "latest_event_at": current_at,
                "latest_event_seq": current_unread_seq,
            }
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        viewed_map[run_id] = {"seq": accepted_seq, "at": now_iso}
        merged: dict[str, Any] = dict(viewed_map)
        # Preserve the migration marker if present without letting it
        # collide with a run entry (leading underscore already reserved
        # by _read_viewed_map_locked).
        try:
            raw = json.loads(AGENT_VIEWED_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(key, str) and key.startswith("_"):
                    merged[key] = value
        _write_viewed_map_locked(merged)
    return {
        "run_id": run_id,
        "last_viewed_at": now_iso,
        "last_viewed_seq": accepted_seq,
        "latest_event_at": current_at,
        "latest_event_seq": current_unread_seq,
    }


@app.get("/api/dashboard/tickets")
def dashboard_tickets() -> dict[str, object]:
    registry: dict = {}
    try:
        value = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            registry = value
    except (OSError, ValueError):
        pass
    statuses: dict[str, dict] = {}
    for path in _agent_status_paths():
        status = read_agent_status(path.stem)
        if status:
            statuses[path.stem] = status
    return dashboard.build_payload(
        registry, statuses, list_archived(limit=None, latest_per_ticket=True)
    )


@app.get("/api/palette/search")
async def palette_search(
    request: Request,
    q: str = "",
    limit: int = palette.DEFAULT_LIMIT,
    mode: str = "lexical",
) -> dict[str, object]:
    if mode not in {"lexical", "semantic"}:
        raise HTTPException(status_code=422, detail="mode must be lexical or semantic")
    agents_payload = agents()

    # Palette walk (session index + vault stat + artifact scan) runs in the
    # threadpool. Concurrently, poll `is_disconnected()` from the event loop
    # so the walk aborts if the client cancelled mid-scan.
    cancelled = asyncio.Event()

    async def watch_disconnect() -> None:
        try:
            while not cancelled.is_set():
                if await request.is_disconnected():
                    cancelled.set()
                    return
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return

    def _run() -> list[dict[str, object]]:
        workers = (
            agents_payload.get("workers", [])
            if isinstance(agents_payload, dict)
            else []
        )
        ticket_by_run = {
            str(worker["run_id"]): str(worker["ticket"])
            for worker in workers
            if isinstance(worker, dict)
            and isinstance(worker.get("run_id"), str)
            and isinstance(worker.get("ticket"), str)
        }
        archive_by_run: dict[str, tuple[str, str]] = {}
        archived = agents_payload.get("archived", []) if isinstance(agents_payload, dict) else []
        all_archived = list_archived(limit=None)
        for entry in all_archived:
            if not isinstance(entry, dict):
                continue
            run_id = entry.get("run_id")
            ticket = entry.get("ticket")
            archived_at = entry.get("archived_at")
            if (
                isinstance(run_id, str)
                and isinstance(ticket, str)
                and isinstance(archived_at, str)
            ):
                ticket_by_run[run_id] = ticket
                archive_by_run[run_id] = (ticket, archived_at)
        for entry in archived:
            if not isinstance(entry, dict):
                continue
            run_id = entry.get("run_id")
            ticket = entry.get("ticket")
            if isinstance(run_id, str) and isinstance(ticket, str):
                ticket_by_run[run_id] = ticket
        indexed_artifacts = palette.collect_artifact_items_from_index(
            _sqlite_event_store(),
            ticket_by_run=ticket_by_run,
            archive_by_run=archive_by_run,
        )
        palette_agents_payload = (
            dict(agents_payload) if isinstance(agents_payload, dict) else {}
        )
        palette_agents_payload["archived"] = all_archived
        return palette.search(
            q,
            limit,
            agents_payload=palette_agents_payload,
            vault_dir=VAULT_DIR,
            runs_dir=SUPERVISOR_CLIENT.paths.runs_dir,
            archive_dir=AGENT_ARCHIVE_DIR,
            artifact_items=indexed_artifacts,
            should_cancel=cancelled.is_set,
        )

    watcher = asyncio.create_task(watch_disconnect())
    try:
        results = await asyncio.to_thread(_run)
    except palette.PaletteCancelled:
        raise HTTPException(status_code=499, detail="client closed request")
    finally:
        cancelled.set()
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    if mode == "lexical":
        return {"mode": mode, "results": results}

    def _semantic() -> dict[str, object]:
        index = knowledge.KnowledgeIndex.from_env(
            runtime_dir=RuntimePaths.from_env().runtime_dir,
            archive_dir=AGENT_ARCHIVE_DIR,
            vault_dir=VAULT_DIR,
        )
        try:
            payload = index.search_semantic(q, limit=limit)
        except knowledge.KnowledgeQueryError:
            return {
                "results": [],
                "semantic": index.semantic_status(),
                "rebuilding": index.rebuilding,
                "stale": True,
            }
        except knowledge.KnowledgeError as exc:
            logger.warning("semantic palette search unavailable: %s", exc)
            return {
                "results": [],
                "semantic": {
                    "available": False,
                    "model": None,
                    "reason": f"semantic search unavailable: {exc}",
                },
                "rebuilding": True,
                "stale": True,
            }
        return payload

    semantic = await asyncio.to_thread(_semantic)
    semantic_status = semantic.get("semantic")
    semantic_is_available = bool(
        isinstance(semantic_status, dict) and semantic_status.get("available")
    )
    semantic_results = [
        {
            "kind": "note",
            "id": f"semantic:{row['path']}",
            "title": row.get("title") or row["path"],
            "subtitle": row.get("snippet") or row["path"],
            "url": f"#/note/{row['path']}",
            "updated_at": None,
            "score": row.get("score", 0),
        }
        for row in semantic.get("semantic_results", semantic.get("results", []))
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    ] if semantic_is_available else []
    return {
        "mode": mode,
        "results": results,
        "lexical_results": results,
        "semantic_results": semantic_results,
        "semantic_available": semantic_is_available,
        "semantic_unavailable_reason": (
            semantic_status.get("reason")
            if isinstance(semantic_status, dict)
            else "semantic search unavailable"
        ),
        "rebuilding": semantic.get("rebuilding", False),
        "stale": semantic.get("stale", False),
    }


@app.get("/api/knowledge/search")
async def knowledge_search(
    q: str,
    mode: str = "lexical",
    ticket: str | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    if mode not in {"lexical", "semantic"}:
        raise HTTPException(status_code=422, detail="mode must be lexical or semantic")

    def _run() -> dict[str, object]:
        index = knowledge.KnowledgeIndex.from_env()
        if mode == "semantic":
            return index.search_semantic(q, ticket=ticket, limit=limit)
        return index.search(q, ticket=ticket, kind=kind, limit=limit)

    try:
        return await asyncio.to_thread(_run)
    except knowledge.KnowledgeQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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


def _load_workgraph_payload(ticket: str) -> tuple[dict[str, object], str, str | None]:
    try:
        graph = workgraph.load_workgraph(ticket, AGENT_STATUS_DIR)
    except workgraph.WorkgraphCorruptError as exc:
        snapshot = workgraph.load_snapshot(ticket)
        if snapshot is not None:
            return snapshot, "snapshot", str(exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if graph is not None:
        return graph, "live", None
    graph = workgraph.load_snapshot(ticket)
    if graph is not None:
        return graph, "snapshot", None
    raise HTTPException(status_code=404, detail="No workgraph found for this ticket")


def _fleet_worker_metadata(registry: dict) -> dict[str, dict[str, object]]:
    """Return current worker identity keyed by the exact worker ticket."""

    workers: dict[str, dict[str, object]] = {}
    for ticket, entry in registry.items():
        if ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if not isinstance(current, dict) or current.get("role") == "orchestrator":
            continue
        status = read_agent_status(ticket) or {}
        workers[ticket] = {
            "ticket": ticket,
            "orch": current.get("orch"),
            "state": status.get("state") or current.get("runtime_state") or "working",
            "role": current.get("role") or "worker",
            "kind": current.get("kind") or "unknown",
        }
    return workers


def _fleet_edge_endpoint(
    endpoint: object,
    node_tickets: dict[str, str],
    orch: str,
) -> str | None:
    if not isinstance(endpoint, str) or not endpoint:
        return None
    if endpoint in node_tickets:
        return node_tickets[endpoint]
    if endpoint == f"orch:{orch}" or endpoint.startswith("orch:"):
        return endpoint
    if endpoint.startswith("monitor:"):
        return endpoint
    return endpoint


def _fleet_graph_ticket(
    *,
    graph: dict[str, object],
    source: str,
    worker_metadata: dict[str, dict[str, object]],
    archived_metadata: dict[str, dict[str, object]],
    allowed_tickets: set[str],
) -> list[dict[str, object]]:
    """Flatten one validated workgraph into worker nodes and normalized edges."""

    orch = str(graph.get("orch") or "unassigned")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return []

    node_tickets: dict[str, str] = {}
    worker_nodes: list[tuple[dict[str, object], str]] = []
    active_node_ids: set[str] = set()
    try:
        graph_module = workgraph
        active_node_ids = {
            str(node.get("id"))
            for node in graph_module._live_worker_nodes(graph)  # noqa: SLF001
            if isinstance(node, dict) and isinstance(node.get("id"), str)
        }
    except Exception:
        active_node_ids = set()

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        if node.get("kind") == "orchestrator":
            node_tickets[node_id] = f"orch:{orch}"
            continue
        if node.get("kind") == "monitor":
            node_tickets[node_id] = node_id
            continue
        ticket = node.get("worker_id") or node_id
        if not isinstance(ticket, str) or not ticket:
            continue
        node_tickets[node_id] = ticket
        if ticket in allowed_tickets:
            worker_nodes.append((node, ticket))

    active_node_tickets = {
        node_tickets.get(node_id, node_id) for node_id in active_node_ids
    }
    edges_by_ticket: dict[str, list[dict[str, object]]] = {ticket: [] for _, ticket in worker_nodes}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        from_node = _fleet_edge_endpoint(edge.get("from"), node_tickets, orch)
        to_node = _fleet_edge_endpoint(edge.get("to"), node_tickets, orch)
        if from_node is None or to_node is None:
            continue
        worker_endpoint_tickets = [
            node_tickets[endpoint]
            for endpoint in (edge.get("from"), edge.get("to"))
            if isinstance(endpoint, str)
            and endpoint in node_tickets
            and not node_tickets[endpoint].startswith(("orch:", "monitor:"))
        ]
        if any(ticket not in allowed_tickets for ticket in worker_endpoint_tickets):
            continue
        active = (
            source == "live"
            and edge.get("kind") != "archive"
            and all(endpoint in active_node_tickets for endpoint in worker_endpoint_tickets)
        )
        normalized: dict[str, object] = {
            "kind": edge.get("kind", "unknown"),
            "from": from_node,
            "to": to_node,
            "created_at": edge.get("created_at", ""),
            "active": active,
        }
        if isinstance(edge.get("payload"), dict):
            normalized["payload"] = edge["payload"]
        for ticket in {from_node, to_node}:
            if ticket in edges_by_ticket:
                edges_by_ticket[ticket].append(normalized)

    result: list[dict[str, object]] = []
    for node, ticket in worker_nodes:
        metadata = worker_metadata.get(ticket) or archived_metadata.get(ticket) or {}
        result.append(
            {
                "ticket": ticket,
                "state": metadata.get("state")
                or ("working" if node.get("id") in active_node_ids else "archived"),
                "role": metadata.get("role") or node.get("kind") or "worker",
                "kind": metadata.get("kind") or "unknown",
                "edges": edges_by_ticket.get(ticket, []),
            }
        )
    return result


def _fleet_graph_payload(limit: int = 10) -> dict[str, object]:
    registry = _read_agent_registry()
    worker_metadata = _fleet_worker_metadata(registry)
    archived_entries = (
        list_archived(limit=limit, latest_per_ticket=True, limit_per_orch=limit)
        if limit
        else []
    )
    archived_metadata: dict[str, dict[str, object]] = {}
    graph_tickets: set[str] = set()
    for entry in archived_entries:
        ticket = entry.get("ticket")
        if not isinstance(ticket, str):
            continue
        archived_metadata[ticket] = entry
        graph_tickets.add(base_ticket(ticket))
    graph_tickets.update(base_ticket(ticket) for ticket in worker_metadata)

    groups: dict[str, dict[str, dict[str, object]]] = {}
    for ticket, metadata in worker_metadata.items():
        orch = str(metadata.get("orch") or "unassigned")
        groups.setdefault(orch, {})[ticket] = {
            "ticket": ticket,
            "state": metadata.get("state") or "working",
            "role": metadata.get("role") or "worker",
            "kind": metadata.get("kind") or "unknown",
            "edges": [],
        }
    for ticket, metadata in archived_metadata.items():
        orch = str(metadata.get("orch") or "unassigned")
        groups.setdefault(orch, {}).setdefault(
            ticket,
            {
                "ticket": ticket,
                "state": metadata.get("state") or "archived",
                "role": metadata.get("role") or "worker",
                "kind": metadata.get("kind") or "unknown",
                "edges": [],
            },
        )

    for graph_ticket in sorted(graph_tickets):
        graph, source = graph_health.load_validated_graph(
            graph_ticket,
            status_dir=AGENT_STATUS_DIR,
        )
        if graph is None:
            continue
        orch = str(graph.get("orch") or "unassigned")
        allowed_tickets = {
            ticket
            for ticket, metadata in worker_metadata.items()
            if base_ticket(ticket) == graph_ticket
            and (metadata.get("orch") is None or str(metadata.get("orch")) == orch)
        }
        allowed_tickets.update(
            ticket
            for ticket, metadata in archived_metadata.items()
            if base_ticket(ticket) == graph_ticket
            and (metadata.get("orch") is None or str(metadata.get("orch")) == orch)
        )
        tickets = _fleet_graph_ticket(
            graph=graph,
            source=source or "snapshot",
            worker_metadata=worker_metadata,
            archived_metadata=archived_metadata,
            allowed_tickets=allowed_tickets,
        )
        group = groups.setdefault(orch, {})
        for ticket in tickets:
            ticket_id = str(ticket["ticket"])
            for other_orch, other_group in groups.items():
                if other_orch != orch:
                    other_group.pop(ticket_id, None)
            group[ticket_id] = ticket

    return {
        "groups": [
            {"orch": orch, "tickets": [group[ticket] for ticket in sorted(group)]}
            for orch, group in sorted(groups.items())
            if group
        ],
        "updated_at_ns": time.time_ns(),
    }


@app.get("/api/fleet/graph")
@app.get("/fleet/graph", include_in_schema=False)
def fleet_graph(limit: int = Query(default=10, ge=0, le=50)) -> dict[str, object]:
    """Return one bounded, normalized DAG view across the worker fleet."""

    return _fleet_graph_payload(limit)


MAX_SCREENCAST_TICKETS = 32


def _screencast_etag(workers: list[dict[str, object]]) -> str:
    """Stable ETag over (ticket, run_id, frame texts) — no timestamp churn.

    Excluding ``updated_at_ns`` from the hash is the point: if the tail
    of every worker's raw.jsonl is unchanged since the last poll, the
    ETag must match so we can return 304 and skip the JSON body. Frames
    are already sanitized / clipped, so hashing their dicts is
    deterministic. Digest is truncated to 16 hex chars — plenty for
    cache-key uniqueness across the worker fleet.
    """

    import hashlib

    hasher = hashlib.blake2b(digest_size=8)
    for worker in workers:
        hasher.update(str(worker.get("ticket") or "").encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(worker.get("run_id") or "").encode("utf-8"))
        hasher.update(b"\x00")
        for frame in worker.get("frames") or []:
            if not isinstance(frame, dict):
                continue
            hasher.update(str(frame.get("kind") or "").encode("utf-8"))
            hasher.update(b"\x1e")
            hasher.update(str(frame.get("text") or "").encode("utf-8"))
            hasher.update(b"\x1f")
        hasher.update(b"\n")
    return f'W/"{hasher.hexdigest()}"'


def _run_id_for_ticket(registry: dict, ticket: str) -> str | None:
    """Resolve the run id currently registered for ``ticket``.

    Screencasts are always live-only: they read the currently-running
    worker's raw.jsonl. Archived runs are intentionally excluded — the strip
    is a "what is happening now" pane, not a history browser.
    """

    if not TICKET_PATTERN.fullmatch(ticket):
        return None
    entry = registry.get(ticket)
    if not isinstance(entry, dict):
        return None
    current = entry.get("current")
    if not isinstance(current, dict):
        return None
    run_id = current.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        return None
    return run_id


def _screencast_payload(tickets: list[str]) -> dict[str, object]:
    """Batched tail read → per-ticket frame lists in one call.

    The fleet view must not poll per worker; it sends the set of visible
    tickets and gets one bounded, ETag-friendly response. Unknown or
    archived tickets return an empty frames list — the caller keeps its
    existing empty-state UI.
    """

    registry = _read_agent_registry()
    runs_root = SUPERVISOR_CLIENT.paths.runs_dir
    workers: list[dict[str, object]] = []
    seen: set[str] = set()
    root_fd: int | None = None
    try:
        root_fd = open_root_directory(runs_root)
    except OSError:
        root_fd = None
    try:
        for ticket in tickets:
            if ticket in seen or len(workers) >= MAX_SCREENCAST_TICKETS:
                continue
            seen.add(ticket)
            run_id = _run_id_for_ticket(registry, ticket)
            frames: list[screencast.ScreencastFrame] = []
            if run_id is not None and root_fd is not None:
                try:
                    frames = screencast.tail_frames(root_fd, run_id)
                except OSError:
                    frames = []
            workers.append(
                {
                    "ticket": ticket,
                    "run_id": run_id,
                    "frames": [screencast.frame_to_dict(frame) for frame in frames],
                }
            )
    finally:
        if root_fd is not None:
            os.close(root_fd)
    return {"workers": workers, "updated_at_ns": time.time_ns()}


@app.get("/api/fleet/screencast", response_model=None)
@app.get("/fleet/screencast", include_in_schema=False, response_model=None)
def fleet_screencast(
    request: Request,
    response: Response,
    ticket: list[str] = Query(default_factory=list),
) -> Response | dict[str, object]:
    """Return short tail-of-raw.jsonl frames for the requested worker tickets.

    Single call for all visible workers on the fleet view. Each worker's
    frame list is a bounded read of the last window of its raw.jsonl —
    never a full-file scan — parsed into short lines suitable for the
    monospace strip.

    Supports conditional GET: the ETag is derived from the stable
    (ticket, run_id, frame text) tuples only — never from wall-clock
    timestamps — so an unchanged fleet returns 304 with no body. The
    ~2 s poll only pays the JSON cost when a worker actually made
    progress.
    """

    payload = _screencast_payload(ticket)
    etag = _screencast_etag(payload["workers"])  # type: ignore[arg-type]
    inm = request.headers.get("if-none-match")
    if inm and etag in {value.strip() for value in inm.split(",")}:
        headers = {
            "ETag": etag,
            "Cache-Control": "no-cache, max-age=1",
        }
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache, max-age=1"
    return payload


@app.get("/api/agents/{ticket}/workgraph")
def agent_workgraph(ticket: str, revision: int | None = None) -> dict[str, object]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    selected_revision: int | None = None
    if revision is None:
        graph, source, warning = _load_workgraph_payload(ticket)
    else:
        selected = workgraph.load_snapshot_revision(ticket, revision)
        latest_snapshot_revision = workgraph.latest_snapshot_revision(ticket)
        if selected is not None and (
            selected[0] == revision or revision < latest_snapshot_revision
        ):
            selected_revision, graph = selected
            source, warning = "snapshot", None
        else:
            # The live graph is the freshest representation of the latest
            # revision when its durable snapshot is unavailable.
            try:
                graph = workgraph.load_workgraph(ticket, AGENT_STATUS_DIR)
            except workgraph.WorkgraphCorruptError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            if graph is None:
                raise HTTPException(status_code=404, detail="No workgraph found for this ticket")
            source, warning = "live", None
            revisions = workgraph.snapshot_revisions(ticket)
            selected_revision = revisions[-1]["revision"] if revisions else revision
    current = workgraph.current_health(graph)
    # Refresh the stored health so the renderer shows now-relative stall — the
    # same clock and computation the health endpoint uses.
    graph["composite_health"] = current["health"]
    loop_state = derive_loop_state(graph)
    payload: dict[str, object] = {
        "ok": True,
        "source": source,
        "workgraph": graph,
        "health": current["health"],
        "alarms": current["alarms"],
        "computed_at": current["computed_at"],
        "loop_state": loop_state.to_json(),
    }
    if selected_revision is not None:
        payload["revision"] = selected_revision
    if warning:
        payload["warning"] = warning
    return payload


@app.get("/api/agents/{ticket}/workgraph/revisions")
def agent_workgraph_revisions(ticket: str) -> list[dict[str, int]]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    return workgraph.snapshot_revisions(ticket)


@app.get("/api/agents/{ticket}/workgraph/health")
def agent_workgraph_health(ticket: str, cap: int = workgraph.DEFAULT_ITERATION_CAP) -> dict[str, object]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    graph, source, warning = _load_workgraph_payload(ticket)
    current = workgraph.current_health(graph, iteration_cap=cap)
    payload: dict[str, object] = {
        "ok": True,
        "source": source,
        "health": current["health"],
        "alarms": current["alarms"],
        "computed_at": current["computed_at"],
    }
    if warning:
        payload["warning"] = warning
    return payload


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
_session_question_overlays: dict[str, dict[str, Any]] = {}
_RAW_OVERLAY_TAIL_BYTES = 256 * 1024


def _direct_transcript_session(path: Path) -> tuple[str, Path] | None:
    if not path.is_file():
        return None
    fmt = transcripts.detect_session_format(path) or "codex"
    return (fmt, path)


def _tail_json_lines(path: Path, *, limit_bytes: int = _RAW_OVERLAY_TAIL_BYTES) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            offset = max(0, size - limit_bytes)
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return []
    if offset > 0:
        first_newline = chunk.find(b"\n")
        if first_newline < 0:
            return []
        chunk = chunk[first_newline + 1 :]
    rows: list[dict[str, Any]] = []
    for line in chunk.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _payload_tool_result_ids(payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    message = payload.get("message")
    if not isinstance(message, dict):
        return ids
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        if isinstance(tool_use_id, str) and tool_use_id:
            ids.add(tool_use_id)
    return ids


def _parse_pending_question_overlay(
    raw_path: Path,
    *,
    seen_tool_use_ids: set[str],
) -> list[dict[str, Any]]:
    rows = _tail_json_lines(raw_path)
    if not rows:
        return []
    open_indices: dict[int, str] = {}
    pending: dict[str, dict[str, Any]] = {}
    resolved_ids: set[str] = set()
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        resolved_ids.update(_payload_tool_result_ids(payload))
        stream_event = payload.get("event") if payload.get("type") == "stream_event" else None
        if not isinstance(stream_event, dict):
            continue
        event_type = stream_event.get("type")
        if event_type == "content_block_start":
            block = stream_event.get("content_block")
            index = stream_event.get("index")
            if not isinstance(block, dict) or not isinstance(index, int):
                continue
            if block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
                continue
            tool_use_id = block.get("id") or block.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                continue
            open_indices[index] = tool_use_id
            entry: dict[str, Any] = {
                "tool_use_id": tool_use_id,
                "seq": int(row.get("seq") or 0),
                "ts": payload.get("timestamp") or row.get("received_at"),
                "partial_json": "",
                "questions": [],
            }
            raw_input = block.get("input")
            if isinstance(raw_input, dict) and isinstance(raw_input.get("questions"), list):
                entry["questions"] = raw_input["questions"]
            pending[tool_use_id] = entry
            continue
        if event_type == "content_block_delta":
            index = stream_event.get("index")
            tool_use_id = open_indices.get(index) if isinstance(index, int) else None
            if tool_use_id is None:
                continue
            delta = stream_event.get("delta")
            if not isinstance(delta, dict) or delta.get("type") != "input_json_delta":
                continue
            entry = pending.get(tool_use_id)
            if entry is None:
                continue
            entry["partial_json"] += str(delta.get("partial_json") or "")
            try:
                parsed = json.loads(entry["partial_json"])
            except ValueError:
                continue
            questions = parsed.get("questions") if isinstance(parsed, dict) else None
            if isinstance(questions, list):
                entry["questions"] = questions
            continue
        if event_type == "content_block_stop":
            index = stream_event.get("index")
            if isinstance(index, int):
                open_indices.pop(index, None)
    events: list[dict[str, Any]] = []
    for entry in sorted(pending.values(), key=lambda item: (int(item["seq"]), str(item["tool_use_id"]))):
        tool_use_id = entry["tool_use_id"]
        if tool_use_id in seen_tool_use_ids or tool_use_id in resolved_ids:
            continue
        questions = entry.get("questions")
        if not isinstance(questions, list) or not questions:
            continue
        events.extend(transcripts.build_question_events(entry.get("ts"), questions, tool_use_id))
    return events


def _overlay_signature(events: list[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple(
        (
            event.get("tool_use_id"),
            event.get("ts"),
            event.get("text"),
            (event.get("question") or {}).get("header"),
            tuple((event.get("question") or {}).get("options") or []),
        )
        for event in events
    )


def _assign_overlay_ids(events: list[dict[str, Any]], start_id: int) -> list[dict[str, Any]]:
    assigned: list[dict[str, Any]] = []
    next_id = start_id
    for event in events:
        assigned_event = dict(event)
        assigned_event["question"] = dict(event.get("question") or {})
        assigned_event["id"] = next_id
        next_id += 1
        assigned.append(assigned_event)
    return assigned


def _copy_overlay_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    for event in events:
        copied_event = dict(event)
        copied_event["question"] = dict(event.get("question") or {})
        copied.append(copied_event)
    return copied


def _overlay_pending_questions(
    delta: dict[str, Any],
    *,
    fmt: str,
    transcript_path: Path,
    raw_path: Path | None,
    client_cursor: int,
) -> dict[str, Any]:
    if fmt != "claude" or raw_path is None or not raw_path.is_file():
        return delta
    current = transcripts.read_session_events(fmt, transcript_path)
    existing_tool_use_ids = {
        question_tool_use_id
        for event in current["events"]
        if event.get("kind") == "question"
        for question_tool_use_id in [
            (event.get("question") or {}).get("tool_use_id") or event.get("tool_use_id")
        ]
        if isinstance(question_tool_use_id, str)
    }
    overlay_events = _parse_pending_question_overlay(
        raw_path,
        seen_tool_use_ids=existing_tool_use_ids,
    )
    cache_key = f"{transcript_path}::{raw_path}"
    cached = _session_question_overlays.get(cache_key)
    transcript_cursor = int(current.get("cursor", delta.get("cursor", 0)))
    signature = _overlay_signature(overlay_events)

    if not overlay_events and cached is None:
        return delta

    if (
        cached is not None
        and signature == cached.get("signature")
        and transcript_cursor == int(cached.get("transcript_cursor", -1))
    ):
        combined_cursor = int(cached["cursor"])
        assigned_overlay = _copy_overlay_events(cached.get("events") or [])
    else:
        prior_cursor = int(cached.get("cursor", transcript_cursor)) if cached is not None else transcript_cursor
        combined_cursor = max(transcript_cursor, prior_cursor) + 1
        max_existing_id = max(
            (int(event.get("id", -1)) for event in current["events"]),
            default=-1,
        )
        assigned_overlay = _assign_overlay_ids(overlay_events, max_existing_id + 1)
        _session_question_overlays[cache_key] = {
            "signature": signature,
            "events": _copy_overlay_events(assigned_overlay),
            "transcript_cursor": transcript_cursor,
            "cursor": combined_cursor,
        }

    if (
        cached is not None
        and not assigned_overlay
        and client_cursor >= combined_cursor
        and transcript_cursor >= combined_cursor
    ):
        _session_question_overlays.pop(cache_key, None)
        return delta

    total = int(current["base"]) + len(current["events"]) + len(assigned_overlay)
    if client_cursor == combined_cursor:
        return {
            **delta,
            "base": int(current["base"]),
            "cursor": combined_cursor,
            "tail_from": total,
            "events": [],
            "patches": [],
        }

    combined_base = int(current["base"])
    combined_events = [*current["events"], *assigned_overlay]
    window_base = max(combined_base, total - transcripts.TAIL_WINDOW_EVENTS)
    return {
        "events": combined_events[window_base - combined_base :],
        "base": window_base,
        "tokens": current.get("tokens"),
        "tasks": current.get("tasks") or [],
        "pr": current.get("pr"),
        "session_meta": current.get("session_meta") or {},
        "dispositions": current.get("dispositions") or {"rendered": 0, "summarized": 0, "ignored": 0, "unknown": 0},
        "cursor": combined_cursor,
        "tail_from": window_base,
        "patches": [],
        "has_older": window_base > combined_base,
    }


def _enrich_session_delta(
    delta: dict[str, Any],
    *,
    fmt: str,
    transcript_path: Path | None,
    raw_path: Path | None,
    client_cursor: int,
) -> dict[str, Any]:
    """Apply the one v2 enrichment pipeline after either read adapter.

    SQLite and transcript readers deliberately produce only the raw v2 delta.
    This keeps question controls, child links, and inspect data identical when
    a route changes source.
    """

    enriched = dict(delta)
    events = list(delta.get("events") or [])
    if fmt == "claude" and transcript_path is not None:
        events = transcripts.annotate_agent_events(transcript_path, events)
    enriched["events"] = events
    if transcript_path is not None:
        enriched = _overlay_pending_questions(
            enriched,
            fmt=fmt,
            transcript_path=transcript_path,
            raw_path=raw_path,
            client_cursor=client_cursor,
        )
    return enriched


def _compose_session_payload(
    raw_delta: dict[str, Any],
    *,
    fmt: str,
    source_path: Path | None,
    raw_path: Path | None,
    client_cursor: int,
    source_key: str,
    model: str | None,
    desired_model: str | None,
    kind: str | None,
    provider: str | None,
    working: bool,
    include_subagents: bool,
    include_queue: bool,
    ticket: str | None,
    provider_inspector: dict[str, object] | None = None,
    composer_messages: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    """Compose one final v2 payload after either source adapter reads raw data."""

    enriched = _enrich_session_delta(
        raw_delta,
        fmt=fmt,
        transcript_path=source_path,
        raw_path=raw_path,
        client_cursor=client_cursor,
    )
    payload: dict[str, object] = {
        "version": 2,
        "format": fmt,
        "path": source_key,
        "tokens": enriched.get("tokens"),
        "tasks": enriched.get("tasks") or [],
        "pr": enriched.get("pr"),
        "session_meta": enriched.get("session_meta") or {},
        "dispositions": enriched.get("dispositions")
        or {"rendered": 0, "summarized": 0, "ignored": 0, "unknown": 0},
        "base": enriched.get("base", 0),
        "cursor": enriched.get("cursor", 0),
        "tail_from": enriched.get("tail_from", 0),
        "events": enriched.get("events") or [],
        "patches": enriched.get("patches") or [],
        "working": working,
        "model": model,
        "desired_model": desired_model,
        "kind": kind,
        "provider": provider,
    }
    if "has_older" in enriched:
        payload["has_older"] = bool(enriched["has_older"])
    if include_subagents:
        payload["subagents"] = _active_subagents(source_path) if source_path else []
    if include_queue:
        payload["queue"] = _queue_messages(ticket) if ticket else []
    if provider_inspector is not None:
        payload["provider_inspector"] = provider_inspector
    if composer_messages is None and provider_inspector is not None:
        candidate = provider_inspector.get("composer_messages")
        if isinstance(candidate, list):
            composer_messages = candidate
    if composer_messages is not None:
        payload["composer_messages"] = composer_messages
    return payload


def _sqlite_session_payload(
    run_id: str,
    *,
    fmt: str,
    cursor: int,
    client_path: str | None,
    model: str | None,
    desired_model: str | None,
    kind: str | None,
    provider: str | None,
    working: bool,
    include_subagents: bool = False,
    include_queue: bool = False,
    ticket: str | None = None,
    tail_window: bool = True,
    tail_events: int | None = None,
    source_class: str = "live",
    transcript_path: Path | None = None,
    raw_path: Path | None = None,
    provider_inspector: dict[str, object] | None = None,
    composer_messages: list[dict[str, Any]] | None = None,
) -> dict[str, object] | None:
    """Build the v2 session view from a ready materialized run.

    ``path`` is already the v2 source identity.  Changing it from a native or
    archive path to this key makes old clients take the established full-reset
    branch without adding a new payload field.
    """

    try:
        event_store = _sqlite_event_store()
        snapshot = event_store.read_session_snapshot(
            run_id,
            source_class=source_class,
            after_cursor=cursor,
        )
    except Exception:
        return None

    try:
        typed_source_key = SQLiteSourceKey.parse(snapshot.source_key)
    except (AttributeError, TypeError, ValueError):
        return None
    if snapshot.state.rebuild_state != "ready":
        return None
    if (
        typed_source_key.run_id != run_id
        or typed_source_key.source_class != source_class
    ):
        return None
    source_key = typed_source_key.format()
    effective_cursor = cursor if client_path == source_key else 0
    state = snapshot.state
    projection = snapshot.projection
    events = list(snapshot.events)
    patches = list(snapshot.patches)

    # A cursor advance on the same source is a true delta.  Source changes
    # (including a rebuild generation change) are the only normal reset.
    full_reset = (
        effective_cursor <= 0
        or effective_cursor > state.change_cursor
        or effective_cursor < state.patch_base_cursor
    )
    event_base = state.event_base
    total = event_base + len(events)
    if full_reset:
        window_size = tail_events
        if window_size is None and tail_window:
            window_size = transcripts.TAIL_WINDOW_EVENTS
        window_base = max(event_base, total - window_size) if window_size else event_base
        event_slice = events[window_base - event_base :]
        patch_payload: list[dict[str, Any]] = []
        tail_from = window_base
    else:
        changed_events = [patch.patch["event"] for patch in patches if "event" in patch.patch]
        tail_from = min((int(event["id"]) for event in changed_events), default=total)
        event_slice = [event for event in events if int(event.get("id", -1)) >= tail_from]
        patch_payload = [
            {"id": patch.event_id, **patch.patch}
            for patch in patches
            if "event" not in patch.patch
        ]

    try:
        tokens = json.loads(projection[7]) if projection[7] else None
        tasks = json.loads(projection[1])
        pr = json.loads(projection[2]) if projection[2] else None
        session_meta = json.loads(projection[3])
        dispositions = json.loads(projection[6])
    except (TypeError, ValueError):
        return None
    raw_delta: dict[str, Any] = {
        "events": event_slice,
        "base": window_base if full_reset else event_base,
        "tokens": tokens,
        "tasks": tasks,
        "pr": pr,
        "session_meta": session_meta,
        "dispositions": dispositions,
        "cursor": state.change_cursor,
        "tail_from": tail_from,
        "patches": patch_payload,
    }
    if full_reset:
        raw_delta["has_older"] = window_base > event_base
    return _compose_session_payload(
        raw_delta,
        fmt=fmt,
        source_path=transcript_path,
        raw_path=raw_path,
        client_cursor=effective_cursor,
        source_key=source_key,
        model=model,
        desired_model=desired_model,
        kind=kind,
        provider=provider,
        working=working,
        include_subagents=include_subagents,
        include_queue=include_queue,
        ticket=ticket,
        provider_inspector=provider_inspector,
        composer_messages=composer_messages,
    )


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
    payload = dict(result)
    model, kind, provider = _session_identity(resolved[2])
    payload["model"] = payload.get("model") or model
    payload["kind"] = payload.get("kind") or kind
    payload["provider"] = payload.get("provider") or provider
    return payload


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


def _open_runs_root_fd_or_404() -> int:
    try:
        return replay.open_runs_root_fd(AGENT_RUNS_DIR)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Runs root missing") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Runs root unreadable: {exc}") from exc


def _validate_run_id_or_400(run_id: str) -> None:
    if not replay.valid_run_id(run_id):
        raise HTTPException(status_code=400, detail="Bad run id")


def _archive_session_for_run_id(run_id: str) -> Path | None:
    """Find the committed archive session for one exact run id."""

    for session_dir in AGENT_ARCHIVE_DIR.glob("*/*"):
        if not archive_is_committed(session_dir):
            continue
        run = _read_json_object(session_dir / "run.json")
        if run.get("run_id") == run_id:
            return session_dir
    return None


def _open_archived_replay_run(run_id: str) -> int:
    archive_dir = _archive_session_for_run_id(run_id)
    if archive_dir is None:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        return replay.open_run_dir_fd(archive_dir)
    except replay.ReplayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _archived_replay_runs(ticket: str) -> list[replay.RunSummary]:
    ticket_dir = AGENT_ARCHIVE_DIR / ticket
    if not ticket_dir.is_dir():
        return []
    summaries: list[replay.RunSummary] = []
    for session_dir in sorted(ticket_dir.glob("*"), reverse=True):
        if not archive_is_committed(session_dir):
            continue
        run_id = _read_json_object(session_dir / "run.json").get("run_id")
        if not isinstance(run_id, str) or not replay.valid_run_id(run_id):
            continue
        try:
            run_fd = replay.open_run_dir_fd(session_dir)
            try:
                summary = replay.build_run_summary_from_run_fd(run_fd, run_id)
            finally:
                os.close(run_fd)
        except replay.ReplayError:
            continue
        if summary.agent_id == ticket:
            summaries.append(summary)
    return summaries


@app.get("/api/agents/{ticket}/replay/runs")
def agent_replay_runs(ticket: str) -> dict[str, object]:
    if not TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    try:
        runs_root_fd = _open_runs_root_fd_or_404()
    except HTTPException:
        listing = None
    else:
        try:
            listing = replay.resolve_ticket_runs(runs_root_fd, ticket)
        finally:
            os.close(runs_root_fd)
    runs = list(listing.runs) if listing is not None else []
    seen_run_ids = {run.run_id for run in runs}
    for archived in _archived_replay_runs(ticket):
        if archived.run_id not in seen_run_ids:
            runs.append(archived)
            seen_run_ids.add(archived.run_id)
    runs_truncated = (
        (listing.truncated if listing is not None else False)
        or len(runs) > replay.MAX_RUN_LIST_ENTRIES
    )
    runs = runs[: replay.MAX_RUN_LIST_ENTRIES]
    return {
        "ticket": ticket,
        "runs": [run.as_dict() for run in runs],
        "runs_truncated": runs_truncated,
    }


@app.get("/api/agent-runs/{run_id}/replay/timeline")
def agent_run_replay_timeline(
    run_id: str,
    cursor: str | None = None,
    limit: int = replay.DEFAULT_LIMIT,
) -> dict[str, object]:
    _validate_run_id_or_400(run_id)
    if limit < 1 or limit > replay.MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {replay.MAX_LIMIT}",
        )
    try:
        runs_root_fd = _open_runs_root_fd_or_404()
    except HTTPException:
        archived_fd = _open_archived_replay_run(run_id)
        try:
            return replay.build_timeline_response_from_run_fd(
                archived_fd, run_id, cursor=cursor, limit=limit
            )
        except replay.ReplayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        finally:
            os.close(archived_fd)
    try:
        try:
            replay.verify_run_dir_exists(runs_root_fd, run_id)
        except replay.ReplayError:
            os.close(runs_root_fd)
            runs_root_fd = -1
            runs_root_fd = _open_archived_replay_run(run_id)
            return replay.build_timeline_response_from_run_fd(
                runs_root_fd, run_id, cursor=cursor, limit=limit
            )
        return replay.build_timeline_response(
            runs_root_fd, run_id, cursor=cursor, limit=limit
        )
    except replay.ReplayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    finally:
        if runs_root_fd >= 0:
            os.close(runs_root_fd)


@app.get("/api/agent-runs/{run_id}/replay/events/{seq}")
def agent_run_replay_event(run_id: str, seq: int) -> dict[str, object]:
    if seq <= 0:
        raise HTTPException(status_code=400, detail="Seq must be positive")
    _validate_run_id_or_400(run_id)
    try:
        runs_root_fd = _open_runs_root_fd_or_404()
    except HTTPException:
        archived_fd = _open_archived_replay_run(run_id)
        try:
            entry = replay.load_raw_event_from_run_fd(archived_fd, run_id, seq)
        finally:
            os.close(archived_fd)
    else:
        try:
            try:
                replay.verify_run_dir_exists(runs_root_fd, run_id)
            except replay.ReplayError:
                os.close(runs_root_fd)
                runs_root_fd = -1
                runs_root_fd = _open_archived_replay_run(run_id)
                entry = replay.load_raw_event_from_run_fd(runs_root_fd, run_id, seq)
            else:
                entry = replay.load_raw_event(runs_root_fd, run_id, seq)
        except replay.ReplayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        finally:
            if runs_root_fd >= 0:
                os.close(runs_root_fd)
    if entry is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"run_id": run_id, "seq": seq, "raw": entry}


def _session_delta_payload(
    fmt: str,
    path: Path,
    *,
    cursor: int,
    client_path: str | None = None,
    ticket: str | None = None,
    include_subagents: bool = False,
    include_queue: bool = False,
    headless_current: dict[str, Any] | None = None,
    model: str | None = None,
    desired_model: str | None = None,
    kind: str | None = None,
    provider: str | None = None,
    run_id: str | None = None,
    tail_window: bool = True,
    tail_events: int | None = None,
    read_route: str | None = None,
    provider_inspector: dict[str, object] | None = None,
    composer_messages: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    effective_cursor = 0 if client_path is not None and client_path != str(path) else cursor
    result = transcripts.read_session_delta(
        fmt,
        path,
        effective_cursor,
        tail_window=tail_window,
        tail_events=tail_events,
        annotate_agents=False,
    )
    raw_path: Path | None = None
    if isinstance(headless_current, dict) and _is_headless(headless_current):
        raw_log = headless_current.get("log")
        raw_path = Path(raw_log) if isinstance(raw_log, str) else None
    if provider_inspector is None and ticket:
        provider_inspector = _provider_events(ticket, limit=50)
    payload = _compose_session_payload(
        result,
        fmt=fmt,
        source_path=path,
        raw_path=raw_path,
        client_cursor=effective_cursor,
        source_key=str(path),
        model=model,
        desired_model=desired_model,
        kind=kind,
        provider=provider,
        working=_transcript_working(path, ticket),
        include_subagents=include_subagents,
        include_queue=include_queue and bool(ticket and valid_agent_id(ticket)),
        ticket=ticket,
        provider_inspector=provider_inspector,
        composer_messages=composer_messages,
    )
    if read_route is not None and read_route not in {"delta", "session"}:
        raise ValueError(f"unknown session read route: {read_route}")
    if read_route == "delta" and _sqlite_read_enabled("delta") and isinstance(run_id, str):
        sqlite_payload = _sqlite_session_payload(
            run_id,
            fmt=fmt,
            cursor=cursor,
            client_path=client_path,
            model=model,
            desired_model=desired_model,
            kind=kind,
            provider=provider,
            working=bool(payload["working"]),
            include_subagents=include_subagents,
            include_queue=include_queue,
            ticket=ticket,
            tail_window=tail_window,
            tail_events=tail_events,
            transcript_path=path,
            raw_path=raw_path,
            provider_inspector=provider_inspector,
            composer_messages=composer_messages,
        )
        if sqlite_payload is not None:
            return sqlite_payload
    return payload


def _archived_events_payload(
    archive_dir: Path,
    *,
    cursor: int,
    client_path: str | None,
    fallback_kind: str | None,
) -> dict[str, object] | None:
    path = archive_dir / "events.jsonl"
    if not path.is_file():
        return None
    entry, model, kind, provider = _archive_runtime_identity(
        archive_dir,
        fallback_kind=fallback_kind,
    )
    if provider not in {"codex", "claude"}:
        return None
    source_format = f"{provider}-normalized"
    effective_cursor = 0 if client_path is not None and client_path != str(path) else cursor
    result = transcripts.read_session_delta(source_format, path, effective_cursor)
    payload = _compose_session_payload(
        result,
        fmt="provider-events",
        source_path=path,
        raw_path=None,
        client_cursor=effective_cursor,
        source_key=str(path),
        model=model,
        desired_model=entry.get("desired_model"),
        kind=kind,
        provider=provider,
        working=False,
        include_subagents=False,
        include_queue=False,
        ticket=None,
    )
    payload["subagents"] = []
    payload["queue"] = []
    return payload


def _legacy_source_for_sqlite_run(ticket: str, run_id: str) -> tuple[str, Path] | None:
    """Find the old source when an independent route remains unflipped."""

    resolved = _registry_agent(_read_agent_registry(), ticket)
    if resolved is not None:
        current = resolved[2]
        if current.get("run_id") == run_id:
            transcript = current.get("transcript")
            if isinstance(transcript, str):
                found = _direct_transcript_session(Path(transcript))
                if found is not None:
                    return found
            found = _session_paths.get(ticket)
            if found is not None and found[1].is_file():
                return found
            found = transcripts.find_session(
                _session_identity(current)[1],
                ticket,
                current.get("spawned_at"),
                current.get("session_id") if isinstance(current.get("session_id"), str) else None,
                current.get("worktree"),
            )
            if found is not None:
                return found
    archive_dir = _archive_session_for_run_id(run_id)
    if archive_dir is not None:
        _entry, _model, _kind, provider = _archive_runtime_identity(archive_dir)
        path = archive_dir / "events.jsonl"
        if provider in {"codex", "claude"} and path.is_file():
            return f"{provider}-normalized", path
    return None


@app.get("/api/agents/{ticket}/session")
def agent_session(
    ticket: str,
    cursor: int = Query(0, ge=0),
    client_path: str | None = Query(None, alias="path"),
    archived_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")

    requested_archive = archived_at is not None or run_id is not None
    archive_dir: Path | None = None
    archive_kind: str | None = None
    spawned_at: str | None = None
    archive_model: str | None = None
    archive_provider: str | None = None
    if requested_archive:
        archive_kind, spawned_at, archive_dir = _archive_hint(ticket, archived_at, run_id)
        if archive_dir is None:
            raise HTTPException(status_code=404, detail="Archived session not found")
        _archive_entry, archive_model, archive_kind, archive_provider = (
            _archive_runtime_identity(archive_dir, fallback_kind=archive_kind)
        )

    registry: dict = {}
    try:
        registry = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    orch = (registry.get("_orchestrators") or {}).get(ticket)
    if not requested_archive and orch and orch.get("transcript"):
        found = _direct_transcript_session(Path(orch["transcript"]))
        if found is not None:
            fmt, path = found
            model, kind, provider = _session_identity(
                orch if isinstance(orch, dict) else None,
                fmt=fmt,
            )
            return _session_delta_payload(
                fmt,
                path,
                cursor=cursor,
                client_path=client_path,
                ticket=ticket,
                include_subagents=fmt == "claude",
                include_queue=True,
                headless_current=orch if isinstance(orch, dict) else None,
                model=model,
                desired_model=(
                    orch.get("desired_model") if isinstance(orch, dict) else None
                ),
                kind=kind,
                provider=provider,
            )
        raise HTTPException(status_code=404, detail="Orchestrator transcript missing")

    current = (registry.get(ticket) or {}).get("current") or {}
    current_model, current_kind, current_provider = _session_identity(
        current if isinstance(current, dict) else None
    )
    spawned_at = current.get("spawned_at")
    registry_session_id = current.get("session_id") if isinstance(current.get("session_id"), str) else None
    if requested_archive:
        current = {}
        current_model = archive_model
        current_kind = archive_kind
        current_provider = archive_provider
    elif not current:
        archive_kind, spawned_at, archive_dir = _archive_hint(ticket, archived_at)
        current_kind = current_kind or archive_kind

    current_run_id = current.get("run_id") if isinstance(current, dict) else None
    found = None
    if not requested_archive and isinstance(current, dict) and _is_headless(current):
        transcript_hint = current.get("transcript")
        if isinstance(transcript_hint, str):
            found = _direct_transcript_session(Path(transcript_hint))
            if found is not None:
                _session_paths[ticket] = found
    if not requested_archive and found is None:
        found = _session_paths.get(ticket)
    if not requested_archive and (found is None or not found[1].is_file()):
        found = transcripts.find_session(
            current_kind,
            ticket,
            spawned_at,
            registry_session_id,
            current.get("worktree"),
        )
        if found:
            _session_paths[ticket] = found
    if found is None:
        if archive_dir is None and isinstance(current, dict):
            current_run_id = current.get("run_id")
            if isinstance(current_run_id, str):
                archive_dir = _archive_session_for_run_id(current_run_id)
        if isinstance(current, dict) and _is_headless(current):
            provider_inspector = _provider_events(ticket, limit=50)
            if _sqlite_read_enabled("session") and isinstance(current_run_id, str):
                sqlite_payload = _sqlite_session_payload(
                    current_run_id,
                    fmt=current_provider or "provider-events",
                    cursor=cursor,
                    client_path=client_path,
                    model=current_model,
                    desired_model=current.get("desired_model"),
                    kind=current_kind,
                    provider=current_provider,
                    working=_transcript_working(Path(current.get("log") or "."), ticket),
                    include_queue=True,
                    ticket=ticket,
                    provider_inspector=provider_inspector,
                )
                if sqlite_payload is not None:
                    return sqlite_payload
            if provider_inspector is None:
                raise HTTPException(
                    status_code=503,
                    detail="Supervisor event inspector is unavailable",
                )
            model, kind, provider = _session_identity_from_provider_inspector(
                provider_inspector,
                (current_model, current_kind, current_provider),
            )
            return _compose_session_payload(
                {
                    "events": [],
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
                    "patches": [],
                },
                fmt="provider-events",
                source_path=None,
                raw_path=None,
                client_cursor=0,
                source_key=f"provider://{current['run_id']}",
                model=model,
                desired_model=current.get("desired_model"),
                kind=kind,
                provider=provider,
                working=_transcript_working(Path(current.get("log") or "."), ticket),
                include_subagents=False,
                include_queue=True,
                ticket=ticket,
                provider_inspector=provider_inspector,
            )
        # Native transcript gone (cleanup) — the archive owns a durable,
        # normalized provider stream for headless runs.
        if archive_dir is not None:
            archived = _archived_events_payload(
                archive_dir,
                cursor=cursor,
                client_path=client_path,
                fallback_kind=current_kind,
            )
            if archived is not None:
                return archived
            # Pre-headless tmux archives have no normalized event stream.
            logs = sorted(archive_dir.glob("*.log"), key=lambda p: p.stat().st_size, reverse=True)
            if logs:
                tail = clean_pane_log(logs[0])
                _, model, kind, provider = _archive_runtime_identity(
                    archive_dir,
                    fallback_kind=current_kind,
                )
                kind = kind or current_kind
                provider = provider or _provider_for_kind(kind)
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
                    "model": model,
                    "kind": kind,
                    "provider": provider,
                }
        raise HTTPException(status_code=404, detail="No session transcript found")

    fmt, path = found
    model = current_model
    kind = current_kind or _kind_for_format(fmt)
    provider = current_provider or _provider_for_kind(kind)
    provider_inspector = (
        _provider_events(ticket, limit=50)
        if isinstance(current, dict) and _is_headless(current)
        else None
    )
    legacy_payload = _session_delta_payload(
        fmt,
        path,
        cursor=cursor,
        client_path=client_path,
        ticket=ticket,
        include_subagents=fmt == "claude",
        include_queue=True,
        headless_current=current if isinstance(current, dict) and _is_headless(current) else None,
        model=model,
        desired_model=current.get("desired_model") if isinstance(current, dict) else None,
        kind=kind,
        provider=provider,
        run_id=current_run_id if isinstance(current_run_id, str) and _is_headless(current) else None,
        read_route="session",
        provider_inspector=provider_inspector,
    )
    if (
        _sqlite_read_enabled("session")
        and isinstance(current_run_id, str)
        and _is_headless(current)
    ):
        sqlite_payload = _sqlite_session_payload(
            current_run_id,
            fmt=fmt,
            cursor=cursor,
            client_path=client_path,
            model=model,
            desired_model=current.get("desired_model") if isinstance(current, dict) else None,
            kind=kind,
            provider=provider,
            working=bool(legacy_payload["working"]),
            include_subagents=fmt == "claude",
            include_queue=True,
            ticket=ticket,
            transcript_path=path,
            raw_path=Path(current["log"]) if isinstance(current.get("log"), str) else None,
            provider_inspector=provider_inspector,
        )
        if sqlite_payload is not None:
            return sqlite_payload
    return legacy_payload


@app.get("/api/agents/{ticket}/session/older")
def agent_session_older(
    ticket: str,
    before: int = Query(..., ge=0),
    count: int = Query(500, ge=1, le=2_000),
    archived_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    session = agent_session(
        ticket,
        cursor=0,
        client_path=None,
        archived_at=archived_at,
        run_id=run_id,
    )
    fmt = session.get("format")
    raw_path = session.get("path")
    if not isinstance(raw_path, str):
        raise HTTPException(status_code=409, detail="Older transcript events are unavailable")
    if raw_path.startswith("sqlite://"):
        try:
            source_key = SQLiteSourceKey.parse(raw_path)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Invalid SQLite source key") from exc
        sqlite_run_id = source_key.run_id
        legacy_source = _legacy_source_for_sqlite_run(ticket, sqlite_run_id)
        if legacy_source is None:
            raise HTTPException(
                status_code=409,
                detail="Older transcript events are unavailable",
            )
        fmt, path = legacy_source
        if fmt.endswith("-normalized"):
            fmt = "provider-events"
        raw_path = str(path)
    else:
        path = Path(raw_path)
    if fmt == "provider-events":
        if raw_path.startswith("provider://") or not path.is_file():
            raise HTTPException(
                status_code=409,
                detail="Older transcript events are unavailable",
            )
        provider = session.get("provider")
        if provider not in {"codex", "claude"}:
            raise HTTPException(
                status_code=409,
                detail="Older transcript events are unavailable",
            )
        result = transcripts.read_older_session(
            f"{provider}-normalized",
            path,
            before,
            count,
        )
    elif fmt in {"codex", "claude"}:
        # Endpoint transaction rule: annotation runs inside the read's
        # path-lock span, never on a released snapshot.
        result = transcripts.read_older_session(
            fmt, path, before, count, annotate_agents=fmt == "claude"
        )
    else:
        raise HTTPException(status_code=409, detail="Older transcript events are unavailable")
    events = result["events"]
    return {
        "version": 2,
        "format": fmt,
        "path": raw_path,
        "base": result["base"],
        "events": events,
        "has_older": result["has_older"],
    }


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
    limit: int | None = Query(None, ge=1, le=1000),
) -> dict[str, object]:
    if not valid_agent_id(ticket) or not SUBAGENT_ID_PATTERN.fullmatch(agent_id):
        raise HTTPException(status_code=400, detail="Bad id")
    main_path = _resolve_main_transcript(ticket)
    if main_path is None:
        raise HTTPException(status_code=404, detail="No claude transcript for this agent")
    path = transcripts.subagents_dir(main_path) / f"agent-{agent_id}.jsonl"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No such subagent")
    resolved = _registry_agent(_read_agent_registry(), ticket)
    current = resolved[2] if resolved is not None else {}
    run_id = current.get("run_id") if _is_headless(current) else None
    # Without a limit the response stays complete (the inspector has no
    # older-page route, so tail-windowed events would become unreachable).
    # The inline child trace passes an explicit limit so its first fetch is
    # bounded server-side instead of transferring the full transcript
    # (WIKI-244 review round 2, H2).
    return _session_delta_payload(
        "claude-sub",
        path,
        cursor=cursor,
        client_path=client_path,
        tail_window=False,
        # Direct (non-HTTP) callers skip FastAPI resolution and pass the Query
        # sentinel; normalize to "no limit" in that case.
        tail_events=limit if isinstance(limit, int) else None,
        read_route="delta",
        headless_current=current if _is_headless(current) else None,
        run_id=run_id if isinstance(run_id, str) else None,
    )


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


ARTIFACT_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "gif": "image/gif",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
}


def _canonical_artifact_id(value: str) -> str | None:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


ARTIFACT_VARIANTS = frozenset({"before", "after"})


def _artifact_file(
    directory: Path, artifact_id: str, variant: str | None = None
) -> tuple[Path, str] | None:
    if directory.is_symlink() or not directory.is_dir():
        return None
    stem = f"{artifact_id}.{variant}" if variant else artifact_id
    for extension, media_type in ARTIFACT_MEDIA_TYPES.items():
        candidate = directory / f"{stem}.{extension}"
        if candidate.is_file() and not candidate.is_symlink():
            return candidate, media_type
    return None


@app.get("/api/agents/{ticket}/artifact/{artifact_id}")
def get_agent_artifact(
    ticket: str, artifact_id: str, variant: str | None = None
) -> FileResponse:
    if not valid_agent_id(ticket) or _canonical_artifact_id(artifact_id) is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if variant is not None and variant not in ARTIFACT_VARIANTS:
        raise HTTPException(status_code=404, detail="Artifact not found")

    registry_match = _registry_agent(_read_agent_registry(), ticket)
    if registry_match is not None:
        canonical_ticket, _, current = registry_match
        run_id = current.get("run_id")
        try:
            canonical_run_id = str(UUID(str(run_id)))
        except (ValueError, AttributeError):
            canonical_run_id = ""
        if canonical_run_id and canonical_run_id == run_id:
            live = _artifact_file(
                SUPERVISOR_CLIENT.paths.runtime_dir
                / "runs"
                / canonical_run_id
                / "artifacts",
                artifact_id,
                variant,
            )
            if live is not None:
                target, media_type = live
                return FileResponse(
                    target,
                    media_type=media_type,
                    headers={"Cache-Control": "private, max-age=3600"},
                )
    else:
        canonical_ticket = ticket.upper()

    _, _, archive_dir = _archive_hint(canonical_ticket)
    if archive_dir is not None:
        archived = _artifact_file(archive_dir / "artifacts", artifact_id, variant)
        if archived is not None:
            target, media_type = archived
            return FileResponse(
                target,
                media_type=media_type,
                headers={"Cache-Control": "private, max-age=3600"},
            )
    raise HTTPException(status_code=404, detail="Artifact not found")


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


@app.get("/api/models")
def list_models() -> dict[str, object]:
    return {"models": list_model_options()}


@app.get("/api/fonts")
async def list_fonts() -> dict[str, object]:
    entries = await asyncio.to_thread(installed_fonts.installed_fonts)
    return {"families": [entry["family"] for entry in entries], "fonts": entries}


def _stream_font_file(handle: installed_fonts.OpenedFontFile) -> Iterator[bytes]:
    remaining = handle.size
    try:
        while remaining > 0:
            chunk = handle.stream.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.stream.close()


@app.get("/api/fonts/file/{font_id}")
async def get_font_file(font_id: str) -> Response:
    try:
        handle = await asyncio.to_thread(installed_fonts.open_font_file, font_id)
    except installed_fonts.FontFileTooLarge:
        raise HTTPException(status_code=413, detail="Font file exceeds the 50MB serving limit")
    if handle is None:
        raise HTTPException(status_code=404, detail="Font not found")
    media_type = "font/otf" if handle.suffix == ".otf" else "font/ttf"
    return StreamingResponse(
        _stream_font_file(handle),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Length": str(handle.size),
        },
    )


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


@app.get("/api/costs")
async def get_costs(
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    ticket: str | None = None,
) -> dict[str, object]:
    """Incremental USD cost data from headless runtime raw event logs."""

    return await asyncio.to_thread(
        costs.query,
        from_ts=from_ts,
        to_ts=to_ts,
        ticket=ticket,
    )


MSG_QUEUE_PATH = Path(os.environ.get("WIKI_MSG_QUEUE_PATH") or "/tmp/wiki-msg-queue.json")
# codex: "• Working (26m 28s • esc to interrupt)" · claude: "✽ Leavening… (4m 26s · ↓ 6.0k tokens)"
SPINNER_PATTERN = re.compile(r"esc to interrupt|\(\d+m\s\d+s\b|\(\d+s\b")


def pane_is_working(pane: str) -> bool:
    return bool(SPINNER_PATTERN.search(pane))


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    mode: str = Field(default="now", pattern="^(now|on-idle)$")
    pending_id: UUID | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=200)
    source: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    findings: list[dict[str, Any]] | None = None
    constraint_bundle: str | None = Field(default=None, max_length=4000)


class AgentRespondIn(BaseModel):
    request_id: str | int
    response: dict[str, Any]


class SetModelIn(BaseModel):
    model: str = Field(..., min_length=2, max_length=64)


class SpawnReplaceIn(BaseModel):
    model: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=8)
    effort: str | None = Field(default=None, max_length=16)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class SpawnWorkerIn(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=80)
    kind: str = Field(..., min_length=2, max_length=8)
    role: str = Field(..., min_length=4, max_length=16)
    auto_archive: bool | None = None
    model: str = Field(..., min_length=2, max_length=64)
    effort: str | None = Field(default=None, max_length=16)
    workdir: str = Field(..., min_length=1, max_length=4096)
    orch: str | None = Field(default=None, max_length=100)
    prompt: str = Field(..., min_length=1, max_length=100_000)
    title: str = Field(default="", max_length=500)
    context_prelude: bool = False
    include_context: bool = False
    context_prelude_override: str | None = Field(default=None, max_length=5_000)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    implicit_request_id: bool = False

    @model_validator(mode="after")
    def validate_model_and_effort(self) -> SpawnWorkerIn:
        kind = self.kind.strip()
        role = self.role.strip().casefold()
        effort = (self.effort or "").strip() or None
        if self.auto_archive is True and role in {"implement", "plan"}:
            raise ValueError(
                f"auto_archive=one-shot is not allowed for role={self.role!r}"
            )
        if kind == "cdx" and effort not in REASONING_EFFORTS:
            raise ValueError("Reasoning effort is required for Codex workers")
        if kind == "cc" and effort is not None:
            raise ValueError("Claude workers do not accept reasoning effort")
        return self


class ContextPreludeIn(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=80)
    title: str = Field(default="", max_length=500)
    prompt: str = Field(default="", max_length=100_000)
    workdir: str = Field(..., min_length=1, max_length=4096)


class SpawnOrchestratorIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=100)
    workdir: str = Field(..., min_length=1, max_length=4096)
    kind: str = Field(default="cc", min_length=2, max_length=8)
    model: str = Field(..., min_length=2, max_length=64)
    effort: str | None = Field(default=None, max_length=16)
    goal: str = Field(default="", max_length=20_000)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    implicit_request_id: bool = False

    @model_validator(mode="after")
    def validate_kind_and_effort(self) -> SpawnOrchestratorIn:
        kind = self.kind.strip()
        effort = (self.effort or "").strip() or None
        if kind not in {"cc", "cdx"}:
            raise ValueError("kind must be cc or cdx")
        if kind == "cdx" and effort not in REASONING_EFFORTS:
            raise ValueError("Reasoning effort is required for Codex orchestrators")
        if kind == "cc" and effort is not None:
            raise ValueError("Claude orchestrators do not accept reasoning effort")
        return self


class AgentArchiveIn(BaseModel):
    outcome: str = Field(pattern="^(merged|closed|abandoned)$")
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class AutopilotEnableIn(BaseModel):
    henry_ack_required_for_merge: bool = False


def _allowed_model_message(kind: str, model: str, *, target: str) -> str:
    provider = {"cdx": "Codex", "cc": "Claude"}.get(kind, kind)
    allowed = ", ".join(model_ids_for_kind(kind))
    return (
        f"{target} model {model!r} is not allowed for {provider}. "
        f"Allowed values: {allowed}"
    )


def _require_allowed_model(kind: str, model: str, *, target: str) -> None:
    if is_model_allowed(kind, model):
        return
    raise HTTPException(
        status_code=400,
        detail=_allowed_model_message(kind, model, target=target),
    )


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
    wanted = agent_id.upper()
    for candidate, entry in registry.items():
        if str(candidate).upper() != wanted or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if isinstance(current, dict):
            return str(candidate), entry, current
    return None


def _is_headless(current: dict) -> bool:
    return isinstance(current.get("run_id"), str) and bool(current["run_id"])


def _has_legacy_control_target(registry: dict, agent_id: str) -> bool:
    for candidate in (agent_id, agent_id.upper()):
        entry = registry.get(candidate)
        if not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if isinstance(current, dict) and not _is_headless(current):
            return True
    orchestrators = registry.get("_orchestrators")
    return bool(
        isinstance(orchestrators, dict)
        and isinstance(orchestrators.get(agent_id), dict)
    )


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
            "NoEligibleAccountError": 409,
            "RotationDebouncedError": 409,
            "RotationError": 500,
            "ProviderBusy": 409,
            "ProviderProcessError": 409,
            "ProviderProtocolError": 409,
            "CommandConflict": 409,
            "CommandReceiptError": 409,
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
    return [dict(message) for message in (_read_queue().get(ticket) or []) if isinstance(message, dict)]


def resolve_existing_dir(raw_path: str, *, field_name: str) -> Path:
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} does not exist") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"{field_name} must be a directory")
    return resolved


def request_backend_base_url(request: Request) -> str:
    try:
        configured = os.environ.get("WIKI_BACKEND_URL")
        return backend_runtime.normalize_loopback_url(
            configured or str(request.base_url)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _stable_spawn_request_id(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"spawn-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _command_hash(
    method: str,
    agent_id: str,
    request_id: str,
    payload: dict[str, Any],
) -> str:
    return AgentCommand(method, agent_id, request_id, payload).command_hash


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
    return replacement_prompt(agent_id, current, status_dir=AGENT_STATUS_DIR)


def _control_headless_agent(
    agent_id: str,
    action: str,
    *,
    outcome: str | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    raw_id = agent_id.strip()
    if not raw_id or not valid_agent_id(raw_id):
        raise HTTPException(status_code=400, detail="Bad agent id")

    if action == "archive" and request_id is not None:
        binding = {"outcome": outcome}
        status = _supervisor_request(
            "idempotency/status",
            {
                "method": "run/archive",
                "request_id": request_id,
                "agent_id": raw_id,
                "command_hash": _command_hash(
                    "run/archive", raw_id, request_id, {"command_hash_payload": binding}
                ),
            },
        )
        receipt = status.get("receipt") if isinstance(status, dict) else None
        prior_result = receipt.get("result") if isinstance(receipt, dict) else None
        if isinstance(prior_result, dict):
            prior_agent_id = prior_result.get("agent_id")
            if not isinstance(prior_agent_id, str):
                prior_agent_id = prior_result.get("ticket")
            if not isinstance(prior_agent_id, str):
                prior_agent_id = raw_id
            prior_role = prior_result.get("role")
            prior_orch = prior_result.get("orchestrator_id")
            if (
                prior_role in WORKER_ROLES
                and prior_result.get("_workgraph_archive_recorded") is not True
            ):
                workgraph_service.record_archive(
                    agent_id=prior_agent_id,
                    orch=prior_orch if isinstance(prior_orch, str) else None,
                    outcome=prior_result.get("outcome")
                    if isinstance(prior_result.get("outcome"), str)
                    else outcome,
                    status_dir=AGENT_STATUS_DIR,
                )
            return dict(prior_result)
    resolved = _registry_agent(_read_agent_registry(), raw_id)
    if resolved is None:
        if action == "archive":
            params: dict[str, object] = {"agent_id": raw_id, "outcome": outcome}
            if request_id is not None:
                params["request_id"] = request_id
                params["command_hash_payload"] = {"outcome": outcome}
            result = _supervisor_request("run/archive", params)
            if isinstance(result, dict):
                return dict(result)
        raise HTTPException(status_code=404, detail="No registered agent")
    resolved_id, _, current = resolved
    if not _is_headless(current):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents must be migrated before lifecycle control",
        )
    params: dict[str, object] = {"agent_id": resolved_id}
    if action == "archive":
        params["outcome"] = outcome
    if request_id is not None:
        params["request_id"] = request_id
    if action == "archive":
        params["command_hash_payload"] = {"outcome": outcome}
    result = _supervisor_request(f"run/{action}", params)
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail="Agent supervisor returned a bad lifecycle response",
        )
    if (
        action == "archive"
        and current.get("role") in WORKER_ROLES
        and result.get("_workgraph_archive_recorded") is not True
    ):
        workgraph_service.record_archive(
            agent_id=resolved_id,
            orch=(
                result.get("orchestrator_id")
                if isinstance(result.get("orchestrator_id"), str)
                else current.get("orch")
                if isinstance(current.get("orch"), str)
                else None
            ),
            outcome=(
                result.get("outcome")
                if isinstance(result.get("outcome"), str)
                else outcome
            ),
            status_dir=AGENT_STATUS_DIR,
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
def archive_agent(
    agent_id: str,
    body: AgentArchiveIn | None = None,
) -> dict[str, object]:
    return _control_headless_agent(
        agent_id,
        "archive",
        outcome=body.outcome if body is not None else None,
        request_id=body.request_id if body is not None else None,
    )


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


def replace_agent(
    agent_id: str,
    body: SpawnReplaceIn | None = None,
    *,
    backend_base_url: str | None = None,
) -> dict[str, object]:
    raw_id = agent_id.strip()
    if not raw_id or not (
        TICKET_PATTERN.fullmatch(raw_id) or ORCH_ID_PATTERN.fullmatch(raw_id)
    ):
        raise HTTPException(status_code=400, detail="Bad agent id")
    request_id = body.request_id if body is not None else None
    if request_id is not None:
        binding = {
            "kind": (body.kind or "") if body is not None else "",
            "model": (body.model or "") if body is not None else "",
            "effort": (body.effort or "") if body is not None else "",
        }
        status = _supervisor_request(
            "idempotency/status",
            {
                "method": "run/replace",
                "request_id": request_id,
                "agent_id": raw_id,
                "command_hash": _command_hash(
                    "run/replace",
                    raw_id,
                    request_id,
                    {"command_hash_payload": binding},
                ),
            },
        )
        receipt = status.get("receipt") if isinstance(status, dict) else None
        if isinstance(receipt, dict):
            receipt_agent = receipt.get("agent_id")
            if isinstance(receipt_agent, str) and receipt_agent.upper() != raw_id.upper():
                raise HTTPException(
                    status_code=409,
                    detail="Replace request belongs to another agent",
                )
            prior_result = receipt.get("result")
            if isinstance(prior_result, dict):
                prior_agent = prior_result.get("agent_id")
                if isinstance(prior_agent, str) and prior_agent.upper() != raw_id.upper():
                    raise HTTPException(
                        status_code=409,
                        detail="Replace receipt belongs to another agent",
                    )
                role = prior_result.get("role")
                return {
                    "id": prior_agent if isinstance(prior_agent, str) else raw_id,
                    "type": "orchestrator" if role == "orchestrator" else "worker",
                    "window": None,
                    "run_id": prior_result.get("run_id"),
                    "log": prior_result.get("log"),
                    "prompt_path": None,
                    "model": prior_result.get("model"),
                    "registration": dict(prior_result),
                }
    registry = _read_agent_registry()
    resolved = _registry_agent(registry, raw_id)
    if resolved is None:
        if _has_legacy_control_target(registry, raw_id):
            raise HTTPException(
                status_code=409,
                detail="Legacy tmux agents must be migrated before Replace",
            )
        raise HTTPException(
            status_code=404,
            detail=f"No registered worker or orchestrator named {raw_id}",
        )
    if not _is_headless(resolved[2]):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents must be migrated before Replace",
        )

    resolved_id, _, current = resolved
    current_kind = current.get("kind")
    if current_kind not in {"cc", "cdx"}:
        raise HTTPException(status_code=400, detail="Agent kind is unknown")
    target = "Orchestrator" if current.get("role") == "orchestrator" else "Worker"
    requested_kind = (body.kind or "").strip() if body is not None else ""
    if requested_kind and requested_kind not in {"cc", "cdx"}:
        raise HTTPException(status_code=400, detail="Kind must be cdx or cc")
    kind = requested_kind or current_kind

    requested_model = (body.model or "").strip() if body is not None else ""
    if requested_model:
        model = requested_model
    elif requested_kind:
        model = default_model_for_kind(kind, target=target)
    else:
        model = str(current.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="Agent model is unknown")
    if requested_model or requested_kind:
        _require_allowed_model(kind, model, target=target)

    requested_effort = (body.effort or "").strip() if body is not None else ""
    has_override = bool(requested_kind or requested_model or requested_effort)
    if kind == "cc":
        if requested_effort:
            raise HTTPException(
                status_code=400,
                detail="Claude replacements do not accept reasoning effort",
            )
        effort = None
    else:
        effort = requested_effort or (
            "high" if has_override else (current.get("effort") or "high")
        )
        if effort not in REASONING_EFFORTS:
            raise HTTPException(status_code=400, detail="Invalid Codex reasoning effort")

    result = _supervisor_request(
        "run/replace",
        {
            "run_id": current["run_id"],
            "prompt": _headless_replacement_prompt(resolved_id, current),
            "provider": "codex" if kind == "cdx" else "claude",
            "model": model,
            "effort": effort,
            "backend_base_url": backend_base_url,
            "request_id": request_id,
            "command_hash_payload": {
                "kind": requested_kind,
                "model": requested_model,
                "effort": requested_effort,
            },
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


@app.post("/api/agents/{agent_id}/replace")
def replace_agent_route(
    request: Request,
    agent_id: str,
    body: SpawnReplaceIn | None = None,
) -> dict[str, object]:
    return replace_agent(
        agent_id,
        body,
        backend_base_url=request_backend_base_url(request),
    )


@app.post("/api/agents/{ticket}/set-model")
def set_agent_model(ticket: str, body: SetModelIn) -> dict[str, object]:
    raw_id = ticket.strip()
    if not raw_id or not valid_agent_id(raw_id):
        raise HTTPException(status_code=400, detail="Bad ticket")
    registry = _read_agent_registry()
    resolved = _registry_agent(registry, raw_id)
    if resolved is None:
        if _has_legacy_control_target(registry, raw_id):
            raise HTTPException(
                status_code=409,
                detail="Legacy tmux agents must be migrated before model changes",
            )
        raise HTTPException(status_code=404, detail="No registered worker")
    _, _, current = resolved
    if not _is_headless(current):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents must be migrated before model changes",
        )
    if current.get("role") == "orchestrator":
        raise HTTPException(status_code=400, detail="Only workers can change model")
    kind = current.get("kind")
    if kind not in {"cc", "cdx"}:
        raise HTTPException(status_code=400, detail="Worker kind is unknown")
    model = body.model.strip()
    _require_allowed_model(kind, model, target="Worker")
    if model == current.get("model"):
        raise HTTPException(status_code=400, detail="Desired model matches current model")
    result = _supervisor_request(
        "run/queue_model_change",
        {
            "run_id": current["run_id"],
            "model": model,
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail="Agent supervisor returned a bad model-change response",
        )
    return {
        "status": result.get("status", "queued"),
        "desired_model": result.get("desired_model", model),
    }


@app.delete("/api/agents/{ticket}/set-model")
def cancel_agent_model(ticket: str) -> dict[str, object]:
    raw_id = ticket.strip()
    if not raw_id or not valid_agent_id(raw_id):
        raise HTTPException(status_code=400, detail="Bad ticket")
    registry = _read_agent_registry()
    resolved = _registry_agent(registry, raw_id)
    if resolved is None:
        if _has_legacy_control_target(registry, raw_id):
            raise HTTPException(
                status_code=409,
                detail="Legacy tmux agents must be migrated before model changes",
            )
        raise HTTPException(status_code=404, detail="No registered worker")
    _, _, current = resolved
    if not _is_headless(current):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents must be migrated before model changes",
        )
    if current.get("role") == "orchestrator":
        raise HTTPException(status_code=400, detail="Only workers can change model")
    result = _supervisor_request(
        "run/cancel_model_change",
        {"run_id": current["run_id"]},
    )
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail="Agent supervisor returned a bad model-change response",
        )
    return {"status": result.get("status", "canceled"), "desired_model": None}


def _build_context_prelude(
    *,
    ticket: str,
    title: str,
    prompt: str,
    repo_root: Path,
) -> context_prelude.PreludeResult:
    builder = context_prelude.ContextPreludeBuilder(
        repo_root=repo_root,
        vault_dir=VAULT_DIR,
        status_dir=AGENT_STATUS_DIR,
        runtime_dir=AGENT_RUNTIME_DIR,
        archive_dir=AGENT_ARCHIVE_DIR,
    )
    return builder.build(ticket=ticket, title=title, prompt=prompt)


def _contextual_prompt(
    body: SpawnWorkerIn,
    *,
    repo_root: Path,
) -> str:
    if not (body.context_prelude or body.include_context):
        return body.prompt
    if body.context_prelude_override is not None:
        prelude = context_prelude.bound_override(body.context_prelude_override)
    else:
        try:
            prelude = _build_context_prelude(
                ticket=body.ticket,
                title=body.title,
                prompt=body.prompt,
                repo_root=repo_root,
            ).text
        except Exception as exc:
            # Context is an optional enhancement. A broken source or path can
            # never prevent the underlying worker spawn.
            logger.warning("context prelude unavailable for %s: %s", body.ticket, exc)
            return body.prompt
    return context_prelude.prepend(prelude, body.prompt)


@app.post("/api/agents/context-prelude")
def context_prelude_route(body: ContextPreludeIn) -> dict[str, object]:
    ticket = body.ticket.strip().upper()
    if not SPAWN_TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(
            status_code=400,
            detail="Ticket must be uppercase letters, numbers, or dashes",
        )
    workdir_path = resolve_existing_dir(body.workdir, field_name="Working directory")
    try:
        result = _build_context_prelude(
            ticket=ticket,
            title=body.title,
            prompt=body.prompt,
            repo_root=workdir_path,
        )
    except context_prelude.PreludeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("context prelude preview failed for %s: %s", ticket, exc)
        return {
            "prelude": "# context prelude\n[context retrieval unavailable; continue with the kickoff prompt]",
            "truncated": False,
            "sources": {},
        }
    return {
        "prelude": result.text,
        "truncated": result.truncated,
        "sources": result.sources,
        "char_budget": context_prelude.MAX_PRELUDE_CHARS,
    }


def spawn_agent(
    body: dict[str, Any] | SpawnWorkerIn,
    *,
    backend_base_url: str | None = None,
) -> dict[str, object]:
    body = cast(SpawnWorkerIn, _coerce_request_model(body, SpawnWorkerIn))
    ticket = body.ticket.strip()
    if not ticket or (
        not SPAWN_TICKET_PATTERN.fullmatch(ticket)
        and parse_reviewer_id(ticket) is None
    ):
        raise HTTPException(status_code=400, detail="Ticket must be uppercase letters, numbers, or dashes")
    parsed_ticket = parse_reviewer_id(ticket)
    if parsed_ticket is not None:
        ticket = canonical_reviewer_id(
            parsed_ticket.ticket,
            parsed_ticket.round,
            parsed_ticket.lens,
        )
    else:
        ticket = ticket.upper()

    kind = body.kind.strip()
    if kind not in {"cdx", "cc"}:
        raise HTTPException(status_code=400, detail="Kind must be cdx or cc")

    role = body.role.strip()
    if role not in WORKER_ROLES:
        raise HTTPException(status_code=400, detail="Role must be plan, implement, or review")

    model = body.model.strip()
    _require_allowed_model(kind, model, target="Worker")
    effort = (body.effort or "").strip() or None

    prompt = body.prompt
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Kickoff prompt is required")
    if len(prompt.encode("utf-8")) >= MAX_SPAWN_PROMPT_BYTES:
        raise HTTPException(status_code=400, detail="Kickoff prompt must be smaller than 100KB")

    workdir_path = resolve_existing_dir(body.workdir, field_name="Working directory")
    orch = (body.orch or "").strip()
    implicit_request_id = body.implicit_request_id or body.request_id is None
    request_id = body.request_id or _stable_spawn_request_id(
        {
            "agent_id": ticket,
            "provider": "codex" if kind == "cdx" else "claude",
            "role": role,
            "auto_archive": body.auto_archive,
            "model": model,
            "effort": effort,
            "worktree": str(workdir_path),
            "prompt": body.prompt,
            "title": body.title,
            "context_prelude": body.context_prelude,
            "include_context": body.include_context,
            "context_prelude_override": body.context_prelude_override,
            "orchestrator_id": orch or None,
        }
    )
    try:
        prompt = _contextual_prompt(body, repo_root=workdir_path)
    except context_prelude.PreludeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if len(prompt.encode("utf-8")) >= MAX_SPAWN_PROMPT_BYTES:
        raise HTTPException(status_code=400, detail="Kickoff prompt must be smaller than 100KB")

    registry = _read_agent_registry()
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

    resolved = _registry_agent(registry, ticket)
    registry_ticket = resolved[0] if resolved is not None else ticket
    current = resolved[2] if resolved is not None else {}
    current_is_headless = isinstance(current, dict) and _is_headless(current)
    replaying = False
    if current_is_headless:
        idempotency = _supervisor_request(
            "idempotency/status",
            {"method": "run/start", "request_id": request_id},
        )
        if not isinstance(idempotency, dict) or not isinstance(idempotency.get("known"), bool):
            raise HTTPException(
                status_code=502,
                detail="Agent supervisor returned a bad idempotency response",
            )
        replaying = idempotency["known"]
    if current_is_headless and not replaying:
        raise HTTPException(
            status_code=409,
            detail=f"{registry_ticket} already has a supervisor-owned run; use Replace",
        )
    live_window = current.get("window")
    if isinstance(live_window, str) and live_window in tmux_live_windows():
        raise HTTPException(status_code=409, detail=f"{ticket} already has a live worker window")

    migrate_legacy_flag = bool(current) and not current_is_headless
    result = _supervisor_request(
        "run/start",
        {
            "agent_id": ticket,
            "provider": "codex" if kind == "cdx" else "claude",
            "role": role,
            "auto_archive": body.auto_archive,
            "model": model,
            "effort": effort,
            "worktree": str(workdir_path),
            "prompt": prompt,
            "orchestrator_id": orch or None,
            "migrate_legacy": migrate_legacy_flag,
            "request_id": request_id,
            "implicit_request_id": implicit_request_id,
            "command_hash_payload": {
                "agent_id": ticket,
                "provider": "codex" if kind == "cdx" else "claude",
                "role": role,
                "auto_archive": body.auto_archive,
                "model": model,
                "effort": effort,
                "worktree": str(workdir_path),
                "prompt": body.prompt,
                "title": body.title,
                "context_prelude": body.context_prelude,
                "include_context": body.include_context,
                "context_prelude_override": body.context_prelude_override,
                "orchestrator_id": orch or None,
            },
            "backend_base_url": backend_base_url,
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Agent supervisor returned a bad run")
    # Key the ticket-only Codex-notice cleanup off the REPLACED legacy identity
    # (persisted in the supervisor result), not the new destination kind. A
    # cdx-to-cc migration still needs to clear the Codex banner for the ticket.
    # The supervisor stamps the flag inside store.create and projects it into
    # the registry snapshot, so /api/agents self-heals on the next refresh
    # even without a spawn retry.
    if result.get("replaced_legacy_provider") == "codex":
        _publish_codex_worker_replaced(ticket)
    # Recorded on supervisor replays too: append_edge dedupes by request id
    # across the full edge history, so a replay whose first append failed
    # heals the graph while a successful one stays a no-op.
    workgraph_service.record_spawn(
        agent_id=ticket,
        orch=orch or None,
        role=role,
        model=model,
        effort=effort,
        worktree=str(workdir_path),
        request_id=request_id,
        status_dir=AGENT_STATUS_DIR,
    )
    refreshed = _registry_agent(_read_agent_registry(), ticket)
    registration = refreshed[2] if refreshed is not None else {}
    response: dict[str, object] = {
        "window": None,
        "run_id": result.get("run_id"),
        "log": registration.get("log"),
        "prompt_path": None,
        "request_id": request_id,
    }
    warnings = []
    if isinstance(result.get("warning"), str):
        warnings.append(result["warning"])
    auth_hint = PROVIDER_HEALTH.spawn_hint(kind)
    if auth_hint:
        warnings.append(auth_hint)
    if warnings:
        response["warning"] = " ".join(warnings)
    return response


@app.post("/api/agents/spawn")
def spawn_agent_route(
    request: Request,
    body: dict[str, Any] | SpawnWorkerIn,
) -> dict[str, object]:
    return spawn_agent(
        body,
        backend_base_url=request_backend_base_url(request),
    )


@app.post("/api/agents/next-review")
def next_review_route(request: Request, body: NextReviewIn) -> dict[str, Any]:
    """Gate a PR and start its next pinned reviewer as one idempotent action."""

    from .agent_runtime.next_review import next_review

    return next_review(
        ticket=body.ticket,
        pr_number=body.pr_number,
        expected_sha=body.expected_sha,
        orch=body.orch,
        reviewer_kind=body.reviewer_kind,
        reviewer_model=body.reviewer_model,
        reviewer_effort=body.reviewer_effort,
        prompt_template=body.prompt_template,
        request_id=body.request_id,
        diversity=body.diversity,
        backend_base_url=request_backend_base_url(request),
    )


@app.post("/api/agents/rebase-dirty-pr")
def rebase_dirty_pr_route(body: RebaseDirtyPrIn) -> dict[str, Any]:
    """Start the scoped conflict helper only when the PR is DIRTY."""

    from .agent_runtime.rebase_bot import rebase_dirty_pr

    return rebase_dirty_pr(
        pr_number=body.pr_number,
        ticket=body.ticket,
        worker_id=body.worker_id,
        notify=_rebase_bot_notification_sender,
    )


@app.post("/api/autopilot/{ticket}/enable")
def autopilot_enable_route(ticket: str, body: AutopilotEnableIn | None = None) -> dict[str, Any]:
    from .agent_runtime.autopilot import AutopilotController

    try:
        return AutopilotController(notify=AutopilotController.live_notify).enable(
            ticket,
            henry_ack_required_for_merge=(body.henry_ack_required_for_merge if body else False),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/autopilot/{ticket}/disable")
def autopilot_disable_route(ticket: str) -> dict[str, Any]:
    from .agent_runtime.autopilot import AutopilotController

    try:
        return AutopilotController(notify=AutopilotController.live_notify).disable(ticket)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/autopilot/{ticket}/ack-merge")
def autopilot_ack_merge_route(ticket: str) -> dict[str, Any]:
    from .agent_runtime.autopilot import AutopilotController

    try:
        return AutopilotController(notify=AutopilotController.live_notify).ack_merge(ticket)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/autopilot")
def autopilot_status_route() -> dict[str, Any]:
    from .agent_runtime.autopilot import AutopilotController

    return AutopilotController(notify=AutopilotController.live_notify).status()


@app.get("/api/autopilot/{ticket}")
def autopilot_ticket_status_route(ticket: str) -> dict[str, Any]:
    from .agent_runtime.autopilot import AutopilotController

    try:
        return AutopilotController(notify=AutopilotController.live_notify).status(ticket)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def spawn_orchestrator(
    body: dict[str, Any] | SpawnOrchestratorIn,
    *,
    backend_base_url: str | None = None,
) -> dict[str, object]:
    body = cast(SpawnOrchestratorIn, _coerce_request_model(body, SpawnOrchestratorIn))
    orch_id = body.id.strip()
    if not ORCH_ID_PATTERN.fullmatch(orch_id):
        raise HTTPException(
            status_code=400,
            detail="Orchestrator id must start with a letter or number and only use letters, numbers, dashes, or underscores",
        )

    kind = body.kind.strip()
    model = body.model.strip()
    _require_allowed_model(kind, model, target="Orchestrator")
    goal = body.goal.strip()
    if len(goal.encode("utf-8")) >= MAX_ORCH_GOAL_BYTES:
        raise HTTPException(status_code=400, detail="Initial goal must stay under 20KB")

    workdir_path = resolve_existing_dir(body.workdir, field_name="Project directory")
    implicit_request_id = body.implicit_request_id or body.request_id is None
    request_id = body.request_id or _stable_spawn_request_id(
        {
            "agent_id": orch_id,
            "provider": "codex" if kind == "cdx" else "claude",
            "role": "orchestrator",
            "model": model,
            "effort": body.effort,
            "worktree": str(workdir_path),
            "goal": goal,
        }
    )

    registry = _read_agent_registry()
    normal_entry = registry.get(orch_id)
    normal_current = (
        normal_entry.get("current") if isinstance(normal_entry, dict) else None
    )
    replaying = False
    if isinstance(normal_current, dict):
        if _is_headless(normal_current):
            idempotency = _supervisor_request(
                "idempotency/status",
                {"method": "run/start", "request_id": request_id},
            )
            if not isinstance(idempotency, dict) or not isinstance(
                idempotency.get("known"), bool
            ):
                raise HTTPException(
                    status_code=502,
                    detail="Agent supervisor returned a bad idempotency response",
                )
            replaying = idempotency["known"]
        if not replaying:
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
            "provider": "codex" if kind == "cdx" else "claude",
            "role": "orchestrator",
            "model": model,
            "effort": body.effort,
            "worktree": str(workdir_path),
            "prompt": prompt,
            "orchestrator_id": None,
            "migrate_legacy": migrate_legacy,
            "request_id": request_id,
            "implicit_request_id": implicit_request_id,
            "command_hash_payload": {
                "agent_id": orch_id,
                "provider": "codex" if kind == "cdx" else "claude",
                "role": "orchestrator",
                "model": model,
                "effort": body.effort,
                "worktree": str(workdir_path),
                "goal": goal,
            },
            "backend_base_url": backend_base_url,
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Agent supervisor returned a bad run")
    # Orchestrator migrations follow the same replaced-identity rule as workers:
    # gate off the supervisor's persisted flag so cross-provider cdx-to-cc
    # replacements clear the legacy Codex notice and idempotent replays reapply
    # cleanup after a post-commit crash.
    if result.get("replaced_legacy_provider") == "codex":
        _publish_codex_worker_replaced(orch_id)
    refreshed = _registry_agent(_read_agent_registry(), orch_id)
    registration = refreshed[2] if refreshed is not None else {}

    response: dict[str, object] = {
        "window": None,
        "run_id": result.get("run_id"),
        "log": registration.get("log"),
        "prompt_path": None,
        "request_id": request_id,
        "note": "orchestrator registered under the durable supervisor",
    }
    auth_hint = PROVIDER_HEALTH.spawn_hint(kind)
    if auth_hint:
        response["warning"] = auth_hint
    return response


@app.post("/api/agents/spawn-orchestrator")
def spawn_orchestrator_route(
    request: Request,
    body: dict[str, Any] | SpawnOrchestratorIn,
) -> dict[str, object]:
    return spawn_orchestrator(
        body,
        backend_base_url=request_backend_base_url(request),
    )


class ComposerGateIn(BaseModel):
    pr: str = Field(..., min_length=1, max_length=512)
    expect_sha: str | None = Field(default=None, max_length=64)


class ComposerProvisionIn(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=80)
    # `orch` identifies which orchestrator's repo to provision under. Since
    # only Wiki.app can reach this endpoint (Path B, round 6), the field is
    # trusted at the transport layer once `require_wiki_app_origin` passes.
    # The registry lookup in `_resolve_orchestrator_root` still rejects
    # unknown ids and worker sessions with role != "orchestrator".
    # Optional at the model layer so the internal `composer_provision_worktree`
    # helper can be exercised directly by tests — the route wrapper enforces
    # presence.
    orch: str | None = Field(default=None, min_length=1, max_length=100)


def _resolve_orchestrator_root(orch_id: str) -> Path:
    """Return the repo root for the calling orchestrator, or raise.

    Rejects unknown ids and worker sessions so `/spawn` never silently targets
    the wrong repo. Multiple orchestrators (wiki, tooling, phoebe, misc, etc.)
    share the composer backend, so we look each caller up in the live agent
    registry rather than defaulting to Wiki's own `ROOT_DIR`.
    """

    try:
        registry = _read_agent_registry()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"agent registry unreadable: {exc}",
        ) from exc

    orch_entry: dict[str, Any] | None = None
    ticket_entry = registry.get(orch_id) if isinstance(registry, dict) else None
    if isinstance(ticket_entry, dict):
        current = ticket_entry.get("current")
        if isinstance(current, dict) and current.get("role") == "orchestrator":
            orch_entry = current
        elif isinstance(current, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"/spawn must be dispatched from an orchestrator session; "
                    f"'{orch_id}' is a worker (role={current.get('role')!r})"
                ),
            )
    if orch_entry is None:
        headless = (registry.get("_orchestrators") or {}) if isinstance(registry, dict) else {}
        candidate = headless.get(orch_id) if isinstance(headless, dict) else None
        if isinstance(candidate, dict):
            orch_entry = candidate
    if orch_entry is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown orchestrator '{orch_id}' — not registered",
        )

    raw_root = orch_entry.get("worktree") or orch_entry.get("cwd")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise HTTPException(
            status_code=400,
            detail=f"Orchestrator '{orch_id}' has no worktree/cwd registered",
        )
    try:
        repo_root = Path(raw_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Orchestrator '{orch_id}' root {raw_root!r} is invalid: {exc}",
        ) from exc
    if not repo_root.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Orchestrator '{orch_id}' root {repo_root} is not a directory",
        )
    if not (repo_root / ".git").exists():
        raise HTTPException(
            status_code=400,
            detail=f"Orchestrator '{orch_id}' root {repo_root} is not a git repository",
        )
    return repo_root


def _git_common_dir(path: Path) -> tuple[bool, Path | str]:
    """Return `(True, canonical common-dir)` or `(False, stderr excerpt)`.

    `--git-common-dir` returns a path relative to the invoking cwd, so we
    canonicalize against `path`. Callers use this on BOTH sides of a
    repo-identity compare so a primary worktree (where common-dir ==
    `<root>/.git`) matches a linked worktree (where `<workdir>/.git` is a
    file whose common-dir points to the primary's `.git`).
    """

    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "git rev-parse failed").strip()[:200]
    raw = result.stdout.strip()
    if not raw:
        return False, "git rev-parse --git-common-dir returned empty"
    common = Path(raw)
    if not common.is_absolute():
        common = (path / common).resolve()
    else:
        common = common.resolve()
    return True, common


def _validate_existing_worktree(
    workdir: Path, repo_root: Path, branch: str
) -> None:
    """Reject stale/unrelated worktrees before returning them as-is.

    An `.git` file/dir alone is not proof — a leftover from a prior repo, an
    empty marker, or a checkout on the wrong branch would all pass a naive
    `.git.exists()` check. We verify:

    - `git rev-parse --git-common-dir` matches on BOTH sides (caller repo
      and target workdir) — accepts linked worktrees whose `.git` file
      points back to the primary's common-dir. (M1 in review4.)
    - `git rev-parse --abbrev-ref HEAD` matches the expected branch.

    Any mismatch is a 409 with a specific message so the caller can pick a
    different ticket or clean up manually.
    """

    root_ok, root_result = _git_common_dir(repo_root)
    if not root_ok:
        raise HTTPException(
            status_code=500,
            detail=f"orchestrator root {repo_root} common-dir unreadable: {root_result}",
        )
    expected_common = cast(Path, root_result)

    workdir_ok, workdir_result = _git_common_dir(workdir)
    if not workdir_ok:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{workdir} exists but is not a git worktree: {workdir_result}"
            ),
        )
    common_path = cast(Path, workdir_result)
    if common_path != expected_common:
        raise HTTPException(
            status_code=409,
            detail=(
                f"existing worktree at {workdir} belongs to a different repository "
                f"({common_path}); expected {expected_common}"
            ),
        )

    try:
        head = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"{workdir} exists but git could not read HEAD: {exc}",
        ) from exc
    if head.returncode != 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{workdir} exists but HEAD is unreadable: "
                f"{(head.stderr or head.stdout or 'rev-parse HEAD failed').strip()[:200]}"
            ),
        )
    actual_branch = head.stdout.strip()
    if actual_branch != branch:
        raise HTTPException(
            status_code=409,
            detail=(
                f"existing worktree at {workdir} is on branch {actual_branch!r}, "
                f"expected {branch!r}"
            ),
        )


def provision_pinned_worktree(
    repo_root: Path,
    workdir: Path,
    expected_sha: str,
) -> Path:
    """Create or validate a detached worktree pinned to ``expected_sha``."""

    workdir = workdir.resolve()
    repo_root = repo_root.resolve()
    try:
        canonical_sha_result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{expected_sha}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"git rev-parse failed: {exc}") from exc
    if canonical_sha_result.returncode != 0 or not canonical_sha_result.stdout.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                canonical_sha_result.stderr
                or canonical_sha_result.stdout
                or f"could not resolve commit {expected_sha}"
            ).strip()[:400],
        )
    expected_sha = canonical_sha_result.stdout.strip()
    if workdir.exists():
        if not workdir.is_dir() or not (workdir / ".git").exists():
            raise HTTPException(
                status_code=409,
                detail=f"review worktree path exists but is not a git worktree: {workdir}",
            )
        root_ok, root_result = _git_common_dir(repo_root)
        work_ok, work_result = _git_common_dir(workdir)
        if not root_ok or not work_ok or root_result != work_result:
            raise HTTPException(
                status_code=409,
                detail=f"review worktree {workdir} belongs to a different repository",
            )
        head = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if head.returncode != 0 or head.stdout.strip() != expected_sha:
            actual = (head.stdout or head.stderr).strip()[:200]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"review worktree {workdir} is not pinned to {expected_sha} "
                    f"(found {actual})"
                ),
            )
        return workdir

    workdir.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                "--detach",
                str(workdir),
                expected_sha,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"git worktree add failed: {exc}") from exc
    if result.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=(result.stderr or result.stdout or "git worktree add failed").strip()[:400],
        )
    return workdir


@app.post(
    "/api/composer/provision-worktree",
    dependencies=[Depends(require_wiki_app_origin)],
)
def composer_provision_worktree_route(body: ComposerProvisionIn) -> dict[str, object]:
    """Provision a `.claude/worktrees/<ticket>` for the caller's orchestrator.

    Gated by `require_wiki_app_origin` — only the Wiki.app main process
    (via the Tauri invoke bridge) holds the in-memory secret. Worker CLI
    sessions cannot reach this endpoint even if they craft the exact HTTP
    request. There is no client-supplied credential path left: the earlier
    per-orch composer-token endpoint and the legacy session-id header have
    both been removed.
    """

    if not body.orch:
        raise HTTPException(
            status_code=400,
            detail="ComposerProvisionIn.orch is required for /spawn dispatch",
        )
    return composer_provision_worktree(body, body.orch)


def composer_provision_worktree(
    body: ComposerProvisionIn, orch_id: str
) -> dict[str, object]:
    """Ensure `.claude/worktrees/<ticket-lower>` exists in the caller's repo.

    Backs the composer `/spawn` slash command. The wrapping route runs the
    Wiki.app-origin transport check (`require_wiki_app_origin`) before
    calling us; `orch_id` therefore comes from the trusted body field and
    is validated against the agent registry via `_resolve_orchestrator_root`
    — unknown orch ids and worker sessions are rejected with 400.
    Existing worktrees are returned as-is only when they are valid git
    worktrees pointing at the expected repo and branch; missing paths get
    `git worktree add -b <branch> <path> FETCH_HEAD` after a fresh
    `git fetch origin main` (whose exit status is required to be zero).
    Uses `-b` — never `-B` — so a colliding branch is a clean error, never a
    silent force-reset of in-progress work.
    """

    ticket = body.ticket.strip()
    if not ticket or not SPAWN_TICKET_PATTERN.fullmatch(ticket):
        raise HTTPException(
            status_code=400,
            detail="Ticket must be uppercase letters, numbers, or dashes",
        )
    repo_root = _resolve_orchestrator_root(orch_id)

    branch = ticket.lower()
    workdir = (repo_root / ".claude" / "worktrees" / branch).resolve()
    if workdir.exists():
        if not workdir.is_dir():
            raise HTTPException(
                status_code=409,
                detail=f"{workdir} exists but is not a directory",
            )
        if not (workdir / ".git").exists():
            raise HTTPException(
                status_code=409,
                detail=f"{workdir} exists but is not a git worktree",
            )
        _validate_existing_worktree(workdir, repo_root, branch)
        return {"workdir": str(workdir), "provisioned": False}
    workdir.parent.mkdir(parents=True, exist_ok=True)
    try:
        fetch = subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "origin", "main"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if fetch.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail=(fetch.stderr or fetch.stdout or "git fetch failed").strip()[:400],
            )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                "-b",
                branch,
                str(workdir),
                "FETCH_HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"git worktree add failed: {exc}",
        ) from exc
    if result.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=(result.stderr or result.stdout or "git worktree add failed").strip()[:400],
        )
    return {"workdir": str(workdir), "provisioned": True}


@app.post(
    "/api/composer/gate",
    dependencies=[Depends(require_wiki_app_origin)],
)
def composer_gate(body: ComposerGateIn) -> dict[str, object]:
    """Run `wiki gate <pr> --json` and normalise the verdict.

    Backs the composer `/gate` slash command. Same Wiki.app-only origin
    gate as `/api/composer/provision-worktree`: worker CLI sessions cannot
    reach it. Read-only relative to git — the underlying CLI only shells
    out to `gh` for PR view + check status.
    """

    wiki_cli = ROOT_DIR / "wiki"
    if not wiki_cli.exists():
        raise HTTPException(status_code=500, detail=f"wiki CLI missing at {wiki_cli}")
    cmd = [str(wiki_cli), "gate", body.pr, "--json"]
    if body.expect_sha:
        cmd += ["--expect-sha", body.expect_sha]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(ROOT_DIR),
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="wiki gate timed out") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"wiki gate exec failed: {exc}") from exc
    parsed: dict[str, object] | None = None
    if proc.stdout:
        try:
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            parsed = None
    if proc.returncode == 2 or parsed is None:
        raise HTTPException(
            status_code=502,
            detail=(proc.stderr or proc.stdout or "wiki gate returned no verdict").strip(),
        )
    ready = bool(parsed.get("ready"))
    reasons = parsed.get("reasons") or []
    summary = "ready" if ready else ", ".join(str(reason) for reason in reasons) or "not ready"
    return {
        "verdict": "pass" if ready else "fail",
        "summary": summary,
        "raw": parsed,
    }


@app.post("/api/agents/{ticket}/message")
def agent_message(ticket: str, body: MessageIn, background: BackgroundTasks) -> dict[str, object]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    registry = _read_agent_registry()
    resolved = _registry_agent(registry, ticket)
    if resolved is not None and _is_headless(resolved[2]):
        method = "run/send_now" if body.mode == "now" else "run/send_on_idle"
        params: dict[str, Any] = {
            "agent_id": resolved[0],
            "text": body.text,
            "pending_id": str(body.pending_id) if body.pending_id else None,
            "request_id": body.request_id,
            "dedupe_key": body.dedupe_key,
        }
        if body.source:
            params["source"] = body.source
        result = _supervisor_request(method, params)
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=502,
                detail="Agent supervisor returned a bad message response",
            )
        current = resolved[2]
        if current.get("role") in WORKER_ROLES:
            workgraph_service.record_steer(
                agent_id=resolved[0],
                orch=current.get("orch") if isinstance(current.get("orch"), str) else None,
                mode=body.mode,
                text=body.text,
                source=body.source,
                request_id=body.request_id,
                status_dir=AGENT_STATUS_DIR,
                findings=body.findings,
                constraint_bundle=body.constraint_bundle,
            )
        return dict(result)
    del background
    if _has_legacy_control_target(registry, ticket):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents must be migrated before composer control",
        )
    raise HTTPException(status_code=404, detail="No registered agent")


@app.get("/api/agents/{ticket}/queue")
def agent_queue(ticket: str) -> dict[str, object]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    registry = _read_agent_registry()
    resolved = _registry_agent(registry, ticket)
    if resolved is not None and _is_headless(resolved[2]):
        return {"messages": _headless_queue(resolved[0])}
    if _has_legacy_control_target(registry, ticket):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents must be migrated before queue inspection",
        )
    return {"messages": []}


@app.delete("/api/agents/{ticket}/queue/{index}")
def agent_queue_delete(ticket: str, index: int) -> dict[str, object]:
    if not valid_agent_id(ticket):
        raise HTTPException(status_code=400, detail="Bad ticket")
    registry = _read_agent_registry()
    resolved = _registry_agent(registry, ticket)
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
    if _has_legacy_control_target(registry, ticket):
        raise HTTPException(
            status_code=409,
            detail="Legacy tmux agents must be migrated before queue mutation",
        )
    raise HTTPException(status_code=404, detail="No registered agent")


# ---------------------------------------------------------------------------
# Agent event broker: watchdog + manual endpoints push, /api/events streams.
# ---------------------------------------------------------------------------
_event_subscribers: set[asyncio.Queue[dict]] = set()
# Captured in `lifespan` so sync route handlers (spawn_agent lives in the
# FastAPI thread pool) can schedule loop-bound work — asyncio.Queue is
# single-loop, so a bare put_nowait from the thread pool is unsafe.
_MAIN_EVENT_LOOP: asyncio.AbstractEventLoop | None = None


async def publish_agent_event(event: dict) -> None:
    _consume_provider_health_signal(event)
    ACCOUNT_NOTICES.apply_event(event)
    dead: list[asyncio.Queue[dict]] = []
    for queue_ in list(_event_subscribers):
        try:
            queue_.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(queue_)
    for queue_ in dead:
        _event_subscribers.discard(queue_)


def _publish_codex_worker_replaced(ticket: str) -> bool:
    """Clear ticket-only legacy Codex notices after a legacy-to-headless commit.

    Runs on every /api/agents/spawn (and /spawn-orchestrator) invocation whose
    supervisor result carries ``replaced_legacy_provider == "codex"``, and on
    every /api/agents refresh whose registry current entry projects the same
    marker (so a post-commit backend crash heals without a spawn replay).
    Only ticket-only entries (no stored run_id) are affected — the apply_event
    handler leaves headless run-scoped entries alone.

    Emits an ``agents`` SSE refresh AFTER the notice mutation lands so the
    frontend refetches ``/api/agents`` and sees the cleared notice. Without
    this the only "agents changed" event fires when the supervisor commits
    (BEFORE cleanup), and the frontend can settle on the stale pre-clean
    snapshot. Gated on ``apply_event`` returning True so the /api/agents
    self-heal loop is one-shot per marked ticket instead of re-firing a
    refresh on every subsequent poll.

    Returns True when the notice store actually changed.
    """

    changed = ACCOUNT_NOTICES.apply_event(
        {
            "type": "codex_worker_replaced",
            "provider": "codex",
            "ticket": ticket,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )
    if changed:
        _schedule_agents_refresh({ticket})
    return changed


def _schedule_agents_refresh(tickets: set[str]) -> None:
    """Push an ``agents`` invalidation onto SSE subscribers from any thread.

    FastAPI runs sync ``def`` route handlers in a thread pool, but asyncio.Queue
    is loop-bound. Route the emission through ``run_coroutine_threadsafe`` when
    a main loop is registered; fall back to a direct in-loop schedule for tests
    that call from the running loop directly.
    """

    ticket_list = sorted(t for t in tickets if isinstance(t, str) and t)
    if not ticket_list:
        return
    event = {"type": "agents", "surface": "agents", "tickets": ticket_list[:20]}

    async def _emit() -> None:
        _invalidate_session_paths(set(ticket_list))
        dead: list[asyncio.Queue[dict]] = []
        for queue_ in list(_event_subscribers):
            try:
                queue_.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(queue_)
        for queue_ in dead:
            _event_subscribers.discard(queue_)

    loop = _MAIN_EVENT_LOOP
    running: asyncio.AbstractEventLoop | None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None or not loop.is_running():
        loop = running
    if loop is not None and loop.is_running():
        if running is loop:
            loop.create_task(_emit())
        else:
            asyncio.run_coroutine_threadsafe(_emit(), loop)
        return
    # No loop at all — session cache still invalidates so the next in-process
    # refresh reads the cleared notice. Purely-synchronous callers (offline
    # scripts) take this path.
    _invalidate_session_paths(set(ticket_list))


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


def _consume_provider_health_signal(event: dict) -> None:
    """Mark only exhausted current-credential auth failures as unauthorized."""

    if (
        event.get("type") == "codex_auth_verified"
        and event.get("provider") == "codex"
        and event.get("credential_source") == "current"
        and event.get("success") is True
    ):
        fingerprint = event.get("credential_fingerprint")
        if isinstance(fingerprint, str):
            PROVIDER_HEALTH.mark_authenticated(
                "cdx", credential_fingerprint=fingerprint
            )
        return
    if (
        event.get("type") != "codex_auth_dead_exhausted"
        or event.get("provider") != "codex"
        or event.get("failure") != "auth"
        or event.get("credential_source") != "current"
        or event.get("exhausted") is not True
    ):
        return
    PROVIDER_HEALTH.mark_unauthorized("cdx")


async def agent_runtime_dispatchers() -> None:
    await supervisor_event_bridge()


async def message_dispatcher() -> None:
    await agent_runtime_dispatchers()


async def _start_dispatcher() -> tuple[asyncio.Task, asyncio.Task, asyncio.Task]:
    dispatcher_task = asyncio.create_task(message_dispatcher())
    watchdog_task = asyncio.create_task(accounts.watchdog_loop(publish_agent_event))
    token_task = asyncio.create_task(tokens.refresh_in_background())
    return (dispatcher_task, watchdog_task, token_task)


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
    extras.extend(_agent_status_paths())
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


@app.get("/api/files/tree", response_model=FileTree)
def list_files(workspace: str = "wiki") -> FileTree:
    summaries: list[FileSummary] = []
    resolution = resolve_workspace(workspace)
    try:
        files, truncated = iter_repo_files(resolution.root, resolution.root_fd)
        for path in files:
            relative_path = path.relative_to(resolution.root)
            try:
                fd = open_relative_file(resolution.root_fd, relative_path.parts)
                try:
                    stat_result = os.fstat(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                if exc.errno in {errno.EMFILE, errno.ENFILE}:
                    truncated = True
                    break
                continue
            if not stat.S_ISREG(stat_result.st_mode):
                continue
            summaries.append(
                FileSummary(
                    path=relative_path.as_posix(),
                    size=stat_result.st_size,
                    updated_at=datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc),
                )
            )
    finally:
        os.close(resolution.root_fd)
    return FileTree(files=summaries, truncated=truncated)


@app.get("/api/files/content", response_model=FileContent)
def get_file_content(
    path: str = Query(..., min_length=1),
    workspace: str = "wiki",
) -> FileContent:
    relative = validate_file_path(path)
    resolution = resolve_workspace(workspace)
    file_root = resolution.root
    relative_path = relative.as_posix()
    target = file_root / Path(*relative.parts)
    try:
        fd = open_relative_file(resolution.root_fd, relative.parts)
    except OSError as exc:
        os.close(resolution.root_fd)
        raise HTTPException(status_code=404, detail="File not found") from exc

    try:
        if not opened_file_is_safe(fd, target, file_root, resolution.root_fd):
            file_not_found()
        size = os.fstat(fd).st_size
        raw = read_open_file(fd, MAX_FILE_BYTES)
        if size > MAX_FILE_BYTES or len(raw) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "file_too_large",
                    "message": f"File exceeds the {MAX_FILE_BYTES // 1_000_000}MB viewing limit",
                },
            )
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")
    finally:
        os.close(fd)
        os.close(resolution.root_fd)
    if b"\x00" in raw:
        return FileContent(path=relative_path, size=size, binary=True, error="binary file")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return FileContent(path=relative_path, size=size, binary=True, error="binary file")
    return FileContent(path=relative_path, size=size, content=content)


_VAULT_ASSET_RESIZE_MIMES = {"image/png", "image/jpeg", "image/webp"}


def _read_vault_asset_bytes(asset_path: str) -> tuple[bytes, str, int]:
    """Return the file bytes, MIME type, and integer-millisecond mtime.

    The mtime is captured off the SAME file descriptor used to read the
    bytes so the thumbnail cache (WIKI-200) can't drift against a race
    where the file is replaced between the read and a follow-up stat."""
    target, _relative_path, media_type = resolve_vault_asset_path(asset_path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    try:
        if not opened_file_is_safe(fd, target, VAULT_DIR.resolve()):
            file_not_found()
        stat_result = os.fstat(fd)
        size = stat_result.st_size
        mtime_ms = stat_result.st_mtime_ns // 1_000_000
        raw = read_open_file(fd, MAX_FILE_BYTES)
        if size > MAX_FILE_BYTES or len(raw) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "file_too_large",
                    "message": f"File exceeds the {MAX_FILE_BYTES // 1_000_000}MB viewing limit",
                },
            )
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")
    finally:
        os.close(fd)
    return raw, media_type, mtime_ms


@app.get("/api/vault/identity")
def get_vault_identity() -> dict[str, str]:
    """Stable identity for the currently-mounted vault. The frontend
    thumbnail cache uses this as a namespace prefix so previews from
    different vaults sharing the same origin cannot collide. WIKI-200."""
    fingerprint = hashlib.sha256(str(VAULT_DIR).encode("utf-8")).hexdigest()
    return {"identity": fingerprint[:16]}


@app.get("/api/vault/assets/{asset_path:path}")
def get_vault_asset(asset_path: str, w: int | None = None) -> Response:
    raw, media_type, _mtime_ms = _read_vault_asset_bytes(asset_path)
    if w is not None and media_type in _VAULT_ASSET_RESIZE_MIMES:
        from .image_scrub import ALLOWED_RESIZE_WIDTHS, ImageScrubError, resize_image_bytes

        try:
            width = int(w)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="w must be an integer") from exc
        if width not in ALLOWED_RESIZE_WIDTHS:
            raise HTTPException(status_code=400, detail="unsupported w value")
        try:
            raw, media_type = resize_image_bytes(raw, media_type, width)
        except ImageScrubError as exc:
            # The bounded resize enforces the same pre-decode side + pixel
            # caps as ingress so decompression bombs cannot slip in through
            # the thumbnail path.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    headers = {
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }
    if media_type == "image/svg+xml":
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return Response(content=raw, media_type=media_type, headers=headers)


def _asset_meta_for(
    raw: bytes, media_type: str, mtime_ms: int | None = None
) -> dict[str, object] | None:
    """Return canonical (orientation-normalised) dimensions + preview for an
    image asset, or None if it cannot be scrubbed. Uses scrub_image so the
    reported width/height match what the browser will actually render — a
    portrait photo tagged with EXIF orientation 6 comes out with its axes
    already swapped, matching the pixels the vault-asset endpoint serves.

    The optional `mtime_ms` is included in the payload so the client
    thumbnail cache (WIKI-200) can invalidate on file change."""
    from .image_scrub import ImageScrubError, scrub_image

    try:
        result = scrub_image(raw, media_type)
    except ImageScrubError:
        return None
    payload: dict[str, object] = {
        "width": result.width,
        "height": result.height,
        "media_type": media_type,
    }
    if result.preview_base64:
        payload["preview_base64"] = result.preview_base64
    if mtime_ms is not None:
        payload["mtime_ms"] = mtime_ms
    return payload


@app.get("/api/vault/asset-meta/{asset_path:path}")
def get_vault_asset_meta(asset_path: str) -> dict[str, object]:
    raw, media_type, mtime_ms = _read_vault_asset_bytes(asset_path)
    if media_type not in _VAULT_ASSET_RESIZE_MIMES:
        raise HTTPException(status_code=415, detail="asset is not an image")
    meta = _asset_meta_for(raw, media_type, mtime_ms)
    if meta is None:
        raise HTTPException(status_code=422, detail="asset could not be scrubbed")
    return meta


class AssetMetaBatchRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=64)


@app.post("/api/vault/asset-meta")
def post_vault_asset_meta_batch(payload: AssetMetaBatchRequest) -> dict[str, dict[str, object]]:
    """Return `{path: {width, height, preview_base64}}` for every readable
    image path in the request. Silently drops entries that resolve outside
    the vault, aren't images, or fail to decode so the frontend can render
    the surviving ones in one round-trip without any per-image race."""
    seen: set[str] = set()
    result: dict[str, dict[str, object]] = {}
    for raw_path in payload.paths:
        if not isinstance(raw_path, str):
            continue
        candidate = raw_path.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            content, media_type, mtime_ms = _read_vault_asset_bytes(candidate)
        except HTTPException:
            continue
        if media_type not in _VAULT_ASSET_RESIZE_MIMES:
            continue
        meta = _asset_meta_for(content, media_type, mtime_ms)
        if meta is not None:
            result[candidate] = meta
    return result


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
    note = to_note(target)
    knowledge.enqueue_refresh(runtime_dir=RuntimePaths.from_env().runtime_dir)
    return note


class RenameRequest(BaseModel):
    path: str = Field(..., max_length=260)
    new_path: str = Field(..., max_length=260)


@app.post("/api/rename")
def rename(payload: RenameRequest) -> dict[str, object]:
    try:
        changed = vaultops.rename_note(VAULT_DIR, payload.path, payload.new_path)
    except vaultops.VaultOpError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    knowledge.enqueue_refresh(runtime_dir=RuntimePaths.from_env().runtime_dir)
    return {"path": changed[0], "changed": changed}


@app.delete("/api/notes/{note_path:path}")
def delete_note(note_path: str) -> dict[str, object]:
    try:
        changed = vaultops.delete_note(VAULT_DIR, note_path)
    except vaultops.VaultOpError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    knowledge.enqueue_refresh(runtime_dir=RuntimePaths.from_env().runtime_dir)
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
    note = to_note(target)
    knowledge.enqueue_refresh(runtime_dir=RuntimePaths.from_env().runtime_dir)
    return note


class AccountRotateIn(BaseModel):
    account: str | None = Field(default=None, max_length=120)


@app.get("/api/accounts")
def get_accounts() -> dict[str, object]:
    return accounts.snapshot()


@app.get("/api/providers/health")
def get_provider_health(refresh: bool = False) -> dict[str, dict[str, object]]:
    if refresh:
        return PROVIDER_HEALTH.refresh(min_interval_seconds=5.0)
    return PROVIDER_HEALTH.snapshot()


@app.post("/api/accounts/rotate")
async def rotate_account(body: AccountRotateIn) -> dict[str, object]:
    force_target = (body.account or "").strip() or None
    result = await asyncio.to_thread(
        _supervisor_request,
        "fleet/rotate_codex",
        {
            "account": force_target,
            "operation_id": str(uuid4()),
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail="Agent supervisor returned a bad rotation response",
        )
    return result


app.include_router(terminal.router)
app.include_router(uistate.router)


def _dashboard_page_payload() -> dict:
    registry: dict = {}
    try:
        value = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            registry = value
    except (OSError, ValueError):
        pass
    statuses: dict[str, dict] = {}
    for path in _agent_status_paths():
        status = read_agent_status(path.stem)
        if status:
            statuses[path.stem] = status
    archived = list_archived(limit=None, latest_per_ticket=False)
    return dashboard.build_page_payload(registry, statuses, archived)


dashboard_page.register_payload_builder(_dashboard_page_payload)
app.include_router(dashboard_page.router)

mount_frontend_static(app)
