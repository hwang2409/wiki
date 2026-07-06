from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


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


def to_summary(path: Path) -> NoteSummary:
    content = read_note(path)
    note_id = note_id_for(path)
    return NoteSummary(
        id=note_id,
        path=note_id,
        title=extract_title(content, note_id),
        excerpt=excerpt_for(content),
        updated_at=updated_at_for(path),
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
    elif not body.lstrip().startswith("#"):
        body = f"# {title.strip()}\n\n{body}\n"
    else:
        body = f"{body}\n"

    if len(body.encode("utf-8")) > MAX_NOTE_BYTES:
        raise HTTPException(status_code=413, detail="Note is too large")

    return body


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
